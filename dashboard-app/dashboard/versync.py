"""「分开看」新版本列表同步：读发版记录表 → 更新版本列表 + 核验。

表的结构（示例）：
- A 列每个 Sprint 一格：「Sprint 11：\\n2030010100：基础版本（…）」，下面若干行是这个 Sprint 的周历；
- D~J 列是周历：一行「X月Y日 …」，下一行写当天放量，如「20300101GP5%」「20300201 GA2.5%」。
规则：
- 新版本 = 归在 Sprint 10 及以后的版本（该版本修了搜索 from 字段）；按版本号精确匹配，不用 ≥（早期包编号大但可能早于该修复）；
- 渠道（GP / GA）、首次放量日从周历里找；数仓 channel 全是 default 分不出渠道，只靠版本号；
- 表里新出现的版本自动加进列表，并对它放量以来的天重算「分开看」；列表里有、表里找不到的只提示不删。
读失败沿用上次结果，记错误给页面提示；从没同步过时用 config.SPLIT_VERSIONS。
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess

from . import config as C, jobs, store

SHEET = os.environ.get("RELEASE_SHEET_TOKEN", "")
SHEET_ID = os.environ.get("RELEASE_SHEET_ID", "")
SHEET_URL = ("https://feishu.example.com/sheets/%s?sheet=%s" % (SHEET, SHEET_ID)) if SHEET else ""
FEISHU = os.environ.get("FEISHU_BIN", "feishu")
MIN_SPRINT = 10
_SPRINT = re.compile(r"Sprint\s*(\d+)", re.I)
_LINE = re.compile(r"^\s*(\d{8}(?:\d{2})?)\s*(?:版本)?\s*[：:]\s*(.*)$")
_ROLL = re.compile(r"(\d{6,10}(?:\s*[/,，]\s*\d{2,10})*)\s*(GP|GA)", re.I)
_DAY = re.compile(r"^\s*(\d{1,2})月(\d{1,2})日\s*$")


def _text(c):
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return "" if c is None else str(c)


def _full(v):
    """8 位（20260714）补成 10 位 app_version（2026071400）。"""
    return v + "00" if len(v) == 8 else v


def _expand(group):
    """「2026113000/01/02」「2026122920,123020」「20260910,20260911」→ 完整版本号列表（后面的简写补前缀）。"""
    out = []
    for t in re.split(r"\s*[/,，]\s*", group.strip()):
        if not out or len(t) in (8, 10):
            out.append(_full(t))
        else:
            out.append(out[-1][:10 - len(t)] + t)
    return [v for v in out if len(v) == 10]


def _date(m, d, today):
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            x = dt.date(y, m, d)
        except ValueError:
            return None
        if best is None or abs((x - today).days) < abs((best - today).days):
            best = x
    return jobs.ymd(best)


def parse_sheet(values, today=None):
    """→ {版本号: {sprint, desc, ch, first}}（只收 Sprint ≥ 90）+ 解析提示。"""
    today = today or dt.date.today()
    rows = [[_text(c) for c in (r or [])] for r in values]
    vers, notes = {}, []
    for r in rows:
        a = r[0] if r else ""
        m = _SPRINT.search(a)
        if not m or int(m.group(1)) < MIN_SPRINT:
            continue
        for line in a.splitlines()[1:]:
            lm = _LINE.match(line)
            if lm:
                vers.setdefault(_full(lm.group(1)), {"sprint": int(m.group(1)), "desc": lm.group(2).strip()[:60]})
    # 周历：表头行是「X月Y日」，下一行同列是当天放量
    roll = {}
    for i in range(1, len(rows)):
        head = rows[i - 1]
        for j, cell in enumerate(rows[i]):
            if j < 3 or not cell or j >= len(head):
                continue
            dm = _DAY.match(head[j])
            day = _date(int(dm.group(1)), int(dm.group(2)), today) if dm else None
            for g, ch in _ROLL.findall(cell):
                for v in _expand(g):
                    hit = roll.setdefault(v, {"ch": set(), "first": None})
                    hit["ch"].add(ch.upper())
                    if day and (hit["first"] is None or day < hit["first"]):
                        hit["first"] = day
    for v, x in vers.items():
        h = roll.get(v)
        x["ch"] = "/".join(sorted(h["ch"])) if h else None
        x["first"] = h["first"] if h else None
        if not h:
            notes.append(v)
    return vers, notes


def _fetch():
    exe = FEISHU if os.path.exists(FEISHU) else shutil.which("feishu")
    if not exe:
        raise RuntimeError("本机没有 feishu 命令行工具")
    p = subprocess.run([exe, "sheet", "read", SHEET, "%s!A1:J2000" % SHEET_ID], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError("读发版记录表失败：%s" % ((p.stderr or p.stdout).strip()[-300:] or "退出码 %d" % p.returncode))
    values = json.loads(p.stdout[p.stdout.index("{"):]).get("values") or []
    if not values:
        raise RuntimeError("发版记录表读到了，但没有内容")
    return values


def current():
    """当前生效的版本列表 {版本号: 说明}：同步过用同步结果，否则用 config 里的初始列表。"""
    c = store.ver_config()
    if c.get("vals"):
        return {v: "%s SP%s %s" % (x.get("ch") or "?", x.get("sprint"), x.get("desc") or "") for v, x in c["vals"].items()}
    return dict(C.SPLIT_VERSIONS)


def sync(log=print, fetch=None, today=None):
    """读表 → 合并进版本列表 → 对新加版本放量以来的天重算「分开看」。返回 {ok, added, missing, job_id}。"""
    today = today or dt.date.today()
    try:
        vers, no_roll = parse_sheet((fetch or _fetch)(), today)
        if not vers:
            raise RuntimeError("没解析出 Sprint %d 及以后的版本（A 列格式变了？）" % MIN_SPRINT)
    except Exception as ex:
        store.save_ver_config(None, str(ex)[:300])
        log("⚠ 版本同步失败，沿用上次列表：%s" % ex)
        return {"ok": False, "error": str(ex)[:300]}
    old = set(current())
    added = sorted(set(vers) - old)
    missing = sorted(old - set(vers))
    # 列表里有、表里找不到的不删（用户定）：原样带上，页面提示
    prev = store.ver_config().get("vals") or {v: {"sprint": None, "desc": d, "ch": None, "first": None} for v, d in C.SPLIT_VERSIONS.items()}
    vals = dict({v: prev[v] for v in missing if v in prev}, **vers)
    report = {"added": added, "missing": missing, "no_rollout": no_roll, "n": len(vals)}
    store.save_ver_config(vals, None, report)          # 先存列表，重算任务跑起来读的就是新列表
    job = None
    if added:
        firsts = [vers[v]["first"] for v in added if vers[v].get("first")]
        start = max(C.SPLIT_START, min(firsts) if firsts else C.SPLIT_START)
        if start <= jobs.yesterday():
            scopes = list(C.MARKETS) + [k for k in store.funnel_scopes() if k not in C.MARKETS]
            job = jobs.submit("split_backfill", {"markets": scopes, "start": start, "end": jobs.yesterday()})
    if job:
        report["job_id"] = job
        store.save_ver_report(report)
    log("版本同步：共 %d 个；新增 %d，表里找不到 %d" % (len(vals), len(added), len(missing)))
    return {"ok": True, "added": added, "missing": missing, "job_id": job}


def summary():
    c = store.ver_config()
    vals = c.get("vals") or {v: {"desc": d} for v, d in C.SPLIT_VERSIONS.items()}
    return {"versions": [dict(v=v, **x) for v, x in sorted(vals.items())], "synced": bool(c.get("vals")),
            "fetched_at": c.get("fetched_at"), "error": c.get("error"), "error_at": c.get("error_at"),
            "report": c.get("report"), "sheet_url": SHEET_URL, "fallback": C.SPLIT_VERSIONS_SYNCED}
