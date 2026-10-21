"""后台任务：漏斗刷新、实验回收。单线程串行执行（数仓并发不可控，串行最稳）。"""
import datetime as dt
import threading
import time
import traceback

from . import config as C, sqlgen, stats, store
from .sources import SourceTimeout, open_source

_wake = threading.Event()


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


def run_experiment(jid, p):
    log = lambda s: store.job_log(jid, s)
    e = store.experiment(int(p["exp_id"]))
    if not e:
        raise RuntimeError("实验不存在")
    store.update_experiment(e["id"], status="running", error=None)
    try:
        _run_experiment(e, log, p.get("only"), p.get("only_daily"), jid=jid)
    except Exception as ex:
        store.update_experiment(e["id"], status="failed", error=str(ex)[:800])
        raise


def _run_experiment(e, log, only=None, only_daily=None, jid=None):
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
    pending = ["@cum"] if cum_cutoffs(d0, d1) and any(_kind(mm[k]) == "ratio" for k in e["metric_keys"] if k in mm) else []
    pending += [m["key"] + "@day" for m in dm if _kind(m["def"]) == "ratio"]
    pending += [m["key"] for m in metrics if _kind(m["def"]) == "quantile"]
    pending += [m["key"] + "@day" for m in dm if _kind(m["def"]) == "quantile"]
    frozen = d1 >= int(e["end"])
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
    pending = [k for k in e["pending"] if k == "@cum" or k.split("@")[0] in mm]
    caps = dict(e.get("caps") or {})
    partial = []

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
                except Exception as ex:
                    partial.append("前 N 天累计：%s" % str(ex)[:200])
                    log("⚠ 前 N 天累计失败，其余照常：%s" % ex)
                done(["@cum"])
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
                except Exception as ex:
                    partial.append("%s：%s" % (label, str(ex)[:200]))
                    log("⚠ %s 失败，其余照常：%s" % (label, ex))
                done([k])
    except Exception as ex:
        partial.append(str(ex)[:300])
        raise
    finally:
        store.update_experiment(e["id"], pending=[], error=("补算失败（可点重跑）：" + "；".join(partial)) if partial else None)
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
        for d in sorted({d for v in day.values() for d in v}):
            add("%s-%s" % (d[4:6], d[6:]), "day", {g: v[d] for g, v in day.items() if d in v}, int(d))
        if kind == "ratio":
            for c, _ in cum_cutoffs(d0, d1):
                add("前 %d 天累计" % c, "cum", res.get("%s@c%d" % (k, c)), days=c)
        add("累计（%d 天）" % ndays, "total", res.get(k), days=ndays)
        out.append(t)
    return out


def experiment_view(e):
    mm = store.metric_map()
    ctrl = [g["id"] for g in e["groups"] if g["role"] == "control"]
    trts = [g["id"] for g in e["groups"] if g["role"] != "control"]
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
    tables = _tables(e, mm, res, ctrl, trts)
    return {"srm": srm, "tables": tables, "summary": summary(e, srm, tables, trts)}


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
    if e.get("status") in ("queued", "running") or e.get("pending"):
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
            hurt = [(t, c) for t in north + guard for c in [vs(t, g)] if c and c["read"] == "显著负向"]
            for t, c in hurt:
                lines.append("%s 前 %d 天显著变差 %s（CI %s 整体在变差一侧），%s" % (
                    t["name"], sl, _fmt_delta(t, c), _fmt_ci(t, c),
                    "建议止损" if t["role"] == "north_star" else "建议先拆解（机制 / 分布 / 分母）确认是真劣化后止损"))
            if not any(vs(t, g) for t in north + guard):
                lines.append("前 %d 天累计还没算出来，点「重跑」补齐后再判" % sl)
            elif not hurt:
                cs = [(t, vs(t, g)) for t in north]
                lines.append(("前 %d 天未见止损信号：" % sl) + "；".join(
                    "%s %s（CI %s）" % (t["name"], _fmt_delta(t, c), _fmt_ci(t, c)) for t, c in cs if c)
                    + ("；" if any(c for _, c in cs) else "") + "护栏未见显著劣化")
            lines.append("止损节点只看是否确定变差，不读收益")
            out.append({"grp": g, "lines": lines})
            continue

        def sig_line(t, c):
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
        lines.append("；".join("%s 显著劣化 %s（z = %+.2f），需拆解后再判" % (t["name"], _fmt_delta(t, c), c["z"]) for t, c in bad)
                     if bad else "护栏未见显著劣化")
        proc = [sig_line(t, c) for t in ratio if t["role"] == "process" for c in [vs(t, g)] if c and c["read"]]
        lines.append("；".join(proc) if proc else "过程指标无显著变化")
        if stage == "中期读数":
            lines.append("中期读数，不作推全结论")
        out.append({"grp": g, "lines": lines})
    return {"state": "ok", "head": head, "groups": out}


# ─────────────────────────── 调度 ───────────────────────────

RUNNERS = {"funnel": run_funnel, "experiment": run_experiment, "exp_followup": run_exp_followup}


def worker():
    while True:
        j = store.next_job()
        if not j:
            _wake.wait(5)
            _wake.clear()
            continue
        try:
            store.job_log(j["id"], "开始 %s %s" % (j["kind"], j["params"]))
            RUNNERS[j["kind"]](j["id"], j["params"])
            store.finish_job(j["id"])
        except Exception as ex:
            store.job_log(j["id"], traceback.format_exc()[-1500:])
            store.finish_job(j["id"], str(ex)[:800])


def daily_tick():
    """每分钟检查：到点刷新漏斗（近 N 天里缺的 / 不完整的 + 昨天），续跑未冻结实验；昨日不完整则隔几小时重试。"""
    sch = C.cfg["schedule"]
    last_day, last_retry = None, 0
    while True:
        now = dt.datetime.now()
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
