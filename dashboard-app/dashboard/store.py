"""SQLite 存储。每次调用开新连接，线程安全够用。"""
import json
import os
import sqlite3
import time

from . import config as C
from .presets import PRESETS
from .sqlgen import FUNNEL_COLS

SCHEMA = """
CREATE TABLE IF NOT EXISTS funnel_daily (
  market TEXT, date INTEGER, ek TEXT, %s, updated_at REAL,
  PRIMARY KEY (market, date, ek));
CREATE TABLE IF NOT EXISTS day_status (
  market TEXT, date INTEGER, complete INTEGER, note TEXT, updated_at REAL,
  PRIMARY KEY (market, date));
CREATE TABLE IF NOT EXISTS position_daily (
  market TEXT, date INTEGER, position INTEGER, tb INTEGER, expose INTEGER, click INTEGER,
  PRIMARY KEY (market, date, position, tb));
CREATE TABLE IF NOT EXISTS flow_daily (
  market TEXT, date INTEGER, payload TEXT, updated_at REAL, PRIMARY KEY (market, date));
CREATE TABLE IF NOT EXISTS core_daily (
  market TEXT, date INTEGER, payload TEXT, updated_at REAL, PRIMARY KEY (market, date));
CREATE TABLE IF NOT EXISTS split_daily (
  market TEXT, date INTEGER, payload TEXT, updated_at REAL, PRIMARY KEY (market, date));
CREATE TABLE IF NOT EXISTS dau_daily (
  market TEXT, date INTEGER, dau INTEGER, PRIMARY KEY (market, date));
CREATE TABLE IF NOT EXISTS metrics (
  key TEXT PRIMARY KEY, def TEXT, preset INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, market TEXT, groups TEXT,
  start INTEGER, end INTEGER, metric_keys TEXT, caps TEXT, status TEXT,
  last_end INTEGER, error TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS exp_results (
  exp_id INTEGER, metric_key TEXT, grp TEXT, payload TEXT, run_end INTEGER, run_at REAL,
  PRIMARY KEY (exp_id, metric_key, grp));
CREATE TABLE IF NOT EXISTS job_queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, label TEXT, kg_task_id TEXT, sql TEXT,
  rows INTEGER, seconds REAL, error TEXT, at REAL);
CREATE INDEX IF NOT EXISTS job_queries_job ON job_queries(job_id);
CREATE TABLE IF NOT EXISTS hw_uv (
  date INTEGER, region TEXT, payload TEXT, PRIMARY KEY (date, region));
CREATE TABLE IF NOT EXISTS hw_total (
  date INTEGER, region TEXT, source TEXT, payload TEXT, PRIMARY KEY (date, region, source));
CREATE TABLE IF NOT EXISTS hw_words (
  date INTEGER, region TEXT, word TEXT, payload TEXT, PRIMARY KEY (date, region, word));
CREATE TABLE IF NOT EXISTS hw_config (
  id INTEGER PRIMARY KEY, vals TEXT, fetched_at REAL, error TEXT, error_at REAL);
CREATE TABLE IF NOT EXISTS exp_config (
  id INTEGER PRIMARY KEY, md TEXT, fetched_at REAL, error TEXT, error_at REAL, report TEXT, report_at REAL);
CREATE TABLE IF NOT EXISTS rollout_daily (
  ukey TEXT, date INTEGER, payload TEXT, updated_at REAL, PRIMARY KEY (ukey, date));
CREATE TABLE IF NOT EXISTS rollout_cap (
  sig TEXT, w0 INTEGER, w1 INTEGER, cap REAL, n_dev INTEGER, updated_at REAL, PRIMARY KEY (sig, w0));
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, params TEXT, status TEXT,
  log TEXT DEFAULT '', error TEXT, created_at REAL, started_at REAL, finished_at REAL);
CREATE TABLE IF NOT EXISTS notify_log (
  key TEXT PRIMARY KEY, sent_at REAL);
CREATE TABLE IF NOT EXISTS ver_config (
  id INTEGER PRIMARY KEY, vals TEXT, fetched_at REAL, error TEXT, error_at REAL, report TEXT);
CREATE TABLE IF NOT EXISTS decision_override (
  no INTEGER, market TEXT, gid TEXT, decision TEXT, src TEXT, updated_at REAL, PRIMARY KEY (no, market, gid));
""" % ", ".join("%s INTEGER" % c for c in FUNNEL_COLS)


def conn():
    os.makedirs(C.DATA_DIR, exist_ok=True)
    c = sqlite3.connect(C.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        now = time.time()
        mcols = [r["name"] for r in c.execute("PRAGMA table_info(metrics)")]
        # 用户改过的预置指标：重启时不再用代码里的定义覆盖（可在页面「恢复预置」）
        if "edited" not in mcols:
            c.execute("ALTER TABLE metrics ADD COLUMN edited INTEGER DEFAULT 0")
        for key, d in PRESETS:
            c.execute("INSERT INTO metrics(key, def, preset, created_at, edited) VALUES (?,?,1,?,0) "
                      "ON CONFLICT(key) DO UPDATE SET def = excluded.def WHERE metrics.edited = 0",
                      (key, json.dumps(d, ensure_ascii=False), now))
        # 老库补列：实验国家筛选（空 = 整个市场）
        cols = [r["name"] for r in c.execute("PRAGMA table_info(experiments)")]
        if "regions" not in cols:
            c.execute("ALTER TABLE experiments ADD COLUMN regions TEXT")
        # 待补算的分位数指标 key 列表（比率指标先出，分位数排后面单独一个任务）
        if "pending" not in cols:
            c.execute("ALTER TABLE experiments ADD COLUMN pending TEXT")
        # 要 By天 出数的指标 key 列表
        if "daily_keys" not in cols:
            c.execute("ALTER TABLE experiments ADD COLUMN daily_keys TEXT")
        # 上线日：P99 封顶取上线日之前 7 天（空 = 开窗日）
        if "launch" not in cols:
            c.execute("ALTER TABLE experiments ADD COLUMN launch INTEGER")
        # 只影响 CUPED / 分群 / 分布的补算失败：单独记，不挡结论提要（error 会挡）
        if "vis_error" not in cols:
            c.execute("ALTER TABLE experiments ADD COLUMN vis_error TEXT")
        # 按事件排除的搜索 source（如 ["5"] = 不计热词）；空 = 全部 source
        if "excl_src" not in cols:
            c.execute("ALTER TABLE experiments ADD COLUMN excl_src TEXT")
        # 热词运营配置：同步后的核验结果
        # Toolbar 排兜底去重 UV（2026-11-21 加；老行为 NULL = 还没补，前端退回 uv_nf − uv_site_nf 近似）
        fcols = [r["name"] for r in c.execute("PRAGMA table_info(funnel_daily)")]
        if "uv_tb_nf" not in fcols:
            c.execute("ALTER TABLE funnel_daily ADD COLUMN uv_tb_nf INTEGER")
        hcols = [r["name"] for r in c.execute("PRAGMA table_info(hw_config)")]
        if "report" not in hcols:
            c.execute("ALTER TABLE hw_config ADD COLUMN report TEXT")
            c.execute("ALTER TABLE hw_config ADD COLUMN report_at REAL")
        for r in c.execute("SELECT params FROM jobs WHERE kind IN ('exp_quantile','exp_followup') AND status='running'").fetchall():
            c.execute("UPDATE experiments SET pending='[]', error='补算（分位数 / By天）被服务重启中断，可点重跑' WHERE id=?",
                      (json.loads(r["params"])["exp_id"],))
        # 服务重启时把卡在 running 的任务标成失败；它们对应的实验也标失败（否则一直显示跑数中、不能编辑、不再续跑）
        for r in c.execute("SELECT params FROM jobs WHERE kind='experiment' AND status='running'").fetchall():
            c.execute("UPDATE experiments SET status='failed', error='跑数被服务重启中断，点「重跑」继续' WHERE id=? AND status IN ('queued','running')",
                      (json.loads(r["params"])["exp_id"],))
        c.execute("UPDATE jobs SET status='failed', error='服务重启中断', finished_at=? WHERE status='running'", (now,))


# ── 漏斗 ──

def fill_tb_nf(market, rows):
    """只回填 uv_tb_nf（补历史用），不动其他列和 day_status。返回更新行数。"""
    now, n = time.time(), 0
    with conn() as c:
        for r in rows:
            n += c.execute("UPDATE funnel_daily SET uv_tb_nf=?, updated_at=? WHERE market=? AND date=? AND ek=?",
                           (int(r["uv_tb_nf"] or 0), now, market, int(r["date"]), r["ek"])).rowcount
    return n


def save_funnel(market, rows, positions, dau, statuses, flow=()):
    now = time.time()
    with conn() as c:
        for r in flow:
            c.execute("INSERT OR REPLACE INTO flow_daily VALUES (?,?,?,?)",
                      (market, int(r["date"]), json.dumps({k: int(v or 0) for k, v in r.items() if k != "date"}), now))
        for r in rows:
            # 显式列名：老库后补的列在 updated_at 之后，不能按位置插
            c.execute("INSERT OR REPLACE INTO funnel_daily (market, date, ek, %s, updated_at) VALUES (?,?,?,%s,?)"
                      % (", ".join(FUNNEL_COLS), ",".join("?" * len(FUNNEL_COLS))),
                      [market, int(r["date"]), r["ek"]] + [int(r[k] or 0) for k in FUNNEL_COLS] + [now])
        for p in positions:
            c.execute("INSERT OR REPLACE INTO position_daily VALUES (?,?,?,?,?,?)",
                      (market, int(p["date"]), int(p["position"]), int(p["tb"]), int(p["expose"]), int(p["click"])))
        for d in dau:
            c.execute("INSERT OR REPLACE INTO dau_daily VALUES (?,?,?)", (market, int(d["date"]), int(d["dau"])))
        for date, (ok, note) in statuses.items():
            c.execute("INSERT OR REPLACE INTO day_status VALUES (?,?,?,?,?)", (market, date, int(ok), note, now))


def funnel(market, d0, d1):
    if market == ALL:
        return _funnel_all(d0, d1)
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM funnel_daily WHERE market=? AND date BETWEEN ? AND ? ORDER BY date", (market, d0, d1))]
        pos = [dict(r) for r in c.execute(
            "SELECT position, tb, SUM(expose) AS expose, SUM(click) AS click FROM position_daily "
            "WHERE market=? AND date BETWEEN ? AND ? GROUP BY position, tb ORDER BY position", (market, d0, d1))]
        dau = {r["date"]: r["dau"] for r in c.execute(
            "SELECT date, dau FROM dau_daily WHERE market=? AND date BETWEEN ? AND ?", (market, d0, d1))}
        st = {r["date"]: {"complete": bool(r["complete"]), "note": r["note"], "updated_at": r["updated_at"]}
              for r in c.execute("SELECT * FROM day_status WHERE market=? AND date BETWEEN ? AND ?", (market, d0, d1))}
        flow = {r["date"]: json.loads(r["payload"]) for r in c.execute(
            "SELECT date, payload FROM flow_daily WHERE market=? AND date BETWEEN ? AND ?", (market, d0, d1))}
    return rows, pos, dau, st, flow


# 全机房视图：不单独取数，读时把三个机房（RU 机房 = region RU）已落库的数按天相加。
# 三个机房设备几乎不重叠（2026-10-20~26 搜索设备 186 万台里跨机房 12 台），UV 直接相加。
# 某天三个机房都有数才出这天；任一机房缺 / 不完整 → 这天标不完整，备注写哪个机房。
ALL = "ALL"
ALL_PARTS = tuple(C.MARKETS)


def _sum_dicts(ds, skip=()):
    """逐键相加（值为 None 的键结果为 None）；skip 里的键不出。"""
    out = {}
    for d in ds:
        for k, v in d.items():
            if k in skip:
                continue
            out[k] = None if v is None or (k in out and out[k] is None) else out.get(k, 0) + v
    return out


def _all_status(parts_st):
    """parts_st = {机房: {date: {"complete", "note", "updated_at"}}} → 全机房每天的状态。"""
    out = {}
    for d in sorted({d for st in parts_st.values() for d in st}):
        bad = []
        for m, st in parts_st.items():
            s = st.get(d)
            if not s:
                bad.append("%s 未拉取" % m)
            elif not s["complete"]:
                bad.append("%s：%s" % (m, s["note"] or "不完整"))
        out[d] = {"complete": not bad, "note": "；".join(bad),
                  "updated_at": max(st[d]["updated_at"] for st in parts_st.values() if d in st)}
    return out


def _funnel_all(d0, d1):
    parts = {m: funnel(m, d0, d1) for m in ALL_PARTS}
    full = set.intersection(*({r["date"] for r in p[0]} for p in parts.values()))
    acc = {}
    for m, p in parts.items():
        for r in p[0]:
            if r["date"] in full:
                a = acc.setdefault((r["date"], r["ek"]), {"market": ALL, "date": r["date"], "ek": r["ek"], **{k: 0 for k in FUNNEL_COLS}})
                for k in FUNNEL_COLS:
                    # 任一机房这天还没补 uv_tb_nf：合计也置空，前端走近似
                    a[k] = None if a[k] is None or (r[k] is None and k == "uv_tb_nf") else a[k] + (r[k] or 0)
    rows = sorted(acc.values(), key=lambda r: (r["date"], r["ek"]))
    pacc = {}
    for p in parts.values():
        for r in p[1]:
            a = pacc.setdefault((r["position"], r["tb"]), {"position": r["position"], "tb": r["tb"], "expose": 0, "click": 0})
            a["expose"] += r["expose"] or 0
            a["click"] += r["click"] or 0
    pos = sorted(pacc.values(), key=lambda r: (r["position"], r["tb"]))
    st = _all_status({m: p[3] for m, p in parts.items()})
    fdays = set.intersection(*(set(p[4]) for p in parts.values()))
    flow = {d: _sum_dicts(p[4][d] for p in parts.values()) for d in sorted(fdays)}
    return rows, pos, {}, st, flow


def save_daily_payload(table, market, rows):
    """core_daily / split_daily：每天一行 JSON。"""
    assert table in ("core_daily", "split_daily")
    now = time.time()
    with conn() as c:
        for r in rows:
            c.execute("INSERT OR REPLACE INTO %s VALUES (?,?,?,?)" % table,
                      (market, int(r["date"]), json.dumps({k: (int(v) if v is not None else None)
                                                           for k, v in r.items() if k != "date"}), now))


def daily_payload(table, market, d0, d1):
    assert table in ("core_daily", "split_daily")
    if market == ALL:
        # 人均 VV：各机房先按自己当天的 P99 封顶再相加；合并后的 P99 没有意义，不出
        parts = [daily_payload(table, m, d0, d1) for m in ALL_PARTS]
        days = set.intersection(*(set(p) for p in parts))
        return {d: _sum_dicts((p[d] for p in parts), skip=("vv_p99",)) for d in sorted(days)}
    with conn() as c:
        return {r["date"]: json.loads(r["payload"]) for r in c.execute(
            "SELECT date, payload FROM %s WHERE market=? AND date BETWEEN ? AND ?" % table, (market, d0, d1))}


def core_series(market, d0, d1):
    """核心指标逐日（周均走势用）：core_daily + DAU。
    complete = False：漏斗判过不完整；只补过历史、没跑过漏斗的天，按结果页加载 UV 低于前 7 天中位数 60% 判。
    全机房：三个机房各自判完整，三个都有数才出这天、都完整才算完整；没有 DAU。"""
    if market == ALL:
        parts = [core_series(m, d0, d1) for m in ALL_PARTS]
        days = set.intersection(*(set(p) for p in parts))
        return {d: dict(_sum_dicts((p[d] for p in parts), skip=("vv_p99", "complete", "dau")),
                        complete=all(p[d]["complete"] for p in parts), dau=None) for d in sorted(days)}
    out = daily_payload("core_daily", market, d0, d1)
    with conn() as c:
        st = {r["date"]: bool(r["complete"]) for r in c.execute(
            "SELECT date, complete FROM day_status WHERE market=? AND date BETWEEN ? AND ?", (market, d0, d1))}
        dau = {r["date"]: r["dau"] for r in c.execute(
            "SELECT date, dau FROM dau_daily WHERE market=? AND date BETWEEN ? AND ?", (market, d0, d1))}
    hist = []
    for d in sorted(out):
        v = out[d]
        med = sorted(hist[-7:])[len(hist[-7:]) // 2] if hist else None
        load = v.get("vv_den_all")
        v["complete"] = st[d] if d in st else bool(load) and not (med and load < 0.6 * med)
        v["dau"] = dau.get(d)
        if v["complete"]:
            hist.append(load)
    return out


# ── 推全观测 ──
# rollout_daily：观测单元 × 日的聚合（ukey = 机房|国家条件|封顶值，见 rollout.data_key）；
# rollout_cap：观测单元人均 VV 封顶（sig = 机房|国家条件，w0~w1 = 封顶窗口），算一次写死

def save_rollout(ukey, rows):
    now = time.time()
    with conn() as c:
        for r in rows:
            c.execute("INSERT OR REPLACE INTO rollout_daily VALUES (?,?,?,?)",
                      (ukey, int(r["date"]), json.dumps({k: v for k, v in r.items() if k != "date"}), now))


def rollout_days(ukey):
    with conn() as c:
        return {r["date"]: json.loads(r["payload"]) for r in c.execute(
            "SELECT date, payload FROM rollout_daily WHERE ukey=?", (ukey,))}


def save_rollout_cap(sig, w0, w1, cap, n_dev):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO rollout_cap VALUES (?,?,?,?,?,?)", (sig, w0, w1, cap, n_dev, time.time()))


def rollout_caps():
    with conn() as c:
        return {(r["sig"], r["w0"]): r["cap"] for r in c.execute("SELECT sig, w0, cap FROM rollout_cap")}


# ── 热词运营 ──

def _ints(r, skip):
    return {k: int(v or 0) for k, v in r.items() if k not in skip}


def save_hw_uv(rows):
    with conn() as c:
        for r in rows:
            c.execute("INSERT OR REPLACE INTO hw_uv VALUES (?,?,?)",
                      (int(r["date"]), r["region"], json.dumps(_ints(r, ("date", "region")))))


def save_hw_total(d0, d1, rows):
    with conn() as c:
        c.execute("DELETE FROM hw_total WHERE date BETWEEN ? AND ?", (d0, d1))
        for r in rows:
            c.execute("INSERT OR REPLACE INTO hw_total VALUES (?,?,?,?)",
                      (int(r["date"]), r["region"], r["source"], json.dumps(_ints(r, ("date", "region", "source")))))


def save_hw_words(d0, d1, rows):
    """词按 strip + lower 归一（与运营配置的匹配键一致）；归一后撞键的累加。"""
    acc = {}
    for r in rows:
        k = (int(r["date"]), r["region"], str(r["query_word"] or "").strip().lower())
        p = acc.setdefault(k, {})
        for f, v in _ints(r, ("date", "region", "query_word")).items():
            p[f] = p.get(f, 0) + v
    with conn() as c:
        c.execute("DELETE FROM hw_words WHERE date BETWEEN ? AND ?", (d0, d1))
        c.executemany("INSERT INTO hw_words VALUES (?,?,?,?)", [(*k, json.dumps(v)) for k, v in acc.items()])


def hw_uv(d0, d1):
    with conn() as c:
        return {(r["date"], r["region"]): json.loads(r["payload"]) for r in c.execute(
            "SELECT * FROM hw_uv WHERE date BETWEEN ? AND ?", (d0, d1))}


def hw_total(d0, d1):
    with conn() as c:
        return {(r["date"], r["region"], r["source"]): json.loads(r["payload"]) for r in c.execute(
            "SELECT * FROM hw_total WHERE date BETWEEN ? AND ?", (d0, d1))}


def hw_words(d0, d1):
    with conn() as c:
        return {(r["date"], r["region"], r["word"]): json.loads(r["payload"]) for r in c.execute(
            "SELECT * FROM hw_words WHERE date BETWEEN ? AND ?", (d0, d1))}


def hw_dates():
    """三张都有数的日期才算拉齐。"""
    with conn() as c:
        sets = [{r[0] for r in c.execute("SELECT DISTINCT date FROM %s" % t)} for t in ("hw_uv", "hw_total", "hw_words")]
    return set.intersection(*sets)


def save_hw_config(vals, error, report=None, keep_time=False):
    """vals 为 None = 同步失败：保留上次快照，只记错误。report = 同步后的核验结果；keep_time = 只更新核验，不改同步时间。"""
    now = time.time()
    with conn() as c:
        if vals is None:
            c.execute("INSERT INTO hw_config(id, error, error_at) VALUES (1,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET error=excluded.error, error_at=excluded.error_at", (error, now))
        elif keep_time:
            c.execute("UPDATE hw_config SET report=?, report_at=? WHERE id=1", (json.dumps(report, ensure_ascii=False), now))
        else:
            c.execute("INSERT OR REPLACE INTO hw_config(id, vals, fetched_at, error, error_at, report, report_at) VALUES (1,?,?,NULL,NULL,?,?)",
                      (json.dumps(vals, ensure_ascii=False), now, json.dumps(report, ensure_ascii=False), now))


def hw_config():
    with conn() as c:
        r = c.execute("SELECT * FROM hw_config WHERE id=1").fetchone()
    if not r:
        return {}
    return {"values": json.loads(r["vals"]) if r["vals"] else None, "fetched_at": r["fetched_at"],
            "error": r["error"], "error_at": r["error_at"],
            "report": json.loads(r["report"]) if r["report"] else None, "report_at": r["report_at"]}


def save_exp_config(md, error, report=None):
    """实验配置（规划文档）快照。md 为 None = 同步失败：保留上次快照，只记错误。"""
    now = time.time()
    with conn() as c:
        if md is None:
            c.execute("INSERT INTO exp_config(id, error, error_at) VALUES (1,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET error=excluded.error, error_at=excluded.error_at", (error, now))
        else:
            c.execute("INSERT OR REPLACE INTO exp_config(id, md, fetched_at, error, error_at, report, report_at) VALUES (1,?,?,NULL,NULL,?,?)",
                      (md, now, json.dumps(report, ensure_ascii=False), now))


def save_exp_report(report):
    """只换核验结果（改了决策标签后重新核对），文档快照和同步时间不动。"""
    with conn() as c:
        c.execute("UPDATE exp_config SET report=?, report_at=? WHERE id=1", (json.dumps(report, ensure_ascii=False), time.time()))


def decision_overrides():
    """实验观测决策标签（页面上「按文档改」写入）：{(轮, 机房, 实验组 expId): 决策}，优先于 observe.ROUNDS / 默认「进行中」。"""
    with conn() as c:
        return {(r["no"], r["market"], r["gid"]): r["decision"] for r in c.execute("SELECT * FROM decision_override")}


def set_decision(no, market, gid, decision, src):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO decision_override(no, market, gid, decision, src, updated_at) VALUES (?,?,?,?,?,?)",
                  (no, market, gid, decision, src, time.time()))


def exp_config():
    with conn() as c:
        r = c.execute("SELECT * FROM exp_config WHERE id=1").fetchone()
    if not r:
        return {}
    return {"md": r["md"], "fetched_at": r["fetched_at"], "error": r["error"], "error_at": r["error_at"],
            "report": json.loads(r["report"]) if r["report"] else None, "report_at": r["report_at"]}


def save_ver_config(vals, error, report=None):
    """「分开看」版本列表（发版记录表同步）。vals 为 None = 同步失败：保留上次列表，只记错误。"""
    now = time.time()
    with conn() as c:
        if vals is None:
            c.execute("INSERT INTO ver_config(id, error, error_at) VALUES (1,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET error=excluded.error, error_at=excluded.error_at", (error, now))
        else:
            c.execute("INSERT OR REPLACE INTO ver_config(id, vals, fetched_at, error, error_at, report) VALUES (1,?,?,NULL,NULL,?)",
                      (json.dumps(vals, ensure_ascii=False), now, json.dumps(report, ensure_ascii=False)))


def save_ver_report(report):
    with conn() as c:
        c.execute("UPDATE ver_config SET report=? WHERE id=1", (json.dumps(report, ensure_ascii=False),))


def ver_config():
    with conn() as c:
        r = c.execute("SELECT * FROM ver_config WHERE id=1").fetchone()
    if not r:
        return {}
    return {"vals": json.loads(r["vals"]) if r["vals"] else None, "fetched_at": r["fetched_at"],
            "error": r["error"], "error_at": r["error_at"], "report": json.loads(r["report"]) if r["report"] else None}


def hw_word_keys():
    """数仓热词卡渠道出现过的 (市场, 词) → 有搜索的日期，核验配置词写法用。"""
    out = {}
    with conn() as c:
        for r in c.execute("SELECT region, word, date FROM hw_words"):
            out.setdefault((r[0], r[1]), set()).add(r[2])
    return out


def funnel_scopes():
    with conn() as c:
        return [r["market"] for r in c.execute("SELECT DISTINCT market FROM day_status")]


def day_status(market):
    with conn() as c:
        return {r["date"]: bool(r["complete"]) for r in
                c.execute("SELECT date, complete FROM day_status WHERE market=?", (market,))}


def result_load_history(market, before, n=7):
    with conn() as c:
        return [r["uv"] for r in c.execute(
            "SELECT uv FROM funnel_daily WHERE market=? AND ek='result_load' AND date < ? ORDER BY date DESC LIMIT ?",
            (market, before, n))]


# ── 指标 ──

def metrics():
    with conn() as c:
        return [{"key": r["key"], "preset": bool(r["preset"]), "edited": bool(r["edited"]), **json.loads(r["def"])}
                for r in c.execute("SELECT * FROM metrics ORDER BY preset DESC, created_at")]


def metric_map():
    return {m["key"]: m for m in metrics()}


def save_metric(key, d):
    with conn() as c:
        row = c.execute("SELECT preset FROM metrics WHERE key=?", (key,)).fetchone()
        if row:
            raise ValueError("key「%s」已存在；要改它请在左侧列表点「编辑」" % key)
        c.execute("INSERT INTO metrics(key, def, preset, created_at, edited) VALUES (?,?,0,?,0)",
                  (key, json.dumps(d, ensure_ascii=False), time.time()))


def update_metric(key, d, edited=True):
    """改已有指标的定义（预置指标改后标 edited，重启不再被代码覆盖；edited=False = 恢复预置）。"""
    with conn() as c:
        row = c.execute("SELECT preset FROM metrics WHERE key=?", (key,)).fetchone()
        if not row:
            raise ValueError("指标不存在：%s" % key)
        c.execute("UPDATE metrics SET def=?, edited=? WHERE key=?",
                  (json.dumps(d, ensure_ascii=False), int(bool(row["preset"]) and edited), key))


def experiments_using(key):
    return [e for e in experiments() if key in e["metric_keys"]]


def delete_metric(key):
    with conn() as c:
        c.execute("DELETE FROM metrics WHERE key=? AND preset=0", (key,))


# ── 实验 ──

def _exp(r):
    d = dict(r)
    for k in ("groups", "metric_keys", "caps", "regions", "pending", "daily_keys", "excl_src"):
        d[k] = json.loads(d.get(k) or ("{}" if k == "caps" else "[]"))
    return d


def create_experiment(name, market, groups, start, end, metric_keys, regions=(), daily_keys=(), launch=None, excl_src=()):
    now = time.time()
    with conn() as c:
        cur = c.execute("INSERT INTO experiments(name, market, groups, start, end, metric_keys, caps, status, "
                        "created_at, updated_at, regions, daily_keys, launch, excl_src) VALUES (?,?,?,?,?,?,'{}','queued',?,?,?,?,?,?)",
                        (name, market, json.dumps(groups), start, end, json.dumps(metric_keys), now, now,
                         json.dumps(list(regions)), json.dumps(list(daily_keys)), launch, json.dumps(list(excl_src))))
        return cur.lastrowid


def experiments():
    with conn() as c:
        return [_exp(r) for r in c.execute("SELECT * FROM experiments ORDER BY id DESC")]


def experiment(eid):
    with conn() as c:
        r = c.execute("SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        e = _exp(r)
        e["results"] = [dict(x, payload=json.loads(x["payload"])) for x in
                        c.execute("SELECT * FROM exp_results WHERE exp_id=?", (eid,))]
        return e


def update_experiment(eid, **kw):
    kw["updated_at"] = time.time()
    for k in ("caps", "groups", "metric_keys", "pending", "daily_keys", "regions", "excl_src"):
        if k in kw:
            kw[k] = json.dumps(kw[k])
    with conn() as c:
        c.execute("UPDATE experiments SET %s WHERE id=?" % ", ".join("%s=?" % k for k in kw),
                  list(kw.values()) + [eid])


def set_vis_error(eid, text):
    with conn() as c:
        c.execute("UPDATE experiments SET vis_error=? WHERE id=?", (text, eid))


def save_exp_results(eid, metric_key, rows, run_end):
    now = time.time()
    with conn() as c:
        c.execute("DELETE FROM exp_results WHERE exp_id=? AND metric_key=?", (eid, metric_key))
        for grp, payload in rows.items():
            c.execute("INSERT INTO exp_results VALUES (?,?,?,?,?,?)",
                      (eid, metric_key, grp, json.dumps(payload), run_end, now))


def delete_exp_results(eid, keys=None):
    """keys=None 删该实验全部结果。"""
    with conn() as c:
        if keys is None:
            c.execute("DELETE FROM exp_results WHERE exp_id=?", (eid,))
        else:
            for k in keys:
                c.execute("DELETE FROM exp_results WHERE exp_id=? AND metric_key=?", (eid, k))


def delete_experiment(eid):
    with conn() as c:
        c.execute("DELETE FROM exp_results WHERE exp_id=?", (eid,))
        c.execute("DELETE FROM experiments WHERE id=?", (eid,))


# ── 任务 ──

def enqueue(kind, params):
    with conn() as c:
        dup = c.execute("SELECT id FROM jobs WHERE kind=? AND params=? AND status IN ('queued','running')",
                        (kind, json.dumps(params, sort_keys=True))).fetchone()
        if dup:
            return dup["id"]
        return c.execute("INSERT INTO jobs(kind, params, status, created_at) VALUES (?,?,'queued',?)",
                         (kind, json.dumps(params, sort_keys=True), time.time())).lastrowid


def next_job():
    with conn() as c:
        # 实验跑数 / 补算优先，其余（漏斗、热词、推全观测）排在后面；同一档按提交顺序
        r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY "
                      "CASE WHEN kind IN ('experiment', 'exp_followup', 'exp_quantile') THEN 0 ELSE 1 END, id LIMIT 1").fetchone()
        if not r:
            return None
        # 只接还在排队的：选中到改状态之间可能被取消
        if not c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                         (time.time(), r["id"])).rowcount:
            return None
        return dict(r, params=json.loads(r["params"]))


def job_log(jid, line):
    with conn() as c:
        c.execute("UPDATE jobs SET log = log || ? WHERE id=?", (time.strftime("%H:%M:%S ") + line + "\n", jid))


def job_query(jid, q):
    with conn() as c:
        c.execute("INSERT INTO job_queries(job_id, label, kg_task_id, sql, rows, seconds, error, at) VALUES (?,?,?,?,?,?,?,?)",
                  (jid, q["label"], q.get("kg_task_id"), q["sql"].strip(), q.get("rows"), q.get("seconds"), q.get("error"), time.time()))


def job_queries(jid):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM job_queries WHERE job_id=? ORDER BY id", (jid,))]


def finish_job(jid, error=None, status=None):
    with conn() as c:
        c.execute("UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
                  (status or ("failed" if error else "done"), error, time.time(), jid))


def cancel_queued_job(jid):
    """排队中的直接标取消；返回该任务（取消前的状态），不存在返回 None。"""
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if r and r["status"] == "queued":
            c.execute("UPDATE jobs SET status='cancelled', error='已取消（排队中，未开始跑）', finished_at=? WHERE id=? AND status='queued'",
                      (time.time(), jid))
        return dict(r, params=json.loads(r["params"] or "{}")) if r else None


def jobs(limit=60):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT j.*, (SELECT COUNT(*) FROM job_queries q WHERE q.job_id = j.id) AS n_queries "
            "FROM jobs j ORDER BY id DESC LIMIT ?", (limit,))]


def last_job(kind, status=None):
    q = "SELECT * FROM jobs WHERE kind=?" + (" AND status=?" if status else "") + " ORDER BY id DESC LIMIT 1"
    with conn() as c:
        r = c.execute(q, (kind, status) if status else (kind,)).fetchone()
        return dict(r) if r else None


# ── 主动通知去重 ──

def notify_claim(key):
    """占用一个通知 key：首次占用返回 True（调用方随后发送），已占用返回 False。先占后发 = 至多一次，重启不重发。"""
    with conn() as c:
        return c.execute("INSERT OR IGNORE INTO notify_log VALUES (?,?)", (key, time.time())).rowcount == 1


def notify_keys(prefix):
    with conn() as c:
        return {r["key"] for r in c.execute("SELECT key FROM notify_log WHERE key LIKE ?", (prefix + "%",))}
