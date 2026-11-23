"""主动通知：日更失败提醒 + 实验节点摘要。发给看板 owner（飞书任务 + 本机系统通知）。

- 日更失败：12:00 与 RETRY_UNTIL_HOUR 各查一次昨天的数是否跑齐（漏斗各视图 / 热词 / 在跑实验），缺就提醒；
  当天提醒过、之后跑齐 → 发一条「已恢复」。
- 实验节点：实验跑数 / 补算成功后，数据第一次到第 3 / 7 / 14 天（或冻结）→ 发一份节点摘要。
- 去重：notify_log 表，先占 key 再发 = 至多一次，重启不重发；飞书发失败只记日志，本机通知兜底。
消息里只放任务类型 / 市场 / 指标读数和截断后的报错首行，不放 SQL、不放明细行。
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time

from . import config as C, jobs, kg_client, store
from .hotword import FEISHU
from .kg_client import LOGIN_HINT, NET_HINT, login_suspect

TAG = "[Dashboard] "
SLOTS = (("noon", (12, 0)), ("eve", (C.RETRY_UNTIL_HOUR, 0)))
# 服务在检查点前不到 1 小时才启动的，跳过这个检查点：重启那一下刚补提交的任务还没跑，不算失败
GRACE = 3600
# 节点摘要读的三项（结果页点击渗透率 = 北极星，另两项是过程指标）
READ_KEYS = (("ctr_ui", "结果页点击渗透率"), ("click_3s", "点击→3s"), ("vv_per_user", "人均 VV"))
CKPT_DAYS = (14, 7, 3)
_ok = set()          # (日期, 检查点)：查过且已跑齐，本进程内不再重复查


def _log(s):
    print("[notify] %s %s" % (time.strftime("%H:%M:%S"), s), file=sys.stderr, flush=True)


def url(eid=None):
    return "http://127.0.0.1:%d/#%s" % (C.PORT, "exp/%d" % eid if eid else "logs")


def md(d):
    s = str(d)
    return "%s-%s" % (s[4:6], s[6:])


# ─────────────────────────── 发送 ───────────────────────────

def send(title, body):
    """飞书任务（截止 = 5 分钟后，到点提醒）+ 本机系统通知。任一失败只记日志，不抛。返回飞书是否发成功。"""
    ok = False
    exe = FEISHU if os.path.exists(FEISHU) else shutil.which("feishu")
    try:
        if not exe:
            raise RuntimeError("本机没有 feishu 命令行工具")
        due = (dt.datetime.now() + dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        p = subprocess.run([exe, "task", "create", "--summary", TAG + title, "--description", body, "--due", due,
                            "--members", C.NOTIFY_OPEN_ID, "--role", "assignee"],
                           capture_output=True, text=True, timeout=60)
        out = p.stdout[p.stdout.index("{"):] if "{" in p.stdout else "{}"
        guid = json.loads(out).get("guid")
        if p.returncode or not guid:
            raise RuntimeError((p.stdout or p.stderr).strip()[-300:] or "退出码 %d" % p.returncode)
        ok = True
        # 截止时刻提醒（0 = 到点）；加提醒失败不影响任务本身
        r = subprocess.run([exe, "task", "reminder", "add", guid, "--minutes", "0"], capture_output=True, text=True, timeout=60)
        if r.returncode:
            _log("飞书任务 %s 加提醒失败：%s" % (guid, (r.stdout or r.stderr).strip()[-200:]))
    except Exception as ex:
        _log("飞书任务发送失败「%s」：%s" % (title, ex))
    try:
        # 标题 / 正文走 argv 传进 AppleScript，不拼进脚本，无需转义引号
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                        TAG + title, body[:200]], capture_output=True, timeout=10)
    except Exception as ex:
        _log("本机通知失败：%s" % ex)
    return ok


def _emit(keys, title, body):
    """占 key 后发送；key 都已占过（别处 / 重启前发过）→ 不发。"""
    if not C.NOTIFY_ON:
        return False
    if not [k for k in keys if store.notify_claim(k)]:
        return False
    _log("发送 %s：%s" % (",".join(keys), title))
    send(title, body)
    return True


# ─────────────────────────── 日更失败 ───────────────────────────

def _err(kind):
    """该类型今天最近一次失败任务的报错：首行、截掉 SQL、限 120 字。"""
    j = store.last_job(kind, "failed")
    if not j or dt.date.fromtimestamp(j["finished_at"] or 0) != dt.date.today():
        return ""
    s = (j["error"] or "").strip().splitlines()[0] if (j["error"] or "").strip() else ""
    s = re.split(r"\b(?:SELECT|WITH|INSERT|CREATE)\b", s)[0].strip()
    return s[:120] + ("…" if len(s) > 120 else "")


def missing_refresh(y=None):
    """昨天的数还缺什么：[(任务类型, 说明)]；跑齐 = []。口径同 _schedule_daily 的补跑判断。"""
    y = y or jobs.yesterday()
    out = []
    scopes = list(C.MARKETS) + [k for k in store.funnel_scopes() if k not in C.MARKETS]
    mk = [m for m in scopes if not store.day_status(m).get(y)]
    if mk:
        out.append(("funnel", "漏斗 %s 缺 %s" % ("、".join(mk), md(y))))
    # 热词：今天的日更任务跑到昨天，或库里三张表已有昨天（之后手动刷了别的区间也不误报）
    if not jobs.hotword_fresh(y) and y not in store.hw_dates():
        out.append(("hotword", "热词收益未拉到 %s" % md(y)))
    today = dt.date.today()
    # 在跑的实验没跑到昨天；当天跑失败被标 failed 的也算（跑数失败会把 active 改成 failed）
    ex = [e for e in store.experiments() if y >= int(e["start"]) and (e["last_end"] or 0) < min(int(e["end"]), y)
          and (e["status"] in ("active", "queued", "running")
               or (e["status"] == "failed" and dt.date.fromtimestamp(e["updated_at"] or 0) == today))]
    if ex:
        out.append(("experiment", "实验回收未跑到 %s：%s" % (md(y), "、".join(
            "#%d %s（数据到 %s）" % (e["id"], e["name"], md(e["last_end"]) if e["last_end"] else "无") for e in ex))))
    return out


def _slot_ts(now, hm):
    return dt.datetime.combine(now.date(), dt.time(*hm)).timestamp()


def check_refresh(now=None, since=0):
    """日更线程每分钟调一次。since = 服务启动时刻（epoch 秒）。"""
    if not C.NOTIFY_ON:
        return
    now = now or dt.datetime.now()
    day = jobs.ymd(now.date())
    sent = store.notify_keys("fail:%d:" % day)
    due = [s for s, hm in SLOTS if (now.hour, now.minute) >= hm and "fail:%d:%s" % (day, s) not in sent
           and (day, s) not in _ok and since <= _slot_ts(now, hm) - GRACE]
    need_ok = any(k.endswith((":noon", ":eve")) for k in sent) and "fail:%d:ok" % day not in sent
    if not due and not need_ok:
        return
    y = jobs.ymd(now.date() - dt.timedelta(days=1))
    miss = missing_refresh(y)
    if miss and due:
        # 两个检查点同时到（如电脑睡过了 12:00）：只发晚的那条，两个 key 一起占
        last = due[-1]
        title = "日更到 %d:00 仍未跑齐%s" % (dict(SLOTS)[last][0], "，今天不再自动重试" if last == "eve" else "")
        lines = ["数据日 %s，以下还缺：" % md(y)]
        errs = []
        for kind, text in miss:
            e = _err(kind)
            errs.append(e)
            lines.append("· %s%s" % (text, "；最近报错：" + e if e else ""))
        # 此刻 KG 连不上（VPN / DNS）：恢复后会自动补，标题点明原因，不说「不再自动重试」
        why = kg_client.reachable()
        if why:
            title = "KG 连不上（VPN/DNS），日更到 %d:00 仍未跑齐" % dict(SLOTS)[last][0]
            lines.insert(1, "原因：%s。%s。" % (why, NET_HINT))
        # kg 登录态缺失 / 过期：自动重试救不回来，标题直接点明，正文给处理办法
        elif not os.path.exists(C.KG_SESSION) or any(login_suspect(e) for e in errs):
            title = "kg 登录态失效，" + title
            lines.insert(1, "处理：" + LOGIN_HINT + "。")
        if last == "noon" and not why:
            lines.append("日更每 %d 小时自动重试到 %d:00。" % (C.RETRY_GAP_HOURS, C.RETRY_UNTIL_HOUR))
        lines.append("任务日志：" + url())
        _emit(["fail:%d:%s" % (day, s) for s in due], title, "\n".join(lines))
    elif not miss:
        _ok.update((day, s) for s in due)
        if need_ok:
            _emit(["fail:%d:ok" % day], "日更已恢复",
                  "今天提醒过的缺数已补齐：漏斗 / 热词 / 实验回收都已跑到 %s（%s 检查）。" % (md(y), now.strftime("%H:%M")))


# ─────────────────────────── 实验节点 ───────────────────────────

def _due_nodes(e, today=None):
    """该实验现在可以发的节点 [(N, 节点名)]，高的在前。
    防补发旧节点：节点日（开窗日 + N − 1）须 ≥ 今天 − 2 天，即数据是最近 1 天内（含晚一天跑到）才到这个节点的；
    更早的（如已冻结的第一、二轮）不补发。冻结且未满 14 天的，按冻结日当「出结论」节点（key 仍记 14）。"""
    if not e.get("last_end"):
        return []
    today = today or dt.date.today()
    d0, last = jobs.parse(e["start"]), jobs.parse(e["last_end"])
    floor = today - dt.timedelta(days=2)
    final = e.get("status") == "frozen" and last >= jobs.parse(e["end"])
    out = []
    for n in CKPT_DAYS:
        node = d0 + dt.timedelta(days=n - 1)
        if n == 14 and final:
            node = min(node, last)
        if last >= node >= floor:
            out.append((n, "出结论" if n == 14 else dict(jobs.NODES)[n]))
    return out


def _gname(e, g):
    x = next((x for x in e["groups"] if x["id"] == g), None)
    return "%s %s" % (x["label"], g) if x and x.get("label") and x["label"] != g else g


def digest(e, node, today=None):
    """节点摘要：(标题, 正文)。读数口径同页面「结论提要」：判止损读前 3 天累计，其余读当前累计行。"""
    v = jobs.experiment_view(e)
    sm, tables = v["summary"], {t["key"]: t for t in v["tables"] if t["kind"] == "ratio"}
    n0, name = node
    d0, d1 = jobs.parse(e["start"]), jobs.parse(e["last_end"])
    n = (d1 - d0).days + 1
    pick = 3 if n0 == 3 else n
    row = lambda t: next((r for r in t["rows"] if r["type"] in ("cum", "total") and r.get("days") == pick), None)
    final = n0 == 14
    nxt = None if final else next(((k, s) for k, s in jobs.NODES if k > n), None)
    lines = []
    # 判止损：北极星 / 护栏 95% CI 整体落在变差一侧（= 显著负向，方向已按 lower_is_better 换算）
    hurt = []
    if n0 == 3:
        for t in tables.values():
            r = row(t) if t["group"] in ("北极星", "护栏") else None
            for g, c in ((r or {}).get("vs") or {}).items():
                if c.get("read") == "显著负向" and (c.get("adj") or {}).get("ok", True):
                    hurt.append("%s%s %s（95%% CI %s）" % ("" if len(v["trts"]) < 2 else _gname(e, g) + " ", t["name"],
                                                         jobs._fmt_delta(t, c), jobs._fmt_ci(t, c)))
    # 长期价值：大盘 LT7 / 次留 / 7 留 显著负向且过了序贯阈值，任何节点都提示
    for m in ((v.get("lt") or {}).get("metrics") or []):
        for g, c in m["vs"].items():
            if c.get("stop"):
                hurt.append("%s%s %s（长期价值，已成熟设备）" % ("" if len(v["trts"]) < 2 else _gname(e, g) + " ", m["name"],
                                                     jobs.lt_fmt(m["unit"], c)))
    if hurt:
        lines.append("⚠ 判止损：" + "；".join(hurt))
    lines.append(e["name"])
    lines.append("第 %d 天 · %s（数据到 %s）%s" % (n, name, md(e["last_end"]),
                                            " · 下一节点：第 %d 天（%s）%s" % (nxt[0], (d0 + dt.timedelta(days=nxt[0] - 1)).strftime("%m-%d"), nxt[1]) if nxt else ""))
    if sm.get("state") != "ok":
        lines.append(sm.get("text") or "")
    for g in v["trts"]:
        lines.append("%s vs 对照（%s）：" % (_gname(e, g), "前 3 天累计" if pick == 3 and n > 3 else "累计 %d 天" % pick))
        for k, label in READ_KEYS:
            t = tables.get(k)
            c = ((row(t) or {}).get("vs") or {}).get(g) if t else None
            if not c:
                lines.append("· %s 暂无读数" % label)
                continue
            a = c.get("adj") or {}
            lines.append("· %s %s（z = %s，%s）" % (label, jobs._fmt_delta(t, c),
                                                 "%+.2f" % c["z"] if c.get("z") is not None else "—",
                                                 (c["read"] + ("，修正后不显著" if a.get("ok") is False else "")) if c.get("read") else "不显著"))
    lines.append(url(e["id"]))
    title = ("⚠ 判止损 · " if hurt else "实验节点 · %s · " % name) + e["name"]
    return title, "\n".join(lines)


def checkpoint(eid, today=None):
    """实验跑数 / 补算成功后调用；结果没出齐（还有补算 / 在跑）先不发，等补算跑完再进来。返回是否发了。"""
    if not C.NOTIFY_ON:
        return False
    e = store.experiment(eid)
    if not e or e["status"] in ("queued", "running") or [k for k in e.get("pending") or [] if k not in jobs.VIS_KEYS]:
        return False
    nodes = _due_nodes(e, today)
    keys = ["ckpt:%d:%d" % (eid, n) for n, _ in nodes]
    if not keys or not set(keys) - store.notify_keys("ckpt:%d:" % eid):
        return False
    # 同时可发多个节点（窗口很短就冻结）：只发最高的，其余一起占掉，免得之后补发低节点
    title, body = digest(e, nodes[0], today)
    return _emit(keys, title, body)
