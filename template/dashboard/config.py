"""读取并校验 dashboard.yaml。其余模块在调用时读 C.cfg，测试可用 C.use(dict) 换一份配置。"""
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
CONFIG_PATH = os.environ.get("DASH_CONFIG", os.path.join(ROOT, "dashboard.yaml"))

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")
_EVENT = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_KEY = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
DIALECTS = ("duckdb", "trino", "spark", "postgres", "mysql", "clickhouse")
SOURCES = ("duckdb", "sqlalchemy", "command")

cfg = {}
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "dashboard.db")


class ConfigError(ValueError):
    pass


def _need(cond, msg):
    if not cond:
        raise ConfigError("dashboard.yaml：" + msg)


def validate(c):
    """缺省值补齐 + 结构校验。where 片段由看板主人写，原样进 SQL；页面上用户输入的值另走白名单。"""
    c = dict(c or {})
    c.setdefault("title", "数据看板")
    srv = c.setdefault("server", {})
    srv.setdefault("host", "127.0.0.1")
    srv.setdefault("port", 5050)
    _need(srv["host"] in ("127.0.0.1", "localhost"), "server.host 只能是 127.0.0.1 / localhost（服务无鉴权）")
    sch = c.setdefault("schedule", {})
    sch.setdefault("daily_at", "09:40")
    sch.setdefault("backfill_days", 14)
    sch.setdefault("retry_gap_hours", 2)
    sch.setdefault("retry_until_hour", 20)
    _need(re.match(r"^\d{1,2}:\d{2}$", str(sch["daily_at"])), "schedule.daily_at 写成 HH:MM")

    s = c.get("source") or {}
    _need(s.get("type") in SOURCES, "source.type 只能是 %s" % " / ".join(SOURCES))
    s.setdefault("dialect", "duckdb" if s["type"] == "duckdb" else None)
    _need(s["dialect"] in DIALECTS, "source.dialect 只能是 %s" % " / ".join(DIALECTS))
    s.setdefault("max_rows", 5000)
    s.setdefault("timeout_s", 900)
    c["source"] = s

    t = c.get("table") or {}
    _need(_TABLE.match(str(t.get("name", ""))), "table.name 不合法")
    for k in ("device", "date", "event"):
        _need(_IDENT.match(str(t.get(k, ""))), "table.%s 要写列名" % k)
    t.setdefault("date_type", "int")
    _need(t["date_type"] in ("int", "date", "string"), "table.date_type 只能是 int / date / string")
    cols = t.setdefault("columns", {})
    for k in ("string", "number", "bool"):
        cols[k] = list(cols.get(k) or [])
        for x in cols[k]:
            _need(_IDENT.match(str(x)), "table.columns.%s 里的列名不合法：%r" % (k, x))
    c["table"] = t

    scopes = c.get("scopes") or [{"key": "ALL", "label": "全部", "where": "1 = 1"}]
    for x in scopes:
        _need(_KEY.match(str(x.get("key", ""))), "scopes[].key 只能用字母数字下划线")
        x.setdefault("label", x["key"])
        x.setdefault("where", "1 = 1")
    c["scopes"] = scopes
    sub = c.get("subscope")
    if sub:
        _need(_IDENT.match(str(sub.get("column", ""))), "subscope.column 要写列名")
        sub["values"] = [str(v) for v in sub.get("values") or []]
        _need(sub["values"] and all(_KEY.match(v) for v in sub["values"]), "subscope.values 要列出可选值（白名单）")
        sub.setdefault("labels", {})
        sub.setdefault("label", "细分")

    f = c.get("funnel") or {}
    steps = f.get("steps") or []
    _need(len(steps) >= 2, "funnel.steps 至少两步")
    extra = f.setdefault("extra_steps", [])
    seen = set()
    for x in steps + extra:
        _need(_IDENT.match(str(x.get("key", ""))) and x["key"] not in seen, "funnel 步骤 key 重复或不合法：%r" % x.get("key"))
        seen.add(x["key"])
        _need(_EVENT.match(str(x.get("event", ""))), "步骤 %s 的 event 不合法" % x["key"])
        x.setdefault("label", x["key"])
        x.setdefault("segment", "")
    tiers = f.get("tiers") or [{"key": "all", "label": "全量"}]
    for x in tiers:
        _need(_IDENT.match(str(x.get("key", ""))), "funnel.tiers[].key 不合法")
        x.setdefault("label", x["key"])
        x["skip"] = [k for k in x.get("skip") or []]
        _need(all(k in {s_["key"] for s_ in steps} for k in x["skip"]), "tier %s 的 skip 里有不存在的步骤" % x["key"])
    f["tiers"] = tiers
    keys = [x["key"] for x in steps]
    f["core"] = [k for k in f.get("core") or keys if k in keys]
    f.setdefault("anchor", f["core"][0] if f["core"] else keys[0])
    _need(f["anchor"] in keys, "funnel.anchor 要是 steps 里的一步")
    comp = f.setdefault("completeness", {})
    comp.setdefault("step", f["anchor"])
    comp.setdefault("min_ratio", 0.6)
    kpis = f.get("kpis") or [{"name": "%s ÷ %s" % (steps[-1]["label"], steps[0]["label"]), "num": keys[-1], "den": keys[0]}]
    for x in kpis:
        _need(x.get("num") in seen and (x.get("den") is None or x.get("den") in seen), "funnel.kpis 的 num / den 要是步骤 key（den 可留空 = 只看 UV）：%r" % x)
        x.setdefault("name", "%s ÷ %s" % (x["num"], x["den"]) if x.get("den") else x["num"])
        x["den"] = x.get("den")
        x["lower_is_better"] = bool(x.get("lower_is_better"))
        x.setdefault("note", "")
    f["kpis"] = kpis
    b = f.get("breakdown")
    if b:
        _need(_IDENT.match(str(b.get("column", ""))), "funnel.breakdown.column 要写数值列名")
        _need(b.get("expose_step") in seen and b.get("click_step") in seen, "funnel.breakdown 的 expose_step / click_step 要是步骤 key")
    a = f.get("dau")
    if a:
        _need(_TABLE.match(str(a.get("table", ""))), "funnel.dau.table 不合法")
        for k in ("date", "device", "subscope_column"):
            _need(a.get(k) is None or _IDENT.match(str(a[k])), "funnel.dau.%s 要写列名" % k)
        _need(isinstance(a.get("where_by_scope"), dict), "funnel.dau.where_by_scope 要写成 {范围 key: where 片段}")
    c["funnel"] = f

    e = c.setdefault("experiment", {})
    g = e.get("group") or {}
    _need(_IDENT.match(str(g.get("column", ""))), "experiment.group.column 要写分组列名")
    g.setdefault("match", "equals")
    _need(g["match"] in ("equals", "like"), "experiment.group.match 只能是 equals / like")
    if g["match"] == "like":
        _need("{id}" in str(g.get("pattern", "")), "experiment.group.pattern 里要有 {id}，如 '%%,{id},%%'")
    e["group"] = g
    cad = e.setdefault("cadence", {})
    for k, v in (("check", 2), ("stop_loss", 3), ("interim", 7), ("final", 14)):
        cad.setdefault(k, v)
    _need(0 < cad["check"] <= cad["stop_loss"] <= cad["interim"] <= cad["final"], "experiment.cadence 要满足 check ≤ stop_loss ≤ interim ≤ final")
    e.setdefault("cum_days", sorted({cad["stop_loss"], cad["interim"], cad["final"]}))
    _need(cad["stop_loss"] in e["cum_days"], "experiment.cum_days 要包含止损节点 %d（判止损读前 %d 天累计）" % (cad["stop_loss"], cad["stop_loss"]))
    e.setdefault("alpha", 0.05)
    e.setdefault("srm_p", 0.001)
    e.setdefault("cap_pre_days", 7)
    e.setdefault("identify_events", [])
    e.setdefault("extra_events", [])
    for ev in e["identify_events"] + e["extra_events"]:
        _need(_EVENT.match(str(ev)), "experiment 事件名不合法：%r" % ev)
    c["metrics"] = list(c.get("metrics") or [])
    return c


def use(c):
    """切换当前配置（服务启动 / 测试）。"""
    global cfg, DATA_DIR, DB_PATH
    cfg = validate(c)
    DATA_DIR = os.path.abspath(os.path.join(ROOT, cfg.get("data_dir", "data")))
    DB_PATH = os.path.join(DATA_DIR, "dashboard.db")
    return cfg


def load(path=None):
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        return use(yaml.safe_load(fh))


def path_of(p):
    """配置里的相对路径按项目根目录解析，支持 ~。"""
    p = os.path.expanduser(str(p))
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def daily_at():
    h, m = str(cfg["schedule"]["daily_at"]).split(":")
    return int(h), int(m)


def all_steps():
    return cfg["funnel"]["steps"] + cfg["funnel"]["extra_steps"]


def events():
    """指标库可选事件 = 漏斗各步事件 + 补充事件（去重保序）。"""
    out = []
    for ev in [s["event"] for s in all_steps()] + cfg["experiment"]["identify_events"] + cfg["experiment"]["extra_events"]:
        if ev not in out:
            out.append(ev)
    return out


if os.path.exists(CONFIG_PATH):
    try:
        load()
    except ConfigError:
        pass   # 导入时不抛；启动入口 / 校验脚本会再 load() 一次并给出错误
