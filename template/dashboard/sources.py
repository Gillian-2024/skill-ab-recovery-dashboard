"""取数通道：duckdb（本地文件）/ sqlalchemy（数据库 URL）/ command（自定义命令，SQL 走 stdin、JSON 走 stdout）。

统一接口：
    with open_source(log, trace) as src:
        rows = src.dicts(sql, "标签")      # -> list[dict]
trace(q) 每条查询回调一次：{label, sql, rows, seconds, error, task_id}，用于任务日志溯源。
"""
import json
import os
import subprocess
import threading
import time

from . import config as C


class SourceError(RuntimeError):
    pass


class SourceTimeout(SourceError):
    pass


class SourceAuthError(SourceError):
    pass


_AUTH_HINTS = ("401", "403", "unauthorized", "forbidden", "token", "credential", "login", "authenticat", "登录", "认证", "鉴权")


def _classify(msg):
    low = msg.lower()
    if any(h in low for h in _AUTH_HINTS):
        return SourceAuthError(msg)
    return SourceError(msg)


class _Base:
    def __init__(self, log=print, trace=None):
        self.log, self.trace = log, trace
        s = C.cfg["source"]
        self.max_rows, self.timeout = int(s["max_rows"]), int(s["timeout_s"])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        pass

    def dicts(self, sql, label):
        t0, info = time.time(), {"label": label, "sql": sql}
        try:
            rows, task_id = self._run(sql)
            info["task_id"] = task_id
            if len(rows) >= self.max_rows:
                raise SourceError("「%s」返回 %d 行，达到上限 source.max_rows=%d，结果可能被截断。缩短日期窗口或调大上限"
                                  % (label, len(rows), self.max_rows))
            info.update(rows=len(rows), seconds=round(time.time() - t0, 1))
            self.log("✓ %s：%d 行，%.1f 秒" % (label, len(rows), time.time() - t0))
            return rows
        except Exception as ex:
            info.update(error=str(ex)[:2000], seconds=round(time.time() - t0, 1))
            self.log("✗ %s：%s" % (label, str(ex)[:300]))
            raise
        finally:
            if self.trace:
                try:
                    self.trace(info)
                except Exception:
                    pass


class DuckDBSource(_Base):
    """path：.duckdb 数据库文件；views：{视图名: CSV / Parquet 路径或通配}，启动时注册成临时视图。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        import duckdb
        s = C.cfg["source"]
        path = s.get("path")
        if path and not os.path.exists(C.path_of(path)):
            raise SourceError("找不到 DuckDB 文件：%s（演示数据可用 tools/demo_data.py 生成）" % C.path_of(path))
        self.con = duckdb.connect(C.path_of(path) if path else ":memory:", read_only=bool(path))
        for name, glob in (s.get("views") or {}).items():
            if not C._IDENT.match(str(name)):
                raise SourceError("source.views 的视图名不合法：%r" % name)
            p = C.path_of(glob).replace("'", "''")
            fn = "read_parquet" if p.endswith(".parquet") or p.endswith(".parquet*") else "read_csv_auto"
            self.con.execute("CREATE TEMP VIEW %s AS SELECT * FROM %s('%s')" % (name, fn, p))

    def _run(self, sql):
        out, err = {}, {}

        def go():
            try:
                cur = self.con.execute(sql)
                cols = [d[0] for d in cur.description]
                out["rows"] = [dict(zip(cols, r)) for r in cur.fetchmany(self.max_rows)]
            except Exception as ex:
                err["e"] = ex

        th = threading.Thread(target=go, daemon=True)
        th.start()
        th.join(self.timeout)
        if th.is_alive():
            self.con.interrupt()
            th.join(10)
            raise SourceTimeout("查询超过 %d 秒，已中断（source.timeout_s）" % self.timeout)
        if "e" in err:
            raise _classify(str(err["e"]))
        return out["rows"], None

    def close(self):
        self.con.close()


class SQLAlchemySource(_Base):
    """url_env：存放连接串的环境变量名（连接串含密码，不写进 dashboard.yaml）。需自行安装对应驱动。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        s = C.cfg["source"]
        env = s.get("url_env") or "DASH_DB_URL"
        url = os.environ.get(env)
        if not url:
            raise SourceAuthError("没有读到数据库连接串：请在 .env 里设置 %s（格式见 .env.example）" % env)
        try:
            import sqlalchemy as sa
        except ImportError:
            raise SourceError("未安装 SQLAlchemy：在项目 venv 里 pip install SQLAlchemy==2.0.36 和对应驱动")
        self.sa = sa
        self.engine = sa.create_engine(url, pool_pre_ping=True)

    def _run(self, sql):
        out, err = {}, {}

        def go():
            try:
                with self.engine.connect() as c:
                    res = c.execute(self.sa.text(sql.replace(":", r"\:")))
                    cols = list(res.keys())
                    out["rows"] = [dict(zip(cols, r)) for r in res.fetchmany(self.max_rows)]
            except Exception as ex:
                err["e"] = ex

        th = threading.Thread(target=go, daemon=True)
        th.start()
        th.join(self.timeout)
        if th.is_alive():
            raise SourceTimeout("查询超过 %d 秒仍未返回（source.timeout_s）。数据库侧可能仍在执行，必要时手动取消" % self.timeout)
        if "e" in err:
            raise _classify(str(err["e"]).split("\n")[0][:1500])
        return out["rows"], None

    def close(self):
        self.engine.dispose()


class CommandSource(_Base):
    """command：argv 列表。SQL 从 stdin 传入；stdout 输出 JSON，三种格式都认：
    [ {列: 值}, ... ] / {"rows": [...]} / {"columns": [...], "data": [[...]]}；可带 "task_id" 便于溯源。
    非 0 退出码视为失败，stderr 末尾会进任务日志。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        s = C.cfg["source"]
        cmd = s.get("command")
        if not cmd or not isinstance(cmd, list):
            raise SourceError("source.command 要写成参数列表，如 [\"python3\", \"my_query.py\"]")
        self.cmd = [os.path.expanduser(str(x)) for x in cmd]
        self.env = dict(os.environ)
        self.env.update({str(k): str(v) for k, v in (s.get("env") or {}).items()})

    def _run(self, sql):
        try:
            p = subprocess.run(self.cmd, input=sql, capture_output=True, text=True, timeout=self.timeout, env=self.env)
        except subprocess.TimeoutExpired:
            raise SourceTimeout("取数命令超过 %d 秒未返回，已终止（source.timeout_s）" % self.timeout)
        except FileNotFoundError:
            raise SourceError("找不到取数命令：%s" % self.cmd[0])
        if p.returncode != 0:
            raise _classify("取数命令退出码 %d：%s" % (p.returncode, (p.stderr or p.stdout or "").strip()[-1500:]))
        try:
            j = json.loads(p.stdout)
        except ValueError:
            raise SourceError("取数命令的输出不是 JSON：%s" % p.stdout.strip()[:300])
        task_id = j.get("task_id") if isinstance(j, dict) else None
        if isinstance(j, dict) and j.get("error"):
            raise _classify(str(j["error"]))
        if isinstance(j, dict) and "columns" in j:
            rows = [dict(zip(j["columns"], r)) for r in j.get("data") or []]
        elif isinstance(j, dict):
            rows = j.get("rows") or []
        else:
            rows = j
        if not isinstance(rows, list) or (rows and not isinstance(rows[0], dict)):
            raise SourceError("取数命令输出格式不对：要 [{...}] / {\"rows\": [...]} / {\"columns\", \"data\"}")
        if isinstance(j, dict) and j.get("truncated"):
            raise SourceError("取数命令报告结果被截断（truncated=true）")
        return rows, task_id


def open_source(log=print, trace=None):
    t = C.cfg["source"]["type"]
    return {"duckdb": DuckDBSource, "sqlalchemy": SQLAlchemySource, "command": CommandSource}[t](log, trace)


def auth_ok():
    """页面顶部提醒用：凭据是否就位（不发请求）。"""
    s = C.cfg["source"]
    if s["type"] == "sqlalchemy":
        return bool(os.environ.get(s.get("url_env") or "DASH_DB_URL"))
    if s["type"] == "command":
        need = s.get("auth_file")
        return os.path.exists(os.path.expanduser(need)) if need else True
    return True
