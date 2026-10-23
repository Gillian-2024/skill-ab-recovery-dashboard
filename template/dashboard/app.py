"""Flask 入口：REST API + 静态单页。只监听 127.0.0.1（无鉴权）。"""
import json
import os
import re
import time

from flask import Flask, jsonify, request, send_from_directory

from . import config as C, jobs, sources, sqlgen, store

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


# 本机服务无鉴权，挡两类跨站请求：
# - DNS 重绑定：Host 必须是本机名，否则外站页面能读到实验结果；
# - CSRF：写接口只收 application/json（跨站发这个类型会先预检，本服务不回 CORS 头就被浏览器拦下），
#   再校验 Origin / Sec-Fetch-Site 必须同源，text/plain 等「简单请求」直接拒。
_HOSTS = {"127.0.0.1", "localhost"}


@app.before_request
def _same_origin_only():
    port = str(C.cfg["server"]["port"])
    host, _, p = (request.host or "").partition(":")
    if host not in _HOSTS or (p and p != port):
        return jsonify(error="只接受本机访问"), 403
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.mimetype != "application/json":
        return jsonify(error="写接口只接受 application/json"), 415
    origin = request.headers.get("Origin")
    if origin is not None and origin not in {"http://%s:%s" % (h, port) for h in _HOSTS}:
        return jsonify(error="跨站请求已拒绝"), 403
    if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
        return jsonify(error="跨站请求已拒绝"), 403
    return None


@app.get("/")
def index():
    return send_from_directory(C.WEB_DIR, "index.html")


@app.get("/static/<path:p>")
def static_files(p):
    return send_from_directory(C.WEB_DIR, p)


@app.get("/api/meta")
def meta():
    c = C.cfg
    f = c["funnel"]
    sub = c.get("subscope")
    return jsonify(
        title=c["title"], scopes=[{"key": s["key"], "label": s["label"]} for s in c["scopes"]],
        subscope=({"label": sub["label"], "values": sub["values"], "labels": sub["labels"]} if sub else None),
        has_dau={s["key"]: bool((f.get("dau") or {}).get("where_by_scope", {}).get(s["key"]) is not None) for s in c["scopes"]},
        steps=[{k: s.get(k) for k in ("key", "label", "segment", "event")} for s in f["steps"]],
        extra_steps=[{k: s.get(k) for k in ("key", "label", "event")} for s in f["extra_steps"]],
        tiers=[{"key": t["key"], "label": t["label"], "skip": t.get("skip") or [], "union": t.get("union") or []} for t in f["tiers"]],
        core=f["core"], anchor=f["anchor"], completeness_step=f["completeness"]["step"], kpis=f["kpis"], breakdown=bool(f.get("breakdown")),
        breakdown_label=(f.get("breakdown") or {}).get("label", ""), labels=c.get("labels") or {},
        notes=c.get("notes") or {}, yesterday=jobs.yesterday(), events=C.events(),
        str_cols=c["table"]["columns"]["string"], num_cols=c["table"]["columns"]["number"], bool_cols=c["table"]["columns"]["bool"],
        ops=sorted(sqlgen.OPS), cadence=c["experiment"]["cadence"], alpha=c["experiment"]["alpha"], srm_p=c["experiment"]["srm_p"],
        cap_pre_days=c["experiment"]["cap_pre_days"], source_type=c["source"]["type"],
        auth_ok=sources.auth_ok(), daily_at=c["schedule"]["daily_at"], backfill_days=c["schedule"]["backfill_days"])


@app.get("/api/funnel")
def funnel():
    v = request.args.get("view") or C.cfg["scopes"][0]["key"]
    sqlgen.split_view(v)
    d0, d1 = _date(request.args.get("start"), "start"), _date(request.args.get("end"), "end")
    flow, bd, dau, st = store.funnel(v, d0, d1)
    return jsonify(flow=flow, breakdown=bd, dau=dau, status=st)


@app.post("/api/refresh")
def refresh():
    b = request.get_json(force=True) or {}
    d0, d1 = _date(b.get("start"), "start"), _date(b.get("end"), "end")
    if d1 < d0:
        raise ValueError("结束日早于开始日")
    vs = b.get("views") or [s["key"] for s in C.cfg["scopes"]]
    for v in vs:
        sqlgen.split_view(v)
    if (jobs.parse(d1) - jobs.parse(d0)).days > 31:
        raise ValueError("单次刷新最多 31 天")
    return jsonify(job_id=jobs.submit("funnel", {"views": vs, "start": d0, "end": d1}))


@app.get("/api/metrics")
def metrics():
    return jsonify(store.metrics())


@app.post("/api/metrics/preview")
def metric_preview():
    d = sqlgen.validate_metric(request.get_json(force=True) or {})
    fake = {"key": "m", "name": d["name"], "def": d}
    y = jobs.yesterday()
    d0 = jobs.ymd(jobs.parse(y) - jobs.dt.timedelta(days=6))
    sc = C.cfg["scopes"][0]["key"]
    if d.get("kind", "ratio") == "ratio":
        sql = sqlgen.experiment_ratio_sql(sc, ["A", "B"], d0, y, [fake], {"m": 1000})
    else:
        sql = sqlgen.experiment_quantile_sql(sc, ["A", "B"], d0, y, fake)
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
    """影响数值的字段（改名 / 角色 / 备注不算口径变化）。"""
    return {k: d.get(k) for k in ("kind", "numerator", "denominator", "numerator_within_denominator", "device_scope", "cap",
                                  "source", "column", "quantiles", "lower_is_better")}


@app.get("/api/metrics/<key>/usage")
def metric_usage(key):
    return jsonify([{"id": e["id"], "name": e["name"], "status": e["status"]} for e in store.experiments_using(key)])


@app.put("/api/metrics/<key>")
def metric_update(key):
    """口径变了 → 用过它的实验删掉该指标旧结果（含 By天 / 累计行 / P99 封顶）并排补算；只改名字 / 备注不重算。"""
    b = request.get_json(force=True) or {}
    old = store.metric_map().get(key)
    if not old:
        return jsonify(error="不存在"), 404
    restore = bool(b.pop("restore", False))
    if restore:
        d = dict(store.presets()).get(key)
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
    reruns = []
    if changed:
        sfx = ["@day"] + ["@c%d" % c for c in C.cfg["experiment"]["cum_days"]]
        for e in store.experiments_using(key):
            store.delete_exp_results(e["id"], [key] + [key + s for s in sfx])
            store.update_experiment(e["id"], caps={k: v for k, v in (e.get("caps") or {}).items() if k != key})
            if e.get("last_end"):
                reruns.append({"exp": e["name"], "job_id": jobs.submit("experiment", {
                    "exp_id": e["id"], "only": [key], "only_daily": [key] if key in (e.get("daily_keys") or []) else []})})
    return jsonify(ok=True, changed=changed, rerun=reruns)


@app.delete("/api/metrics/<key>")
def metric_delete(key):
    store.delete_metric(key)
    return jsonify(ok=True)


@app.get("/api/experiments")
def exp_list():
    return jsonify([dict(e, stage=jobs.stage_of(e)) for e in store.experiments()])


@app.get("/api/health")
def health():
    """页面顶部提醒：凭据是否就位 + 当前失败的实验（常驻）+ 近 24 小时失败且未被后续成功覆盖的任务。"""
    since, js = time.time() - 86400, store.jobs(200)
    exps = store.experiments()
    ids = {e["id"] for e in exps}

    def resolved(j):
        p = json.loads(j["params"] or "{}")
        if "exp_id" in p:
            return p["exp_id"] not in ids or any(x["id"] > j["id"] and x["status"] == "done" and
                                                 json.loads(x["params"] or "{}").get("exp_id") == p["exp_id"] for x in js)
        if j["kind"] == "funnel":
            ds = list(jobs.days_between(p["start"], p["end"]))
            return all(all(store.day_status(v).get(d) for d in ds) for v in p.get("views") or [])
        # 其他：之后同类、同参数的任务跑成功过
        return p in [json.loads(x["params"] or "{}") for x in js if x["id"] > j["id"] and x["kind"] == j["kind"] and x["status"] == "done"]

    bad = [{"id": e["id"], "name": e["name"], "partial": e["status"] != "failed", "error": (e["error"] or "")[:200]}
           for e in exps if e["status"] == "failed" or (e.get("error") and not e.get("pending"))]
    bad_ids = {e["id"] for e in bad}
    fails = []
    for j in js:
        if j["created_at"] < since:
            break
        if j["status"] == "failed" and not resolved(j) and json.loads(j["params"] or "{}").get("exp_id") not in bad_ids:
            fails.append({"id": j["id"], "kind": j["kind"], "error": (j["error"] or "")[:200], "at": j["finished_at"]})
    login = any(any(h in (f["error"] or "").lower() for h in sources._AUTH_HINTS) for f in fails + bad)
    return jsonify(auth_ok=sources.auth_ok(), login_suspect=login, fails=fails, failed_exps=bad,
                   auth_hint=C.cfg["source"].get("auth_hint", ""))


def _parse_exp(b):
    name = str(b.get("name", "")).strip()
    if not name:
        raise ValueError("实验名不能为空")
    sc = b.get("scope")
    sqlgen.scope_of(sc)
    subs = sorted({str(r).strip() for r in b.get("subs") or [] if str(r).strip()})
    sqlgen.check_subs(subs)
    groups = []
    for g in b.get("groups") or []:
        gid = sqlgen.check_gid(str(g.get("id", "")).strip())
        if g.get("role") not in ("control", "treatment"):
            raise ValueError("每个组都要选对照 / 实验")
        w = float(g.get("weight") or 1)
        if w <= 0:
            raise ValueError("流量占比要 > 0")
        groups.append({"id": gid, "role": g["role"], "label": str(g.get("label") or gid)[:30], "weight": w})
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
    if daily and ((jobs.parse(d1) - jobs.parse(d0)).days + 1) * (len(groups) + 1) > int(C.cfg["source"]["max_rows"]):
        raise ValueError("By天 出数：天数 ×（组数 + 1）超过 source.max_rows，请缩短窗口")
    return dict(name=name, scope=sc, groups=groups, start=d0, end=d1, metric_keys=keys, subs=subs, daily_keys=daily, launch=launch)


@app.post("/api/experiments")
def exp_create():
    n = _parse_exp(request.get_json(force=True) or {})
    eid = store.create_experiment(n["name"], n["scope"], n["groups"], n["start"], n["end"], n["metric_keys"], n["subs"], n["daily_keys"], n["launch"])
    return jsonify(id=eid, job_id=jobs.submit("experiment", {"exp_id": eid}))


@app.put("/api/experiments/<int:eid>")
def exp_update(eid):
    """口径没变只补算新增的指标 / By天；改了范围、细分、分组、窗口、上线日才整体重跑。"""
    e = store.experiment(eid)
    if not e:
        return jsonify(error="不存在"), 404
    if e["status"] in ("queued", "running") or e.get("pending"):
        raise ValueError("正在跑数，跑完再编辑")
    n = _parse_exp(request.get_json(force=True) or {})
    scope = lambda x: (x["scope"], sorted(x.get("subs") or []), int(x.get("launch") or x["start"]))
    base = lambda x: scope(x) + ([(g["id"], g["role"], float(g.get("weight") or 1)) for g in x["groups"]], int(x["start"]), int(x["end"]))
    if base(n) != base(e):
        store.update_experiment(eid, **n, caps={} if scope(n) != scope(e) else e["caps"], last_end=None, error=None, status="queued")
        store.delete_exp_results(eid)
        return jsonify(mode="full", job_id=jobs.submit("experiment", {"exp_id": eid}))
    old_m, old_d = set(e["metric_keys"]), set(e.get("daily_keys") or [])
    add_m = [k for k in n["metric_keys"] if k not in old_m]
    add_d = [k for k in n["daily_keys"] if k not in old_d]
    gone = [k for k in old_m if k not in n["metric_keys"]]
    sfx = ["@day"] + ["@c%d" % c for c in C.cfg["experiment"]["cum_days"]]
    store.delete_exp_results(eid, gone + [k + s for k in gone for s in sfx] + [k + "@day" for k in old_d if k not in n["daily_keys"]])
    store.update_experiment(eid, **n)
    if not (add_m or add_d):
        return jsonify(mode="none")
    return jsonify(mode="incremental", add=add_m, add_daily=add_d,
                   job_id=jobs.submit("experiment", {"exp_id": eid, "only": add_m, "only_daily": add_d}))


@app.get("/api/experiments/<int:eid>")
def exp_get(eid):
    e = store.experiment(eid)
    if not e:
        return jsonify(error="不存在"), 404
    e["view"] = jobs.experiment_view(e, request.args.get("base") or None)
    e.pop("results", None)
    return jsonify(e)


@app.post("/api/experiments/<int:eid>/rerun")
def exp_rerun(eid):
    """全窗口结果齐、只缺 By天 / 累计 / 分位数时，只补缺的；否则整体重跑。"""
    e = store.experiment(eid)
    if not e:
        return jsonify(error="不存在"), 404
    have = {r["metric_key"] for r in e["results"]}
    mm = store.metric_map()
    ratio = [k for k in e["metric_keys"] if k in mm and mm[k].get("kind", "ratio") == "ratio"]
    if e["status"] != "failed" and e.get("last_end") and all(k in have for k in ratio):
        miss_m = [k for k in e["metric_keys"] if k in mm and k not in have]
        miss_d = [k for k in e.get("daily_keys") or [] if k + "@day" not in have]
        miss_c = [k for k in ratio for c, _ in jobs.cum_cutoffs(int(e["start"]), int(e["last_end"])) if "%s@c%d" % (k, c) not in have]
        if miss_m or miss_d or miss_c or (ratio and "@dist" not in have):
            return jsonify(mode="incremental", job_id=jobs.submit("experiment", {"exp_id": eid, "only": miss_m, "only_daily": miss_d}))
    return jsonify(mode="full", job_id=jobs.submit("experiment", {"exp_id": eid}))


@app.delete("/api/experiments/<int:eid>")
def exp_delete(eid):
    store.delete_experiment(eid)
    return jsonify(ok=True)


@app.get("/api/jobs")
def job_list():
    return jsonify(store.jobs())


@app.post("/api/jobs/<int:jid>/cancel")
def job_cancel(jid):
    r = jobs.cancel(jid)
    if r is None:
        return jsonify(error="任务不存在"), 404
    return jsonify(ok=True, state=r)


@app.get("/api/jobs/<int:jid>/queries")
def job_queries(jid):
    return jsonify(store.job_queries(jid))


def _load_env():
    """读项目根目录 .env（KEY=VALUE），不覆盖已有环境变量；不引入 python-dotenv。"""
    p = os.path.join(C.ROOT, ".env")
    if not os.path.exists(p):
        return
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    _load_env()
    if not os.path.exists(C.CONFIG_PATH):
        raise SystemExit("没有找到 %s：先复制 dashboard.example.yaml 改成自己的口径" % C.CONFIG_PATH)
    try:
        C.load()
    except C.ConfigError as ex:
        raise SystemExit("%s\n改完可运行 python3 tools/check_config.py 校验" % ex)
    store.init()
    jobs.start_background()
    app.run(host=C.cfg["server"]["host"], port=int(C.cfg["server"]["port"]), debug=False, threaded=True)


if __name__ == "__main__":
    main()
