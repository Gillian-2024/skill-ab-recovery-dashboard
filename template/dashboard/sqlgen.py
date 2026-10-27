"""SQL 生成：日常漏斗（一次扫描出包含式 + 独立去重两套 UV）+ 实验指标（设备级充分统计量）。

约束：
- 只输出聚合，不输出设备明细（结果集小，也不外泄设备键）；
- 不用 UNION（部分数仓的血缘解析对 UNION 分支很挑剔）；
- 页面上用户可输入的值（指标筛选值、分组号、细分值）一律白名单 + 正则校验后才拼进 SQL；
  dashboard.yaml 里的 where 片段由看板主人编写，按受信任配置原样拼接。
"""
import datetime as dt
import re

from . import config as C

RESERVED = {"from", "select", "where", "order", "group", "by", "user", "date", "time", "timestamp", "event", "position",
            "value", "values", "key", "type", "source", "level", "count", "range", "desc", "asc", "limit", "table",
            "column", "index", "page", "status", "name", "default", "interval", "day", "month", "year", "hour"}


class MetricError(ValueError):
    pass


def _cfg():
    return C.cfg


def q(col):
    """列名：配置里已校验过只含字母数字下划线；撞关键字的按方言加引号。"""
    col = str(col)
    if not C._IDENT.match(col):
        raise MetricError("列名不合法：%r" % col)
    if col.lower() not in RESERVED:
        return col
    return ("`%s`" if _cfg()["source"]["dialect"] in ("mysql", "spark", "clickhouse") else '"%s"') % col


def T():
    return _cfg()["table"]["name"]


def DEV():
    return q(_cfg()["table"]["device"])


def DT():
    return q(_cfg()["table"]["date"])


def EV():
    return q(_cfg()["table"]["event"])


def _sq(s):
    return str(s).replace("'", "''")


def dlit(d, date_type=None):
    """YYYYMMDD 整数 → 该表日期列的字面量。"""
    d = int(d)
    iso = "%04d-%02d-%02d" % (d // 10000, d // 100 % 100, d % 100)
    t = date_type or _cfg()["table"]["date_type"]
    if t == "int":
        return str(d)
    if t == "date":
        return "CAST('%s' AS DATE)" % iso
    fmt = _cfg()["table"].get("date_format", "%Y-%m-%d")
    return "'%s'" % dt.date(d // 10000, d // 100 % 100, d % 100).strftime(fmt)


def to_ymd(v):
    """数据库返回的日期（int / 'YYYY-MM-DD' / 'YYYYMMDD' / date）→ YYYYMMDD 整数。"""
    if isinstance(v, (dt.date, dt.datetime)):
        return int(v.strftime("%Y%m%d"))
    s = re.sub(r"\D", "", str(v))[:8]
    return int(s)


def pct_fn(expr, p):
    d = _cfg()["source"]["dialect"]
    p = float(p)
    if d == "duckdb":
        return "quantile_cont(%s, %s)" % (expr, p)
    if d == "trino":
        return "approx_percentile(%s, %s)" % (expr, p)
    if d == "spark":
        return "percentile_approx(%s, %s)" % (expr, p)
    if d == "postgres":
        return "percentile_cont(%s) WITHIN GROUP (ORDER BY %s)" % (p, expr)
    if d == "clickhouse":
        return "quantile(%s)(%s)" % (p, expr)
    raise MetricError("%s 没有分位数函数：分位数指标与 P99 封顶在该方言下不可用" % d)


# ─────────────────────────── 范围 / 细分 ───────────────────────────

def scope_of(key):
    for s in _cfg()["scopes"]:
        if s["key"] == key:
            return s
    raise MetricError("未知范围：%s" % key)


def split_view(view):
    """漏斗视图键：'APP' = 整个范围，'APP:US' = 范围下的单个细分值。返回 (scope_key, [细分值] 或 None)。"""
    k, _, sub = str(view).partition(":")
    scope_of(k)
    if sub:
        check_subs([sub])
        return k, [sub]
    return k, None


def check_subs(subs):
    sc = _cfg().get("subscope")
    if not subs:
        return []
    if not sc:
        raise MetricError("dashboard.yaml 没有配置 subscope，不能按细分筛选")
    bad = [s for s in subs if s not in sc["values"]]
    if bad:
        raise MetricError("不在 subscope.values 白名单里：%s" % ",".join(map(str, bad)))
    return list(subs)


def where_scope(key, subs=None):
    s = "(%s)" % scope_of(key)["where"]
    subs = check_subs(subs)
    if subs:
        s += " AND %s IN (%s)" % (q(_cfg()["subscope"]["column"]), ", ".join("'%s'" % v for v in subs))
    return s


def where_dates(d0, d1):
    return "%s BETWEEN %s AND %s" % (DT(), dlit(d0), dlit(d1))


# ─────────────────────────── 日常漏斗 ───────────────────────────

def _tiers():
    f = _cfg()["funnel"]
    base = [t for t in f["tiers"] if not t.get("union")]
    union = [t for t in f["tiers"] if t.get("union")]
    if not base:
        base = [{"key": "all", "label": "全量", "where": "1 = 1", "skip": []}]
    return base, union


def _step_cond(s, tier):
    parts = ["%s = '%s'" % (EV(), _sq(s["event"]))]
    if s.get("where"):
        parts.append("(%s)" % s["where"])
    if not s.get("shared"):
        tw = (s.get("tier_where") or {}).get(tier["key"], tier.get("where"))
        if tw:
            parts.append("(%s)" % tw)
    return " AND ".join(parts)


def flow_cols():
    """flow_sql 返回的列名（不含 d）。"""
    f = _cfg()["funnel"]
    out = []
    for t in f["tiers"] if f["tiers"] else _tiers()[0]:
        for s in C.all_steps():
            out += ["uv__%s__%s" % (s["key"], t["key"]), "pv__%s__%s" % (s["key"], t["key"])]
        for s in f["steps"]:
            out.append("in__%s__%s" % (s["key"], t["key"]))
    return out


def flow_sql(scope, d0, d1, subs=None):
    """单位 = 设备-天。
    uv__步骤__档 = 当天有过该步的设备数（独立去重）；pv = 事件条数；
    in__步骤__档 = 包含式到达：当天走过本步及之前所有步（档位 skip 的步视为通过），每步转化 ≤ 100%。
    union 档 = 任一成员档的路径到达（greatest，不是相加）；PV 为成员相加（成员档的 where 应互斥）。"""
    f = _cfg()["funnel"]
    base, union = _tiers()
    marks, cols = [], []
    for t in base:
        for s in C.all_steps():
            c = _step_cond(s, t)
            marks.append("MAX(CASE WHEN %s THEN 1 ELSE 0 END) AS m__%s__%s" % (c, s["key"], t["key"]))
            marks.append("SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS n__%s__%s" % (c, s["key"], t["key"]))

    def chain(tk, upto):
        skip = set(next(t for t in base if t["key"] == tk).get("skip") or [])
        acc = []
        for s in f["steps"]:
            if s["key"] not in skip:
                acc.append("m__%s__%s" % (s["key"], tk))
            if s["key"] == upto:
                break
        return " * ".join(acc) or "1"

    for t in base:
        for s in C.all_steps():
            cols.append("SUM(m__{s}__{t}) AS uv__{s}__{t}, SUM(n__{s}__{t}) AS pv__{s}__{t}".format(s=s["key"], t=t["key"]))
        for s in f["steps"]:
            cols.append("SUM(%s) AS in__%s__%s" % (chain(t["key"], s["key"]), s["key"], t["key"]))
    for u in union:
        mem = [m for m in u["union"] if any(b["key"] == m for b in base)]
        if not mem:
            raise MetricError("tier %s 的 union 成员不存在" % u["key"])
        for s in C.all_steps():
            if s.get("shared"):
                cols.append("SUM(m__{s}__{m}) AS uv__{s}__{u}, SUM(n__{s}__{m}) AS pv__{s}__{u}".format(s=s["key"], m=mem[0], u=u["key"]))
            else:
                g = "greatest(%s)" % ", ".join("m__%s__%s" % (s["key"], m) for m in mem) if len(mem) > 1 else "m__%s__%s" % (s["key"], mem[0])
                cols.append("SUM(%s) AS uv__%s__%s, SUM(%s) AS pv__%s__%s" % (
                    g, s["key"], u["key"], " + ".join("n__%s__%s" % (s["key"], m) for m in mem), s["key"], u["key"]))
        for s in f["steps"]:
            chains = [chain(m, s["key"]) for m in mem]
            g = "greatest(%s)" % ", ".join("(%s)" % c for c in chains) if len(chains) > 1 else chains[0]
            cols.append("SUM(%s) AS in__%s__%s" % (g, s["key"], u["key"]))
    evs = sorted({s["event"] for s in C.all_steps()})
    return f"""
SELECT d, {(","+chr(10)+"  ").join(cols)}
FROM (
  SELECT {DT()} AS d, {DEV()} AS u,
    {(","+chr(10)+"    ").join(marks)}
  FROM {T()}
  WHERE {where_dates(d0, d1)} AND {where_scope(scope, subs)}
    AND {EV()} IN ({", ".join("'%s'" % _sq(e) for e in evs)})
  GROUP BY {DT()}, {DEV()}
) x
GROUP BY d"""


def breakdown_sql(scope, d0, d1, subs=None):
    """可选：按某个数值列分桶看「曝光步 → 点击步」的 PV 转化（如位置 CTR）。"""
    b = _cfg()["funnel"].get("breakdown")
    if not b:
        return None
    steps = {s["key"]: s for s in C.all_steps()}
    ex, ck = steps[b["expose_step"]], steps[b["click_step"]]
    base, _ = _tiers()
    col = q(b["column"])
    cols = []
    for t in base:
        cols.append("SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS e__%s" % (_step_cond(ex, t), t["key"]))
        cols.append("SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS c__%s" % (_step_cond(ck, t), t["key"]))
    return f"""
SELECT {DT()} AS d, {col} AS b, {", ".join(cols)}
FROM {T()}
WHERE {where_dates(d0, d1)} AND {where_scope(scope, subs)}
  AND {EV()} IN ('{_sq(ex["event"])}', '{_sq(ck["event"])}') AND {col} BETWEEN 1 AND {int(b.get("max", 20))}
GROUP BY {DT()}, {col}"""


def dau_sql(scope, d0, d1, subs=None):
    """可选：活跃设备数（算渗透率）。dau.where_by_scope 没配的范围不出；选了细分但没配 subscope_column 也不出。"""
    a = _cfg()["funnel"].get("dau")
    if not a:
        return None
    w = (a.get("where_by_scope") or {}).get(scope)
    if w is None:
        return None
    if subs:
        if not a.get("subscope_column"):
            return None
        w = "(%s) AND %s IN (%s)" % (w, q(a["subscope_column"]), ", ".join("'%s'" % v for v in check_subs(subs)))
    dtype = a.get("date_type") or _cfg()["table"]["date_type"]
    dcol = q(a.get("date") or _cfg()["table"]["date"])
    return f"""
SELECT {dcol} AS d, COUNT(DISTINCT {q(a.get("device") or _cfg()["table"]["device"])}) AS dau
FROM {a["table"]}
WHERE {dcol} BETWEEN {dlit(d0, dtype)} AND {dlit(d1, dtype)} AND ({w})
GROUP BY {dcol}"""


# ─────────────────────────── 指标定义校验 ───────────────────────────

OPS = {"=", "!=", "in", "not_in", ">", ">=", "<", "<=", "like", "is_null", "not_null"}
_STR_VAL = re.compile(r"^[A-Za-z0-9_.:\-%]{1,64}$")
_NUM_VAL = re.compile(r"^-?\d+(\.\d+)?$")
_COL_EXPR = re.compile(r"^(\d+(?:\.\d+)?)\s*\*\s*([A-Za-z_][A-Za-z0-9_]*)$")
_GID = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


def str_cols():
    return set(_cfg()["table"]["columns"]["string"])


def num_cols():
    return set(_cfg()["table"]["columns"]["number"])


def bool_cols():
    return set(_cfg()["table"]["columns"]["bool"])


def _lit_str(v):
    v = str(v)
    if not _STR_VAL.match(v):
        raise MetricError("取值只允许字母数字及 _ . : - %%：%r" % v)
    return "'%s'" % v


def _lit_num(v):
    v = str(v).strip()
    if _NUM_VAL.match(v):
        return v
    m = _COL_EXPR.match(v)
    if m and m.group(2) in num_cols():
        return "%s * %s" % (m.group(1), q(m.group(2)))
    raise MetricError("数值条件只允许数字或「系数*数值列」：%r" % v)


def filter_sql(f):
    if isinstance(f, (list, tuple)) and len(f) == 2 and f[0] == "any":
        if not isinstance(f[1], (list, tuple)) or not f[1]:
            raise MetricError("any 条件应为 [\"any\", [条件1, 条件2, ...]]")
        return "(%s)" % " OR ".join(filter_sql(x) for x in f[1])
    if not isinstance(f, (list, tuple)) or len(f) not in (2, 3):
        raise MetricError("筛选条件格式应为 [列, 运算符, 值]：%r" % (f,))
    col, op = f[0], f[1]
    val = f[2] if len(f) == 3 else None
    if op not in OPS:
        raise MetricError("不支持的运算符：%r" % op)
    if col in str_cols():
        c = q(col)
        if op in ("is_null", "not_null"):
            return "%s %s" % (c, "IS NULL" if op == "is_null" else "IS NOT NULL")
        if op in ("in", "not_in"):
            vals = val if isinstance(val, list) else [x.strip() for x in str(val).split(",") if x.strip()]
            if not vals:
                raise MetricError("%s 的 in 列表为空" % col)
            return "%s %s (%s)" % (c, "IN" if op == "in" else "NOT IN", ", ".join(_lit_str(x) for x in vals))
        if op == "like":
            return "%s LIKE %s" % (c, _lit_str(val))
        if op in ("=", "!="):
            return "%s %s %s" % (c, "=" if op == "=" else "<>", _lit_str(val))
        raise MetricError("字符串列 %s 不支持 %s" % (col, op))
    if col in num_cols():
        c = q(col)
        if op in ("is_null", "not_null"):
            return "%s %s" % (c, "IS NULL" if op == "is_null" else "IS NOT NULL")
        if op in ("in", "not_in"):
            vals = val if isinstance(val, list) else [x.strip() for x in str(val).split(",") if x.strip()]
            return "%s %s (%s)" % (c, "IN" if op == "in" else "NOT IN", ", ".join(_lit_num(x) for x in vals))
        if op == "like":
            raise MetricError("数值列 %s 不支持 like" % col)
        return "%s %s %s" % (c, "<>" if op == "!=" else op, _lit_num(val))
    if col in bool_cols():
        if op not in ("=", "!="):
            raise MetricError("布尔列 %s 只支持 = / !=" % col)
        truth = str(val).lower() in ("1", "true", "yes")
        if op == "!=":
            truth = not truth
        return q(col) if truth else "NOT %s" % q(col)
    raise MetricError("列不在白名单（table.columns）：%r" % col)


def side_cond(side):
    evs = side.get("event")
    evs = evs if isinstance(evs, list) else [evs]
    allowed = set(C.events())
    if not evs or any(e not in allowed for e in evs):
        raise MetricError("事件不在白名单（漏斗步骤 + experiment.extra_events）：%r" % evs)
    parts = ["%s IN (%s)" % (EV(), ", ".join("'%s'" % _sq(e) for e in evs))]
    parts += [filter_sql(f) for f in side.get("filters") or []]
    return " AND ".join(parts)


def side_expr(side):
    """设备级取值表达式。"""
    cond = side_cond(side)
    agg = side.get("agg", "uv")
    if agg == "uv":
        return "MAX(CASE WHEN %s THEN 1 ELSE 0 END)" % cond
    if agg == "pv":
        return "SUM(CASE WHEN %s THEN 1 ELSE 0 END)" % cond
    if agg.startswith("sum:") and agg[4:] in num_cols():
        return "SUM(CASE WHEN %s THEN %s ELSE 0 END)" % (cond, q(agg[4:]))
    raise MetricError("不支持的计数方式：%r（uv / pv / sum:<数值列>）" % agg)


ROLES = ("north_star", "process", "guardrail")


def validate_metric(d):
    kind = d.get("kind", "ratio")
    if not str(d.get("name", "")).strip():
        raise MetricError("指标名不能为空")
    if d.get("role", "process") not in ROLES:
        raise MetricError("role 只能是 north_star / process / guardrail")
    if kind == "ratio":
        side_expr(d["numerator"])
        side_expr(d["denominator"])
        if d.get("device_scope"):
            side_cond(d["device_scope"])
        if d.get("cap") not in (None, "", "p99_pre"):
            raise MetricError("cap 只支持 p99_pre")
    elif kind == "quantile":
        side_cond(d["source"])
        if d.get("column") not in num_cols():
            raise MetricError("分位数列不在白名单")
        qs = d.get("quantiles") or []
        if not qs or any(not (0 < float(x) < 1) for x in qs):
            raise MetricError("分位点须在 (0,1)")
    else:
        raise MetricError("未知指标类型：%r" % kind)
    return d


# ─────────────────────────── 实验 ───────────────────────────

def check_gid(gid):
    if not _GID.match(str(gid)):
        raise MetricError("分组号只允许字母数字及 _ . : -：%r" % gid)
    return str(gid)


def _grp_flag(gid):
    g = _cfg()["experiment"]["group"]
    gid = check_gid(gid)
    col = q(g["column"])
    if g["match"] == "equals":
        return "CASE WHEN %s = '%s' THEN 1 ELSE 0 END" % (col, gid)
    return "CASE WHEN %s LIKE '%s' THEN 1 ELSE 0 END" % (col, _sq(g["pattern"].replace("{id}", gid)))


def _ratio_cols(metrics):
    """设备-天层的取值列：x_k / y_k（side_expr），可选 s_k（device_scope）。"""
    conds, cols = set(), []
    for m in metrics:
        k, d = m["key"], m["def"]
        conds.add(side_cond(d["numerator"]))
        conds.add(side_cond(d["denominator"]))
        cols.append("%s AS x_%s" % (side_expr(d["numerator"]), k))
        cols.append("%s AS y_%s" % (side_expr(d["denominator"]), k))
        if d.get("device_scope"):
            conds.add(side_cond(d["device_scope"]))
            cols.append("MAX(CASE WHEN %s THEN 1 ELSE 0 END) AS s_%s" % (side_cond(d["device_scope"]), k))
    return conds, cols


def _cap_of(m, caps):
    if m["def"].get("cap") != "p99_pre":
        return None
    cap = caps.get(m["key"])
    if cap is None:
        raise MetricError("指标 %s 缺少上线前 P99 封顶值" % m["name"])
    return float(cap)


def _day_x(m, caps):
    """设备-天的分子：封顶指标按「单设备单天 least(值, P99)」；numerator_within_denominator 时只算当天有分母的设备-天。"""
    k, cap = m["key"], _cap_of(m, caps)
    x = "x_%s" % k if cap is None else "least(x_%s, %s)" % (k, cap)
    return "CASE WHEN y_%s > 0 THEN %s ELSE 0 END" % (k, x) if m["def"].get("numerator_within_denominator") else x


def _stats_cols(metrics):
    return ["SUM(xx_{k}) AS sx_{k}, SUM(yy_{k}) AS sy_{k}, SUM(xx_{k} * xx_{k}) AS sxx_{k}, "
            "SUM(yy_{k} * yy_{k}) AS syy_{k}, SUM(xx_{k} * yy_{k}) AS sxy_{k}".format(k=m["key"]) for m in metrics]


def _adj(metrics, caps, xsrc="x"):
    """xsrc="xd"：By天直接用设备-天层已封顶的 xd_k。"""
    out = []
    for m in metrics:
        k, d = m["key"], m["def"]
        x, y = "%s_%s" % (xsrc, k), "y_%s" % k
        if d.get("numerator_within_denominator") and _cap_of(m, caps) is None and xsrc == "x":
            x = "CASE WHEN %s > 0 THEN %s ELSE 0 END" % (y, x)
        if d.get("device_scope"):
            x, y = "%s * s_%s" % (x, k), "%s * s_%s" % (y, k)
        out.append("%s AS xx_%s, %s AS yy_%s" % (x, k, y, k))
    return out


def _grp_case(gids, prefix):
    hits = " + ".join("%s%d" % (prefix, i) for i in range(len(gids)))
    case = " ".join("WHEN %s%d = 1 THEN '%s'" % (prefix, i, g) for i, g in enumerate(gids))
    return "CASE WHEN %s > 1 THEN 'X' %s END" % (hits, case), hits


def _ev_cols():
    t = _cfg()["table"]
    cols = [t["device"], t["date"], t["event"]] + t["columns"]["string"] + t["columns"]["number"] + t["columns"]["bool"]
    seen, out = set(), []
    for c in cols:
        if c not in seen:
            seen.add(c)
            out.append(q(c))
    return ", ".join(out)


def _dd_sql(scope, gids, d0, d1, metrics, caps, subs):
    """设备-天层：只保留当天事件带组号的设备-天；f_i = 当天命中第 i 组。"""
    gflags = ", ".join("%s AS g%d" % (_grp_flag(g), i) for i, g in enumerate(gids))
    fday = ", ".join("MAX(g%d) AS f%d" % (i, i) for i in range(len(gids)))
    conds, cols = _ratio_cols(metrics)
    ide = _cfg()["experiment"]["identify_events"]
    if ide:
        conds.add("%s IN (%s)" % (EV(), ", ".join("'%s'" % _sq(e) for e in ide)))
    caps_cols = ", ".join("%s AS xd_%s" % (_day_x(m, caps), m["key"]) for m in metrics)
    _, hits = _grp_case(gids, "f")
    return f"""
      SELECT d.*, {caps_cols}
      FROM (
        SELECT {DEV()} AS u, {DT()} AS dd, {fday}, {", ".join(cols)}
        FROM (
          SELECT {_ev_cols()}, {gflags}
          FROM {T()}
          WHERE {where_dates(d0, d1)} AND {where_scope(scope, subs)}
            AND ({" OR ".join("(%s)" % c for c in sorted(conds))})
        ) ev
        GROUP BY {DEV()}, {DT()}
      ) d
      WHERE {hits} > 0"""


def _roll(metrics, caps):
    """设备-天 → 设备（窗口内并集）的取值列。"""
    roll = []
    for m in metrics:
        k = m["key"]
        uv_num = _cap_of(m, caps) is None and m["def"]["numerator"].get("agg", "uv") == "uv"
        den = "MAX" if m["def"]["denominator"].get("agg", "uv") == "uv" else "SUM"
        if uv_num and m["def"].get("numerator_within_denominator"):
            roll.append("MAX(x_%s) * MAX(CASE WHEN y_%s > 0 THEN 1 ELSE 0 END) AS x_%s, %s(y_%s) AS y_%s" % (k, k, k, den, k, k))
        else:
            roll.append("%s(xd_%s) AS x_%s, %s(y_%s) AS y_%s" % ("MAX" if uv_num else "SUM", k, k, den, k, k))
        if m["def"].get("device_scope"):
            roll.append("MAX(s_%s) AS s_%s" % (k, k))
    return roll


def _dev_sql(scope, gids, d0, d1, metrics, caps, subs, first=False, keep_id=False):
    """设备层：每设备一行 grp + xx_k / yy_k；全窗口比率与分布共用同一并集口径。first = 带上首次带组号的日期；
    keep_id = 带上设备列 u（CUPED / 分群要按设备 JOIN，外层仍只出聚合）。"""
    dd = _dd_sql(scope, gids, d0, d1, metrics, caps, subs)
    grp, _ = _grp_case(gids, "a")
    aflags = ", ".join("MAX(f%d) AS a%d" % (i, i) for i in range(len(gids)))
    return f"""
  SELECT {"u, " if keep_id else ""}grp, {"first_date, " if first else ""}{", ".join(_adj(metrics, caps))}
  FROM (
    SELECT {grp} AS grp, v.*
    FROM (
      SELECT u, {"MIN(dd) AS first_date, " if first else ""}{aflags}, {", ".join(_roll(metrics, caps))}
      FROM ({dd}
      ) dd
      GROUP BY u
    ) v
  ) g
  WHERE grp IS NOT NULL"""


def experiment_ratio_sql(scope, gids, d0, d1, metrics, caps, subs=None):
    """全窗口 / 前 N 天累计：设备在窗口内（只看带组号的设备-天）取并集；同一设备跨组 → X 剔除。每组一行充分统计量。
    UV 分子 + 分子只算分母设备：窗口内有过分子 × 窗口内有过分母（不要求同一天）。"""
    return f"""
SELECT grp, COUNT(*) AS n, {", ".join(_stats_cols(metrics))}
FROM ({_dev_sql(scope, gids, d0, d1, metrics, caps, subs)}
) a
GROUP BY grp"""


CUPED_V = "xyab"   # x / y = 实验期分子 / 分母；a / b = 上线前同口径分子 / 分母


def cuped_metrics(metrics):
    """能做 CUPED 的比率指标：不带 device_scope（上线前没有对应的设备范围定义）。"""
    return [m for m in metrics if not m["def"].get("device_scope")]


def _pre_dev_sql(scope, p0, p1, metrics, caps, subs):
    """上线前（p0~p1）每设备一行 px_k / py_k：口径同实验期（同一 P99 封顶、窗口内并集、分子只算分母设备），不认组。"""
    conds, cols = _ratio_cols(metrics)
    xd = ", ".join("%s AS xd_%s" % (_day_x(m, caps), m["key"]) for m in metrics)
    roll = [c.replace(" AS x_", " AS px_").replace(" AS y_", " AS py_") for c in _roll(metrics, caps)]
    return f"""
      SELECT pu, {", ".join(roll)}
      FROM (
        SELECT d.*, {xd}
        FROM (
          SELECT {DEV()} AS pu, {DT()} AS dd, {", ".join(cols)}
          FROM (
            SELECT {_ev_cols()}
            FROM {T()}
            WHERE {where_dates(p0, p1)} AND {where_scope(scope, subs)}
              AND ({" OR ".join("(%s)" % c for c in sorted(conds))})
          ) ev
          GROUP BY {DEV()}, {DT()}
        ) d
      ) pd
      GROUP BY pu"""


def experiment_cuped_sql(scope, gids, d0, d1, metrics, caps, subs, p0, p1):
    """CUPED 原料：实验期设备层（同全窗口比率）LEFT JOIN 上线前设备层，每组一行：n + 四个变量的和 + 两两乘积和。
    上线前没有行为的设备 a = b = 0（照样计入：协变量是上线前就定了的，不受实验影响）。"""
    ms = cuped_metrics(metrics)
    agg = ["COUNT(*) AS n"]
    for m in ms:
        k = m["key"]
        v = {"x": "xx_%s" % k, "y": "yy_%s" % k, "a": "COALESCE(px_%s, 0)" % k, "b": "COALESCE(py_%s, 0)" % k}
        for i, p in enumerate(CUPED_V):
            agg.append("SUM(%s) AS s%s_%s" % (v[p], p, k))
            for q_ in CUPED_V[i:]:
                agg.append("SUM(%s * %s) AS s%s%s_%s" % (v[p], v[q_], p, q_, k))
    return f"""
SELECT grp, {", ".join(agg)}
FROM ({_dev_sql(scope, gids, d0, d1, ms, caps, subs, keep_id=True)}
) p
LEFT JOIN ({_pre_dev_sql(scope, p0, p1, ms, caps, subs)}
) r ON p.u = r.pu
GROUP BY grp"""


SEG_NA, SEG_OTHER = "@na", "@other"   # 分群取值里的「未知」「其他」：带 @ 不会和真实取值撞


def _seg_dev_sql(scope, d0, d1, subs, col):
    """每设备一个分群取值 = 窗口内认组事件上该列的最大值（列值多数设备在窗口内不变；变了取其一）。不要求事件带组号：组号已由设备层认定。"""
    ide = _cfg()["experiment"]["identify_events"]
    ev = (" AND %s IN (%s)" % (EV(), ", ".join("'%s'" % _sq(e) for e in ide))) if ide else ""
    return f"""
      SELECT {DEV()} AS su, 1 AS hit, MAX({q(col)}) AS sv
      FROM {T()}
      WHERE {where_dates(d0, d1)} AND {where_scope(scope, subs)}{ev}
      GROUP BY {DEV()}"""


def experiment_seg_sql(scope, gids, d0, d1, metrics, caps, subs, seg):
    """分群充分统计量：每组 × 分群取值一行（只出聚合）。seg = experiment.segments 的一项；
    取值只单列设备数最多的前 top 个，其余并成「其他」，免得行数随取值个数膨胀。"""
    col = seg["column"]
    if col not in str_cols():
        raise MetricError("分群列不在 table.columns.string 白名单：%s" % col)
    sd = _seg_dev_sql(scope, d0, d1, subs, col)
    vals = ", ".join("p.xx_{k}, p.yy_{k}".format(k=m["key"]) for m in metrics)
    # 「JOIN 不上」用标记列 hit 判断，输出列只引用聚合后的取值，不引用设备列
    key = ("CASE WHEN s.hit IS NULL OR s.sv IS NULL THEN '%s' WHEN tr.top_v IS NULL THEN '%s' ELSE s.sv END AS d_v"
           % (SEG_NA, SEG_OTHER))
    return f"""
SELECT grp, d_v, COUNT(*) AS n, {", ".join(_stats_cols(metrics))}
FROM (
  SELECT p.grp, {vals}, {key}
  FROM ({_dev_sql(scope, gids, d0, d1, metrics, caps, subs, keep_id=True)}
  ) p
  LEFT JOIN ({sd}
  ) s ON p.u = s.su
  LEFT JOIN (
    SELECT sv AS top_v, DENSE_RANK() OVER (ORDER BY cnt DESC, sv) AS rk
    FROM (SELECT sv, COUNT(*) AS cnt FROM ({sd}
    ) s0 WHERE sv IS NOT NULL GROUP BY sv) rc
  ) tr ON s.sv = tr.top_v AND tr.rk <= {int(seg["top"])}
) a
GROUP BY grp, d_v"""


DIST_Q = (0.05, 0.25, 0.5, 0.75, 0.95)


def experiment_dist_sql(scope, gids, d0, d1, metrics, caps, subs, dist_keys, days):
    """分布 + 进组趋势，一次扫描，每组一行（只出聚合）。
    metrics = 实验全部比率指标（设备集合与全窗口比率一致）；
    分布：dist_keys 里的指标，每设备取值 = 分子 ÷ 分母（同一并集、同一 P99 封顶），只看分母 > 0 的设备，出 P5/P25/P50/P75/P95 + 设备均值；
    进组：e_<日期> = 窗口内首次带组号是这天的设备数（跨组 → X 剔除），累计到窗口末 = 分流检查的各组设备数。"""
    try:
        pct_fn("v", 0.5)
        has_q = True
    except MetricError:     # 没有分位数函数的方言（mysql）：只出均值和进组
        has_q = False
    cols = []
    for k in dist_keys:
        v = "CASE WHEN yy_%s > 0 THEN (xx_%s * 1.0) / yy_%s END" % (k, k, k)
        if has_q:
            cols += ["%s AS q%d_%s" % (pct_fn(v, q), i, k) for i, q in enumerate(DIST_Q)]
        cols.append("AVG(%s) AS avg_%s, SUM(CASE WHEN yy_%s > 0 THEN 1 ELSE 0 END) AS n_%s" % (v, k, k, k))
    cols += ["SUM(CASE WHEN first_date = %s THEN 1 ELSE 0 END) AS e_%d" % (dlit(d), int(d)) for d in days]
    return f"""
SELECT grp, COUNT(*) AS n, {", ".join(cols)}
FROM ({_dev_sql(scope, gids, d0, d1, metrics, caps, subs, first=True)}
) a
GROUP BY grp"""


def experiment_daily_ratio_sql(scope, gids, d0, d1, metrics, caps, subs=None):
    """By天：单位 = 设备-天，组号按当天认（当天跨组 → X 剔除），封顶与全窗口同一个单设备单天 P99。"""
    dd = _dd_sql(scope, gids, d0, d1, metrics, caps, subs)
    grp, _ = _grp_case(gids, "f")
    return f"""
SELECT grp, d, COUNT(*) AS n, {", ".join(_stats_cols(metrics))}
FROM (
  SELECT grp, dd AS d, {", ".join(_adj(metrics, caps, "xd"))}
  FROM (
    SELECT {grp} AS grp, dd.*
    FROM ({dd}
    ) dd
  ) g
  WHERE grp IS NOT NULL
) a
GROUP BY grp, d"""


def cap_sql(scope, d0, d1, numerator, denominator=None, subs=None):
    """上线前单设备单天取值的 P99：只看当天有分母事件的设备-天，未发生记 0。"""
    if denominator is None:
        cond, where, dcol = side_cond(numerator), "v > 0", ""
    else:
        cond = "(%s) OR (%s)" % (side_cond(numerator), side_cond(denominator))
        dcol, where = ", %s AS de" % side_expr(denominator), "de > 0"
    return f"""
SELECT {pct_fn("v", 0.99)} AS cap, COUNT(*) AS n_dev
FROM (
  SELECT {DEV()} AS u, {DT()} AS dd, {side_expr(numerator)} AS v{dcol}
  FROM {T()}
  WHERE {where_dates(d0, d1)} AND {where_scope(scope, subs)} AND ({cond})
  GROUP BY {DEV()}, {DT()}
) d
WHERE {where}"""


def experiment_quantile_sql(scope, gids, d0, d1, metric, subs=None, daily=False):
    """分位数：每条事件按自身分组列认组；同一事件带多个组号的剔除。"""
    d = metric["def"]
    if d["column"] not in num_cols():
        raise MetricError("分位数列不在白名单")
    qcols = ", ".join("%s AS q%d" % (pct_fn("v", qq), i) for i, qq in enumerate(d["quantiles"]))
    gflags = ", ".join("%s AS g%d" % (_grp_flag(g), i) for i, g in enumerate(gids))
    grp, _ = _grp_case(gids, "g")
    dc = "dd AS d, " if daily else ""
    return f"""
SELECT grp, {"d, " if daily else ""}COUNT(*) AS n_events, COUNT(DISTINCT u) AS n_dev, {qcols}
FROM (
  SELECT u, {dc}v, {grp} AS grp
  FROM (
    SELECT {DEV()} AS u, {DT()} AS dd, {q(d["column"])} AS v, {gflags}
    FROM {T()}
    WHERE {where_dates(d0, d1)} AND {where_scope(scope, subs)} AND ({side_cond(d["source"])})
  ) ev
) t
WHERE grp IS NOT NULL
GROUP BY grp{", d" if daily else ""}"""


def probe_sql(scope, d):
    """接入自检：某一天各步事件条数 / 设备数，以及分组列非空的事件占比。"""
    g = q(_cfg()["experiment"]["group"]["column"])
    evs = sorted({s["event"] for s in C.all_steps()})
    return f"""
SELECT {EV()} AS ev, COUNT(*) AS n, COUNT(DISTINCT {DEV()}) AS uv,
  SUM(CASE WHEN {g} IS NOT NULL THEN 1 ELSE 0 END) AS with_group
FROM {T()}
WHERE {where_dates(d, d)} AND {where_scope(scope)} AND {EV()} IN ({", ".join("'%s'" % _sq(e) for e in evs)})
GROUP BY {EV()}"""
