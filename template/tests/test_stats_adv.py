"""序贯阈值 / BH / CUPED / 分群：统计正确性 + 协变量窗口 + 重算节奏 + SQL 只出聚合。"""
import copy
import math
import os
import random
import sys
import unittest
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dashboard import config as C, jobs, sqlgen, stats  # noqa: E402

with open(os.path.join(ROOT, "dashboard.example.yaml"), encoding="utf-8") as fh:
    CFG = yaml.safe_load(fh)


def use():
    C.use(copy.deepcopy(CFG))


class SeqTest(unittest.TestCase):
    def setUp(self):
        use()

    def test_single_look_is_196(self):
        self.assertAlmostEqual(stats.gs_bounds([1.0])[0], 1.96, places=2)

    def test_obf_two_looks(self):
        b = stats.gs_bounds([0.5, 1.0])
        self.assertAlmostEqual(b[0], 2.80, delta=0.05)    # Lan-DeMets OBF 型常用表值 2.797 / 1.977
        self.assertAlmostEqual(b[1], 1.98, delta=0.02)

    def test_overall_alpha_by_simulation(self):
        random.seed(1)
        fr = [3 / 14, 7 / 14, 1.0]
        for spend in ("obf", "pocock"):
            b, hit, n = stats.gs_bounds(fr, 0.05, spend), 0, 40000
            for _ in range(n):
                w, t0, h = 0.0, 0.0, False
                for t, c in zip(fr, b):
                    w += random.gauss(0, math.sqrt(t - t0))
                    t0 = t
                    h = h or abs(w / math.sqrt(t)) >= c
                hit += h
            self.assertAlmostEqual(hit / n, 0.05, delta=0.006, msg=spend)

    def test_seq_bound_by_day(self):
        self.assertGreater(jobs.seq_bound(3, 14, False, False), 4.0)        # 收益侧第 3 天很严
        self.assertLess(jobs.seq_bound(3, 14, False, True), 2.5)            # 变差侧第 3 天仍能报警
        self.assertAlmostEqual(jobs.seq_bound(14, 14, True, False), 1.98, delta=0.02)
        self.assertAlmostEqual(jobs.seq_bound(2, 14, True, False), 1.96, delta=0.01)   # 冻结前没看过节点


class BHTest(unittest.TestCase):
    def setUp(self):
        use()

    def test_bh(self):
        self.assertEqual(stats.bh_pass([0.01, 0.04, 0.03, 0.2, None], 0.05), [True, False, False, False, False])
        self.assertEqual(stats.bh_pass([0.01, 0.02, 0.03], 0.05), [True, True, True])
        self.assertEqual(stats.bh_pass([], 0.05), [])

    def test_adjust_reads_keeps_original(self):
        e = {"start": 20261001, "end": 20261014, "last_end": 20261003, "status": "active"}
        c = lambda z: {"z": z, "p": math.erfc(abs(z) / math.sqrt(2)), "diff": z, "read": "显著负向" if abs(z) > 1.96 else None}
        t = lambda k, role, z, lower=False: {"key": k, "kind": "ratio", "role": role, "lower_is_better": lower,
                                            "rows": [{"type": "cum", "days": 3, "vs": {"2": c(z)}}]}
        ts = jobs.adjust_reads(e, [t("ns", "north_star", -2.1), t("nr", "guardrail", 2.1, True), t("a", "process", 0.1),
                                   t("b", "process", 0.2), t("d", "process", 0.3)])
        north, guard = ts[0]["rows"][0]["vs"]["2"], ts[1]["rows"][0]["vs"]["2"]
        self.assertEqual(north["read"], "显著负向")              # 原判读不动
        self.assertIsNone(north["adj"]["bh"])                    # 北极星不进 BH
        self.assertFalse(north["adj"]["seq"])                    # 第 3 天变差侧阈值约 2.4
        self.assertFalse(guard["adj"]["bh"])                     # 护栏 p≈0.036，4 项一起修正后不过
        self.assertFalse(guard["adj"]["ok"])
        self.assertIn("BH", guard["adj"]["why"])


class CupedTest(unittest.TestCase):
    def _sim(self, effect, rho_on, seed):
        random.seed(seed)
        out = {}
        for g, eff in (("c", 0.0), ("t", effect)):
            st = {"n": 0}
            for _ in range(4000):
                base = random.gauss(5, 2)
                x = max(base + (random.gauss(0, 1) if rho_on else random.gauss(0, 3)) + eff, 0)
                a = max(base + random.gauss(0, 1), 0) if rho_on else max(random.gauss(5, 2), 0)
                v = {"x": x, "y": 1.0, "a": a, "b": 1.0}
                st["n"] += 1
                for i, p in enumerate("xyab"):
                    st["s" + p] = st.get("s" + p, 0) + v[p]
                    for q in "xyab"[i:]:
                        st["s" + p + q] = st.get("s" + p + q, 0) + v[p] * v[q]
            out[g] = stats.cuped_group(st)
        return stats.cuped_compare(out["c"], out["t"])

    def test_variance_cut_when_correlated(self):
        r = self._sim(0.2, True, 3)
        self.assertGreater(r["var_cut"], 0.5)
        self.assertAlmostEqual(r["diff"], 0.2, delta=0.1)

    def test_no_cut_when_uncorrelated(self):
        self.assertLess(self._sim(0.0, False, 4)["var_cut"], 0.02)

    def test_raw_matches_plain_delta(self):
        r = self._sim(0.2, True, 5)
        self.assertAlmostEqual(r["raw_z"], r["raw_diff"] / r["raw_se"])
        self.assertLess(r["se"], r["raw_se"])

    def test_unbiased_under_null(self):
        zs = [self._sim(0.0, True, s)["z"] for s in range(30)]
        self.assertLess(abs(sum(zs) / len(zs)), 0.5)

    def test_missing_metric_no_crash(self):
        self.assertIsNone(stats.cuped_group({}))


class HetTest(unittest.TestCase):
    def test_q(self):
        q, df, p = stats.heterogeneity([(0.01, 0.005), (0.01, 0.005)])
        self.assertAlmostEqual(q, 0.0)
        self.assertGreater(p, 0.99)
        self.assertLess(stats.heterogeneity([(0.05, 0.005), (-0.05, 0.005)])[2], 0.001)
        self.assertIsNone(stats.heterogeneity([(0.1, 0.01)]))


G = lambda *ids: [{"id": i, "role": "control" if n == 0 else "treatment"} for n, i in enumerate(ids)]


class CupedWindowTest(unittest.TestCase):
    def setUp(self):
        use()

    def test_window_cut_by_previous_round(self):
        e = {"id": 2, "name": "新一轮", "scope": "ALL", "launch": 20250316, "start": 20250317, "end": 20250330, "groups": G("G1", "G2")}
        prev = {"id": 1, "name": "上一轮", "scope": "ALL", "start": 20250314, "end": 20250315, "last_end": 20250315, "groups": G("G1", "G2")}
        other = {"id": 3, "name": "别的范围", "scope": "MOBILE", "start": 20250310, "end": 20250320, "last_end": None, "groups": G("G1")}
        p0, p1, why = jobs.cuped_window(e, [e, prev, other])
        self.assertGreater(p0, p1)                                # 上一轮跑到上线前一天 → 没有干净窗口
        self.assertIn("上一轮", why)
        prev["end"] = prev["last_end"] = 20250311                 # 剩 4 天 < 7 天，也不做
        self.assertGreater(*jobs.cuped_window(e, [e, prev])[:2])
        prev["end"] = prev["last_end"] = 20250228                 # 剩 15 天：截短后照做
        self.assertEqual(jobs.cuped_window(e, [e, prev])[:2], (20250301, 20250315))

    def test_window_untouched_and_off(self):
        e = {"id": 6, "name": "x", "scope": "ALL", "launch": 20250610, "start": 20250611, "end": 20250624, "groups": G("G1", "G2")}
        self.assertEqual(jobs.cuped_window(e, [e])[:2], (20250513, 20250609))
        C.cfg["experiment"]["cuped_pre_days"] = 0
        p0, p1, why = jobs.cuped_window(e, [e])
        self.assertGreater(p0, p1)
        self.assertIn("关闭", why)


class HeavyRecomputeTest(unittest.TestCase):
    """CUPED / 分群只在跨节点、冻结、缺失、失败时重算；分布每次都算。"""
    def setUp(self):
        use()

    def _pending(self, have_end, d1, end=20261114, vis_error=None):
        m = dict(C.cfg["metrics"][0])
        mm = {m.pop("key"): m}
        e = {"id": 1, "name": "x", "scope": "ALL", "start": 20261101, "end": end, "launch": 20261031, "status": "active",
             "groups": G("G1", "G2"), "metric_keys": list(mm), "daily_keys": [], "subs": [], "caps": {}, "vis_error": vis_error,
             "last_end": have_end,
             "results": [{"metric_key": k, "grp": "G1", "payload": {}, "run_end": have_end} for k in ("@dist", "@cuped", "@seg")]}
        got = {}

        class FakeSrc:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def dicts(s, *a, **k): return []
        with mock.patch.object(jobs, "_src", lambda *a: FakeSrc()), mock.patch.object(jobs.store, "metric_map", return_value=mm), \
                mock.patch.object(jobs.store, "update_experiment", side_effect=lambda i, **kw: got.update(kw)), \
                mock.patch.object(jobs.store, "save_exp_results"), mock.patch.object(jobs, "submit", return_value=0), \
                mock.patch.object(jobs.store, "experiments", return_value=[]), mock.patch.object(jobs, "yesterday", return_value=d1):
            jobs._run_experiment(e, lambda s: None)
        return [k for k in got.get("pending", []) if k.startswith("@") and k != "@cum"]

    def test_same_bucket_skips_heavy(self):
        self.assertEqual(self._pending(20261104, 20261105), ["@dist"])          # 第 4 → 5 天：同在判止损段

    def test_day3_node_recomputes(self):
        self.assertEqual(self._pending(20261101, 20261103), ["@dist", "@cuped", "@seg"])   # 第 1 天算过，到第 3 天要重算

    def test_cross_node_recomputes(self):
        self.assertEqual(self._pending(20261106, 20261107), ["@dist", "@cuped", "@seg"])   # 第 6 → 7 天

    def test_frozen_recomputes(self):
        self.assertEqual(self._pending(20261108, 20261109, end=20261109), ["@dist", "@cuped", "@seg"])

    def test_vis_error_recomputes(self):
        self.assertEqual(self._pending(20261104, 20261105, vis_error="分群下钻：x"), ["@dist", "@cuped", "@seg"])


class SqlTest(unittest.TestCase):
    def setUp(self):
        use()
        self.ms = [{"key": m["key"], "name": m["name"], "def": {k: v for k, v in m.items() if k != "key"}}
                   for m in C.cfg["metrics"] if m.get("kind", "ratio") == "ratio"]
        self.caps = {"plays_per_user": 20}

    def _only_aggregates_out(self, sql):
        head = sql.strip().split("FROM", 1)[0]
        self.assertNotIn("user_id", head)                          # 最外层不出设备列
        self.assertNotIn(" u,", head)
        self.assertTrue(sql.strip().splitlines()[-1].startswith("GROUP BY grp"))
        self.assertNotIn("SELECT *", sql.upper())

    def test_cuped_sql(self):
        sql = sqlgen.experiment_cuped_sql("ALL", ["G100", "G101"], 20250317, 20250321, self.ms, self.caps, [], 20250216, 20250315)
        self._only_aggregates_out(sql)
        self.assertIn("BETWEEN 20250216 AND 20250315", sql)
        self.assertIn("least(x_plays_per_user, 20.0)", sql)        # 上线前同一个 P99 封顶
        self.assertIn("SUM(xx_click_rate * COALESCE(px_click_rate, 0)) AS sxa_click_rate", sql)

    def test_seg_sql(self):
        sg = {"column": "region", "label": "地区", "top": 2}
        sql = sqlgen.experiment_seg_sql("ALL", ["G100", "G101"], 20250611, 20250624, self.ms, self.caps, [], sg)
        self._only_aggregates_out(sql)
        self.assertIn("rk <= 2", sql)
        self.assertNotIn("s.su IS NULL", sql)                     # 「JOIN 不上」用标记列判断，输出列不引用设备列
        with self.assertRaises(sqlgen.MetricError):
            sqlgen.experiment_seg_sql("ALL", ["G100"], 1, 2, self.ms, self.caps, [], {"column": "position", "top": 3})

    def test_cuped_skips_device_scope(self):
        d = dict(self.ms[0]["def"], device_scope={"event": "result_view", "agg": "uv"})
        self.assertEqual(sqlgen.cuped_metrics([{"key": "sc", "name": "x", "def": d}]), [])

    def test_config_rejects_bad_segment(self):
        c = copy.deepcopy(CFG)
        c["experiment"]["segments"] = [{"column": "position"}]
        with self.assertRaises(C.ConfigError):
            C.validate(c)


if __name__ == "__main__":
    unittest.main()
