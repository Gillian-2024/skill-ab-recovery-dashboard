"""看板模板的回归测试：配置校验、各方言 SQL、跨站防护、演示数据上的端到端读数。

运行：python3 -m unittest discover -s tests（在看板目录下）
端到端用例需要 duckdb；没装就跳过。
"""
import copy
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dashboard import config as C, jobs, sqlgen, store  # noqa: E402

EXAMPLE = os.path.join(ROOT, "dashboard.example.yaml")
try:
    import duckdb  # noqa: F401
    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


def example():
    with open(EXAMPLE, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class ConfigTest(unittest.TestCase):
    def test_example_valid(self):
        c = C.validate(example())
        self.assertEqual(c["funnel"]["anchor"], "query")
        self.assertIn(c["experiment"]["cadence"]["stop_loss"], c["experiment"]["cum_days"])

    def bad(self, mutate, needle):
        c = example()
        mutate(c)
        with self.assertRaises(C.ConfigError) as cm:
            C.validate(c)
        self.assertIn(needle, str(cm.exception))

    def test_rejects_public_host(self):
        self.bad(lambda c: c["server"].update(host="0.0.0.0"), "server.host")

    def test_rejects_unknown_skip(self):
        self.bad(lambda c: c["funnel"]["tiers"][2].update(skip=["search_open"]), "skip")

    def test_rejects_bad_cadence(self):
        self.bad(lambda c: c["experiment"].update(cadence={"check": 5, "stop_loss": 3, "interim": 7, "final": 14}), "cadence")

    def test_rejects_like_without_id(self):
        self.bad(lambda c: c["experiment"]["group"].update(pattern="%G%"), "{id}")

    def test_rejects_bad_column(self):
        self.bad(lambda c: c["table"]["columns"]["string"].append("a;drop"), "列名")


class SqlTest(unittest.TestCase):
    def setUp(self):
        self.base = example()

    def use(self, dialect, date_type="int"):
        c = copy.deepcopy(self.base)
        c["source"] = {"type": "sqlalchemy", "dialect": dialect}
        c["table"]["date_type"] = date_type
        C.use(c)

    def test_every_dialect_builds(self):
        for d in C.DIALECTS:
            self.use(d)
            sqlgen.flow_sql("ALL", 20261001, 20261007)
            sqlgen.breakdown_sql("ALL", 20261001, 20261007, ["NA"])
            mm = {m["key"]: m for m in C.cfg["metrics"]}
            ratio = [{"key": k, "name": m["name"], "def": m} for k, m in mm.items() if m["kind"] == "ratio"]
            sqlgen.experiment_ratio_sql("ALL", ["G100", "G101"], 20261001, 20261007, ratio, {"plays_per_user": 2.0})
            ds = sqlgen.experiment_dist_sql("ALL", ["G100", "G101"], 20261001, 20261003, ratio, {"plays_per_user": 2.0}, None,
                                            jobs.dist_keys(mm, list(mm)), [20261001, 20261002, 20261003])
            self.assertIn("e_20261003", ds)
            self.assertNotIn("UNION", ds.upper())
            self.assertEqual("q4_plays_per_user" in ds, d != "mysql")
            if d != "mysql":
                sqlgen.experiment_quantile_sql("ALL", ["G100", "G101"], 20261001, 20261007, {"key": "startup_ms", "def": mm["startup_ms"]})
            else:
                with self.assertRaises(sqlgen.MetricError):
                    sqlgen.pct_fn("x", 0.5)

    def test_date_literals(self):
        self.use("trino", "date")
        self.assertIn("CAST('2026-10-01' AS DATE)", sqlgen.where_dates(20261001, 20261007))
        self.use("spark", "string")
        C.cfg["table"]["date_format"] = "%Y%m%d"
        self.assertIn("'20261001'", sqlgen.where_dates(20261001, 20261007))

    def test_reserved_word_quoting(self):
        self.use("mysql")
        self.assertEqual(sqlgen.q("position"), "`position`")
        self.use("postgres")
        self.assertEqual(sqlgen.q("position"), '"position"')

    def test_filter_whitelist(self):
        self.use("duckdb")
        self.assertEqual(sqlgen.filter_sql(["entry", "=", "widget"]), "entry = 'widget'")
        self.assertEqual(sqlgen.filter_sql(["duration_ms", ">=", "0.9*item_duration_ms"]), "duration_ms >= 0.9 * item_duration_ms")
        for f in (["entry", "=", "x' OR '1'='1"], ["secret_col", "=", "a"], ["duration_ms", ">", "1; drop"], ["entry", "regex", "a"]):
            with self.assertRaises(sqlgen.MetricError):
                sqlgen.filter_sql(f)

    def test_subscope_and_group_whitelist(self):
        self.use("duckdb")
        with self.assertRaises(sqlgen.MetricError):
            sqlgen.split_view("ALL:XX")
        with self.assertRaises(sqlgen.MetricError):
            sqlgen.check_gid("G1' OR 1=1")
        self.assertEqual(sqlgen.split_view("ALL:NA"), ("ALL", ["NA"]))


class _Env(unittest.TestCase):
    """独立数据目录 + 演示数据。"""
    db = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dash-test-")
        c = example()
        c["data_dir"] = cls.tmp
        if HAS_DUCKDB:
            cls.db = os.path.join(cls.tmp, "demo.duckdb")
            out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "demo_data.py"), cls.db, "--users", "4000"],
                                 check=True, capture_output=True, text=True).stdout
            cls.marks = dict(kv.split("=") for kv in out.strip().splitlines()[-1].split())
            c["source"]["path"] = cls.db
        cls.cfg = c

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        C.use(copy.deepcopy(self.cfg))
        if os.path.exists(C.DB_PATH):
            os.remove(C.DB_PATH)
        store.init()


class SameOriginTest(_Env):
    def setUp(self):
        super().setUp()
        from dashboard.app import app
        self.c = app.test_client()
        self.base = "http://127.0.0.1:%d" % C.cfg["server"]["port"]

    def post(self, body, **headers):
        return self.c.post("/api/refresh", json=body, headers={"Host": "127.0.0.1:%d" % C.cfg["server"]["port"], **headers})

    def test_cross_site_rejected(self):
        body = {"start": 20261001, "end": 20261002}
        self.assertEqual(self.post(body, Origin="https://evil.example").status_code, 403)
        self.assertEqual(self.post(body, **{"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        r = self.c.post("/api/refresh", data="{}", headers={"Host": "127.0.0.1:%d" % C.cfg["server"]["port"], "Content-Type": "text/plain"})
        self.assertEqual(r.status_code, 415)

    def test_rebinding_host_rejected(self):
        self.assertEqual(self.c.get("/api/meta", headers={"Host": "evil.example:%d" % C.cfg["server"]["port"]}).status_code, 403)
        self.assertEqual(self.c.get("/api/meta", headers={"Host": "localhost:%d" % C.cfg["server"]["port"]}).status_code, 200)

    def test_same_origin_ok_and_date_order(self):
        h = {"Host": "127.0.0.1:%d" % C.cfg["server"]["port"], "Origin": self.base, "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.c.post("/api/refresh", json={"start": 20261002, "end": 20261001}, headers=h).status_code, 400)
        self.assertEqual(self.c.post("/api/refresh", json={"start": 20261001, "end": 20261002}, headers=h).status_code, 200)


def _run_queue():
    """同步跑完队列（测试里不起后台线程）。"""
    while True:
        j = store.next_job()
        if not j:
            return
        try:
            jobs.RUNNERS[j["kind"]](j["id"], j["params"])
            store.finish_job(j["id"])
        except Exception as ex:
            store.finish_job(j["id"], str(ex))
            raise


@unittest.skipUnless(HAS_DUCKDB, "需要 duckdb")
class EndToEndTest(_Env):
    def test_funnel(self):
        end = int(self.marks["END"])
        d0 = jobs.ymd(jobs.parse(end) - dt.timedelta(days=2))
        jobs.submit("funnel", {"views": ["ALL", "ALL:NA"], "start": d0, "end": end})
        _run_queue()
        flow, bd, dau, st = store.funnel("ALL", d0, end)
        self.assertEqual(len(flow), 3)
        r = flow[end]
        # union 档 = 成员档路径到达之和（成员互斥）；widget 没有「打开搜索」
        self.assertEqual(r["in__query__all"], r["in__query__app"] + r["in__query__widget"])
        self.assertEqual(r["uv__open__widget"], 0)
        for k in ("query", "result", "imp", "click", "play"):
            self.assertLessEqual(r["in__%s__app" % k], r["uv__%s__app" % k])
        self.assertTrue(bd and dau[end] > 0 and st[end]["complete"])
        nflow, _, _, _ = store.funnel("ALL:NA", d0, end)
        self.assertLess(nflow[end]["uv__home__all"], r["uv__home__all"])

    def test_experiment_reads_planted_lift(self):
        o, end = int(self.marks["OPEN_DAY"]), int(self.marks["END"])
        keys = [m["key"] for m in C.cfg["metrics"]]
        eid = store.create_experiment("t", "ALL", [{"id": "G100", "role": "control", "label": "G100", "weight": 1},
                                                    {"id": "G101", "role": "treatment", "label": "G101", "weight": 1}],
                                      o, end, keys, daily_keys=["click_rate"], launch=o)
        jobs.submit("experiment", {"exp_id": eid})
        _run_queue()
        e = store.experiment(eid)
        self.assertEqual(e["status"], "frozen")
        self.assertFalse(e["pending"])
        v = jobs.experiment_view(e)
        self.assertGreater(v["srm"]["p"], 0.001)
        t = {x["key"]: x for x in v["tables"]}
        tot = lambda k: next(r for r in t[k]["rows"] if r["type"] == "total")["vs"]["G101"]
        self.assertEqual(tot("click_rate")["read"], "显著正向")       # 埋的 +12% 点击概率
        self.assertIsNone(tot("noresult_rate")["read"])              # 无结果与实验无关
        self.assertIn("plays_per_user", e["caps"])                   # P99 封顶取上线前
        self.assertTrue(any(r["type"] == "day" for r in t["click_rate"]["rows"]))
        self.assertTrue(any(r["type"] == "cum" and r["days"] == C.cfg["experiment"]["cadence"]["stop_loss"] for r in t["click_rate"]["rows"]))
        q = next(r for r in t["startup_ms"]["rows"] if r["type"] == "total")["groups"]["G101"]["q"]
        self.assertEqual(len(q), 2)
        # 分布 + 进组趋势：人均类指标出箱型图；各组进组累计 = 分流检查设备数
        self.assertEqual(v["dist_keys"], ["plays_per_user"])
        for g in ("G100", "G101"):
            d = v["dist"][g]
            self.assertEqual(sum(d["entry"].values()), v["srm"]["counts"][g])
            qs = d["metrics"]["plays_per_user"]["q"]
            self.assertEqual(qs, sorted(qs))
            # 封顶是单设备单天，窗口内按天累加：每设备取值 ≤ 封顶 × 天数
            self.assertLessEqual(qs[-1], e["caps"]["plays_per_user"] * len(d["entry"]))
        # 换基准：表格按新基准比，结论提要仍按对照组
        vb = jobs.experiment_view(e, "G101")
        self.assertEqual((vb["base"], vb["trts"]), ("G101", ["G100"]))
        cb = next(r for r in {x["key"]: x for x in vb["tables"]}["click_rate"]["rows"] if r["type"] == "total")["vs"]["G100"]
        self.assertAlmostEqual(cb["diff"], -tot("click_rate")["diff"])
        self.assertEqual(vb["summary"], v["summary"])
        sm = v["summary"]
        self.assertEqual(sm["state"], "ok")
        self.assertEqual(sm["head"]["stage"], "出结论")
        self.assertTrue(sm["groups"][0]["lines"][0].startswith("结果页点击渗透率显著正向"))


class StageTest(unittest.TestCase):
    def setUp(self):
        C.use(example())

    def test_nodes_follow_cadence(self):
        today = dt.date(2026, 10, 21)
        e = lambda start, end="20261231", status="active": {"start": start, "end": end, "status": status}
        self.assertEqual(jobs.stage_of(e("20261019"), today)["stage"], "查生效")     # 第 2 天
        self.assertEqual(jobs.stage_of(e("20261018"), today)["stage"], "判止损")     # 第 3 天
        self.assertEqual(jobs.stage_of(e("20261014"), today)["stage"], "中期读数")   # 第 7 天
        self.assertEqual(jobs.stage_of(e("20261007"), today)["stage"], "出结论")     # 第 14 天
        self.assertEqual(jobs.stage_of(e("20261019", "20261019"), today)["stage"], "出结论")
        self.assertEqual(jobs.stage_of(e("20261030"), today)["stage"], "未开窗")


if __name__ == "__main__":
    unittest.main()
