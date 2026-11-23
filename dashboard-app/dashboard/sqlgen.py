"""SQL 生成：漏斗（单次扫描、CASE 派生环节键）+ 实验指标（设备级充分统计量）。

KG 合规检查约束（2026-10-19 实测）：
- 只输出聚合，不输出设备键；
- 不用 UNION（分支缺别名会让血缘解析失败 → 整表按 L3 拒绝）。
所有用户输入的筛选值都经白名单 + 正则校验后才拼进 SQL。
"""
import datetime as dt
import re

from . import config as C

T = C.TABLE


def split_scope(key):
    """漏斗视图键：'SG' = 整机房，'SG:ID' = 该机房下单个国家，'SG:BR+ID' = 多个国家合计（代码升序、+ 连接）。返回 (market, regions)。"""
    m, _, r = str(key).partition(":")
    if m not in C.MARKETS:
        raise MetricError("未知市场：%s" % m)
    rs = r.split("+") if r else []
    bad = [x for x in rs if x not in C.REGIONS.get(m, [])]
    if bad:
        raise MetricError("%s 市场不支持的国家：%s" % (m, ",".join(bad)))
    if rs != sorted(set(rs)):
        raise MetricError("国家须去重、按代码升序：%s" % r)
    return m, (rs or None)


class Scope(list):
    """实验的读数范围：国家列表（list 本身）+ 按事件排除的搜索 source（excl）。
    当 regions 传给各 SQL 生成函数，_scope 会带上排除条件；空国家列表也照样带（list 为空时仍是 Scope）。"""
    def __init__(self, regions=(), excl=()):
        super().__init__(regions or [])
        self.excl = sorted({str(x) for x in excl or []})


def scope_of(e):
    return Scope(e.get("regions") or [], e.get("excl_src") or [])


_SRC = re.compile(r"^[0-5]$")


def _scope(market, regions=None):
    m = C.MARKETS[market]
    s = "cluster = %d" % m["cluster"]
    excl = getattr(regions, "excl", None) or []
    bad = [x for x in excl if not _SRC.match(x)]
    if bad:
        raise MetricError("排除的 source 应为 0~5：%s" % ",".join(bad))
    # 按事件排除：exp_id 里带 "source":N 的事件不计（没带 source 的事件保留）
    excl_sql = "".join(" AND COALESCE(exp_id, '') NOT LIKE '%%\"source\":%s%%'" % x for x in excl)
    # 选了国家就只按所选国家；没选才用市场默认口径（RU 机房默认只算 region=RU）
    if m["region"] and not regions:
        s += " AND region = '%s'" % m["region"]
    if regions:
        bad = [r for r in regions if r not in C.REGIONS.get(market, [])]
        if bad:
            raise MetricError("%s 市场不支持的国家：%s" % (market, ",".join(map(str, bad))))
        s += " AND region IN (%s)" % ",".join("'%s'" % r for r in regions)
    return s + excl_sql


# ─────────────────────────── 漏斗 ───────────────────────────

FUNNEL_STEPS = [
    # (环节键, 中文, 是否只有全量档)
    ("entry_click", "入口点击", False),
    ("mid_expose", "中间页曝光", False),
    ("mid_click", "中间页点击", False),
    ("result_load", "结果页加载", False),
    ("no_result", "无结果", True),
    ("card_expose", "卡片曝光", False),
    ("card_click", "卡片点击", False),
    ("play_vv", "准备播放", False),
    ("play_3s", "3s 有效播放", False),
]
FUNNEL_COLS = ["pv", "uv", "pv_tb", "uv_tb", "pv_site", "uv_site",
               "pv_nf", "uv_nf", "pv_site_nf", "uv_site_nf", "uv_tb_nf"]


def funnel_sql(market, d0, d1, regions=None):
    return f"""
SELECT date, ek,
  COUNT(*) AS pv, COUNT(DISTINCT instance_id) AS uv,
  SUM(tb) AS pv_tb, COUNT(DISTINCT CASE WHEN tb = 1 THEN instance_id END) AS uv_tb,
  SUM(1 - tb) AS pv_site, COUNT(DISTINCT CASE WHEN tb = 0 THEN instance_id END) AS uv_site,
  SUM(nf) AS pv_nf, COUNT(DISTINCT CASE WHEN nf = 1 THEN instance_id END) AS uv_nf,
  SUM(nf * (1 - tb)) AS pv_site_nf, COUNT(DISTINCT CASE WHEN nf = 1 AND tb = 0 THEN instance_id END) AS uv_site_nf,
  COUNT(DISTINCT CASE WHEN nf = 1 AND tb = 1 THEN instance_id END) AS uv_tb_nf
FROM (
  SELECT date, instance_id,
    CASE
      WHEN event_name = 'search_click' THEN 'entry_click'
      WHEN event_name = 'toolbar_click' AND click LIKE 'search:%' THEN 'entry_click'
      WHEN event_name = 'search_page_expose' THEN 'mid_expose'
      WHEN event_name = 'search_page_click' THEN 'mid_click'
      WHEN event_name = 'search_detail_expose' THEN 'result_load'
      WHEN event_name = 'search_no_results' THEN 'no_result'
      WHEN event_name = 'search_card_expose' THEN 'card_expose'
      WHEN event_name = 'search_card_click' THEN 'card_click'
      WHEN event_name = 'play_start_ready' THEN 'play_vv'
      WHEN event_name = 'play_close' AND (is_3s_valid_play OR play_duration >= 3000) THEN 'play_3s'
    END AS ek,
    CASE
      WHEN event_name = 'toolbar_click' THEN 1
      WHEN event_name = 'search_click' THEN 0
      WHEN event_name IN ('play_start_ready', 'play_close') THEN CASE WHEN COALESCE(source, '') = 'toolbar' THEN 1 ELSE 0 END
      ELSE CASE WHEN `from` = 'toolbar' THEN 1 ELSE 0 END
    END AS tb,
    CASE WHEN `from` = '{C.FALLBACK_FROM}' THEN 0 ELSE 1 END AS nf
  FROM {T}
  WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)}
    AND (event_name IN ('search_click', 'toolbar_click', 'search_page_expose', 'search_page_click',
                        'search_detail_expose', 'search_no_results', 'search_card_expose', 'search_card_click')
         OR (event_name IN ('play_start_ready', 'play_close') AND `from` IN {PLAY_FROM3}))
) e
WHERE ek IS NOT NULL
GROUP BY date, ek"""


# 包含式 UV 漏斗：设备当天走到第 k 步，必须当天也走过前面所有步，转化率因此 ≤ 100%。
# 站内 / Toolbar 各一条路径；Toolbar 带词直达结果页，「中间页点击」视为通过；全量 = 任一路径到达。
FLOW_STEPS = [
    # (环节键, 中文, 所属段)
    ("entry", "入口点击", "入口 → 中间页"),
    ("mid_expose", "中间页曝光", "入口 → 中间页"),
    ("mid_click", "中间页点击", "入口 → 中间页"),
    ("result_load", "结果页加载", "结果页 → 卡片"),
    ("card_expose", "卡片曝光", "结果页 → 卡片"),
    ("card_click", "卡片点击", "结果页 → 卡片"),
    ("play_ready", "准备播放", "播放（搜索来源）"),
    ("play_close", "播放结束", "播放（搜索来源）"),
    ("play_3s", "3s 有效播放", "播放（搜索来源）"),
]
FLOW_TIERS = ("all", "site", "tb")
PLAY_FROM3 = "(%s)" % ",".join("'%s'" % f for f in C.PLAY_FROM if f != C.FALLBACK_FROM)


def _flow_chain(tier):
    """每一步到达条件 = 本步及之前各步标记之积（标记均为 0/1）。"""
    f = {"entry": "e_%s", "mid_expose": "m_%s", "mid_click": "c_%s", "result_load": "r_%s",
         "card_expose": "x_%s", "card_click": "k_%s", "play_ready": "p", "play_close": "q", "play_3s": "v"}
    out, acc = [], []
    for k, _, _ in FLOW_STEPS:
        if not (tier == "tb" and k == "mid_click"):
            acc.append(f[k] % tier if "%s" in f[k] else f[k])
        out.append((k, " * ".join(acc)))
    return out


def flow_sql(market, d0, d1, regions=None):
    """一次扫描出三档包含式漏斗（设备-天单位），外加两条注释用的旁路量。"""
    nf = "`from` <> '%s'" % C.FALLBACK_FROM
    site, tb = "COALESCE(`from`, '') <> 'toolbar'", "`from` = 'toolbar'"
    flag = lambda cond: "MAX(CASE WHEN %s THEN 1 ELSE 0 END)" % cond
    marks = {
        "e_site": "event_name = 'search_click'",
        "e_tb": "event_name = 'toolbar_click' AND click LIKE 'search:%'",
        "m_site": "event_name = 'search_page_expose' AND " + site,
        "m_tb": "event_name = 'search_page_expose' AND " + tb,
        "c_site": "event_name = 'search_page_click' AND " + site,
        "r_site": "event_name = 'search_detail_expose' AND " + site,
        "r_tb": "event_name = 'search_detail_expose' AND " + tb,
        "x_site": "event_name = 'search_card_expose' AND %s AND %s" % (nf, site),
        "x_tb": "event_name = 'search_card_expose' AND " + tb,
        "k_site": "event_name = 'search_card_click' AND %s AND %s" % (nf, site),
        "k_tb": "event_name = 'search_card_click' AND " + tb,
        # 播放侧不按 source 分档：Toolbar 拉起的会话里做的站内搜索，播放也带 source=toolbar
        "p": "event_name = 'play_start_ready'",
        "q": "event_name = 'play_close'",
        "v": "event_name = 'play_close' AND (is_3s_valid_play OR play_duration >= 3000)",
    }
    site_c, tb_c = dict(_flow_chain("site")), dict(_flow_chain("tb"))
    cols = []
    for t, ch in (("site", site_c), ("tb", tb_c)):
        cols += ["SUM(%s) AS %s_%s" % (ch[k], t, k) for k, _, _ in FLOW_STEPS]
    cols += ["SUM(GREATEST(%s, %s)) AS all_%s" % (site_c[k], tb_c[k], k) for k, _, _ in FLOW_STEPS]
    # 旁路①：站内中间页点击后当天没进结果页、但有搜索来源播放（联想词直达播放）
    cols.append("SUM(%s * (1 - r_site) * p) AS site_direct_play" % site_c["mid_click"])
    # 旁路②：走到卡片点击、有播放结束但当天没准备播放（冷启动补发，包含式口径下被剔除）
    for t, k in (("site", site_c["card_click"]), ("tb", tb_c["card_click"]),
                 ("all", "GREATEST(%s, %s)" % (site_c["card_click"], tb_c["card_click"]))):
        cols.append("SUM(%s * q * (1 - p)) AS %s_close_no_ready" % (k, t))
    return f"""
SELECT date,
  {(","+chr(10)+"  ").join(cols)}
FROM (
  SELECT date, instance_id,
    {(","+chr(10)+"    ").join("%s AS %s" % (flag(c), k) for k, c in marks.items())}
  FROM {T}
  WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)}
    AND (event_name IN ('search_click', 'toolbar_click', 'search_page_expose', 'search_page_click',
                        'search_detail_expose', 'search_card_expose', 'search_card_click')
         OR (event_name IN ('play_start_ready', 'play_close') AND `from` IN {PLAY_FROM3}))
  GROUP BY date, instance_id
) d
GROUP BY date"""


FLOW_COLS = (["%s_%s" % (t, k) for t in FLOW_TIERS for k, _, _ in FLOW_STEPS]
             + ["site_direct_play"] + ["%s_close_no_ready" % t for t in FLOW_TIERS])


def core_sql(market, d0, d1, regions=None):
    """核心指标逐日（设备-天，对齐实验预置口径）；漏斗刷新时顺带拉，周均走势补历史也用它。
    设备-天标记求和 = 当天去重 UV，所以 vv_den / c3_den / expose / nr 与 funnel_sql 的 result_load / card_click /
    card_expose / no_result UV（排兜底档位规则同 TIERS_NF）同值。
    点击→3s = 当天点过卡且有 3s 播放的设备 ÷ 点卡设备；
    人均 VV = Σ min(当天 VV, 当天 P99) ÷ 结果页加载设备，P99 取当天有结果页加载设备的单设备 VV（未播放按 0）。"""
    nf = "COALESCE(`from`, '') <> '%s'" % C.FALLBACK_FROM
    site, tb = "COALESCE(`from`, '') <> 'toolbar'", "`from` = 'toolbar'"
    flag = lambda cond: "MAX(CASE WHEN %s THEN 1 ELSE 0 END)" % cond
    marks = {
        "r_site": "event_name = 'search_detail_expose' AND " + site,
        "r_tb": "event_name = 'search_detail_expose' AND " + tb,
        "x_site": "event_name = 'search_card_expose' AND %s AND %s" % (nf, site),
        "x_tb": "event_name = 'search_card_expose' AND " + tb,
        "k_site": "event_name = 'search_card_click' AND %s AND %s" % (nf, site),
        "k_tb": "event_name = 'search_card_click' AND " + tb,
        "n": "event_name = 'search_no_results'",
        "v": "event_name = 'play_close' AND (is_3s_valid_play OR play_duration >= 3000)",
    }
    cols = []
    for t, r, x, k in (("site", "r_site", "x_site", "k_site"), ("tb", "r_tb", "x_tb", "k_tb"),
                       ("all", "GREATEST(r_site, r_tb)", "GREATEST(x_site, x_tb)", "GREATEST(k_site, k_tb)")):
        cols += ["SUM(%s) AS c3_den_%s" % (k, t), "SUM(%s * v) AS c3_num_%s" % (k, t),
                 "SUM(%s) AS vv_den_%s" % (r, t), "SUM(%s * vv) AS vv_raw_%s" % (r, t),
                 "SUM(%s * LEAST(vv, cap)) AS vv_cap_%s" % (r, t), "SUM(%s) AS expose_%s" % (x, t)]
    # 无结果事件没有 from，只有全量档
    cols += ["SUM(n) AS nr_all", "MAX(cap) AS vv_p99"]
    return f"""
SELECT date,
  {(","+chr(10)+"  ").join(cols)}
FROM (
  SELECT date, {", ".join(marks)}, vv,
    approx_percentile(CASE WHEN GREATEST(r_site, r_tb) = 1 THEN vv END, 0.99) OVER (PARTITION BY date) AS cap
  FROM (
    SELECT date, instance_id,
      {(","+chr(10)+"      ").join("%s AS %s" % (flag(c), k) for k, c in marks.items())},
      SUM(CASE WHEN event_name = 'play_start_ready' THEN 1 ELSE 0 END) AS vv
    FROM {T}
    WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)}
      AND (event_name IN ('search_detail_expose', 'search_card_expose', 'search_card_click', 'search_no_results')
           OR (event_name IN ('play_start_ready', 'play_close') AND `from` IN {PLAY_FROM3}))
    GROUP BY date, instance_id
  ) d
) w
GROUP BY date"""


CORE_COLS = (["%s_%s" % (c, t) for t in FLOW_TIERS for c in ("c3_den", "c3_num", "vv_den", "vv_raw", "vv_cap", "expose")]
             + ["nr_all", "vv_p99"])


def split_sql(market, d0, d1, regions=None, versions=None):
    """分来源观测逐日（只看新版本设备，设备-天，全量档）。主线合并口径不变，这里另出两条：
    结果页：res_load 结果页加载 / res_click 其中点卡（排兜底）/ res_c3 点卡且有结果页 3s 播放 / res_play 有结果页起播 / res_vv、res_vv_cap 起播次数（P99 封顶按当天）；
    中间页：mid_expose 中间页曝光 / mid_rec_click 其中点「为你推荐」/ mid_c3 点「为你推荐」且有其 3s 播放 / mid_play 有「为你推荐」起播 / mid_vv、mid_vv_cap 同上（分母 = 中间页曝光设备）。"""
    nf = "COALESCE(`from`, '') <> '%s'" % C.FALLBACK_FROM
    fr, fm = _in(C.FROM_RESULT), _in(C.FROM_MIDREC)
    flag = lambda cond: "MAX(CASE WHEN %s THEN 1 ELSE 0 END)" % cond
    cnt = lambda cond: "SUM(CASE WHEN %s THEN 1 ELSE 0 END)" % cond
    marks = {
        "r": "event_name = 'search_detail_expose'",
        "k": "event_name = 'search_card_click' AND " + nf,
        "rv3": "event_name = 'play_close' AND (is_3s_valid_play OR play_duration >= 3000) AND `from` IN " + fr,
        "m": "event_name = 'search_page_expose'",
        "mk": "event_name = 'search_page_click' AND click = 'related'",
        "mv3": "event_name = 'play_close' AND (is_3s_valid_play OR play_duration >= 3000) AND `from` IN " + fm,
    }
    return f"""
SELECT date,
  SUM(r) AS res_load, SUM(r * k) AS res_click, SUM(k) AS res_c3_den, SUM(k * rv3) AS res_c3,
  SUM(CASE WHEN rvv > 0 THEN 1 ELSE 0 END) AS res_play, SUM(r * rvv) AS res_vv, SUM(r * LEAST(rvv, rcap)) AS res_vv_cap,
  SUM(m) AS mid_expose, SUM(m * mk) AS mid_rec_click, SUM(mk) AS mid_c3_den, SUM(mk * mv3) AS mid_c3,
  SUM(CASE WHEN mvv > 0 THEN 1 ELSE 0 END) AS mid_play, SUM(m * mvv) AS mid_vv, SUM(m * LEAST(mvv, mcap)) AS mid_vv_cap
FROM (
  SELECT date, {", ".join(marks)}, rvv, mvv,
    approx_percentile(CASE WHEN r = 1 THEN rvv END, 0.99) OVER (PARTITION BY date) AS rcap,
    approx_percentile(CASE WHEN m = 1 THEN mvv END, 0.99) OVER (PARTITION BY date) AS mcap
  FROM (
    SELECT date, instance_id,
      {(","+chr(10)+"      ").join("%s AS %s" % (flag(c), k) for k, c in marks.items())},
      {cnt("event_name = 'play_start_ready' AND `from` IN " + fr)} AS rvv,
      {cnt("event_name = 'play_start_ready' AND `from` IN " + fm)} AS mvv
    FROM {T}
    WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)} AND CAST(app_version AS STRING) IN {_in(sorted(versions or C.SPLIT_VERSIONS))}
      AND (event_name IN ('search_detail_expose', 'search_card_click', 'search_page_expose', 'search_page_click')
           OR (event_name IN ('play_start_ready', 'play_close') AND `from` IN {_in(C.FROM_RESULT + C.FROM_MIDREC)}))
    GROUP BY date, instance_id
  ) d
) w
GROUP BY date"""


def _in(fs):
    return "(%s)" % ",".join("'%s'" % f for f in fs)


def position_sql(market, d0, d1, regions=None):
    """Top20 位置 CTR（PV 口径，排兜底）。"""
    return f"""
SELECT date, position, tb,
  SUM(CASE WHEN event_name = 'search_card_expose' THEN 1 ELSE 0 END) AS expose,
  SUM(CASE WHEN event_name = 'search_card_click' THEN 1 ELSE 0 END) AS click
FROM (
  SELECT date, position, event_name, CASE WHEN `from` = 'toolbar' THEN 1 ELSE 0 END AS tb
  FROM {T}
  WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)}
    AND event_name IN ('search_card_expose', 'search_card_click')
    AND `from` <> '{C.FALLBACK_FROM}' AND position BETWEEN 1 AND 20
) e
GROUP BY date, position, tb"""


def experiment_position_sql(market, gids, d0, d1, regions=None):
    """实验期各组 Top20 位置 CTR（PV，排兜底）：按 组 × 位置 × 档位（站内 / Toolbar）计数。
    按事件自带的 exp_id 认组；跨组事件（X）不出。口径同漏斗的「Top20 位置 CTR」。"""
    gflags = ", ".join("%s AS g%d" % (_grp_flag(g), i) for i, g in enumerate(gids))
    grp = _grp_case(gids, "g")[0]
    return f"""
SELECT grp, position, tb,
  SUM(CASE WHEN event_name = 'search_card_expose' THEN 1 ELSE 0 END) AS expose,
  SUM(CASE WHEN event_name = 'search_card_click' THEN 1 ELSE 0 END) AS click
FROM (
  SELECT {grp} AS grp, position,
         CASE WHEN `from` = 'toolbar' THEN 1 ELSE 0 END AS tb, event_name
  FROM (
    SELECT position, event_name, `from`, {gflags}
    FROM {T}
    WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)}
      AND event_name IN ('search_card_expose', 'search_card_click')
      AND `from` <> '{C.FALLBACK_FROM}' AND position BETWEEN 1 AND 20
  ) ev
) s
GROUP BY grp, position, tb"""


def dau_sql(market, d0, d1, regions=None):
    # 选了国家就按所选国家的 region_app 取 DAU（多国 = 设备去重合计）；整机房（SG / EU）没有 DAU 口径
    regs = regions or ([C.MARKETS[market]["dau_region"]] if C.MARKETS[market]["dau_region"] else [])
    if not regs:
        return None
    vals = ", ".join("'%s', '%s'" % (r, r.lower()) for r in regs)
    return f"""
SELECT date, COUNT(DISTINCT instance_id) AS dau
FROM {C.DAU_TABLE}
WHERE date BETWEEN {d0} AND {d1} AND region_app IN ({vals})
GROUP BY date"""


# ─────────────────────────── 指标定义校验 ───────────────────────────

STR_COLS = {"from": "`from`", "source": "source", "click": "click", "video_type": "video_type",
            "cp": "cp", "etl_source": "etl_source", "page": "page"}
NUM_COLS = {"position": "position", "app_version": "app_version", "use_time": "use_time",
            "play_duration": "play_duration", "video_duration": "video_duration"}
BOOL_COLS = {"is_3s_valid_play": "is_3s_valid_play"}
# 派生列（不在事件表里）：用到时事件明细层才 LEFT JOIN 内容表，没用到的指标 SQL 与原来逐字一致。
# publish_age_days = 曝光日 − 视频 publish_time 日期（天）；关联不到或 publish_time 为空 = NULL，任何比较都不成立。
# 「≤ 30」与 PRD AnaEddYGvoe7X9xMKLscBCDWnVB 附 SQL 的 publish_time >= date_sub(曝光日, 30) 等价（Spark 语法）。
AGE_COL = "publish_age_days"
NUM_COLS[AGE_COL] = AGE_COL
# 带 video_id 的事件（2026-10-20 SG 实测：卡片曝光/点击全量带，播放类 ≥ 99.97%；结果页加载、无结果、入口类全为空）
VIDEO_EVENTS = {"search_card_expose", "search_card_click", "play_start", "play_start_ready", "play_close"}
_AGE_RE = re.compile(r"\b%s\b" % AGE_COL)
SUM_COLS = NUM_COLS
EVENTS = ["search_click", "toolbar_click", "search_page_expose", "search_page_click",
          "search_detail_expose", "search_no_results", "search_card_expose", "search_card_click",
          "search_related_expose", "play_start", "play_start_ready", "play_close"]
OPS = {"=", "!=", "in", "not_in", ">", ">=", "<", "<=", "like", "is_null", "not_null"}
# 指标库表单里给人看的中文名（只用于展示和模糊匹配，不进 SQL）
LABELS = {
    "search_click": "搜索入口点击（站内）", "toolbar_click": "Toolbar 点击", "search_page_expose": "中间页曝光",
    "search_page_click": "中间页点击", "search_detail_expose": "结果页加载", "search_no_results": "无结果",
    "search_card_expose": "卡片曝光", "search_card_click": "卡片点击", "search_related_expose": "联想词曝光",
    "play_start": "开始播放", "play_start_ready": "准备播放", "play_close": "播放结束",
    "from": "来源页", "source": "拉起来源", "click": "点击位", "video_type": "视频类型", "cp": "内容方",
    "etl_source": "ETL 来源", "page": "页面", "position": "位置", "app_version": "App 版本号",
    "use_time": "耗时（ms）", "play_duration": "播放时长（ms）", "video_duration": "视频时长（ms）",
    "is_3s_valid_play": "3s 有效播放", AGE_COL: "发布距曝光日天数（关联内容表 publish_time）",
}
_STR_VAL = re.compile(r"^[A-Za-z0-9_.:\-%]{1,64}$")
_NUM_VAL = re.compile(r"^-?\d+(\.\d+)?$")
_COL_EXPR = re.compile(r"^(\d+(?:\.\d+)?)\s*\*\s*([a-z_0-9]+)$")


class MetricError(ValueError):
    pass


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
    if m and m.group(2) in NUM_COLS:
        return "%s * %s" % (m.group(1), NUM_COLS[m.group(2)])
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
    if col in STR_COLS:
        c = STR_COLS[col]
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
    if col in NUM_COLS:
        c = NUM_COLS[col]
        if op in ("is_null", "not_null"):
            return "%s %s" % (c, "IS NULL" if op == "is_null" else "IS NOT NULL")
        if op in ("in", "not_in"):
            vals = val if isinstance(val, list) else [x.strip() for x in str(val).split(",") if x.strip()]
            return "%s %s (%s)" % (c, "IN" if op == "in" else "NOT IN", ", ".join(_lit_num(x) for x in vals))
        if op == "like":
            raise MetricError("数值列 %s 不支持 like" % col)
        return "%s %s %s" % (c, "<>" if op == "!=" else op, _lit_num(val))
    if col in BOOL_COLS:
        if op not in ("=", "!="):
            raise MetricError("布尔列 %s 只支持 = / !=" % col)
        truth = str(val).lower() in ("1", "true", "yes")
        if op == "!=":
            truth = not truth
        return BOOL_COLS[col] if truth else "NOT %s" % BOOL_COLS[col]
    raise MetricError("列不在白名单：%r" % col)


def side_cond(side):
    evs = side.get("event")
    evs = evs if isinstance(evs, list) else [evs]
    if not evs or any(e not in EVENTS for e in evs):
        raise MetricError("事件不在白名单：%r" % evs)
    parts = ["event_name IN (%s)" % ", ".join("'%s'" % e for e in evs)]
    parts += [filter_sql(f) for f in side.get("filters") or []]
    cond = " AND ".join(parts)
    # 没有 video_id 的事件关联不到内容表，publish_age_days 恒为 NULL → 静默算成 0，直接拒绝
    if _AGE_RE.search(cond) and any(e not in VIDEO_EVENTS for e in evs):
        raise MetricError("%s 只能用在带 video_id 的事件上（%s）：%r" % (AGE_COL, " / ".join(sorted(VIDEO_EVENTS)), evs))
    return cond


def _uses_age(*texts):
    return any(_AGE_RE.search(t) for t in texts)


def _source(market, regions, d0, d1, events, used):
    """事件明细层的 FROM 目标。used = 本层是否用到 publish_age_days：否 → 原表名，SQL 一个字符不变；
    是 → 包一层子查询：先按日期 / 集群 / 本层全部事件（events，含不用该列的指标和认组事件，否则同扫描的其他指标会丢行）过滤，
    再 LEFT JOIN 内容表（只取 video_id, publish_time），派生 publish_age_days。列全部显式列出（不 SELECT *），外层原有 WHERE 照旧。
    内容表也有 region 列，所以集群 / 国家条件放在 JOIN 前的内层子查询里，免得列名二义。"""
    if not used:
        return T
    cols = ["instance_id", "date", "cluster", "region", "event_name", "exp_id"] + \
        [c for c in list(STR_COLS.values()) + list(NUM_COLS.values()) + list(BOOL_COLS.values()) if c != AGE_COL]
    ev_in = ", ".join("'%s'" % e for e in sorted(events))
    # datediff(曝光日, publish_time 日期) ≤ N ⇔ PRD 的 publish_time >= date_sub(曝光日, N)（同一会话时区下 timestamptz 转日期）
    return f"""(
            SELECT {", ".join("e." + c for c in cols)},
                   datediff(to_date(CAST(e.date AS string), 'yyyyMMdd'), to_date(c.publish_time)) AS {AGE_COL}
            FROM (
              SELECT {", ".join(cols)}, video_id
              FROM {T}
              WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)} AND event_name IN ({ev_in})
            ) e
            LEFT JOIN (SELECT video_id, publish_time FROM {C.CONTENT_TABLE}) c
              ON CAST(e.video_id AS string) = CAST(c.video_id AS string)
          ) t"""


def _side_events(*sides):
    out = set()
    for s in sides:
        if s:
            evs = s.get("event")
            out.update(evs if isinstance(evs, list) else [evs])
    return out


def side_expr(side):
    """设备级取值表达式。"""
    cond = side_cond(side)
    agg = side.get("agg", "uv")
    if agg == "uv":
        return "MAX(CASE WHEN %s THEN 1 ELSE 0 END)" % cond
    if agg == "pv":
        return "SUM(CASE WHEN %s THEN 1 ELSE 0 END)" % cond
    if agg == "sum:" + AGE_COL:
        # 关联不到 publish_time 的条数在 SUM 里记 0、在 PV 分母里照计，会把均值拉低；只允许做筛选条件
        raise MetricError("%s 只能用作筛选条件（如 <= 30），不支持求和" % AGE_COL)
    if agg.startswith("sum:") and agg[4:] in SUM_COLS:
        return "SUM(CASE WHEN %s THEN %s ELSE 0 END)" % (cond, SUM_COLS[agg[4:]])
    raise MetricError("不支持的计数方式：%r（uv / pv / sum:<数值列>）" % agg)


def validate_metric(d):
    if not isinstance(d, dict):
        raise MetricError("指标定义要是 JSON 对象")
    kind = d.get("kind", "ratio")
    need = {"ratio": ("numerator", "denominator"), "quantile": ("source",)}.get(kind, ())
    miss = [k for k in need if not isinstance(d.get(k), dict)]
    if miss:
        raise MetricError("缺少 %s" % " / ".join(miss))
    if not str(d.get("name", "")).strip():
        raise MetricError("指标名不能为空")
    if kind == "ratio":
        side_expr(d["numerator"])
        side_expr(d["denominator"])
        if d.get("device_scope"):
            side_cond(d["device_scope"])
        if d.get("cap") not in (None, "", "p99_pre7d"):
            raise MetricError("cap 只支持 p99_pre7d")
    elif kind == "quantile":
        side_cond(d["source"])
        if d.get("column") not in NUM_COLS:
            raise MetricError("分位数列不在白名单")
        evs = d["source"].get("event")
        if d["column"] == AGE_COL and any(e not in VIDEO_EVENTS for e in (evs if isinstance(evs, list) else [evs])):
            raise MetricError("%s 只能用在带 video_id 的事件上（%s）" % (AGE_COL, " / ".join(sorted(VIDEO_EVENTS))))
        qs = d.get("quantiles") or []
        if not qs or any(not (0 < float(q) < 1) for q in qs):
            raise MetricError("分位点须在 (0,1)")
    else:
        raise MetricError("未知指标类型：%r" % kind)
    return d


# ─────────────────────────── 实验 ───────────────────────────

_GID = re.compile(r"^\d{1,12}$")


def _grp_flag(gid):
    if not _GID.match(str(gid)):
        raise MetricError("exp_id 应为纯数字：%r" % gid)
    return "CASE WHEN exp_id LIKE '%%\"exp_id\":\"%s\"%%' THEN 1 ELSE 0 END" % gid


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
    if m["def"].get("cap") != "p99_pre7d":
        return None
    cap = caps.get(m["key"])
    if cap is None:
        raise MetricError("指标 %s 缺少开窗前 P99 封顶值" % m["name"])
    return float(cap)


def _day_x(m, caps):
    """设备-天的分子：封顶指标按「单设备单天 min(值, P99)」；numerator_within_denominator 时只算当天有分母的设备-天。"""
    k, cap = m["key"], _cap_of(m, caps)
    x = "x_%s" % k if cap is None else "LEAST(x_%s, %s)" % (k, cap)
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


def _dd_sql(market, gids, d0, d1, metrics, caps, regions):
    """设备-天层（对齐回收文档 SQL 的 dd/dc）：只保留当天事件带组号的设备-天；f_i = 当天命中第 i 组。"""
    gflags = ", ".join("%s AS g%d" % (_grp_flag(g), i) for i, g in enumerate(gids))
    fday = ", ".join("MAX(g%d) AS f%d" % (i, i) for i in range(len(gids)))
    conds, cols = _ratio_cols(metrics)
    # 认组事件对齐回收文档 ev：四个结果页事件不论 from 都参与认组
    conds.add("event_name IN ('search_detail_expose', 'search_card_expose', 'search_card_click', 'search_no_results')")
    caps_cols = ", ".join("%s AS xd_%s" % (_day_x(m, caps), m["key"]) for m in metrics)
    _, hits = _grp_case(gids, "f")
    # 用到 publish_age_days 才关联内容表；JOIN 前的事件过滤要覆盖本层全部事件（各指标分子 / 分母 / device_scope + 认组事件）
    age = _uses_age(*cols)
    src = _source(market, regions, d0, d1, _side_events(
        *[m["def"].get(s) for m in metrics for s in ("numerator", "denominator", "device_scope")])
        | {"search_detail_expose", "search_card_expose", "search_card_click", "search_no_results"}, age)
    return f"""
      SELECT d.*, {caps_cols}
      FROM (
        SELECT instance_id, date, {fday}, {", ".join(cols)}
        FROM (
          SELECT instance_id, date, event_name, `from`, source, click, video_type, cp, etl_source, page,
                 position, app_version, use_time, play_duration, video_duration, is_3s_valid_play, {"%s, " % AGE_COL if age else ""}{gflags}
          FROM {src}
          WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)}
            AND ({" OR ".join("(%s)" % c for c in sorted(conds))})
        ) ev
        GROUP BY instance_id, date
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
            # 对齐回收文档累计列 MAX(cc)*MAX(p3)：窗口内有过分子 × 窗口内有过分母，不要求同一天
            roll.append("MAX(x_%s) * MAX(CASE WHEN y_%s > 0 THEN 1 ELSE 0 END) AS x_%s, %s(y_%s) AS y_%s" % (k, k, k, den, k, k))
        else:
            roll.append("%s(xd_%s) AS x_%s, %s(y_%s) AS y_%s" % ("MAX" if uv_num else "SUM", k, k, den, k, k))
        if m["def"].get("device_scope"):
            roll.append("MAX(s_%s) AS s_%s" % (k, k))
    return roll


def _dev_sql(market, gids, d0, d1, metrics, caps, regions, first=False, keep_id=False):
    """设备层：每设备一行 grp + xx_k / yy_k；全窗口比率与分布共用同一并集口径。first = 带上首次带组号的日期；
    keep_id = 带上 instance_id（CUPED / 分群要按设备 JOIN，外层仍只出聚合）。"""
    dd = _dd_sql(market, gids, d0, d1, metrics, caps, regions)
    grp, _ = _grp_case(gids, "a")
    aflags = ", ".join("MAX(f%d) AS a%d" % (i, i) for i in range(len(gids)))
    return f"""
  SELECT {"instance_id, " if keep_id else ""}grp, {"first_date, " if first else ""}{", ".join(_adj(metrics, caps))}
  FROM (
    SELECT {grp} AS grp, v.*
    FROM (
      SELECT instance_id, {"MIN(date) AS first_date, " if first else ""}{aflags}, {", ".join(_roll(metrics, caps))}
      FROM ({dd}
      ) dd
      GROUP BY instance_id
    ) v
  ) g
  WHERE grp IS NOT NULL"""


def experiment_ratio_sql(market, gids, d0, d1, metrics, caps, regions=None):
    """全窗口 / 前 N 天累计：设备在窗口内（只看带组号的设备-天）取并集；同一设备跨组 → X 剔除。每组一行充分统计量。
    UV 分子 + 分子只算分母设备：窗口内有过分子 × 窗口内有过分母（对齐回收文档 MAX(cc)*MAX(p3)，不要求同一天）。"""
    return f"""
SELECT grp, COUNT(*) AS n, {", ".join(_stats_cols(metrics))}
FROM ({_dev_sql(market, gids, d0, d1, metrics, caps, regions)}
) a
GROUP BY grp"""


DIST_Q = (0.05, 0.25, 0.5, 0.75, 0.95)


def experiment_dist_sql(market, gids, d0, d1, metrics, caps, regions, dist_keys, days):
    """分布 + 进组趋势，一次扫描，每组一行（只出聚合）。
    metrics = 实验全部比率指标（设备集合与全窗口比率一致）；
    分布：dist_keys 里的指标，每设备取值 = 分子 ÷ 分母（同一并集、同一 P99 封顶），只看分母 > 0 的设备，出 P5/P25/P50/P75/P95 + 设备均值；
    进组：e_<日期> = 窗口内首次带组号是这天的设备数（跨组 → X 剔除），累计到窗口末 = 分流检查的各组设备数。"""
    cols = []
    for k in dist_keys:
        v = "CASE WHEN yy_%s > 0 THEN CAST(xx_%s AS DOUBLE) / yy_%s END" % (k, k, k)
        cols += ["approx_percentile(%s, %s) AS q%d_%s" % (v, q, i, k) for i, q in enumerate(DIST_Q)]
        cols.append("AVG(%s) AS avg_%s, SUM(CASE WHEN yy_%s > 0 THEN 1 ELSE 0 END) AS n_%s" % (v, k, k, k))
    cols += ["SUM(CASE WHEN first_date = %d THEN 1 ELSE 0 END) AS e_%d" % (int(d), int(d)) for d in days]
    return f"""
SELECT grp, COUNT(*) AS n, {", ".join(cols)}
FROM ({_dev_sql(market, gids, d0, d1, metrics, caps, regions, first=True)}
) a
GROUP BY grp"""


CUPED_V = "xyab"   # x / y = 实验期分子 / 分母；a / b = 上线前同口径分子 / 分母


def cuped_metrics(metrics):
    """能做 CUPED 的比率指标：不带 device_scope（上线前没有对应的设备范围定义）。"""
    return [m for m in metrics if not m["def"].get("device_scope")]


def _pre_dev_sql(market, p0, p1, metrics, caps, regions):
    """上线前（p0~p1）每设备一行 px_k / py_k：口径同实验期（单设备单天 P99 封顶、窗口内并集、分子只算分母设备），不认组。"""
    conds, cols = _ratio_cols(metrics)
    xd = ", ".join("%s AS xd_%s" % (_day_x(m, caps), m["key"]) for m in metrics)
    age = _uses_age(*cols)
    src = _source(market, regions, p0, p1, _side_events(
        *[m["def"].get(s) for m in metrics for s in ("numerator", "denominator")]), age)
    roll = [c.replace(" AS x_", " AS px_").replace(" AS y_", " AS py_") for c in _roll(metrics, caps)]
    return f"""
      SELECT instance_id, {", ".join(roll)}
      FROM (
        SELECT d.*, {xd}
        FROM (
          SELECT instance_id, date, {", ".join(cols)}
          FROM (
            SELECT instance_id, date, event_name, `from`, source, click, video_type, cp, etl_source, page,
                   position, app_version, use_time, play_duration, video_duration, is_3s_valid_play{", " + AGE_COL if age else ""}
            FROM {src}
            WHERE date BETWEEN {p0} AND {p1} AND {_scope(market, regions)}
              AND ({" OR ".join("(%s)" % c for c in sorted(conds))})
          ) ev
          GROUP BY instance_id, date
        ) d
      ) pd
      GROUP BY instance_id"""


def experiment_cuped_sql(market, gids, d0, d1, metrics, caps, regions, p0, p1):
    """CUPED 原料：实验期设备层（同全窗口比率）LEFT JOIN 上线前设备层，每组一行：n + 四个变量的和 + 两两乘积和。
    上线前没有行为的设备 a = b = 0（照样计入，协变量是上线前就定了的，不受实验影响）。"""
    ms = cuped_metrics(metrics)
    agg = ["COUNT(*) AS n"]
    for m in ms:
        k = m["key"]
        # 分子只算分母设备：_roll 已在上线前层做了（UV 类 MAX(x) * 有过分母；其余按天只计有分母的设备-天）
        v = {"x": "xx_%s" % k, "y": "yy_%s" % k, "a": "COALESCE(px_%s, 0)" % k, "b": "COALESCE(py_%s, 0)" % k}
        for i, p in enumerate(CUPED_V):
            agg.append("SUM(%s) AS s%s_%s" % (v[p], p, k))
            for q in CUPED_V[i:]:
                agg.append("SUM(%s * %s) AS s%s%s_%s" % (v[p], v[q], p, q, k))
    return f"""
SELECT grp, {", ".join(agg)}
FROM ({_dev_sql(market, gids, d0, d1, ms, caps, regions, keep_id=True)}
) p
LEFT JOIN ({_pre_dev_sql(market, p0, p1, ms, caps, regions)}
) r ON p.instance_id = r.instance_id
GROUP BY grp"""


# 长期价值（@lt）：每设备一个值，组内取设备均值。认组只用结果页四个事件（不拼指标事件），所以不复用 _dd_sql / _dev_sql。
LT_KEYS = ("lt7", "r1", "r7", "vv", "vv3", "dur")
LT_CAPPED = ("vv", "vv3", "dur")          # 单设备 7 日累计值按上线前 7 天 P99 封顶，caps 键 = "@lt_" + key
_RESULT4 = "event_name IN ('search_detail_expose', 'search_card_expose', 'search_card_click', 'search_no_results')"


def _ymd(d, days=0):
    return int((dt.datetime.strptime(str(d), "%Y%m%d") + dt.timedelta(days=days)).strftime("%Y%m%d"))


def _as_date(col):
    return "to_date(CAST(%s AS string), 'yyyyMMdd')" % col


def dau_max_sql(d0, d1):
    """App 活跃表在 d0~d1 内的最新日期：成熟截止不能晚于它（活跃表晚到时，最后几天的活跃会被少算）。"""
    return f"""
SELECT MAX(date) AS d
FROM {C.DAU_TABLE}
WHERE date BETWEEN {d0} AND {d1}"""


def _lt_play_sql(market, p0, p1):
    """play_close 按设备-天：条数 / 3s 条数 / 时长（play_duration ∈ [0, 3600000] ms 之和）；只限实验市场的 cluster，不限 region / from
    （设备范围已由认组限定）。"""
    return f"""
    SELECT instance_id, date, COUNT(*) AS vv, SUM(CASE WHEN is_3s_valid_play THEN 1 ELSE 0 END) AS vv3,
           SUM(CASE WHEN play_duration BETWEEN 0 AND 3600000 THEN play_duration ELSE 0 END) AS dur
    FROM {T}
    WHERE date BETWEEN {p0} AND {p1} AND cluster = {C.MARKETS[market]["cluster"]} AND event_name = 'play_close'
    GROUP BY instance_id, date"""


def lt_cap_sql(market, p0, p1, regions=None):
    """长期价值时长类的封顶值：上线前 p0~p1（7 天）有结果页事件的设备，单设备 7 日累计 VV / 3sVV / 时长（ms）的 P99，没播放记 0。"""
    return f"""
SELECT approx_percentile(vv, 0.99) AS vv, approx_percentile(vv3, 0.99) AS vv3, approx_percentile(dur, 0.99) AS dur,
       COUNT(*) AS n_dev
FROM (
  SELECT COALESCE(q.vv, 0) AS vv, COALESCE(q.vv3, 0) AS vv3, COALESCE(q.dur, 0) AS dur
  FROM (
    SELECT instance_id
    FROM {T}
    WHERE date BETWEEN {p0} AND {p1} AND {_scope(market, regions)} AND {_RESULT4}
    GROUP BY instance_id
  ) s
  LEFT JOIN (
    SELECT instance_id, SUM(vv) AS vv, SUM(vv3) AS vv3, SUM(dur) AS dur
    FROM ({_lt_play_sql(market, p0, p1)}
    ) d
    GROUP BY instance_id
  ) q ON q.instance_id = s.instance_id
) v"""


def experiment_lt_sql(market, gids, d0, d1, regions, cut, caps=None):
    """长期价值原料，每组一行（只出聚合）。
    认组：窗口 d0~d1 内结果页四个事件带组号的设备-天取并集，跨组 → X 剔除；设备的 d0 = 首次带组号的日期，一台设备只算一次。
    成熟：只留 d0 + 7 ≤ cut 的设备。窗口 = [d0+1, d0+7]（不含 d0）：
    大盘 LT7 = 窗口内出现在 App 活跃表的天数，次留 / 7 留 = d0+1 / d0+7 当天是否活跃（活跃表按 instance_id + date 去重，不按 region 过滤）；
    7 日 VV / 3sVV / 时长 = 窗口内 play_close（不限 from），单设备 7 日累计值按 caps 封顶（None = 不封顶），时长出分钟。"""
    gflags = ", ".join("MAX(%s) AS f%d" % (_grp_flag(g), i) for i, g in enumerate(gids))
    aflags = ", ".join("MAX(f%d) AS a%d" % (i, i) for i in range(len(gids)))
    grp, _ = _grp_case(gids, "a")
    _, fhits = _grp_case(gids, "f")
    off = lambda c: "datediff(%s, %s)" % (_as_date(c), _as_date("p.first_date"))

    def cap(k, x):
        return "LEAST(%s, %s)" % (x, float(caps["@lt_" + k])) if caps else x
    return f"""
WITH dev AS (
  SELECT instance_id, grp, first_date
  FROM (
    SELECT instance_id, first_date, {grp} AS grp
    FROM (
      SELECT instance_id, MIN(date) AS first_date, {aflags}
      FROM (
        SELECT instance_id, date, {gflags}
        FROM {T}
        WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)} AND {_RESULT4}
        GROUP BY instance_id, date
      ) dd
      WHERE {fhits} > 0
      GROUP BY instance_id
    ) v
  ) g
  WHERE grp <> 'X' AND first_date <= {_ymd(cut, -7)}
),
act AS (
  SELECT p.instance_id, SUM(u.hit) AS lt7,
         MAX(CASE WHEN {off("u.date")} = 1 THEN u.hit ELSE 0 END) AS r1,
         MAX(CASE WHEN {off("u.date")} = 7 THEN u.hit ELSE 0 END) AS r7
  FROM dev p
  JOIN (
    SELECT instance_id, date, 1 AS hit
    FROM {C.DAU_TABLE}
    WHERE date BETWEEN {_ymd(d0, 1)} AND {cut}
    GROUP BY instance_id, date
  ) u ON u.instance_id = p.instance_id AND {off("u.date")} BETWEEN 1 AND 7
  GROUP BY p.instance_id
),
ply AS (
  SELECT p.instance_id, SUM(q.vv) AS vv, SUM(q.vv3) AS vv3, SUM(q.dur) AS dur
  FROM dev p
  JOIN ({_lt_play_sql(market, _ymd(d0, 1), cut)}
  ) q ON q.instance_id = p.instance_id AND {off("q.date")} BETWEEN 1 AND 7
  GROUP BY p.instance_id
)
SELECT grp, COUNT(*) AS n, {", ".join("SUM({k}) AS s_{k}, SUM({k} * {k}) AS ss_{k}".format(k=k) for k in LT_KEYS)}
FROM (
  SELECT p.grp AS grp, COALESCE(a.lt7, 0) AS lt7, COALESCE(a.r1, 0) AS r1, COALESCE(a.r7, 0) AS r7,
         {cap("vv", "COALESCE(y.vv, 0)")} AS vv, {cap("vv3", "COALESCE(y.vv3, 0)")} AS vv3,
         {cap("dur", "COALESCE(y.dur, 0)")} / 60000.0 AS dur
  FROM dev p
  LEFT JOIN act a ON a.instance_id = p.instance_id
  LEFT JOIN ply y ON y.instance_id = p.instance_id
) v
GROUP BY grp"""



# 长期价值 · 收入（SG）：7 日人均收入 / 广告曝光。人群、窗口与上面完全相同（_lt_dev 与 experiment_lt_sql 的 dev 段逐字一致，测试保证）。
# 口径：事件日志 client_info = 广告侧 gaid（SG 已验证是 GAID）；
# 广告表 date 是 UTC 日，按 log_time（epoch 毫秒）切北京日（北京 D 00:00 = UTC D-1 16:00），多读前一天 UTC 分区；
# 占位 GAID 用标记列剔；L3 字段只在子查询和 JOIN ON 里。查询走广告侧 catalog（REV_CATALOG）。
REV_KEYS = ("fee", "imp")                 # 单设备 7 日累计按上线前 7 天 P99.9 封顶，caps 键 = "@lt_" + key
REV_MARKETS = {"SG": "ad_catalog.ads.ad_event_table"}
REV_CATALOG = {"SG": "ad_catalog"}
REV_PIDS = ("1.000.1.0000", "1.000.2.1", "1.000.3.0000", "1.000.4.0000", "1.000.5.0")


def _bj_ms(d):
    """北京日 d 00:00 的 epoch 毫秒。"""
    t = dt.datetime.strptime(str(d), "%Y%m%d").replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return int(t.timestamp()) * 1000


def _ad_daily_sql(market, b0, b1):
    """广告侧按 gaid × 北京日：计费收入 / 计费曝光（5 个流内广告位，source = billing_new），zg = 占位 GAID 标记。"""
    days, d = [], b0
    while d <= b1:
        days.append(d)
        d = _ymd(d, 1)
    lt = "CAST(log_time AS BIGINT)"
    case = "\n".join("             WHEN %s >= %d AND %s < %d THEN %d" % (lt, _bj_ms(x), lt, _bj_ms(_ymd(x, 1)), x) for x in days)
    return f"""
    SELECT gaid, bd, SUM(fee) AS fee, SUM(imp) AS imp, MAX(zg) AS zg
    FROM (
      SELECT gaid, billing_fee AS fee, billing_view AS imp,
             CASE WHEN gaid = '' OR gaid LIKE '00000000%' THEN 1 ELSE 0 END AS zg,
             CASE
{case}
             END AS bd
      FROM {REV_MARKETS[market]}
      WHERE date BETWEEN {_ymd(b0, -1)} AND {b1} AND source = 'billing_new'
        AND pid IN ({", ".join("'%s'" % p for p in REV_PIDS)})
    ) t
    WHERE bd IS NOT NULL
    GROUP BY gaid, bd"""


def _lt_dev(market, gids, d0, d1, regions, cut):
    """长期价值人群（同 experiment_lt_sql 的 dev 段）。"""
    gflags = ", ".join("MAX(%s) AS f%d" % (_grp_flag(g), i) for i, g in enumerate(gids))
    aflags = ", ".join("MAX(f%d) AS a%d" % (i, i) for i in range(len(gids)))
    grp, _ = _grp_case(gids, "a")
    _, fhits = _grp_case(gids, "f")
    return f"""dev AS (
  SELECT instance_id, grp, first_date
  FROM (
    SELECT instance_id, first_date, {grp} AS grp
    FROM (
      SELECT instance_id, MIN(date) AS first_date, {aflags}
      FROM (
        SELECT instance_id, date, {gflags}
        FROM {T}
        WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)} AND {_RESULT4}
        GROUP BY instance_id, date
      ) dd
      WHERE {fhits} > 0
      GROUP BY instance_id
    ) v
  ) g
  WHERE grp <> 'X' AND first_date <= {_ymd(cut, -7)}
)"""


def _rev_ids(market, d0, d1, regions):
    """设备 → client_info（取自认组用的结果页事件）。一台设备有多个 client_info 时各自关联、收入相加。"""
    return f"""
    SELECT instance_id, client_info
    FROM {T}
    WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)} AND {_RESULT4}
    GROUP BY instance_id, client_info"""


def lt_rev_cap_sql(market, p0, p1, regions=None):
    """收入封顶值：上线前 p0~p1（7 天）有结果页事件的设备，单设备 7 日累计计费收入 / 曝光的 P99.9，没广告记 0。"""
    return f"""
SELECT approx_percentile(fee, 0.999) AS fee, approx_percentile(imp, 0.999) AS imp,
       approx_percentile(fee, 0.99) AS fee_p99, COUNT(*) AS n_dev, SUM(CASE WHEN fee > 0 THEN 1 ELSE 0 END) AS n_fee
FROM (
  SELECT COALESCE(r.fee, 0) AS fee, COALESCE(r.imp, 0) AS imp
  FROM (
    SELECT instance_id
    FROM {T}
    WHERE date BETWEEN {p0} AND {p1} AND {_scope(market, regions)} AND {_RESULT4}
    GROUP BY instance_id
  ) s
  LEFT JOIN (
    SELECT i.instance_id, SUM(a.fee) AS fee, SUM(a.imp) AS imp
    FROM ({_rev_ids(market, p0, p1, regions)}
    ) i
    JOIN ({_ad_daily_sql(market, p0, p1)}
    ) a ON a.gaid = i.client_info
    WHERE a.zg = 0
    GROUP BY i.instance_id
  ) r ON r.instance_id = s.instance_id
) v"""


def experiment_rev_sql(market, gids, d0, d1, regions, cut, caps=None):
    """7 日人均收入 / 广告曝光原料，每组一行（只出聚合）。人群 / 窗口同 experiment_lt_sql：d0 + 7 ≤ cut 的设备，[d0+1, d0+7] 北京日。
    同时出不封顶（s_fee）和封顶（s_feec，caps 为 None 时等于不封顶）；n_fee = 窗口内有收入的设备数，mx_imp = 单设备单日最大曝光（核数用）。"""
    off = "datediff(%s, %s)" % (_as_date("a.bd"), _as_date("i.first_date"))

    def cap(k):
        x = "COALESCE(r.%s, 0)" % k
        return "LEAST(%s, %s)" % (x, float(caps["@lt_" + k])) if caps else x
    cols = ", ".join("SUM({k}) AS s_{k}, SUM({k} * {k}) AS ss_{k}, SUM({k}c) AS s_{k}c, SUM({k}c * {k}c) AS ss_{k}c".format(k=k)
                     for k in REV_KEYS)
    return f"""
WITH {_lt_dev(market, gids, d0, d1, regions, cut)},
ids AS (
  SELECT p.instance_id, p.first_date, k.client_info
  FROM dev p
  JOIN ({_rev_ids(market, d0, d1, regions)}
  ) k ON k.instance_id = p.instance_id
),
rev AS (
  SELECT i.instance_id, SUM(a.fee) AS fee, SUM(a.imp) AS imp, MAX(a.imp) AS mx_imp
  FROM ids i
  JOIN ({_ad_daily_sql(market, _ymd(d0, 1), cut)}
  ) a ON a.gaid = i.client_info AND {off} BETWEEN 1 AND 7
  WHERE a.zg = 0
  GROUP BY i.instance_id
)
SELECT grp, COUNT(*) AS n, {cols},
       SUM(CASE WHEN fee > 0 THEN 1 ELSE 0 END) AS n_fee, MAX(mx_imp) AS mx_imp
FROM (
  SELECT p.grp AS grp, COALESCE(r.fee, 0) AS fee, {cap("fee")} AS feec, COALESCE(r.imp, 0) AS imp, {cap("imp")} AS impc,
         COALESCE(r.mx_imp, 0) AS mx_imp
  FROM dev p
  LEFT JOIN rev r ON r.instance_id = p.instance_id
) v
GROUP BY grp"""


# 分群下钻：每设备一个分群取值，按实验期窗口内的行为划。
# 国家 / 新老 / 入口取自窗口内的 search_detail_expose（新老 = 窗口内任一条 new_user = 1）；
# 搜索词类型取自中间页 search_page_click.click（手输 type_search / 历史 history / 热词 popular，按次数取最多的一类；
# 窗口内没点过这三类 = 「无中间页点词」，主要是 Toolbar 带词直达）。
SEG_DIMS = {
    "nu": ("新老用户", "CASE WHEN nu = 1 THEN 'new' ELSE 'old' END"),
    "ent": ("入口", "CASE WHEN tb = 1 AND st = 1 THEN 'both' WHEN tb = 1 THEN 'tb' ELSE 'site' END"),
    "qt": ("搜索词类型", "CASE WHEN kt + kh + kp = 0 THEN 'none' WHEN kt >= kh AND kt >= kp THEN 'type' "
                      "WHEN kh >= kp THEN 'history' ELSE 'hot' END"),
    "region": ("国家", "rg"),
}
SEG_LABELS = {"new": "新用户", "old": "老用户", "both": "两个入口都用", "tb": "仅 Toolbar", "site": "仅站内",
              "none": "无中间页点词", "type": "手输为主", "history": "历史词为主", "hot": "热词为主", "na": "未知"}
# 一次查询交叉的维度组（国家取值多，单独一组，免得超过 KG 单次 1000 行）
SEG_RUNS = (("nu", "ent", "qt"), ("region",))
# 国家只单列设备数最多的前 N 个，其余并成「其他」：SG 机房有 200+ 个国家，× 组数会超过 KG 单次 1000 行
SEG_TOP_REGIONS = 12
SEG_LABELS["other"] = "其他国家"


def _seg_dev_sql(market, d0, d1, regions):
    # 组号已由 _dev_sql 认定；这里只取设备属性，不要求事件带 exp_id（结果页约 2/3 的 exp_id 为空，10-20 实测）
    det = "event_name = 'search_detail_expose'"
    clk = lambda v: "SUM(CASE WHEN event_name = 'search_page_click' AND click = '%s' THEN 1 ELSE 0 END)" % v
    return f"""
      SELECT instance_id, 1 AS hit, MAX(CASE WHEN {det} THEN region END) AS rg,
             MAX(CASE WHEN {det} AND new_user = 1 THEN 1 ELSE 0 END) AS nu,
             MAX(CASE WHEN {det} AND `from` = 'toolbar' THEN 1 ELSE 0 END) AS tb,
             MAX(CASE WHEN {det} AND COALESCE(`from`, '') <> 'toolbar' THEN 1 ELSE 0 END) AS st,
             {clk("type_search")} AS kt, {clk("history")} AS kh, {clk("popular")} AS kp
      FROM {T}
      WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)}
        AND (({det}) OR event_name = 'search_page_click')
      GROUP BY instance_id"""


def experiment_seg_sql(market, gids, d0, d1, metrics, caps, regions, dims):
    """分群充分统计量：每组 × 分群取值一行（只出聚合）。dims = SEG_DIMS 的键；多个维度交叉分组，页面按维度求和边缘化
    （充分统计量可加）。窗口内结果页没有带组号事件的设备（只出现在播放事件里）归「未知」。"""
    expr = dict((d, SEG_DIMS[d][1]) for d in dims)
    top = ""
    if "region" in dims:
        # 前 N 个国家按本次窗口内设备数排；DENSE_RANK 在聚合后的国家粒度上算，输出列血缘不含 instance_id
        top = f"""
  LEFT JOIN (
    SELECT rg AS top_rg, DENSE_RANK() OVER (ORDER BY cnt DESC, rg) AS rk
    FROM (SELECT rg, COUNT(*) AS cnt FROM ({_seg_dev_sql(market, d0, d1, regions)}
    ) s0 WHERE rg IS NOT NULL GROUP BY rg) rc
  ) tr ON s.rg = tr.top_rg AND tr.rk <= {SEG_TOP_REGIONS}"""
        expr["region"] = "CASE WHEN s.rg IS NULL THEN NULL WHEN tr.top_rg IS NULL THEN 'other' ELSE s.rg END"
    keys = ", ".join("CASE WHEN s.hit IS NULL THEN 'na' ELSE COALESCE(%s, 'na') END AS d_%s" % (expr[d], d)
                     for d in dims)
    gb = ", ".join("d_%s" % d for d in dims)
    # 输出列的血缘里不能有 instance_id（KG 合规按 L3 整条拒绝，10-27 实测）：中间层逐列列出、不用 p.*；
    # 「JOIN 不上」用 s.hit 判断，不用 s.instance_id IS NULL
    vals = ", ".join("p.xx_{k}, p.yy_{k}".format(k=m["key"]) for m in metrics)
    return f"""
SELECT grp, {gb}, COUNT(*) AS n, {", ".join(_stats_cols(metrics))}
FROM (
  SELECT p.grp, {vals}, {keys}
  FROM ({_dev_sql(market, gids, d0, d1, metrics, caps, regions, keep_id=True)}
  ) p
  LEFT JOIN ({_seg_dev_sql(market, d0, d1, regions)}
  ) s ON p.instance_id = s.instance_id{top}
) a
GROUP BY grp, {gb}"""


def experiment_daily_ratio_sql(market, gids, d0, d1, metrics, caps, regions=None):
    """By天：单位 = 设备-天，组号按当天认（当天跨组 → X 剔除），封顶与全窗口同一个单设备单天 P99。"""
    dd = _dd_sql(market, gids, d0, d1, metrics, caps, regions)
    grp, _ = _grp_case(gids, "f")
    adj = _adj(metrics, caps, "xd")
    return f"""
SELECT grp, date, COUNT(*) AS n, {", ".join(_stats_cols(metrics))}
FROM (
  SELECT grp, date, {", ".join(adj)}
  FROM (
    SELECT {grp} AS grp, dd.*
    FROM ({dd}
    ) dd
  ) g
  WHERE grp IS NOT NULL
) a
GROUP BY grp, date"""


def cap_sql(market, d0, d1, numerator, denominator=None, regions=None):
    """开窗前单设备单天取值的 P99：只看当天有分母事件的设备-天，未发生记 0（对齐回收文档 ① 的 dd WHERE de=1）。"""
    if denominator is None:
        cond, where = side_cond(numerator), "v > 0"
        dcol = ""
    else:
        cond = "(%s) OR (%s)" % (side_cond(numerator), side_cond(denominator))
        dcol, where = ", %s AS de" % side_expr(denominator), "de > 0"
    vcol = side_expr(numerator)
    src = _source(market, regions, d0, d1, _side_events(numerator, denominator), _uses_age(cond, vcol, dcol))
    return f"""
SELECT approx_percentile(v, 0.99) AS cap, COUNT(*) AS n_dev
FROM (
  SELECT instance_id, date, {vcol} AS v{dcol}
  FROM {src}
  WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)} AND ({cond})
  GROUP BY instance_id, date
) d
WHERE {where}"""


# ─────────────────────────── 推全观测 ───────────────────────────
# 同一批国家推全前后周均对比（口径照搬飞书《第一轮推全的收益观测（1010推全）》W6SKdILuqo2cvaxWzwEcRDXgnrc）。
# 事件与过滤条件直接取预置指标 ctr_ui / click_3s / vv_per_user 的定义（presets.py），不另写一套。
# 2026-10-23 对文档逐日校准过：holdback 只认结果页四个事件、只认本机房的 holdback expId，命中的设备当周整周剔除；
# 点击→3s 的分母是全部点卡设备（不要求当天有结果页加载），点击率 / 人均 VV 的分子只算当天有结果页加载的设备。

_RESULT_EVENTS = "event_name IN ('search_detail_expose', 'search_card_expose', 'search_card_click', 'search_no_results')"


def _rollout_conds():
    from .presets import PRESETS
    P = dict(PRESETS)
    return {"r": side_cond(P["ctr_ui"]["denominator"]), "k": side_cond(P["ctr_ui"]["numerator"]),
            "v": side_cond(P["click_3s"]["numerator"]), "vv": side_cond(P["vv_per_user"]["numerator"])}


def _rollout_unit_case(units):
    """观测单元 → 'u<下标>'。units[i] = {market, op: in / not_in, regions}；not_in 空列表 = 整机房。"""
    whens = []
    for i, u in enumerate(units):
        m = u["market"]
        if m not in C.MARKETS:
            raise MetricError("未知市场：%s" % m)
        bad = [r for r in u["regions"] if r not in C.REGIONS.get(m, [])]
        if bad:
            raise MetricError("%s 市场不支持的国家：%s" % (m, ",".join(map(str, bad))))
        if u["op"] not in ("in", "not_in") or (u["op"] == "in" and not u["regions"]):
            raise MetricError("观测单元国家条件只支持 in（非空）/ not_in")
        cond = "cluster = %d" % C.MARKETS[m]["cluster"]
        if u["regions"]:
            cond += " AND region %s (%s)" % ("IN" if u["op"] == "in" else "NOT IN", ", ".join("'%s'" % r for r in sorted(u["regions"])))
        whens.append("WHEN %s THEN 'u%d'" % (cond, i))
    return "CASE %s END" % " ".join(whens)


def _rollout_dd(units, d0, d1, holdback):
    """设备-天层：r / k / v = 当天有结果页加载 / 点卡（排兜底）/ 3s 播放；vv = 当天搜索来源 VV 次数；
    h = 当天结果页事件带本机房 holdback expId（holdback = {market: expId}）。"""
    c = _rollout_conds()
    hb = []
    for m in sorted({u["market"] for u in units}):
        if m in holdback:
            if not _GID.match(str(holdback[m])):
                raise MetricError("holdback expId 应为纯数字：%r" % holdback[m])
            hb.append("(cluster = %d AND exp_id LIKE '%%\"exp_id\":\"%s\"%%')" % (C.MARKETS[m]["cluster"], holdback[m]))
    h = "%s AND (%s)" % (_RESULT_EVENTS, " OR ".join(hb)) if hb else "1 = 0"
    unit = _rollout_unit_case(units)
    clusters = ", ".join(str(x) for x in sorted({C.MARKETS[u["market"]]["cluster"] for u in units}))
    return f"""
      SELECT date, instance_id, {unit} AS unit,
        MAX(CASE WHEN {c["r"]} THEN 1 ELSE 0 END) AS r,
        MAX(CASE WHEN {c["k"]} THEN 1 ELSE 0 END) AS k,
        MAX(CASE WHEN {c["v"]} THEN 1 ELSE 0 END) AS v,
        SUM(CASE WHEN {c["vv"]} THEN 1 ELSE 0 END) AS vv,
        MAX(CASE WHEN {h} THEN 1 ELSE 0 END) AS h
      FROM {T}
      WHERE date BETWEEN {d0} AND {d1} AND cluster IN ({clusters})
        AND ({_RESULT_EVENTS} OR (event_name IN ('play_start_ready', 'play_close') AND `from` IN {PLAY_FROM3}))
      GROUP BY date, instance_id, {unit}"""


def rollout_obs_sql(units, d0, d1, holdback):
    """推全观测「单元 × 日」聚合（d0~d1 = 一个周一~周日）。holdback 在设备-天层打标，按设备取窗口内 MAX：
    当周任一天带 holdback 的设备整周剔除。人均 VV 按单元封顶值 units[i]["cap"]（同一轮两周同一个值）。
    出：expose 结果页加载设备 / click 其中点卡（排兜底）的设备 / c3_den 点卡设备 / c3_num 其中有 3s 播放的 / vv_cap 封顶后 VV 合计。"""
    caps = " ".join("WHEN 'u%d' THEN %s" % (i, float(u["cap"])) for i, u in enumerate(units))
    return f"""
SELECT date, unit,
  SUM(r) AS expose, SUM(r * k) AS click, SUM(k) AS c3_den, SUM(k * v) AS c3_num,
  SUM(r * LEAST(vv, cap)) AS vv_cap
FROM (
  SELECT date, unit, r, k, v, vv, CASE unit {caps} END AS cap
  FROM (
    SELECT date, unit, r, k, v, vv, MAX(h) OVER (PARTITION BY instance_id) AS hw
    FROM ({_rollout_dd(units, d0, d1, holdback)}
    ) d
    WHERE unit IS NOT NULL
  ) w
  WHERE hw = 0
) x
GROUP BY date, unit"""


def rollout_cap_sql(units, d0, d1, holdback):
    """推全观测人均 VV 封顶：d0~d1（基线周开始前 7 天）各单元单设备单天 VV 的 P99，只看当天有结果页加载的设备-天，
    未播放按 0；窗口内带过 holdback 的设备剔除（同取数口径）。每单元一行。"""
    return f"""
SELECT unit, approx_percentile(vv, 0.99) AS cap, COUNT(*) AS n_dev
FROM (
  SELECT unit, r, vv, MAX(h) OVER (PARTITION BY instance_id) AS hw
  FROM ({_rollout_dd(units, d0, d1, holdback)}
  ) d
  WHERE unit IS NOT NULL
) w
WHERE r = 1 AND hw = 0
GROUP BY unit"""


def experiment_quantile_sql(market, gids, d0, d1, metric, regions=None, daily=False):
    """分位数：每条事件按自身 exp_id 认组（对齐回收文档 ③）；同一事件带多个组号的剔除。"""
    d = metric["def"]
    col = NUM_COLS[d["column"]]
    cond = side_cond(d["source"])
    src = _source(market, regions, d0, d1, _side_events(d["source"]), _uses_age(col, cond))
    qcols = ", ".join("approx_percentile(v, %s) AS q%d" % (float(q), i) for i, q in enumerate(d["quantiles"]))
    gflags = ", ".join("%s AS g%d" % (_grp_flag(g), i) for i, g in enumerate(gids))
    grp, _ = _grp_case(gids, "g")
    dc = "date, " if daily else ""
    return f"""
SELECT grp, {dc}COUNT(*) AS n_events, COUNT(DISTINCT instance_id) AS n_dev, {qcols}
FROM (
  SELECT instance_id, {dc}v, {grp} AS grp
  FROM (
    SELECT instance_id, date, {col} AS v, {gflags}
    FROM {src}
    WHERE date BETWEEN {d0} AND {d1} AND {_scope(market, regions)} AND ({cond})
  ) ev
) t
WHERE grp IS NOT NULL
GROUP BY grp{", date" if daily else ""}"""
