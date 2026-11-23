"""实验观测：搜索结果排序实验按「轮次 × 机房」汇总（只读，不拉数）。

数据全部来自「实验回收」已算好的结果（store.experiment + jobs.experiment_view）。
本文件只放两样：规划文档《搜索结果排序实验规划 v2》的静态配置（轮次进度、决策标签），和把配置与回收结果拼起来的组装函数。
实验按 机房 + 上线日 + 实验组 expId 匹配，不按实验名（第三轮复用了第二轮的 expId，必须带上线日区分）。
"""
from . import jobs, store

METRICS = (("ctr_ui", "结果页点击渗透率", "北极星"),
           ("click_3s", "点击 → 3s 播放", "过程"),
           ("vv_per_user", "人均 VV", "过程"))
KEYS = [k for k, _, _ in METRICS]
MARKETS = (("SG", "SGP"), ("RU", "RU"), ("EU", "EU"))
DECISIONS = ("推全", "未采纳", "已回退", "已关闭", "进行中", "仅作对照")
NO_LIFT_HINT = "按现口径三项指标未测出显著提升"


def _x(name, ctrl, *groups):
    """一个实验：对照 expId + 实验组 (expId, 组名, 决策, 备注)。"""
    return {"name": name, "ctrl": ctrl, "groups": [dict(zip(("id", "label", "decision", "note"), g)) for g in groups]}


# 分段实验：同一轮同机房中途改了参照、数据不合并，按段各建一个回收实验。
# 每段按 机房 + 上线日 + expId 匹配看板实验；list 第一段是本轮主块，old = 旧版对照（总览灰显、决策标「仅作对照」，不算本轮决策）。
# head / detail 是实验详情页顶部提示，detail 后面接「另一段实验名」的链接。
SG3_SEGS = {"ctrl": "100001", "trts": (("100002", "treatment A"), ("100003", "treatment B")), "list": [
    {"launch": 20300101, "name": "Round 1 · example segment", "old": False, "tag": "", "note": "",
     "head": "", "detail": "示例数据"},
]}


ROUNDS = [
    {"no": 1, "name": "Round 1", "launch": 20300101, "full": None, "judge": None, "rollout": None, "note": "",
     "markets": {
         "SG": {"config": "example config", "plan": ["treatment A", "treatment B"], "segs": SG3_SEGS},
         "RU": {"config": "example config",
                "exps": [_x("example strategy", "100001", ("100002", "A", "推全", ""))]},
         "EU": {"config": "example config",
                "exps": [_x("example strategy", "100001", ("100003", "B", "未采纳", ""))]},
     }},
]


def _launch(e):
    return int(e.get("launch") or e["start"])


def find_exp(exps, market, launch, ctrl, trt_ids):
    """机房 + 上线日相同、对照组 = ctrl、实验组覆盖配置里的全部 expId。"""
    for e in exps:
        if e["market"] != market or _launch(e) != launch:
            continue
        ids = {g["id"] for g in e["groups"]}
        c = [g["id"] for g in e["groups"] if g["role"] == "control"]
        if c == [ctrl] and set(trt_ids) <= ids:
            return e
    return None


def _segs_iter():
    for r in ROUNDS:
        for mk, m in r["markets"].items():
            if m.get("segs"):
                yield mk, m["segs"]


def _seg_hit(exps, mk, segs, s):
    return find_exp(exps, mk, s["launch"], segs["ctrl"], [i for i, _ in segs["trts"]])


def segment_of(e, exps=None):
    """实验详情页顶部提示：e 是某个分段实验的一段 → {head, text, old, link: {id, name} | None}；否则 None。
    link = 另一段在看板里的实验（主段链旧段、旧段链主段），没建就只给文字。"""
    exps = store.experiments() if exps is None else exps
    for mk, segs in _segs_iter():
        hits = [(s, _seg_hit(exps, mk, segs, s)) for s in segs["list"]]
        me = next((s for s, h in hits if h and h["id"] == e["id"]), None)
        if not me:
            continue
        other = next(((s, h) for s, h in hits if s is not me), None)
        link = {"id": other[1]["id"], "name": other[1]["name"]} if other and other[1] else None
        return {"head": me["head"], "text": me["detail"], "old": me["old"], "link": link,
                "name": other[0]["name"] if other else ""}
    return None


def _num(v):
    return None if v is None else float(v)


def _metric_cell(t, gid, ctrl):
    """全窗口累计（type == total）行 → 对照 / 实验取值 + 差值 + 显著性；没有累计行返回 None（回收中）。"""
    r = next((r for r in t["rows"] if r["type"] == "total"), None)
    if not r or gid not in r["groups"] or ctrl not in r["groups"]:
        return None
    pick = lambda g: {k: _num(g.get(k)) for k in ("value", "num", "den")}
    out = {"c": pick(r["groups"][ctrl]), "t": pick(r["groups"][gid]), "diff": None, "rel": None, "z": None, "p": None, "sig": None}
    c = r["vs"].get(gid)
    if c:
        out.update(diff=c.get("diff"), rel=c.get("rel"), z=c.get("z"), p=c.get("p"))
        sig = (c.get("p") is not None and c["p"] < 0.05) or (c.get("z") is not None and abs(c["z"]) >= 1.96)
        if sig:
            out["sig"] = "up" if c.get("read") == "显著正向" else "down"
    return out


def _daily(t, gids, pending):
    rows = [r for r in t["rows"] if r["type"] == "day"]
    if not rows:
        return {"state": "pending" if pending else "none"}
    # 样本不足的天（jobs.thin_days）不画点，标在 thin 里给图注用
    return {"state": "ok", "days": [r["date"] for r in rows], "thin": [r["date"] for r in rows if r.get("thin")],
            "series": {g: [None if r.get("thin") else _num((r["groups"].get(g) or {}).get("value")) for r in rows] for g in gids}}


LT_GUARD = ("lt7", "r1", "r7")     # 留存护栏只看这三项；7 日人均 VV / 3sVV / 时长和人均 VV 重复，不放


def _lt_delta(m, c):
    """留存行的 Δ：LT7 写相对差「+1.2%」，次留 / 7 留写「+0.35pp」。"""
    return "%+.1f%%" % (c["rel"] * 100) if m["unit"] == "day" else "%+.2fpp" % (c["diff"] * 100)


def _retention(lt, gid, busy):
    """一个实验组的「留存」护栏，直接读 @lt 读数（jobs.lt_view），不取数。None = 回收中（补算中 / 失败 / 还没有 @lt）。
    state：immature 未成熟 / stop 判止损（同实验详情「⚠ 判止损」：留存显著变差且过序贯阈值）/ up 有显著变好 /
    down 显著变差但没过序贯阈值（不判止损）/ ns 三项都不显著。"""
    if busy or not lt:
        return None
    out = {"cut": lt.get("cut")}
    if lt.get("immature"):
        return dict(out, state="immature")
    ms = [m for m in lt["metrics"] if m["key"] in LT_GUARD]
    hit = [(m, m["vs"][gid]) for m in ms if gid in m["vs"]]
    if not hit:
        return None
    pick = lambda xs: [{"key": m["key"], "name": m["name"], "delta": _lt_delta(m, c)} for m, c in xs]
    stop = [(m, c) for m, c in hit if c.get("stop")]
    up = [(m, c) for m, c in hit if c.get("read") == "显著正向"]
    down = [(m, c) for m, c in hit if c.get("read") == "显著负向"]
    if stop:
        return dict(out, state="stop", items=pick(stop))
    if up:
        return dict(out, state="up", items=pick(up))
    if down:
        return dict(out, state="down", items=pick(down))
    m, c = next(((m, c) for m, c in hit if m["key"] == "lt7"), hit[0])
    base = (m["groups"].get(lt.get("base")) or {}).get("value")
    mde = c["mde_abs"] / base * 100 if base else None
    return dict(out, state="ns", items=pick([(m, c)]), mde="±%.1f%%" % mde if mde is not None else None)


def _primary_key(e, mm):
    """实验自己的主指标：metric_keys 里第一个「北极星」组指标，没配置就退回 ctr_ui。
    口径按 PRD：EU 3s 率的主指标是站内北极星（全设备会被 Toolbar 稀释），不能统一按 ctr_ui 读。"""
    for k in (e or {}).get("metric_keys") or []:
        if (mm.get(k) or {}).get("group") == "北极星":
            return k
    return "ctr_ui"


def _spec(e, mm):
    """总览每个实验块展示的指标：本实验北极星 + 点击→3s + 人均 VV（北极星口径随实验）。"""
    pk = _primary_key(e, mm)
    names = {k: n for k, n, _ in METRICS}
    out = []
    for k in [pk] + [k for k in KEYS if k != "ctr_ui"]:
        m = mm.get(k) or {}
        out.append({"key": k, "name": names.get(k) or m.get("name") or k,
                    "role": "北极星" if k == pk else "过程",
                    "unit": jobs._unit(m) if m else "pct"})
    return out


def exp_block(e, cfg_groups, cfg_name, ctrl_id):
    """一个实验块。e = None → 看板里还没建这个实验。cfg_groups = 实验组配置（含决策标签）。
    state：missing 没建 / waiting 已建、开窗日还没到（opens = 开窗日，等每日调度有数后开跑）/ pending 回收中 / ok。"""
    spec = _spec(e, store.metric_map())
    ks = [s["key"] for s in spec]
    out = {"name": cfg_name, "eid": None, "state": "missing", "ctrl": {"id": ctrl_id, "label": ""},
           "window": None, "groups": [], "daily": {}, "opens": None,
           "spec": spec, "primary": ks[0]}
    labels = {g["id"]: g.get("label") for g in (e or {}).get("groups", [])}
    if e:
        out.update(eid=e["id"], ctrl={"id": ctrl_id, "label": labels.get(ctrl_id) or ""},
                   window=[int(e["start"]), int(e["last_end"])] if e.get("last_end") else None)
        v = jobs.experiment_view(e) if e.get("last_end") else {}
        tables = {t["key"]: t for t in v.get("tables") or []}
        lt = v.get("lt")
        if lt:
            lt = dict(lt, base=v.get("base"))
    else:
        tables, lt = {}, None
    pend = set((e or {}).get("pending") or [])
    # @lt 补算中 / 上次失败 → 留存行和三格一样显示「回收中」
    lt_busy = "@lt" in pend or "长期价值" in ((e or {}).get("vis_error") or "")
    for g in cfg_groups:
        lab = labels.get(g["id"]) if labels.get(g["id"]) not in (None, g["id"]) else g.get("label")
        cell = {"id": g["id"], "label": lab or g["id"], "decision": g["decision"], "note": g.get("note") or "",
                "hint": "", "metrics": {}, "retention": _retention(lt, g["id"], lt_busy)}
        for k in ks:
            cell["metrics"][k] = _metric_cell(tables[k], g["id"], ctrl_id) if k in tables else None
        done = all(cell["metrics"][k] for k in ks)
        if g["decision"] == "推全" and done and not any(cell["metrics"][k]["sig"] == "up" for k in ks):
            cell["hint"] = NO_LIFT_HINT
        out["groups"].append(cell)
    if e:
        ready = any(cell["metrics"][k] for cell in out["groups"] for k in ks)
        out["state"] = "ok" if ready else "pending"
        if not ready and not e.get("last_end") and int(e["start"]) > jobs.yesterday():
            out.update(state="waiting", opens=int(e["start"]))
        gids = [ctrl_id] + [g["id"] for g in cfg_groups]
        for k in ks:
            if k in tables:
                out["daily"][k] = _daily(tables[k], gids, k + "@day" in pend)
            else:
                out["daily"][k] = {"state": "pending" if out["state"] == "pending" else "none"}
    return out


_RKEYS = ("launch", "full", "judge", "rollout", "note")


def _dec(ov, no, mk, gid, default):
    """决策标签：页面上「按文档改」写进库的优先，其次 ROUNDS 手写 / 默认值。"""
    return ov.get((no, mk, gid), default)


def _auto_exps(exps, mk, launch, used):
    """本轮同机房、上线日 = 本轮上线日、没被分段认走的实验 → [(实验, 对照 expId, 实验组)]，实验组决策默认「进行中」。"""
    out = []
    for e in sorted((e for e in exps if e["market"] == mk and _launch(e) == launch and e["id"] not in used), key=lambda e: e["id"]):
        c = [g["id"] for g in e["groups"] if g["role"] == "control"]
        if len(c) == 1:
            out.append((e, c[0], [{"id": g["id"], "label": g.get("label"), "decision": "进行中", "note": ""}
                                  for g in e["groups"] if g["role"] == "treatment"]))
    return out


def decision_groups(rounds, exps, ov):
    """总览里能标决策的组（核对推全进度表用）：{(轮, 机房): [(实验名, 组, 手写?)]}，决策已叠加库里的修改。
    旧参照段固定「仅作对照」，不在内。"""
    out = {}
    for r in rounds:
        for mk, m in r["markets"].items():
            lst = out.setdefault((r["no"], mk), [])
            for x in m.get("exps") or []:
                lst += [(x["name"], dict(g, decision=_dec(ov, r["no"], mk, g["id"], g["decision"])), True) for g in x["groups"]]
            used = set()
            if m.get("segs"):
                sg = m["segs"]
                for s in sg["list"]:
                    hit = _seg_hit(exps, mk, sg, s)
                    if hit:
                        used.add(hit["id"])
                    if hit and not s["old"]:
                        # 组名优先用看板实验里的（已按文档同步过），手写配置里的短名只作兜底
                        dbl = {g["id"]: g.get("label") for g in hit["groups"]}
                        lst += [(s["name"], {"id": i, "label": dbl.get(i) or lab,
                                             "decision": _dec(ov, r["no"], mk, i, "进行中")}, False)
                                for i, lab in sg["trts"]]
            if "plan" in m:
                for e, _, trts in _auto_exps(exps, mk, m.get("launch") or r["launch"], used):
                    lst += [(e["name"], dict(g, decision=_dec(ov, r["no"], mk, g["id"], g["decision"])), False) for g in trts]
    return out


def merged_rounds(doc):
    """ROUNDS（手写）× 规划文档快照（自动）→ 总览的轮次结构。手写的优先：手写里空着的日期 / 备注才用文档补，
    手写里有的机房整栏照手写；文档里有、手写里没有的轮 / 机房，按分桶表生成（配置一句话 = 对照组「实际生效配置」精简，
    实验组 = 分桶表实验组，决策默认「进行中」，看板里同机房同上线日的实验自动归入）。"""
    from . import expsync
    if not doc:
        return ROUNDS
    hand = {r["no"]: r for r in ROUNDS}
    units = {}
    for u in doc["units"]:
        units.setdefault(u["no"], {})[u["market"]] = u
    out = []
    for no in sorted(set(hand) | set(doc["progress"]) | set(units), reverse=True):
        h, p = hand.get(no) or {}, doc["progress"].get(no) or {}
        r = {"no": no, "name": h.get("name") or p.get("name") or "第%s轮" % (expsync._CN[no - 1] if no <= 9 else no)}
        for k in _RKEYS:
            r[k] = h.get(k) or p.get(k) or ("" if k == "note" else None)
        ms = dict(h.get("markets") or {})
        for mk, _ in MARKETS:
            if mk in ms:
                continue
            u = (units.get(no) or {}).get(mk)
            if u:
                ms[mk] = {"config": expsync.config_line(u), "launch": u["launch"],     # 晚上线的机房按备注里的上线日
                          "plan": [g["label"] for g in u["groups"] if g["role"] == "treatment"], "auto": True}
            elif no not in hand:
                ms[mk] = {"config": "规划文档里还没有本轮分桶表", "plan": [], "auto": True}
        r["markets"] = ms
        out.append(r)
    return out


def view():
    from . import expsync
    exps = store.experiments()
    full = {}

    def load(e):
        if e["id"] not in full:
            full[e["id"]] = store.experiment(e["id"])
        return full[e["id"]]

    from . import rollout
    doc = expsync.snapshot_doc()
    merged = merged_rounds(doc)
    ro = rollout.view(merged, doc)
    ov = store.decision_overrides()
    rounds = []
    for r in merged:
        cols = []
        for mk, mlabel in MARKETS:
            m = r["markets"].get(mk) or {}
            col = {"market": mk, "label": mlabel, "config": m.get("config", ""), "notes": list(m.get("notes") or []),
                   "plan": list(m.get("plan") or []), "exps": []}
            dec = lambda gs: [dict(g, decision=_dec(ov, r["no"], mk, g["id"], g["decision"])) for g in gs]
            for x in m.get("exps") or []:
                hit = find_exp(exps, mk, r["launch"], x["ctrl"], [g["id"] for g in x["groups"]])
                col["exps"].append(exp_block(load(hit) if hit else None, dec(x["groups"]), x["name"], x["ctrl"]))
            used = set()
            if m.get("segs"):
                # 分段实验：主段在前（进行中），旧段在后（灰显、仅作对照，不算本轮决策）；没建的段显示「看板里还没有」
                sg = m["segs"]
                hits = [(s, _seg_hit(exps, mk, sg, s)) for s in sg["list"]]
                for s, hit in hits if any(h for _, h in hits) else ():     # 一段都没建 → 照旧显示 plan 占位
                    trts = [{"id": i, "label": lab, "note": "",
                             "decision": "仅作对照" if s["old"] else _dec(ov, r["no"], mk, i, "进行中")} for i, lab in sg["trts"]]
                    blk = exp_block(load(hit) if hit else None, trts, s["name"], sg["ctrl"])
                    blk.update(old=s["old"], tag=s["tag"], seg_note=s["note"])
                    col["exps"].append(blk)
                    if hit:
                        used.add(hit["id"])
            if "plan" in m:
                # 第三轮：同机房、上线日 = 本轮上线日的实验自动归入，决策默认「进行中」（已被分段认走的不再归入）
                for e, ctrl, trts in _auto_exps(exps, mk, m.get("launch") or r["launch"], used):
                    col["exps"].append(exp_block(load(e), dec(trts), e["name"], ctrl))
            cols.append(col)
        rounds.append({k: r[k] for k in ("no", "name", "launch", "full", "judge", "rollout", "note")} | {"markets": cols}
                      | {"rollout_obs": ro["rounds"].get(r["no"])})
    mm = store.metric_map()
    catalog = {k: {"name": n, "unit": jobs._unit(mm[k]) if k in mm else "pct"} for k, n, _ in METRICS}
    for r in rounds:
        for col in r["markets"]:
            for x in col["exps"]:
                for s in x.get("spec") or []:
                    catalog[s["key"]] = {"name": s["name"], "unit": s["unit"]}
    return {"metrics": [{"key": k, "name": n, "role": g, "unit": jobs._unit(mm[k]) if k in mm else "pct"} for k, n, g in METRICS],
            "catalog": catalog,
            "rounds": rounds, "rollout": {"global": ro["global"], "caveat": ro["caveat"], "t_crit": ro["t_crit"]}}
