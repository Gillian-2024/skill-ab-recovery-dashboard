# 异常处理

先看页面顶部提醒和「任务日志」：每个任务都能展开看到实际跑过的 SQL、行数、耗时和报错，SQL 可以复制去数据源复跑核对。

| 现象 | 原因 | 处理 |
|---|---|---|
| 顶部「数据源凭据未配置」 | sqlalchemy：`.env` 里没有 `DASH_DB_URL`（或 `url_env` 指的变量）；command：`auth_file` 不存在 | 复制 `.env.example` 为 `.env` 填连接串 / 重新登录生成凭据文件，**重启看板**（`.env` 只在启动时读） |
| 顶部「凭据可能已失效」 | 最近失败任务的报错里有 401 / 403 / token / login 等字样 | 按 `source.auth_hint` 重新登录；到「任务日志」或实验页点「重跑」 |
| 「查询超过 N 秒」 | 单条 SQL 超过 `source.timeout_s` | 漏斗会自动对半拆窗口重试；仍超时就缩短刷新窗口、调大 `timeout_s`，或确认事件表按日期分区、`table.date` 填的是分区列。sqlalchemy 超时后数据库侧可能还在跑，必要时手动取消 |
| 「返回 N 行，达到上限 source.max_rows」 | 结果被截断风险 | 缩短窗口（By天 = 天数 ×（组数 + 1）行）或调大 `max_rows`；command 通道的工具若自己有行数上限，要在输出里带 `truncated: true` |
| 「取数命令的输出不是 JSON」 | 包装脚本往 stdout 打了日志 | 日志改打到 stderr，stdout 只留一段 JSON |
| 「找不到 DuckDB 文件」 | `source.path` 相对路径按看板目录解析 | 写绝对路径，或把文件放到看板目录下 |
| 实验「实验组没有设备进组」/ 各组设备数为 0 | 分组列格式和 `group.match/pattern` 对不上；`identify_events` 里的事件不带组号 | `python3 tools/check_config.py --probe --date <实验期内某天>` 看各事件「分组列非空」占比；用一条样例值核对 `pattern`（`%,{id},%` 要求两侧有分隔符） |
| SRM 不通过 | 分流不均、进组口径和分流口径不一致、流量占比填错 | 先核对新建回收时填的「流量占比」；再查是否只有部分事件带组号导致某组漏认 |
| 「上线前 N 天无数据，算不出 P99 封顶」 | 上线日之前的数据不在表里 | 改上线日，或该指标去掉 P99 封顶 |
| 「没有分位数函数」 | mysql 方言 | 分位数指标和 P99 封顶在 mysql 下不可用；换成支持分位数的引擎，或不用这两类指标 |
| 某天标「数据可能不完整」 | `completeness.step` 当天 UV 低于前 7 天中位数 × `min_ratio` | 一般是数仓还没产完，系统会按 `retry_gap_hours` 重试；长期误报就调低 `min_ratio` 或换判定步骤 |
| 「较上一周」列不可算 | 上一段同长窗口有缺失 / 不完整的日期 | 点表格下方「补同周期AA变化」只补上一段 |
| 页面打开 403「只接受本机访问」 | 用了本机名以外的地址（含局域网 IP、自定义域名） | 用 `http://127.0.0.1:<port>` 或 `http://localhost:<port>` 打开 |
| 写操作 415 / 403「跨站请求已拒绝」 | 请求不是同源 JSON（脚本直调时常见） | 脚本调用加 `Content-Type: application/json`，不要带其他站点的 `Origin` |
| 启动报 `Address already in use` | 端口被占用 | 改 `server.port`；Chrome 会拒绝一些端口（如 5060、5061、6000），选 5050、8050 这类 |
| 启动报「dashboard.yaml：…」 | 配置不合法 | 按提示改，再跑 `python3 tools/check_config.py` |
| 改了预置指标但页面没变 | 页面上编辑过的预置指标不再被 yaml 覆盖 | 指标库里点「恢复预置」 |
| 服务停了 / 电脑重启后没起来 | 看板是普通进程 | `python3 tools/install_service.py` 生成 launchd / systemd 配置，按输出提示安装 |
