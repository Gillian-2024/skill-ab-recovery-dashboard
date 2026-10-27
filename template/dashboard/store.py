"""SQLite 存储。每次调用开新连接，线程安全够用。"""
import json
import os
import sqlite3
import time

from . import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_daily (
  view TEXT, date INTEGER, payload TEXT, updated_at REAL, PRIMARY KEY (view, date));
CREATE TABLE IF NOT EXISTS day_status (
  view TEXT, date INTEGER, complete INTEGER, note TEXT, updated_at REAL, PRIMARY KEY (view, date));
CREATE TABLE IF NOT EXISTS breakdown_daily (
  view TEXT, date INTEGER, b INTEGER, tier TEXT, expose INTEGER, click INTEGER, PRIMARY KEY (view, date, b, tier));
CREATE TABLE IF NOT EXISTS dau_daily (
  view TEXT, date INTEGER, dau INTEGER, PRIMARY KEY (view, date));
CREATE TABLE IF NOT EXISTS metrics (
  key TEXT PRIMARY KEY, def TEXT, preset INTEGER, edited INTEGER DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS experiments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, scope TEXT, groups TEXT,
  start INTEGER, end INTEGER, launch INTEGER, metric_keys TEXT, daily_keys TEXT, subs TEXT, caps TEXT, status TEXT,
  pending TEXT, last_end INTEGER, error TEXT, created_at REAL, updated_at REAL, vis_error TEXT);
CREATE TABLE IF NOT EXISTS exp_results (
  exp_id INTEGER, metric_key TEXT, grp TEXT, payload TEXT, run_end INTEGER, run_at REAL,
  PRIMARY KEY (exp_id, metric_key, grp));
CREATE TABLE IF NOT EXISTS job_queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, label TEXT, task_id TEXT, sql TEXT,
  rows INTEGER, seconds REAL, error TEXT, at REAL);
CREATE INDEX IF NOT EXISTS job_queries_job ON job_queries(job_id);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, params TEXT, status TEXT,
  log TEXT DEFAULT '', error TEXT, created_at REAL, started_at REAL, finished_at REAL);
"""


def conn():
    os.makedirs(C.DATA_DIR, exist_ok=True)
    c = sqlite3.connect(C.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def presets():
    """预置指标 = dashboard.yaml 的 metrics 段。"""
    return [(m["key"], {k: v for k, v in m.items() if k != "key"}) for m in C.cfg["metrics"]]


def init():
    from .sqlgen import validate_metric
    with conn() as c:
        c.executescript(SCHEMA)
        # 旧库升级：辅助读数（分布 / CUPED / 分群）失败单独记，不挡结论提要
        if "vis_error" not in [r["name"] for r in c.execute("PRAGMA table_info(experiments)")]:
            c.execute("ALTER TABLE experiments ADD COLUMN vis_error TEXT")
        now = time.time()
        for key, d in presets():
            validate_metric(d)
            # 页面上改过的预置指标（edited=1）不被配置覆盖，可点「恢复预置」
            c.execute("INSERT INTO metrics(key, def, preset, created_at, edited) VALUES (?,?,1,?,0) "
                      "ON CONFLICT(key) DO UPDATE SET def = excluded.def, preset = 1 WHERE metrics.edited = 0",
                      (key, json.dumps(d, ensure_ascii=False), now))
        for r in c.execute("SELECT params FROM jobs WHERE kind='exp_followup' AND status='running'").fetchall():
            c.execute("UPDATE experiments SET pending='[]', error='补算被服务重启中断，可点重跑' WHERE id=?",
                      (json.loads(r["params"])["exp_id"],))
        c.execute("UPDATE jobs SET status='failed', error='服务重启中断', finished_at=? WHERE status='running'", (now,))


# ── 漏斗 ──

def save_funnel(view, flow, breakdown, dau, statuses):
    now = time.time()
    with conn() as c:
        for d, payload in flow.items():
            c.execute("INSERT OR REPLACE INTO flow_daily VALUES (?,?,?,?)", (view, int(d), json.dumps(payload), now))
        for p in breakdown:
            c.execute("INSERT OR REPLACE INTO breakdown_daily VALUES (?,?,?,?,?,?)",
                      (view, int(p["d"]), int(p["b"]), p["tier"], int(p["expose"] or 0), int(p["click"] or 0)))
        for d, v in dau.items():
            c.execute("INSERT OR REPLACE INTO dau_daily VALUES (?,?,?)", (view, int(d), int(v)))
        for d, (ok, note) in statuses.items():
            c.execute("INSERT OR REPLACE INTO day_status VALUES (?,?,?,?,?)", (view, int(d), int(ok), note, now))


def funnel(view, d0, d1):
    with conn() as c:
        flow = {r["date"]: json.loads(r["payload"]) for r in c.execute(
            "SELECT date, payload FROM flow_daily WHERE view=? AND date BETWEEN ? AND ?", (view, d0, d1))}
        bd = [dict(r) for r in c.execute(
            "SELECT b, tier, SUM(expose) AS expose, SUM(click) AS click FROM breakdown_daily "
            "WHERE view=? AND date BETWEEN ? AND ? GROUP BY b, tier ORDER BY b", (view, d0, d1))]
        dau = {r["date"]: r["dau"] for r in c.execute(
            "SELECT date, dau FROM dau_daily WHERE view=? AND date BETWEEN ? AND ?", (view, d0, d1))}
        st = {r["date"]: {"complete": bool(r["complete"]), "note": r["note"], "updated_at": r["updated_at"]}
              for r in c.execute("SELECT * FROM day_status WHERE view=? AND date BETWEEN ? AND ?", (view, d0, d1))}
    return flow, bd, dau, st


def funnel_views():
    with conn() as c:
        return [r["view"] for r in c.execute("SELECT DISTINCT view FROM day_status")]


def day_status(view):
    with conn() as c:
        return {r["date"]: bool(r["complete"]) for r in c.execute("SELECT date, complete FROM day_status WHERE view=?", (view,))}


def anchor_history(view, before, key, n=7):
    with conn() as c:
        out = []
        for r in c.execute("SELECT payload FROM flow_daily WHERE view=? AND date < ? ORDER BY date DESC LIMIT ?", (view, before, n)):
            out.append(json.loads(r["payload"]).get(key) or 0)
        return out


# ── 指标 ──

def metrics():
    with conn() as c:
        return [{"key": r["key"], "preset": bool(r["preset"]), "edited": bool(r["edited"]), **json.loads(r["def"])}
                for r in c.execute("SELECT * FROM metrics ORDER BY preset DESC, created_at")]


def metric_map():
    return {m["key"]: m for m in metrics()}


def save_metric(key, d):
    with conn() as c:
        if c.execute("SELECT 1 FROM metrics WHERE key=?", (key,)).fetchone():
            raise ValueError("key「%s」已存在；要改它请在左侧列表点「编辑」" % key)
        c.execute("INSERT INTO metrics(key, def, preset, created_at, edited) VALUES (?,?,0,?,0)",
                  (key, json.dumps(d, ensure_ascii=False), time.time()))


def update_metric(key, d, edited=True):
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

_JSON = ("groups", "metric_keys", "caps", "subs", "pending", "daily_keys")


def _exp(r):
    d = dict(r)
    for k in _JSON:
        d[k] = json.loads(d.get(k) or ("{}" if k == "caps" else "[]"))
    return d


def create_experiment(name, scope, groups, start, end, metric_keys, subs=(), daily_keys=(), launch=None):
    now = time.time()
    with conn() as c:
        return c.execute(
            "INSERT INTO experiments(name, scope, groups, start, end, launch, metric_keys, daily_keys, subs, caps, status, "
            "pending, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,'{}','queued','[]',?,?)",
            (name, scope, json.dumps(groups), start, end, launch, json.dumps(metric_keys), json.dumps(list(daily_keys)),
             json.dumps(list(subs)), now, now)).lastrowid


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
    for k in _JSON:
        if k in kw:
            kw[k] = json.dumps(kw[k])
    with conn() as c:
        c.execute("UPDATE experiments SET %s WHERE id=?" % ", ".join("%s=?" % k for k in kw), list(kw.values()) + [eid])


def save_exp_results(eid, metric_key, rows, run_end):
    now = time.time()
    with conn() as c:
        c.execute("DELETE FROM exp_results WHERE exp_id=? AND metric_key=?", (eid, metric_key))
        for grp, payload in rows.items():
            c.execute("INSERT INTO exp_results VALUES (?,?,?,?,?,?)", (eid, metric_key, grp, json.dumps(payload), run_end, now))


def delete_exp_results(eid, keys=None):
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
        p = json.dumps(params, sort_keys=True)
        dup = c.execute("SELECT id FROM jobs WHERE kind=? AND params=? AND status IN ('queued','running')", (kind, p)).fetchone()
        if dup:
            return dup["id"]
        return c.execute("INSERT INTO jobs(kind, params, status, created_at) VALUES (?,?,'queued',?)", (kind, p, time.time())).lastrowid


def next_job():
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
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
        c.execute("INSERT INTO job_queries(job_id, label, task_id, sql, rows, seconds, error, at) VALUES (?,?,?,?,?,?,?,?)",
                  (jid, q["label"], q.get("task_id"), q["sql"].strip(), q.get("rows"), q.get("seconds"), q.get("error"), time.time()))


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


def job(jid):
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return dict(r) if r else None
