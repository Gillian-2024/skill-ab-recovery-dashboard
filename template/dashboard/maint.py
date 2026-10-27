"""本机维护：结果库每日快照（online backup + 完整性校验 + gzip，留最近 snapshot_keep 份）、server.log 超限轮转。只写数据目录，不出本机。"""
import gzip
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import traceback

from . import config as C

SNAP_RE = re.compile(r"^dashboard-\d{8}\.db\.gz$")


def _opt(k):
    return C.cfg["maintenance"][k]


def snapshot_dir():
    return os.path.join(C.DATA_DIR, "snapshots")


def log_path():
    return os.path.join(C.DATA_DIR, "server.log")   # tools/install_service.py 把 stdout / stderr 都重定向到这里


def daily(today):
    """每天第一次 tick 调一次；两件事互不挡。"""
    try:
        snapshot(today)
    except Exception:
        traceback.print_exc()
    try:
        rotate_log()
    except Exception:
        traceback.print_exc()


def snapshot(today):
    """今天的快照已有就跳过。backup → integrity_check → gzip → 原子 rename，再按份数清理。返回最终路径，跳过返回 None。"""
    if not os.path.exists(C.DB_PATH):
        return None
    d = snapshot_dir()
    os.makedirs(d, exist_ok=True)
    final = os.path.join(d, "dashboard-%s.db.gz" % today.strftime("%Y%m%d"))
    if os.path.exists(final):
        return None
    # 临时文件以 . 开头，不会被 prune 匹配；失败时一律删掉，不留半截文件
    fd, tmp_db = tempfile.mkstemp(dir=d, prefix=".snap-", suffix=".db")
    os.close(fd)
    tmp_gz = tmp_db + ".gz"
    try:
        src = sqlite3.connect(C.DB_PATH, timeout=30)
        try:
            dst = sqlite3.connect(tmp_db)
            try:
                src.backup(dst)           # 在线备份：后台任务线程同时在写也能拿到一致的副本
            finally:
                dst.close()
        finally:
            src.close()
        r = _integrity(tmp_db)
        if r != "ok":
            raise RuntimeError("快照完整性校验失败：%s" % r[:300])
        with open(tmp_db, "rb") as fi, gzip.open(tmp_gz, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
        os.replace(tmp_gz, final)
    finally:
        for p in (tmp_db, tmp_gz):
            if os.path.exists(p):
                os.remove(p)
    prune(d, _opt("snapshot_keep"))
    return final


def _integrity(path):
    c = sqlite3.connect(path)
    try:
        return "\n".join(str(r[0]) for r in c.execute("PRAGMA integrity_check"))
    finally:
        c.close()


def prune(d, keep):
    """只动 dashboard-YYYYMMDD.db.gz，按文件名里的日期留最新 keep 份。"""
    names = sorted((n for n in os.listdir(d) if SNAP_RE.match(n)), reverse=True)
    for n in names[keep:]:
        os.remove(os.path.join(d, n))


def rotate_log():
    """server.log 超过 log_max_mb 且 fd 1 确实指向它（作为系统服务跑）才轮转；终端 / 测试里跑不动 fd。返回是否轮转。"""
    p = log_path()
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return False
    if st.st_size <= _opt("log_max_mb") * 1024 * 1024 or not _is_stdout(st):
        return False
    _shift(p, _opt("log_keep"))
    # 先 rename 再新开（O_APPEND）+ dup2：直接截断原文件而 fd 没开 O_APPEND 会写出空洞文件
    old = p + ".rotating"
    os.replace(p, old)
    _reopen(p)
    tmp = old + ".gz"
    with open(old, "rb") as fi, gzip.open(tmp, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    os.replace(tmp, p + ".1.gz")
    os.remove(old)
    return True


def _shift(p, keep):
    """.N.gz → .N+1.gz，序号到 keep 的删掉（腾出 .1.gz 给这次）。"""
    base = os.path.basename(p)
    rx = re.compile(r"^%s\.(\d+)\.gz$" % re.escape(base))
    d = os.path.dirname(p)
    nums = sorted((int(m.group(1)) for m in map(rx.match, os.listdir(d)) if m), reverse=True)
    for n in nums:
        src = "%s.%d.gz" % (p, n)
        if n >= keep:
            os.remove(src)
        else:
            os.replace(src, "%s.%d.gz" % (p, n + 1))


def _is_stdout(st):
    try:
        o = os.fstat(1)
    except OSError:
        return False
    return (o.st_ino, o.st_dev) == (st.st_ino, st.st_dev)


def _reopen(p):
    """新开 server.log 接管 fd 1 / 2（服务的日志重定向就靠这两个 fd）。"""
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        for f in (sys.stdout, sys.stderr):
            if f is not None:
                f.flush()
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)
