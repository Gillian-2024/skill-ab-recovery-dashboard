# dashboard.yaml 字段说明

完整样例：`template/dashboard.example.yaml`（对应合成数据，能直接跑）。没写的字段用默认值。改完运行 `python3 tools/check_config.py` 校验。

所有 `where` 片段是**看板主人写的 SQL**，原样拼进查询；页面上用户能输入的东西（日期、细分值、分组号、指标筛选）都走白名单和格式校验，不会拼进原始字符串。

## 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | | 页面标题，默认「数据看板」 |
| `server.host` | | 只能 `127.0.0.1` / `localhost`（服务无鉴权） |
| `server.port` | | 默认 5050 |
| `data_dir` | | 本地 SQLite 结果库所在目录，默认 `data` |
| `labels` | | `{事件名或列名: 中文名}`，指标库选择框显示用 |
| `notes.funnel_foot` | | 漏斗页底部口径说明 |

## schedule

| 字段 | 默认 | 说明 |
|---|---|---|
| `daily_at` | `09:40` | 每天几点检查并拉数（要晚于数仓产出昨天数据的时间） |
| `backfill_days` | 14 | 每次检查最近几天，缺的 / 不完整的补拉 |
| `retry_gap_hours` | 2 | 昨天不完整时隔几小时重试 |
| `retry_until_hour` | 20 | 重试到几点为止 |

## source

| 字段 | 说明 |
|---|---|
| `type` | `duckdb` / `sqlalchemy` / `command` |
| `dialect` | `duckdb` / `postgres` / `mysql` / `trino` / `spark` / `clickhouse`。mysql 没有分位数函数：分位数指标和 P99 封顶不可用 |
| `max_rows` | 单条查询行数上限，默认 5000；到上限直接报错，防止用截断的结果 |
| `timeout_s` | 单条查询超时，默认 900。漏斗查询超时会自动对半拆窗口重试 |
| `auth_hint` | 凭据失效时页面顶部提示的处理办法（如「重新运行 xxx login」） |

**duckdb**：`path`（.duckdb 文件，只读打开）；可选 `views: {视图名: CSV/Parquet 路径或通配}`，启动时注册成临时视图，`table.name` 写视图名即可直接查文件。

**sqlalchemy**：连接串放 `.env` 的 `DASH_DB_URL`（或用 `url_env` 指定变量名）。需在看板 venv 里另装 `SQLAlchemy` 和驱动。

**command**：
```yaml
source:
  type: command
  dialect: trino
  command: ["python3", "~/bin/run_sql.py"]   # 参数列表，不经过 shell
  env: { QUERY_QUEUE: default }              # 可选，追加环境变量
  auth_file: ~/.config/mytool/token          # 可选，文件不存在时页面提示「凭据未配置」
```
约定：SQL 从 stdin 传入；stdout 打印 JSON，以下任一格式：
```json
[{"d": 20261019, "uv": 123}]
{"rows": [...], "task_id": "可选，进任务日志便于溯源"}
{"columns": ["d", "uv"], "data": [[20261019, 123]]}
{"error": "出错信息"}      // 或非 0 退出码 + stderr
{"rows": [...], "truncated": true}   // 结果被截断时必须声明，看板会报错而不是用残缺数据
```
错误信息里含 401 / 403 / token / login / 认证 等字样会被识别成凭据问题，页面顶部常驻提醒。

包装脚本骨架（把公司内部 CLI / HTTP 接口包一层）：
```python
#!/usr/bin/env python3
import json, subprocess, sys
sql = sys.stdin.read()
p = subprocess.run(["your-cli", "query", "--format", "json"], input=sql, capture_output=True, text=True)
if p.returncode:
    print(json.dumps({"error": p.stderr[-1000:]})); sys.exit(0)
print(json.dumps({"rows": json.loads(p.stdout)}))
```

## table

| 字段 | 说明 |
|---|---|
| `name` | 事件明细表，`库.表` 或 `catalog.库.表` |
| `device` / `date` / `event` | 去重单位列、日期分区列、事件名列 |
| `date_type` | `int`（20261019）/ `date` / `string`（再写 `date_format`，如 `"%Y-%m-%d"`） |
| `columns.string/number/bool` | 指标筛选可用的列（白名单）。不要放敏感列 |

## scopes / subscope

```yaml
scopes:                       # 页面上的范围按钮；第一个是默认
  - { key: ALL, label: 全部, where: "1 = 1" }
subscope:                     # 可选：范围下按某列细分（国家 / 渠道）
  column: region
  label: 地区
  values: [NA, EU, AP]        # 白名单，页面只能选这些
  labels: { NA: 北美 }
```

## funnel

**包含式漏斗**：单位是设备-天，到达第 k 步要求当天走过前面所有步，所以每步转化 ≤ 100%。顶部指标卡和北极星用**独立去重 UV**（和实验回收口径一致）。

| 字段 | 说明 |
|---|---|
| `steps[]` | 有顺序。`key`、`label`、`event`，可选 `where`（附加条件）、`segment`（分段名，表格合并单元格）、`shared: true`（不分档的公共步）、`tier_where: {档: where}`（某档对这一步用不同条件） |
| `extra_steps[]` | 只独立去重计数、不进链路的事件 |
| `tiers[]` | 档位（入口拆分）。`key`、`label`、`where`；`skip: [步骤 key]` 该档没有的步骤视为通过；`union: [档 key]` 合并档 = 任一成员档路径到达（成员档 where 应互斥）。不写 = 只有一档「全量」 |
| `core` | 核心链路步骤 key |
| `anchor` | 「较 X」的基准步 |
| `completeness` | `{step, min_ratio}`：该步 UV < 前 7 天中位数 × min_ratio → 当天标「不完整」，定时重试 |
| `kpis[]` | `{name, num, den, note, lower_is_better}`，num/den 是步骤 key，den 为 null = 只显示 UV。第一张是北极星 |
| `breakdown` | 可选，`{column, max, expose_step, click_step, label}`：按数值列分桶的 PV 转化 |
| `dau` | 可选，`{table, date, device, date_type, subscope_column, where_by_scope: {范围: where}}`：活跃设备数，算渗透率 |

## experiment

| 字段 | 默认 | 说明 |
|---|---|---|
| `group.column` | 必填 | 分组列 |
| `group.match` | `equals` | `equals`：列值 = 组号；`like`：列里含组号，配 `pattern`（含 `{id}`） |
| `identify_events` | `[]` | 设备当天有这些事件且带组号 = 当天进组；空 = 只看指标用到的事件是否带组号 |
| `extra_events` | `[]` | 指标库还要用、但不在漏斗里的事件 |
| `cadence` | 2/3/7/14 | `check` 查生效、`stop_loss` 判止损、`interim` 中期读数、`final` 出结论 |
| `cum_days` | 节点天数 | 「前 N 天累计」行；必须包含 `stop_loss` |
| `alpha` | 0.05 | 显著性水平；MDE 按 power 0.8 |
| `srm_p` | 0.001 | 分流检查卡方 p 低于它判 SRM |
| `cap_pre_days` | 7 | P99 封顶取上线日前几天 |
| `cuped_pre_days` | 28 | CUPED 协变量取上线日前几天的同口径取值；`0` = 不做 CUPED |
| `cuped_min_days` | 7 | 协变量窗口里同一批组号跑过别的实验时，窗口从那个实验结束次日起；剩下不足几天就不做 |
| `segments` | `[]` | 分群下钻：`[{column, label, top}]`，`column` 要在 `table.columns.string` 里；`top` = 只单列设备最多的前几个取值（默认 12，其余并成「其他」） |
| `seg_min_n` | 300 | 分群每组至少几台设备才出读数 |

组号只允许字母数字和 `_ . : -`，最长 64。

## metrics（预置指标）

```yaml
- key: click_rate              # 小写字母开头
  name: 结果页点击渗透率
  role: north_star             # north_star / process / guardrail
  kind: ratio                  # ratio / quantile
  numerator:   { event: item_click,  agg: uv }      # agg: uv / pv / sum:<数值列>；event 可写列表
  denominator: { event: result_view, agg: uv, filters: [["entry", "=", "in_app"]] }
  numerator_within_denominator: true   # 分子只算分母里的设备
  cap: p99_pre                 # 可选：分子按上线前单设备单天 P99 封顶（人均次数 / 时长类）
  lower_is_better: false
  device_scope: { event: result_view }  # 可选：只统计窗口内有过该事件的设备
  note: 说明
- key: startup_ms
  kind: quantile
  source: { event: play_start, filters: [["duration_ms", ">", "0"]] }
  column: duration_ms          # 必须在 table.columns.number
  quantiles: [0.5, 0.9]
```

筛选条件 `[列, 运算符, 值]`：运算符 `= != in not_in > >= < <= like is_null not_null`；字符串值只允许字母数字和 `_ . : - %`；数值可以写 `0.9*另一数值列`；`["any", [条件1, 条件2]]` = 任一满足。

页面「指标库」里也能新增 / 编辑；改了口径，用过它的实验会自动删旧结果并补算。
