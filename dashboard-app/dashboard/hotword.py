"""热词运营：运营配置（飞书表）× 词级消费聚合表 × DWD 设备去重 CTR。

北极星（UV，设备-天去重，用户 2026-10-21 定）：
- 中间页热词 CTR = 点热词（search_page_click, click='popular'）设备 ÷ 中间页曝光设备
- 结果页 CTR = 热词搜索（exp_id 带 "source":5）卡片点击设备 ÷ 卡片曝光设备，排兜底
  （不用 search_detail_expose 做分母：它早于 exp_id 写入上报，只约 40% 带 source）
其余（搜索占比 / 3s 转化率 / 无结果率 / 位置分档 / 词级）都是观测指标，次数口径，
来自词级消费聚合表（source='hot' = 中间页热词卡渠道）。
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess

from . import config as C, store
from .kg_client import KG, KGError

CONSUME = os.environ.get("CONSUME_TABLE", "catalog.db.query_consume_table")
MARKETS = ("RU", "ID", "TR", "BR")
START = 20300101          # 示例起点：表刚上线那几天数据还在回填，偏低
# 热词运营配置：运营手动维护的批次表，唯一配置源（示例占位，用环境变量配置表 token）
BOOK = os.environ.get("HOTWORD_BOOK_TOKEN", "")
TABS = {"tab1": ("sheet1", "A1:I2000")}
MANUAL = "tab1"
SHEET_URL = ("https://feishu.example.com/wiki?sheet=%s" % MANUAL) if BOOK else ""
FEISHU = os.environ.get("FEISHU_BIN", "feishu")
# 多机房混载：不同地区按 cluster + region 取数（示例，按自己的数仓改写）
DWD_SCOPE = os.environ.get("DWD_SCOPE", "1 = 1")
S5 = """exp_id LIKE '%"source":5%'"""
S0 = """exp_id LIKE '%"source":0%'"""
NUMS = ("search", "search_dev", "play", "p3", "play_dev", "nr", "ce", "cc", "de")
TIERS = (("1-3", "1–3 首屏", 1, 3), ("4-9", "4–9 换一批", 4, 9), ("10+", "10+", 10, 999))
# 结论规则阈值（页面上原样展示）
RULE = {"tail_share": 0.05, "low_eff": 0.7, "low_eff_min": 200, "cold_daily": 10, "cold_days": 3, "residual": 100,
        "gap": 0.10}


def toiso(n):
    s = str(n)
    return "%s-%s-%s" % (s[:4], s[4:6], s[6:])


def ymd(d):
    return int(d.strftime("%Y%m%d"))


def parse(d):
    return dt.datetime.strptime(str(d), "%Y%m%d").date()


def days(d0, d1):
    d, out = parse(d0), []
    while ymd(d) <= d1:
        out.append(ymd(d))
        d += dt.timedelta(days=1)
    return out


def chunks(d0, d1, n):
    ds = days(d0, d1)
    return [(ds[i], ds[min(i + n, len(ds)) - 1]) for i in range(0, len(ds), n)]


# ─────────────────────────── SQL（只出聚合） ───────────────────────────

def uv_sql(d0, d1):
    nf = "COALESCE(`from`, '') <> '%s'" % C.FALLBACK_FROM
    dev = lambda cond: "COUNT(DISTINCT CASE WHEN %s THEN instance_id END)" % cond
    return f"""
SELECT date, region,
  {dev("event_name = 'search_page_expose'")} AS mid,
  {dev("event_name = 'search_page_click' AND click = 'popular'")} AS hot,
  {dev("event_name = 'search_page_click' AND click = 'hotword_shuffle'")} AS shuffle,
  {dev("event_name = 'search_card_expose' AND %s AND %s" % (S5, nf))} AS ce5,
  {dev("event_name = 'search_card_click' AND %s AND %s" % (S5, nf))} AS cc5,
  {dev("event_name = 'search_card_expose' AND %s AND %s" % (S0, nf))} AS ce0,
  {dev("event_name = 'search_card_click' AND %s AND %s" % (S0, nf))} AS cc0
FROM {C.TABLE}
WHERE date BETWEEN {int(d0)} AND {int(d1)} AND {DWD_SCOPE}
  AND event_name IN ('search_page_expose', 'search_page_click', 'search_card_expose', 'search_card_click')
GROUP BY date, region"""


_SUMS = """SUM(search_cnt) AS search, SUM(play_cnt) AS play, SUM(play_3s_cnt) AS p3, SUM(noresult_cnt) AS nr,
  SUM(card_expose_cnt) AS ce, SUM(card_click_cnt) AS cc, SUM(detail_expose_cnt) AS de"""
_MK = "region IN (%s)" % ", ".join("'%s'" % m for m in MARKETS)


def total_sql(d0, d1):
    return f"""
SELECT date, region, source, {_SUMS}
FROM {CONSUME}
WHERE date BETWEEN {int(d0)} AND {int(d1)} AND {_MK}
GROUP BY date, region, source"""


def word_sql(d0, d1):
    # 设备数行内去重、跨行不可加总：只在「词 × 天」明细里用
    return f"""
SELECT date, region, query_word, {_SUMS},
  SUM(search_device_cnt) AS search_dev, SUM(play_device_cnt) AS play_dev
FROM {CONSUME}
WHERE date BETWEEN {int(d0)} AND {int(d1)} AND {_MK} AND source = 'hot'
GROUP BY date, region, query_word"""


# ─────────────────────────── 运营配置 ───────────────────────────

_MD = re.compile(r"^\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?\s*$")
_YMD = re.compile(r"^\s*(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})\s*$")


def _cell(c):
    if isinstance(c, list):      # 富文本单元格
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
    if isinstance(c, dict):
        return c.get("text") or ""
    return c


def _date(c, year):
    """'10月21日' / '2026-10-21' / Excel 序列号 → (date, 是否带年份)。"""
    c = _cell(c)
    if c is None or c == "":
        return None, False
    if isinstance(c, (int, float)) and c > 40000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(c)), True
    s = str(c)
    m = _YMD.match(s)
    if m:
        return dt.date(*map(int, m.groups())), True
    m = _MD.match(s)
    if m:
        return dt.date(year, int(m.group(1)), int(m.group(2))), False
    return None, False


def parse_config(values, year=2026):
    """上线情况表 → [{market, word, key, zh, cat, weight, on, off, batch}]。
    列：上线日 / 下线日 / 国家 / 原词 / 爬取 / 中文 / 垂类 / 含义 / 权重。只写月日时按表内顺序推年份（跨年 +1）。
    下线日空 = 仍在线。匹配键 = 原词 strip + lower（与聚合表 query_word 清洗一致，整串相等）。"""
    out, last_on = [], None
    for r in (values or [])[1:]:
        r = [_cell(c) for c in list(r) + [None] * 9]
        word = str(r[3] or "").strip()
        mk = str(r[2] or "").strip().upper()
        if not word or mk not in MARKETS:
            continue
        on, explicit = _date(r[0], year)
        if not on:
            continue
        if not explicit and last_on and on < last_on - dt.timedelta(days=180):
            year += 1
            on = on.replace(year=year)
        last_on = on
        off, explicit = _date(r[1], on.year)
        if off and not explicit and off < on:
            off = off.replace(year=off.year + 1)
        try:
            w = int(float(_cell(r[8])))
        except (TypeError, ValueError):
            w = None
        out.append({"market": mk, "word": word, "key": word.lower(), "zh": str(r[5] or "").strip(),
                    "cat": str(r[6] or "").strip(), "weight": w, "on": ymd(on), "off": ymd(off) if off else None,
                    "batch": on.strftime("%m%d") + "-" + (off.strftime("%m%d") if off else "")})
    return out


def _skipped_manual(values):
    """上线情况表里没被采用的行和原因（核验用）。"""
    out = []
    for i, r in enumerate((values or [])[1:], start=2):
        r = [_cell(c) for c in list(r) + [None] * 9]
        if not any(str(x or "").strip() for x in r[:9]) or str(r[4] or "").strip() == "0" and not str(r[3] or "").strip():
            continue
        word, mk = str(r[3] or "").strip(), str(r[2] or "").strip().upper()
        on, _ = _date(r[0], 2026)
        why = "缺原词" if not word else "国家「%s」不在四国" % (r[2] or "") if mk not in MARKETS else "上线日「%s」认不出" % (r[0] or "") if not on else None
        if not why and _cell(r[8]) not in (None, ""):
            try:
                int(float(_cell(r[8])))
            except (TypeError, ValueError):
                why = "权重「%s」不是数字（词照常回收，只是不进位置分档）" % r[8]
        if why:
            out.append({"row": i, "why": why, "word": word})
    return out


def config_rows(vals):
    """快照 → 人工批次词。兼容旧快照（只存了上线情况一张的 list 格式）。"""
    if isinstance(vals, list):
        vals = {MANUAL: vals}
    vals = vals or {}
    return parse_config(vals.get(MANUAL)) if vals.get(MANUAL) else []


def verify(old_vals, new_vals):
    """同步后核验：飞书配置表 ↔ 看板实际采用的配置 ↔ 数仓里的词。返回给页面直接展示的检查项列表。"""
    man = config_rows(new_vals)
    oman = config_rows(old_vals) if old_vals else []
    checks = []
    add = lambda lv, t, items=(): checks.append({"lv": lv, "t": t, "items": list(items)[:30], "n": len(list(items))})
    vals = new_vals if isinstance(new_vals, dict) else {MANUAL: new_vals}
    for tab, rows, skipped in ((MANUAL, man, _skipped_manual(vals.get(MANUAL))),):
        name = TABS[tab][0]
        bs = sorted({(r["batch"], r["market"]) for r in rows})
        add("ok", "「%s」读到 %d 个词、%d 个批次×市场" % (name, len(rows), len(bs)))
        if skipped:
            add("warn", "「%s」有 %d 行没采用" % (name, len(skipped)), ["第 %d 行 %s：%s" % (x["row"], x["word"] or "（空）", x["why"]) for x in skipped])
    # 和上次快照比：新增 / 删除 / 改了日期或权重
    if old_vals:
        sig = lambda r: (r["market"], r["key"], r["on"])
        o = {sig(r): r for r in oman}
        n = {sig(r): r for r in man}
        fmt = lambda r: "%s·%s「%s」" % (r["market"], r["batch"], r["word"])
        added = [fmt(r) for k, r in n.items() if k not in o]
        gone = [fmt(r) for k, r in o.items() if k not in n]
        chg = [fmt(r) + "：" + "，".join("%s %s→%s" % (lab, o[k][f], r[f]) for f, lab in (("off", "下线日"), ("weight", "权重"), ("word", "原词")) if o[k][f] != r[f])
               for k, r in n.items() if k in o and any(o[k][f] != r[f] for f in ("off", "weight", "word"))]
        add("info" if added or gone or chg else "ok", "比上次同步：新增 %d、删除 %d、改动 %d" % (len(added), len(gone), len(chg)),
            ["新增 " + x for x in added] + ["删除 " + x for x in gone] + ["改动 " + x for x in chg])
    # 同一张表里同市场同词在线窗口重叠（换批续配属正常，列出来供确认）
    dup = []
    for rows in (man,):
        by = {}
        for r in rows:
            by.setdefault((r["market"], r["key"]), []).append(r)
        for (m, k), rs in by.items():
            rs = sorted(rs, key=lambda r: r["on"])
            for a, b in zip(rs, rs[1:]):
                if b["on"] <= (a["off"] or 99999999):
                    dup.append("%s「%s」%s 与 %s 重叠 %s" % (m, a["word"], a["batch"], b["batch"], "（换批日当天两批各算一次）" if b["on"] == a["off"] else ""))
    if dup:
        add("info", "同一个词在线窗口重叠 %d 处" % len(dup), dup)
    # 在线窗口和数仓有重叠、但热词卡渠道一次搜索都没有：多半是写法和用户实际搜的不一样，或没真正下发
    last = max(store.hw_dates() or {0})
    pre = sorted("%s·%s「%s」" % (r["market"], r["batch"], r["word"]) for r in man if (r["off"] or 99999999) < START)
    part = sorted({"%s·%s" % (r["market"], r["batch"]) for r in man if r["on"] < START <= (r["off"] or 99999999)})
    seen = store.hw_word_keys()
    miss, near = [], []
    norm = lambda k: re.sub(r"[\W_]+", "", k)
    nk = {}
    for (m, k) in seen:
        nk.setdefault((m, norm(k)), set()).add(k)
    for r in man:
        lo, hi = max(r["on"], START), min(r["off"] or 99999999, last)
        if lo > hi or any(lo <= d <= hi for d in seen.get((r["market"], r["key"]), ())):
            continue
        cand = sorted(nk.get((r["market"], norm(r["key"])), set()) - {r["key"]})
        (near if cand else miss).append("%s·%s「%s」" % (r["market"], r["batch"], r["word"]) + ("，数仓里是「%s」" % "」「".join(cand[:3]) if cand else ""))
    if near:
        add("warn", "%d 个词和数仓里的写法不一致（大小写 / 空格 / 符号），看板匹配不上" % len(near), near)
    if miss:
        add("warn", "%d 个词在线期内热词卡 0 搜索：确认词写对了、确实下发了" % len(miss), miss)
    if not near and not miss and last:
        add("ok", "在线期内的配置词都在数仓里找到了搜索")
    if part:
        add("info", "%d 个批次上线早于 %s，只算 %s 起的部分" % (len(part), toiso(START), toiso(START)), part)
    if pre:
        add("info", "%d 个词整段在线期都早于 %s（聚合表那时还在填），不回收" % (len(pre), toiso(START)), pre)
    return checks


def sync_config(log=print):
    """读飞书配置表存快照；失败沿用上次快照，错误记下来给页面提示。"""
    exe = FEISHU if os.path.exists(FEISHU) else shutil.which("feishu")
    try:
        if not exe:
            raise RuntimeError("本机没有 feishu 命令行工具")
        vals = {}
        for tab, (name, rng) in TABS.items():
            p = subprocess.run([exe, "sheet", "read", BOOK, "%s!%s" % (tab, rng)], capture_output=True, text=True, timeout=120)
            if p.returncode:
                raise RuntimeError("读「%s」失败：%s" % (name, (p.stderr or p.stdout).strip()[-300:] or "退出码 %d" % p.returncode))
            vals[tab] = json.loads(p.stdout[p.stdout.index("{"):]).get("values") or []
        man = config_rows(vals)
        if not man:
            raise RuntimeError("「上线情况」读到了，但没解析出任何热词（表头或列顺序变了？）")
    except Exception as ex:
        store.save_hw_config(None, str(ex)[:300])
        log("⚠ 运营配置同步失败，沿用上次快照：%s" % ex)
        return False
    old = store.hw_config().get("values")
    report = verify(old, vals)
    store.save_hw_config(vals, None, report)
    log("运营配置：%d 个词；核验 %d 项需要看" % (len(man), sum(c["lv"] == "warn" for c in report)))
    return True


# ─────────────────────────── 跑数 ───────────────────────────

def _words(kg, d0, d1):
    try:
        rows = kg.dicts(word_sql(d0, d1), "热词词级 %d~%d" % (d0, d1))
    except KGError as ex:
        if "截断" in str(ex) and d1 > d0:           # >1000 行：对半拆
            ds = days(d0, d1)
            return _words(kg, d0, ds[len(ds) // 2 - 1]) + _words(kg, ds[len(ds) // 2], d1)
        raise
    return rows


def run(jid, p):
    log = lambda s: store.job_log(jid, s)
    d0, d1 = max(int(p["start"]), START), int(p["end"])
    sync_config(log)
    if d1 < d0:
        return
    with KG(log, trace=lambda q: store.job_query(jid, q)) as kg:
        for a, b in chunks(d0, d1, 7):
            store.save_hw_uv(kg.dicts(uv_sql(a, b), "热词 CTR（设备去重）%d~%d" % (a, b)))
        for a, b in chunks(d0, d1, 31):
            store.save_hw_total(a, b, kg.dicts(total_sql(a, b), "热词 / 主动搜索汇总 %d~%d" % (a, b)))
        for a, b in chunks(d0, d1, 4):
            store.save_hw_words(a, b, _words(kg, a, b))
    # 数仓数据更新后重算核验（保留同步时那条「比上次同步」）
    cfg = store.hw_config()
    if cfg.get("values"):
        keep = [c for c in cfg.get("report") or [] if c["t"].startswith("比上次同步")]
        store.save_hw_config(cfg["values"], None, verify(None, cfg["values"]) + keep, keep_time=True)


def schedule(submit, yesterday):
    """日更：补齐 START 以来缺的日期，并重拉最近 3 天（聚合表 T+2 滚动、播放迟到）。"""
    have = store.hw_dates()
    missing = [d for d in days(START, yesterday) if d not in have]
    s = min(missing + [ymd(parse(yesterday) - dt.timedelta(days=2))])
    return submit("hotword", {"start": s, "end": yesterday})


# ─────────────────────────── 读数 ───────────────────────────

def _div(a, b):
    return a / b if b else None


def _add(acc, x, keys):
    for k in keys:
        acc[k] = acc.get(k, 0) + int(x.get(k) or 0)
    return acc


UVK = ("mid", "hot", "shuffle", "ce5", "cc5", "ce0", "cc0")


def _north(u):
    return {"mid_ctr": _div(u.get("hot"), u.get("mid")), "res_ctr": _div(u.get("cc5"), u.get("ce5")),
            "act_ctr": _div(u.get("cc0"), u.get("ce0")), "shuffle_rate": _div(u.get("shuffle"), u.get("mid"))}


def _obs(h, a):
    return {"eff_hot": _div(h.get("p3"), h.get("search")), "eff_act": _div(a.get("p3"), a.get("search")),
            "ctr_hot": _div(h.get("cc"), h.get("ce")), "ctr_act": _div(a.get("cc"), a.get("ce")),
            "nr_hot": _div(h.get("nr"), h.get("search")), "nr_act": _div(a.get("nr"), a.get("search")),
            "share": _div(h.get("search"), (h.get("search") or 0) + (a.get("search") or 0))}


def _rates(x):
    return {"eff": _div(x.get("p3"), x.get("search")), "nr_rate": _div(x.get("nr"), x.get("search")),
            "ctr": _div(x.get("cc"), x.get("ce"))}


def _pct(v, d=1):
    return "—" if v is None else "%.*f%%" % (d, v * 100)


def _rel(a, b):
    return None if a is None or not b else a / b - 1


def _word_rows(cfg_rows, mks, ds, wd, last):
    """配置词 × 在线窗口 → (词级汇总, 词级逐日)。窗口外的搜索不算；下线后残留 = 下线后、且不在本表其他批次在线期内的搜索。"""
    online = {}
    for b in cfg_rows:
        for d in ds:
            if b["on"] <= d <= (b["off"] or 99999999):
                online.setdefault((d, b["market"], b["key"]), []).append(b)
    words, daily = [], []
    for b in [b for b in cfg_rows if b["market"] in mks]:
        acc, n = {}, 0
        for d in ds:
            if b["on"] <= d <= (b["off"] or 99999999) and (d, b["market"], b["key"]) in wd:
                _add(acc, wd[(d, b["market"], b["key"])], NUMS)
                n += 1
        live = [d for d in ds if b["on"] <= d <= (b["off"] or 99999999) and (not last or d <= last)]
        if not live:
            continue
        resid = sum(int(wd[(d, b["market"], b["key"])].get("search") or 0) for d in ds
                    if b["off"] and d > b["off"] and (d, b["market"], b["key"]) in wd
                    and (d, b["market"], b["key"]) not in online)
        words.append(dict({k: acc.get(k, 0) for k in NUMS if k not in ("search_dev", "play_dev")}, **_rates(acc),
                          batch=b["batch"], market=b["market"], weight=b["weight"], word=b["word"], zh=b["zh"],
                          cat=b["cat"], on=b["on"], off=b["off"], live_days=len(live), data_days=n,
                          daily_search=_div(acc.get("search", 0), len(live)), residual=resid, flags=[]))
    for (d, m, k), r in sorted(wd.items()):
        if m in mks and (d, m, k) in online:
            for b in online[(d, m, k)]:
                daily.append(dict({x: int(r.get(x) or 0) for x in NUMS}, **_rates(r), date=d, market=m,
                                  batch=b["batch"], weight=b["weight"], word=b["word"], zh=b["zh"]))
    return words, daily


def parse_markets(market):
    """'ALL' / 'RU' / 'RU,ID' → 按 MARKETS 顺序的国家列表。"""
    if not market or market == "ALL":
        return list(MARKETS)
    ms = [x.strip().upper() for x in str(market).split(",") if x.strip()]
    bad = [x for x in ms if x not in MARKETS]
    if bad or not ms:
        raise ValueError("未知市场：%s" % ",".join(bad or [str(market)]))
    return [m for m in MARKETS if m in ms]


def _top_slots(batches, m, ds, last, wd, uv):
    """单市场首屏 1–3 位逐日：位置 = 运营表权重。换批日同一位置有新旧两个词，数字合并、词都列出。"""
    out = {}
    for pos in (1, 2, 3):
        rows = []
        for d in ds:
            if last and d > last:
                rows.append(None)
                continue
            bs = [b for b in batches if b["market"] == m and b["weight"] == pos and b["on"] <= d <= (b["off"] or 99999999)]
            keys = list(dict.fromkeys(b["key"] for b in bs))
            if not keys:
                rows.append(None)
                continue
            acc = {}
            for k in keys:
                _add(acc, wd.get((d, m, k)) or {}, ("search", "search_dev", "p3", "cc", "ce"))
            mid = int((uv.get((d, m)) or {}).get("mid") or 0)
            rows.append({"words": [b["word"] for b in {b["key"]: b for b in bs}.values()], "dev": acc["search_dev"],
                         "search": acc["search"], "p3": acc["p3"], "mid_rate": _div(acc["search_dev"], mid),
                         "ctr": _div(acc["cc"], acc["ce"])})
        out[pos] = rows
    return out


def view(d0, d1, market="ALL"):
    d0, d1 = max(int(d0), START), int(d1)
    mks = parse_markets(market)
    cfg = store.hw_config()
    batches = config_rows(cfg.get("values"))
    ds = days(d0, d1) if d1 >= d0 else []
    uv, tot, wd = store.hw_uv(d0, d1), store.hw_total(d0, d1), store.hw_words(d0, d1)
    data_days = sorted({d for d, m, s in tot if s == "hot"})
    last = data_days[-1] if data_days else None

    # 北极星（UV）：窗口内设备-天加总再相除
    north, series = {}, {}
    for m in mks + (["ALL"] if len(mks) > 1 else []):
        ms = mks if m == "ALL" else [m]
        acc = {}
        for d in ds:
            for x in ms:
                if (d, x) in uv:
                    _add(acc, uv[(d, x)], UVK)
        nd = len([d for d in ds if any((d, x) in uv for x in ms)])
        north[m] = dict(_north(acc), mid_uv=_div(acc.get("mid"), nd), hot_uv=_div(acc.get("hot"), nd),
                        shuffle_uv=_div(acc.get("shuffle"), nd), days=nd)
        series[m] = []
        for d in ds:
            a, t = {}, {}
            for x in ms:
                if (d, x) in uv:
                    _add(a, uv[(d, x)], UVK)
                if (d, x, "hot") in tot:
                    _add(t, tot[(d, x, "hot")], ("search", "p3"))
            # 消费量：点热词设备（DWD，与北极星同源）+ 热词搜索次数 / 带来的 3s 有效播放（聚合表）
            series[m].append(dict(_north(a), hot_uv=a.get("hot"), mid_uv=a.get("mid"),
                                  search=t.get("search"), p3=t.get("p3")) if a or t else None)

    # 观测：热词渠道 vs 主动搜索（聚合表 source=hot / search_page）
    obs, daily = {}, []
    for m in mks + (["ALL"] if len(mks) > 1 else []):
        ms = mks if m == "ALL" else [m]
        h, a = {}, {}
        for (d, x, s), r in tot.items():
            if x in ms:
                _add(h if s == "hot" else a, r, NUMS)
        nd = len([d for d in data_days if any((d, x, "hot") in tot for x in ms)])
        obs[m] = dict(_obs(h, a), hot_search=h.get("search", 0), daily_search=_div(h.get("search"), nd), days=nd)
    for d in ds:
        for m in mks:
            r = tot.get((d, m, "hot"))
            if r:
                daily.append(dict({k: int(r.get(k) or 0) for k in ("search", "play", "p3", "nr", "ce", "cc", "de")},
                                  date=d, market=m, **_rates(r)))

    # 配置词 × 在线窗口：上线日 ≤ date ≤ 下线日（换批日新旧两批各自计数）
    words, word_daily = _word_rows(batches, mks, ds, wd, last)
    # 位置分档：4–9 只有点了「换一批」的设备看得到，可见设备分母按档分开
    tiers = []
    total_search = sum(w["search"] for w in words) or 0
    for key, name, lo, hi in TIERS:
        ws = [w for w in words if w["weight"] is not None and lo <= w["weight"] <= hi]
        if not ws:
            continue
        acc, wdays, vis = {}, 0, 0
        for w in ws:
            _add(acc, w, ("search", "p3", "cc", "ce"))
            for d in ds:
                if w["on"] <= d <= (w["off"] or 99999999) and (not last or d <= last):
                    wdays += 1
                    u = uv.get((d, w["market"]))
                    if u:
                        vis += int(u.get("mid" if key == "1-3" else "shuffle") or 0) if key != "10+" else 0
        tiers.append({"key": key, "name": name, "words": len(ws), "search": acc.get("search", 0),
                      "share": _div(acc.get("search", 0), total_search), "eff": _div(acc.get("p3"), acc.get("search")),
                      "ctr": _div(acc.get("cc"), acc.get("ce")), "per_word_day": _div(acc.get("search", 0), wdays),
                      "per_k_visible": _div(acc.get("search", 0) * 1000, vis) if vis else None,
                      "visible": {"1-3": "中间页曝光设备", "4-9": "点「换一批」设备"}.get(key, "连点多次「换一批」（无设备数，不算）")})

    top = _top_slots(batches, mks[0], ds, last, wd, uv) if len(mks) == 1 else None

    # 问题词
    for w in words:
        act = obs.get(w["market"], {}).get("eff_act")
        if w["search"] >= RULE["low_eff_min"] and act and w["eff"] is not None and w["eff"] < act * RULE["low_eff"]:
            w["flags"].append("3s 转化低")
        if w["live_days"] >= RULE["cold_days"] and (w["daily_search"] or 0) < RULE["cold_daily"]:
            w["flags"].append("几乎没人搜")
        if w["residual"] >= RULE["residual"]:
            w["flags"].append("下线后仍在搜")
    words.sort(key=lambda w: (-w["on"], w["market"], w["weight"] if w["weight"] is not None else 99))

    return {"start": d0, "end": d1, "market": ",".join(mks) if len(mks) < len(MARKETS) else "ALL", "markets": mks,
            "days": ds, "last": last, "config": _cfg_summary(cfg, batches),
            "north": north, "series": series, "top": top, "obs": obs, "daily": daily, "tiers": tiers, "words": words,
            "word_daily": word_daily, "rule": RULE, "conclusions": conclusions(mks, north, obs, tiers, words)}


def _cfg_summary(cfg, batches):
    """给页面看的配置快照 + 同步后的核验结果。"""
    latest = max((b["on"] for b in batches), default=None)
    cur = [b for b in batches if b["on"] == latest]
    return {"fetched_at": cfg.get("fetched_at"), "error": cfg.get("error"), "error_at": cfg.get("error_at"),
            "rows": len(batches), "n_batches": len({b["batch"] for b in batches}),
            "tabs": [{"name": TABS[t][0], "url": SHEET_URL % t} for t in TABS],
            "latest": {"batch": cur[0]["batch"] if cur else None,
                       "by_market": {m: sum(1 for b in cur if b["market"] == m) for m in MARKETS}},
            "report": cfg.get("report"), "report_at": cfg.get("report_at")}


def conclusions(mks, north, obs, tiers, words):
    """按规则出结论和下一步建议；每条带它依据的数，阈值见 RULE。"""
    out = []
    g = RULE["gap"]
    for m in mks:
        n, o = north.get(m) or {}, obs.get(m) or {}
        if n.get("res_ctr") is None or o.get("eff_hot") is None:
            continue
        r_ctr, r_eff = _rel(n["res_ctr"], n.get("act_ctr")), _rel(o["eff_hot"], o.get("eff_act"))
        facts = "结果页 CTR %s（主动搜索 %s），3s 转化率 %s（主动搜索 %s）" % (
            _pct(n["res_ctr"]), _pct(n.get("act_ctr")), _pct(o["eff_hot"]), _pct(o.get("eff_act")))
        if r_ctr is not None and r_eff is not None and r_ctr >= -g and r_eff >= -g:
            out.append({"lv": "good", "m": m, "t": "热词流量质量不低于主动搜索，值得继续配", "d": facts})
        elif r_ctr is not None and r_eff is not None and r_ctr < -g and r_eff < -g:
            out.append({"lv": "bad", "m": m, "t": "热词流量质量低于主动搜索，先查选词和内容供给", "d": facts})
        else:
            out.append({"lv": "mid", "m": m, "t": "热词点击和消费一高一低，看下方问题词", "d": facts})
    t = {x["key"]: x for x in tiers}
    if "10+" in t and t["10+"]["share"] is not None and t["10+"]["share"] < RULE["tail_share"]:
        out.append({"lv": "info", "t": "10 位以后的词搜索占比只有 %s，每批配 9 个就够" % _pct(t["10+"]["share"]),
                    "d": "10+ 位要连点多次「换一批」才看得到"})
    if "1-3" in t and "4-9" in t and t["1-3"]["per_k_visible"] and t["4-9"]["per_k_visible"]:
        a, b = t["1-3"]["per_k_visible"], t["4-9"]["per_k_visible"]
        out.append({"lv": "info", "t": "4–9 位单词每千可见设备搜索 %.1f 次，首屏 %.1f 次" % (b, a),
                    "d": "4–9 位只有点「换一批」的设备看得到；位置效应和词本身拆不开（前排本来就放最热的词）"})
    for flag, tip in (("3s 转化低", "多是点进来了但结果不好看，建议查内容供给或换词"), ("几乎没人搜", "建议换词"), ("下线后仍在搜", "可考虑延长在线")):
        ws = [w for w in words if flag in w["flags"]]
        if ws:
            out.append({"lv": "bad" if flag != "下线后仍在搜" else "info",
                        "t": "%d 个词%s，%s" % (len(ws), flag, tip),
                        "d": "、".join("%s·%s「%s」" % (w["market"], w["batch"], w["word"]) for w in ws[:8]) + (" 等" if len(ws) > 8 else "")})
    return out
