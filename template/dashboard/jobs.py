"""后台任务：漏斗刷新、实验回收。单线程串行执行（数仓并发不可控，串行最稳）。"""
import datetime as dt
import functools
import threading
import time
import traceback

from . import config as C, sqlgen, stats, store
from . import sources
from .sources import Cancelled, SourceTimeout, open_source

_wake = threading.Event()
# 取消：_cur = 正在跑的任务号；_want = 被要求取消、但工作线程可能还没接手的任务号
_lock = threading.Lock()
_cur, _want = None, set()


def ymd(d):
    return int(d.strftime("%Y%m%d"))


def parse(d):
    return dt.datetime.strptime(str(d), "%Y%m%d").date()


def yesterday():
    return ymd(dt.date.today() - dt.timedelta(days=1))


def days_between(d0, d1):
    d = parse(d0)
    while ymd(d) <= d1:
        yield ymd(d)
        d += dt.timedelta(days=1)


def submit(kind, params):
    jid = store.enqueue(kind, params)
    _wake.set()
    return jid


def _src(jid, log):
    return open_source(log, trace=(lambda q: store.job_query(jid, q)) if jid else None)


# ─────────────────────────── 漏斗 ───────────────────────────

def _flow_rows(src, view, d0, d1, log):
    """超时自动对半拆窗口重试（单日仍超时才报错）。"""
    scope, subs = sqlgen.split_view(view)
    try:
        return src.dicts(sqlgen.flow_sql(scope, d0, d1, subs), "%s 漏斗 %d~%d" % (view, d0, d1))
    except SourceTimeout:
        if d0 == d1:
            raise
        mid = ymd(parse(d0) + (parse(d1) - parse(d0)) / 2)
        log("⚠ 超时，拆成 %d~%d 与 %d~%d 重试" % (d0, mid, ymd(parse(mid) + dt.timedelta(days=1)), d1))
        return _flow_rows(src, view, d0, mid, log) + _flow_rows(src, view, ymd(parse(mid) + dt.timedelta(days=1)), d1, log)


def run_funnel(jid, p):
    log = lambda s: store.job_log(jid, s)
    d0, d1 = int(p["start"]), int(p["end"])
    base, _ = sqlgen._tiers()
    with _src(jid, log) as src:
        for view in p.get("views") or [s["key"] for s in C.cfg["scopes"]]:
            scope, subs = sqlgen.split_view(view)
            log("── %s %d~%d" % (view, d0, d1))
            flow = {}
            for r in _flow_rows(src, view, d0, d1, log):
                d = sqlgen.to_ymd(r.pop("d"))
                flow[d] = {k.lower(): int(v or 0) for k, v in r.items()}
            bd = []
            bsql = sqlgen.breakdown_sql(scope, d0, d1, subs)
            if bsql:
                for r in src.dicts(bsql, "%s 分桶" % view):
                    for t in base:
                        bd.append({"d": sqlgen.to_ymd(r["d"]), "b": r["b"], "tier": t["key"],
                                   "expose": r.get("e__" + t["key"]), "click": r.get("c__" + t["key"])})
            dsql = sqlgen.dau_sql(scope, d0, d1, subs)
            dau = {sqlgen.to_ymd(r["d"]): int(r["dau"]) for r in src.dicts(dsql, "%s 活跃设备" % view)} if dsql else {}
            statuses = completeness(view, d0, d1, flow)
            for d, (ok, note) in sorted(statuses.items()):
                if not ok:
                    log("⚠ %s %d 不完整：%s" % (view, d, note))
            store.save_funnel(view, flow, bd, dau, statuses)


def completeness(view, d0, d1, flow):
    """无数据 / 校验步 UV 低于此前 7 天中位数 × min_ratio → 视为分区未产出完整。"""
    comp = C.cfg["funnel"]["completeness"]
    tier = C.cfg["funnel"]["tiers"][0]["key"]
    key = "uv__%s__%s" % (comp["step"], tier)
    by = {d: r.get(key, 0) for d, r in flow.items()}
    out = {}
    for d in days_between(d0, d1):
        hist = [v for k, v in sorted(by.items()) if k < d][-7:] or store.anchor_history(view, d, key)
        med = sorted(hist)[len(hist) // 2] if hist else None
        v = by.get(d)
        if not v:
            out[d] = (False, "无数据")
        elif med and v < comp["min_ratio"] * med:
            out[d] = (False, "%s UV %d < 前 7 日中位数 %d 的 %d%%" % (comp["step"], v, med, comp["min_ratio"] * 100))
        else:
            out[d] = (True, "")
    return out


# ─────────────────────────── 实验 ───────────────────────────

STAT_F = ("sx", "sy", "sxx", "syy", "sxy")


def _lower(r):
    return {str(k).lower(): v for k, v in r.items()}


def _ratio_payload(rows, k):
    return {str(r["grp"]): dict(n=int(r["n"]), **{f: float(r.get("%s_%s" % (f, k)) or 0) for f in STAT_F}) for r in map(_lower, rows)}


def cum_cutoffs(d0, d1):
    n = (parse(d1) - parse(d0)).days + 1
    return [(c, ymd(parse(d0) + dt.timedelta(days=c - 1))) for c in C.cfg["experiment"]["cum_days"] if c < n]


def _kind(m):
    return m.get("kind", "ratio")


def dist_keys(mm, keys):
    """出分布箱型图的指标：人均类（分母按设备 UV、分子是次数 / 求和），每设备取值才有分布。"""
    return [k for k in keys if k in mm and _kind(mm[k]) == "ratio"
            and mm[k]["numerator"].get("agg", "uv") != "uv" and mm[k]["denominator"].get("agg", "uv") == "uv"]


def _run_dist(src, e, mm, gids, subs, d0, d1, caps):
    """设备集合与全窗口比率一致（同一批指标拼认组事件），所以各组进组累计 = 分流检查的设备数。"""
    ratio = [{"key": k, "name": mm[k]["name"], "def": mm[k]} for k in e["metric_keys"] if k in mm and _kind(mm[k]) == "ratio"]
    dk = dist_keys(mm, e["metric_keys"])
    days = list(days_between(d0, d1))
    rows = [_lower(r) for r in src.dicts(sqlgen.experiment_dist_sql(e["scope"], gids, d0, d1, ratio, caps, subs, dk, days), "分布 / 进组趋势")]
    num = lambda v: float(v) if v is not None else None
    out = {}
    for r in rows:
        out[str(r["grp"])] = {
            "metrics": {k: {"q": [num(r.get("q%d_%s" % (i, k))) for i in range(len(sqlgen.DIST_Q))],
                            "avg": num(r.get("avg_%s" % k)), "n": int(r.get("n_%s" % k) or 0)} for k in dk},
            "entry": {str(d): int(r.get("e_%d" % d) or 0) for d in days}}
    store.save_exp_results(e["id"], "@dist", out, d1)


# 只做可视化 / 辅助读数、不进结论提要的补算项：不挡结论
VIS_KEYS = ("@dist", "@cuped", "@seg")
VIS_LABELS = ("分布 / 进组趋势", "CUPED", "分群下钻")


def md_(d):
    s = str(d)
    return "%s-%s" % (s[4:6], s[6:])


def cuped_window(e, exps=None):
    """协变量窗口 = 上线前 cuped_pre_days 天。同一范围内有别的实验用过本实验的分组号、且跑在这段时间里的（分组号被复用），
    窗口起点挪到那个实验结束日的次日：那段时间同一批设备已按上一个实验分组、用着它的策略，协变量会和分组相关，CUPED 就有偏。
    返回 (p0, p1, 说明)；p0 > p1 = 没有干净的窗口，不做 CUPED。"""
    cfg = C.cfg["experiment"]
    pre_n, min_n = int(cfg["cuped_pre_days"]), int(cfg["cuped_min_days"])
    launch = parse(e.get("launch") or e["start"])
    p1 = ymd(launch - dt.timedelta(days=1))
    if pre_n <= 0:
        return ymd(launch), p1, "配置里关闭了 CUPED（experiment.cuped_pre_days = 0）"
    p0 = ymd(launch - dt.timedelta(days=pre_n))
    ids = {g["id"] for g in e["groups"]}
    hit = [o for o in (exps if exps is not None else store.experiments()) if o["id"] != e["id"] and o["scope"] == e["scope"]
           and ids & {g["id"] for g in o["groups"]} and int(o["start"]) <= p1 and int(o.get("last_end") or o["end"]) >= p0]
    if not hit:
        return p0, p1, ""
    o = max(hit, key=lambda o: int(o.get("last_end") or o["end"]))
    oend = o.get("last_end") or o["end"]
    p0 = ymd(parse(oend) + dt.timedelta(days=1))
    if (parse(p1) - parse(p0)).days + 1 < min_n:
        return p0, ymd(parse(p0) - dt.timedelta(days=1)), "上线前 %d 天内同一批分组号在「%s」里跑过（到 %s），剩下的干净窗口不足 %d 天" % (
            pre_n, o["name"], md_(oend), min_n)
    return p0, p1, "上线前 %d 天内同一批分组号在「%s」里跑过（到 %s），协变量窗口从 %s 起" % (pre_n, o["name"], md_(oend), md_(p0))


def _cuped_window_changed(e):
    """已存的 CUPED 协变量窗口和按现在的规则 / 配置算出来的不一样 → 要重算。"""
    raw = next((r["payload"] for r in e.get("results") or [] if r["metric_key"] == "@cuped" and r["grp"] == "@pre"), None)
    if raw is None:
        return False
    p0, p1, _ = cuped_window(e)
    return (raw.get("p0"), raw.get("p1")) != (p0, p1)


def _node_bucket(d0, d):
    """数据到 d 时所处的节点段 = 已过的最后一个节点（判止损 / 中期读数 / 出结论）；判止损之前 = 0。"""
    n = (parse(d) - parse(d0)).days + 1
    cad = C.cfg["experiment"]["cadence"]
    return max([k for k in (cad["stop_loss"], cad["interim"], cad["final"]) if k <= n] or [0])


def _ratio_ms(e, mm):
    return [{"key": k, "name": mm[k]["name"], "def": mm[k]} for k in e["metric_keys"] if k in mm and _kind(mm[k]) == "ratio"]


def _run_cuped(src, e, mm, gids, subs, d0, d1, caps):
    """CUPED 原料（全窗口累计）：实验期设备层 × 上线前同口径设备层。"""
    ms = sqlgen.cuped_metrics(_ratio_ms(e, mm))
    if not ms:
        return
    p0, p1, why = cuped_window(e)
    if p0 > p1:
        store.save_exp_results(e["id"], "@cuped", {"@pre": {"p0": p0, "p1": p1, "skip": why}}, d1)
        return
    rows = src.dicts(sqlgen.experiment_cuped_sql(e["scope"], gids, d0, d1, ms, caps, subs, p0, p1), "CUPED ×%d" % len(ms))
    f = ["s" + a for a in sqlgen.CUPED_V] + ["s" + a + b for i, a in enumerate(sqlgen.CUPED_V) for b in sqlgen.CUPED_V[i:]]
    out = {str(r["grp"]): {m["key"]: dict(n=int(r["n"]), **{x: float(r.get("%s_%s" % (x, m["key"])) or 0) for x in f})
                           for m in ms} for r in map(_lower, rows)}
    out["@pre"] = {"p0": p0, "p1": p1, "cut": why, "d1": d1}
    store.save_exp_results(e["id"], "@cuped", out, d1)


def _run_seg(src, e, mm, gids, subs, d0, d1, caps):
    """分群充分统计量：每个分群列一次查询。"""
    ms, segs = _ratio_ms(e, mm), C.cfg["experiment"]["segments"]
    if not ms or not segs:
        return
    out = {}
    for sg in segs:
        rows = src.dicts(sqlgen.experiment_seg_sql(e["scope"], gids, d0, d1, ms, caps, subs, sg), "分群 %s" % sg["label"])
        out[sg["column"]] = [dict({"grp": str(r["grp"]), "n": int(r["n"]), "v": str(r["d_v"])},
                                  **{"%s_%s" % (f, m["key"]): float(r.get("%s_%s" % (f, m["key"])) or 0) for m in ms for f in STAT_F})
                             for r in map(_lower, rows)]
    store.save_exp_results(e["id"], "@seg", {"_": out, "d1": d1}, d1)


def run_experiment(jid, p):
    log = lambda s: store.job_log(jid, s)
    e = store.experiment(int(p["exp_id"]))
    if not e:
        raise RuntimeError("实验不存在")
    store.update_experiment(e["id"], status="running", error=None)
    try:
        _run_experiment(e, log, p.get("only"), p.get("only_daily"), jid=jid, refresh_vis=p.get("refresh_vis"))
    except Cancelled:
        raise                             # 实验状态由 _after_cancel 统一处理
    except Exception as ex:
        store.update_experiment(e["id"], status="failed", error=str(ex)[:800])
        raise


def _run_experiment(e, log, only=None, only_daily=None, jid=None, refresh_vis=False):
    """only / only_daily 不为 None = 编辑后的增量补算：只算新增指标 / 新增 By天，窗口沿用已跑到的日期。"""
    incr = only is not None
    mm = store.metric_map()
    metrics = []
    for k in (only if incr else e["metric_keys"]):
        if k not in mm:
            log("跳过已删除的指标 %s" % k)
            continue
        metrics.append({"key": k, "name": mm[k]["name"], "def": mm[k]})
    gids = [g["id"] for g in e["groups"]]
    subs = e.get("subs") or []
    d0 = int(e["start"])
    d1 = min(int(e["end"]), yesterday())
    if incr and e.get("last_end"):
        d1 = int(e["last_end"])
        log("增量补算：新增指标 %s；新增 By天 %s；窗口沿用 %d~%d" % (only or "无", only_daily or "无", d0, d1))
    if d1 < d0:
        raise RuntimeError("窗口内还没有可用数据（开窗日在今天或之后）")
    caps = dict(e.get("caps") or {})
    pre_n = int(C.cfg["experiment"]["cap_pre_days"])
    launch = int(e.get("launch") or d0)
    pre1, pre0 = ymd(parse(launch) - dt.timedelta(days=1)), ymd(parse(launch) - dt.timedelta(days=pre_n))
    with _src(jid, log) as src:
        # 1) 上线前 P99 封顶（只算一次，写死）
        for m in metrics:
            if m["def"].get("cap") == "p99_pre" and m["key"] not in caps:
                r = src.dicts(sqlgen.cap_sql(e["scope"], pre0, pre1, m["def"]["numerator"], m["def"]["denominator"], subs), "P99 " + m["name"])
                r = [_lower(x) for x in r]
                if not r or r[0]["cap"] is None:
                    raise RuntimeError("上线前 %d 天无数据，算不出 P99 封顶" % pre_n)
                caps[m["key"]] = float(r[0]["cap"])
                log("P99 封顶 %s = %s（上线前 %d~%d 单设备单天，%s 设备-天）" % (m["name"], caps[m["key"]], pre0, pre1, r[0]["n_dev"]))
        store.update_experiment(e["id"], caps=caps)
        # 2) 比率指标：一次扫描全部算完
        ratio = [m for m in metrics if _kind(m["def"]) == "ratio"]
        if ratio:
            rows = src.dicts(sqlgen.experiment_ratio_sql(e["scope"], gids, d0, d1, ratio, caps, subs), "比率指标 ×%d" % len(ratio))
            for m in ratio:
                store.save_exp_results(e["id"], m["key"], _ratio_payload(rows, m["key"]), d1)
    # 3) 慢的都不挡全窗口读数：累计行 / By天 / 分位数排一个补算任务在后面
    daily = set(only_daily if incr else e.get("daily_keys") or [])
    dm = [{"key": k, "def": mm[k]} for k in e["metric_keys"] if k in daily and k in mm]
    any_ratio = any(_kind(mm[k]) == "ratio" for k in e["metric_keys"] if k in mm)
    pending = ["@cum"] if cum_cutoffs(d0, d1) and any_ratio else []
    # 辅助读数（不进判读）：分布每次续跑都算；CUPED / 分群各要整窗口再扫一遍，只在跨节点、冻结、缺 / 失败、新增指标时重算
    have = {r["metric_key"]: r["run_end"] for r in e.get("results") or []}
    new_ratio = any(_kind(m["def"]) == "ratio" for m in metrics)
    frozen = d1 >= int(e["end"])
    for k in VIS_KEYS if any_ratio else ():
        if k == "@seg" and not C.cfg["experiment"]["segments"]:
            continue
        heavy = k != "@dist"
        if refresh_vis or k not in have or e.get("vis_error") or (incr and new_ratio) or \
                (k == "@cuped" and _cuped_window_changed(e)) or (not heavy and not incr) or \
                (heavy and not incr and (frozen and int(have[k]) != d1 or _node_bucket(d0, int(have[k])) != _node_bucket(d0, d1))):
            pending.append(k)
    pending += [m["key"] + "@day" for m in dm if _kind(m["def"]) == "ratio"]
    pending += [m["key"] for m in metrics if _kind(m["def"]) == "quantile"]
    pending += [m["key"] + "@day" for m in dm if _kind(m["def"]) == "quantile"]
    store.update_experiment(e["id"], status="frozen" if frozen else "active", last_end=d1, error=None, pending=pending)
    log("完成：窗口 %d~%d%s" % (d0, d1, "，已到结束日，冻结" if frozen else "，结束日前每日续跑"))
    if pending:
        fid = submit("exp_followup", {"exp_id": e["id"], "run_end": d1})
        log("补算 ×%d（累计行 / By天 / 分位数）已排到任务 #%d" % (len(pending), fid))


def _quant_payload(rows, m, daily):
    nq = len(m["def"]["quantiles"])
    out = {}
    for r in map(_lower, rows):
        v = {"n_events": int(r["n_events"]), "n_dev": int(r["n_dev"]), "q": [r.get("q%d" % i) for i in range(nq)]}
        if daily:
            out.setdefault(str(r["grp"]), {})[str(sqlgen.to_ymd(r["d"]))] = v
        else:
            out[str(r["grp"])] = v
    return out


def run_exp_followup(jid, p):
    """补算 前 N 天累计 / By天 / 分位数；单项失败不影响其他，做完一项从 pending 里划掉一项。"""
    log = lambda s: store.job_log(jid, s)
    e = store.experiment(int(p["exp_id"]))
    if not e:
        raise RuntimeError("实验不存在")
    mm = store.metric_map()
    gids = [g["id"] for g in e["groups"]]
    subs = e.get("subs") or []
    d0, d1 = int(e["start"]), int(p["run_end"])
    pending = [k for k in e["pending"] if k in ("@cum",) + VIS_KEYS or k.split("@")[0] in mm]
    caps = dict(e.get("caps") or {})
    partial, stopped = [], False

    def done(keys):
        nonlocal pending
        pending = [k for k in pending if k not in keys]
        store.update_experiment(e["id"], pending=pending)

    try:
        with _src(jid, log) as src:
            if "@cum" in pending:
                try:
                    have = {r["metric_key"] for r in store.experiment(e["id"])["results"]}
                    ratio = [k for k in e["metric_keys"] if k in mm and _kind(mm[k]) == "ratio"]
                    for c, cend in cum_cutoffs(d0, d1):
                        miss = [k for k in ratio if "%s@c%d" % (k, c) not in have]
                        if miss:
                            ms = [{"key": k, "name": mm[k]["name"], "def": mm[k]} for k in miss]
                            rows = src.dicts(sqlgen.experiment_ratio_sql(e["scope"], gids, d0, cend, ms, caps, subs), "前 %d 天累计 ×%d" % (c, len(ms)))
                            for k in miss:
                                store.save_exp_results(e["id"], "%s@c%d" % (k, c), _ratio_payload(rows, k), cend)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("前 N 天累计：%s" % str(ex)[:200])
                    log("⚠ 前 N 天累计失败，其余照常：%s" % ex)
                done(["@cum"])
            if "@dist" in pending:
                try:
                    _run_dist(src, e, mm, gids, subs, d0, d1, caps)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("分布 / 进组趋势：%s" % str(ex)[:200])
                    log("⚠ 分布 / 进组趋势失败，其余照常：%s" % ex)
                done(["@dist"])
            # CUPED / 分群下钻：失败不影响主读数
            for k, fn, label in (("@cuped", _run_cuped, "CUPED"), ("@seg", _run_seg, "分群下钻")):
                if k in pending:
                    try:
                        fn(src, e, mm, gids, subs, d0, d1, caps)
                    except Cancelled:
                        raise
                    except Exception as ex:
                        partial.append("%s：%s" % (label, str(ex)[:200]))
                        log("⚠ %s 失败，其余照常：%s" % (label, ex))
                    done([k])
            rk = [k for k in pending if k.endswith("@day") and _kind(mm[k[:-4]]) == "ratio"]
            if rk:
                ms = [{"key": k[:-4], "name": mm[k[:-4]]["name"], "def": mm[k[:-4]]} for k in rk]
                try:
                    rows = [_lower(r) for r in src.dicts(sqlgen.experiment_daily_ratio_sql(e["scope"], gids, d0, d1, ms, caps, subs), "By天 比率 ×%d" % len(ms))]
                    for m in ms:
                        payload = {}
                        for r in rows:
                            vals = {f: float(r.get("%s_%s" % (f, m["key"])) or 0) for f in STAT_F}
                            payload.setdefault(str(r["grp"]), {})[str(sqlgen.to_ymd(r["d"]))] = dict(n=int(r["n"]), **vals)
                        store.save_exp_results(e["id"], m["key"] + "@day", payload, d1)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("By天 比率：%s" % str(ex)[:200])
                    log("⚠ By天 比率失败，其余照常：%s" % ex)
                done(rk)
            for k in list(pending):
                base, daily = (k[:-4], True) if k.endswith("@day") else (k, False)
                m = {"key": base, "name": mm[base]["name"], "def": mm[base]}
                label = m["name"] + ("（By天）" if daily else "")
                try:
                    rows = src.dicts(sqlgen.experiment_quantile_sql(e["scope"], gids, d0, d1, m, subs, daily), label)
                    store.save_exp_results(e["id"], k, _quant_payload(rows, m, daily), d1)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("%s：%s" % (label, str(ex)[:200]))
                    log("⚠ %s 失败，其余照常：%s" % (label, ex))
                done([k])
    except Cancelled:
        stopped = True
        raise
    except Exception as ex:
        partial.append(str(ex)[:300])
        raise
    finally:
        # 只是辅助读数（分布 / CUPED / 分群）失败：记在 vis_error，不写 error（error 会挡结论提要）
        main = [x for x in partial if not x.startswith(VIS_LABELS)]
        vis = [x for x in partial if x.startswith(VIS_LABELS)]
        err = "补算失败（可点重跑）：" + "；".join(main) if main else None
        if stopped:
            err = "补算已取消（已出的结果保留，可点重跑）" + ("；另有失败：" + "；".join(main) if main else "")
        store.update_experiment(e["id"], pending=[], error=err, vis_error="；".join(vis) if vis else None)
    log("补算完成")


# ─────────────────────────── 读数组装 ───────────────────────────

def _unit(m):
    return m.get("unit") or ("pct" if m.get("numerator", {}).get("agg", "uv") == "uv" or
                             m.get("numerator", {}).get("agg") == m.get("denominator", {}).get("agg") else "num")


ROLE_NAME = {"north_star": "北极星", "process": "过程", "guardrail": "护栏"}


def _row_stats(per, ctrl, trts, lower):
    alpha = float(C.cfg["experiment"]["alpha"])
    est, groups, vs = {}, {}, {}
    for g, s in per.items():
        if g == "X":
            continue
        r, var = stats.ratio_stats(s["n"], s["sx"], s["sy"], s["sxx"], s["syy"], s["sxy"])
        est[g] = (r, var)
        groups[g] = {"value": r, "num": s["sx"], "den": s["sy"], "n": s["n"]}
    c = ctrl[0] if ctrl else None
    for t in trts:
        if c in est and t in est and est[c][0] is not None and est[t][0] is not None:
            x = stats.compare(est[c], est[t], alpha)
            if not x:
                continue
            x["z"] = x["diff"] / x["se"] if x["se"] else None
            sig = x["p"] is not None and x["p"] < alpha
            x["read"] = ("显著正向" if (x["diff"] > 0) != lower else "显著负向") if sig else None
            vs[t] = x
    return groups, vs


@functools.lru_cache(maxsize=256)
def _seq_bound(n, planned, final, harm, alpha, looks_cfg):
    looks = [d for d in looks_cfg if d < n and d < planned] + [n]
    fr = [min(d / float(planned), 1.0) for d in looks]
    if final:
        fr[-1] = 1.0
    return stats.gs_bounds(fr, alpha, "pocock" if harm else "obf")[-1]


def seq_bound(n, planned, final, harm):
    """第 n 天累计读数的序贯 |z| 阈值：看数节点 = 早于 n 的判止损 / 中期读数节点 + 本次；信息比例按天数 ÷ 计划窗口天数近似，
    窗口已结束（final）本次按 1。收益侧 O'Brien-Fleming 型消耗（早期严），变差侧 Pocock 型（早期也能报警）；整体双侧 α = experiment.alpha。"""
    cfg = C.cfg["experiment"]
    cad = cfg["cadence"]
    return _seq_bound(n, planned, final, harm, float(cfg["alpha"]), (cad["stop_loss"], cad["interim"]))


def adjust_reads(e, tables):
    """在原判读（p < alpha）之外加两层修正标注，原 read 不动：
    ① 多次看数（序贯）：本行 |z| 是否过了该天数的序贯阈值；
    ② 多指标多组（Benjamini-Hochberg，FDR = alpha）：同一累计行上，北极星以外的比率指标 × 实验组一起修正；北极星是事先指定的主指标，不进族。
    c["adj"] = {bound, seq, bh, ok, why}；ok = 原判读显著且两层都过。"""
    if not e.get("last_end"):
        return tables
    alpha = float(C.cfg["experiment"]["alpha"])
    d0, d1, d2 = parse(e["start"]), parse(e["last_end"]), parse(e["end"])
    planned = (d2 - d0).days + 1
    final = e.get("status") == "frozen" or d1 >= d2
    fam = {}
    for t in tables:
        if t["kind"] != "ratio":
            continue
        for r in t["rows"]:
            if r["type"] not in ("cum", "total"):
                continue
            for g, c in r["vs"].items():
                if c.get("z") is None:
                    continue
                harm = (c["diff"] > 0) == bool(t["lower_is_better"])
                b = seq_bound(r["days"], planned, final and r["type"] == "total", harm)
                c["adj"] = {"bound": b, "seq": abs(c["z"]) >= b, "bh": None}
                if t["role"] != "north_star":
                    fam.setdefault(r["days"], []).append(c)
    for cs in fam.values():
        for c, ok in zip(cs, stats.bh_pass([c["p"] for c in cs], alpha)):
            c["adj"]["bh"] = ok
    for t in tables:
        for r in t["rows"]:
            for c in r["vs"].values():
                a = c.get("adj")
                if not a:
                    continue
                why = [] if a["seq"] else ["多次看数阈值 |z| ≥ %.2f" % a["bound"]]
                if a["bh"] is False:
                    why.append("多指标修正（BH）")
                a["ok"] = bool(c.get("read")) and not why
                a["why"] = "未过" + "、".join(why) if why else ""
    return tables


THIN_DAY = 0.3


def thin_days(day):
    """逐日样本不足的天：任一组当天设备数 < 该组窗口内单日最高值的 30%（如实验中途关停后的残留流量）。只影响逐日图不画点，累计不变。
    参照取最高值不取中位数：关停后残留天数过半时，中位数本身就被拉低，会漏判。"""
    out = set()
    for g, v in day.items():
        ns = [s["n"] for s in v.values() if isinstance(s, dict) and s.get("n")]
        if g == "X" or not ns:
            continue
        top = max(ns)
        out |= {d for d, s in v.items() if isinstance(s, dict) and (s.get("n") or 0) < THIN_DAY * top}
    return out


def _tables(e, mm, res, ctrl, trts):
    """每个指标一张「总分」表：逐日行 → 前 N 天累计 → 全窗口累计。"""
    if not e.get("last_end"):
        return []
    d0, d1 = int(e["start"]), int(e["last_end"])
    ndays = (parse(d1) - parse(d0)).days + 1
    pend = set(e.get("pending") or [])
    out = []
    for k in e["metric_keys"]:
        m = mm.get(k)
        if not m:
            continue
        kind, lower = _kind(m), bool(m.get("lower_is_better"))
        waits = (["全窗口"] if k in pend else []) + (["By天"] if k + "@day" in pend else []) + (["前 N 天累计"] if "@cum" in pend and kind == "ratio" else [])
        t = {"key": k, "name": m["name"], "role": m.get("role", "process"), "group": ROLE_NAME[m.get("role", "process")],
             "kind": kind, "unit": _unit(m), "lower_is_better": lower, "note": m.get("note", ""), "waits": waits,
             "has_daily": k in (e.get("daily_keys") or []), "rows": []}

        def add(label, typ, per, date=None, days=None):
            if not per:
                return
            if kind == "ratio":
                g, vs = _row_stats(per, ctrl, trts, lower)
            else:
                g, vs = {gg: s for gg, s in per.items() if gg != "X"}, {}
            t["rows"].append({"label": label, "type": typ, "date": date, "days": days, "groups": g, "vs": vs})

        day = res.get(k + "@day") or {}
        thin = thin_days(day)
        for d in sorted({d for v in day.values() for d in v}):
            add("%s-%s" % (d[4:6], d[6:]), "day", {g: v[d] for g, v in day.items() if d in v}, int(d))
            if d in thin and t["rows"]:
                t["rows"][-1]["thin"] = True
        if kind == "ratio":
            for c, _ in cum_cutoffs(d0, d1):
                add("前 %d 天累计" % c, "cum", res.get("%s@c%d" % (k, c)), days=c)
        add("累计（%d 天）" % ndays, "total", res.get(k), days=ndays)
        out.append(t)
    return out


def experiment_view(e, base=None):
    """base = 临时当基准的组（只换比较对象，统计口径不变；默认 = 对照组）。"""
    mm = store.metric_map()
    ids = [g["id"] for g in e["groups"]]
    ctrl = [base] if base in ids else [g["id"] for g in e["groups"] if g["role"] == "control"]
    trts = [i for i in ids if i not in ctrl]
    res = {}
    for r in e["results"]:
        res.setdefault(r["metric_key"], {})[r["grp"]] = r["payload"]
    counts = None
    for k in e["metric_keys"]:
        if k in res and _kind(mm.get(k, {})) == "ratio":
            counts = {g: s["n"] for g, s in res[k].items()}
            break
    srm = None
    if counts:
        ids = [g["id"] for g in e["groups"]]
        w = [float(g.get("weight") or 1) for g in e["groups"]]
        obs = [counts.get(i, 0) for i in ids]
        chi, p = stats.srm(obs, w)
        srm = {"counts": dict(zip(ids, obs)), "conflict": counts.get("X", 0), "chi2": chi, "p": p}
    tables = adjust_reads(e, _tables(e, mm, res, ctrl, trts))
    # 结论提要始终按真对照组出，不随临时基准变
    c0 = [g["id"] for g in e["groups"] if g["role"] == "control"]
    t0 = [i for i in ids if i not in c0]
    st = tables if c0 == ctrl else adjust_reads(e, _tables(e, mm, res, c0, t0))
    dist = (res.get("@dist") or {}).get
    return {"srm": srm, "tables": tables, "summary": summary(e, srm, st, t0), "base": ctrl[0] if ctrl else None, "trts": trts,
            "cuped": cuped_view(e, res.get("@cuped"), tables, ctrl, trts),
            "seg": seg_view(e, _seg_raw(res.get("@seg")), tables, ctrl, trts),
            "dist": {g: dist(g) for g in ids if dist(g)} or None, "dist_keys": dist_keys(mm, e["metric_keys"])}


def cuped_view(e, raw, tables, ctrl, trts):
    """CUPED 读数（全窗口累计）：{指标: {组: 调整后读数}}；判读口径同原表（p < alpha + 指标方向），另跑序贯阈值。只并列展示，不改原读数。"""
    if not raw or not ctrl:
        return None
    alpha = float(C.cfg["experiment"]["alpha"])
    pre = raw.get("@pre") or {}
    p0, p1, why = cuped_window(e)
    if (pre.get("p0"), pre.get("p1")) != (p0, p1):
        return {"pre": pre, "skip": "协变量窗口规则已更新，等下次重跑后再看（%s）" % (why or "窗口有变"), "metrics": {}}
    if pre.get("skip"):
        return {"pre": pre, "skip": pre["skip"], "metrics": {}}
    d1 = int(pre.get("d1") or e["last_end"])
    n = (parse(d1) - parse(e["start"])).days + 1
    planned = (parse(e["end"]) - parse(e["start"])).days + 1
    out = {}
    for t in tables:
        if t["kind"] != "ratio":
            continue
        k = t["key"]
        c0 = stats.cuped_group((raw.get(ctrl[0]) or {}).get(k) or {})
        for g in trts:
            x = stats.cuped_compare(c0, stats.cuped_group((raw.get(g) or {}).get(k) or {}), alpha)
            if not x:
                continue
            harm = (x["diff"] > 0) == bool(t["lower_is_better"])
            x["read"] = ("显著正向" if not harm else "显著负向") if x["p"] < alpha else None
            x["seq"] = abs(x["z"]) >= seq_bound(n, planned, d1 >= int(e["end"]), harm)
            out.setdefault(k, {})[g] = x
    return {"pre": pre, "metrics": out, "d1": d1, "days": n, "stale": d1 != int(e["last_end"])} if out else None


def _seg_raw(r):
    """@seg 存成 {"_": {分群列: 行}, "d1": 截止日}，按 grp 列落库后在 res 里是 {"_": ..., "d1": ...}。"""
    if not r or "_" not in r:
        return None
    return {"runs": r["_"], "d1": r.get("d1")}


def seg_view(e, raw, tables, ctrl, trts):
    """分群下钻（全窗口累计）：每个分群列 × 取值 × 指标 × 实验组出 Δ / z；同一维度内各群效应是否一致做 Cochran Q。
    下钻只用来解释，不进结论：同一维度内各群 p 值另做 BH，未过的不标显著。"""
    if not raw or not ctrl:
        return None
    cfg = C.cfg["experiment"]
    min_n, alpha = int(cfg["seg_min_n"]), float(cfg["alpha"])
    segs = {sg["column"]: sg for sg in cfg["segments"]}
    labels = C.cfg.get("labels") or {}
    sub = C.cfg.get("subscope") or {}
    ratio = [t for t in tables if t["kind"] == "ratio" and any(r["type"] == "total" for r in t["rows"])]
    dims_out = []
    for col, rows in raw["runs"].items():
        if col not in segs:                  # 配置里删掉的分群列不再展示
            continue
        acc = {}
        for r in rows:
            a = acc.setdefault((r["grp"], r["v"]), {"n": 0})
            a["n"] += r["n"]
            for t in ratio:
                for f in STAT_F:
                    kk = "%s_%s" % (f, t["key"])
                    a[kk] = a.get(kk, 0.0) + r.get(kk, 0.0)
        vals = sorted({v for _, v in acc}, key=lambda v: (v.startswith("@"), -sum(a["n"] for (g, vv), a in acc.items() if vv == v)))
        if len([v for v in vals if not v.startswith("@")]) < 2:     # 只有一个取值：拆了也没东西比
            continue
        mets = []
        for t in ratio:
            lower = bool(t["lower_is_better"])
            for g in trts:
                cells, eff = [], []
                for v in vals:
                    per = {gg: dict(n=a["n"], **{f: a.get("%s_%s" % (f, t["key"]), 0.0) for f in STAT_F})
                           for (gg, vv), a in acc.items() if vv == v and gg in (ctrl[0], g)}
                    small = min((per.get(x) or {}).get("n", 0) for x in (ctrl[0], g)) < min_n
                    _, vs = _row_stats(per, ctrl, [g], lower) if not small else ({}, {})
                    c = vs.get(g)
                    cell = {"val": v, "n": {x: (per.get(x) or {}).get("n", 0) for x in (ctrl[0], g)}, "small": small}
                    if c:
                        cell.update(diff=c["diff"], rel=c.get("rel"), z=c["z"], p=c["p"], read=c["read"])
                        eff.append((c["diff"], c["se"]))
                    cells.append(cell)
                for c, ok in zip(cells, stats.bh_pass([c.get("p") for c in cells], alpha)):
                    c["bh"] = ok
                h = stats.heterogeneity(eff)
                mets.append({"key": t["key"], "name": t["name"], "unit": t["unit"], "grp": g, "cells": cells,
                             "het": {"q": h[0], "df": h[1], "p": h[2]} if h else None})
        names = {sqlgen.SEG_NA: "未知", sqlgen.SEG_OTHER: "其他"}
        vlab = (sub.get("labels") or {}) if col == sub.get("column") else {}
        dims_out.append({"dim": col, "name": segs[col]["label"], "metrics": mets,
                         "labels": {v: names.get(v) or vlab.get(v) or labels.get(v, v) for v in vals}})
    d1 = int(raw.get("d1") or e["last_end"])
    return {"dims": dims_out, "min_n": min_n, "d1": d1, "stale": d1 != int(e["last_end"])}


def _fmt_delta(t, c):
    return ("%+.2fpp" % (c["diff"] * 100)) if t["unit"] != "num" else ("%+.1f%%" % (c["rel"] * 100))


def _fmt_mde(t, c):
    return ("±%.2fpp" % (c["mde_abs"] * 100)) if t["unit"] != "num" else ("±%.1f%%" % (c["mde_rel"] * 100))


def _fmt_ci(t, c):
    if t["unit"] != "num":
        return "[%+.2f, %+.2f]pp" % (c["ci_low"] * 100, c["ci_high"] * 100)
    return "[%+.1f%%, %+.1f%%]" % (c["rel_ci_low"] * 100, c["rel_ci_high"] * 100)


def nodes():
    c = C.cfg["experiment"]["cadence"]
    return ((c["check"], "查生效"), (c["stop_loss"], "判止损"), (c["interim"], "中期读数"), (c["final"], "出结论"))


def _stage(n, final):
    c = C.cfg["experiment"]["cadence"]
    if final:
        return "出结论"
    return "中期读数" if n >= c["interim"] else "判止损" if n >= c["stop_loss"] else "查生效"


def _next(d0, n, final):
    nxt = None if final else next(((k, name) for k, name in nodes() if k > n), None)
    return "第 %d 天（%s）%s" % (nxt[0], (d0 + dt.timedelta(days=nxt[0] - 1)).strftime("%m-%d"), nxt[1]) if nxt else ""


def stage_of(e, today=None):
    """列表用：按开窗日到「今天的前一天」算第 N 天。冻结 / 过了结束日 / 到了出结论节点 = 出结论。"""
    d0 = parse(e["start"])
    last = min(parse(e["end"]), (today or dt.date.today()) - dt.timedelta(days=1))
    n = (last - d0).days + 1
    if n < 1:
        return {"day": 0, "stage": "未开窗", "next": "第 1 天（%s）开始" % d0.strftime("%m-%d")}
    final = e.get("status") == "frozen" or n >= C.cfg["experiment"]["cadence"]["final"] or last >= parse(e["end"])
    return {"day": n, "stage": _stage(n, final), "next": _next(d0, n, final)}


def summary(e, srm, tables, trts):
    """结论提要：按实验节点挑累计行、固定规则出句子，不给采纳建议；逐日同向天数不作判据。"""
    cfg = C.cfg["experiment"]
    # 分布 / 进组趋势只做可视化、不进判读，不挡结论
    if e.get("status") in ("queued", "running") or [k for k in e.get("pending") or [] if k not in VIS_KEYS]:
        return {"state": "wait", "text": "待全窗口跑完再出结论"}
    if e.get("status") == "failed" or e.get("error"):
        return {"state": "wait", "text": "有指标跑数失败，补齐后再出结论"}
    if srm and srm["p"] is not None and srm["p"] < float(cfg["srm_p"]):
        return {"state": "block", "text": "分流不均（SRM p = %.1e），实验未正常生效，不读效果" % srm["p"]}
    if not e.get("last_end"):
        return {"state": "wait", "text": "还没跑数"}
    d0, d1 = parse(e["start"]), parse(e["last_end"])
    n = (d1 - d0).days + 1
    final = e.get("status") == "frozen" or n >= cfg["cadence"]["final"]
    stage = _stage(n, final)
    head = {"day": n, "stage": stage,
            "note": ("窗口已结束，共 %d 天%s" % (n, "（未满 %d 天）" % cfg["cadence"]["final"] if n < cfg["cadence"]["final"] else "")) if final else "",
            "next": _next(d0, n, final)}
    ratio = [t for t in tables if t["kind"] == "ratio"]
    sl = cfg["cadence"]["stop_loss"]
    pick = sl if stage == "判止损" else n
    row = {t["key"]: next((r for r in t["rows"] if r["type"] in ("cum", "total") and r.get("days") == pick), None) for t in ratio}
    north = [t for t in ratio if t["role"] == "north_star"]
    guard = [t for t in ratio if t["role"] == "guardrail"]
    vs = lambda t, g: (row[t["key"]] or {}).get("vs", {}).get(g)
    out = []
    for g in trts:
        lines = []
        if stage == "查生效":
            cnt = (srm or {}).get("counts", {})
            lines.append("实验组 %s 台设备进组（对照 %s 台），分流均衡；第 %d 天判止损前不读效果" % (
                format(cnt.get(g, 0), ","), format(sum(v for k, v in cnt.items() if k != g), ","), sl) if cnt.get(g)
                else "实验组没有设备进组，实验未生效，先查配置")
            out.append({"grp": g, "lines": lines})
            continue
        if stage == "判止损":
            neg = [(t, c) for t in north + guard for c in [vs(t, g)] if c and c["read"] == "显著负向"]
            # 判止损看修正后：变差侧序贯阈值（Pocock 型）+ 护栏 BH；名义显著但没过的单列一句、不提示止损
            hurt = [(t, c) for t, c in neg if (c.get("adj") or {}).get("ok", True)]
            for t, c in hurt:
                lines.append("%s 前 %d 天显著变差 %s（CI %s 整体在变差一侧），%s" % (
                    t["name"], sl, _fmt_delta(t, c), _fmt_ci(t, c),
                    "建议止损" if t["role"] == "north_star" else "建议先拆解（机制 / 分布 / 分母）确认是真劣化后止损"))
            for t, c in neg:
                if (t, c) not in hurt:
                    lines.append("%s 前 %d 天名义显著变差 %s（z = %+.2f），%s，先观察到第 %d 天" % (
                        t["name"], sl, _fmt_delta(t, c), c["z"], c["adj"]["why"], cfg["cadence"]["interim"]))
            if not any(vs(t, g) for t in north + guard):
                lines.append("前 %d 天累计还没算出来，点「重跑」补齐后再判" % sl)
            elif not neg:
                cs = [(t, vs(t, g)) for t in north]
                lines.append(("前 %d 天未见止损信号：" % sl) + "；".join(
                    "%s %s（CI %s）" % (t["name"], _fmt_delta(t, c), _fmt_ci(t, c)) for t, c in cs if c)
                    + ("；" if any(c for _, c in cs) else "") + "护栏未见显著劣化")
            lines.append("止损节点只看是否确定变差，不读收益")
            out.append({"grp": g, "lines": lines})
            continue

        def sig_line(t, c):
            return _sig_line(t, c) + ("（%s）" % c["adj"]["why"] if (c.get("adj") or {}).get("why") else "")

        def _sig_line(t, c):
            d = _fmt_delta(t, c)
            if abs(c["diff"]) < c["mde_abs"]:
                return "%s %s %s（z = %+.2f），但幅度低于可测下限 %s，量级不可信" % (
                    t["name"], c["read"][:2] + ("上升" if c["diff"] > 0 else "下降"), d, c["z"], _fmt_mde(t, c))
            return "%s%s %s（z = %+.2f）" % (t["name"], c["read"], d, c["z"])

        for t in north:
            c = vs(t, g)
            if not c:
                lines.append("%s 暂无累计读数" % t["name"])
            elif c["read"]:
                lines.append(sig_line(t, c))
            else:
                lines.append("%s未测出差异：%s（可测下限 %s）" % (t["name"], _fmt_delta(t, c), _fmt_mde(t, c)))
        if not north:
            lines.append("未选北极星指标")
        bad = [(t, c) for t in guard for c in [vs(t, g)] if c and c["read"] == "显著负向"]
        lines.append("；".join("%s 显著劣化 %s（z = %+.2f）%s，需拆解后再判" % (
            t["name"], _fmt_delta(t, c), c["z"], "（%s）" % c["adj"]["why"] if (c.get("adj") or {}).get("why") else "")
            for t, c in bad) if bad else "护栏未见显著劣化")
        proc = [sig_line(t, c) for t in ratio if t["role"] == "process" for c in [vs(t, g)] if c and c["read"]]
        lines.append("；".join(proc) if proc else "过程指标无显著变化")
        if stage == "中期读数":
            lines.append("中期读数，不作推全结论")
        out.append({"grp": g, "lines": lines})
    return {"state": "ok", "head": head, "groups": out}


# ─────────────────────────── 调度 ───────────────────────────

RUNNERS = {"funnel": run_funnel, "experiment": run_experiment, "exp_followup": run_exp_followup}


def cancel(jid):
    """取消任务。排队中 → 直接取消；运行中 → 发信号，当前这条查询的结果不再收、后面的不再跑，已落库的保留。
    返回 'cancelled' / 'cancelling' / None（不存在）；已结束的抛 ValueError（含已取消过的，免得和刚取消混淆）。"""
    with _lock:
        _want.add(jid)
        j = store.cancel_queued_job(jid)
        if not j:
            _want.discard(jid)
            return None
        if j["status"] == "queued":
            _want.discard(jid)
            _after_cancel(j["kind"], j["params"])
            return "cancelled"
        if j["status"] == "running":
            if _cur == jid:
                sources.CANCEL.set()
            return "cancelling"
        _want.discard(jid)
        raise ValueError("任务已结束（%s），不能取消" % j["status"])


def _after_cancel(kind, p):
    """被取消的实验任务：实验标失败（停每日续跑，等人点重跑），已出的结果不动。"""
    if "exp_id" not in p:
        return
    e = store.experiment(int(p["exp_id"]))
    if not e:
        return
    if kind == "experiment":
        store.update_experiment(e["id"], status="failed", error="跑数已取消（已出的结果保留），点「重跑」继续")
    elif e.get("pending"):
        store.update_experiment(e["id"], pending=[], error="补算已取消（已出的结果保留，可点重跑）")


def worker():
    while True:
        try:
            j = store.next_job()
        except Exception:                 # 库锁等瞬时错误：别让工作线程死掉
            traceback.print_exc()
            time.sleep(10)
            continue
        if not j:
            _wake.wait(5)
            _wake.clear()
            continue
        run_job(j)


def run_job(j):
    global _cur
    with _lock:
        _cur = j["id"]
        sources.CANCEL.clear()
        if j["id"] in _want:              # 取消请求先到、工作线程后接手
            sources.CANCEL.set()
    try:
        store.job_log(j["id"], "开始 %s %s" % (j["kind"], j["params"]))
        sources.check_cancel()
        RUNNERS[j["kind"]](j["id"], j["params"])
        store.finish_job(j["id"])
    except Cancelled:
        store.job_log(j["id"], "已取消：已落库的部分保留，没跑的不再跑")
        _after_cancel(j["kind"], j["params"])
        store.finish_job(j["id"], "已取消（已落库的部分保留）", status="cancelled")
    except Exception as ex:
        store.job_log(j["id"], traceback.format_exc()[-1500:])
        store.finish_job(j["id"], str(ex)[:800])
    finally:
        with _lock:
            _cur = None
            _want.discard(j["id"])
            sources.CANCEL.clear()


def daily_tick():
    """每分钟检查：到点刷新漏斗（近 N 天里缺的 / 不完整的 + 昨天），续跑未冻结实验；昨日不完整则隔几小时重试。"""
    from . import maint
    sch = C.cfg["schedule"]
    last_day, last_retry, last_maint = None, 0, None
    while True:
        now = dt.datetime.now()
        if last_maint != now.date():      # 每天第一次 tick（含启动后第一次）：结果库快照 + 日志轮转；失败当天不再重试
            last_maint = now.date()
            try:
                maint.daily(now.date())
            except Exception:             # 维护出错不能挡调度
                traceback.print_exc()
        due = (now.hour, now.minute) >= C.daily_at()
        if due and last_day != now.date():
            schedule_daily(force=True)
            last_day, last_retry = now.date(), time.time()
        elif due and now.hour < sch["retry_until_hour"] and time.time() - last_retry > sch["retry_gap_hours"] * 3600:
            if schedule_daily(force=False):
                last_retry = time.time()
        time.sleep(60)


def schedule_daily(force):
    y = yesterday()
    start = ymd(parse(y) - dt.timedelta(days=int(C.cfg["schedule"]["backfill_days"]) - 1))
    todo = False
    views = [s["key"] for s in C.cfg["scopes"]]
    for v in views + [k for k in store.funnel_views() if k not in views]:
        st = store.day_status(v)
        missing = [d for d in days_between(start, y) if not st.get(d)]
        if missing:
            submit("funnel", {"views": [v], "start": min(missing), "end": y})
            todo = True
    if force:
        for e in store.experiments():
            if e["status"] == "active" and (e["last_end"] or 0) < min(int(e["end"]), y):
                submit("experiment", {"exp_id": e["id"]})
    return todo


def start_background():
    threading.Thread(target=worker, daemon=True, name="worker").start()
    threading.Thread(target=daily_tick, daemon=True, name="daily").start()
