"""健壮性：指标定义缺字段报 400、删指标不假成功、队列实验优先、日更线程不因异常退出、数据源连通预检（socket 已 mock，不碰真网络）。

运行：python3 -m unittest discover -s tests（在看板目录下）
"""
import copy
import datetime as dt
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dashboard import config as C, jobs, sources, sqlgen, store  # noqa: E402

EXAMPLE = os.path.join(ROOT, "dashboard.example.yaml")
TODAY = dt.date.today()


def example():
    with open(EXAMPLE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def at(h, m=0):
    return dt.datetime.combine(TODAY, dt.time(h, m))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        c = example()
        c["data_dir"] = self.tmp.name
        C.use(c)
        store.init()
        self.cfg = c

    def tearDown(self):
        self.tmp.cleanup()

    def client(self):
        from dashboard.app import app
        port = C.cfg["server"]["port"]
        h = {"Host": "127.0.0.1:%d" % port, "Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % port, "Sec-Fetch-Site": "same-origin"}
        return app.test_client(), h


class MetricTest(Base):
    def test_missing_fields_400(self):
        c, h = self.client()
        for body, needle in (({"key": "x_rate", "name": "x"}, "numerator / denominator"),
                             ({"key": "x_q", "name": "x", "kind": "quantile", "column": "a", "quantiles": [0.5]}, "source"),
                             ({"key": "x_rate", "name": "x", "numerator": "a", "denominator": {}}, "numerator")):
            r = c.post("/api/metrics", json=body, headers=h)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(needle, r.get_json()["error"])
        with self.assertRaises(sqlgen.MetricError):
            sqlgen.validate_metric(["not", "a", "dict"])

    def test_delete_not_silent(self):
        c, h = self.client()
        r = c.delete("/api/metrics/no_such", headers=h)
        self.assertEqual(r.status_code, 404)
        preset = C.cfg["metrics"][0]["key"]
        r = c.delete("/api/metrics/%s" % preset, headers=h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("预置指标不能删除", r.get_json()["error"])
        self.assertIn(preset, store.metric_map())
        d = {k: v for k, v in copy.deepcopy(C.cfg["metrics"][0]).items() if k != "key"}
        store.save_metric("my_copy", d)
        self.assertEqual(c.delete("/api/metrics/my_copy", headers=h).status_code, 200)
        self.assertNotIn("my_copy", store.metric_map())


class QueueTest(Base):
    def test_experiment_first(self):
        f1 = store.enqueue("funnel", {"start": 1, "end": 1})
        e1 = store.enqueue("experiment", {"exp_id": 1})
        f2 = store.enqueue("funnel", {"start": 2, "end": 2})
        x1 = store.enqueue("exp_followup", {"exp_id": 1})
        order = []
        while True:
            j = store.next_job()
            if not j:
                break
            order.append(j["id"])
            store.finish_job(j["id"])
        self.assertEqual(order, [e1, x1, f1, f2])


class DailyTest(Base):
    """日更：异常不退出线程；配了 healthcheck 时连不上不提交，恢复后下一分钟补上（过了重试截止也补）。"""

    def setUp(self):
        super().setUp()
        self.calls = []
        self.sd = mock.patch.object(jobs, "schedule_daily", side_effect=lambda force: self.calls.append(force) or True)
        self.sd.start()
        jobs.NET_DOWN = ""

    def tearDown(self):
        self.sd.stop()
        jobs.NET_DOWN = ""
        super().tearDown()

    def step(self, now, st, why=""):
        with mock.patch.object(sources, "reachable", return_value=why):
            jobs.daily_step(now, st)

    def test_tick_survives_exception(self):
        class Stop(Exception):
            pass
        with mock.patch.object(jobs, "daily_step", side_effect=RuntimeError("boom")), \
                mock.patch("dashboard.maint.daily"), mock.patch.object(jobs.traceback, "print_exc") as pe, \
                mock.patch.object(jobs.time, "sleep", side_effect=Stop):
            with self.assertRaises(Stop):                                 # 走到了 sleep：异常没把线程带走
                jobs.daily_tick()
        pe.assert_called()

    def test_force_waits_for_net(self):
        st = {"run_day": None, "retry": 0, "net_wait": False}
        self.step(at(9, 0), st, "DNS 解析不了 db")
        self.assertEqual(self.calls, [])                                  # 不到点：连都不查
        self.assertEqual(jobs.NET_DOWN, "")
        self.step(at(9, 40), st, "DNS 解析不了 db")
        self.assertEqual(self.calls, [])                                  # 连不上：不提交
        self.assertIsNone(st["run_day"])
        self.assertEqual(jobs.NET_DOWN, "DNS 解析不了 db")
        self.step(at(21, 5), st)                                           # 晚上才恢复：照样整批提交
        self.assertEqual(self.calls, [True])
        self.assertEqual(st["run_day"], TODAY)
        self.assertEqual(jobs.NET_DOWN, "")
        self.step(at(21, 6), st)
        self.assertEqual(self.calls, [True])

    def test_retry_after_net_back_past_cutoff(self):
        st = {"run_day": TODAY, "retry": time.time() - 3 * 3600, "net_wait": False}
        self.step(at(19, 50), st, "连不上 db:5432")
        self.assertEqual(self.calls, [])
        self.step(at(20, 30), st)                                          # 过了截止，但这次是网络耽误的：补
        self.assertEqual(self.calls, [False])
        self.step(at(20, 31), st)
        self.assertEqual(self.calls, [False])                             # 回到正常节奏

    def test_health_reports_net_down(self):
        c, h = self.client()
        jobs.NET_DOWN = "连不上 db:5432（timed out）"
        self.assertEqual(c.get("/api/health", headers=h).get_json()["net_down"], "连不上 db:5432（timed out）")


class ReachableTest(Base):
    def test_not_configured_skips(self):
        with mock.patch.object(sources.socket, "getaddrinfo") as g:
            self.assertEqual(sources.reachable(), "")
            g.assert_not_called()

    def test_reasons_and_open_source(self):
        C.cfg["source"]["healthcheck"] = "db.example.com:5432"
        with mock.patch.object(sources.socket, "getaddrinfo", side_effect=OSError(8, "nodename nor servname")):
            self.assertTrue(sources.reachable().startswith("DNS 解析不了 db.example.com"))
            with self.assertRaises(sources.SourceError) as cm:
                sources.open_source()
            self.assertIn("VPN", str(cm.exception))
        with mock.patch.object(sources.socket, "getaddrinfo", return_value=[]), \
                mock.patch.object(sources.socket, "create_connection", side_effect=OSError(60, "timed out")):
            self.assertTrue(sources.reachable().startswith("连不上 db.example.com:5432"))
        with mock.patch.object(sources.socket, "getaddrinfo", return_value=[]), \
                mock.patch.object(sources.socket, "create_connection"):
            self.assertEqual(sources.reachable(), "")

    def test_config_validation(self):
        for ok in ("db.example.com:443", "10.0.0.1:5432"):
            c = example()
            c["source"]["healthcheck"] = ok
            C.validate(c)
        for bad in ("db.example.com", "http://db:443", "db:port"):
            c = example()
            c["source"]["healthcheck"] = bad
            with self.assertRaises(C.ConfigError):
                C.validate(c)


if __name__ == "__main__":
    unittest.main()
