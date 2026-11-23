"""全局配置：数据源、市场路由、调度参数。敏感值与业务参数全部可用环境变量覆盖。"""
import os
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "dashboard.db")
WEB_DIR = os.path.join(ROOT, "web")

# 本机访问（127.0.0.1）= 完整权限；局域网访问 = 只读，须带分享口令（见 app.py _guard）
# 想关掉局域网分享：启动时设 DASHBOARD_LAN=0，只监听本机
PORT = int(os.environ.get("DASHBOARD_PORT", "5050"))
LAN_SHARE = os.environ.get("DASHBOARD_LAN", "1") != "0"
HOST = "0.0.0.0" if LAN_SHARE else "127.0.0.1"
SHARE_TOKEN_PATH = os.path.join(DATA_DIR, "share_token")   # 口令放数据目录，不进代码；删掉文件重启 = 换口令

# ── 数据源：数仓表名 / 查询服务（示例占位，实际使用请通过环境变量配置） ──
TABLE = os.environ.get("EVENT_TABLE", "catalog.db.event_log_table")
DAU_TABLE = os.environ.get("DAU_TABLE", "catalog.db.dau_table")
CONTENT_TABLE = os.environ.get("CONTENT_TABLE", "catalog.db.content_table")
CATALOG = os.environ.get("CATALOG", "catalog")
BUSINESS = os.environ.get("KG_BUSINESS", "video")

KG_CLIENT = os.environ.get("KG_CLIENT", "kg-mcp-client")
KG_API_URL = os.environ.get("KG_API_URL", "https://kg.example.com")
# spawn 出来的 client 会继承系统代理；这里强制直连查询服务域名
KG_NO_PROXY = os.environ.get("KG_NO_PROXY", urlparse(KG_API_URL).hostname or "")
KG_SESSION = os.environ.get("KG_SESSION", os.path.expanduser("~/.kg-mcp/session.json"))
KG_QUERY_TIMEOUT = int(os.environ.get("KG_QUERY_TIMEOUT", "1800"))

MARKETS = {
    "RU": {"label": "RU", "cluster": 3, "region": "RU", "dau_region": "RU"},
    "SG": {"label": "SG", "cluster": 1, "region": None, "dau_region": None},
    "EU": {"label": "EU", "cluster": 4, "region": None, "dau_region": None},
}

# 实验可选国家（示例：按需自行补充）
REGIONS = {
    "SG": "ID TR BR".split(),
    "RU": "RU TM UA".split(),
    "EU": "ES FR IT DE PL".split(),
}

# 结果页带来的播放：新版本覆盖不足前四值只能加总
PLAY_FROM = ("search", "search_related", "search_related_feed", "no_results_recommend")
FALLBACK_FROM = "no_results_recommend"
# 分来源观测（主线仍是合并口径，不断档）：新版本起结果页 / 中间页推荐播放可拆
SPLIT_START = 20300101
# 新版本 = 含结果页 from 字段修复的版本，按渠道 + 版本号精确匹配（示例数据）
SPLIT_VERSIONS = {
    "2030010100": "GP release A", "2030020100": "GA release A",
}
SPLIT_VERSIONS_SYNCED = "example"
FROM_RESULT = ("search",)
FROM_MIDREC = ("search_related", "search_related_feed")

DAILY_AT = (9, 40)        # 每日刷新时刻（本地时间）
BACKFILL_DAYS = 14        # 每次日更检查并补齐的天数
RETRY_GAP_HOURS = 2       # 昨日数据不完整时的重试间隔
RETRY_UNTIL_HOUR = 20

# 主动通知（见 notify.py）：日更失败提醒 + 实验节点摘要；收件人 open_id 用环境变量配置
NOTIFY_ON = os.environ.get("NOTIFY_ON", "0") != "0"
NOTIFY_OPEN_ID = os.environ.get("NOTIFY_OPEN_ID", "")

# 本机维护（见 maint.py）：每天第一次 tick 做一次库快照 + 日志轮转，都只落 data/
SNAPSHOT_DIR = os.path.join(DATA_DIR, "snapshots")
SNAPSHOT_KEEP = 14
LOG_PATH = os.path.join(DATA_DIR, "server.log")
LOG_MAX_MB = 5
LOG_KEEP = 3

# 业务月度 LTV（示例为空；口径说明见 README）
SEARCH_LTV_DOC = ""
SEARCH_LTV = []
