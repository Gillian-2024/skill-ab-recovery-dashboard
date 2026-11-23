"""KG raw_sql 通道：spawn kg-mcp-client，走 MCP JSON-RPC over stdio。

改写自 ~/.claude/skills/search-funnel-collect/scripts/build_pipeline.py 的 KG 类，
增加：强制 NO_PROXY、登录态预检、失败/截断显式报错、上下文管理。
"""
import collections
import json
import re
import os
import queue
import socket
import subprocess
import threading
import time
import urllib.parse

from . import config as C


# 取消当前任务：工作线程单线程串行，同一时刻只有一个任务在用 KG，一个全局信号就够
CANCEL = threading.Event()


class KGError(RuntimeError):
    pass


class Cancelled(KGError):
    """用户在任务日志里点了取消。数仓侧已提交的那条查询停不了，只是不再等它、不再收结果。"""


def check_cancel():
    if CANCEL.is_set():
        raise Cancelled("已取消")


LOGIN_HINT = "数据源查询登录态缺失或已过期：在本机终端运行配置的查询客户端完成登录，再到任务日志重跑失败的任务"
_LOGIN_RX = re.compile(r"登录态|session|(?<!\d)401(?!\d)|unauthori[sz]ed", re.I)


def login_suspect(err):
    """报错像是 kg 登录态缺失 / 过期（页面顶部提醒、日更失败通知共用）。"""
    return bool(_LOGIN_RX.search(err or ""))


# 10-31 / 11-01 周末实测：VPN 断开时 kg 域名解析不了，每个任务 1 秒内报 [Errno 8] nodename nor servname，
# 日更每 2 小时整批提交一次、整批失败，实验跑数失败还会把实验标 failed、停每日续跑
NET_HINT = "多半是 VPN 没连或本机 DNS 断了；看板已暂停提交跑数，恢复后 1 分钟内自动补跑，不用手动重跑"
_NET_RX = re.compile(r"KG 连不上|Errno (?:8|50|51|54|60|61|65)\b|nodename nor servname|getaddrinfo|ENOTFOUND|"
                     r"Connection (?:reset|refused)|Network is (?:unreachable|down)|No route to host", re.I)


def net_suspect(err):
    """报错像是本机到 kg 的网络不通（VPN / DNS）。"""
    return bool(_NET_RX.search(err or ""))


def reachable(timeout=5):
    """本机能否直连 kg（DNS 解析 + TCP 握手，不走代理，和 client 的 NO_PROXY 一致）。能连回 ""，否则回原因。"""
    u = urllib.parse.urlparse(C.KG_API_URL)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        return "DNS 解析不了 %s（%s）" % (host, e)
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError as e:
        return "连不上 %s:%d（%s）" % (host, port, e)
    return ""


class KG:
    def __init__(self, log=print, trace=None):
        if not os.path.exists(C.KG_CLIENT):
            raise KGError("找不到 kg-mcp-client：%s" % C.KG_CLIENT)
        if not os.path.exists(C.KG_SESSION):
            raise KGError("kg 飞书登录态缺失（%s），请在终端运行 kg-mcp-client 完成登录" % C.KG_SESSION)
        why = reachable()
        if why:
            raise KGError("KG 连不上：%s。%s" % (why, NET_HINT))
        self.log = log
        # trace(dict)：每条查询跑完（成功或失败）回调一次，供任务日志溯源：label / sql / kg_task_id / 行数 / 耗时 / 错误
        self.trace = trace
        self._id = 0
        env = dict(os.environ)
        for k in ("NO_PROXY", "no_proxy"):
            env[k] = ",".join(x for x in (env.get(k, ""), C.KG_NO_PROXY) if x)
        self.p = subprocess.Popen(
            [C.KG_CLIENT, "--api-url", C.KG_API_URL, "--log-level", "ERROR"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env)
        self.q = queue.Queue()
        self.stderr = collections.deque(maxlen=20)
        threading.Thread(target=lambda: [self.q.put(l.rstrip()) for l in self.p.stdout], daemon=True).start()
        # 只留有用的 stderr：client 启动时会打一段方框字符画 banner
        keep = lambda l: l.strip() and not any(ch in l for ch in "│█▀▄╭╮╰╯─")
        threading.Thread(target=lambda: [self.stderr.append(l.rstrip()) for l in self.p.stderr if keep(l)],
                         daemon=True).start()
        self._send({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "search-dashboard", "version": "1"}}})
        try:
            ok = self._wait(self._id, 30)
        except Cancelled:
            self.close()
            raise
        if not ok:
            self.close()
            raise KGError("kg-mcp-client initialize 无响应：%s" % " | ".join(self.stderr))
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _next(self):
        self._id += 1
        return self._id

    def _send(self, o):
        self.p.stdin.write(json.dumps(o) + "\n")
        self.p.stdin.flush()

    def _wait(self, want, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            check_cancel()
            if self.p.poll() is not None and self.q.empty():
                return None
            try:
                line = self.q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("id") == want:
                return o
        return None

    def _call(self, name, args, timeout=120):
        mid = self._next()
        self._send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                    "params": {"name": name, "arguments": args}})
        r = self._wait(mid, timeout)
        if not r:
            raise KGError("%s 无响应（%ss）%s" % (name, timeout, " | ".join(self.stderr)))
        if "error" in r:
            raise KGError("%s 报错：%s" % (name, json.dumps(r["error"], ensure_ascii=False)))
        txt = "".join(c.get("text", "") for c in r.get("result", {}).get("content", []))
        try:
            return json.loads(txt)
        except ValueError:
            raise KGError("%s 返回非 JSON：%s" % (name, txt[:600]))

    @staticmethod
    def _fail(r):
        msg = r.get("message") or r.get("error") or json.dumps(r, ensure_ascii=False)[:400]
        hint = r.get("error_hint")
        e = KGError("KG 任务失败（%s）：%s%s" % (r.get("error_category") or r.get("status"), msg,
                                              ("；" + hint) if hint else ""))
        # 引擎侧线程池被回收（Kyuubi session 关闭）也是瞬时故障，重提即可
        # 网关 502/503/504（2026-10-20 实测 openresty 502，KG 标成 system）同理
        # KG 实例重启把长任务标 stale（10-27 实测：SG 分群查询跑 20 分钟后「Task stale — instance may have restarted」）同理
        e.retryable = r.get("error_category") in ("connection", "timeout") or r.get("status") == "timeout" \
            or "Task stale" in json.dumps(r, ensure_ascii=False) \
            or "ThreadPoolExecutor" in str(msg) and "Terminated" in str(msg) \
            or bool(re.search(r"HTTP 50[234]|Bad Gateway|Gateway Time-?out|Service Unavailable", str(msg)))
        return e

    def rows(self, sql, label="", timeout=C.KG_QUERY_TIMEOUT, retries=2, catalog=None):
        """执行一条只读 SQL，返回 (列名列表, 行列表)。连接类错误自动重试。catalog 默认 C.CATALOG（广告表走另一个 catalog）。"""
        for attempt in range(retries + 1):
            try:
                return self._rows(sql, label, timeout, catalog)
            except KGError as e:
                if not getattr(e, "retryable", False) or attempt == retries:
                    raise
                self.log("KG %s 瞬时故障，60s 后重试（%d/%d）：%s" % (label, attempt + 1, retries, str(e)[:80]))
                if CANCEL.wait(60):
                    check_cancel()

    def _rows(self, sql, label, timeout, catalog=None):
        t0, info = time.time(), {"label": label, "sql": sql, "kg_task_id": None}
        try:
            cols, out = self._rows_inner(sql, label, timeout, info, catalog)
        except KGError as e:
            self._trace(info, t0, error=str(e))
            raise
        self._trace(info, t0, rows=len(out))
        return cols, out

    def _trace(self, info, t0, rows=None, error=None):
        if self.trace:
            try:
                self.trace(dict(info, rows=rows, error=error, seconds=round(time.time() - t0, 1)))
            except Exception:
                pass

    def _rows_inner(self, sql, label, timeout, info, catalog=None):
        check_cancel()
        t0 = time.time()
        r = self._call("kg_execute_analysis",
                       {"analysis_type": "raw_sql", "business": C.BUSINESS,
                        "params": {"catalog": catalog or C.CATALOG, "sql": sql},
                        # kg-mcp-client 0.6.5 起必填；看板是定时取数，没有用户原话，填取数任务名
                        "original_question": "搜索看板定时取数：%s" % (label or "")})
        info["kg_task_id"] = r.get("task_id")
        res = None
        if r.get("status") == "completed":
            res = r.get("result") or {}
        elif r.get("status") in ("failed", "timeout") or "error" in r:
            raise self._fail(r)
        else:
            tid = r.get("task_id")
            if not tid:
                raise KGError("无 task_id：%s" % json.dumps(r, ensure_ascii=False)[:300])
            ghost = 0
            while time.time() - t0 < timeout:
                rr = self._call("kg_get_analysis_result", {"task_id": tid, "wait": 25}, timeout=60)
                st = rr.get("status")
                if st == "completed":
                    res = rr.get("result") or {}
                    break
                # 2026-10-20 实测：长查询轮询偶发回 failed 但无任何错误字段，任务其实仍在跑 → 继续轮询
                if st == "failed" and not any(rr.get(k) for k in ("message", "error", "error_category", "error_hint")) \
                        and rr.get("current_step") and ghost < 20:
                    ghost += 1
                    if ghost == 1:
                        self.log("KG %s 轮询回了无错误信息的 failed（当前：%s），视为仍在执行，继续等" % (label, rr["current_step"][:40]))
                    if CANCEL.wait(15):
                        check_cancel()
                    continue
                if st in ("failed", "timeout"):
                    raise self._fail(rr)
            if res is None:
                raise KGError("任务 %s 超时（%ss）" % (tid, timeout))
        if res.get("truncated"):
            raise KGError("%s 结果被截断（>1000 行），需要收窄窗口" % label)
        cols = [c["name"] for c in (res.get("columns") or [])]
        out = res.get("rows") or []
        self.log("KG %s：%d 行，%.0fs" % (label, len(out), time.time() - t0))
        return cols, out

    def dicts(self, sql, label="", catalog=None):
        cols, rows = self.rows(sql, label, catalog=catalog)
        return [dict(zip(cols, r)) for r in rows]

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        time.sleep(0.2)
        if self.p.poll() is None:
            self.p.kill()
