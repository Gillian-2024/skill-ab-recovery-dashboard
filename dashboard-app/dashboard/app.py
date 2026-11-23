"""Flask 入口：REST API + 静态单页。只监听 127.0.0.1（无鉴权）。"""
import hmac
import json
import logging
import os
import secrets
import sys
import time
import re
from logging.handlers import RotatingFileHandler

from flask import Flask, Response, jsonify, redirect, request, send_from_directory

from . import config as C, jobs, sqlgen, store

app = Flask(__name__, static_folder=None)
_DATE = re.compile(r"^20\d{6}$")


def _date(v, name):
    v = str(v or "").replace("-", "")
    if not _DATE.match(v):
        raise ValueError("%s 日期格式应为 YYYYMMDD" % name)
    jobs.parse(v)
    return int(v)


@app.errorhandler(ValueError)
def _bad(e):
    return jsonify(error=str(e)), 400


# 访问控制：
# - 本机（remote_addr 为回环地址）= 完整权限，挡两类跨站请求：
#   · DNS 重绑定：Host 必须是本机名，否则外站页面能读到实验结果；
#   · CSRF：写接口只收 application/json（跨站发这个类型会先预检，本服务不回 CORS 头就被浏览器拦下），
#     再校验 Origin / Sec-Fetch-Site 必须同源，text/plain 等「简单请求」直接拒。
# - 局域网（其他地址）= 只读分享：必须带分享口令（首次 ?t=口令 → 写进 HttpOnly cookie），
#   只放行 GET；写接口一律 403；任务日志 / SQL 不对外（含查询语句与内部报错）。
_HOSTS = {"127.0.0.1", "localhost"}
_ORIGINS = {"http://%s:%d" % (h, C.PORT) for h in _HOSTS}
_LOOPBACK = {"127.0.0.1", "::1"}
_COOKIE = "share_t"
_LAN_HIDDEN = ("/api/jobs",)


def share_token():
    """读分享口令；没有就生成一个（32 位随机），只存本机数据目录。"""
    try:
        with open(C.SHARE_TOKEN_PATH) as f:
            t = f.read().strip()
        if t:
            return t
    except FileNotFoundError:
        pass
    os.makedirs(C.DATA_DIR, exist_ok=True)
    t = secrets.token_urlsafe(24)
    fd = os.open(C.SHARE_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


def is_local():
    return request.remote_addr in _LOOPBACK


@app.before_request
def _guard():
    if is_local():
        host, _, port = (request.host or "").partition(":")
        if host not in _HOSTS or (port and port != str(C.PORT)):
            return jsonify(error="只接受本机访问"), 403
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if request.mimetype != "application/json":
            return jsonify(error="写接口只接受 application/json"), 415
        origin = request.headers.get("Origin")
        if origin is not None and origin not in _ORIGINS:
            return jsonify(error="跨站请求已拒绝"), 403
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return jsonify(error="跨站请求已拒绝"), 403
        return None
    # 局域网只读
    if not C.LAN_SHARE:
        return jsonify(error="只接受本机访问"), 403
    tok = share_token()
    given = request.args.get("t") or request.cookies.get(_COOKIE) or ""
    if not hmac.compare_digest(given, tok):
        return Response("需要分享链接才能访问（链接里带口令，向看板维护人索取）", status=401, mimetype="text/plain")
    if request.method not in ("GET", "HEAD"):
        return jsonify(error="只读分享：不能刷新数仓 / 建实验 / 改指标，请找看板维护人操作"), 403
    if request.path.startswith(_LAN_HIDDEN):
        return jsonify(error="只读分享不开放任务日志"), 403
    if request.args.get("t") and request.path == "/":
        # 口令换成 cookie 后去掉地址栏里的口令，避免截图 / 转发时带出去
        # Lax 而非 Strict：从飞书等别的站点点链接进来，跳转后的这次请求也要带上 cookie（只读分享只放行 GET，Lax 不放宽写权限）
        r = redirect("/")
        r.set_cookie(_COOKIE, tok, httponly=True, samesite="Lax", max_age=30 * 86400)
        return r
    return None


@app.after_request
def _cookie(resp):
    if not is_local() and C.LAN_SHARE and request.args.get("t") and resp.status_code < 400:
        resp.set_cookie(_COOKIE, share_token(), httponly=True, samesite="Lax", max_age=30 * 86400)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


@app.get("/")
def index():
    return send_from_directory(C.WEB_DIR, "index.html")


@app.get("/static/<path:p>")
def static_files(p):
    return send_from_directory(C.WEB_DIR, p)


@app.get("/api/meta")
def meta():
    return jsonify(markets={k: v["label"] for k, v in C.MARKETS.items()}, market_order=list(C.MARKETS), regions=C.REGIONS,
                   all_key=store.ALL, all_parts=list(store.ALL_PARTS),
                   has_dau={k: bool(v["dau_region"]) for k, v in C.MARKETS.items()},
                   steps=sqlgen.FUNNEL_STEPS, flow_steps=sqlgen.FLOW_STEPS, yesterday=jobs.yesterday(),
                   events=sqlgen.EVENTS, str_cols=sorted(sqlgen.STR_COLS), num_cols=sorted(sqlgen.NUM_COLS),
                   bool_cols=sorted(sqlgen.BOOL_COLS), ops=sorted(sqlgen.OPS), labels=sqlgen.LABELS,
                   kg_session=os.path.exists(C.KG_SESSION), daily_at="%02d:%02d" % C.DAILY_AT,
                   search_ltv=C.SEARCH_LTV, search_ltv_doc=C.SEARCH_LTV_DOC,
                   readonly=not is_local(), share=_share_info() if is_local() else None)


def _share_info():
    """本机页面上显示的分享链接（局域网 IP 取默认出口地址；连不上网时为空）。"""
    if not C.LAN_SHARE:
        return None
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            ip = sk.getsockname()[0]
    except OSError:
        return None
    return {"url": "http://%s:%d/?t=%s" % (ip, C.PORT, share_token()), "ip": ip}


@app.get("/api/funnel")
def funnel():
    m = _view(request.args.get("market", store.ALL))
    d0 = _date(request.args.get("start"), "start")
    d1 = _date(request.args.get("end"), "end")
    rows, pos, dau, st, flow = store.funnel(m, d0, d1)
    core = store.daily_payload("core_daily", m, d0, d1)
    split = store.daily_payload("split_daily", m, d0, d1)
    from . import versync
    vs = versync.summary()
    synced = time.strftime("%Y-%m-%d", time.localtime(vs["fetched_at"])) if vs["synced"] else vs["fallback"]
    return jsonify(rows=rows, positions=pos, dau=dau, status=st, flow=flow, core=core, split=split, split_start=C.SPLIT_START,
                   split_versions=len(vs["versions"]), split_synced=synced)


def _view(m):
    """漏斗视图键：全机房 ALL，或 split_scope 认的机房 / 国家键。"""
    if m != store.ALL:
        sqlgen.split_scope(m)
    return m


WEEKLY_FROM = 20260803   # 核心指标周均走势起点：2026 年 8 月第一个工作周（周一）


def _weekly_missing(m, have, y):
    """没拉过的天 + 拉过但缺列的天（老版本核心指标查询少卡片曝光 / 无结果列），都算缺、补历史时重拉。"""
    return [d for d in jobs._days(WEEKLY_FROM, y) if d not in have or any(c not in have[d] for c in sqlgen.CORE_COLS)]


@app.get("/api/core_weekly")
def core_weekly():
    """核心指标逐日（WEEKLY_FROM ~ 昨天），前端按自然周（周一 ~ 周日）求周均。"""
    m = _view(request.args.get("market", store.ALL))
    y = jobs.yesterday()
    days = store.core_series(m, WEEKLY_FROM, y)
    if m == store.ALL:                         # 全机房：任一机房缺的天都算缺
        miss = sorted({d for p in store.ALL_PARTS for d in _weekly_missing(p, store.core_series(p, WEEKLY_FROM, y), y)})
    else:
        miss = _weekly_missing(m, days, y)
    return jsonify(start=WEEKLY_FROM, end=y, days=days, missing=miss)


@app.post("/api/core_history")
def core_history():
    b = request.get_json(force=True) or {}
    m = _view(str(b.get("market") or ""))
    y = jobs.yesterday()
    if m == store.ALL:                         # 全机房：三个机房各补各缺的
        ids = []
        for p in store.ALL_PARTS:
            miss = _weekly_missing(p, store.core_series(p, WEEKLY_FROM, y), y)
            if miss:
                ids.append(jobs.submit("core_history", {"markets": [p], "start": min(miss), "end": max(miss)}))
        return jsonify(job_id=ids[-1] if ids else None, job_ids=ids)
    missing = _weekly_missing(m, store.core_series(m, WEEKLY_FROM, y), y)
    if not missing:
        return jsonify(job_id=None)
    return jsonify(job_id=jobs.submit("core_history", {"markets": [m], "start": min(missing), "end": max(missing)}))


@app.post("/api/refresh")
def refresh():
    b = request.get_json(force=True) or {}
    d0, d1 = _date(b.get("start"), "start"), _date(b.get("end"), "end")
    if d1 < d0:
        raise ValueError("结束日早于开始日")
    ms = b.get("markets") or list(C.MARKETS)
    # 全机房不单独取数：展开成三个机房（去重、保持顺序）
    ms = list(dict.fromkeys(x for m in ms for x in (store.ALL_PARTS if m == store.ALL else [m])))
    for m in ms:
        sqlgen.split_scope(m)
    if (jobs.parse(d1) - jobs.parse(d0)).days > 31:
        raise ValueError("单次刷新最多 31 天")
    return jsonify(job_id=jobs.submit("funnel", {"markets": ms, "start": d0, "end": d1}))


@app.get("/api/metrics")
def metrics():
    return jsonify(store.metrics())


@app.post("/api/metrics/preview")
def metric_preview():
    d = sqlgen.validate_metric(request.get_json(force=True) or {})
    fake = {"key": "m", "name": d["name"], "def": d}
    if d.get("kind", "ratio") == "ratio":
        sql = sqlgen.experiment_ratio_sql("RU", ["111111", "222222"], 20261012, 20261018, [fake], {"m": 1000})
    else:
        sql = sqlgen.experiment_quantile_sql("RU", ["111111", "222222"], 20261012, 20261018, fake)
    return jsonify(sql=sql.strip())


@app.post("/api/metrics")
def metric_save():
    b = request.get_json(force=True) or {}
    key = str(b.pop("key", "") or "").strip()
    if not re.match(r"^[a-z][a-z0-9_]{1,40}$", key):
        raise ValueError("key 只能用小写字母、数字、下划线，字母开头")
    store.save_metric(key, sqlgen.validate_metric(b))
    return jsonify(ok=True)


def _calibre(d):
    """影响数值的字段（改名 / 分组 / 备注不算口径变化）。"""
    return {k: d.get(k) for k in ("kind", "numerator", "denominator", "numerator_within_denominator", "device_scope", "cap",
                                  "source", "column", "quantiles", "lower_is_better")}


@app.get("/api/metrics/<key>/usage")
def metric_usage(key):
    return jsonify([{"id": e["id"], "name": e["name"], "status": e["status"]} for e in store.experiments_using(key)])


@app.put("/api/metrics/<key>")
def metric_update(key):
    """编辑指标：口径变了 → 用过它的实验删掉该指标旧结果（含 By天 / 累计行 / P99 封顶）并排补算；只改名字 / 备注不重算。"""
    b = request.get_json(force=True) or {}
    old = store.metric_map().get(key)
    if not old:
        return jsonify(error="不存在"), 404
    restore = bool(b.pop("restore", False))
    if restore:
        from .presets import PRESETS
        d = dict(PRESETS)[key] if key in dict(PRESETS) else None
        if d is None:
            raise ValueError("只有预置指标能恢复")
    else:
        b.pop("key", None)
        d = sqlgen.validate_metric(b)
    changed = _calibre(d) != _calibre(old)
    busy = [e for e in store.experiments_using(key) if e["status"] in ("queued", "running") or e.get("pending")] if changed else []
    if busy:
        raise ValueError("用到这个指标的实验正在跑数（%s），跑完再改" % "、".join(e["name"] for e in busy))
    store.update_metric(key, d, edited=not restore)
    jobs_ = []
    if changed:
        for e in store.experiments_using(key):
            store.delete_exp_results(e["id"], [key] + [key + s for s in ("@day", "@c3", "@c7", "@c14")])
            caps = {k: v for k, v in (e.get("caps") or {}).items() if k not in (key, key + "@day")}
            store.update_experiment(e["id"], caps=caps)
            if e.get("last_end"):
                jobs_.append({"exp": e["name"], "job_id": jobs.submit("experiment", {
                    "exp_id": e["id"], "only": [key], "only_daily": [key] if key in (e.get("daily_keys") or []) else []})})
    return jsonify(ok=True, changed=changed, rerun=jobs_)


@app.delete("/api/metrics/<key>")
def metric_delete(key):
    m = store.metric_map().get(key)
    if not m:
        return jsonify(error="指标不存在"), 404
    if m["preset"]:
        raise ValueError("预置指标不能删除，可在指标库里「恢复预置」")
    store.delete_metric(key)
    return jsonify(ok=True)


def _lan_err(d, *keys):
    """只读分享不回内部报错原文（可能带 SQL 片段 / 本机路径），只留「有错」这个事实。"""
    if not is_local():
        for k in keys:
            if d.get(k):
                d[k] = "跑数出错，请找看板维护人"
    return d


@app.get("/api/experiments")
def exp_list():
    return jsonify([_lan_err(dict(e, stage=jobs.stage_of(e)), "error") for e in store.experiments()])


@app.get("/api/health")
def health():
    """页面顶部提醒：kg 登录态 + 最近 24 小时失败的任务（按类型取最近一次，后面同类成功过就不算）。只读分享不给。"""
    if not is_local():
        return jsonify(kg_session=os.path.exists(C.KG_SESSION), login_suspect=False, fails=[], failed_exps=[])
    since, js = time.time() - 86400, store.jobs(200)
    exps = {e["id"] for e in store.experiments()}

    def resolved(j):
        p = json.loads(j["params"] or "{}")
        if "exp_id" in p:                       # 实验已删除 / 之后同一实验跑成功过
            return p["exp_id"] not in exps or any(x["id"] > j["id"] and x["status"] == "done" and
                                                   json.loads(x["params"] or "{}").get("exp_id") == p["exp_id"] for x in js)
        if j["kind"] == "funnel":               # 漏斗：涉及的日期现在都已完整入库
            days = list(jobs._days(p["start"], p["end"]))
            return all(all(store.day_status(m).get(d) for d in days) for m in p.get("markets") or C.MARKETS)
        if j["kind"] == "core_history":         # 核心指标补历史：这些市场现在不缺天
            y = jobs.yesterday()
            return all(not _weekly_missing(m, store.core_series(m, WEEKLY_FROM, y), y) for m in p.get("markets") or C.MARKETS)
        later = [json.loads(x["params"] or "{}") for x in js if x["id"] > j["id"] and x["kind"] == j["kind"] and x["status"] == "done"]
        if j["kind"] == "hotword":              # 热词：之后有一次成功的刷新覆盖了同一段日期
            return any(q.get("start", 0) <= p.get("start", 0) and q.get("end", 0) >= p.get("end", 0) for q in later)
        return p in later                       # 其他：之后同参数的任务跑成功过

    # 当前处于失败状态的实验：常驻，不受 24 小时限制（失败的实验不会被每日续跑，不提醒就会一直停着）
    bad_exps = [{"id": e["id"], "name": e["name"], "partial": e["status"] != "failed", "error": (e["error"] or "")[:200]}
                for e in store.experiments() if e["status"] == "failed" or (e.get("error") and not e.get("pending"))]
    bad_ids = {e["id"] for e in bad_exps}
    fails = []
    for j in js:
        if j["created_at"] < since:
            break
        if j["status"] == "failed" and not resolved(j) and json.loads(j["params"] or "{}").get("exp_id") not in bad_ids:
            fails.append({"id": j["id"], "kind": j["kind"], "error": (j["error"] or "")[:200], "at": j["finished_at"]})
    from .kg_client import login_suspect
    login = any(login_suspect(f["error"]) for f in fails + bad_exps)
    # net_down：日更上一次提交前查到 KG 连不上的原因（每分钟复查，恢复即清空）
    return jsonify(kg_session=os.path.exists(C.KG_SESSION), login_suspect=login, fails=fails, failed_exps=bad_exps,
                   net_down=jobs.NET_DOWN)


def _parse_exp(b):
    """新建 / 编辑共用的表单校验。"""
    name = str(b.get("name", "")).strip()
    if not name:
        raise ValueError("实验名不能为空")
    m = b.get("market")
    if m not in C.MARKETS:
        raise ValueError("未知市场")
    regions = sorted({str(r).strip().upper() for r in b.get("regions") or [] if str(r).strip()})
    bad = [r for r in regions if r not in C.REGIONS.get(m, [])]
    if bad:
        raise ValueError("%s 市场不支持的国家：%s" % (m, ",".join(bad)))
    groups = []
    for g in b.get("groups") or []:
        gid = str(g.get("id", "")).strip()
        if not re.match(r"^\d{1,12}$", gid):
            raise ValueError("exp_id 应为纯数字：%r" % gid)
        if g.get("role") not in ("control", "treatment"):
            raise ValueError("每个组都要选对照/实验")
        groups.append({"id": gid, "role": g["role"], "label": str(g.get("label") or gid)[:30]})
    if len(groups) < 2 or len({g["id"] for g in groups}) != len(groups):
        raise ValueError("至少两个不重复的组")
    if sum(g["role"] == "control" for g in groups) != 1:
        raise ValueError("必须且只能有一个对照组")
    d0 = _date(b.get("start"), "开窗日")
    d1 = _date(b.get("end") or jobs.yesterday(), "结束日")
    if d1 < d0:
        raise ValueError("结束日早于开窗日")
    launch = _date(b["launch"], "上线日") if b.get("launch") else d0
    if launch > d0:
        raise ValueError("上线日不能晚于开窗日")
    mm = store.metric_map()
    keys = [k for k in b.get("metric_keys") or [] if k in mm]
    if not keys:
        raise ValueError("至少勾选一个指标")
    daily = [k for k in b.get("daily_keys") or [] if k in keys]
    # KG 单次最多回 1000 行：By天 每组每天一行（含冲突组 X）
    if daily and ((jobs.parse(d1) - jobs.parse(d0)).days + 1) * (len(groups) + 1) > 1000:
        raise ValueError("By天 出数：天数 ×（组数 + 1）不能超过 1000，请缩短窗口")
    excl = sorted({str(x).strip() for x in b.get("excl_src") or [] if str(x).strip()})
    if any(x not in ("0", "2", "5") for x in excl):
        raise ValueError("排除的 source 只能是 0 / 2 / 5")
    return dict(name=name, market=m, groups=groups, start=d0, end=d1, metric_keys=keys, regions=regions, daily_keys=daily,
                launch=launch, excl_src=excl)


@app.post("/api/experiments")
def exp_create():
    n = _parse_exp(request.get_json(force=True) or {})
    eid = store.create_experiment(n["name"], n["market"], n["groups"], n["start"], n["end"], n["metric_keys"],
                                  n["regions"], n["daily_keys"], n["launch"], n["excl_src"])
    return jsonify(id=eid, job_id=jobs.submit("experiment", {"exp_id": eid}))


@app.put("/api/experiments/<int:eid>")
def exp_update(eid):
    """编辑：口径没变只补算新增的指标 / By天；改了集群、国家、分组、窗口才整体重跑。"""
    e = store.experiment(eid)
    if not e:
        return jsonify(error="不存在"), 404
    if e["status"] in ("queued", "running") or e.get("pending"):
        raise ValueError("正在跑数，跑完再编辑")
    n = _parse_exp(request.get_json(force=True) or {})
    # 封顶口径 = 市场 + 国家 + 上线日（老实验没存上线日 = 开窗日）
    scope = lambda x: (x["market"], sorted(x.get("regions") or []), int(x.get("launch") or x["start"]),
                       sorted(x.get("excl_src") or []))
    base = lambda x: scope(x) + ([(g["id"], g["role"]) for g in x["groups"]], int(x["start"]), int(x["end"]))
    if base(n) != base(e):
        store.update_experiment(eid, **n, caps={} if scope(n) != scope(e) else e["caps"], last_end=None, error=None,
                                status="queued")
        store.delete_exp_results(eid)
        # 开窗日还没到（如按规划文档改了上线日）：只标等待，由每日调度有数后开跑
        return jsonify(mode="full", job_id=jobs.kick(eid))
    old_m, old_d = set(e["metric_keys"]), set(e.get("daily_keys") or [])
    add_m = [k for k in n["metric_keys"] if k not in old_m]
    add_d = [k for k in n["daily_keys"] if k not in old_d]
    drop = [k for k in old_m if k not in n["metric_keys"]]
    drop += [k + "@day" for k in old_d if k not in n["daily_keys"]]
    gone = [k for k in drop if k in old_m]
    store.delete_exp_results(eid, drop + [k + s for k in gone for s in ("@day", "@c3", "@c7", "@c14")])
    store.update_experiment(eid, **n)
    if not (add_m or add_d):
        return jsonify(mode="none")
    if not e.get("last_end"):           # 从没跑完过：只补新增指标会让原有指标一直没结果
        store.update_experiment(eid, status="queued", error=None)
        return jsonify(mode="full", job_id=jobs.kick(eid))
    return jsonify(mode="incremental", add=add_m, add_daily=add_d,
                   job_id=jobs.submit("experiment", {"exp_id": eid, "only": add_m, "only_daily": add_d}))


@app.get("/api/experiments/<int:eid>")
def exp_get(eid):
    e = store.experiment(eid)
    if not e:
        return jsonify(error="不存在"), 404
    e["view"] = jobs.experiment_view(e, request.args.get("base") or None)
    from . import observe
    e["segment"] = observe.segment_of(e)          # 分段实验（中途改参照）：详情页顶部提示 + 另一段链接
    e.pop("results", None)
    return jsonify(_lan_err(e, "error"))


@app.get("/api/observe")
def observe_get():
    """实验观测：排序实验按轮次 × 机房汇总，只读、不拉数（局域网只读分享可看）。"""
    from . import expsync, observe
    v = observe.view()
    v["sync"] = _lan_err(expsync.summary(), "error")
    if not is_local() and v["sync"].get("report"):
        # 只读分享不给「按文档更新并重跑」/「按文档改」的表单（写接口本来也被拦）
        v["sync"]["report"] = [{k: x for k, x in c.items() if k not in ("fixes", "dec_fixes")} for c in v["sync"]["report"]]
    return jsonify(v)


@app.post("/api/observe/sync")
def observe_sync():
    """同步实验配置：读规划文档 → 建缺的回收实验 + 核验（只读分享被 _guard 拦在写接口外）。"""
    from . import expsync
    ok = expsync.sync_config(lambda s: None)
    return jsonify(ok=ok, error=store.exp_config().get("error") if not ok else None)


@app.get("/api/versions")
def versions_get():
    """「分开看」新版本列表 + 上次同步结果（指标库页展示）。"""
    from . import versync
    return jsonify(_lan_err(versync.summary(), "error"))


@app.post("/api/versions/sync")
def versions_sync():
    """同步版本：读发版记录表 → 更新列表；有新版本就提交「分开看」补算（只读分享被 _guard 拦在写接口外）。"""
    from . import versync
    return jsonify(versync.sync(lambda s: None))


@app.post("/api/observe/decision")
def observe_decision():
    """「按文档改」决策标签：只接受这次核验报告里列出的那一条（轮 / 机房 / 组 / 目标决策都要对上），写库后重新核对。"""
    from . import expsync
    b = request.get_json(force=True) or {}
    if not isinstance(b, dict):
        raise ValueError("请求体要是 JSON 对象")
    key = (b.get("no"), b.get("market"), b.get("gid"), b.get("want"))
    rep = store.exp_config().get("report") or []
    f = next((f for c in rep for f in c.get("dec_fixes") or [] if (f["no"], f["market"], f["gid"], f["want"]) == key), None)
    if not f:
        return jsonify(error="核验结果里没有这一条（可能已改过或文档又变了），请先点「同步实验配置」"), 409
    store.set_decision(f["no"], f["market"], f["gid"], f["want"], f["src"])
    expsync.recheck()
    return jsonify(ok=True)


@app.post("/api/experiments/<int:eid>/rerun")
def exp_rerun(eid):
    """全窗口结果齐、只缺 By天 / 分位数时，只补缺的；否则整体重跑。"""
    e = store.experiment(eid)
    if not e:
        return jsonify(error="不存在"), 404
    have = {r["metric_key"] for r in e["results"]}
    mm = store.metric_map()
    ratio = [k for k in e["metric_keys"] if k in mm and mm[k].get("kind", "ratio") == "ratio"]
    if e["status"] != "failed" and e.get("last_end") and all(k in have for k in ratio):
        miss_m = [k for k in e["metric_keys"] if k in mm and k not in have]
        miss_d = [k for k in e.get("daily_keys") or [] if k + "@day" not in have]
        miss_c = [k for k in ratio for c, _ in jobs.cum_cutoffs(int(e["start"]), int(e["last_end"]))
                  if "%s@c%d" % (k, c) not in have]
        if miss_m or miss_d or miss_c or (ratio and (any(k not in have for k in jobs.VIS_KEYS) or e.get("vis_error")
                                                      or jobs._cuped_window_changed(e))):
            return jsonify(mode="incremental", add=miss_m, add_daily=miss_d,
                           job_id=jobs.submit("experiment", {"exp_id": eid, "only": miss_m, "only_daily": miss_d}))
        # 已冻结且结果齐全：不重扫整个窗口（SG 一次几十分钟、结果也不会变），只重算辅助读数
        if e["status"] == "frozen" and ratio:
            return jsonify(mode="incremental", add=[], add_daily=[],
                           job_id=jobs.submit("experiment", {"exp_id": eid, "only": [], "only_daily": [], "refresh_vis": True}))
    return jsonify(mode="full", job_id=jobs.submit("experiment", {"exp_id": eid}))


@app.delete("/api/experiments/<int:eid>")
def exp_delete(eid):
    store.delete_experiment(eid)
    return jsonify(ok=True)


# ── 热词运营 ──

@app.get("/api/hotword")
def hotword_get():
    from . import hotword
    a = request.args
    d1 = _date(a.get("end") or jobs.yesterday(), "end")
    d0 = _date(a.get("start") or hotword.START, "start")
    if d1 < d0:
        raise ValueError("结束日早于开始日")
    v = hotword.view(d0, d1, a.get("market") or "ALL")
    j = store.last_job("hotword")
    v["job"] = _lan_err({k: j[k] for k in ("id", "status", "error", "finished_at")}, "error") if j else None
    _lan_err(v["config"], "error")
    return jsonify(v)


@app.post("/api/hotword/refresh")
def hotword_refresh():
    from . import hotword
    b = request.get_json(force=True) or {}
    if b.get("config_only"):
        ok = hotword.sync_config(lambda s: None)
        cfg = store.hw_config()
        return jsonify(ok=ok, error=cfg.get("error") if not ok else None)
    d1 = _date(b.get("end") or jobs.yesterday(), "end")
    d0 = max(_date(b.get("start") or hotword.START, "start"), hotword.START)
    if d1 < d0:
        raise ValueError("结束日早于开始日")
    return jsonify(job_id=jobs.submit("hotword", {"start": d0, "end": d1}))


@app.get("/api/jobs")
def job_list():
    return jsonify(store.jobs())


@app.post("/api/jobs/<int:jid>/cancel")
def job_cancel(jid):
    r = jobs.cancel(jid)
    if r is None:
        return jsonify(error="任务不存在"), 404
    if r not in ("cancelled", "cancelling"):
        raise ValueError("任务已取消过" if r == "already_cancelled" else "任务已结束（%s），不能取消" % r)
    return jsonify(ok=True, state=r)


@app.get("/api/jobs/<int:jid>/queries")
def job_queries(jid):
    return jsonify(store.job_queries(jid))


_LOG_PATH = os.path.join(C.DATA_DIR, "server.log")
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3


class _StdErrToLog:
    """把 print(file=sys.stderr) 和未捕获异常转进 logging，避免轮转后与 launchd 的旧句柄分叉。"""

    def __init__(self):
        self._buf = ""
        self._busy = False

    def write(self, s):
        if self._busy:  # handler 自身报错时会回写 stderr，防止递归
            return
        self._busy = True
        try:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    logging.info(line)
        finally:
            self._busy = False

    def flush(self):
        if self._buf.strip():
            logging.info(self._buf)
            self._buf = ""


def _init_log():
    # server.log 按 5MB 轮转、保留 3 份；waitress 的 "Serving on" 经 root handler 落盘
    h = RotatingFileHandler(_LOG_PATH, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    sys.stderr = _StdErrToLog()


def main():
    _init_log()
    store.init()
    jobs.start_background()
    # waitress 替代 Werkzeug 开发服务器；不信任代理头，remote_addr 即真实对端
    # log_socket_errors=False：客户端断连 / 超时（RST、Errno 60）不刷 traceback；应用异常照常记日志
    import waitress
    waitress.serve(app, host=C.HOST, port=C.PORT, threads=8, log_socket_errors=False)


if __name__ == "__main__":
    main()
