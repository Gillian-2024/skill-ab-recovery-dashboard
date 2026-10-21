#!/usr/bin/env python3
"""校验 dashboard.yaml，并可选连上数据源做一次接入自检。

用法（在看板目录下）：
  python3 tools/check_config.py                 # 只校验配置结构 + 预置指标
  python3 tools/check_config.py --probe         # 再连数据源，查昨天各步事件量和分组列覆盖
  python3 tools/check_config.py --probe --date 20261019 --scope ALL
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "dashboard.yaml"))
    ap.add_argument("--probe", action="store_true", help="连接数据源做一次自检查询")
    ap.add_argument("--date", type=int, help="自检日期 YYYYMMDD，默认昨天")
    ap.add_argument("--scope", help="自检范围 key，默认第一个")
    a = ap.parse_args()
    os.environ["DASH_CONFIG"] = a.config
    from dashboard import app as A, config as C, jobs, sources, sqlgen

    try:
        c = C.load(a.config)
        for m in c["metrics"]:
            sqlgen.validate_metric({k: v for k, v in m.items() if k != "key"})
        sqlgen.flow_sql(c["scopes"][0]["key"], 20260101, 20260101)
    except Exception as ex:
        print("✗ 配置有误：%s" % ex)
        return 1
    f = c["funnel"]
    print("✓ 配置通过：%d 个范围，%d 档，%d 步漏斗（+%d 补充步），%d 个预置指标，取数通道 %s / %s"
          % (len(c["scopes"]), len(f["tiers"]), len(f["steps"]), len(f["extra_steps"]), len(c["metrics"]),
             c["source"]["type"], c["source"]["dialect"]))
    if not a.probe:
        return 0
    A._load_env()
    if not sources.auth_ok():
        print("✗ 数据源凭据未就位（sqlalchemy 看 .env 里的连接串；command 看 auth_file）")
        return 2
    d = a.date or jobs.yesterday()
    sc = a.scope or c["scopes"][0]["key"]
    sql = sqlgen.probe_sql(sc, d)
    try:
        with sources.open_source(log=lambda s: None) as src:
            rows = [{k.lower(): v for k, v in r.items()} for r in src.dicts(sql, "接入自检")]
    except Exception as ex:
        print("✗ 自检查询失败：%s\n--- SQL ---%s" % (ex, sql))
        return 3
    got = {r["ev"]: r for r in rows}
    ok = True
    print("\n%s 范围 %s 的事件量：" % (d, sc))
    for s in C.all_steps():
        r = got.get(s["event"])
        if not r:
            ok = False
            print("  ✗ %-10s %-24s 没有数据（事件名写错？或这天还没落表）" % (s["key"], s["event"]))
        else:
            print("  ✓ %-10s %-24s %10s 条 %9s 设备  分组列非空 %5.1f%%"
                  % (s["key"], s["event"], format(int(r["n"]), ","), format(int(r["uv"]), ","), 100 * int(r["with_group"]) / max(int(r["n"]), 1)))
    idf = set(c["experiment"]["identify_events"])
    if idf and not any(int((got.get(e) or {}).get("with_group") or 0) for e in idf):
        print("  ⚠ identify_events 里的事件这天都没带分组信息；没有实验在跑时属正常，否则检查 experiment.group.column")
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
