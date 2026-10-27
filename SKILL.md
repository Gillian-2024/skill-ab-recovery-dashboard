---
name: ab-recovery-dashboard
description: 访谈用户的数据口径和实验回收习惯，一键生成一个本地数据看板：日常漏斗每天自动滚动（包含式 UV 漏斗、核心指标、逐日趋势），同时支持 AB 实验回收（分流检查、显著性、MDE、多次看数 / 多指标修正、CUPED、分群下钻、四节点结论提要）。数据源可插拔：本地 DuckDB 文件 / SQLAlchemy 连接串 / 自定义取数命令。用户说「做个数据看板」「漏斗看板」「实验回收看板」「AB 回收」「每天自动看转化」时使用。
---

# AB 回收数据看板

## 适用场景

- 有一张**一行一个事件**的明细表（设备 / 用户 id、日期、事件名 + 若干属性列），想每天自动看一条转化漏斗。
- 经常做 AB 实验，想每次填分组号就自动出读数：分流是否均衡、各指标差异和显著性、可测下限、修正多次看数 / 多指标后是否仍显著、CUPED 方差缩减、按属性列分群下钻、到了哪个节点、能下什么结论。
- 数据在本地文件、能用连接串连的库，或只能通过内部命令行 / 接口跑 SQL。

不适用：只有聚合宽表没有事件明细；需要多人共享访问（本看板只监听本机、无登录）；实时（分钟级）监控。

## 用户输入格式

触发语句示例：

```
帮我做一个数据看板，表是 analytics.events，trino，漏斗是 打开 → 搜索 → 点击 → 播放
做个 AB 回收看板，分组号在 ab_groups 字段里，形如 ",101,205,"
先用演示数据跑个看板给我看看
```

需要从用户处拿到的信息（缺的按 `references/interview.md` 逐轮问，答不上用默认值）：

| 类别 | 必需 | 例 |
|---|---|---|
| 数据源 | 类型 + 方言 + 凭据放哪 | trino；连接串放 `.env` |
| 事件表 | 表名、设备列、日期列（类型）、事件列、可筛选列 | `analytics.events`，`device_id`，`dt` int |
| 范围 | 0~N 个范围（SQL where）+ 可选细分列及取值 | 全部 / 仅 Android；按国家细分 |
| 漏斗 | 有序步骤（事件 + 条件）、档位、核心链路、指标卡 | 北极星 = 点击 UV ÷ 结果页 UV |
| 实验 | 分组列及格式、带组号的事件、节奏、α、预置指标及角色 | `like '%,{id},%'`；2/3/7/14 天 |
| 运行 | 生成到哪个目录、端口、每天几点拉数 | `~/dashboards/search`，5050，09:40 |

参数要求：组号只能是字母数字和 `_ . : -`；细分值、筛选列都是白名单；端口避开浏览器禁用端口（如 5060/5061/6000）；`server.host` 只能是本机。

## 核心执行步骤（按优先级）

1. **先跑演示，确认环境可用。** `python3 scripts/generate.py <临时目录> --demo --port <端口>`，再 `cd <临时目录> && ./start.sh`。要求 Python ≥ 3.9，可访问 PyPI。跑不起来先修环境，不进访谈。用户只是想看效果时，到这一步就交付。
2. **访谈口径。** 按 `references/interview.md` 的 4 轮（数据源 → 表结构 → 漏斗 → 实验）提问；用户贴了 DDL / SQL / 文档就先从里面读，只请用户确认推断。每轮把对应的 yaml 片段给用户看。凭据不在对话里回显、不写进 yaml。
3. **写配置并校验。** 以 `template/dashboard.example.yaml` 为底稿写 `dashboard.yaml`（字段见 `references/config-schema.md`），运行 `python3 template/tools/check_config.py --config <yaml>` 直到通过。
4. **生成看板。** `python3 scripts/generate.py <目标目录> --config <yaml> [--port N] --probe`。`--probe` 会查昨天各步事件量和分组列覆盖：有「没有数据」的步骤，逐个和用户核对事件名 / 条件，改 yaml 后重跑 `tools/check_config.py --probe`。
5. **启动并拉第一批数。** `./start.sh`，打开 `http://127.0.0.1:<端口>`，日常漏斗页点「从数据源刷新」（单次最多 31 天）。确认核心指标卡、漏斗表、趋势图都有数。
6. **建第一个实验回收（如用户有实验）。** 实验回收页「新建回收」：分组号 + 角色（对照 / 实验）+ 流量占比、上线日、开窗日、勾指标。看「分流检查」是否通过、结论提要是否到了正确节点。
7. **交付说明。** 告诉用户：每天 `schedule.daily_at` 自动续拉、未冻结实验每天续跑；常驻运行用 `python3 tools/install_service.py` 生成 launchd / systemd 配置（只打印，用户自己安装）；口径规则见 `references/best-practices.md`。

## 异常处理

完整表见 `references/troubleshooting.md`。最常见的：

| 情况 | 处理 |
|---|---|
| **凭据未配置**（顶部红条「数据源凭据未配置」） | sqlalchemy：复制 `.env.example` → `.env` 填 `DASH_DB_URL`，重启看板；command：重新登录生成 `auth_file`。不要把连接串写进 yaml 或贴进对话 |
| **凭据失效**（报错含 401/403/token/login） | 按 `source.auth_hint` 重新登录，到任务日志 / 实验页点「重跑」 |
| **跑错了 / 跑太久** | 任务日志点「取消」：已落库的保留、后续不跑；被取消的实验标失败，点「重跑」继续 |
| **SQL 超时** | 漏斗查询会自动对半拆窗口重试；仍超时则缩短窗口、调大 `source.timeout_s`、确认 `table.date` 是分区列 |
| **结果到达 max_rows** | 直接报错不用截断数据：缩短窗口或调大 `source.max_rows` |
| **实验没有设备进组** | `tools/check_config.py --probe --date <实验期内某天>` 看分组列覆盖；核对 `group.pattern` 和样例值 |
| **SRM 不通过** | 结论提要会停在「不读效果」；查流量占比、`identify_events`、跨组设备 |
| **配置写错** | 启动 / 校验会指出字段名；按 `references/config-schema.md` 改 |
| **端口被占 / 浏览器拒绝端口** | 换 `server.port`（5050、8050 这类） |

不要为绕过报错去改 `server.host` 为 `0.0.0.0`、关掉同源校验或放宽白名单。

## 示例

**示例 1：先看效果**

```
用户：先用演示数据跑个看板给我看看
执行：python3 scripts/generate.py ~/dashboards/demo --demo --port 5050
      cd ~/dashboards/demo && ./start.sh
输出：✓ 配置通过：2 个范围，3 档，8 步漏斗（+1 补充步），6 个预置指标，取数通道 duckdb / duckdb
      浏览器打开 http://127.0.0.1:5050 → 点「从数据源刷新」→ 实验回收页新建：G100 对照 / G101 实验，
      开窗日 = demo_data 输出的 OPEN_DAY
结论提要（演示数据埋了 +12% 点击概率；周末效应随生成日期变，数值会略有不同）：
      1. 结果页点击渗透率显著正向 +5.83pp（z = +3.83）
      2. 护栏未见显著劣化
      3. 人均播放（P99 封顶）显著正向 +17.6%（z = +4.81）
```

**示例 2：接自己的数仓**

```
用户：表是 dw.app_events（trino），device_id / dt(int) / event_name；
      漏斗 首页曝光 home_show → 搜索 search_submit → 结果页 result_show → 点击 result_click；
      分组在 abtest 字段，值形如 ",1203,1388,"
执行：访谈补齐范围、指标卡、实验节奏后写 dashboard.yaml：
      source: { type: sqlalchemy, dialect: trino }        # .env: DASH_DB_URL=trino://user@host:443/hive/dw
      table: { name: dw.app_events, device: device_id, date: dt, date_type: int, event: event_name, ... }
      experiment: { group: { column: abtest, match: like, pattern: "%,{id},%" }, ... }
      python3 scripts/generate.py ~/dashboards/search --config ./dashboard.yaml --probe
输出：自检逐步列出昨天的事件条数、设备数、分组列非空占比；
      有「✗ … 没有数据」的步骤 → 和用户核对事件名后重跑。
```

**示例 3：只能通过内部命令跑 SQL**

```
用户：我们只能用 corp-sql CLI 跑查询
执行：按 references/config-schema.md「command 通道」写一个包装脚本（stdin 读 SQL，stdout 打 JSON），
      source: { type: command, dialect: spark, command: ["python3", "~/bin/run_sql.py"], auth_file: ~/.corp/token,
                auth_hint: "终端运行 corp-sql login" }
输出：任务日志里每条 SQL 带包装脚本返回的 task_id，可回到内部平台追溯。
```

## 目录

```
SKILL.md                     本文件
scripts/generate.py          复制模板 → 放配置 → 建 venv → 自检
template/                    看板本体（Flask + SQLite + 原生 JS），生成时整份复制
  dashboard.example.yaml     样例配置（对应合成数据）
  tools/check_config.py      配置校验 + 接入自检
  tools/demo_data.py         合成数据（埋了已知效果的实验，用于验收）
  tools/install_service.py   生成开机自启配置
  tests/                     python3 -m unittest discover -s tests
references/interview.md      访谈清单（4 轮 + 默认值）
references/config-schema.md  配置字段说明
references/best-practices.md 口径与回收规则
references/troubleshooting.md 异常处理
```
