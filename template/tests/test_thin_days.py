"""逐日样本不足判定 thin_days：实验中途关停后的残留流量天，逐日图不画点（纯函数，不碰库、不连数据源）。

运行：python3 -m unittest discover -s tests（在看板目录下）
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dashboard import jobs  # noqa: E402

DAYS = ["202601%02d" % d for d in range(11, 22)]


def _day(ns):
    """{日期: 充分统计量}；只有 n 参与判定，其余字段随便填。"""
    return {d: {"n": n, "sx": n // 4, "sy": n, "sxx": n // 4, "syy": n, "sxy": n // 4} for d, n in zip(DAYS, ns)}


# 合成数据：第 5 天（0115）中途关停只剩半天量，之后 6 天只剩零星残留流量
STOPPED = {"G100": _day([5000, 5020, 5050, 5100, 2900, 26, 30, 18, 7, 5, 6]),
           "G101": _day([5030, 5080, 5110, 5150, 2880, 24, 31, 17, 11, 8, 4])}


class ThinDaysTest(unittest.TestCase):
    TAIL = {"20260116", "20260117", "20260118", "20260119", "20260120", "20260121"}

    def test_stopped_tail(self):
        # 单日最高 5100 → 低于 1530 的天：关停后的 0116~0121 全部；0115（2900，关停当天半天量）保留
        self.assertEqual(jobs.thin_days({"G100": STOPPED["G100"]}), self.TAIL)
        self.assertEqual(jobs.thin_days(STOPPED), self.TAIL)

    def test_x_ignored(self):
        x = _day([2, 1, 2, 1, 600, 1, 1, 1, 1, 1, 1])   # 跨组冲突设备 X 里除 0115 外都 < 30% 最高值，也不该算
        self.assertEqual(jobs.thin_days({"X": x}), set())
        self.assertEqual(jobs.thin_days(dict(STOPPED, X=x)), self.TAIL)

    def test_uniform_none(self):
        self.assertEqual(jobs.thin_days({"G100": _day([5000] * 11), "G101": _day([5100] * 11)}), set())
        self.assertEqual(jobs.thin_days({}), set())


if __name__ == "__main__":
    unittest.main()
