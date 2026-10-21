#!/usr/bin/env python3
"""生成一份合成事件表（DuckDB 文件），带一个已知效果的假实验，用来验证看板读数是否正确。

用法：python3 tools/demo_data.py <输出 .duckdb 路径> [--days 21] [--users 6000] [--seed 7]

数据形态（全部是虚构的，不对应任何真实产品）：
  一张宽表 events(user_id, event_date INT YYYYMMDD, event_name, entry, region, platform, position, duration_ms,
                 item_duration_ms, is_valid, exp_groups)
  漏斗：home_view → search_open → query_submit → result_view → item_impression → item_click → play_start → play_end
  entry ∈ (in_app, widget)：widget 入口跳过 search_open（直达）
  实验：开窗日起，用户按 hash 均分到 G100（对照）/ G101（实验）；实验组 item_click 概率 × (1 + LIFT)
  exp_groups 形如 ",G100," —— 演示 like 匹配；分组只打在结果页相关事件上
"""
import argparse
import csv
import datetime as dt
import hashlib
import os
import random

LIFT = 0.12          # 实验组点击概率的相对提升（已知真值，用于验收）
OPEN_DAY = 8         # 第几天开窗（前面几天是上线前基线，用来算 P99 封顶）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--users", type=int, default=6000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    import duckdb

    rnd = random.Random(a.seed)
    end = dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=a.days - 1)
    open_day = start + dt.timedelta(days=OPEN_DAY - 1)
    regions = ["NA", "EU", "AP"]
    users = [("u%05d" % i, rnd.choice(regions), rnd.choice(["android", "ios"]), rnd.random()) for i in range(a.users)]
    rows = []
    for k in range(a.days):
        day = start + dt.timedelta(days=k)
        d = int(day.strftime("%Y%m%d"))
        in_exp = day >= open_day
        weekend = day.weekday() >= 5
        for uid, reg, plat, act in users:
            if rnd.random() > 0.35 + 0.4 * act + (0.05 if weekend else 0):
                continue
            arm = ("G101" if int(hashlib.md5(uid.encode()).hexdigest(), 16) % 2 else "G100") if in_exp else None
            tag = ",%s," % arm if arm else None
            ev = lambda name, entry=None, **kw: rows.append((uid, d, name, entry, reg, plat, kw.get("position"), kw.get("dur"),
                                                             kw.get("idur"), kw.get("valid"), kw.get("tag")))
            ev("home_view")
            if rnd.random() > 0.55:
                continue
            entry = "widget" if rnd.random() < 0.3 else "in_app"
            if entry == "in_app":
                ev("search_open", entry)
                if rnd.random() > 0.8:
                    continue
            ev("query_submit", entry)
            if rnd.random() > 0.9:
                continue
            ev("result_view", entry, tag=tag)
            if rnd.random() < 0.05:
                ev("no_result", entry, tag=tag)
                continue
            n_imp = rnd.randint(3, 12)
            for p in range(1, n_imp + 1):
                ev("item_impression", entry, position=p, tag=tag)
            p_click = 0.30 * (1 + LIFT if arm == "G101" else 1)
            if rnd.random() > p_click:
                continue
            clicks = 1 + (rnd.random() < 0.3) + (rnd.random() < 0.1)
            for _ in range(clicks):
                pos = min(n_imp, 1 + int(rnd.expovariate(0.6)))
                ev("item_click", entry, position=pos, tag=tag)
                if rnd.random() < 0.93:
                    ev("play_start", entry, dur=int(rnd.lognormvariate(6.2, 0.5)), tag=tag)
                    idur = rnd.choice([15000, 30000, 60000, 120000])
                    watched = int(min(idur, rnd.expovariate(1 / (idur * 0.55))))
                    ev("play_end", entry, dur=watched, idur=idur, valid=watched > 3000, tag=tag)
    if os.path.exists(a.out):
        os.remove(a.out)
    # 逐行 INSERT 很慢：先写临时 CSV，再整表导入
    tmp = a.out + ".csv"
    with open(tmp, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    con = duckdb.connect(a.out)
    con.execute("""CREATE TABLE events (user_id VARCHAR, event_date INTEGER, event_name VARCHAR, entry VARCHAR, region VARCHAR,
                   platform VARCHAR, position INTEGER, duration_ms BIGINT, item_duration_ms BIGINT, is_valid BOOLEAN, exp_groups VARCHAR)""")
    con.execute("INSERT INTO events SELECT * FROM read_csv(?, header = false, delim = ',', quote = '\"', auto_detect = false, columns = {'user_id': 'VARCHAR', 'event_date': 'INTEGER', "
                "'event_name': 'VARCHAR', 'entry': 'VARCHAR', 'region': 'VARCHAR', 'platform': 'VARCHAR', 'position': 'INTEGER', "
                "'duration_ms': 'BIGINT', 'item_duration_ms': 'BIGINT', 'is_valid': 'BOOLEAN', 'exp_groups': 'VARCHAR'})", [tmp])
    os.remove(tmp)
    con.execute("CREATE TABLE daily_active AS SELECT event_date, region, user_id FROM events WHERE event_name = 'home_view' GROUP BY 1, 2, 3")
    con.close()
    print("写入 %d 行事件，%s ~ %s；实验开窗 %s，组 G100（对照）/ G101（实验），点击概率真实提升 +%d%%"
          % (len(rows), start, end, open_day, LIFT * 100))
    print("OPEN_DAY=%s LAUNCH=%s END=%s" % (open_day.strftime("%Y%m%d"), open_day.strftime("%Y%m%d"), end.strftime("%Y%m%d")))


if __name__ == "__main__":
    main()
