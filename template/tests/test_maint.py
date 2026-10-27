"""本机维护：库快照（有效 gz / 当天跳过 / 只清本类文件 / 校验失败不留半截）、日志轮转（阈值 / fd 不是日志不动 / 序号移位，_reopen 已 mock）。

运行：python3 -m unittest discover -s tests（在看板目录下）
"""
import datetime as dt
import gzip
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dashboard import config as C  # noqa: E402

TODAY = dt.date(2025, 3, 14)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.snap = os.path.join(t, "snapshots")
        self.log = os.path.join(t, "server.log")
        mt = {"snapshot_keep": 14, "log_max_mb": 5, "log_keep": 3}
        self.p = mock.patch.multiple(C, DATA_DIR=t, DB_PATH=os.path.join(t, "t.db"), cfg={"maintenance": mt})
        self.p.start()
        from dashboard import maint
        self.M = maint

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()


class SnapshotTest(Base):
    def setUp(self):
        super().setUp()
        with sqlite3.connect(C.DB_PATH) as c:
            c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            c.executemany("INSERT INTO t (v) VALUES (?)", [("x%d" % i,) for i in range(500)])

    def rows(self, gz):
        out = os.path.join(self.tmp.name, "out.db")
        with gzip.open(gz, "rb") as fi, open(out, "wb") as fo:
            fo.write(fi.read())
        c = sqlite3.connect(out)
        try:
            return c.execute("SELECT id, v FROM t ORDER BY id").fetchall()
        finally:
            c.close()

    def test_creates_valid_gz(self):
        p = self.M.snapshot(TODAY)
        self.assertEqual(os.path.basename(p), "dashboard-20250314.db.gz")
        src = sqlite3.connect(C.DB_PATH).execute("SELECT id, v FROM t ORDER BY id").fetchall()
        self.assertEqual(self.rows(p), src)
        self.assertEqual(os.listdir(self.snap), ["dashboard-20250314.db.gz"])   # 临时文件已清

    def test_skip_when_today_exists(self):
        p = self.M.snapshot(TODAY)
        mt = os.stat(p).st_mtime_ns
        with mock.patch.object(self.M.sqlite3, "connect") as cn:
            self.assertIsNone(self.M.snapshot(TODAY))
            cn.assert_not_called()
        self.assertEqual(os.stat(p).st_mtime_ns, mt)

    def test_prune_keeps_newest_and_ignores_others(self):
        os.makedirs(self.snap)
        for i in range(20):
            d = TODAY - dt.timedelta(days=i + 1)
            open(os.path.join(self.snap, "dashboard-%s.db.gz" % d.strftime("%Y%m%d")), "wb").close()
        others = ["notes.txt", "dashboard-2025.db.gz", "dashboard-20200101.db"]
        for n in others:
            open(os.path.join(self.snap, n), "wb").close()
        self.M.snapshot(TODAY)
        names = os.listdir(self.snap)
        snaps = sorted((n for n in names if self.M.SNAP_RE.match(n)), reverse=True)
        self.assertEqual(len(snaps), 14)
        self.assertEqual(snaps[0], "dashboard-20250314.db.gz")
        self.assertEqual(snaps[-1], "dashboard-%s.db.gz" % (TODAY - dt.timedelta(days=13)).strftime("%Y%m%d"))
        for n in others:
            self.assertIn(n, names)

    def test_integrity_fail_leaves_nothing(self):
        with mock.patch.object(self.M, "_integrity", return_value="*** in database main ***\nPage 2: btree bad"):
            with self.assertRaises(RuntimeError):
                self.M.snapshot(TODAY)
        self.assertEqual(os.listdir(self.snap), [])

    def test_skip_when_no_db_yet(self):
        os.remove(C.DB_PATH)
        self.assertIsNone(self.M.snapshot(TODAY))
        self.assertFalse(os.path.exists(self.snap))

    def test_daily_swallows_errors(self):
        with mock.patch.object(self.M, "snapshot", side_effect=RuntimeError("x")), \
                mock.patch.object(self.M, "rotate_log") as rl, mock.patch("traceback.print_exc"):
            self.M.daily(TODAY)
            rl.assert_called_once()


class RotateTest(Base):
    def write_log(self, mb):
        with open(self.log, "wb") as f:
            f.write(b"a" * int(mb * 1024 * 1024 + 1))

    def gz(self, n):
        return "%s.%d.gz" % (self.log, n)

    def test_noop_missing_or_under_threshold(self):
        self.assertFalse(self.M.rotate_log())
        self.write_log(4)
        with mock.patch.object(self.M, "_is_stdout", return_value=True), mock.patch.object(self.M, "_reopen") as ro:
            self.assertFalse(self.M.rotate_log())
            ro.assert_not_called()
        self.assertEqual(os.listdir(self.tmp.name), ["server.log"])

    def test_noop_when_fd1_not_log(self):
        self.write_log(6)
        with mock.patch.object(self.M, "_reopen") as ro:   # 测试进程的 fd 1 不是这个文件，真实 _is_stdout 判否
            self.assertFalse(self.M.rotate_log())
            ro.assert_not_called()
        self.assertEqual(os.path.getsize(self.log), 6 * 1024 * 1024 + 1)

    def test_is_stdout_real_fd(self):
        self.write_log(0)
        self.assertFalse(self.M._is_stdout(os.stat(self.log)))
        self.assertTrue(self.M._is_stdout(os.fstat(1)))

    def test_rotate_shifts_and_keeps_3(self):
        for n in (1, 2, 3):
            with gzip.open(self.gz(n), "wb") as f:
                f.write(b"old%d" % n)
        self.write_log(6)
        reopen = lambda p: open(p, "ab").close()
        with mock.patch.object(self.M, "_is_stdout", return_value=True), \
                mock.patch.object(self.M, "_reopen", side_effect=reopen) as ro:
            self.assertTrue(self.M.rotate_log())
            ro.assert_called_once_with(self.log)
        self.assertEqual(sorted(os.listdir(self.tmp.name)),
                         ["server.log", "server.log.1.gz", "server.log.2.gz", "server.log.3.gz"])
        self.assertEqual(os.path.getsize(self.log), 0)
        with gzip.open(self.gz(1)) as f:
            self.assertEqual(len(f.read()), 6 * 1024 * 1024 + 1)
        with gzip.open(self.gz(2)) as f:
            self.assertEqual(f.read(), b"old1")
        with gzip.open(self.gz(3)) as f:
            self.assertEqual(f.read(), b"old2")          # 原 .3 已丢

    def test_reopen_redirects_fds(self):
        """真跑 _reopen：在子进程里，fd 1 / 2 换到新文件。"""
        import subprocess
        code = ("import os,sys;from dashboard import maint;p=sys.argv[1];maint._reopen(p);"
                "print('out',flush=True);os.write(2,b'err\\n')")
        subprocess.run([sys.executable, "-c", code, self.log], cwd=ROOT, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(self.log) as f:
            self.assertEqual(f.read(), "out\nerr\n")


class ConfigTest(unittest.TestCase):
    def base(self):
        import yaml
        with open(os.path.join(ROOT, "dashboard.example.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_defaults(self):
        self.assertEqual(C.validate(self.base())["maintenance"], {"snapshot_keep": 14, "log_max_mb": 5, "log_keep": 3})

    def test_reject_bad(self):
        for v in (0, 400, "7", True, 1.5):
            c = self.base()
            c["maintenance"] = {"snapshot_keep": v}
            with self.assertRaises(C.ConfigError):
                C.validate(c)


if __name__ == "__main__":
    unittest.main()
