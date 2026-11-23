# Search Dashboard

一个本地数据看板：搜索漏斗（包含式 UV 漏斗、核心指标、逐日趋势）+ 实验回收（SRM、显著性、MDE、多次看数 / 多指标修正、CUPED、分群下钻）+ 指标库 + 任务日志。

## 说明

- 本仓库是脱敏后的学习版：数据源表名、通知收件人、文档 token 等全部通过环境变量配置，仓库内为占位示例。
- 不附带 `LICENSE`，版权归作者所有，仅供学习参考，请勿直接用于商业产品或对外发布为自有产品。
- 业务口径相关的常量（事件名、档位、指标定义）保留在代码中，作为实现示例。

## 目录

- `dashboard/` 后端：`app.py` 路由、`jobs.py` 任务与调度、`sqlgen.py` SQL 生成、`store.py` SQLite、`kg_client.py` 取数通道、`stats.py` 统计、`notify.py` 通知、`rollout.py` / `observe.py` / `hotword.py` / `versync.py` 观测模块。
- `web/` 前端：原生 JS + CSS，无构建步骤。
- `scripts/` 独立脚本。
- `data/` 运行时数据（库、日志、快照、分享口令），已被 gitignore。

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh
```

打开 http://127.0.0.1:5050 。

## 配置

复制 `.env.example` 为 `.env` 并按环境填写（变量见文件）。未配置数据源时服务能启动，但取数会失败。

## 主要常量

- 每日刷新时刻 `DAILY_AT`、补数天数 `BACKFILL_DAYS`、失败重试窗口 `RETRY_GAP_HOURS` / `RETRY_UNTIL_HOUR` 见 `dashboard/config.py`。
- 市场 / 机房路由 `MARKETS`、国家清单 `REGIONS`、播放 from 口径 `PLAY_FROM` 见 `dashboard/config.py`。
