# skill-ab-recovery-dashboard
一键生成属于你的业务数据看板，既能可视化数据漏斗，又能自定义AB实验回收。

这是一个 Agent Skill（Claude Code / 兼容 `SKILL.md` 的 Agent 均可用）。装好后对 Agent 说「帮我做个数据看板」，它会问清你的数据源、事件表、漏斗口径和实验回收习惯，然后生成一个在本机运行的看板：

- **日常漏斗**：每天定时拉昨天的数，补齐缺失 / 不完整的日期；包含式 UV 漏斗（每步转化 ≤ 100%）、北极星指标卡、逐日趋势、分桶转化、多入口分档。
- **实验回收**：填分组号即出读数。分流检查（SRM）、按设备聚类的标准误、显著性与 MDE、逐日 / 前 N 天累计 / 全窗口三层表格，结论提要按「查生效 → 判止损 → 中期读数 → 出结论」四个节点自动推进。
- **指标库**：页面上用事件 + 白名单列条件拼指标，预览 SQL；改口径自动补算用到它的实验。
- **任务日志**：每次拉数跑了哪些 SQL、多少行、多久，都能展开复制复跑。
- **数据源可插拔**：本地 DuckDB / CSV / Parquet，SQLAlchemy 连接串（Postgres、MySQL、Trino、ClickHouse 等），或任意「stdin 读 SQL、stdout 出 JSON」的命令。

## 安装

```sh
# 方式一：skills CLI
npx skills add Gillian-2024/skill-ab-recovery-dashboard

# 方式二：手动
git clone https://github.com/Gillian-2024/skill-ab-recovery-dashboard.git ~/.claude/skills/ab-recovery-dashboard
```

依赖：Python ≥ 3.9。生成看板时会在看板目录下建独立 venv，安装 `template/requirements.txt` 中锁定版本的依赖。

## 不经过 Agent，直接试

```sh
python3 scripts/generate.py ~/dashboards/demo --demo --port 5050
cd ~/dashboards/demo && ./start.sh
# 打开 http://127.0.0.1:5050
```

演示数据是合成的，并埋了一个已知效果的实验（分组 G100 对照 / G101 实验），可以用来验收读数。

接自己的数据：复制 `template/dashboard.example.yaml` 按 `references/config-schema.md` 改，然后

```sh
python3 template/tools/check_config.py --config ./dashboard.yaml
python3 scripts/generate.py ~/dashboards/mine --config ./dashboard.yaml --probe
```

## 安全

看板没有登录，只监听 `127.0.0.1`；写接口只接受同源 JSON 请求（挡 CSRF 和 DNS 重绑定）。页面上能输入的值（日期、细分、分组号、指标筛选列）都走白名单。连接串放 `.env`，不要提交。

## 目录

见 [SKILL.md](SKILL.md#目录)。测试：`cd template && python3 -m unittest discover -s tests`。
