"""后台任务：漏斗刷新、实验回收。单线程串行执行（KG 并发不可控）。"""
import datetime as dt
import functools
import json
import sys
import threading
import time
import traceback

from . import config as C, sqlgen, stats, store
from . import kg_client
from .kg_client import KG, Cancelled

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


def submit(kind, params):
    jid = store.enqueue(kind, params)
    _wake.set()
    return jid


def kick(eid):
    """新建 / 整体重跑的实验：开窗日 ≤ 昨天 → 提交跑数；还没到 → 只标等待（active、没跑过），
    由每日调度在有数后自动开跑（直接提交会报「窗口内还没有可用数据」、把实验标失败）。返回任务号或 None。"""
    e = store.experiment(eid)
    if int(e["start"]) > yesterday():
        store.update_experiment(eid, status="active", error=None)
        return None
    return submit("experiment", {"exp_id": eid})


# ─────────────────────────── 漏斗 ───────────────────────────

def run_funnel(jid, p):
    log = lambda s: store.job_log(jid, s)
    d0, d1 = int(p["start"]), int(p["end"])
    with KG(log, trace=lambda q: store.job_query(jid, q)) as kg:
        for m in p.get("markets") or list(C.MARKETS):
            # m 是视图键：'SG' 整机房 / 'SG:ID' 单个国家；落库按视图键分开存
            mk, rg = sqlgen.split_scope(m)
            log("── %s %d~%d" % (m, d0, d1))
            rows = kg.dicts(sqlgen.funnel_sql(mk, d0, d1, rg), "%s 漏斗" % m)
            flow = kg.dicts(sqlgen.flow_sql(mk, d0, d1, rg), "%s 包含式漏斗" % m)
            # 每天最多 20 位 × 2 档 = 40 行，KG 单次 1000 行封顶 → 按 12 天一段拉
            pos, ds = [], list(_days(d0, d1))
            for a in range(0, len(ds), 12):
                b = ds[min(a + 12, len(ds)) - 1]
                pos += kg.dicts(sqlgen.position_sql(mk, ds[a], b, rg), "%s 位置CTR %d~%d" % (m, ds[a], b))
            core = kg.dicts(sqlgen.core_sql(mk, d0, d1, rg), "%s 点击→3s / 人均VV" % m)
            dsql = sqlgen.dau_sql(mk, d0, d1, rg)
            dau = kg.dicts(dsql, "%s DAU" % m) if dsql else []
            statuses = _completeness(m, d0, d1, rows)
            for d, (ok, note) in sorted(statuses.items()):
                if not ok:
                    log("⚠ %s %d 不完整：%s" % (m, d, note))
            store.save_funnel(m, rows, pos, dau, statuses, flow)
            store.save_daily_payload("core_daily", m, core)
            # 分来源观测：新版本起才可拆，只取 SPLIT_START 以后的天
            if d1 >= C.SPLIT_START:
                store.save_daily_payload("split_daily", m, kg.dicts(
                    sqlgen.split_sql(mk, max(d0, C.SPLIT_START), d1, rg, _split_versions()), "%s 分来源观测" % m))


def _split_versions():
    from . import versync
    return versync.current()


def run_split_backfill(jid, p):
    """同步版本后补算「分开看」：只重跑 split_sql，不动漏斗 / 核心指标。"""
    log = lambda s: store.job_log(jid, s)
    d0, d1 = max(int(p["start"]), C.SPLIT_START), int(p["end"])
    vs = _split_versions()
    log("版本列表 %d 个" % len(vs))
    with KG(log, trace=lambda q: store.job_query(jid, q)) as kg:
        for m in p.get("markets") or list(C.MARKETS):
            mk, rg = sqlgen.split_scope(m)
            log("── %s %d~%d" % (m, d0, d1))
            store.save_daily_payload("split_daily", m, kg.dicts(sqlgen.split_sql(mk, d0, d1, rg, vs), "%s 分来源观测" % m))


def run_tb_nf_backfill(jid, p):
    """补 funnel_daily.uv_tb_nf（Toolbar 排兜底去重 UV）：只跑漏斗 SQL、只更新这一列；按 31 天分段。
    范围 = 每个视图库里已有的日期（p 里没给 start / end 时）。"""
    log = lambda s: store.job_log(jid, s)
    with KG(log, trace=lambda q: store.job_query(jid, q)) as kg:
        for m in p.get("markets") or list(C.MARKETS):
            mk, rg = sqlgen.split_scope(m)
            with store.conn() as c:
                r = c.execute("SELECT MIN(date) a, MAX(date) b FROM funnel_daily WHERE market=?", (m,)).fetchone()
            if not r["a"]:
                continue
            s, e1 = parse(max(int(p.get("start") or 0), r["a"])), parse(min(int(p.get("end") or 99991231), r["b"]))
            while s <= e1:
                e = min(e1, s + dt.timedelta(days=30))
                rows = kg.dicts(sqlgen.funnel_sql(mk, ymd(s), ymd(e), rg), "%s 补 Toolbar 排兜底 UV %d~%d" % (m, ymd(s), ymd(e)))
                log("── %s %d~%d 更新 %d 行" % (m, ymd(s), ymd(e), store.fill_tb_nf(m, rows)))
                s = e + dt.timedelta(days=1)


def run_core_history(jid, p):
    """核心指标周均走势补历史：核心指标逐日 + DAU，按 14 天分段（人均 VV 的 P99 是按天开窗，分段不影响口径）。"""
    log = lambda s: store.job_log(jid, s)
    d0, d1 = int(p["start"]), int(p["end"])
    with KG(log, trace=lambda q: store.job_query(jid, q)) as kg:
        for m in p.get("markets") or list(C.MARKETS):
            mk, rg = sqlgen.split_scope(m)
            s = parse(d0)
            while ymd(s) <= d1:
                e = min(parse(d1), s + dt.timedelta(days=13))
                log("── %s 核心指标 %d~%d" % (m, ymd(s), ymd(e)))
                store.save_daily_payload("core_daily", m, kg.dicts(sqlgen.core_sql(mk, ymd(s), ymd(e), rg), "%s 核心指标逐日" % m))
                s = e + dt.timedelta(days=1)
            dsql = sqlgen.dau_sql(mk, d0, d1, rg)
            if dsql:
                store.save_funnel(m, [], [], kg.dicts(dsql, "%s DAU" % m), {})


def _completeness(market, d0, d1, rows):
    """无数据 / 结果页加载 UV 低于前 7 日中位数 60% → 视为分区未产出完整。"""
    by = {}
    for r in rows:
        if r["ek"] == "result_load":
            by[int(r["date"])] = int(r["uv"] or 0)
    out = {}
    d = parse(d0)
    while ymd(d) <= d1:
        k = ymd(d)
        # 前 7 天 = 按日期最近的 7 天；本次拉取范围里不够 7 天，用库里更早的补齐
        hist = [by[kk] for kk in sorted(kk for kk in by if kk < k)][-7:]
        if len(hist) < 7:
            hist = store.result_load_history(market, min([kk for kk in by if kk < k] + [k]), 7 - len(hist)) + hist
        med = sorted(hist)[len(hist) // 2] if hist else None
        uv = by.get(k)
        if not uv:
            out[k] = (False, "无结果页加载数据")
        elif med and uv < 0.6 * med:
            out[k] = (False, "结果页加载 UV %d < 前 7 日中位数 %d 的 60%%" % (uv, med))
        else:
            out[k] = (True, "")
        d += dt.timedelta(days=1)
    return out


# ─────────────────────────── 实验 ───────────────────────────

STAT_F = ("sx", "sy", "sxx", "syy", "sxy")
CUM_DAYS = (3, 7, 14)   # 「前 N 天累计」行；N < 窗口天数才出（等于窗口的就是全窗口累计）


def _ratio_payload(rows, k):
    return {str(r["grp"]): dict(n=int(r["n"]), **{f: float(r.get("%s_%s" % (f, k)) or 0) for f in STAT_F})
            for r in rows}


def cum_cutoffs(d0, d1):
    n = (parse(d1) - parse(d0)).days + 1
    return [(c, ymd(parse(d0) + dt.timedelta(days=c - 1))) for c in CUM_DAYS if c < n]


def _run_cum(kg, e, mm, gids, regions, d0, d1, caps, log):
    """前 3 / 7 / 14 天累计：口径同全窗口（设备并集、同一 P99 封顶），只是结束日提前；已算过的不重算。"""
    have = {r["metric_key"] for r in store.experiment(e["id"])["results"]}
    ratio = [k for k in e["metric_keys"] if k in mm and mm[k].get("kind", "ratio") == "ratio"]
    for c, cend in cum_cutoffs(d0, d1):
        miss = [k for k in ratio if "%s@c%d" % (k, c) not in have]
        if not miss:
            continue
        ms = [{"key": k, "name": mm[k]["name"], "def": mm[k]} for k in miss]
        rows = kg.dicts(sqlgen.experiment_ratio_sql(e["market"], gids, d0, cend, ms, caps, regions),
                        "前 %d 天累计 ×%d" % (c, len(ms)))
        for k in miss:
            store.save_exp_results(e["id"], "%s@c%d" % (k, c), _ratio_payload(rows, k), cend)


def dist_keys(mm, keys):
    """出分布箱型图的指标：人均类（分母按设备 UV、分子是次数 / 求和），每设备取值才有分布。"""
    return [k for k in keys if k in mm and mm[k].get("kind", "ratio") == "ratio"
            and mm[k]["numerator"].get("agg", "uv") != "uv" and mm[k]["denominator"].get("agg", "uv") == "uv"]


def _run_dist(kg, e, mm, gids, regions, d0, d1, caps):
    """设备集合与全窗口比率一致（同一批指标拼认组事件），所以各组进组累计 = 分流检查的设备数。"""
    ratio = [{"key": k, "name": mm[k]["name"], "def": mm[k]} for k in e["metric_keys"]
             if k in mm and mm[k].get("kind", "ratio") == "ratio"]
    dk = dist_keys(mm, e["metric_keys"])
    days = list(_days(d0, d1))
    rows = kg.dicts(sqlgen.experiment_dist_sql(e["market"], gids, d0, d1, ratio, caps, regions, dk, days),
                    "分布 / 进组趋势")
    out = {}
    for r in rows:
        out[str(r["grp"])] = {
            "metrics": {k: {"q": [r.get("q%d_%s" % (i, k)) for i in range(len(sqlgen.DIST_Q))],
                            "avg": r.get("avg_%s" % k), "n": int(r.get("n_%s" % k) or 0)} for k in dk},
            "entry": {str(d): int(r.get("e_%d" % d) or 0) for d in days}}
    store.save_exp_results(e["id"], "@dist", out, d1)


def _run_pos(kg, e, gids, regions, d0, d1):
    """Top20 位置 CTR（PV，排兜底）：各组 位置 × 档位（站内 / Toolbar）的曝光 / 点击计数。"""
    rows = kg.dicts(sqlgen.experiment_position_sql(e["market"], gids, d0, d1, regions), "位置 CTR")
    out = {}
    for r in rows:
        g = str(r["grp"])
        if g in ("X", "None"):            # 跨组 / 没带组号的事件不出
            continue
        out.setdefault(g, []).append({"position": int(r["position"]), "tb": int(r["tb"]),
                                      "expose": int(r["expose"] or 0), "click": int(r["click"] or 0)})
    store.save_exp_results(e["id"], "@pos", out, d1)


# 只做可视化 / 辅助读数、不进结论提要的补算项：不挡结论、不挡节点通知
VIS_KEYS = ("@dist", "@cuped", "@seg", "@lt", "@pos")
VIS_LABELS = ("分布 / 进组趋势", "CUPED", "分群下钻", "长期价值", "位置 CTR")
CUPED_MIN_DAYS = 7     # 协变量窗口被上一轮截短后不足 7 天就不做（1~2 天的协变量相关性低，还可能落在推全过渡期）
CUPED_PRE_DAYS = 28    # 协变量 = 上线前 28 天同口径取值（10-27 在实验 4 / 6 / 23 上试过：比 7 天覆盖高、人均 VV 方差多降 1~2 个点）
SEG_MIN_N = 300        # 分群每组至少这么多台设备才出读数


def _run_cuped(kg, e, mm, gids, regions, d0, d1, caps):
    """CUPED 原料（全窗口累计行）：实验期设备层 × 上线前 28 天同口径设备层。"""
    ms = sqlgen.cuped_metrics([{"key": k, "name": mm[k]["name"], "def": mm[k]} for k in e["metric_keys"]
                               if k in mm and mm[k].get("kind", "ratio") == "ratio"])
    if not ms:
        return
    p0, p1, why = cuped_window(e)
    if p0 > p1:
        store.save_exp_results(e["id"], "@cuped", {"@pre": {"p0": p0, "p1": p1, "skip": why}}, d1)
        return
    rows = kg.dicts(sqlgen.experiment_cuped_sql(e["market"], gids, d0, d1, ms, caps, regions, p0, p1), "CUPED ×%d" % len(ms))
    f = ["s" + a for a in sqlgen.CUPED_V] + ["s" + a + b for i, a in enumerate(sqlgen.CUPED_V) for b in sqlgen.CUPED_V[i:]]
    out = {str(r["grp"]): {m["key"]: dict(n=int(r["n"]), **{x: float(r.get("%s_%s" % (x, m["key"])) or 0) for x in f})
                           for m in ms} for r in rows}
    out["@pre"] = {"p0": p0, "p1": p1, "cut": why, "d1": d1}
    store.save_exp_results(e["id"], "@cuped", out, d1)


def _cuped_window_changed(e):
    """已存的 CUPED 协变量窗口和现在的规则算出来的不一样（如 10-27 起避开同组号的上一轮）→ 要重算。"""
    raw = next((r["payload"] for r in e.get("results") or [] if r["metric_key"] == "@cuped" and r["grp"] == "@pre"), None)
    if raw is None:
        return False
    p0, p1, _ = cuped_window(e)
    return (raw.get("p0"), raw.get("p1")) != (p0, p1)


def _node_bucket(d0, d):
    """数据到 d 时所处的节点段：第 1~2 天 = 0，第 3~6 天 = 3，第 7~13 天 = 7，第 14 天起 = 14（第 1 天算过的到第 3 天要重算）。"""
    n = (parse(d) - parse(d0)).days + 1
    return 14 if n >= 14 else 7 if n >= 7 else 3 if n >= 3 else 0


def cuped_window(e, exps=None):
    """协变量窗口 = 上线前 28 天；同一市场有别的实验用过本实验的 expId、且跑在这 28 天里的（第三轮复用前几轮的组号），
    窗口起点挪到那个实验结束日的次日：那段时间同一批设备按上一轮分了组、吃了上一轮策略，协变量会和分组相关，CUPED 就有偏。
    返回 (p0, p1, 说明)；p0 > p1 = 没有干净的窗口，不做 CUPED。"""
    launch = parse(e.get("launch") or e["start"])
    p0, p1 = ymd(launch - dt.timedelta(days=CUPED_PRE_DAYS)), ymd(launch - dt.timedelta(days=1))
    ids = {g["id"] for g in e["groups"]}
    hit = [o for o in (exps if exps is not None else store.experiments()) if o["id"] != e["id"] and o["market"] == e["market"]
           and ids & {g["id"] for g in o["groups"]} and int(o["start"]) <= p1 and int(o.get("last_end") or o["end"]) >= p0]
    if not hit:
        return p0, p1, ""
    o = max(hit, key=lambda o: int(o.get("last_end") or o["end"]))
    p0 = ymd(parse(o.get("last_end") or o["end"]) + dt.timedelta(days=1))
    if (parse(p1) - parse(p0)).days + 1 < CUPED_MIN_DAYS:
        return p0, ymd(parse(p0) - dt.timedelta(days=1)), "上线前 28 天内同一批组号在「%s」里跑过（到 %s），剩下的干净窗口不足 %d 天" % (
            o["name"], md_(o.get("last_end") or o["end"]), CUPED_MIN_DAYS)
    return p0, p1, "上线前 28 天内同一批组号在「%s」里跑过（到 %s），协变量窗口从 %s 起" % (
        o["name"], md_(o.get("last_end") or o["end"]), md_(p0))


def md_(d):
    s = str(d)
    return "%s-%s" % (s[4:6], s[6:])


def _run_seg(kg, e, mm, gids, regions, d0, d1, caps):
    """分群充分统计量：两次查询（新老 × 入口 × 搜索词类型交叉一次，国家一次），页面再按维度边缘化。"""
    ms = [{"key": k, "name": mm[k]["name"], "def": mm[k]} for k in e["metric_keys"]
          if k in mm and mm[k].get("kind", "ratio") == "ratio"]
    if not ms:
        return
    out = {}
    for dims in sqlgen.SEG_RUNS:
        rows = kg.dicts(sqlgen.experiment_seg_sql(e["market"], gids, d0, d1, ms, caps, regions, dims),
                        "分群 %s" % "×".join(sqlgen.SEG_DIMS[d][0] for d in dims))
        out[",".join(dims)] = [dict({"grp": str(r["grp"]), "n": int(r["n"])},
                                    **{"d_" + d: str(r["d_" + d]) for d in dims},
                                    **{"%s_%s" % (f, m["key"]): float(r.get("%s_%s" % (f, m["key"])) or 0)
                                       for m in ms for f in STAT_F}) for r in rows]
    store.save_exp_results(e["id"], "@seg", {"_": out, "d1": d1}, d1)


def _run_lt(kg, e, mm, gids, regions, d0, d1, caps):
    """长期价值（@lt）：认组同读数表（结果页四个事件、实验国家范围），只算 d0 + 7 ≤ 成熟截止的设备。
    成熟截止 = 本次跑到的日期（冻结 = 冻结日）与 App 活跃表最新日取小；一台都不可能成熟时不查数，直接记「未成熟」。"""
    meta = {"d1": d1, "cut": d1}
    if ymd(parse(d1) - dt.timedelta(days=7)) < d0:
        store.save_exp_results(e["id"], "@lt", {"@meta": dict(meta, immature=True)}, d1)
        return
    r = kg.dicts(sqlgen.dau_max_sql(d0, d1), "App 活跃表最新日")
    if r and r[0].get("d"):
        meta["cut"] = min(d1, int(r[0]["d"]))
    if ymd(parse(meta["cut"]) - dt.timedelta(days=7)) < d0:
        store.save_exp_results(e["id"], "@lt", {"@meta": dict(meta, immature=True)}, d1)
        return
    # 时长类封顶：上线日前 7 天单设备 7 日累计值的 P99，只算一次、写死
    keys = ["@lt_" + k for k in sqlgen.LT_CAPPED]
    if any(k not in caps for k in keys):
        launch = int(e.get("launch") or d0)
        p0, p1 = ymd(parse(launch) - dt.timedelta(days=7)), ymd(parse(launch) - dt.timedelta(days=1))
        c = kg.dicts(sqlgen.lt_cap_sql(e["market"], p0, p1, regions), "长期价值 P99 封顶")
        if not c or any(c[0].get(k) is None for k in sqlgen.LT_CAPPED):
            raise RuntimeError("上线前 7 天无数据，算不出长期价值封顶值")
        caps.update({"@lt_" + k: float(c[0][k]) for k in sqlgen.LT_CAPPED})
        fresh = dict((store.experiment(e["id"]) or {}).get("caps") or {})
        fresh.update({k: caps[k] for k in keys})
        store.update_experiment(e["id"], caps=fresh)
    rows = kg.dicts(sqlgen.experiment_lt_sql(e["market"], gids, d0, d1, regions, meta["cut"], caps), "长期价值")
    out = {str(x["grp"]): dict(n=int(x["n"]), **{"%s_%s" % (p, k): float(x.get("%s_%s" % (p, k)) or 0)
                                                for k in sqlgen.LT_KEYS for p in ("s", "ss")}) for x in rows}
    meta["rev"] = _run_lt_rev(kg, e, gids, regions, d0, d1, meta["cut"], caps, out)
    out["@meta"] = meta
    store.save_exp_results(e["id"], "@lt", out, d1)


def _run_lt_rev(kg, e, gids, regions, d0, d1, cut, caps, out):
    """长期价值 · 收入（只有 SG）：结果并进 out 的各组（s_fee / s_feec / s_imp / s_impc …），返回 meta 里的收入状态。
    收入失败不拖累留存：记在 meta.rev.error，留存照常落库。"""
    mk = e["market"]
    if mk not in sqlgen.REV_MARKETS:
        return {"skip": "事件日志 client_info 不是 GAID"}
    cat = sqlgen.REV_CATALOG[mk]
    try:
        keys = ["@lt_" + k for k in sqlgen.REV_KEYS]
        if any(k not in caps for k in keys):
            launch = int(e.get("launch") or d0)
            p0, p1 = ymd(parse(launch) - dt.timedelta(days=7)), ymd(parse(launch) - dt.timedelta(days=1))
            c = kg.dicts(sqlgen.lt_rev_cap_sql(mk, p0, p1, regions), "长期价值 收入 P99.9 封顶", catalog=cat)
            if not c or any(c[0].get(k) is None for k in sqlgen.REV_KEYS):
                raise RuntimeError("上线前 7 天无数据，算不出收入封顶值")
            caps.update({"@lt_" + k: float(c[0][k]) for k in sqlgen.REV_KEYS})
            fresh = dict((store.experiment(e["id"]) or {}).get("caps") or {})
            fresh.update({k: caps[k] for k in keys})
            store.update_experiment(e["id"], caps=fresh)
        rows = kg.dicts(sqlgen.experiment_rev_sql(mk, gids, d0, d1, regions, cut, caps), "长期价值 收入", catalog=cat)
    except Cancelled:
        raise
    except Exception as ex:
        return {"error": str(ex)[:200]}
    for x in rows:
        g = out.get(str(x["grp"]))
        if g is None or int(x["n"]) != g["n"]:
            return {"error": "收入人群与留存人群设备数不一致（%s：%s vs %s）" % (x["grp"], x["n"], g and g["n"])}
        g.update({"%s_%s%s" % (p, k, c): float(x.get("%s_%s%s" % (p, k, c)) or 0)
                  for k in sqlgen.REV_KEYS for c in ("", "c") for p in ("s", "ss")})
        g["n_fee"] = int(x.get("n_fee") or 0)
    return {"ok": True}


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
    regions = sqlgen.scope_of(e)              # 国家 + 按事件排除的 source（第四轮起排除热词 5）
    d0 = int(e["start"])
    d1 = min(int(e["end"]), yesterday())
    if incr and e.get("last_end"):
        d1 = int(e["last_end"])
        log("增量补算：新增指标 %s；新增 By天 %s；窗口沿用 %d~%d" % (only or "无", only_daily or "无", d0, d1))
    if d1 < d0:
        raise RuntimeError("窗口内还没有可用数据（开窗日在今天或之后）")
    caps = dict(e.get("caps") or {})
    # 封顶取上线日之前 7 天：上线当天已有实验流量，不能算进基线
    launch = int(e.get("launch") or d0)
    pre1 = ymd(parse(launch) - dt.timedelta(days=1))
    pre0 = ymd(parse(launch) - dt.timedelta(days=7))
    with KG(log, trace=(lambda q: store.job_query(jid, q)) if jid else None) as kg:
        # 1) 开窗前 P99 封顶（只算一次，写死）
        for m in metrics:
            if m["def"].get("cap") == "p99_pre7d" and m["key"] not in caps:
                r = kg.dicts(sqlgen.cap_sql(e["market"], pre0, pre1, m["def"]["numerator"], m["def"]["denominator"], regions),
                             "P99 " + m["name"])
                if not r or r[0]["cap"] is None:
                    raise RuntimeError("开窗前 7 天无数据，算不出 P99 封顶")
                caps[m["key"]] = float(r[0]["cap"])
                log("P99 封顶 %s = %s（上线前 %d~%d 单设备单天，%s 设备-天）" % (m["name"], caps[m["key"]], pre0, pre1, r[0]["n_dev"]))
        store.update_experiment(e["id"], caps=caps)

        # 2) 比率指标：一次扫描全部算完
        ratio = [m for m in metrics if m["def"].get("kind", "ratio") == "ratio"]
        if ratio:
            rows = kg.dicts(sqlgen.experiment_ratio_sql(e["market"], gids, d0, d1, ratio, caps, regions), "比率指标 ×%d" % len(ratio))
            for m in ratio:
                store.save_exp_results(e["id"], m["key"], _ratio_payload(rows, m["key"]), d1)

    # 3) 慢的都不挡全窗口比率读数：先出结果，By天 / 分位数排一个补算任务在后面
    #    pending 顺序 = 补算顺序：比率 By天（一次扫描）→ 分位数全窗口 → 分位数 By天
    daily = set(only_daily if incr else e.get("daily_keys") or [])
    dm = [{"key": k, "def": mm[k]} for k in e["metric_keys"] if k in daily and k in mm]   # 已有指标也可能新加 By天
    any_ratio = any(mm[k].get("kind", "ratio") == "ratio" for k in e["metric_keys"] if k in mm)
    pending = ["@cum"] if cum_cutoffs(d0, d1) and any_ratio else []
    # 分布 + 进组趋势：窗口变了 / 新增比率指标 / 还没算过 → 重算（只读数可视化用，不进判读）
    have = {r["metric_key"]: r["run_end"] for r in e.get("results") or []}
    # 加收入之前算的 @lt（SG、已成熟、meta 里没有 rev）→ 重算一次补上收入
    lt_meta = next((r["payload"] for r in e.get("results") or [] if r["metric_key"] == "@lt" and r["grp"] == "@meta"), None)
    lt_no_rev = e["market"] in sqlgen.REV_MARKETS and lt_meta is not None and not lt_meta.get("immature") and "rev" not in lt_meta
    new_ratio = any(m["def"].get("kind", "ratio") == "ratio" for m in metrics)
    frozen = d1 >= int(e["end"])
    for k in VIS_KEYS:
        if not any_ratio:
            continue
        # 分布每次都算；CUPED / 分群在 SG 上各要 ~20 分钟，只在跨节点（第 3 / 7 / 14 天）、冻结、缺 / 失败、新增指标时重算
        # 长期价值不看第 3 天节点（只在第 7 / 14 天、冻结、缺 / 失败时重算），和比率指标无关，新增指标不重算
        heavy = k not in ("@dist", "@pos")
        bucket = lambda d: (lambda b: 0 if k == "@lt" and b == 3 else b)(_node_bucket(d0, d))
        stale = refresh_vis or k not in have or e.get("vis_error") or (incr and new_ratio and k != "@lt") or \
            (k == "@lt" and lt_no_rev) or \
            (k == "@cuped" and _cuped_window_changed(e)) or \
            (not heavy and not incr) or (heavy and not incr and (
                frozen and int(have[k]) != d1 or bucket(int(have[k])) != bucket(d1)))
        if stale:
            pending.append(k)
    pending += [m["key"] + "@day" for m in dm if m["def"].get("kind", "ratio") == "ratio"]
    pending += [m["key"] for m in metrics if m["def"].get("kind") == "quantile"]
    pending += [m["key"] + "@day" for m in dm if m["def"].get("kind") == "quantile"]
    store.update_experiment(e["id"], status="frozen" if frozen else "active", last_end=d1, error=None, pending=pending)
    log("完成：窗口 %d~%d%s" % (d0, d1, "，已到结束日，冻结" if frozen else "，结束日前每日续跑"))
    if pending:
        fid = submit("exp_followup", {"exp_id": e["id"], "run_end": d1})
        log("补算 ×%d（By天 / 分位数）已排到任务 #%d" % (len(pending), fid))


def _quant_payload(rows, m, daily):
    nq = len(m["def"]["quantiles"])
    out = {}
    for r in rows:
        v = {"n_events": int(r["n_events"]), "n_dev": int(r["n_dev"]), "q": [r.get("q%d" % i) for i in range(nq)]}
        if daily:
            out.setdefault(str(r["grp"]), {})[str(r["date"])] = v
        else:
            out[str(r["grp"])] = v
    return out


def run_exp_followup(jid, p):
    """补算 By天 与分位数指标；单项失败不影响其他，做完一项从 pending 里划掉一项。"""
    log = lambda s: store.job_log(jid, s)
    e = store.experiment(int(p["exp_id"]))
    if not e:
        raise RuntimeError("实验不存在")
    mm = store.metric_map()
    gids = [g["id"] for g in e["groups"]]
    regions = sqlgen.scope_of(e)              # 国家 + 按事件排除的 source（第四轮起排除热词 5）
    d0, d1 = int(e["start"]), int(p["run_end"])
    pending = [k for k in e["pending"] if k in ("@cum",) + VIS_KEYS or k.split("@")[0] in mm]
    caps = dict(e.get("caps") or {})
    partial = []

    def done(keys):
        nonlocal pending
        pending = [k for k in pending if k not in keys]
        store.update_experiment(e["id"], pending=pending)

    try:
        with KG(log, trace=lambda q: store.job_query(jid, q)) as kg:
            # 0) 前 3 / 7 / 14 天累计
            if "@cum" in pending:
                try:
                    _run_cum(kg, e, mm, gids, regions, d0, d1, caps, log)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("前 N 天累计：%s" % str(ex)[:200])
                    log("⚠ 前 N 天累计失败，其余照常：%s" % ex)
                done(["@cum"])
            # 0.5) 分布箱型图 + 进组趋势：一次扫描
            if "@dist" in pending:
                try:
                    _run_dist(kg, e, mm, gids, regions, d0, d1, caps)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("分布 / 进组趋势：%s" % str(ex)[:200])
                    log("⚠ 分布 / 进组趋势失败，其余照常：%s" % ex)
                done(["@dist"])
            # 0.55) Top20 位置 CTR：一次轻量扫描（只 card 曝光 / 点击）
            if "@pos" in pending:
                try:
                    _run_pos(kg, e, gids, regions, d0, d1)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("位置 CTR：%s" % str(ex)[:200])
                    log("⚠ 位置 CTR 失败，其余照常：%s" % ex)
                done(["@pos"])
            # 0.6) CUPED / 分群下钻：各一次（分群两次）扫描，失败不影响主读数
            for k, fn, label in (("@cuped", _run_cuped, "CUPED"), ("@seg", _run_seg, "分群下钻"), ("@lt", _run_lt, "长期价值")):
                if k in pending:
                    try:
                        fn(kg, e, mm, gids, regions, d0, d1, caps)
                    except Cancelled:
                        raise
                    except Exception as ex:
                        partial.append("%s：%s" % (label, str(ex)[:200]))
                        log("⚠ %s 失败，其余照常：%s" % (label, ex))
                    done([k])
            # 1) 比率 By天：一次扫描，单位 = 设备-天
            rk = [k for k in pending if k.endswith("@day") and mm[k[:-4]].get("kind", "ratio") == "ratio"]
            if rk:
                ms = [{"key": k[:-4], "name": mm[k[:-4]]["name"], "def": mm[k[:-4]]} for k in rk]
                try:
                    rows = kg.dicts(sqlgen.experiment_daily_ratio_sql(e["market"], gids, d0, d1, ms, caps, regions),
                                    "By天 比率 ×%d" % len(ms))
                    for m in ms:
                        k = m["key"]
                        payload = {}
                        for r in rows:
                            vals = {f: float(r.get("%s_%s" % (f, k)) or 0) for f in STAT_F}
                            payload.setdefault(str(r["grp"]), {})[str(r["date"])] = dict(n=int(r["n"]), **vals)
                        store.save_exp_results(e["id"], k + "@day", payload, d1)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("By天 比率：%s" % str(ex)[:200])
                    log("⚠ By天 比率失败，其余照常：%s" % ex)
                done(rk)

            # 2) 分位数：全窗口，再 By天
            for k in list(pending):
                base, daily = (k[:-4], True) if k.endswith("@day") else (k, False)
                m = {"key": base, "name": mm[base]["name"], "def": mm[base]}
                label = m["name"] + ("（By天）" if daily else "")
                try:
                    rows = kg.dicts(sqlgen.experiment_quantile_sql(e["market"], gids, d0, d1, m, regions, daily), label)
                    store.save_exp_results(e["id"], k, _quant_payload(rows, m, daily), d1)
                except Cancelled:
                    raise
                except Exception as ex:
                    partial.append("%s：%s" % (label, str(ex)[:200]))
                    log("⚠ %s 失败，其余照常：%s" % (label, ex))
                done([k])
    except Cancelled:
        partial.append("已取消")
        raise
    except Exception as ex:
        partial.append(str(ex)[:300])
        raise
    finally:
        # 只是 CUPED / 分群 / 分布失败：记在 vis_error，不写 error（error 会挡结论提要、让健康检查报失败）
        main = [x for x in partial if not x.startswith(VIS_LABELS)]
        vis = [x for x in partial if x.startswith(VIS_LABELS)]
        store.update_experiment(e["id"], pending=[],
                                error=("补算未完成（可点重跑）：" + "；".join(main)) if main else None)
        store.set_vis_error(e["id"], "；".join(vis) if vis else None)
    log("补算完成")


def experiment_view(e, base=None):
    """把充分统计量组装成页面数据。base = 临时当基准的组（只换比较对象，统计口径不变；默认 = 对照组）。"""
    mm = store.metric_map()
    ids = [g["id"] for g in e["groups"]]
    ctrl = [base] if base in ids else [g["id"] for g in e["groups"] if g["role"] == "control"]
    trts = [i for i in ids if i not in ctrl]
    res = {}
    for r in e["results"]:
        res.setdefault(r["metric_key"], {})[r["grp"]] = r["payload"]
    counts = None
    rows = []
    for k in e["metric_keys"]:
        m = mm.get(k)
        if m and k in (e.get("pending") or []):
            rows.append({"key": k, "name": m["name"], "group": m.get("group", "自定义"), "kind": m.get("kind", "ratio"),
                         "note": m.get("note", ""), "pending": True, "groups": {}, "vs": {}})
            continue
        if not m or k not in res:
            continue
        per = res[k]
        item = {"key": k, "name": m["name"], "group": m.get("group", "自定义"), "kind": m.get("kind", "ratio"),
                "unit": m.get("unit") or ("pct" if m.get("numerator", {}).get("agg", "uv") == "uv" or
                                          m.get("numerator", {}).get("agg") == m.get("denominator", {}).get("agg") else "num"),
                "note": m.get("note", ""), "lower_is_better": bool(m.get("lower_is_better")), "groups": {}, "vs": {}}
        if item["kind"] == "ratio":
            est = {}
            for g, s in per.items():
                rr, var = stats.ratio_stats(s["n"], s["sx"], s["sy"], s["sxx"], s["syy"], s["sxy"])
                est[g] = (rr, var)
                item["groups"][g] = {"value": rr, "se": var ** 0.5 if var is not None else None,
                                     "num": s["sx"], "den": s["sy"], "n": s["n"]}
                if counts is None and g != "X":
                    pass
            if counts is None:
                counts = {g: s["n"] for g, s in per.items()}
            if ctrl and ctrl[0] in est:
                for t in trts:
                    if t in est:
                        item["vs"][t] = stats.compare(est[ctrl[0]], est[t])
        else:
            for g, s in per.items():
                item["groups"][g] = s
        rows.append(item)
    srm = None
    if counts:
        ids = [g["id"] for g in e["groups"]]
        obs = [counts.get(i, 0) for i in ids]
        chi, p = stats.srm(obs)
        srm = {"counts": dict(zip(ids, obs)), "conflict": counts.get("X", 0), "chi2": chi, "p": p}
    tables = adjust_reads(e, _tables(e, mm, res, ctrl, trts))
    # 结论提要始终按真对照组出，不随临时基准变
    c0 = [g["id"] for g in e["groups"] if g["role"] == "control"]
    t0 = [i for i in ids if i not in c0]
    st = tables if c0 == ctrl else adjust_reads(e, _tables(e, mm, res, c0, t0))
    dist = (res.get("@dist") or {}).get
    return {"metrics": rows, "srm": srm, "tables": tables, "summary": _summary(e, srm, st, t0),
            "cuped": cuped_view(e, mm, res.get("@cuped"), tables, ctrl, trts),
            "lt": lt_view(e, res.get("@lt"), ctrl, trts),
            "seg": seg_view(e, mm, _seg_raw(res.get("@seg")), tables, ctrl, trts),
            "base": ctrl[0] if ctrl else None, "trts": trts,
            "dist": {g: dist(g) for g in ids if dist(g)} or None,
            "dist_keys": [k for k in dist_keys(mm, e["metric_keys"])],
            "pos": res.get("@pos") or None}


def cuped_view(e, mm, raw, tables, ctrl, trts):
    """CUPED 读数（全窗口累计行）：{指标: {组: adjusted}}，另附原读数的 se 方便对比方差降幅。
    判读口径同原表（p < 0.05 + 指标方向），也跑一遍序贯阈值；不改原读数，只并列展示。"""
    if not raw or not ctrl:
        return None
    pre = raw.get("@pre") or {}
    p0, p1, why = cuped_window(e)
    if (pre.get("p0"), pre.get("p1")) != (p0, p1):
        return {"pre": pre, "skip": "协变量窗口规则已更新，等下次重跑后再看（%s）" % (why or "窗口有变"), "metrics": {}}
    if pre.get("skip"):
        return {"pre": pre, "skip": pre["skip"], "metrics": {}}
    d1 = int(pre.get("d1") or e["last_end"])
    n = (parse(d1) - parse(e["start"])).days + 1
    planned = (parse(e["end"]) - parse(e["start"])).days + 1
    final = d1 >= int(e["end"])
    out = {}
    for t in tables:
        k = t["key"]
        if t["kind"] != "ratio":
            continue
        c0 = stats.cuped_group((raw.get(ctrl[0]) or {}).get(k) or {})
        for g in trts:
            x = stats.cuped_compare(c0, stats.cuped_group((raw.get(g) or {}).get(k) or {}))
            if not x:
                continue
            harm = (x["diff"] > 0) == bool(t["lower_is_better"])
            x["read"] = ("显著正向" if not harm else "显著负向") if x["p"] < 0.05 else None
            x["seq"] = abs(x["z"]) >= seq_bound(n, planned, final, harm)
            out.setdefault(k, {})[g] = x
    return {"pre": pre, "metrics": out, "d1": d1, "days": n, "stale": d1 != int(e["last_end"])} if out else None



# 长期价值：(键, 名称, 单位)；单位 day = 天，pct = 留存（pp），num = 数值（绝对差 + 相对差）
LT_METRICS = (("lt7", "大盘 LT7", "day"), ("r1", "次留", "pct"), ("r7", "7 留", "pct"),
              ("vv", "7 日人均 VV", "num"), ("vv3", "7 日人均 3sVV", "num"), ("dur", "7 日人均时长（分钟）", "num"))
LT_STOP = ("lt7", "r1", "r7")      # 显著负向（过序贯阈值）→ 判止损
# 收入（SG 计费口径，单位美丝）：判读用 P99.9 封顶值，参与 BH / 序贯标注，不进止损；非 SG 显示「暂不支持」
LT_REV = (("fee", "7 日人均收入"), ("imp", "7 日人均广告曝光"))


def lt_view(e, raw, ctrl, trts):
    """长期价值读数：每设备一个值、组内设备均值，标准误按设备（均值的方差 = 设备方差 ÷ n）。
    判读同读数表（p < 0.05 + 序贯阈值）；6 个指标 × 实验组自成一族做 BH，不并进读数表的族（读数表的修正标注不变）。"""
    if not raw or not ctrl:
        return None
    meta = raw.get("@meta") or {}
    cut = int(meta.get("cut") or meta.get("d1") or e["last_end"])
    entry = ymd(parse(cut) - dt.timedelta(days=7))
    out = {"d1": int(meta.get("d1") or cut), "cut": cut, "entry_end": entry, "caps": {
        k: (e.get("caps") or {}).get("@lt_" + k) for k in sqlgen.LT_CAPPED},
        "stale": int(meta.get("d1") or cut) != int(e.get("last_end") or 0)}
    if meta.get("immature"):
        return dict(out, immature=True, n=0, metrics=[])
    gs = {g: s for g, s in raw.items() if g not in ("@meta", "X") and s.get("n")}
    out["n"] = sum(s["n"] for s in gs.values())
    if not out["n"]:
        return dict(out, immature=True, metrics=[])
    d0 = parse(e["start"])
    n = (parse(cut) - d0).days + 1
    planned = (parse(e["end"]) - d0).days + 1
    final = e.get("status") == "frozen" or cut >= int(e["end"])
    mets, fam = [], []
    for k, name, unit in LT_METRICS:
        est, groups = {}, {}
        for g, s in gs.items():
            m = s["s_" + k] / s["n"]
            var = max(s["ss_" + k] / s["n"] - m * m, 0.0) / s["n"]
            est[g] = (m, var)
            groups[g] = {"value": m, "n": s["n"]}
        vs = {}
        for t in trts:
            if ctrl[0] in est and t in est:
                x = stats.compare(est[ctrl[0]], est[t])
                if not x or not x["se"]:
                    continue
                x["z"] = x["diff"] / x["se"]
                x["read"] = ("显著正向" if x["diff"] > 0 else "显著负向") if x["p"] < 0.05 else None
                b = seq_bound(n, planned, final, x["diff"] < 0)
                x["adj"] = {"bound": b, "seq": abs(x["z"]) >= b, "bh": None}
                x["stop"] = k in LT_STOP and x["read"] == "显著负向" and x["adj"]["seq"]
                vs[t] = x
                fam.append(x)
        mets.append({"key": k, "name": name, "unit": unit, "groups": groups, "vs": vs})
    rev = meta.get("rev") or {}
    for k, name in LT_REV:
        row = {"key": k, "name": name, "unit": "rev", "groups": {}, "vs": {}}
        if rev.get("skip") or not rev.get("ok"):
            row["na"] = "暂不支持：" + rev["skip"] if rev.get("skip") else "收入未算出" + ("：" + rev["error"] if rev.get("error") else "")
            mets.append(row)
            continue
        # 判读 / 显著性用封顶值（s_kc）；不封顶均值只展示
        est = {}
        for g, s in gs.items():
            m = s["s_%sc" % k] / s["n"]
            est[g] = (m, max(s["ss_%sc" % k] / s["n"] - m * m, 0.0) / s["n"])
            row["groups"][g] = {"value": m, "raw": s["s_" + k] / s["n"], "n": s["n"], "n_fee": s.get("n_fee")}
        for t in trts:
            if ctrl[0] in est and t in est:
                x = stats.compare(est[ctrl[0]], est[t])
                if not x or not x["se"]:
                    continue
                x["z"] = x["diff"] / x["se"]
                x["read"] = ("显著正向" if x["diff"] > 0 else "显著负向") if x["p"] < 0.05 else None
                b = seq_bound(n, planned, final, x["diff"] < 0)
                x["adj"] = {"bound": b, "seq": abs(x["z"]) >= b, "bh": None}
                x["stop"] = False
                row["vs"][t] = x
                fam.append(x)
        mets.append(row)
    out["caps"].update({k: (e.get("caps") or {}).get("@lt_" + k) for k, _ in LT_REV})
    for x, ok in zip(fam, stats.bh_pass([x["p"] for x in fam], 0.05)):
        a = x["adj"]
        a["bh"] = ok
        why = ([] if a["seq"] else ["多次看数阈值 |z| ≥ %.2f" % a["bound"]]) + ([] if ok else ["多指标修正（BH）"])
        a["ok"] = bool(x["read"]) and not why
        a["why"] = "未过" + "、".join(why) if why else ""
    return dict(out, days=n, metrics=mets)


def lt_fmt(unit, c):
    """长期价值 Δ：天「+0.015 天（+1.2%）」、留存「+0.35pp」、数值 / 收入「+2.85（+9.1%）」。"""
    rel = "（%+.1f%%）" % (c["rel"] * 100) if "rel" in c else ""
    if unit == "pct":
        return "%+.2fpp" % (c["diff"] * 100)
    return ("%+.3f 天" % c["diff"] if unit == "day" else "%+.2f" % c["diff"]) + rel


def lt_mde(unit, c):
    if unit == "rev":       # 收入绝对值小、单位美丝，MDE 同时给相对值
        return "±%.2f" % c["mde_abs"] + ("（±%.0f%%）" % (c["mde_rel"] * 100) if c.get("mde_rel") is not None else "")
    return "±%.2fpp" % (c["mde_abs"] * 100) if unit == "pct" else "±%.3f 天" % c["mde_abs"] if unit == "day" else "±%.2f" % c["mde_abs"]


def _seg_raw(r):
    """@seg 存成 {"_": {维度组: 行}, "d1": 截止日}，按 grp 列落库后在 res 里是 {"_": ..., "d1": ...}。"""
    if not r or "_" not in r:
        return None
    return dict(r["_"], d1=r.get("d1"))


def seg_view(e, mm, raw, tables, ctrl, trts):
    """分群下钻（全窗口累计）：每个维度 × 取值 × 指标 × 实验组出 Δ / z；同一维度内各群效应是否一致做 Cochran Q。
    下钻只用来解释，不进结论：同一维度内各群 p 值另做 BH（FDR 0.05），未过的不标显著。"""
    if not raw or not ctrl:
        return None
    seg_d1 = raw.pop("d1", None) if isinstance(raw, dict) else None
    ratio = [t for t in tables if t["kind"] == "ratio" and any(r["type"] == "total" for r in t["rows"])]
    dims_out = []
    for run, rows in raw.items():
        for d in run.split(","):
            # 边缘化：按 (组, 本维度取值) 求和充分统计量
            acc = {}
            for r in rows:
                key = (r["grp"], r["d_" + d])
                a = acc.setdefault(key, {"n": 0})
                a["n"] += r["n"]
                for t in ratio:
                    for f in STAT_F:
                        kk = "%s_%s" % (f, t["key"])
                        a[kk] = a.get(kk, 0.0) + r.get(kk, 0.0)
            vals = sorted({v for _, v in acc}, key=lambda v: -sum(a["n"] for (g, vv), a in acc.items() if vv == v))
            if len([v for v in vals if v != "na"]) < 2:      # 只有一个取值（如单国家实验的国家维度）：拆了也没东西比
                continue
            mets = []
            for t in ratio:
                lower = bool(t["lower_is_better"])
                for g in trts:
                    cells, eff = [], []
                    for v in vals:
                        per = {gg: dict(n=a["n"], **{f: a.get("%s_%s" % (f, t["key"]), 0.0) for f in STAT_F})
                               for (gg, vv), a in acc.items() if vv == v and gg in (ctrl[0], g)}
                        small = min((per.get(x) or {}).get("n", 0) for x in (ctrl[0], g)) < SEG_MIN_N
                        _, vs = _row_stats(per, ctrl, [g], lower) if not small else ({}, {})
                        c = vs.get(g)
                        cell = {"val": v, "n": {x: (per.get(x) or {}).get("n", 0) for x in (ctrl[0], g)}, "small": small}
                        if c:
                            cell.update(diff=c["diff"], rel=c.get("rel"), z=c["z"], p=c["p"], read=c["read"],
                                        ci=[c["ci_low"], c["ci_high"]])
                            eff.append((c["diff"], c["se"]))
                        cells.append(cell)
                    ok = stats.bh_pass([c.get("p") for c in cells], 0.05)
                    for c, o in zip(cells, ok):
                        c["bh"] = o
                    h = stats.heterogeneity(eff)
                    mets.append({"key": t["key"], "name": t["name"], "group": t["group"], "unit": t["unit"], "grp": g,
                                 "cells": cells, "het": {"q": h[0], "df": h[1], "p": h[2]} if h else None})
            dims_out.append({"dim": d, "name": sqlgen.SEG_DIMS[d][0],
                             "labels": {v: sqlgen.SEG_LABELS.get(v, v) for v in vals}, "metrics": mets})
    order = ["region", "ent", "qt", "nu"]
    dims_out.sort(key=lambda x: order.index(x["dim"]) if x["dim"] in order else 9)
    seg_d1 = int(seg_d1 or e["last_end"])
    return {"dims": dims_out, "min_n": SEG_MIN_N, "d1": seg_d1, "stale": seg_d1 != int(e["last_end"])}


# 对照值为 0 时没有相对变化（stats.compare 不给 rel*），数值型指标显示「—」
def _fmt_delta(t, c):
    if t["unit"] != "num":
        return "%+.2fpp" % (c["diff"] * 100)
    return "%+.1f%%" % (c["rel"] * 100) if "rel" in c else "—"


def _fmt_mde(t, c):
    if t["unit"] != "num":
        return "±%.2fpp" % (c["mde_abs"] * 100)
    return "±%.1f%%" % (c["mde_rel"] * 100) if "mde_rel" in c else "—"


def _fmt_ci(t, c):
    if t["unit"] != "num":
        return "[%+.2f, %+.2f]pp" % (c["ci_low"] * 100, c["ci_high"] * 100)
    return "[%+.1f%%, %+.1f%%]" % (c["rel_ci_low"] * 100, c["rel_ci_high"] * 100) if "rel_ci_low" in c else "—"


# 实验节奏：第 2 天查生效、第 3 天判止损、第 7 天中期读数、第 14 天出结论
NODES = ((2, "查生效"), (3, "判止损"), (7, "中期读数"), (14, "出结论"))


def stage_of(e, today=None):
    """实验当前节点：按开窗日到「今天的前一天」（数据最新日）算第 N 天，不依赖是否跑完。冻结 / 过了结束日 = 出结论。"""
    d0 = parse(e["start"])
    last = min(parse(e["end"]), (today or dt.date.today()) - dt.timedelta(days=1))
    n = (last - d0).days + 1
    if n < 1:
        return {"day": 0, "stage": "未开窗", "next": "第 1 天（%s）开始" % d0.strftime("%m-%d")}
    final = e.get("status") == "frozen" or n >= 14 or last >= parse(e["end"])
    stage = "出结论" if final else "中期读数" if n >= 7 else "判止损" if n >= 3 else "查生效"
    nxt = None if final else next((k, name) for k, name in NODES if k > n)
    return {"day": n, "stage": stage,
            "next": "第 %d 天（%s）%s" % (nxt[0], (d0 + dt.timedelta(days=nxt[0] - 1)).strftime("%m-%d"), nxt[1]) if nxt else ""}


def _summary(e, srm, tables, trts):
    """结论提要：按实验节点挑累计行、固定规则出句子，不给采纳建议。逐日同向天数不作判据。
    判止损（第 3~6 天）：前 3 天累计里北极星 / 护栏 95% CI 整体落在变差一侧 → 建议止损；
    中期（第 7~13 天）与结论（第 14 天或窗口结束）：读当前累计行，显著但 |Δ| < MDE 的不按点估计引用。"""
    # 分布 / 进组趋势只做可视化、不进判读，不挡结论
    if e.get("status") in ("queued", "running") or [k for k in e.get("pending") or [] if k not in VIS_KEYS]:
        return {"state": "wait", "text": "待全窗口跑完再出结论"}
    if e.get("status") == "failed" or e.get("error"):
        return {"state": "wait", "text": "有指标跑数失败，补齐后再出结论"}
    if srm and srm["p"] is not None and srm["p"] < 0.001:
        return {"state": "block", "text": "分流不均（SRM p = %.1e），实验未正常生效，不读效果" % srm["p"]}
    if not e.get("last_end"):
        return {"state": "wait", "text": "还没跑数"}
    d0, d1 = parse(e["start"]), parse(e["last_end"])
    n = (d1 - d0).days + 1
    final = e.get("status") == "frozen" or n >= 14
    stage = "出结论" if final else "中期读数" if n >= 7 else "判止损" if n >= 3 else "查生效"
    nxt = None if final else next((k, name) for k, name in NODES if k > n)
    head = {"day": n, "stage": stage,
            "note": "窗口已结束，共 %d 天%s" % (n, "（未满 14 天）" if n < 14 else "") if final else "",
            "next": "第 %d 天（%s）%s" % (nxt[0], (d0 + dt.timedelta(days=nxt[0] - 1)).strftime("%m-%d"), nxt[1]) if nxt else ""}

    ratio = [t for t in tables if t["kind"] == "ratio"]
    pick_days = 3 if stage == "判止损" else n
    row = {t["key"]: next((r for r in t["rows"] if r["type"] in ("cum", "total") and r.get("days") == pick_days), None)
           for t in ratio}
    north = [t for t in ratio if t["group"] == "北极星"]
    guard = [t for t in ratio if t["group"] == "护栏"]
    vs = lambda t, g: (row[t["key"]] or {}).get("vs", {}).get(g)
    out = []
    for g in trts:
        lines = []
        if stage == "查生效":
            cnt = (srm or {}).get("counts", {})
            lines.append("实验组 %s 台设备进组（对照 %s 台），分流均衡；第 3 天判止损前不读效果" % (
                format(cnt.get(g, 0), ","), format(sum(v for k, v in cnt.items() if k != g), ",")) if cnt.get(g)
                else "实验组没有设备进组，实验未生效，先查配置")
            out.append({"grp": g, "lines": lines})
            continue
        if stage == "判止损":
            neg = [(t, c) for t in north + guard for c in [vs(t, g)] if c and c["read"] == "显著负向"]
            # 判止损看修正后：变差侧序贯阈值（Pocock 型）+ 护栏 BH；名义显著但没过的单列一句、不提示止损
            hurt = [(t, c) for t, c in neg if (c.get("adj") or {}).get("ok", True)]
            for t, c in hurt:
                lines.append("%s 前 3 天显著变差 %s（95%% CI %s 整体在变差一侧），%s" % (
                    t["name"], _fmt_delta(t, c), _fmt_ci(t, c),
                    "建议止损" if t["group"] == "北极星" else "建议先拆解（机制 / 分布 / 分母）确认是真劣化后止损"))
            for t, c in neg:
                if (t, c) not in hurt:
                    lines.append("%s 前 3 天名义显著变差 %s（z = %+.2f），%s，先观察到第 7 天" % (
                        t["name"], _fmt_delta(t, c), c["z"], c["adj"]["why"]))
            if not any(vs(t, g) for t in north + guard):
                lines.append("前 3 天累计还没算出来，点「重跑」补齐后再判")
            elif not neg:
                cs = [(t, vs(t, g)) for t in north]
                lines.append("前 3 天未见止损信号：" + "；".join(
                    "%s %s（95%% CI %s）" % (t["name"], _fmt_delta(t, c), _fmt_ci(t, c)) for t, c in cs if c)
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
        proc = [sig_line(t, c) for t in ratio if t["group"] not in ("北极星", "护栏")
                for c in [vs(t, g)] if c and c["read"]]
        lines.append("；".join(proc) if proc else "过程指标无显著变化")
        if stage == "中期读数":
            lines.append("中期读数，不作推全结论")
        out.append({"grp": g, "lines": lines})
    return {"state": "ok", "head": head, "groups": out}


def _unit(m):
    return m.get("unit") or ("pct" if m.get("numerator", {}).get("agg", "uv") == "uv" or
                             m.get("numerator", {}).get("agg") == m.get("denominator", {}).get("agg") else "num")


def _row_stats(per, ctrl, trts, lower):
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
            x = stats.compare(est[c], est[t])
            if not x:
                continue
            x["z"] = x["diff"] / x["se"] if x["se"] else None
            sig = x["p"] is not None and x["p"] < 0.05
            x["read"] = ("显著正向" if (x["diff"] > 0) != lower else "显著负向") if sig else None
            vs[t] = x
    return groups, vs


@functools.lru_cache(maxsize=256)
def seq_bound(n, planned, final, harm):
    """第 n 天累计读数的序贯 |z| 阈值：看数节点 = 第 3 / 7 天（早于 n 的）+ 本次；信息比例按天数 ÷ 计划窗口天数近似，
    窗口已结束（final）本次按 1。收益侧 O'Brien-Fleming 型消耗（早期严），变差侧 Pocock 型（早期也能报警）；整体双侧 α 0.05。"""
    looks = [d for d in (3, 7) if d < n and d < planned] + [n]
    fr = [min(d / float(planned), 1.0) for d in looks]
    if final:
        fr[-1] = 1.0
    return stats.gs_bounds(fr, 0.05, "pocock" if harm else "obf")[-1]


def adjust_reads(e, tables):
    """在原判读（p < 0.05）之外加两层修正标注，原 read 不动：
    ① 多次看数（序贯）：本行 |z| 是否过了该天数的序贯阈值；
    ② 多指标多组（BH，FDR 0.05）：同一累计行上，北极星以外的比率指标 × 实验组一起修正；北极星是事先指定的主指标，不进族。
    c["adj"] = {bound, seq, bh, ok, why}；ok = 原判读显著且两层都过。"""
    if not e.get("last_end"):
        return tables
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
            n = r["days"]
            for g, c in r["vs"].items():
                if c.get("z") is None:
                    continue
                harm = (c["diff"] > 0) == bool(t["lower_is_better"])
                b = seq_bound(n, planned, final and r["type"] == "total", harm)
                c["adj"] = {"bound": b, "seq": abs(c["z"]) >= b, "bh": None}
                if t["group"] != "北极星":
                    fam.setdefault(n, []).append(c)
    for cs in fam.values():
        for c, ok in zip(cs, stats.bh_pass([c["p"] for c in cs], 0.05)):
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
    参照取最高值不取中位数：关停后残留天数过半时中位数本身就被拉低（实验 8：中位数 40，10-16~10-18 的 34~40 台漏判）。"""
    out = set()
    for g, v in day.items():
        ns = [s["n"] for s in v.values() if isinstance(s, dict) and s.get("n")]
        if g == "X" or not ns:
            continue
        top = max(ns)
        out |= {d for d, s in v.items() if isinstance(s, dict) and (s.get("n") or 0) < THIN_DAY * top}
    return out


def _tables(e, mm, res, ctrl, trts):
    """每个指标一张「总分」表：逐日行 → 前 3/7/14 天累计 → 全窗口累计；判读按 p<0.05（|z|≥1.96）。"""
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
        kind, lower = m.get("kind", "ratio"), bool(m.get("lower_is_better"))
        waits = []
        if k in pend:
            waits.append("全窗口")
        if k + "@day" in pend:
            waits.append("By天")
        if "@cum" in pend and kind == "ratio":
            waits.append("前 N 天累计")
        t = {"key": k, "name": m["name"], "group": m.get("group", "自定义"), "kind": kind, "unit": _unit(m),
             "lower_is_better": lower, "note": m.get("note", ""), "waits": waits,
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

# ─────────────────────────── 调度 ───────────────────────────

# exp_quantile 是旧名，留着兼容队列里的老任务
RUNNERS = {"funnel": run_funnel, "core_history": run_core_history, "experiment": run_experiment,
           "exp_followup": run_exp_followup, "exp_quantile": run_exp_followup, "split_backfill": run_split_backfill,
           "tb_nf_backfill": run_tb_nf_backfill}


def run_hotword(jid, p):
    from . import hotword
    hotword.run(jid, p)


RUNNERS["hotword"] = run_hotword


def run_rollout_obs(jid, p):
    from . import rollout
    rollout.run(jid, p)


RUNNERS["rollout_obs"] = run_rollout_obs


def cancel(jid):
    """取消任务。排队中 → 直接取消；运行中 → 发信号，当前这条数仓查询不再等（数仓侧已提交的停不了），已落库的保留。
    返回 'cancelled' / 'cancelling' / 状态名（已结束，不可取消；早先已取消的回 'already_cancelled'）/ None（不存在）。"""
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
                kg_client.CANCEL.set()
            return "cancelling"
        _want.discard(jid)
        return "already_cancelled" if j["status"] == "cancelled" else j["status"]


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
        store.update_experiment(e["id"], pending=[], error="补算已取消（可点重跑）")


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
        kg_client.CANCEL.clear()
        if j["id"] in _want:
            kg_client.CANCEL.set()
    try:
        store.job_log(j["id"], "开始 %s %s" % (j["kind"], j["params"]))
        if j["kind"] not in RUNNERS:
            raise RuntimeError("未知任务类型 %s（旧版本留下的）" % j["kind"])
        kg_client.check_cancel()
        RUNNERS[j["kind"]](j["id"], j["params"])
        store.finish_job(j["id"])
        _after_done(j)
    except Cancelled:
        store.job_log(j["id"], "已取消：已落库的部分保留，没跑的不再跑")
        _after_cancel(j["kind"], j["params"])
        store.finish_job(j["id"], "已取消（已落库的部分保留）", status="cancelled")
    except Exception as ex:
        for _ in range(3):
            try:
                store.job_log(j["id"], traceback.format_exc()[-1500:])
                store.finish_job(j["id"], str(ex)[:800])
                break
            except Exception:
                time.sleep(5)
    finally:
        with _lock:
            _cur = None
            _want.discard(j["id"])
            kg_client.CANCEL.clear()


def _after_done(j):
    """任务成功后：实验跑数 / 补算跑完 → 看是否到了第 3 / 7 / 14 天节点，发一次节点摘要。通知出错不影响任务状态。"""
    if j["kind"] not in ("experiment", "exp_followup", "exp_quantile"):
        return
    try:
        from . import notify
        notify.checkpoint(int(j["params"]["exp_id"]))
    except Exception:
        traceback.print_exc()


def daily_tick():
    """每分钟检查：到点刷新漏斗（近 BACKFILL_DAYS 天里缺的/不完整的 + 昨天），续跑未冻结实验；12:00 / RETRY_UNTIL_HOUR 查日更是否跑齐。"""
    from . import maint, notify
    st = {"run_day": None, "retry": 0, "net_wait": False}
    last_maint_day = None
    since = time.time()
    while True:
        now = dt.datetime.now()
        today = now.date()
        if last_maint_day != today:       # 每天第一次 tick（含启动后第一次）：库快照 + 日志轮转；失败当天不再重试
            last_maint_day = today
            try:
                maint.daily(today)
            except Exception:             # 维护出错不能挡调度
                traceback.print_exc()
        try:
            _daily_step(now, st)
        except Exception:                 # 调度异常不能让日更线程退出（否则直到重启都不再刷新）
            traceback.print_exc()
        try:
            notify.check_refresh(now, since)
        except Exception:                 # 通知出错同样不能挡调度
            traceback.print_exc()
        time.sleep(60)


# KG 连不上的原因（"" = 能连）：日更提交前查一次，连不上就不提交；页面顶部提醒 / 失败通知读它
NET_DOWN = ""


def _daily_step(now, st):
    """日更线程每分钟一次：到点整批提交（force），之后每 RETRY_GAP_HOURS 小时补缺到 RETRY_UNTIL_HOUR。
    提交前先查 KG 连通：连不上不提交、不记这次（下一分钟再查），恢复后立刻补上，过了重试截止时刻也补。"""
    global NET_DOWN
    due = (now.hour, now.minute) >= C.DAILY_AT
    force = due and st["run_day"] != now.date()
    retry = due and not force and (st["net_wait"] or (now.hour < C.RETRY_UNTIL_HOUR
                                                      and time.time() - st["retry"] > C.RETRY_GAP_HOURS * 3600))
    if not (force or retry):
        return
    why = kg_client.reachable()
    if why != NET_DOWN:
        print("[daily] %s %s" % (now.strftime("%H:%M"), "KG 连不上，暂停提交跑数：" + why if why else "KG 已可连，继续日更"),
              file=sys.stderr, flush=True)
        NET_DOWN = why
    st["net_wait"] = bool(why)
    if why:
        return
    if force:
        _schedule_daily(force=True)
        st["run_day"], st["retry"] = now.date(), time.time()
    elif _schedule_daily(force=False):
        st["retry"] = time.time()


def _schedule_daily(force):
    y = yesterday()
    start = ymd(parse(y) - dt.timedelta(days=C.BACKFILL_DAYS - 1))
    todo = False
    # 整机房 + 拉过的国家视图（看过一次就每天自动续）
    for m in list(C.MARKETS) + [k for k in store.funnel_scopes() if k not in C.MARKETS]:
        st = store.day_status(m)
        missing = [d for d in _days(start, y) if not st.get(d)]
        if missing:
            submit("funnel", {"markets": [m], "start": min(missing), "end": y})
            todo = True
    from . import hotword
    hw = store.last_job("hotword")
    # 热词：每天强制一次；当天失败的在重试窗口内再跑
    # 服务每次重启也会走一遍 force：今天已经拉到昨天的就不重复拉
    fresh = hotword_fresh(y, hw)
    if not (hw and hw["status"] in ("queued", "running")) and not fresh and (force or (hw and hw["status"] == "failed")):
        hotword.schedule(submit, y)
        todo = todo or not force
    # 实验：网络 / KG 超时导致的失败（11-11 VPN 断开时整批失败），和漏斗一起在重试窗口内自动补跑，整批时也带上
    for e in store.experiments():
        if (e["status"] == "failed" and transient_error(e.get("error")) and y >= int(e["start"])
                and (e["last_end"] or 0) < min(int(e["end"]), y)):
            submit("experiment", {"exp_id": e["id"]})
            todo = True
    if force:
        # 实验配置：每天跟规划文档同步一次（建新一轮的回收实验 + 核验）；失败只记快照，不挡其他调度
        try:
            from . import expsync
            expsync.sync_config(lambda s: None)
        except Exception:
            traceback.print_exc()
        for e in store.experiments():
            # 开窗日还没到（昨天 < 开窗日）的等待实验不提交：窗口里还没数
            if e["status"] in ("active",) and y >= int(e["start"]) and (e["last_end"] or 0) < min(int(e["end"]), y):
                submit("experiment", {"exp_id": e["id"]})
        # 推全观测：各轮 / 全局累计需要的完整周（已过完的）缺数或缺封顶才提交；失败不挡其他调度
        try:
            from . import rollout
            if rollout.pending(y):
                submit("rollout_obs", {})
        except Exception:
            traceback.print_exc()
    return todo


def transient_error(err):
    """网络断开 / KG 无响应 / 上游超时这类过一会儿就好的失败；配置错、取消等不算，不自动重跑。"""
    return kg_client.net_suspect(err) or any(w in (err or "") for w in ("无响应", "timed out", "Timed out", "超时"))


def hotword_fresh(y, hw=None):
    """热词今天已拉到昨天：最近一次热词任务成功、end = 昨天、今天跑完的。日更调度和失败提醒共用。"""
    hw = hw or store.last_job("hotword")
    return bool(hw and hw["status"] == "done" and json.loads(hw["params"] or "{}").get("end") == y
                and dt.date.fromtimestamp(hw["finished_at"] or 0) == dt.date.today())


def _days(d0, d1):
    d = parse(d0)
    while ymd(d) <= d1:
        yield ymd(d)
        d += dt.timedelta(days=1)


def start_background():
    threading.Thread(target=worker, daemon=True, name="worker").start()
    threading.Thread(target=daily_tick, daemon=True, name="daily").start()
