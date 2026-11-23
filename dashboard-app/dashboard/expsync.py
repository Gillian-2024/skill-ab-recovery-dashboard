"""实验配置同步：读规划文档《搜索结果排序实验规划 v2》→ 建看板缺的回收实验 + 核验（照热词「同步运营配置」的做法）。

用到文档三段：
- 「## 实验进度表」（现名「## 时间进度表」）：每轮 上线 / 满窗 / 判读 / 推全（MMDD，年份取离今天最近的那一年）；
- 「# 三、实验规划」下的「## 第N轮」万象分桶表（「## 常驻holdback」不放，用户定了）：每个「轮 × 机房」= 一个回收实验；
- 「## 推全进度表 By 机房」（HTML 表，rowspan；<del> = 已回退）：只拿来核对总览的决策标签。
规则（用户 2026-10-23 拍板）：
- 文档有、看板没有 → 自动建。开窗日 = 上线日 + 1，结束日 = 上线日 + 14；开窗日还没到的只建不跑，等每日调度有数后自动开跑；
- 文档和看板对不上（expId / 对照实验角色 / 国家范围 / 上线日）→ 只列差异不改库，页面上逐个点「按文档更新并重跑」；
  结束日不同不算差异（用户会主动提前收窗）；
- 决策标签不自动改：和文档推全进度表对不上的列进核验，页面上逐个点「按文档改」才写库（store.decision_override，优先于 observe.ROUNDS）；
  模糊匹配拿不准的不报。
读失败沿用上次快照，记错误给页面提示。
"""
import datetime as dt
import html.parser
import json
import os
import re
import shutil
import subprocess

from . import config as C, jobs, store

DOC = os.environ.get("PLAN_DOC_TOKEN", "")
DOC_URL = ("https://feishu.example.com/wiki/%s" % DOC) if DOC else ""
FEISHU = os.environ.get("FEISHU_BIN", "feishu")
MK = {"SGP": "SG", "SG": "SG", "RU": "RU", "EU": "EU"}          # 文档「机房」→ 看板 market
MLABEL = {"SG": "SGP", "RU": "RU", "EU": "EU"}
# 与看板现有排序实验同一套指标；By天 = 前五个
METRIC_KEYS = ["ctr_ui", "expose_conv", "click_3s", "vv_per_user", "noresult_rate", "completion", "avg_vdur"]
DAILY_KEYS = METRIC_KEYS[:5]
WINDOW = 14               # 结束日 = 上线日 + 14（第二轮 1010 上线 → 1024 满窗）
EXCL_FROM = 4             # 第四轮起读数排除热词（source 5，按事件排除）：规划文档「4️⃣」读数范围，2026-11-02 定
LABEL_MAX = 30            # 与 app._parse_exp 的组名长度一致
_CN = "一二三四五六七八九"


# ─────────────────────────── 解析 ───────────────────────────

def _clean(s):
    """去掉 markdown 加粗 / 反引号 / HTML 标签，<br/> 换空格，压空白。"""
    s = re.sub(r"<br\s*/?>", " ", str(s or ""))
    s = re.sub(r"<[^>]+>", "", s).replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


def _round_no(s):
    """「第三轮」→ 3（支持 1~19 与阿拉伯数字）。"""
    m = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*轮", s or "")
    if not m:
        return None
    t = m.group(1)
    if t.isdigit():
        return int(t)
    if t == "十":
        return 10
    if t.startswith("十") and len(t) == 2:
        return 10 + _CN.index(t[1]) + 1
    return _CN.index(t) + 1 if len(t) == 1 and t in _CN else None


def _mmdd(s, today):
    """'1023' → 20261023；年份取离今天最近的那一年（跨年时不会把 0105 算成去年）。"""
    m = re.fullmatch(r"(\d{1,2})[-/.]?(\d{2})", _clean(s))
    if not m:
        return None
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            d = dt.date(y, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return jobs.ymd(best)


def _sections(md):
    out, cur = [], None
    for line in (md or "").splitlines():
        m = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if m:
            cur = {"level": len(m.group(1)), "title": _clean(m.group(2)), "lines": []}
            out.append(cur)
        elif cur:
            cur["lines"].append(line)
    return out


def _md_table(lines):
    """段落里第一张管道表 → 行列表（含表头；去掉 |-|-| 分隔行；空行保留，分桶表靠它分隔机房）。"""
    rows = []
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if any(cells) and all(re.fullmatch(r":?-+:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


class _Html(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.cell = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th") and self.rows:
            self.cell = {"text": "", "del": False, "th": tag == "th", "span": int(dict(attrs).get("rowspan") or 1)}
            self.rows[-1].append(self.cell)
        elif tag == "del" and self.cell is not None:
            self.cell["del"] = True

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.cell = None

    def handle_data(self, d):
        if self.cell is not None:
            self.cell["text"] += d


def _html_grid(text):
    """HTML 表 → 展开 rowspan 后的行（每格 {text, del}），跳过表头行。"""
    p = _Html()
    p.feed(text)
    out, carry = [], {}
    for r in p.rows:
        if r and all(c["th"] for c in r):
            continue
        cells, line, col = list(r), [], 0
        while cells or any(k >= col for k in carry):
            if col in carry:
                c, n = carry.pop(col)
                if n > 1:
                    carry[col] = (c, n - 1)
            elif cells:
                c = cells.pop(0)
                if c["span"] > 1:
                    carry[col] = (c, c["span"] - 1)
            else:
                break
            line.append(c)
            col += 1
        out.append(line)
    return out


def parse_regions(text, market):
    """「仅 ID+BR+TR」→ [BR, ID, TR]；「仅 EU 五国（ES,FR,...）」→ 五国；「… 全部国家」→ []。返回 (国家, 是否认出)。"""
    t = _clean(text)
    if "全部" in t:
        return [], True
    codes = sorted({c for c in re.findall(r"(?<![A-Z])[A-Z]{2}(?![A-Z])", t) if c in C.REGIONS.get(market, [])})
    return codes, bool(codes)


def _label(ab, role):
    s = re.sub(r"\s*=\s*", "=", _clean(ab).split("⚠️")[0].strip())   # 单元格里 ⚠️ 起是给服务端的提示，不进组名
    if s in ("", "无", "—", "-"):
        return "对照" if role == "control" else "实验组"
    if len(s) > LABEL_MAX:            # 先去掉夹在中文里的英文标识（exp_duration），还长再截
        s = re.sub(r"\s+", " ", re.sub(r"(?<![=A-Za-z0-9_])[A-Za-z]+_[A-Za-z_]+(?![=A-Za-z0-9_])", " ", s)).strip()
    return s if len(s) <= LABEL_MAX else s[:LABEL_MAX - 1] + "…"


_PREFIX = ("站内播放质量加权", "站外互动率加权", "站外互动", "搜索点击加权")


def _short(label):
    """实验名用的短名：去参数（x=1.0）、去英文标识（exp_duration）、去大类前缀和结尾「加权」。
    '站内播放质量加权 完成度exp_duration λ_p=1.0' → 完成度；'站外互动三率等权：…' → 三率等权。"""
    s = label.rstrip("…").split("：")[0]
    par = re.findall(r"[^\s一-鿿]*=[^\s一-鿿]*", s)
    s = re.sub(r"[^\s一-鿿]*=[^\s一-鿿]*", " ", s)
    s = re.sub(r"[A-Za-z]+_[A-Za-z_]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for p in _PREFIX:
        if s.startswith(p):
            s = s[len(p):].strip() or (par[0] if par else s)      # 只剩大类名（搜索点击加权 λ=1）→ 用参数
            break
    else:
        if s.endswith("加权") and len(s) > 4:
            s = s[:-2].strip()
    return s or (par[0] if par else label[:12])


def _units(rows, no, rname):
    """一轮的分桶表 → 每个机房一个单元 {market, regions, groups[{id, role, label, eff}], problems}。"""
    head = [_clean(h) for h in rows[0]]
    find = lambda f: next((i for i, h in enumerate(head) if f(h)), None)
    ci = {"mk": find(lambda h: h == "机房"), "scope": find(lambda h: "范围" in h), "role": find(lambda h: h == "组别"),
          "ab": find(lambda h: h.replace(" ", "").upper() == "AB配置"), "eff": find(lambda h: "实际生效" in h),
          "eid": find(lambda h: h.lower() == "expid")}
    if None in ci.values():
        raise RuntimeError("「%s」分桶表表头认不出（缺 %s）" % (rname, "、".join(k for k, v in ci.items() if v is None)))
    out, cur = [], None
    cell = lambda r, k: r[ci[k]] if ci[k] < len(r) else ""
    for r in rows[1:]:
        if not any(_clean(c) for c in r):
            cur = None
            continue
        mk = MK.get(_clean(cell(r, "mk")).upper())
        if not mk:
            continue
        if not cur or cur["market"] != mk:
            regions, ok = parse_regions(cell(r, "scope"), mk)
            cur = {"no": no, "round": rname, "market": mk, "scope": re.sub(r"^仅\s*", "", _clean(cell(r, "scope"))),
                   "regions": regions, "groups": [], "problems": [] if ok else ["国家范围「%s」认不出" % _clean(cell(r, "scope"))]}
            out.append(cur)
        rl = _clean(cell(r, "role"))
        role = "control" if "对照" in rl else "treatment" if "实验" in rl else None
        m = re.search(r"\d{5,12}", _clean(cell(r, "eid")))
        if not role:
            cur["problems"].append("组别「%s」认不出对照 / 实验" % rl)
            continue
        # 中途换臂：同一 expId 旧配置那行标「⚠️ … 已关闭」，旧段在看板里单独冻结，这里只认新段
        if "已关闭" in _clean(cell(r, "ab")):
            cur["n_closed"] = cur.get("n_closed", 0) + 1
            continue
        cur["groups"].append({"id": m.group(0) if m else None, "role": role, "label": _label(cell(r, "ab"), role),
                              "eff": _clean(cell(r, "eff")).split("⚠️")[0].strip(), "scope": parse_regions(cell(r, "scope"), mk)[0]})
    out = [x for u in out for x in _split_scope(u)]
    for u in out:
        # 实验组都标了「已关闭」（中途换臂，新臂挪到下一轮的分桶表）：这一段已结束，不建、不报解析不了
        u["closed"] = bool(u.get("n_closed")) and not any(g["role"] == "treatment" for g in u["groups"])
        if u["closed"]:
            u["missing_id"] = []
            u["name"] = "%s %s" % (rname, MLABEL[u["market"]])
            continue
        ids = [g["id"] for g in u["groups"] if g["id"]]
        if sum(g["role"] == "control" for g in u["groups"]) != 1:
            u["problems"].append("对照组不是正好一个")
        if len(set(ids)) != len(ids):
            u["problems"].append("expId 有重复")
        if len(u["groups"]) < 2:
            u["problems"].append("不到两个组")
        u["missing_id"] = [g["label"] for g in u["groups"] if not g["id"]]
        u["name"] = "%s %s · %s" % (rname, MLABEL[u["market"]], " vs ".join(_short(g["label"]) for g in u["groups"] if g["role"] == "treatment"))
    return out


def _split_scope(u):
    """同机房实验组各自限定了更小的国家范围（第四轮 SGP：ID+BR 测完播率、TR 测 hashtag，共用一个对照）
    → 每个国家范围拆成一个单元，对照组复用，国家范围取实验组的。范围都一样就原样返回。"""
    trts = [g for g in u["groups"] if g["role"] == "treatment"]
    scopes = []
    for g in trts:
        sc = g.get("scope") or u["regions"]
        if sc not in scopes:
            scopes.append(sc)
    if not scopes or not all(set(sc) < set(u["regions"]) for sc in scopes):
        return [u]
    ctrl = [g for g in u["groups"] if g["role"] == "control"]
    return [dict(u, regions=sc, scope="/".join(sc), groups=ctrl + [g for g in trts if (g.get("scope") or u["regions"]) == sc],
                 problems=list(u["problems"])) for sc in scopes]


def parse_doc(md, today=None):
    """文档 markdown → {progress: {轮号: {...}}, units: [...], rollout: [...], rounds: [轮号...]}。"""
    today = today or dt.date.today()
    secs = _sections(md)
    progress, units, rollout, rounds = {}, [], [], []
    in_plan = False
    for s in secs:
        if s["level"] == 1:
            in_plan = "实验规划" in s["title"]
            continue
        if "实验进度表" in s["title"] or "时间进度表" in s["title"]:   # 文档 11 月起改名「时间进度表」
            rows = _md_table(s["lines"])
            if rows:
                head = [_clean(h) for h in rows[0]]
                idx = {}
                for i, h in enumerate(head):
                    for k, w in (("launch", "上线"), ("full", "满窗"), ("judge", "判读"), ("rollout", "推全"), ("note", "备注")):
                        if w in h and k not in idx:
                            idx[k] = i
                for r in rows[1:]:
                    no = _round_no(_clean(r[0]) if r else "")
                    if not no:
                        continue
                    get = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else ""
                    note = _clean(get("note"))
                    progress[no] = dict({k: _mmdd(get(k), today) for k in ("launch", "full", "judge", "rollout")},
                                        no=no, name=_clean(r[0]), note="" if note in ("-", "—") else note)
        elif "推全进度表" in s["title"]:
            for line in _html_grid("\n".join(s["lines"])):
                line = line + [{"text": "", "del": False}] * (3 - len(line))
                mk = MK.get(_clean(line[0]["text"]).upper())
                prog, strat = _clean(line[1]["text"]), _clean(line[2]["text"])
                if mk and (prog or strat):
                    rollout.append({"market": mk, "progress": prog, "strategy": strat,
                                    "deleted": line[1]["del"] or line[2]["del"]})
        elif in_plan and s["level"] == 2 and _round_no(s["title"]) and "holdback" not in s["title"].lower():
            rows = _md_table(s["lines"])
            if rows:
                no = _round_no(s["title"])
                rounds.append(no)
                units += _units(rows, no, "第%s轮" % (_CN[no - 1] if no <= 9 else no))
    for u in units:
        p = progress.get(u["no"]) or {}
        u["launch"] = _market_launch(p.get("note"), u["market"], today) or p.get("launch")
    return {"progress": progress, "units": units, "rollout": rollout, "rounds": rounds}


def _market_launch(note, market, today):
    """进度表的轮次按三机房整体记，上线日取最早的机房（用户 2026-11-10 定）；晚上线的机房写在备注里
    （如「EU 3s 率 w=2.0 于 1111 上线」）→ 该机房单元的上线日按备注。"""
    for seg in re.split(r"[；;]", note or ""):
        m = re.search(r"^\s*(SGP|SG|RU|EU)\b.*?于\s*(\d{4})\s*上线", seg.strip())
        if m and MK[m.group(1)] == market:
            return _mmdd(m.group(2), today)
    return None


def config_line(u):
    """对照组「实际生效配置」精简成一句：'ID+BR+TR；对照 = 第二轮推全态（publish_time + λ_click=1.0 + …）'。"""
    c = next((g for g in u["groups"] if g["role"] == "control"), None)
    if not c:
        return u["scope"]
    eff = c["eff"]
    base = re.search(r"（\s*=\s*([^）]+)）", eff)
    eff = re.sub(r"（[^）]*）", "", eff)
    eff = re.sub(r"时效\s*", "", eff)
    eff = re.sub(r"\s+", " ", eff).strip(" +")
    return "%s；对照 = %s" % (u["scope"], "%s（%s）" % (base.group(1).strip(), eff) if base else eff)


# ─────────────────────────── 看板对照 ───────────────────────────

def _launch(e):
    return int(e.get("launch") or e["start"])


def _ids(gs):
    return {g["id"] for g in gs}


def _regtxt(rs):
    return "/".join(rs) if rs else "全机房"


def _iso(d):
    s = str(d)
    return "%s-%s" % (s[4:6], s[6:])


def _plus(d, n):
    return jobs.ymd(jobs.parse(d) + dt.timedelta(days=n))


def diffs(u, e):
    """文档单元 vs 看板实验：expId / 角色 / 国家 / 上线日。结束日不比（会主动提前收窗）。"""
    out = []
    ub, bb = {g["id"]: g for g in u["groups"]}, {g["id"]: g for g in e["groups"]}
    if set(ub) - set(bb):
        out.append("文档有、看板没有的组：%s" % "、".join("%s（%s）" % (i, ub[i]["label"]) for i in sorted(set(ub) - set(bb))))
    if set(bb) - set(ub):
        out.append("看板有、文档没有的组：%s" % "、".join(sorted(set(bb) - set(ub))))
    role = {"control": "对照", "treatment": "实验"}
    for i in sorted(set(ub) & set(bb)):
        if ub[i]["role"] != bb[i]["role"]:
            out.append("%s 角色：文档是%s组，看板是%s组" % (i, role[ub[i]["role"]], role[bb[i]["role"]]))
    if sorted(e.get("regions") or []) != u["regions"]:
        out.append("国家范围：文档 %s，看板 %s" % (_regtxt(u["regions"]), _regtxt(sorted(e.get("regions") or []))))
    if _launch(e) != u["launch"]:
        out.append("上线日：文档 %s，看板 %s" % (_iso(u["launch"]), _iso(_launch(e))))
    return out


def fix_body(u, e):
    """「按文档更新并重跑」提交给 PUT /api/experiments/<id> 的表单：分组 / 国家 / 上线日按文档，其余沿用看板。"""
    old = {g["id"]: g for g in e["groups"]}
    launch = u["launch"]
    start = int(e["start"]) if launch == _launch(e) else _plus(launch, 1)
    end = int(e["end"]) if int(e["end"]) >= start else _plus(launch, WINDOW)
    return {"name": e["name"], "market": e["market"], "regions": u["regions"], "launch": launch, "start": start, "end": end,
            "groups": [{"id": g["id"], "role": g["role"], "label": (old.get(g["id"]) or {}).get("label") or g["label"]}
                       for g in u["groups"]],
            "metric_keys": e["metric_keys"], "daily_keys": e.get("daily_keys") or [], "excl_src": e.get("excl_src") or []}


def _same_round(e, u):
    """看板实验名带「第N轮」就必须和文档单元同一轮；名字里没写轮次的老实验（第二轮及以前手建）按上线日认。"""
    n = _round_no(e["name"])
    return n == u["no"] if n else _launch(e) == u["launch"]


def match(units, exps):
    """每个可建的单元 → 看板实验（或 None）。先认「机房 + 上线日 + expId 集合」全等；
    再认同机房同上线日、expId 有交集的（expId 改了）；再认 expId 全等、上线日不同的（上线日改了）。
    不同轮次可能复用同一 expId（如 100001 等）：expId 集合不同、上线日不同，不会被认成同一轮。"""
    out, used = {}, set()
    # 中途换臂关掉的旧段（名字带「已关闭」）与新段共用 expId 和上线日，只留着看历史读数，不参与配对
    exps = [e for e in exps if "已关闭" not in e["name"]]
    key = lambda u: (u["no"], u["market"], tuple(u["regions"]))
    # 只在同一轮里认：expId 跨轮复用（第四轮全部复用第三轮的号），不同轮必须是两个实验，绝不互认
    for u in units:
        e = next((e for e in exps if e["id"] not in used and _same_round(e, u) and e["market"] == u["market"] and _launch(e) == u["launch"]
                  and _ids(e["groups"]) == _ids(u["groups"])), None)
        if e:
            out[key(u)] = e
            used.add(e["id"])
    for u in units:
        if key(u) in out:
            continue
        cand = [(len(_ids(e["groups"]) & _ids(u["groups"])), -e["id"], e) for e in exps if e["id"] not in used and _same_round(e, u)
                and e["market"] == u["market"] and _launch(e) == u["launch"] and _ids(e["groups"]) & _ids(u["groups"])]
        if not cand:
            cand = [(0, -e["id"], e) for e in exps if e["id"] not in used and _same_round(e, u) and e["market"] == u["market"]
                    and sorted(e.get("regions") or []) == u["regions"]
                    and _ids(e["groups"]) == _ids(u["groups"])]
        if cand:
            e = max(cand, key=lambda x: x[:2])[2]
            out[key(u)] = e
            used.add(e["id"])
    return out


def create(u):
    """建一个回收实验。开窗日还没到 → 只建不跑（status=active、没跑过），等每日调度有数后开跑；能跑的直接提交。"""
    mm = store.metric_map()
    keys = [k for k in METRIC_KEYS if k in mm]
    start, end = _plus(u["launch"], 1), _plus(u["launch"], WINDOW)
    groups = [{"id": g["id"], "role": g["role"], "label": g["label"]} for g in u["groups"]]
    eid = store.create_experiment(u["name"], u["market"], groups, start, end, keys, u["regions"],
                                  [k for k in DAILY_KEYS if k in keys], u["launch"], ["5"] if u["no"] >= EXCL_FROM else [])
    return eid, jobs.kick(eid)


# ─────────────────────────── 决策标签核对 ───────────────────────────

def _n(s):
    s = _clean(s).lower()
    s = re.sub(r"λ_?[a-z]+", "λ", s)
    return re.sub(r"[\s\-—_：:·]+", "", s)


def _np(s):
    return re.sub(r"加权$", "", _n(s))


def _doc_state(r):
    m = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*轮\s*推全", r["progress"])
    no = _round_no(m.group(0)) if m else None
    if not no:
        return None, None
    return no, "已回退" if r["deleted"] or "回退" in r["progress"] else "推全"


def check_decisions(rollout, rounds, exps=(), ov=None):
    """文档推全进度表 ↔ 总览决策标签（ROUNDS 手写 / 自动归入的组，叠加库里改过的）。返回 (对不上, 没找到对应组, 可按文档改的)。
    匹配：策略名拆「大类 -- 参数」（或「大类 λ=…」），大类是实验名前缀（或组名里含大类）、参数等于组名（都去空格 / λ_x→λ / 结尾「加权」）；
    没有参数 = 该实验只有一个实验组。必须正好命中一个组（或一条「未单独开实验」备注）才核对，否则只列为未核对。"""
    from .observe import decision_groups
    bad, miss, fixes = [], [], []
    by = {r["no"]: r for r in rounds}
    groups = decision_groups(rounds, list(exps), ov or {})
    for r in rollout:
        no, want = _doc_state(r)
        if not no:
            continue                      # 第三轮ing 等：还没决策
        s = r["strategy"]
        fam, par = (s.split("--", 1) if "--" in s else
                    (lambda m: (m.group(1), m.group(2)) if m else (s, ""))(re.match(r"(.*?)\s*(λ.*)$", s)))
        gs = groups.get((no, r["market"])) or []
        cnt = {}
        for name, _, _ in gs:
            cnt[name] = cnt.get(name, 0) + 1
        # 参数先按原样比，没有再去掉结尾「加权」比（「三率加权」不能同时认成「三率」）
        # 候选组：实验名以大类开头 → 组名里含大类 → （有参数时）本轮本机房全部组，靠参数唯一命中
        cand = [(n, g) for n, g, _ in gs if _n(n).startswith(_n(fam))] or \
            [(n, g) for n, g, _ in gs if _np(fam) and _np(fam) in _n(g["label"] or "")] or \
            ([(n, g) for n, g, _ in gs] if _n(par) else [])
        lab = lambda g: _n(g["label"] or "")
        # 文档策略名和某个组名整体一致（「完播率加权 w=1.0」「端内 3s 率 w=2.0」这类写法）→ 直接认这一组，
        # 不受下面「没参数 = 该实验只有一个组」的限制
        exact = [(n, g) for n, g, _ in gs if _n(s) == _n(g["label"] or "")]
        hits = exact or [(n, g) for n, g in cand if (lab(g) == _n(par) if _n(par) else cnt[n] == 1)] or \
            [(n, g) for n, g in cand if _n(par) and _np(lab(g)) == _np(par)] or \
            [(n, g) for n, g in cand if _n(par) and _n(par) in lab(g)] or \
            [(n, g) for n, g in cand if _n(par) and _np(par) in lab(g)]
        hits = [("%s · %s" % (n, g["label"]), g["decision"], g["id"]) for n, g in hits]
        m = ((by.get(no) or {}).get("markets") or {}).get(r["market"]) or {}
        hits += [(n, "推全", None) for n in m.get("notes") or [] if _n(fam) in _n(n) and (_n(par) in _n(n))]
        src = "%s｜%s%s" % (r["progress"], r["strategy"], "（删除线）" if r["deleted"] else "")
        where = "%s·第%s轮" % (MLABEL[r["market"]], _CN[no - 1] if no <= 9 else no)
        if len(hits) != 1:
            miss.append("%s「%s」：%s" % (where, src, "看板里没找到对应的组" if not hits else "对应到多个组，拿不准"))
        elif hits[0][1] != want:
            bad.append("%s「%s」：看板标「%s」，文档推全进度表这一格是「%s」→ 按文档应为「%s」" % (where, hits[0][0], hits[0][1], src, want))
            if hits[0][2]:
                fixes.append({"no": no, "market": r["market"], "gid": hits[0][2], "name": "%s「%s」" % (where, hits[0][0]),
                              "now": hits[0][1], "want": want, "src": src})
    return bad, miss, fixes


# ─────────────────────────── 同步 ───────────────────────────

def _fetch():
    exe = FEISHU if os.path.exists(FEISHU) else shutil.which("feishu")
    if not exe:
        raise RuntimeError("本机没有 feishu 命令行工具")
    p = subprocess.run([exe, "fetch", DOC], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError("读规划文档失败：%s" % ((p.stderr or p.stdout).strip()[-300:] or "退出码 %d" % p.returncode))
    md = json.loads(p.stdout[p.stdout.index("{"):]).get("markdown") or ""
    if not md.strip():
        raise RuntimeError("规划文档读到了，但正文是空的")
    return md


def reconcile(doc, apply=True):
    """文档 ↔ 看板：建缺的实验（apply=False 只出报告不建），返回 (核验报告, 新建清单)。"""
    from .observe import merged_rounds
    checks, created = [], []
    add = lambda lv, t, items=(), **kw: checks.append(dict({"lv": lv, "t": t, "items": list(items)[:30], "n": len(list(items))}, **kw))
    closed = [u for u in doc["units"] if u.get("closed")]
    units = [u for u in doc["units"] if not u.get("closed")]
    add("ok", "读到 %d 轮分桶表（%s）、进度表 %d 轮、推全进度表 %d 行" % (
        len(doc["rounds"]), "、".join("第%s轮" % (_CN[n - 1] if n <= 9 else n) for n in doc["rounds"]) or "无",
        len(doc["progress"]), len(doc["rollout"])))
    bad_u = [u for u in units if u["problems"]]
    wait_id = [u for u in units if not u["problems"] and u["missing_id"]]
    no_launch = [u for u in units if not u["problems"] and not u["missing_id"] and not u["launch"]]
    ready = [u for u in units if not u["problems"] and not u["missing_id"] and u["launch"]]
    if closed:
        add("info", "%d 个轮 × 机房的实验组都已关闭（换臂到下一轮），不再核对" % len(closed), [u["name"] for u in closed])
    if bad_u:
        add("warn", "%d 个轮 × 机房的分桶表解析不了，没建" % len(bad_u), ["%s：%s" % (u["name"], "；".join(u["problems"])) for u in bad_u])
    if wait_id:
        add("info", "%d 个轮 × 机房待分桶号，暂不建（有组的 expId 是空或「？」）" % len(wait_id),
            ["%s：%s 还没有 expId" % (u["name"], "、".join(u["missing_id"])) for u in wait_id])
    if no_launch:
        add("warn", "%d 个轮 × 机房在进度表里没有上线日，暂不建" % len(no_launch), [u["name"] for u in no_launch])
    exps = store.experiments()
    hit = match(ready, exps)
    same, fixes = [], []
    y = jobs.yesterday()
    for u in ready:
        e = hit.get((u["no"], u["market"], tuple(u["regions"])))
        if not e:
            if apply:
                eid, jid = create(u)
                created.append({"id": eid, "name": u["name"], "job_id": jid, "start": _plus(u["launch"], 1)})
            else:
                created.append({"id": None, "name": u["name"], "job_id": None, "start": _plus(u["launch"], 1)})
            continue
        d = diffs(u, e)
        if d:
            fixes.append({"eid": e["id"], "name": e["name"], "doc": u["name"], "diffs": d, "body": fix_body(u, e)})
        else:
            same.append("%s ↔ 看板「%s」" % (u["name"], e["name"]))
    if created:
        add("info", "新建回收实验 %d 个" % len(created) if apply else "看板缺 %d 个回收实验" % len(created),
            ["%s：%s" % (c["name"], ("已提交跑数 #%d" % c["job_id"]) if c["job_id"] else
                         "等待开窗（%s 起有数，每日 %02d:%02d 调度有数后自动开跑）" % (_iso(c["start"]), *C.DAILY_AT)
                         if c["start"] > y else "待提交") for c in created])
    if same:
        add("ok", "%d 个已在看板，按机房 + 上线日 + expId 对上" % len(same), same)
    if fixes:
        add("warn", "%d 个看板实验和文档对不上（只提示，没改）：确认后点「按文档更新并重跑」" % len(fixes),
            ["%s：%s" % (f["name"], "；".join(f["diffs"])) for f in fixes], fixes=fixes)
    if not doc["rollout"]:
        add("warn", "没读到「推全进度表 By 机房」，决策标签没核对")
    else:
        bad, miss, dfix = check_decisions(doc["rollout"], merged_rounds(doc), store.experiments(), store.decision_overrides())
        if bad:
            add("warn", "%d 个决策标签和文档推全进度表对不上（只提示，没改）：确认后点「按文档改」" % len(bad), bad, dec_fixes=dfix)
        else:
            add("ok", "决策标签和文档推全进度表一致（已核对 %d 行）" % (sum(1 for r in doc["rollout"] if _doc_state(r)[0]) - len(miss)))
        if miss:
            add("info", "%d 行推全记录没核对：策略名对不到唯一的组，不按模糊匹配报" % len(miss), miss)
    no_tbl = sorted(n for n in doc["progress"] if n not in doc["rounds"])
    if no_tbl:
        add("info", "%s 在本文档没有分桶表（第一轮在另一份文档），不建表，只核对决策标签" %
            "、".join("第%s轮" % (_CN[n - 1] if n <= 9 else n) for n in no_tbl))
    return checks, created


def sync_config(log=print, fetch=None):
    """读规划文档 → 建缺的回收实验 → 存快照 + 核验。失败沿用上次快照，错误记下来给页面提示。"""
    try:
        md = (fetch or _fetch)()
        doc = parse_doc(md)
        if not doc["units"] and not doc["progress"]:
            raise RuntimeError("规划文档读到了，但没解析出进度表和分桶表（标题或表头变了？）")
    except Exception as ex:
        store.save_exp_config(None, str(ex)[:300])
        log("⚠ 实验配置同步失败，沿用上次快照：%s" % ex)
        return False
    report, created = reconcile(doc)
    store.save_exp_config(md, None, report)
    log("实验配置：新建 %d 个；核验 %d 项需要看" % (len(created), sum(c["lv"] == "warn" for c in report)))
    return True


def recheck():
    """改了决策标签后：用现有文档快照重新核对一遍（不读飞书、不建实验），只换核验结果。"""
    doc = snapshot_doc()
    if doc:
        store.save_exp_report(reconcile(doc, apply=False)[0])


def snapshot_doc():
    """上次同步成功的文档 → 解析结果（没有快照 / 解析失败 = None，总览退回纯代码配置）。"""
    md = store.exp_config().get("md")
    if not md:
        return None
    try:
        return parse_doc(md)
    except Exception:
        return None


def summary():
    c = store.exp_config()
    return {"fetched_at": c.get("fetched_at"), "error": c.get("error"), "error_at": c.get("error_at"),
            "report": c.get("report"), "report_at": c.get("report_at"), "doc_url": DOC_URL,
            "daily_at": "%02d:%02d" % C.DAILY_AT}
