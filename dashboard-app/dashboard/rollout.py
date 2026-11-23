"""推全观测：同一批不在试点里的国家，推全前后周均对比（AA）。

口径照搬飞书《第一轮推全的收益观测（1010推全）》（docx W6SKdILuqo2cvaxWzwEcRDXgnrc）：
- 周一至周日周均；周均 = 7 天日值的平均；Δ = 周均差；t = 7 天对 7 天两样本 t，|t| ≥ 2.18（双侧 95%，自由度 12）为显著；
- 剔除当周带 holdback expId 的设备（结果页四个事件带本机房的 holdback 号，命中一天整周剔除）；
- 指标同预置 ctr_ui / click_3s / vv_per_user（SQL 在 sqlgen.rollout_obs_sql）；人均 VV 单设备单日封顶，同一轮两周同一个封顶值。
基线规则（用户 2026-10-23 拍板）：
- 每一轮：基线 = 上一轮推全后的第一个完整周，观测周 = 本轮推全后的第一个完整周；第一轮的「上一轮」= 第一轮上线前最后 7 天（见 week_before）；
- 全局累计：第一轮上线前那周 vs 最近一个已有完整数据的周；
- 观测单元只用不在试点里的国家：机房国家 − 基线周在跑的试点国家 − 观测周在跑的试点国家（第一轮照文档 5 个单元写死）；
- 封顶：第一轮照文档写死（10-03~10-09 算）；其余 = 基线周开始前 7 天，按单元算一次后入库。
轮次与推全日来自 observe.merged_rounds(expsync.snapshot_doc())，试点国家来自 expsync 解析出的各轮 regions。
"""
import datetime as dt
import statistics

from . import jobs, sqlgen, stats, store
from . import config as C

T_CRIT = 2.18
KEYS = ("ctr_ui", "click_3s", "vv_per_user")
# 规划文档「常驻holdback」段（expsync 不解析这一段）：各机房 holdback 组 expId（示例值）
HOLDBACK = {"SG": "100011", "RU": "100012", "EU": "100013"}
MLABEL = {"SG": "SGP", "RU": "RU", "EU": "EU"}
MORDER = ("SG", "EU", "RU")
EU5 = ("DE", "ES", "FR", "IT", "PL")
CAP_DAYS = 7
GLOBAL_BACK = 4           # 全局累计：最近一周还没取齐时，最多往前找几周已有完整数据的
CAVEAT = "前后对比会混入同期其他变化（如起播耗时大降），只用来看方向，不等于推全净收益。"
RU_MIX = ("RU 推全后为 b_ctrl（推全态）与 b_exp（推全态 + λ_click=0.5）混合：两组结果页曝光事件 expId 上报率只有 34% 至 42%，"
          "只留带 b_ctrl 标的设备会把没出卡片的设备筛掉，点击率虚高到 32%，所以不按组号筛、整个 RU 一起看。")


def _unit(key, label, market, op, regions, cap=None, note=""):
    return {"key": key, "label": label, "market": market, "op": op, "regions": sorted(regions), "cap": cap, "note": note}


# 第一轮：照文档 5 个单元与封顶值（10-03~10-09 单设备单日 P99）写死
HAND = {1: [
    _unit("BR", "SGP · BR", "SG", "in", ["BR"], 79.0),
    _unit("TR", "SGP · TR", "SG", "in", ["TR"], 70.0),
    _unit("SGO", "SGP · 其余国家", "SG", "not_in", ["BR", "ID", "TR"], 71.0),
    _unit("EUO", "EU · 五国以外", "EU", "not_in", list(EU5), 212.0),
    _unit("RU", "RU", "RU", "in", ["RU"], 294.0, RU_MIX),
]}
# 试点范围：代码里写死的轮（第一轮在另一份文档，全机房上线）；[] = 整机房
HAND_PILOT = {1: {"SG": [], "RU": [], "EU": []}}


# ─────────────────────────── 周推算 ───────────────────────────

def _ymd(d):
    return jobs.ymd(d)


def week_after(d):
    """d 之后第一个完整周（周一 ~ 周日）。d 当天不算（推全当天是新旧混合），所以 d 是周一时取下一周。"""
    x = jobs.parse(d)
    mon = x + dt.timedelta(days=7 - x.weekday())
    return _ymd(mon), _ymd(mon + dt.timedelta(days=6))


def week_before(d):
    """上线前基线 = 上线日前 7 天（d−7 ~ d−1），不对齐周一。
    对齐文档：第一轮 09-22（周二）上线，基线 09-15 ~ 09-21 是周二 ~ 周一（文档写的「周一至周日」只对推全后那周成立）；
    按周一 ~ 周日取会变成 09-14 ~ 09-20，和文档对不上。7 天仍各含一个周几，周内节律抵消。"""
    x = jobs.parse(d)
    return _ymd(x - dt.timedelta(days=7)), _ymd(x - dt.timedelta(days=1))


def last_full_week(y):
    """周日 ≤ y 的最近一个完整周。"""
    x = jobs.parse(y)
    sun = x - dt.timedelta(days=(x.weekday() + 1) % 7)
    return _ymd(sun - dt.timedelta(days=6)), _ymd(sun)


def shift(w, k):
    a = jobs.parse(w[0]) + dt.timedelta(days=7 * k)
    return _ymd(a), _ymd(a + dt.timedelta(days=6))


def cap_window(base):
    """封顶窗口 = 基线周开始前 7 天。"""
    a = jobs.parse(base[0])
    return _ymd(a - dt.timedelta(days=CAP_DAYS)), _ymd(a - dt.timedelta(days=1))


def _md(d):
    s = str(d)
    return "%s-%s" % (s[4:6], s[6:])


# ─────────────────────────── 试点与单元 ───────────────────────────

def pilots(rounds, doc):
    """各轮试点：[{no, start, end, regions: {market: 国家列表（[] = 整机房）/ None = 文档没写}}]。
    在跑区间 = [上线日, 推全日]（推全当天算在跑）；没推全 = 一直在跑。"""
    units = {(u["no"], u["market"]): u["regions"] for u in (doc or {}).get("units") or []}
    out = []
    for r in rounds:
        if not r.get("launch"):
            continue
        hand = HAND_PILOT.get(r["no"])
        regs = {m: (hand[m] if hand and m in hand else units.get((r["no"], m))) for m in MORDER}
        out.append({"no": r["no"], "start": int(r["launch"]), "end": int(r["rollout"]) if r.get("rollout") else None, "regions": regs})
    return out


def _active(pil, w):
    return [p for p in pil if p["start"] <= w[1] and (p["end"] is None or p["end"] >= w[0])]


def _cn(no):
    from .expsync import _CN
    return "第%s轮" % (_CN[no - 1] if no <= 9 else no)


def rule_units(pil, weeks):
    """机房国家 − weeks 里任一周在跑的试点国家。返回单元列表（不可比的 op = 'na'，带 reason）。"""
    act = [p for w in weeks for p in _active(pil, w)]
    out = []
    for m in MORDER:
        ex, whole, unknown = set(), False, []
        for p in act:
            rg = p["regions"].get(m)
            if rg is None:
                unknown.append(p["no"])
                whole = True
            elif not rg:
                whole = True
            else:
                ex |= set(rg)
        fixed = [C.MARKETS[m]["region"]] if C.MARKETS[m]["region"] else None
        left = sorted(set(fixed) - ex) if fixed else None
        if whole or (fixed is not None and not left):
            why = "%s 整体在试点中，无可比单元" % MLABEL[m] if fixed else "%s 整机房在试点中，无可比单元" % MLABEL[m]
            if unknown:
                why += "（%s %s 的试点范围文档里没写，按整机房算）" % ("、".join(_cn(n) for n in sorted(set(unknown))), MLABEL[m])
            out.append(_unit(m, MLABEL[m], m, "na", [], note=why))
        elif fixed:
            out.append(_unit(m, MLABEL[m] if left == fixed else "%s · %s" % (MLABEL[m], "、".join(left)), m, "in", left))
        else:
            ex = sorted(ex)
            label = ("%s · 整机房" % MLABEL[m] if not ex else "%s · 五国以外" % MLABEL[m] if m == "EU" and ex == list(EU5)
                     else "%s · 其余国家（排除 %s）" % (MLABEL[m], "、".join(ex)))
            out.append(_unit(m, label, m, "not_in", ex))
    return out


def mix_whole(units, pil, weeks):
    """全局累计（用户 2026-10-23 定）：整国都在试点里的单国家机房（RU）不标无可比，照第一轮文档整国一起看，注明混入了哪几轮实验组。"""
    out = []
    for u in units:
        reg = C.MARKETS[u["market"]]["region"]
        if u["op"] == "na" and reg:
            nos = sorted({p["no"] for w in weeks for p in _active(pil, w)
                          if p["regions"].get(u["market"]) is None or not p["regions"][u["market"]] or reg in p["regions"][u["market"]]})
            u = _unit(u["key"], u["label"], u["market"], "in", [reg],
                      note="%s 整国都在%s试点里：对照组（推全态）与实验组混合一起看，涨幅含实验组策略的贡献，不全是已推全策略的收益。"
                           % (MLABEL[u["market"]], "、".join(_cn(n) for n in nos)))
        out.append(u)
    return out


def sig(u):
    """单元口径签名：机房 + 国家条件（不含封顶）。"""
    return "%s|%s|%s" % (u["market"], u["op"], "+".join(u["regions"]))


def rolled_markets(doc, no):
    """本轮真正推全的机房（文档「推全进度表 By 机房」，删除线 / 回退不算）。
    推全观测只比本轮推全过的机房：没推全的机房整段没有策略变化，比出来的是 A/A 噪声。"""
    from . import expsync
    out = set()
    for r in (doc or {}).get("rollout") or []:
        n, state = expsync._doc_state(r)
        if n == no and state == "推全":
            out.add(r["market"])
    return out


def data_key(u):
    """落库键：口径签名 + 封顶值（人均 VV 分子随封顶值变）。"""
    return "%s|cap=%g" % (sig(u), u["cap"])


def _with_caps(units, base):
    """规则单元按「签名 + 基线周前 7 天」从库里取封顶值；没算过的 cap = None。"""
    w0, _ = cap_window(base)
    caps = store.rollout_caps()
    for u in units:
        if u["op"] != "na" and u["cap"] is None:
            u["cap"] = caps.get((sig(u), w0))
    return units


# ─────────────────────────── 计划：每块要哪几周、哪些单元 ───────────────────────────

def _block(bid, title, base, obs, units, y, **kw):
    b = {"id": bid, "title": title, "base": list(base) if base else None, "obs": list(obs) if obs else None,
         "units": units, "state": "ok", "opens": None, "note": ""}
    b.update(kw)
    if obs and obs[1] > y:
        b.update(state="waiting", opens=jobs.ymd(jobs.parse(obs[1]) + dt.timedelta(days=1)))
    return b


def plan(rounds, doc, y):
    """各轮 + 全局累计的观测块（不含取数结果）。rounds = observe.merged_rounds(doc)。"""
    rs = sorted((r for r in rounds if r.get("launch")), key=lambda r: r["no"])
    pil = pilots(rs, doc)
    out = {"rounds": {}, "global": None}
    prev = None
    for r in rs:
        no = r["no"]
        if not r.get("rollout"):
            out["rounds"][no] = _block("r%d" % no, r.get("name") or _cn(no), None, None, [], y, state="not_rolled")
        else:
            if prev is None:
                base = week_before(r["launch"])
            else:
                base = week_after(prev["rollout"]) if prev.get("rollout") else None
            obs = week_after(r["rollout"])
            if base is None:
                out["rounds"][no] = _block("r%d" % no, r.get("name") or _cn(no), None, obs, [], y, state="no_base")
            else:
                units = [dict(u) for u in HAND[no]] if no in HAND else _with_caps(rule_units(pil, [base, obs]), base)
                rm = rolled_markets(doc, no)          # 只留本轮机房里真正推全的（EU 第三轮没有推全任何策略）
                if rm:
                    units = [u for u in units if u["market"] in rm]
                out["rounds"][no] = _block("r%d" % no, r.get("name") or _cn(no), base, obs, units, y,
                                           base_note=("%s上线前" % _cn(no)) if prev is None else "%s推全后" % _cn(prev["no"]),
                                           obs_note="%s推全后" % _cn(no))
        prev = r
    if rs:
        base = week_before(rs[0]["launch"])
        cands = []
        w = last_full_week(y)
        for k in range(GLOBAL_BACK):
            ww = shift(w, -k)
            if ww[0] <= base[1]:
                break
            cands.append(_block("global", "全局累计", base, ww, _with_caps(mix_whole(rule_units(pil, [base, ww]), pil, [base, ww]), base), y,
                                base_note="%s上线前" % _cn(rs[0]["no"]), obs_note="最近完整周"))
        out["global"] = cands[0] if cands else None
        out["global_cands"] = cands
    return out


def needs(blocks, y):
    """取数清单：(要算封顶的 [(块, 窗口, 单元)], 要取数的 [(块, 周, 单元)])。只看已过完的周；全局累计只取最近一周。"""
    caps, data = [], []
    have = {}
    for b in list(blocks["rounds"].values()) + ([blocks["global"]] if blocks.get("global") else []):
        if not b["base"] or not b["obs"]:
            continue
        units = [u for u in b["units"] if u["op"] != "na"]
        no_cap = [u for u in units if u["cap"] is None]
        if no_cap:
            caps.append((b, cap_window(b["base"]), no_cap))
        for w in (b["base"], b["obs"]):
            if w[1] > y:
                continue
            miss = []
            for u in units:
                if u["cap"] is None:
                    miss.append(u)          # 封顶算完再取
                    continue
                k = data_key(u)
                if k not in have:
                    have[k] = store.rollout_days(k)
                if any(d not in have[k] for d in jobs._days(*w)):
                    miss.append(u)
            if miss:
                data.append((b, w, miss))
    return caps, data


def _snapshot():
    from . import expsync, observe
    doc = expsync.snapshot_doc()
    return observe.merged_rounds(doc), doc


def pending(y=None):
    """日更调度用：有没有已过完的周缺数 / 缺封顶。"""
    y = y or jobs.yesterday()
    rounds, doc = _snapshot()
    c, d = needs(plan(rounds, doc, y), y)
    return bool(c or d)


# ─────────────────────────── 取数任务 ───────────────────────────

def _complete_days(rows):
    """同一单元一周里结果页加载设备数低于本周中位数 60% 的天视为分区没产出完整，不落库（下次日更再取）。"""
    med = statistics.median([r["expose"] for r in rows]) if rows else 0
    return [r for r in rows if r["expose"] and not (med and r["expose"] < 0.6 * med)]


def run(jid, p):
    log = lambda s: store.job_log(jid, s)
    from .kg_client import KG, Cancelled
    y = jobs.yesterday()
    rounds, doc = _snapshot()
    failed = []
    with KG(log, trace=lambda q: store.job_query(jid, q)) as kg:
        # 1) 封顶：每块一条查询（块内单元互不重叠）
        caps, _ = needs(plan(rounds, doc, y), y)
        for b, (w0, w1), units in caps:
            log("── %s 人均 VV 封顶 %d~%d：%s" % (b["title"], w0, w1, "、".join(u["label"] for u in units)))
            try:
                rows = kg.dicts(sqlgen.rollout_cap_sql(units, w0, w1, HOLDBACK), "%s 封顶" % b["title"])
            except Cancelled:
                raise
            except Exception as ex:
                failed.append("%s 封顶：%s" % (b["title"], str(ex)[:200]))
                log("⚠ %s 封顶失败，其余照常：%s" % (b["title"], ex))
                continue
            by = {r["unit"]: r for r in rows}
            for i, u in enumerate(units):
                r = by.get("u%d" % i)
                if not r or r["cap"] is None:
                    failed.append("%s %s 封顶窗口无数据" % (b["title"], u["label"]))
                    continue
                store.save_rollout_cap(sig(u), w0, w1, float(r["cap"]), int(r["n_dev"] or 0))
                log("封顶 %s = %s（%d~%d 单设备单天 P99，%s 设备-天）" % (u["label"], r["cap"], w0, w1, r["n_dev"]))
        # 2) 单元 × 日：每块每周一条查询；重新算清单（封顶刚入库；别的块可能已取过同一单元同一周）
        _, data = needs(plan(rounds, doc, y), y)
        for b, w, _ in data:
            units = [u for u in b["units"] if u["op"] != "na" and u["cap"] is not None
                     and any(d not in store.rollout_days(data_key(u)) for d in jobs._days(*w))]
            if not units:
                continue
            log("── %s %d~%d：%s" % (b["title"], w[0], w[1], "、".join(u["label"] for u in units)))
            try:
                rows = kg.dicts(sqlgen.rollout_obs_sql(units, w[0], w[1], HOLDBACK), "%s 推全观测 %d~%d" % (b["title"], w[0], w[1]))
            except Cancelled:
                raise
            except Exception as ex:
                failed.append("%s %d~%d：%s" % (b["title"], w[0], w[1], str(ex)[:200]))
                log("⚠ %s %d~%d 失败，其余照常：%s" % (b["title"], w[0], w[1], ex))
                continue
            for i, u in enumerate(units):
                ur = [{"date": int(r["date"]), **{c: float(r[c] or 0) for c in ("expose", "click", "c3_den", "c3_num", "vv_cap")}}
                      for r in rows if r["unit"] == "u%d" % i]
                ok = _complete_days(ur)
                if len(ok) < len(ur) or len(ur) < 7:
                    log("⚠ %s %d~%d 有 %d 天不完整 / 无数据，没落库，下次日更再取" % (u["label"], w[0], w[1], 7 - len(ok)))
                store.save_rollout(data_key(u), ok)
    if failed:
        raise RuntimeError("推全观测部分失败：" + "；".join(failed))
    log("推全观测取数完成")


# ─────────────────────────── 组装 ───────────────────────────

VALUE = {"ctr_ui": lambda r: r["click"] / r["expose"] if r["expose"] else None,
         "click_3s": lambda r: r["c3_num"] / r["c3_den"] if r["c3_den"] else None,
         "vv_per_user": lambda r: r["vv_cap"] / r["expose"] if r["expose"] else None}


def compare(bv, ov):
    """基线 7 天 vs 观测 7 天的日值：周均、Δ（周均差）、相对变化、t、显著（|t| ≥ 2.18，正向 up / 负向 down）。"""
    if len(bv) < 7 or len(ov) < 7 or any(v is None for v in bv + ov):
        return None
    diff, t = stats.two_sample_t(bv, ov)
    b, o = sum(bv) / len(bv), sum(ov) / len(ov)
    sigv = None
    if t is not None and abs(t) >= T_CRIT:
        sigv = "up" if diff > 0 else "down"
    return {"b": b, "o": o, "diff": diff, "rel": (o / b - 1) if b else None, "t": t, "sig": sigv}


def unit_result(u, base, obs):
    out = {k: u[k] for k in ("key", "label", "market", "note")}
    if u["op"] == "na":
        return dict(out, state="na")
    days = list(jobs._days(*base)) + list(jobs._days(*obs))
    if u["cap"] is None:
        return dict(out, state="pending", days=days)
    have = store.rollout_days(data_key(u))
    rows = [have.get(d) for d in days]
    out.update(state="ok" if all(rows) else "pending", days=days, cap=u["cap"],
               daily={k: [VALUE[k](r) if r else None for r in rows] for k in KEYS},
               expose=[sum(r["expose"] for r in part) / 7 if all(part) else None for part in (rows[:7], rows[7:])],
               metrics={k: compare([VALUE[k](r) for r in rows[:7] if r], [VALUE[k](r) for r in rows[7:] if r]) for k in KEYS})
    return out


def _result(b):
    if not b:
        return None
    out = {k: b.get(k) for k in ("id", "title", "base", "obs", "state", "opens", "note", "base_note", "obs_note")}
    out["units"] = [unit_result(u, b["base"], b["obs"]) if b["base"] and b["obs"] else
                    {k: u[k] for k in ("key", "label", "market", "note")} | {"state": "na" if u["op"] == "na" else "pending"}
                    for u in b["units"]]
    if out["state"] == "ok" and any(u["state"] == "pending" for u in out["units"]):
        out["state"] = "pending"
    return out


def view(rounds, doc, y=None):
    """总览用：{rounds: {轮号: 块}, global: 块, caveat}。全局累计取最近一个已有完整数据的周（最近一周没取齐就往前找）。"""
    y = y or jobs.yesterday()
    blocks = plan(rounds, doc, y)
    g = None
    cands = [_result(b) for b in blocks.get("global_cands") or []]
    for c in cands:
        if c["state"] == "ok":
            g = c
            break
    if g is None and cands:
        g = cands[0]
    elif g is not cands[0]:
        g["note"] = "最近一周（%s ~ %s）取数中，先显示已取齐的最近一周" % (_md(cands[0]["obs"][0]), _md(cands[0]["obs"][1]))
    return {"rounds": {no: _result(b) for no, b in blocks["rounds"].items()}, "global": g, "caveat": CAVEAT, "t_crit": T_CRIT}
