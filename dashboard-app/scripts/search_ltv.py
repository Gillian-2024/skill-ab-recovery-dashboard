"""搜索业务月度 LTV（只 SG）：python -m scripts.search_ltv D0 [--dry]，在仓库根目录跑。

口径（飞书《搜索实验指标》「附：LT7、ARPU、LTV」）：
- 搜索业务月收入 = D0+1 ~ D0+30 每个北京日：设备日流内 5 个广告位计费收入 × 当日搜索播放 / 全部播放（play_close，搜索 from = config.PLAY_FROM）
- 每搜索用户月 LTV = D0 当天有搜索结果页事件的设备，D0+1 ~ D0+30 的分摊收入 ÷ 设备数（不含 D0）；ltv_d0 = D0 当天分摊收入 ÷ 当天有搜索播放的设备
- 广告侧按 log_time 切北京日、client_info 关联 gaid、剔占位 GAID；rev_adj 按占位 GAID 收入占比同比例补
整条 30 天在 KG 网关超时，所以分摊按 5 天一段、总收入按 10 天一段串行跑；结果缓存在 data/search_ltv/D0/，失败重跑只补缺的段。
打印的 dict 贴进 config.SEARCH_LTV。--dry 只写 SQL 不跑。"""
import json
import os
import sys

from dashboard import config as C
from dashboard.sqlgen import T, _RESULT4, _ymd, _bj_ms, _ad_daily_sql, REV_PIDS, REV_CATALOG


def _bd_case(days):
    lt = "CAST(log_time AS BIGINT)"
    return "\n".join("           WHEN %s >= %d AND %s < %d THEN %d" % (lt, _bj_ms(x), lt, _bj_ms(_ymd(x, 1)), x) for x in days)


def _days(b0, b1):
    out, d = [], b0
    while d <= b1:
        out.append(d)
        d = _ymd(d, 1)
    return out


def attr_sql(d0, b0, b1):
    """b0~b1 逐日：分摊收入（fee5 = 5 个流内 pid，fee = 全 1.313.*，只作参照）；c_* = D0 搜索用户那批设备。"""
    pf = ", ".join("'%s'" % f for f in C.PLAY_FROM)
    pids = ", ".join("'%s'" % p for p in REV_PIDS)
    return f"""
WITH coh AS (
  SELECT instance_id, 1 AS c
  FROM {T}
  WHERE date = {d0} AND cluster = 1 AND {_RESULT4}
  GROUP BY instance_id
),
p AS (
  SELECT instance_id, date, COUNT(*) AS vv, SUM(CASE WHEN `from` IN ({pf}) THEN 1 ELSE 0 END) AS svv
  FROM {T}
  WHERE date BETWEEN {b0} AND {b1} AND cluster = 1 AND event_name = 'play_close'
  GROUP BY instance_id, date
),
ids AS (
  SELECT instance_id, date, client_info
  FROM {T}
  WHERE date BETWEEN {b0} AND {b1} AND cluster = 1 AND event_name = 'play_close'
  GROUP BY instance_id, date, client_info
),
a AS (
  SELECT gaid, bd, SUM(fee) AS fee, SUM(fee5) AS fee5, MAX(zg) AS zg
  FROM (
    SELECT gaid, billing_fee AS fee, CASE WHEN pid IN ({pids}) THEN billing_fee ELSE 0 END AS fee5,
           CASE WHEN gaid = '' OR gaid LIKE '00000000%' THEN 1 ELSE 0 END AS zg,
           CASE
{_bd_case(_days(b0, b1))}
           END AS bd
    FROM ad_catalog.ads.ad_event_table
    WHERE date BETWEEN {_ymd(b0, -1)} AND {b1} AND source = 'billing_new' AND pid LIKE '1.000.%'
  ) t
  WHERE bd IS NOT NULL
  GROUP BY gaid, bd
),
r AS (
  SELECT i.instance_id, i.date, SUM(a.fee) AS fee, SUM(a.fee5) AS fee5
  FROM ids i JOIN a ON a.gaid = i.client_info AND a.bd = i.date
  WHERE a.zg = 0
  GROUP BY i.instance_id, i.date
)
SELECT p.date, COUNT(*) AS n, SUM(CASE WHEN p.svv > 0 THEN 1 ELSE 0 END) AS n_s, SUM(p.vv) AS vv, SUM(p.svv) AS svv,
       SUM(COALESCE(r.fee5, 0)) AS fee5, SUM(COALESCE(r.fee5, 0) * CAST(p.svv AS DOUBLE) / p.vv) AS fee5_attr,
       SUM(COALESCE(r.fee, 0)) AS fee, SUM(COALESCE(r.fee, 0) * CAST(p.svv AS DOUBLE) / p.vv) AS fee_attr,
       SUM(COALESCE(k.c, 0) * COALESCE(r.fee5, 0) * CAST(p.svv AS DOUBLE) / p.vv) AS c_fee5_attr,
       SUM(COALESCE(k.c, 0) * COALESCE(r.fee, 0) * CAST(p.svv AS DOUBLE) / p.vv) AS c_fee_attr,
       SUM(COALESCE(k.c, 0) * CASE WHEN p.svv > 0 THEN 1 ELSE 0 END) AS c_n_s
FROM p
LEFT JOIN r ON r.instance_id = p.instance_id AND r.date = p.date
LEFT JOIN coh k ON k.instance_id = p.instance_id
GROUP BY p.date"""


def total_sql(b0, b1):
    """b0~b1 每北京日 5 个流内 pid 计费收入总额（含占位 GAID，单列）：分摊占比的分母 + 占位补正。"""
    return f"""
SELECT bd, SUM(fee) AS s_fee, SUM(CASE WHEN zg = 1 THEN fee ELSE 0 END) AS s_fee_zg
FROM ({_ad_daily_sql("SG", b0, b1)}
) a
GROUP BY bd"""


def coh_sql(d0):
    return f"""
SELECT COUNT(*) AS n
FROM (SELECT instance_id FROM {T} WHERE date = {d0} AND cluster = 1 AND {_RESULT4} GROUP BY instance_id) c"""


def plan(d0):
    """查询清单：名字 → SQL。"""
    q = {"coh": coh_sql(d0), "attr_d0": attr_sql(d0, d0, d0)}
    for i in range(6):
        q["attr%d" % (i + 1)] = attr_sql(d0, _ymd(d0, 1 + 5 * i), _ymd(d0, 5 + 5 * i))
    for i in range(3):
        q["total%d" % (i + 1)] = total_sql(_ymd(d0, 1 + 10 * i), _ymd(d0, 10 + 10 * i))
    return q


def summarize(d0, out):
    rows = [r for i in range(6) for r in out["attr%d" % (i + 1)]]
    tot = [r for i in range(3) for r in out["total%d" % (i + 1)]]
    if len({int(r["date"]) for r in rows}) != 30 or len({int(r["bd"]) for r in tot}) != 30:
        raise RuntimeError("30 天没取齐：分摊 %d 天 / 总收入 %d 天" % (len(rows), len(tot)))
    s = lambda k, rs=rows: sum(float(r[k] or 0) for r in rs)
    app = s("s_fee", tot)
    adj = app / (app - s("s_fee_zg", tot))
    n_coh = int(out["coh"][0]["n"])
    d0r = out["attr_d0"][0]
    return {"month": "%s-%s" % (str(d0)[:4], str(d0)[4:6]), "d0": d0, "w0": _ymd(d0, 1), "w1": _ymd(d0, 30), "n_coh": n_coh,
            "rev": round(s("fee5_attr") / 1e5), "rev_adj": int(round(s("fee5_attr") * adj / 1e5, -1)), "app_rev": round(app / 1e5),
            "share": round(s("fee5_attr") / app, 4), "ltv": round(s("c_fee5_attr") / n_coh, 1),
            "ltv_d0": round(float(d0r["fee5_attr"]) / int(d0r["n_s"])), "svv_share": round(s("svv") / s("vv"), 3)}


def main():
    d0 = int(sys.argv[1])
    dry = "--dry" in sys.argv
    od = os.path.join(C.DATA_DIR, "search_ltv", str(d0))
    os.makedirs(od, exist_ok=True)
    q = plan(d0)
    for n, sql in q.items():
        open(os.path.join(od, n + ".sql"), "w").write(sql)
    if dry:
        print("SQL 已写到", od)
        return
    from dashboard.kg_client import KG
    C.CATALOG = REV_CATALOG["SG"]          # 收入表在 paimon；事件表名带全路径，同一 catalog 能读
    out = {}
    with KG(log=lambda s: print(s, flush=True)) as kg:
        for n, sql in q.items():
            f = os.path.join(od, n + ".json")
            if os.path.exists(f):
                out[n] = json.load(open(f))
                continue
            for t in (1, 2):                # KG 网关偶发超时 / DNS 失败，重试一次
                try:
                    out[n] = kg.dicts(sql, "搜索 LTV %s %s" % (d0, n))
                    break
                except Exception as ex:
                    print("⚠ %s 第 %d 次失败：%s" % (n, t, str(ex)[:300]), flush=True)
            if n not in out:
                raise SystemExit("%s 两次都失败，重跑本脚本只补缺的段" % n)
            json.dump(out[n], open(f, "w"), default=str)
            print("✓", n, flush=True)
    print(json.dumps(summarize(d0, out), ensure_ascii=False))


if __name__ == "__main__":
    main()
