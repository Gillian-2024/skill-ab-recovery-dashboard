"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = v => v == null || isNaN(v) ? "—" : Math.round(v).toLocaleString("en-US");
const pct = (v, d = 2) => v == null || !isFinite(v) ? "—" : (v * 100).toFixed(d) + "%";
const toISO = n => { const s = String(n); return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}`; };
const toNum = s => s.replaceAll("-", "");
const addDays = (n, k) => { const d = new Date(toISO(n) + "T00:00:00"); d.setDate(d.getDate() + k);
  return +`${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`; };

async function api(path, opt = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opt,
    body: opt.body ? JSON.stringify(opt.body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

let META = null;
const S = { market: "ALL", region: [], tier: "all", trend: "rates", wk: "ns", split: "ctr" };
// 漏斗视图键：整机房 = 'SG'；单个国家 = 'SG:ID'；多国合计 = 'SG:BR+ID'（代码升序，同一组合同一份数据）
const scopeKey = () => S.region.length ? `${S.market}:${[...S.region].sort().join("+")}` : S.market;
const scopeText = () => S.region.length ? `${CLUSTER[S.market] || S.market} · ${S.region.length === 1 ? `${REG_NAME(S.region[0])}（${S.region[0]}）` : [...S.region].sort().join(" + ") + " 合计"}`
  : S.market === "ALL" ? "全机房（RU + SGP + EU）" : (S.market === "RU" ? "RU" : `${CLUSTER[S.market] || S.market} 整机房`);
// 搜索渗透率要 DAU：选了国家、或机房本身有 DAU 口径（RU）；整机房 / 全机房没有
const hasDau = () => !!(S.region.length || (META.has_dau || {})[S.market]);
const TIER_NAME = { all: "全量", site: "站内", tb: "Toolbar" };

// ─────────── 一键复制表格 ───────────
// 每个组件渲染时登记 COPY[key] = () => ({ caption, header, rows })；
// 复制时同时写 text/html（飞书文档粘成表格）和 text/plain TSV（Excel 按列拆分）
const COPY = {};
const cellV = c => c && typeof c === "object" ? c.v : c;
const tsvCell = c => String(cellV(c) ?? "").replace(/[\t\r\n]+/g, " ");
const CELL_STYLE = { dl: "font-weight:700", up: "color:#006300;font-weight:700", down: "color:#d03b3b;font-weight:700" };
// t = { caption, header, rows } 或 { sections: [同结构...] }；单元格可为字符串或 { v, cls }
function copyPayload(t) {
  const secs = t.sections || [t];
  const td = (c, tag) => `<${tag} style="border:1px solid #ccc;padding:4px 8px;${c && c.cls ? c.cls.split(" ").map(k => CELL_STYLE[k] || "").join(";") : ""}">${esc(cellV(c))}</${tag}>`;
  const tsv = secs.map(x => [x.caption, x.header.map(tsvCell).join("\t"), ...x.rows.map(r => r.map(tsvCell).join("\t"))]
    .filter(y => y != null).join("\n")).join("\n\n");
  const html = secs.map(x => (x.caption ? `<p><b>${esc(x.caption)}</b></p>` : "") +
    `<table style="border-collapse:collapse"><thead><tr>${x.header.map(h => td(h, "th")).join("")}</tr></thead>` +
    `<tbody>${x.rows.map(r => `<tr>${r.map(v => td(v, "td")).join("")}</tr>`).join("")}</tbody></table>`).join("<p></p>");
  return { tsv, html, n: secs.reduce((a, x) => a + x.rows.length, 0) };
}
async function copyTable(key, btn) {
  const t = COPY[key] && COPY[key]();
  if (!t || !(t.sections || [t]).some(x => x.rows.length)) { flash(btn, "暂无数据", false); return; }
  const { tsv, html, n } = copyPayload(t);
  try {
    await navigator.clipboard.write([new ClipboardItem({
      "text/html": new Blob([html], { type: "text/html" }), "text/plain": new Blob([tsv], { type: "text/plain" }) })]);
  } catch (e) {
    // 退路：选中临时节点走 execCommand，同样带 HTML
    const box = document.createElement("div");
    box.style.cssText = "position:fixed;left:-9999px;top:0"; box.innerHTML = html; document.body.appendChild(box);
    const rg = document.createRange(); rg.selectNodeContents(box);
    const sel = getSelection(); sel.removeAllRanges(); sel.addRange(rg);
    const ok = document.execCommand("copy"); sel.removeAllRanges(); box.remove();
    if (!ok) { flash(btn, "复制失败", false); return; }
  }
  flash(btn, `已复制 ${n} 行`, true);
}
function flash(btn, text, ok) {
  const old = btn.dataset.label || (btn.dataset.label = btn.textContent);
  btn.textContent = text; btn.classList.toggle("done", ok);
  clearTimeout(btn._t); btn._t = setTimeout(() => { btn.textContent = old; btn.classList.remove("done"); }, 1600);
}
document.addEventListener("click", ev => { const b = ev.target.closest("[data-copy]"); if (b) copyTable(b.dataset.copy, b); });
const tip = $("#tip");
function showTip(html, ev) {
  tip.innerHTML = html; tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.hidden = true; };

// ─────────── 路由 ───────────
function route() {
  let t = (location.hash || "#funnel").slice(1);
  // #exp/<id>：直接打开某个实验（主动通知里的链接）
  const m = /^exp\/(\d+)$/.exec(t);
  if (m) { CUR_EXP = +m[1]; t = "exp"; }
  $$(".tab").forEach(s => s.hidden = s.id !== "tab-" + t);
  $$("nav a").forEach(a => a.classList.toggle("on", a.dataset.tab === t));
  ({ funnel: loadFunnel, exp: loadExpList, obs: loadObs, hot: loadHot, metrics: loadMetrics, logs: loadJobs })[t]?.();
}
addEventListener("hashchange", route);

// ─────────── 漏斗 ───────────
const TIERS = { all: ["pv", "uv"], site: ["pv_site", "uv_site"], tb: ["pv_tb", "uv_tb"] };
const TIERS_NF = { all: ["pv_nf", "uv_nf"], site: ["pv_site_nf", "uv_site_nf"], tb: ["pv_tb", "uv_tb"] };
const NF_STEPS = new Set(["card_expose", "card_click"]);

function stepVal(r, ek, tier) {
  if (!r || !r[ek]) return { pv: null, uv: null };
  // Toolbar 档的卡片步骤也排兜底：PV 相减精确；UV 用 uv_tb_nf（11-21 起补的列），还没补的老行退回 uv_nf − uv_site_nf（两档设备约 1% 重叠，略偏低）
  if (NF_STEPS.has(ek) && tier === "tb") { const x = r[ek];
    return { pv: x.pv_nf - x.pv_site_nf, uv: x.uv_tb_nf ?? x.uv_nf - x.uv_site_nf, approx: x.uv_tb_nf == null }; }
  const [p, u] = (NF_STEPS.has(ek) ? TIERS_NF : TIERS)[tier];
  return { pv: r[ek][p], uv: r[ek][u] };
}
// 北极星三档并排：总 / Toolbar / 主动搜（= 站内，from ≠ toolbar），不跟随档位按钮
const NS3 = [["all", "总"], ["tb", "Toolbar"], ["site", "主动搜"]];
const nsDay = (r, t) => ratio(stepVal(r, "card_click", t).uv, stepVal(r, "result_load", t).uv);

let FUNNEL = null;
let FUNNEL_SEQ = 0;
async function loadFunnel() {
  const d0 = toNum($("#fStart").value), d1 = toNum($("#fEnd").value), seq = ++FUNNEL_SEQ;
  try {
    // 「较上一周」：取紧挨着的前一段同长窗口（7 天窗口即上一周）
    const len = Math.round((new Date(toISO(d1)) - new Date(toISO(d0))) / 864e5) + 1;
    const [cur, prev] = await Promise.all([
      api(`/api/funnel?market=${encodeURIComponent(scopeKey())}&start=${d0}&end=${d1}`),
      api(`/api/funnel?market=${encodeURIComponent(scopeKey())}&start=${addDays(+d0, -len)}&end=${addDays(+d0, -1)}`)]);
    if (seq !== FUNNEL_SEQ) return;   // 期间又切了市场 / 日期：丢掉旧响应
    FUNNEL = cur; FUNNEL.prev = { flow: prev.flow || {}, status: prev.status || {}, d0: addDays(+d0, -len), d1: addDays(+d0, -1), len };
  } catch (e) { $("#fNotice").innerHTML = `<div class="notice crit"><b>读取失败</b>${esc(e.message)}</div>`; return; }
  loadCoreWeekly();   // 本地库查询，随漏斗一起刷新（刷新数仓后本周值也会变）
  renderFunnel(d0, d1);
}

function byDate() {
  const m = {};
  for (const r of FUNNEL.rows) (m[r.date] ||= {})[r.ek] = r;
  return m;
}

function renderFunnel(d0, d1) {
  const days = [];
  for (let d = +d0; d <= +d1; d = addDays(d, 1)) days.push(d);
  const bd = byDate();
  const have = days.filter(d => bd[d]);
  const bad = days.filter(d => FUNNEL.status[d] && !FUNNEL.status[d].complete);
  const missing = days.filter(d => !FUNNEL.status[d]);
  let n = "";
  if (!have.length) n += `<div class="notice info"><b>暂无数据</b>${S.region.length ? `${esc(scopeText())} 还没拉过` : "这个窗口还没从数仓拉过"}，${META.readonly ? "请找看板维护人拉数" : "点右上角「从数仓刷新」（一次约 1~3 分钟/市场）"}。拉过一次后每天 ${META.daily_at} 自动续。</div>`;
  else if (missing.length) n += `<div class="notice info"><b>部分日期未拉取</b>${missing.map(toISO).join("、")}</div>`;
  if (bad.length) n += `<div class="notice warn"><b>⚠ 数据可能不完整</b>${bad.map(d => toISO(d) + "：" + esc(FUNNEL.status[d].note)).join("；")}。系统会在 ${META.daily_at} 后每 2 小时重试。</div>`;
  if (S.tier !== "all") n += `<div class="notice info"><b>注意</b>无结果事件没有 from，站内 / Toolbar 档的无结果率不可拆，只显示全量。</div>`;
  if (S.market === "ALL" && have.length) n += `<div class="notice info"><b>全机房</b>RU（region = RU）+ SGP + EU 三个机房已拉好的数按天相加，三个机房都有数的天才出；UV 直接相加（三个机房设备几乎不重叠：10-20~26 搜索设备 186 万台里跨机房 12 台）；人均 VV 各机房先按自己当天的 P99 封顶再相加。「从数仓刷新」会把三个机房一起排队。</div>`;
  $("#fNotice").innerHTML = n;

  const good = have.filter(d => !bad.includes(d));
  const use = good.length ? good : have;
  const avg = ek => {
    const xs = use.map(d => stepVal(bd[d], ek, S.tier).uv).filter(v => v != null);
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  };
  const avgAll = ek => {
    const xs = use.map(d => stepVal(bd[d], ek, "all").uv).filter(v => v != null);
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  };
  const load = avg("result_load"), click = avg("card_click"), cexp = avg("card_expose");
  const nr = avgAll("no_result"), loadAll = avgAll("result_load");
  const dauXs = use.map(d => FUNNEL.dau[d]).filter(Boolean);
  const dau = dauXs.length ? dauXs.reduce((a, b) => a + b, 0) / dauXs.length : null;
  // 点击→3s、人均 VV：设备-天口径，来自 core_daily；老数据没有这两列时提示重拉
  const core = FUNNEL.core || {}, cuse = use.filter(d => core[d]);
  const csum = k => cuse.reduce((a, d) => a + (core[d][`${k}_${S.tier}`] || 0), 0);
  const cnote = cuse.length < use.length ? `（${use.length - cuse.length} 天缺这项，点「从数仓刷新」补）` : "";
  const tiles = [
    ["结果页加载 UV（日均）", fmt(load), use.length + " 天" + (good.length < have.length ? "（已剔除不完整日）" : "")],
    ["结果页点击渗透率", pct(click / load), "卡片点击 UV（排兜底）÷ 结果页加载 UV；事件与过滤同实验（排兜底），这里按设备-天日均聚合"],
    ["卡片 CTR（UV）", pct(click / cexp), "排兜底"],
    ["点击 → 3s", cuse.length ? pct(ratio(csum("c3_num"), csum("c3_den"))) : "—", "当天点卡且有 3s 播放的设备 ÷ 点卡设备" + cnote],
    ["人均 VV", cuse.length && ratio(csum("vv_cap"), csum("vv_den")) != null ? ratio(csum("vv_cap"), csum("vv_den")).toFixed(2) : "—",
      "准备播放 ÷ 结果页加载设备，单设备按当天 P99 封顶" + (S.market === "ALL" ? "（各机房按自己的 P99）" : "") + cnote],
    ["无结果率（全量）", pct(nr / loadAll), "无结果 UV ÷ 结果页加载 UV"],
    ["搜索渗透率", dau && S.tier === "all" ? pct(load / dau) : "—",
      !hasDau() ? (S.market === "ALL" ? "全机房无 DAU 口径（选单个机房的国家后可算）" : "整机房无 DAU 口径（选国家后可算）") : S.tier !== "all" ? "只在全量档计算" : dau ? "÷ DAU（region_app 口径）" : "暂无 DAU 数据"],
  ];
  // 北极星单独一张宽卡，带窗口内逐日小折线；其余六个小卡
  // Toolbar / 主动搜各一张卡，固定档位、不跟随档位按钮（窗口日均：分子、分母各取日均再相除，同上）
  const avgT = (ek, t) => { const xs = use.map(d => stepVal(bd[d], ek, t).uv).filter(v => v != null); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null; };
  const approx = use.some(d => stepVal(bd[d], "card_click", "tb").approx);
  tiles.splice(2, 0, ...NS3.slice(1).map(([t, nm]) => [`结果页点击渗透率 · ${nm}`, pct(ratio(avgT("card_click", t), avgT("result_load", t))),
    (t === "tb" ? "from = toolbar" : "from ≠ toolbar") + "：本档卡片点击 UV（排兜底）÷ 本档结果页加载 UV；固定本档，不跟随档位按钮" + (t === "tb" && approx ? "（部分天为近似，见下方趋势卡说明）" : "")]));
  tiles[1][0] += S.tier === "all" ? " · 总" : ` · ${TIER_NAME[S.tier]}`;
  const [hero, ...rest] = [tiles[1], tiles[0], ...tiles.slice(2)];
  $("#fTiles").innerHTML = `<div class="tile hero"><div><div class="k">${hero[0]}</div><div class="v">${hero[1]}</div>` +
    `<div class="s">${hero[2]}；跟随档位按钮</div></div></div>` +
    // Toolbar / 主动搜两张卡固定在总的右侧上下排，其余卡依次排满两行
    rest.map(([k, v, s]) => `<div class="tile${k.endsWith(" · Toolbar") ? " ns-tb" : k.endsWith(" · 主动搜") ? " ns-site" : ""}"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`).join("");
  $("#fCtx").textContent = `${scopeText()} · ${TIER_NAME[S.tier]} · ${toISO(d0)} ~ ${toISO(d1)}，共 ${days.length} 天`;
  const ctx = `${scopeText()} · ${TIER_NAME[S.tier]} · ${toISO(d0)} ~ ${toISO(d1)}`;
  COPY.tiles = () => ({ caption: ctx + "（日均）", header: ["指标", "值", "说明"], rows: have.length ? tiles : [] });

  renderFlow(days, ctx, bad);

  // 趋势：复制时三组（转化率 / UV / VV）全给，用精确值不用缩写
  COPY.trend = () => {
    const fl = FUNNEL.flow || {}, cols = Object.values(TREND).flatMap(cfg => cfg.series.map(([name, f]) => [name + (cfg === TREND.uv ? " UV" : ""), f, cfg === TREND.rates ? pct : fmt]));
    return { caption: ctx + "（北极星为独立去重 UV 口径，其余为设备-天包含式）", header: ["日期", ...cols.map(c => c[0])],
      rows: days.filter(d => fl[d]).map(d => [toISO(d), ...cols.map(([, f, fm]) => fm(f(fl[d], S.tier, bd[d])))]) };
  };

  renderTrend(days, bd);
  renderNsMix(days, bd, ctx);
  renderPositions(ctx);
  renderTable(days, bd, bad, ctx);
  renderCoreWeekly();
  renderSplit(days);
}

// ─────────── 结果页 / 中间页「为你推荐」分开看：上方核心指标是合并口径（不断档），这里只看新版本设备 ───────────
// 每天一行 split_daily（全量档）；窗口日均 = 分子、分母分别加总后相除
const SPLIT = [
  // [键, 名称, 结果页(分子, 分母), 中间页(分子, 分母), 格式]
  ["base", "曝光 UV（日均）", ["res_load"], ["mid_expose"], fmt, "结果页加载 / 中间页曝光"],
  ["ctr", "点击渗透率", ["res_click", "res_load"], ["mid_rec_click", "mid_expose"], v => pct(v, 2), "结果页：点卡 ÷ 结果页加载；中间页：点「为你推荐」÷ 中间页曝光"],
  ["c3", "点击 → 3s", ["res_c3", "res_c3_den"], ["mid_c3", "mid_c3_den"], v => pct(v, 2), "点过且有该来源 3s 播放 ÷ 点过"],
  ["play", "播放 UV（日均）", ["res_play"], ["mid_play"], fmt, "有该来源起播的设备"],
  ["vv", "人均 VV", ["res_vv_cap", "res_load"], ["mid_vv_cap", "mid_expose"], v => v == null ? "—" : v.toFixed(2), "该来源起播次数（当天 P99 封顶）÷ 曝光 UV"],
];
function splitVal(rows, keys) {
  const sum = k => rows.reduce((a, r) => a + (r[k] || 0), 0);
  return keys.length === 1 ? (rows.length ? sum(keys[0]) / rows.length : null) : ratio(sum(keys[0]), sum(keys[1]));
}
function renderSplit(days) {
  const card = $("#fSplitCard"), sp = FUNNEL.split || {}, start = FUNNEL.split_start;
  const ds = days.filter(d => sp[d]);
  if (days[days.length - 1] < start) { card.hidden = true; return; }
  card.hidden = false;
  if (!ds.length) {
    $("#fSplit").innerHTML = `<p class="muted">这个窗口还没有分开看的数据，点右上角「从数仓刷新」补（${toISO(start)} 起才有）。</p>`;
    $("#fSplitTrend").innerHTML = ""; $("#splitSeg").innerHTML = ""; $("#fSplitFoot").textContent = "";
    COPY.split = () => ({ header: [], rows: [] });
    return;
  }
  const rows = ds.map(d => sp[d]);
  const header = ["指标", "结果页", "中间页「为你推荐」", "说明"];
  const body = SPLIT.map(([, name, r, m, f, note]) => [name, f(splitVal(rows, r)), f(splitVal(rows, m)), note]);
  const resP = splitVal(rows, ["res_play"]), midP = splitVal(rows, ["mid_play"]);
  $("#fSplit").innerHTML = `<div class="scroll"><table><thead><tr>${header.map(x => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>` +
    body.map(r => `<tr>${r.map((c, i) => `<td${i === 3 ? ' class="muted" style="text-align:left"' : ""}>${esc(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table></div>";
  COPY.split = () => ({ caption: `${scopeText()} · 结果页 / 中间页分开看（新版本，${toISO(ds[0])} ~ ${toISO(ds[ds.length - 1])}，日均）`, header, rows: body });
  $("#splitSeg").innerHTML = SPLIT.map(([k, name]) => `<button type="button" data-k="${k}" class="${S.split === k ? "on" : ""}">${name.replace("（日均）", "")}</button>`).join("");
  $$("#splitSeg button").forEach(b => b.onclick = () => { S.split = b.dataset.k; renderSplit(days); });
  const [, name, r, m, f] = SPLIT.find(x => x[0] === S.split) || SPLIT[(S.split = "ctr", 1)];
  const td = days.filter(d => d >= start);
  lineChart($("#fSplitTrend"), td, [
    { name: "结果页", color: "var(--s1)", pts: td.map(d => sp[d] ? splitVal([sp[d]], r) : null) },
    { name: "中间页「为你推荐」", color: "var(--s2)", pts: td.map(d => sp[d] ? splitVal([sp[d]], m) : null) },
  ], f, false, null, { key: "fSplitTrend:" + S.split, title: `${scopeText()} · ${name} · 结果页 vs 中间页（新版本）` });
  $("#fSplitFoot").innerHTML = `只看新版本设备（含结果页 from 修复的版本，按发版记录表精确匹配 ${FUNNEL.split_versions || ""} 个，同步于 ${FUNNEL.split_synced || "—"}），全量档，设备-天；${ds.length} 天日均。` +
    `结果页播放 = from=search，中间页播放 = from ∈ search_related / search_related_feed。中间页播放 UV 占两者之和 ${pct(ratio(midP, resP + midP), 1)}（两者有重叠，用户数不可直接相加）。` +
    `新版本残留的 search_related_feed 计入中间页、不剔除（约占中间页播放 PV 4%，约 90% 是 backup=1 兜底的旧缓存对象，入口归属核查显示基本不来自为你推荐，属版本过渡残留）。` +
    `上方核心指标、漏斗、趋势是合并口径（全版本、播放三种来源加总），与 11-16 前连续可比。`;
}

// ─────────── 核心指标周均走势（2026-08-03 起，自然周 周一~周日） ───────────
// 比率指标：周内分子、分母分别合计后再相除（与核心指标卡一致）；量级指标（加载 UV）取日均。不完整日不进
// 一次只画一条：七个指标单位不同（% / UV / 次），不共用 y 轴
const WK = [
  // [键, 名称, 取值(当天 core 行, 档位), 格式, 只有全量档, [分子, 分母]（比率指标按周合计再相除；没有 = 取日均）]
  ["ns", "结果页点击渗透率", (v, t) => ratio(v[`c3_den_${t}`], v[`vv_den_${t}`]), x => pct(x, 2), false,
    [(v, t) => v[`c3_den_${t}`], (v, t) => v[`vv_den_${t}`]]],
  ["load", "结果页加载 UV（日均）", (v, t) => v[`vv_den_${t}`], fmt],
  ["ctr", "卡片 CTR（UV）", (v, t) => ratio(v[`c3_den_${t}`], v[`expose_${t}`]), x => pct(x, 2), false,
    [(v, t) => v[`c3_den_${t}`], (v, t) => v[`expose_${t}`]]],
  ["c3s", "点击 → 3s", (v, t) => ratio(v[`c3_num_${t}`], v[`c3_den_${t}`]), x => pct(x, 2), false,
    [(v, t) => v[`c3_num_${t}`], (v, t) => v[`c3_den_${t}`]]],
  ["vv", "人均 VV", (v, t) => ratio(v[`vv_cap_${t}`], v[`vv_den_${t}`]), x => x == null ? "—" : x.toFixed(2), false,
    [(v, t) => v[`vv_cap_${t}`], (v, t) => v[`vv_den_${t}`]]],
  ["nr", "无结果率（全量）", v => ratio(v.nr_all, v.vv_den_all), x => pct(x, 2), true,
    [v => v.nr_all, v => v.vv_den_all]],
  ["pen", "搜索渗透率", v => ratio(v.vv_den_all, v.dau), x => pct(x, 2), true,
    [v => v.vv_den_all, v => v.dau]],
];
let CW = null;
async function loadCoreWeekly() {
  const key = scopeKey();
  let r;
  try { r = { key, ...(await api(`/api/core_weekly?market=${encodeURIComponent(key)}`)) }; }
  catch (e) { r = { key, error: e.message }; }
  if (key !== scopeKey()) return;     // 旧视图的响应晚到：不覆盖
  CW = r;
  if (FUNNEL) renderCoreWeekly();
}
function coreWeeks(f, t, nd) {
  const weeks = [];
  for (let w = CW.start; w <= CW.end; w = addDays(w, 7)) {
    const ds = [0, 1, 2, 3, 4, 5, 6].map(k => addDays(w, k)).filter(d => d <= CW.end);
    const rows = ds.map(d => CW.days[d]).filter(v => v && v.complete);
    const xs = rows.map(v => f(v, t)).filter(x => x != null && isFinite(x));
    const cs = nd ? rows.filter(r => nd[0](r, t) != null && nd[1](r, t) != null) : [];   // 比率指标：分子分母都能用那几天
    let v = null;
    if (nd) {
      const num = cs.reduce((a, r) => a + nd[0](r, t), 0), den = cs.reduce((a, r) => a + nd[1](r, t), 0);
      v = den ? num / den : null;
    } else if (xs.length) v = xs.reduce((a, b) => a + b, 0) / xs.length;
    weeks.push({ w, end: ds[ds.length - 1], n: nd ? cs.length : xs.length, span: ds.length, v, pooled: !!nd,
      bad: ds.filter(d => CW.days[d] && !CW.days[d].complete).length, miss: ds.filter(d => !CW.days[d]).length });
  }
  return weeks;
}
function renderCoreWeekly() {
  const el = $("#fCoreWeek"), ft = $("#fCoreWeekFoot");
  $("#wkSeg").innerHTML = WK.map(([k, name]) => `<button type="button" data-k="${k}" class="${S.wk === k ? "on" : ""}">${name.replace(/（日均）|（UV）/, "")}</button>`).join("");
  $$("#wkSeg button").forEach(b => b.onclick = () => { S.wk = b.dataset.k; renderCoreWeekly(); });
  if (!CW || CW.key !== scopeKey()) { el.innerHTML = `<p class="muted">加载中…</p>`; ft.innerHTML = ""; return; }
  if (CW.error) { el.innerHTML = `<div class="notice crit"><b>读取失败</b>${esc(CW.error)}</div>`; ft.innerHTML = ""; return; }
  const [, name, f, fm, allOnly, nd] = WK.find(x => x[0] === S.wk) || WK[S.wk = "ns", 0];
  const t = allOnly ? "all" : S.tier, wk = coreWeeks(f, t, nd), lab = w => `${toISO(w.w).slice(5)} ~ ${toISO(w.end).slice(5)}`;
  const tierNote = allOnly && S.tier !== "all" ? `（只有全量档，当前显示全量）` : "";
  // 结果页点击渗透率：总 / Toolbar / 主动搜三条线同图（同为 %），固定三档、不跟随档位按钮；其余指标一次一条
  const isNs = S.wk === "ns";
  const ser = isNs ? NS3.map(([tt, nm], i) => ({ name: `${name} · ${nm} · 周`, color: `var(--s${i + 1})`, pts: coreWeeks(f, tt, nd).map(w => w.v) }))
    : [{ name: `${name} · ${TIER_NAME[t]} · 周${tierNote}`, color: "var(--s1)", pts: wk.map(w => w.v) }];
  lineChart(el, wk.map(w => w.w), ser, fm, true,
    i => `<b>${lab(wk[i])}</b>${wk[i].n < 7 ? `<br><span class="muted">${wk[i].n} 天${wk[i].pooled ? "合计口径" : "均值"}${wk[i].span < 7 ? "（本周进行中）" : ""}${wk[i].bad ? `，剔除不完整 ${wk[i].bad} 天` : ""}${wk[i].miss ? `，缺 ${wk[i].miss} 天` : ""}</span>` : ""}`,
    { key: "fCoreWeek", title: `${scopeText()} · ${name} · ${isNs ? "总 / Toolbar / 主动搜" : TIER_NAME[t]} · 周走势（${toISO(CW.start)} 起）` });
  const miss = CW.missing.length;
  const noDau = S.wk === "pen" && !hasDau() ? ` <b class="warn-t">${S.market === "ALL" ? "全机房" : "整机房"}没有 DAU 口径，搜索渗透率要选国家后才有。</b>` : "";
  ft.innerHTML = `周 = 周一 ~ 周日；比率指标是周内分子、分母分别合计后再相除（与上方核心指标卡一致），加载 UV 取日均；不完整日不进。` +
    `口径同上方核心指标卡（设备-天；人均 VV 按当天 P99 封顶）。` +
    (isNs ? `结果页点击渗透率固定画总 / Toolbar（from = toolbar）/ 主动搜（from ≠ toolbar）三条线，不跟随档位按钮；其余指标跟随上方档位（${TIER_NAME[S.tier]}）。` : `跟随上方档位（${TIER_NAME[S.tier]}）。`) +
    `跟随市场 / 国家，不跟随日期选择器。` + noDau +
    (miss ? ` <b class="warn-t">缺 ${miss} 天（${toISO(CW.missing[0])} 起）：</b><button type="button" class="btn" id="cwFill">补历史</button>（七个指标一次补齐，按 14 天分段）` : "");
  const b = $("#cwFill");
  if (b) b.onclick = async () => {
    b.disabled = true;
    try {
      const j = await api("/api/core_history", { method: "POST", body: { market: scopeKey() } });
      if (j.job_id) { b.textContent = `已提交 #${j.job_id}，拉完自动刷新…`; await waitJob(j.job_id); }
      loadCoreWeekly();
    } catch (e) { b.disabled = false; b.textContent = "补数失败：" + e.message; }
  };
  // 复制：七个指标按周一次给全
  COPY.coreWeek = () => {
    const all = WK.map(([, , g, , ao, nd]) => coreWeeks(g, ao ? "all" : S.tier, nd));
    const [, , g0, fm0, , nd0] = WK[0], nsTb = coreWeeks(g0, "tb", nd0), nsSite = coreWeeks(g0, "site", nd0);
    return { caption: `${scopeText()} · ${TIER_NAME[S.tier]} · 核心指标周（${toISO(CW.start)} 起；比率指标为周合计口径，无结果率、搜索渗透率为全量档；渗透率另附 Toolbar / 主动搜两档）`,
      header: ["周", ...WK.map(x => x[1]), "结果页点击渗透率 · Toolbar", "结果页点击渗透率 · 主动搜", "有效天数", "备注"],
      rows: all[0].map((w, i) => [lab(w), ...WK.map((x, j) => x[3](all[j][i].v)), fm0(nsTb[i].v), fm0(nsSite[i].v), w.n,
        [w.span < 7 ? "本周进行中" : "", w.bad ? `剔除不完整 ${w.bad} 天` : "", w.miss ? `缺 ${w.miss} 天` : ""].filter(Boolean).join("；")])
        .filter(r => r.slice(1, 1 + WK.length).some(c => c !== "—")) };
  };
}

// ─────────── 包含式 UV 漏斗 ───────────
// 单位 = 设备-天：走到第 k 步要求当天走过前面所有步，所以每步转化 ≤ 100%；三档列来自同一次扫描
const CORE = new Set(["mid_expose", "mid_click", "result_load", "card_click", "play_ready", "play_close"]);
function flowAvg(days, bad, key) {
  const fl = FUNNEL.flow || {}, have = days.filter(d => fl[d]), good = have.filter(d => !bad.includes(d));
  const use = good.length ? good : have;
  const xs = use.map(d => fl[d][key]).filter(v => v != null);
  return { v: xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null, n: use.length };
}
function renderFlow(days, ctx, bad) {
  const steps = META.flow_steps, tiers = ["all", "site", "tb"];
  const A = Object.fromEntries(tiers.map(t => [t, steps.map(([k]) => flowAvg(days, bad, `${t}_${k}`).v)]));
  const n = flowAvg(days, bad, "all_entry").n;
  if (!n) {
    const msg = `<p class="muted">这个窗口还没有转化漏斗数据（新口径 2026-10-20 上线），点右上角「从数仓刷新」重拉。</p>`;
    $("#fFlow").innerHTML = msg; $("#fSteps").innerHTML = msg; $("#fFlowDaily").innerHTML = msg;
    COPY.flow = COPY.steps = COPY.flowDaily = () => ({ header: [], rows: [] });
    return;
  }
  const tbSkip = (t, k) => t === "tb" && k === "mid_click";
  const iMid = steps.findIndex(([k]) => k === "mid_expose");
  // 总表
  // 较上一周：两段窗口每一天都要有转化漏斗数据且完整，否则整列给「—」并在表下说明
  const P = FUNNEL.prev, pdays = [];
  for (let d = P.d0; d <= P.d1; d = addDays(d, 1)) pdays.push(d);
  const full = (ds, fl, st) => ds.every(d => fl[d] && (!st[d] || st[d].complete));
  const curFull = full(days, FUNNEL.flow, FUNNEL.status), wow = curFull && full(pdays, P.flow, P.status);
  const pavg = key => pdays.reduce((a, d) => a + (P.flow[d][key] || 0), 0) / pdays.length;
  const PA = wow ? Object.fromEntries(tiers.map(t => [t, steps.map(([k]) => pavg(`${t}_${k}`))])) : null;
  const wowCell = (t, i) => {
    if (!wow || tbSkip(t, steps[i][0])) return "—";
    // 转化率环比（较上一步，pp）；入口点击行给 UV 环比（%）
    if (!i) return sgnPct(ratio(A[t][0], PA[t][0]) - 1);
    const now = ratio(A[t][i], A[t][i - 1]), was = ratio(PA[t][i], PA[t][i - 1]);
    return now == null || was == null ? "—" : sgnPp(now - was);
  };
  const header = ["段", "环节"];
  for (const t of tiers) header.push(`${TIER_NAME[t]} UV`, "较上一步", "较上一周", "较中间页曝光");
  // 较中间页曝光：中间页曝光之前的环节不算（—），中间页曝光本身 = 100%
  const vsMid = (v, t, i) => i < iMid ? "—" : i === iMid ? "100%" : pct(ratio(v, A[t][iMid]), 1);
  const rows = steps.map(([k, label, seg], i) => {
    const r = [i && steps[i - 1][2] === seg ? "" : seg, { v: label, cls: CORE.has(k) ? "core" : "" }];
    for (const t of tiers) {
      const v = A[t][i], p = i ? A[t][i - 1] : null;
      r.push(fmt(v), tbSkip(t, k) ? "带词直达" : i ? pct(ratio(v, p), 1) : "—", wowCell(t, i), vsMid(v, t, i));
    }
    return r;
  });
  const fl = FUNNEL.flow, sum = key => days.filter(d => fl[d]).reduce((a, d) => a + (fl[d][key] || 0), 0);
  const dp = ratio(sum("site_direct_play"), sum("site_mid_click")), cnr = ratio(sum("all_close_no_ready"), sum("all_card_click"));
  // 按段分组：段名跨行合并，段与段之间分隔线；核心链路环节只加粗 + 左侧标记，不铺整行底色
  const span = steps.map(([, , seg], i) => i && steps[i - 1][2] === seg ? 0 : steps.slice(i).findIndex(([, , g]) => g !== seg) >>> 0 || steps.length - i);
  const body = rows.map((r, i) => `<tr class="${span[i] ? "seg-start" : ""}">` +
    (span[i] ? `<td class="segc" rowspan="${span[i]}">${esc(cellV(r[0]))}</td>` : "") +
    r.slice(1).map((c, j) => { const k = [j === 0 ? "lab" : "", j === 0 && CORE.has(steps[i][0]) ? "core" : "", j % 4 === 1 ? "ts" : "",
        j % 4 === 3 ? "wow " + wowCls(cellV(c)) : ""].filter(Boolean).join(" ").trim();
      return `<td${k ? ` class="${k}"` : ""}>${esc(cellV(c))}</td>`; }).join("") + "</tr>").join("");
  requestAnimationFrame(() => { const t = $("#fFlow .ftab"), c = t && t.querySelector("th"); if (c) t.style.setProperty("--segw", c.getBoundingClientRect().width + "px"); });
  $("#fFlow").innerHTML = `<div class="scroll"><table class="ftab"><thead><tr>${header.map(x => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>` +
    `<p class="foot"><span class="core-key">粗体带竖条</span> = 核心链路（中间页曝光 → 播放结束）。「较上一周」= 本窗口「较上一步」转化率 − 上一段同长窗口（${toISO(P.d0).slice(5)}~${toISO(P.d1).slice(5)}）的，单位 pp；入口点击行为 UV 环比；|Δ| ≥ 1pp 着色，只作描述、不做显著性。${wow ? "" : ` <b class="warn-t">本列暂不可算：</b>${curFull ? `上一段同长窗口（${toISO(P.d0)} ~ ${toISO(P.d1)}）有缺失或不完整的日期。<button type="button" class="btn" id="fillPrev">补同周期AA变化</button>（只拉上一段，当前窗口不动）` : "本窗口有缺失或不完整的日期，先点右上角「从数仓刷新」补齐本窗口"}。`}单位 = 设备-天，${n} 天日均。到达第 k 步 = 当天也走过前面所有步，所以每一步转化都 ≤ 100%，可直接当转化率读；全量 = 站内、Toolbar 任一路径到达（不是两档相加）。
    Toolbar 带词直达结果页，「中间页点击」视为通过。播放侧不按 source 分档：Toolbar 拉起的会话里做的站内搜索，播放也带 source=toolbar。<br>
    旁路（不进漏斗）：站内中间页点击后当天没进结果页、直接播放（联想词直达）占站内中间页点击 ${pct(dp, 1)}；走到卡片点击但只有播放结束、没有准备播放（冷启动补发）占 ${pct(cnr, 2)}，已剔除。</p>`;
  const fp = $("#fillPrev");
  if (fp) fp.onclick = () => fillPrev(fp, P.d0, P.d1);
  COPY.flow = () => ({ caption: ctx.replace(` · ${TIER_NAME[S.tier]} · `, " · ") + "（UV 转化漏斗，设备-天，日均）", header, rows });
  // 条形图：三档并排，共用一个刻度（以全量入口点击为满格），三档长度可直接比
  const max = Math.max(...A.all.filter(v => v != null), 1);
  const stepRows = steps.map(([k, label]) => [label]);
  $("#fSteps").innerHTML = `<div class="steps3">` + tiers.map(t => {
    const vs = A[t];
    return `<div class="steps"><h3>${TIER_NAME[t]}</h3>` + steps.map(([k, label, seg], i) => {
      const v = vs[i], gap = i && steps[i - 1][2] !== seg ? " gap" : "";
      const mid = i > iMid ? pct(ratio(v, vs[iMid]), 1) : null;
      const ret = i ? (tbSkip(t, k) ? "带词直达" : pct(ratio(v, vs[i - 1]), 1)) : "";
      const tipTxt = `${TIER_NAME[t]} · ${label}：${fmt(v)}` + (i && !tbSkip(t, k) ? `；较上一步 ${pct(ratio(v, vs[i - 1]), 1)}` : "") + (mid ? `；较中间页曝光 ${mid}` : "");
      stepRows[i].push(fmt(v), tbSkip(t, k) ? "带词直达" : i ? pct(ratio(v, vs[i - 1]), 2) : "", i < iMid ? "—" : i === iMid ? "100%" : pct(ratio(v, vs[iMid]), 2));
      return `<div class="row${gap}${CORE.has(k) ? " core" : ""}" data-tip="${esc(tipTxt)}"><div class="lbl">${label}</div><div class="track"><div class="fill" style="width:${v ? (v / max * 100) : 0}%"></div></div><div class="val">${fmt(v)}</div><div class="rate">${ret}</div></div>`;
    }).join("") + `</div>`;
  }).join("") + `</div><p class="foot">三档共用一个刻度（全量入口点击 = 满格），条长可跨档比较；右列 = 较上一步，深色条 = 核心链路。鼠标悬停看较中间页曝光。</p>`;
  COPY.steps = () => ({ caption: ctx.replace(` · ${TIER_NAME[S.tier]} · `, " · ") + "（UV 转化漏斗，设备-天，日均）",
    header: ["环节", ...tiers.flatMap(t => [`${TIER_NAME[t]} UV`, "较上一步", "较中间页曝光"])], rows: stepRows });
  // 逐日转化
  const dh = ["日期", "入口点击 UV", ...steps.slice(1).map(([k, l]) => l), "核心链路"];
  const drows = days.map(d => {
    const r = fl[d];
    if (!r) return [toISO(d), { v: "未拉取", cls: "muted" }, ...dh.slice(2).map(() => "")];
    const g = k => r[`${S.tier}_${k}`];
    return [toISO(d) + (bad.includes(d) ? " ⚠" : ""), fmt(g("entry")),
      ...steps.slice(1).map(([k], i) => tbSkip(S.tier, k) ? "—" : pct(ratio(g(k), g(steps[i][0])), 1)),
      pct(ratio(g("play_close"), g("mid_expose")), 1)];
  });
  $("#fFlowDaily").innerHTML = tableHTML({ header: dh, rows: drows }, "") + `<p class="foot">各环节列 = 该步 ÷ 上一步（设备-天包含式）；核心链路 = 播放结束 ÷ 中间页曝光。</p>`;
  COPY.flowDaily = () => ({ caption: ctx + "（逐日转化，设备-天包含式）", header: dh, rows: drows.filter(r => cellV(r[1]) !== "未拉取") });
}

// 逐日趋势：除北极星外与上面转化漏斗同一口径（设备-天包含式）；r = 当天 flow 行，t = 档位，raw = 当天独立去重行
const fv = (r, t, k) => r ? r[`${t}_${k}`] : null;
const TREND = {
  rates: { label: "核心链路转化", series: [
    ["中间页点击 ÷ 中间页曝光", (r, t) => t === "tb" ? null : ratio(fv(r, t, "mid_click"), fv(r, t, "mid_expose"))],
    // 北极星不走包含式：与实验同一口径（卡片点击 UV 排兜底 ÷ 结果页加载 UV，各自去重），raw = 当天独立去重行
    ["结果页点击渗透率", (r, t, raw) => ratio(stepVal(raw, "card_click", t).uv, stepVal(raw, "result_load", t).uv)],
    ["播放结束 ÷ 卡片点击", (r, t) => ratio(fv(r, t, "play_close"), fv(r, t, "card_click"))],
    ["核心链路（播放结束 ÷ 中间页曝光）", (r, t) => ratio(fv(r, t, "play_close"), fv(r, t, "mid_expose"))],
  ], fmt: v => pct(v, 1) },
  uv: { label: "UV", series: [
    ["中间页曝光", (r, t) => fv(r, t, "mid_expose")],
    ["结果页加载", (r, t) => fv(r, t, "result_load")],
    ["卡片点击", (r, t) => fv(r, t, "card_click")],
    ["播放结束", (r, t) => fv(r, t, "play_close")],
  ], fmt: fmtShort },
};
function ratio(a, b) { return a != null && b ? a / b : null; }
const sgnPp = v => v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(2) + "pp";
const sgnPct = v => v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
const wowCls = v => { const m = /^([+-]?\d+\.\d+)pp$/.exec(v || ""); return !m ? "" : +m[1] >= 1 ? "wup" : +m[1] <= -1 ? "wdn" : ""; };
function fmtShort(v) { if (v == null) return "—"; const a = Math.abs(v);
  return a >= 1e6 ? (v / 1e6).toFixed(1) + "M" : a >= 1e3 ? (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k" : String(Math.round(v)); }

function renderTrend(days, bd) {
  $("#trendSeg").innerHTML = Object.entries(TREND).map(([k, v]) => `<button data-k="${k}" class="${S.trend === k ? "on" : ""}">${v.label}</button>`).join("");
  $$("#trendSeg button").forEach(b => b.onclick = () => { S.trend = b.dataset.k; renderTrend(days, bd); });
  const cfg = TREND[S.trend] || TREND[S.trend = "rates"], fl = FUNNEL.flow || {};
  const series = cfg.series.map(([name, f], i) => ({ name, color: `var(--s${i + 1})`, pts: days.map(d => fl[d] ? f(fl[d], S.tier, bd[d]) : null) }))
    .filter(se => se.pts.some(v => v != null));
  lineChart($("#fTrend"), days, series, cfg.fmt, false, null, { key: "fTrend:" + S.trend,
    title: `${scopeText()} · ${TIER_NAME[S.tier]} · 逐日趋势（${cfg.label}，${toISO(days[0])} ~ ${toISO(days[days.length - 1])}）` });
}

// 点击渗透率 × 结果页加载 UV：回答「分母在降，渗透率提升是不是分母变小带来的」
// 上图 = 三档渗透率 + 结构固定（Toolbar / 主动搜加载 UV 占比固定在窗口前 7 天，各档用当天自己的渗透率重新加权）；下图 = 两档加载 UV。同一时间轴
function renderNsMix(days, bd, ctx) {
  const ld = (d, t) => bd[d] ? stepVal(bd[d], "result_load", t).uv : null;
  const base = days.filter(d => bd[d]).slice(0, 7), bt = base.reduce((a, d) => a + (ld(d, "tb") || 0), 0), bs = base.reduce((a, d) => a + (ld(d, "site") || 0), 0);
  const w = bt + bs ? bt / (bt + bs) : null;
  const p = (d, t) => bd[d] ? nsDay(bd[d], t) : null;
  const mix = d => { const a = p(d, "tb"), b = p(d, "site"); return w == null || a == null || b == null ? null : w * a + (1 - w) * b; };
  const ser = [...NS3.map(([t, nm], i) => ({ name: `渗透率 · ${nm}`, color: `var(--s${i + 1})`, pts: days.map(d => p(d, t)) })),
    { name: "渗透率 · 总（结构固定）", color: "var(--s5)", pts: days.map(mix) }];
  lineChart($("#fNsMix"), days, ser, v => pct(v, 1), true, null, { key: "fNsMix", title: `${scopeText()} · 结果页点击渗透率三档（${toISO(days[0])} ~ ${toISO(days[days.length - 1])}）` });
  lineChart($("#fNsMixLoad"), days, [["tb", "Toolbar"], ["site", "主动搜"]].map(([t, nm], i) => ({ name: `结果页加载 UV · ${nm}`, color: `var(--s${i + 2})`, pts: days.map(d => ld(d, t)) })),
    fmtShort, false, null, { key: "fNsMixLoad", h: 170, title: `${scopeText()} · 结果页加载 UV（Toolbar / 主动搜）` });
  const ap = days.filter(d => bd[d] && stepVal(bd[d], "card_click", "tb").approx);
  $("#fNsMixFoot").innerHTML = `总渗透率的提升一部分来自 Toolbar 占比下降（结构变化：Toolbar 渗透率远低于主动搜，它的加载 UV 减少会把总渗透率抬高），主动搜自身渗透率在上升。` +
    `结构固定 = w × Toolbar 当天渗透率 + (1 − w) × 主动搜当天渗透率，w = 窗口前 ${base.length} 天（${base.length ? toISO(base[0]) + " ~ " + toISO(base[base.length - 1]) : "—"}）Toolbar 加载 UV ÷（Toolbar + 主动搜加载 UV）= ${pct(w, 1)}；它与「总」的差就是结构变化的贡献，它自身的变化才是各入口自己提升了多少。` +
    `口径同北极星：卡片点击 UV（排兜底）÷ 结果页加载 UV，按天去重；Toolbar = from = toolbar，主动搜 = from ≠ toolbar。两档设备当天约 1% 重叠，两档加载 UV 之和略大于总。` +
    (ap.length ? ` <b class="warn-t">${ap.length} 天（${toISO(ap[0])} 起）Toolbar 排兜底 UV 尚未补数，按 总 − 主动搜 相减近似（略偏低）。</b>` : "");
  COPY.nsMix = () => ({ caption: ctx + " · 结果页点击渗透率三档 + 加载 UV（逐日）", header: ["日期", ...ser.map(x => x.name), "加载 UV · Toolbar", "加载 UV · 主动搜", "备注"],
    rows: days.filter(d => bd[d]).map(d => [toISO(d), ...ser.map(x => pct(x.pts[days.indexOf(d)])), fmt(ld(d, "tb")), fmt(ld(d, "site")), ap.includes(d) ? "Toolbar 为近似" : ""]) });
}

// ─────────── 图表公共：图例开关 + 复制图片 ───────────
// HIDE[图表键] = 被图例关掉的线名；重绘（换档 / 轮询刷新）时保留
const HIDE = {};
const chartTop = legend => `<div class="chart-top"><div class="legend">${legend}</div>` +
  `<button type="button" class="btn copy" data-img title="复制成 PNG，可直接粘到飞书文档">复制图片</button></div>`;
const staticLegend = items => items.map(([c, t, k]) => `<span class="lg static"><i class="${k || ""}" style="background:${c}"></i>${esc(t)}</span>`).join("");

// tipHead(i)：悬停浮层标题（默认 = 日期）
// series[i] = { name, color, pts, band?: [下界[], 上界[]] }；opt = { key（图例开关状态的键，默认 = 容器 id）, zero（画 0 参考线）, title（复制图片的标题）, h }
function lineChart(el, days, series, yfmt, fit, tipHead, opt = {}) {
  el._lc = [days, series, yfmt, fit, tipHead, opt];
  if (opt.title) el.dataset.title = opt.title;
  if (!series.some(se => se.pts.some(v => v != null))) { el.innerHTML = `<p class="muted">无数据</p>`; return; }
  const hid = HIDE[opt.key || el.id] ||= new Set(), vis = series.filter(se => !hid.has(se.name));
  const W = Math.max(el.clientWidth || 560, 320), H = opt.h || 240, L = 58, R = 22, T = 12, B = 26;
  const all = vis.flatMap(se => [...se.pts, ...(se.band ? se.band.flat() : [])]).filter(v => v != null && isFinite(v));
  if (opt.zero && all.length) all.push(0);
  const legend = series.map((se, i) => { const off = hid.has(se.name);
    return `<button type="button" class="lg${off ? " off" : ""}" data-i="${i}" aria-pressed="${!off}" title="点击${off ? "显示" : "隐藏"}这条线"><i style="background:${se.color}"></i>${esc(se.name)}</button>`; }).join("");
  let s = `<svg width="${W}" height="${H}" role="img" aria-label="${esc(opt.title || "趋势图")}">`;
  if (!all.length) s += `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">线都关掉了，点上方图例重新显示</text></svg>`;
  else {
    // fit：y 轴贴合数据范围（实验 By天 组间差常 <1pp，从 0 起画会压成一条线）
    const lo = Math.min(...all), hi = Math.max(...all), pad = (hi - lo) * 0.2 || Math.abs(hi) * 0.05 || 0.01;
    // zero：差值图以 0 为中轴对称，0 线落在刻度上
    const zm = opt.zero ? Math.max(Math.abs(lo), Math.abs(hi)) * 1.1 || 0.01 : 0;
    let ymin = opt.zero ? -zm : fit ? (lo >= 0 ? Math.max(0, lo - pad) : lo - pad) : Math.min(0, lo), ymax = opt.zero ? zm : fit ? hi + pad : niceMax(hi);
    const x = i => L + (days.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (days.length - 1));
    const y = v => T + (H - T - B) * (1 - (v - ymin) / (ymax - ymin));
    let ticks = [0, .25, .5, .75, 1].map(t => ymin + t * (ymax - ymin));
    // fit 图刻度取整（12.00% 而不是 12.78%）：步长取 1 / 2 / 2.5 / 5 × 10^n，上下界对齐到步长
    if (fit && !opt.zero) {
      const r = (ymax - ymin) / 4, p = 10 ** Math.floor(Math.log10(r)), n = r / p;
      const st = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
      ymin = Math.floor(ymin / st + 1e-9) * st; ymax = Math.ceil(ymax / st - 1e-9) * st;
      if (lo >= 0) ymin = Math.max(0, ymin);
      ticks = [];
      for (let v = ymin; v <= ymax + st / 2; v += st) ticks.push(+v.toPrecision(12));
    }
    const every = Math.ceil(days.length / Math.max(1, Math.floor((W - L - R) / 56)));
    s += ticks.map(t => `<line class="gl" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${yfmt(t)}</text>`).join("");
    s += `<line class="ax" x1="${L}" x2="${W - R}" y1="${y(ymin)}" y2="${y(ymin)}"/>`;
    if (opt.zero && ymin < 0 && ymax > 0) s += `<line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>`;
    const xlab = opt.xlab || (d => `${String(d).slice(4, 6)}-${String(d).slice(6)}`);
    s += days.map((d, i) => i % every ? "" : `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${xlab(d)}</text>`).join("");
    // 区间带先画，线压在上面
    for (const se of vis.filter(se => se.band)) {
      const [blo, bhi] = se.band;
      let d = "", run = [];
      const flush = () => { if (run.length > 1) d += "M" + run.map(i => x(i).toFixed(1) + "," + y(bhi[i]).toFixed(1)).join("L") + "L" +
        [...run].reverse().map(i => x(i).toFixed(1) + "," + y(blo[i]).toFixed(1)).join("L") + "Z"; run = []; };
      blo.forEach((v, i) => v == null || bhi[i] == null ? flush() : run.push(i)); flush();
      s += `<path class="band" d="${d}" style="fill:${se.color}"/>`;
      if (days.length <= 14) s += blo.map((v, i) => v == null || bhi[i] == null ? "" : `<line class="whisk" x1="${x(i)}" x2="${x(i)}" y1="${y(bhi[i])}" y2="${y(v)}" style="stroke:${se.color}"/>`).join("");
    }
    // split：第 split 个点起是另一段（推全观测 基线周 | 观测周），线断开、中间画虚线分隔
    if (opt.split) s += `<line class="split" x1="${(x(opt.split - 1) + x(opt.split)) / 2}" x2="${(x(opt.split - 1) + x(opt.split)) / 2}" y1="${T}" y2="${H - B}"/>`;
    for (const se of vis) {
      let path = "", pen = false;
      se.pts.forEach((v, i) => { if (v == null || i === opt.split) pen = false; if (v == null) return; path += (pen ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1); pen = true; });
      s += `<path class="line" d="${path}" style="stroke:${se.color}"/>`;
      if (days.length <= 14) s += se.pts.map((v, i) => v == null ? "" : `<circle cx="${x(i)}" cy="${y(v)}" r="3.5" fill="${se.color}" stroke="var(--card)" stroke-width="2"/>`).join("");
    }
    s += `<line class="cross" y1="${T}" y2="${H - B}" visibility="hidden"/><rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" class="hit"/></svg>`;
    el.innerHTML = chartTop(legend) + s + (opt.note ? `<p class="foot chart-note">${esc(opt.note)}</p>` : "");
    const svg = el.querySelector("svg"), xh = svg.querySelector(".cross"), hit = svg.querySelector(".hit");
    const band = (se, i) => se.band && se.band[0][i] != null ? ` <span class="muted">[${yfmt(se.band[0][i])}, ${yfmt(se.band[1][i])}]</span>` : "";
    hit.addEventListener("mousemove", ev => {
      const px = ev.clientX - svg.getBoundingClientRect().left;
      const i = Math.max(0, Math.min(days.length - 1, Math.round((px - L) / ((W - L - R) / Math.max(1, days.length - 1)))));
      xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
      showTip((tipHead ? tipHead(i) : `<b>${toISO(days[i])}</b>`) + "<br>" + vis.map(se => `<span class="sw" style="background:${se.color}"></span>${esc(se.name)}：${yfmt(se.pts[i])}${band(se, i)}`).join("<br>"), ev);
    });
    hit.addEventListener("mouseleave", () => { xh.setAttribute("visibility", "hidden"); hideTip(); });
  }
  if (!all.length) el.innerHTML = chartTop(legend) + s;
  $$(".legend .lg[data-i]", el).forEach(b => b.onclick = () => {
    const n = series[+b.dataset.i].name;
    hid.has(n) ? hid.delete(n) : hid.add(n);
    lineChart(el, ...el._lc);
  });
}

// 复制图片：把图表 SVG（含标题、显示中的图例项）栅格化成 2 倍 PNG 写进剪贴板，飞书文档里直接粘贴
const IMG_PROPS = ["fill", "fill-opacity", "stroke", "stroke-width", "stroke-dasharray", "stroke-opacity", "stroke-linecap", "opacity",
  "font-size", "font-weight", "font-family", "visibility"];
function chartPng(box) {
  const svg = box && box.querySelector("svg");
  if (!svg) return null;
  const card = box.closest(".card, section");
  const title = box.dataset.title || (card && card.querySelector("h2") ? card.querySelector("h2").textContent.trim() : "图表");
  const W = +svg.getAttribute("width"), H = +svg.getAttribute("height");
  // 计算样式（CSS 变量、类样式）逐个写成内联，脱离页面后颜色不丢
  const clone = svg.cloneNode(true), src = [svg, ...svg.querySelectorAll("*")], dst = [clone, ...clone.querySelectorAll("*")];
  src.forEach((n, i) => { const cs = getComputedStyle(n); IMG_PROPS.forEach(p => dst[i].style.setProperty(p, cs.getPropertyValue(p))); });
  clone.querySelectorAll(".hit, .cross").forEach(n => n.remove());
  const bs = getComputedStyle(document.body), bgc = getComputedStyle(box.closest(".card") || document.body).backgroundColor;
  const bg = !bgc || bgc === "rgba(0, 0, 0, 0)" ? "#ffffff" : bgc, ink = bs.color;
  const tw = t => [...t].reduce((a, c) => a + (c.charCodeAt(0) > 255 ? 12 : 6.8), 0);
  const items = $$(".legend .lg:not(.off)", box).map(b => [getComputedStyle(b.querySelector("i")).backgroundColor, b.textContent.trim()]);
  let lx = 0, ly = 46, head = "";
  for (const [c, t] of items) {
    const w = 22 + tw(t) + 18;
    if (lx && lx + w > W) { lx = 0; ly += 18; }
    head += `<rect x="${lx}" y="${ly - 8}" width="14" height="4" rx="2" fill="${c}"/><text x="${lx + 20}" y="${ly - 3}" font-size="12" fill="${ink}">${esc(t)}</text>`;
    lx += w;
  }
  const top = items.length ? ly + 12 : 36, OW = W + 32, OH = H + top + 16;
  const out = `<svg xmlns="http://www.w3.org/2000/svg" width="${OW}" height="${OH}" font-family="${esc(bs.fontFamily)}">` +
    `<rect width="100%" height="100%" fill="${bg}"/><g transform="translate(16,0)"><text x="0" y="24" font-size="14" font-weight="600" fill="${ink}">${esc(title)}</text>${head}</g>` +
    `<g transform="translate(16,${top})">${new XMLSerializer().serializeToString(clone)}</g></svg>`;
  return new Promise((ok, fail) => {
    const img = new Image();
    img.onload = () => {
      const cv = document.createElement("canvas"), k = 2;
      cv.width = OW * k; cv.height = OH * k;
      const g = cv.getContext("2d"); g.scale(k, k); g.drawImage(img, 0, 0);
      cv.toBlob(blob => blob ? ok({ blob, url: cv.toDataURL("image/png"), title }) : fail(new Error("导出失败")), "image/png");
    };
    img.onerror = () => fail(new Error("导出失败"));
    img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(out);
  });
}
function copyChartImage(btn) {
  const job = chartPng(btn.closest(".chart"));
  if (!job) { flash(btn, "暂无图", false); return; }
  // ClipboardItem 直接收 Promise：写剪贴板要在点击当下发起，等图片生成完再写会丢掉用户手势
  const write = navigator.clipboard && window.ClipboardItem
    ? navigator.clipboard.write([new ClipboardItem({ "image/png": job.then(x => x.blob) })]) : Promise.reject(new Error("无剪贴板权限"));
  write.then(() => flash(btn, "已复制图片", true),
    () => job.then(x => { showImage(x.url, x.title); flash(btn, "右键复制图片", true); }, () => flash(btn, "复制失败", false)));
}
// 退路（局域网 http 访问没有剪贴板权限）：弹出图片，右键复制
function showImage(src, title) {
  const d = document.createElement("div");
  d.className = "imgdlg";
  d.innerHTML = `<div class="imgdlg-box"><div class="card-h"><b>${esc(title)}</b><button type="button" class="btn">关闭</button></div>` +
    `<p class="muted">浏览器不让直接写剪贴板（局域网 http 访问时常见）：在图片上右键 →「复制图片」，再粘到飞书。</p><img src="${src}" alt="${esc(title)}"></div>`;
  d.onclick = ev => { if (ev.target === d || ev.target.closest("button")) d.remove(); };
  document.body.appendChild(d);
}
document.addEventListener("click", ev => { const b = ev.target.closest("[data-img]"); if (b) copyChartImage(b); });
function niceMax(v) { if (v <= 0) return 1; const p = 10 ** Math.floor(Math.log10(v)); const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p; }

function renderPositions(ctx) {
  const el = $("#fPos");
  const agg = {};
  for (const p of FUNNEL.positions) {
    if (S.tier === "site" && p.tb) continue;
    if (S.tier === "tb" && !p.tb) continue;
    const a = (agg[p.position] ||= { e: 0, c: 0 }); a.e += p.expose; a.c += p.click;
  }
  const ps = Object.keys(agg).map(Number).sort((a, b) => a - b);
  const ctr = ps.map(p => agg[p].e ? agg[p].c / agg[p].e : 0);
  COPY.pos = () => ({ caption: ctx + "（窗口累计，PV，排兜底）", header: ["位置", "曝光 PV", "点击 PV", "CTR"],
    rows: ps.map((p, i) => [p, fmt(agg[p].e), fmt(agg[p].c), pct(ctr[i])]) });
  if (!ps.length) { el.innerHTML = `<p class="muted">无数据</p>`; return; }
  const W = Math.max(el.clientWidth || 700, 340), H = 200, L = 46, R = 10, T = 18, B = 24;
  const ymax = niceMax(Math.max(...ctr)), bw = (W - L - R) / ps.length;
  const y = v => T + (H - T - B) * (1 - v / ymax);
  let s = `<svg width="${W}" height="${H}" role="img" aria-label="位置 CTR">`;
  s += [0, .5, 1].map(t => `<line class="gl" x1="${L}" x2="${W - R}" y1="${y(t * ymax)}" y2="${y(t * ymax)}"/><text x="${L - 6}" y="${y(t * ymax) + 4}" text-anchor="end">${pct(t * ymax, 0)}</text>`).join("");
  ps.forEach((p, i) => {
    const x0 = L + i * bw + 1, w = Math.max(bw - 2, 2), y0 = y(ctr[i]), h = y(0) - y0;
    s += `<path d="M${x0},${y(0)} V${y0 + 4} q0,-4 4,-4 H${x0 + w - 4} q4,0 4,4 V${y(0)} Z" fill="var(--s1)" data-i="${i}"/>`;
    s += `<text x="${x0 + w / 2}" y="${H - 8}" text-anchor="middle">${p}</text>`;
    if (i < 3) s += `<text class="lab" x="${x0 + w / 2}" y="${y0 - 4}" text-anchor="middle">${pct(ctr[i], 1)}</text>`;
    s += `<rect x="${L + i * bw}" y="${T}" width="${bw}" height="${H - T - B}" fill="transparent" data-i="${i}" class="hit"/>`;
  });
  s += `<line class="ax" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/></svg>`;
  el.innerHTML = s;
  $$(".hit", el).forEach(r => {
    r.addEventListener("mousemove", ev => { const i = +r.dataset.i, p = ps[i];
      showTip(`<b>位置 ${p}</b><br>CTR ${pct(ctr[i])}<br>曝光 ${fmt(agg[p].e)} · 点击 ${fmt(agg[p].c)}`, ev); });
    r.addEventListener("mouseleave", hideTip);
  });
}

// 实验的 Top20 位置 CTR（PV，排兜底）：多组按位置画线，tier = all / site / tb
const POS_TIER = { all: null, site: 0, tb: 1 };
function posAgg(pos, gid, tier) {
  const t = POS_TIER[tier] ?? null, agg = {};
  for (const r of (pos && pos[gid]) || []) {
    if (t != null && r.tb !== t) continue;
    const a = (agg[r.position] ||= { e: 0, c: 0 }); a.e += r.expose; a.c += r.click;
  }
  return agg;
}
function posChart(el, e, pos, tier, title) {
  const ps = [...new Set(Object.values(pos || {}).flat().map(r => r.position))].sort((a, b) => a - b);
  if (!ps.length) { el.innerHTML = `<p class="muted">暂无位置数据</p>`; return null; }
  const ids = (e.groups || []).map(g => g.id).filter(id => pos[id]);
  const series = ids.map((id, i) => {
    const agg = posAgg(pos, id, tier), g = (e.groups || []).find(x => x.id === id) || { id };
    return { name: gLab(g), color: `var(--s${Math.min(i + 1, 5)})`,
             pts: ps.map(p => { const a = agg[p]; return a && a.e ? a.c / a.e : null; }) };
  });
  lineChart(el, ps, series, v => v == null ? "—" : pct(v), true, null,
    { xlab: p => p, key: `pos:${e.id}:${tier}`, title: title || `${e.name} · Top20 位置 CTR（PV，排兜底）` });
  return { ps, pairs: ids.map(id => [id, (e.groups || []).find(x => x.id === id) || { id }, posAgg(pos, id, tier)]) };
}
function posTable(r, tier) {
  return { caption: `Top20 位置 CTR（PV，排兜底，${TIER_NAME[tier] || tier}）`, header: ["位置", ...r.pairs.map(([, g]) => gLab(g))],
    rows: r.ps.map(p => [p, ...r.pairs.map(([, , agg]) => { const a = agg[p]; return a && a.e ? pct(a.c / a.e) : "—"; })]) };
}

function renderTable(days, bd, bad, ctx) {
  const steps = META.steps.filter(([, , allOnly]) => !(allOnly && S.tier !== "all"));
  const header = ["日期", ...steps.map(([, l]) => l + " UV"), "起播 VV", "3s VV", "结果页点击渗透率", "无结果率"];
  let h = `<table><thead><tr>${header.map(x => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>`;
  const rows = [];
  const prevLoad = [];
  for (const d of days) {
    const r = bd[d];
    if (!r) {
      h += `<tr><td>${toISO(d)}</td><td colspan="${steps.length + 4}" class="muted" style="text-align:left">未拉取</td></tr>`;
      rows.push([toISO(d), ...header.slice(1).map(() => ""), "未拉取"]);
      continue;
    }
    const load = stepVal(r, "result_load", S.tier).uv;
    const p = prevLoad.length ? prevLoad[prevLoad.length - 1] : null;
    const jump = p && load && (load / p > 2 || p / load > 2);
    prevLoad.push(load);
    const cells = [...steps.map(([ek]) => fmt(stepVal(r, ek, ek === "no_result" ? "all" : S.tier).uv)),
      fmt(stepVal(r, "play_vv", S.tier).pv), fmt(stepVal(r, "play_3s", S.tier).pv),
      pct(ratio(stepVal(r, "card_click", S.tier).uv, load)), pct(ratio(stepVal(r, "no_result", "all").uv, stepVal(r, "result_load", "all").uv))];
    h += `<tr class="${bad.includes(d) ? "bad" : ""}"><td>${toISO(d)}${bad.includes(d) ? " ⚠" : ""}${jump ? ' <span class="flag" title="与前一日相差超过 2 倍">台阶</span>' : ""}</td>`;
    h += cells.map(c => `<td>${c}</td>`).join("") + "</tr>";
    rows.push([toISO(d), ...cells, [bad.includes(d) ? "数据可能不完整" : "", jump ? "台阶（与前一日相差 >2 倍）" : ""].filter(Boolean).join("；")]);
  }
  $("#fTable").innerHTML = h + "</tbody></table>";
  COPY.daily = () => ({ caption: ctx, header: [...header, "备注"], rows: rows.some(r => r[r.length - 1] !== "未拉取") ? rows : [] });
}

async function refreshFunnel() {
  const b = $("#fRefresh"); b.disabled = true;
  try {
    const j = await api("/api/refresh", { method: "POST", body: { markets: [scopeKey()], start: toNum($("#fStart").value), end: toNum($("#fEnd").value) } });
    $("#fNotice").innerHTML = `<div class="notice info"><b>已提交 #${j.job_id}</b>正在从数仓拉数，完成后自动刷新…</div>`;
    await waitJob(j.job_id);
    loadFunnel();
  } catch (e) { $("#fNotice").innerHTML = `<div class="notice crit"><b>刷新失败</b>${esc(e.message)}</div>`; }
  finally { b.disabled = false; }
}

// 补「较上一周」的上一段同长窗口：只拉那几天（单次刷新上限 31 天，超了分段），日期选择器不动
async function fillPrev(btn, d0, d1) {
  btn.disabled = true;
  try {
    const ids = [];
    for (let s = d0; s <= d1; s = addDays(s, 31))
      ids.push((await api("/api/refresh", { method: "POST", body: { markets: [scopeKey()], start: s, end: Math.min(addDays(s, 30), d1) } })).job_id);
    btn.textContent = `已提交 #${ids.join(" / #")}，拉完自动刷新…`;
    for (const id of ids) await waitJob(id);
    loadFunnel();
  } catch (e) { btn.disabled = false; btn.textContent = "补数失败：" + e.message; }
}

async function waitJob(id, onTick) {
  for (;;) {
    await new Promise(r => setTimeout(r, 4000));
    const j = (await api("/api/jobs")).find(x => x.id === id);
    if (onTick) onTick(j);
    if (!j || j.status === "done") return j;
    if (j.status === "failed" || j.status === "cancelled") throw new Error(j.error || "任务失败，见任务日志");
  }
}

// ─────────── 实验 ───────────
let METRICS = [];
let CUR_EXP = null;
async function loadExpList() {
  METRICS = await api("/api/metrics");
  const list = await api("/api/experiments");
  $("#expList").innerHTML = list.length ? list.map(e => `<div class="item ${e.id === CUR_EXP ? "on" : ""}" data-id="${e.id}">
      <div class="t">${esc(e.name)}</div>
      <div class="m">${esc(mktText(e))} · ${toISO(e.start)} ~ ${toISO(e.end)} · ${expPill(e)}</div>
      ${e.stage ? `<div class="stg"><span class="stage s-${({ 查生效: 1, 判止损: 2, 中期读数: 3, 出结论: 4 })[e.stage.stage] || 0}">${esc(e.stage.stage)}</span>${[e.stage.day ? `第 ${e.stage.day} 天` : "", e.stage.next ? `下一节点 ${esc(e.stage.next)}` : "",
        e.last_end ? `数据到 ${toISO(e.last_end).slice(5)}` : e.stage.stage === "未开窗" ? "" : "还没跑数"].filter(Boolean).join(" · ")}</div>` : ""}
    </div>`).join("") : `<p class="muted">还没有实验。</p>`;
  $$("#expList .item").forEach(it => it.onclick = () => openExp(+it.dataset.id));
  if (!CUR_EXP && list.length) CUR_EXP = list[0].id;
  if (CUR_EXP) openExp(CUR_EXP);
}
// 国家筛选：国名用浏览器自带的 Intl 翻译，不手写对照表
const REG_NAME = (() => { try { const d = new Intl.DisplayNames(["zh-CN"], { type: "region" }); return c => { try { return d.of(c) || c; } catch (e) { return c; } }; } catch (e) { return c => c; } })();
const CLUSTER = { SG: "SGP", RU: "RU", EU: "EU" };
const CLUSTER_ALL = { SG: "全部（cluster=1 整个机房）", RU: "全部（cluster=3，只算 region=RU）", EU: "全部（cluster=4 整个机房）" };
const mktText = e => (CLUSTER[e.market] || e.market) + (e.regions && e.regions.length && e.regions.join("/") !== e.market ? ` · ${e.regions.join("/")}` : "") +
  ((e.excl_src || []).length ? ` · 排除 source ${e.excl_src.join("/")}` : "");
function renderRegions() {
  const mkt = $("#expMkt").value, codes = (META.regions || {})[mkt] || [];
  $("#regQ").value = "";
  $("#regList").innerHTML = codes.map(c => `<label data-k="${c} ${esc(REG_NAME(c))}"><input type="checkbox" name="rg" value="${c}"><span class="code">${c}</span>${esc(REG_NAME(c))}</label>`).join("");
  syncRegions();
}
function syncRegions() {
  const sel = $$("[name=rg]:checked").map(x => x.value);
  const mkt = $("#expMkt").value, n = ((META.regions || {})[mkt] || []).length;
  $("#regSum").textContent = sel.length ? sel.map(c => `${c} ${REG_NAME(c)}`).join("、") : CLUSTER_ALL[mkt] || "全部";
  $("#regHint").textContent = sel.length ? `已选 ${sel.length} 个；分组、P99 封顶、全部指标都只算这些国家`
    : `该集群近 7 天出现过的 ${n} 个国家，可单选或多选`;
}

// active = 窗口没到结束日、每天 09:40 自动补昨天，平时并没有在跑；正在跑的是 queued / running
const statusText = s => ({ queued: "排队中", running: "跑数中", active: "回收中", frozen: "已冻结", failed: "失败" }[s] || s);
// 已建、开窗日还没到（同步实验配置建的新一轮）：不跑数，每日调度在有数后自动开跑
const expPill = e => e.status === "active" && !e.last_end && e.start > META.yesterday
  ? `<span class="pill queued">等待开窗（${toISO(e.start).slice(5)} 起有数）</span>`
  : `<span class="pill ${e.status}"${e.status === "active" ? ` title="每天 ${META.daily_at} 自动补到前一天"` : ""}>${statusText(e.status)}</span>`;

let EDIT_EXP = null, EDIT_NOTE = null;
const metricName = k => (METRICS.find(m => m.key === k) || {}).name || k;
function openNewExp(ev, e) {
  const f = $("#newExp");
  EDIT_EXP = e || null;
  if (!e && !f.hidden && ev) { f.hidden = true; return; }   // 「新建回收」再点一次 = 收起
  f.hidden = false;
  $("#expFormTitle").textContent = e ? `编辑「${e.name}」` : "新建回收";
  $("#expSubmit").textContent = e ? "保存并补算" : "提交并跑数";
  $("#expEditHint").hidden = !e; $("#expMsg").textContent = "";
  $("#expForm").reset();
  $("#grpParseRes").innerHTML = $("#grpParseMsg").innerHTML = "";
  $("#expMkt").innerHTML = META.market_order.map(k => `<option value="${k}">${CLUSTER[k] || k}</option>`).join("");
  renderRegions();
  $("#grpRows").innerHTML = ""; addGrpRow("control"); addGrpRow("treatment");
  $("#expMetrics").innerHTML = METRICS.map(m => { const on = m.preset && m.kind !== "quantile";
    return `<div class="mk-row"><label><input type="checkbox" name="mk" value="${m.key}" ${on ? "checked" : ""}> ${esc(m.name)}${m.preset ? "" : ' <span class="pill">自定义</span>'}</label>
      <label class="daily" title="除全窗口读数外，再按天出一份趋势"><input type="checkbox" name="dk" value="${m.key}" ${on ? "" : "disabled"}> By天</label></div>`; }).join("");
  $$("[name=mk]").forEach(x => x.onchange = () => { const d = x.closest(".mk-row").querySelector("[name=dk]"); d.disabled = !x.checked; if (!x.checked) d.checked = false; });
  $("#dkAll").onclick = () => { const ds = $$("[name=dk]:not(:disabled)"); const all = ds.every(d => d.checked); ds.forEach(d => d.checked = !all); };
  $("#expForm [name=end]").value = toISO(META.yesterday);
  if (e) fillExpForm(e);
  f.scrollIntoView({ behavior: "smooth", block: "start" });
}
function fillExpForm(e) {
  const f = $("#expForm");
  f.name.value = e.name; f.market.value = e.market; renderRegions();
  (e.regions || []).forEach(c => { const x = $(`[name=rg][value="${c}"]`); if (x) x.checked = true; }); syncRegions();
  f.start.value = toISO(e.start); f.end.value = toISO(e.end); f.launch.value = e.launch ? toISO(e.launch) : "";
  f.excl_src.value = (e.excl_src || []).includes("5") ? "5" : "";
  $("#grpRows").innerHTML = "";
  e.groups.forEach(g => { addGrpRow(g.role); const r = $$("#grpRows .grp-row").pop(); r.querySelector("[name=gid]").value = g.id; r.querySelector("[name=glabel]").value = g.label && g.label !== g.id ? g.label : ""; });
  $$("[name=mk]").forEach(x => { x.checked = e.metric_keys.includes(x.value); x.onchange(); });
  $$("[name=dk]").forEach(x => { x.checked = !x.disabled && (e.daily_keys || []).includes(x.value); });
}
function addGrpRow(role) {
  const d = document.createElement("div"); d.className = "grp-row";
  d.innerHTML = `<input name="gid" placeholder="exp_id，如 100001" inputmode="numeric" size="22" required>
    <input name="glabel" placeholder="备注（如 λ=0.5）" size="16">
    <div class="seg small"><button type="button" data-r="control">对照</button><button type="button" data-r="treatment">实验</button></div>
    <button type="button" class="btn danger" title="删除这一组">×</button>`;
  const setRole = r => { d.dataset.role = r; $$(".seg button", d).forEach(b => b.classList.toggle("on", b.dataset.r === r)); };
  $$(".seg button", d).forEach(b => b.onclick = () => setRole(b.dataset.r));
  if (role) setRole(role);
  d.querySelector(".danger").onclick = () => d.remove();
  $("#grpRows").appendChild(d);
}

// ─────────── 一键识别分组表（飞书 / Excel 复制粘贴） ───────────
// 优先读剪贴板 HTML（飞书表格合并单元格 rowspan 能展开），没有就按 TSV；按表头认列，没表头就按内容猜
const MKT_ALIAS = [[/^(sgp?|新加坡|singapore)$/i, "SG"], [/^(ru|俄罗斯|russia)$/i, "RU"], [/^(eu|欧洲|europe)$/i, "EU"]];
const toMkt = s => { const t = String(s || "").trim(); for (const [re, k] of MKT_ALIAS) if (re.test(t)) return k; return null; };
const ROLE_CTRL = /对照|基线|holdback|回退|control|baseline|ctrl/i;
function htmlGrid(html) {
  const tb = new DOMParser().parseFromString(html, "text/html").querySelector("table");
  if (!tb) return null;
  const grid = [];
  [...tb.rows].forEach((tr, i) => {
    grid[i] ||= []; let j = 0;
    for (const td of tr.cells) {
      while (grid[i][j] != null) j++;
      const v = td.innerText ?? td.textContent, rs = td.rowSpan || 1, cs = td.colSpan || 1;
      for (let a = 0; a < rs; a++) for (let b = 0; b < cs; b++) (grid[i + a] ||= [])[j + b] = v.trim();
      j += cs;
    }
  });
  return grid;
}
const tsvGrid = t => t.split(/\r?\n/).filter(l => l.trim()).map(l => l.split("\t").map(c => c.trim()));
function parseGroupTable(grid) {
  if (!grid || !grid.length) return [];
  const H = grid[0].map(h => String(h || ""));
  const find = re => H.findIndex(h => re.test(h));
  let cId = find(/exp_?id|实验\s*id|组号|分组\s*id/i), cMkt = find(/集群|机房|市场|cluster/i),
    cName = find(/^分组$|组名|分组名|group|arm/i), cReg = find(/国家|地区|region|country/i);
  const hasHead = cId >= 0 || cName >= 0;
  const body = hasHead ? grid.slice(1) : grid;
  if (cId < 0) {   // 没表头：哪一列 5~12 位纯数字最多，就当组号列
    const w = Math.max(...body.map(r => r.length));
    let best = 0;
    for (let j = 0; j < w; j++) { const n = body.filter(r => /^\d{5,12}$/.test(r[j] || "")).length; if (n > best) { best = n; cId = j; } }
  }
  if (cId < 0) return [];
  if (cName < 0) cName = H.findIndex((h, j) => j !== cId && j !== cMkt && /组/.test(h));
  const out = [];
  let lastMkt = null;
  for (const r of body) {
    const id = (String(r[cId] || "").match(/\d{5,12}/) || [])[0];
    if (!id) continue;
    const mkt = (cMkt >= 0 ? toMkt(r[cMkt]) : null) || r.map(toMkt).find(Boolean) || lastMkt;
    lastMkt = mkt;
    const name = cName >= 0 ? String(r[cName] || "") : "";
    const role = ROLE_CTRL.test(name || r.join(" ")) ? "control" : "treatment";
    const label = name.replace(/^实验组\s*[-－—_:：]?\s*/, "").replace(/^对照组$/, "对照").trim();
    const reg = cReg >= 0 ? String(r[cReg] || "").toUpperCase().match(/\b[A-Z]{2}\b/g) || [] : [];
    out.push({ id, mkt, role, label, reg });
  }
  return out;
}
function renderParsed(rows) {
  const box = $("#grpParseRes");
  if (!rows.length) { box.innerHTML = `<span class="warn-t">没认出组号：需要一列 5~12 位数字的 exp_id。</span>`; return; }
  const by = {};
  for (const r of rows) (by[r.mkt || "?"] ||= []).push(r);
  const keys = Object.keys(by);
  const apply = k => {
    const gs = by[k], f = $("#expForm");
    if (k !== "?" && f.market.value !== k) { f.market.value = k; renderRegions(); }
    const regs = [...new Set(gs.flatMap(g => g.reg))].filter(c => $(`[name=rg][value="${c}"]`));
    if (regs.length) { $$("[name=rg]").forEach(x => x.checked = regs.includes(x.value)); syncRegions(); }
    $("#grpRows").innerHTML = "";
    gs.forEach(g => { addGrpRow(g.role); const r = $$("#grpRows .grp-row").pop(); r.querySelector("[name=gid]").value = g.id; r.querySelector("[name=glabel]").value = g.label; });
    const nc = gs.filter(g => g.role === "control").length;
    $$("#grpParseRes button").forEach(b => b.classList.toggle("on", b.dataset.k === k));
    $("#grpParseMsg").innerHTML = `已填入 ${gs.length} 组${k === "?" ? "" : `（${CLUSTER[k] || k}${regs.length ? " · " + regs.join("/") : ""}）`}：` +
      gs.map(g => `${g.id}${g.label ? " " + esc(g.label) : ""} → ${g.role === "control" ? "<b>对照</b>" : "实验"}`).join("；") +
      (nc === 1 ? "。角色按组名关键词猜的，提交前请核对。" : ` <b class="warn-t">认出 ${nc} 个对照组，需要恰好 1 个，请手动改。</b>`);
  };
  box.innerHTML = keys.length > 1
    ? `表里有 ${keys.length} 个集群，一个回收只读一个集群，选一个：<span class="seg small">${keys.map(k => `<button type="button" data-k="${k}">${k === "?" ? "未识别集群" : CLUSTER[k] || k} · ${by[k].length} 组</button>`).join("")}</span>`
    : "";
  $$("#grpParseRes button").forEach(b => b.onclick = () => apply(b.dataset.k));
  const cur = $("#expForm").market.value;
  apply(by[cur] ? cur : keys[0]);
}
function onGroupPaste(ev) {
  const cd = ev.clipboardData, html = cd && cd.getData("text/html"), txt = cd ? cd.getData("text/plain") : "";
  const grid = (html && htmlGrid(html)) || tsvGrid(txt || ev.target.value);
  if (cd) { ev.preventDefault(); ev.target.value = txt; }
  renderParsed(parseGroupTable(grid));
}

async function submitExp(ev) {
  ev.preventDefault();
  const f = ev.target, msg = $("#expMsg");
  const groups = $$(".grp-row", f).map(r => ({ id: r.querySelector("[name=gid]").value.trim(), label: r.querySelector("[name=glabel]").value.trim(), role: r.dataset.role }));
  if (groups.some(g => !g.role)) { msg.textContent = "每个组都要选「对照 / 实验」"; return; }
  const body = { name: f.name.value, market: f.market.value, start: toNum(f.start.value), end: f.end.value ? toNum(f.end.value) : null,
    launch: f.launch.value ? toNum(f.launch.value) : null,
    groups, metric_keys: $$("[name=mk]:checked", f).map(x => x.value), daily_keys: $$("[name=dk]:checked", f).map(x => x.value), regions: $$("[name=rg]:checked", f).map(x => x.value),
    excl_src: f.excl_src.value ? [f.excl_src.value] : [] };
  try {
    if (EDIT_EXP) {
      const j = await api(`/api/experiments/${EDIT_EXP.id}`, { method: "PUT", body });
      EDIT_NOTE = { id: EDIT_EXP.id, text: { full: "改了集群 / 国家 / 分组 / 日期：已按新口径整体重跑。", none: "已保存（没有新增指标或 By天，不用跑数）。",
        incremental: `已保存，只补算新增部分：${[...(j.add || []).map(k => metricName(k)), ...(j.add_daily || []).map(k => metricName(k) + " By天")].join("、")}。已出结果保留。` }[j.mode] };
      CUR_EXP = EDIT_EXP.id;
    } else {
      const j = await api("/api/experiments", { method: "POST", body });
      CUR_EXP = j.id;
    }
    $("#newExp").hidden = true; EDIT_EXP = null; loadExpList();
  } catch (e) { msg.textContent = e.message; }
}

// BASE[实验 id] = 临时当基准的组（只换比较对象；默认 = 对照组）
const BASE = {};
const expUrl = id => `/api/experiments/${id}` + (BASE[id] ? `?base=${encodeURIComponent(BASE[id])}` : "");
async function openExp(id, quiet) {
  if (EDIT_NOTE && EDIT_NOTE.id !== id) EDIT_NOTE = null;
  CUR_EXP = id;
  $$("#expList .item").forEach(it => it.classList.toggle("on", +it.dataset.id === id));
  let e;
  try { e = await api(expUrl(id)); }
  catch (err) {                       // 实验在别处被删了：清掉选中，别让详情停在旧实验上
    if (CUR_EXP === id) { CUR_EXP = null; $("#expDetail").innerHTML = `<p class="muted">${esc(err.message)}：这个实验可能已被删除。左侧另选一个。</p>`; }
    return;
  }
  renderExp(e);
  if ((["queued", "running"].includes(e.status) || (e.pending || []).length) && !quiet) pollExp(id);
}
const POLLING = new Set();
const expSig = e => JSON.stringify([e.status, e.last_end, e.pending, e.error, (e.view || {}).base]);
async function pollExp(id) {
  if (POLLING.has(id)) return;        // 同一个实验只留一条轮询（点列表 / 重跑 / 切回页签都会进来）
  POLLING.add(id);
  try {
    let sig = null;
    for (;;) {
      await new Promise(r => setTimeout(r, 5000));
      if (CUR_EXP !== id) return;
      const e = await api(expUrl(id));
      const n = expSig(e);
      if (n !== sig && sig !== null) renderExp(e);   // 有进展才重画，避免每 5 秒收起展开的趋势图
      sig = n;
      if (!["queued", "running"].includes(e.status) && !(e.pending || []).length) { renderExp(e); loadExpList(); return; }
    }
  } catch (_) { /* 网络抖动：下次打开再轮询 */ }
  finally { POLLING.delete(id); }
}

let SUM_TEXT = "";
const PEND_LABEL = { "@cum": "前 N 天累计", "@dist": "分布 / 进组趋势", "@cuped": "CUPED", "@seg": "分群下钻", "@lt": "长期价值" };
function renderExp(e) {
  const v = e.view, ctrl = e.groups.find(g => g.role === "control"), trts = e.groups.filter(g => g.role !== "control");
  const name = id => { const g = e.groups.find(x => x.id === id); return g ? `${g.label && g.label !== g.id ? esc(g.label) + " · " : ""}${g.id}` : id; };
  let h = `<div class="bar"><div><h1>${esc(e.name)}</h1><div class="muted">${esc(mktText(e))} · ${toISO(e.start)} ~ ${toISO(e.end)}${e.last_end ? ` · 已跑到 ${toISO(e.last_end)}` : ""} · ${expPill(e)}</div></div>
    <div class="controls"><button class="btn" id="editExp">编辑</button><button class="btn" id="rerun">重跑</button><button class="btn danger" id="delExp">删除</button></div></div>`;
  // 分段实验（中途改参照、数据不合并）：旧段 warn、主段 info；链到另一段（段配置在 observe.py SG3_SEGS）
  const sg = e.segment;
  if (sg) h += `<div class="notice ${sg.old ? "warn" : "info"}"><b>${esc(sg.head)}</b><span>${esc(sg.text)}${sg.link
    ? `<a href="#exp/${sg.link.id}" data-seg="${sg.link.id}">「${esc(sg.link.name)}」</a>` : `「${esc(sg.name)}」（看板里还没建）`}。</span></div>`;
  if (EDIT_NOTE && EDIT_NOTE.id === e.id) h += `<div class="notice info"><b>已编辑</b>${esc(EDIT_NOTE.text)}</div>`;
  if (e.status === "failed") h += `<div class="notice crit"><b>失败</b>${esc(e.error)}</div>`;
  else if (e.error) h += `<div class="notice warn"><b>部分失败</b>${esc(e.error.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").slice(0, 300))}<br>${act("点「重跑」只补失败的部分，已出结果不动")}。</div>`;
  if (["queued", "running"].includes(e.status)) h += `<div class="notice info"><b>跑数中</b>先算开窗前 P99，再一次扫描算全部比率指标，比率指标出来就先展示；分位数（起播耗时）排在后面补算。进度见「任务日志」。</div>`;
  else if ((e.pending || []).length) h += `<div class="notice info"><b>补算中</b>比率指标已出，可以先看；${e.pending.map(k => esc(PEND_LABEL[k] || ((m => m ? m.name + (k.endsWith("@day") ? "（By天）" : "") : k)(METRICS.find(m => m.key === k.replace("@day", "")))))).join("、")} 在后台补算，完成后自动刷新（SG 按国家约 7 分钟）。</div>`;
  if (e.vis_error && e.status !== "failed" && !(e.pending || []).length) h += `<div class="notice info"><b>辅助读数未算出</b>${esc(e.vis_error.slice(0, 300))}。不影响读数表和结论提要，${act("点「重跑」只补这部分")}。</div>`;
  const sm = v.summary;
  if (sm) {
    const gname = id => { const g = e.groups.find(x => x.id === id); return g ? (g.label && g.label !== g.id ? `${g.label} ${g.id}` : g.id) : id; };
    const head = `${e.name}（${mktText(e)}，${toISO(e.start).slice(5)}~${toISO(e.last_end || e.end).slice(5)}，${e.last_end ? Math.round((new Date(toISO(e.last_end)) - new Date(toISO(e.start))) / 864e5) + 1 + " 天" : "未跑数"}）`;
    const blocks = sm.state === "ok" ? sm.groups.map(g => ({ title: sm.groups.length > 1 ? `实验 ${gname(g.grp)} vs 对照` : "", lines: g.lines })) : [{ title: "", lines: [sm.text] }];
    const sh = sm.head, stageLine = sh ? `第 ${sh.day} 天 · ${sh.stage}${sh.note ? `（${sh.note}）` : ""}${sh.next ? ` · 下一节点：${sh.next}` : ""}` : "";
    h += `<div class="card summary"><div class="card-h"><h2>结论提要</h2><button type="button" class="btn copy" id="copySum">复制结论</button></div>
      <p class="sum-head">${esc(head)}</p>${stageLine ? `<p class="sum-stage">${(st => ["查生效", "判止损", "中期读数", "出结论"].map((x, i, a) => `<span class="${x === st ? "on" : i < a.indexOf(st) ? "done" : ""}">${x}</span>`).join(""))(sh.stage)}<small>${esc(stageLine)}</small></p>` : ""}${blocks.map(b => `${b.title ? `<p class="sum-sub">${esc(b.title)}</p>` : ""}<ol>${b.lines.map(l => `<li>${esc(l)}</li>`).join("")}</ol>`).join("")}
      <p class="foot">节点：第 2 天查生效（分流 / 进组）→ 第 3 天判止损（前 3 天累计，北极星或护栏 95% CI 整体落在变差一侧才提示止损）→ 第 7 天中期读数 → 第 14 天出结论（读当前累计行）。显著 = p &lt; 0.05；显著但 |Δ| 小于可测下限（MDE）的不引用幅度；逐日同向天数不作判据。看板不给采纳建议。</p></div>`;
    SUM_TEXT = [head, stageLine, ...blocks.flatMap(b => [b.title, ...b.lines.map((l, i) => `${i + 1}. ${l}`)])].filter(Boolean).join("\n");
  }
  if (v.srm) {
    const s = v.srm, bad = s.p != null && s.p < 0.001;
    h += `<div class="notice ${bad ? "crit" : "info"}"><b>${bad ? "⚠ SRM 不通过" : "分流检查"}</b>各组设备数 ${Object.entries(s.counts).map(([g, n]) => name(g) + "：" + fmt(n)).join("，")}；
      跨组冲突设备 ${fmt(s.conflict)} 台（已剔除）；卡方 p = ${s.p == null ? "—" : s.p.toExponential(2)}${bad ? "。分流不均，以下读数不可信。" : "（等比假设）"}</div>`;
  }
  const lz = e.launch || e.start, pre = d => toISO(addDays(lz, d)).slice(5);
  const capE = Object.entries(e.caps || {}).filter(([k]) => !k.startsWith("@lt_"));
  if (capE.length) h += `<p class="foot">P99 封顶（上线日 ${toISO(lz).slice(5)} 之前 7 天 ${pre(-7)}~${pre(-1)}，写死）：${capE.map(([k, c]) => esc((METRICS.find(m => m.key === k.replace("@day", "")) || {}).name || k) + (k.endsWith("@day") ? "（By天，单设备单天）" : "") + " = " + fmt(c)).join("；")}</p>`;
  // ── 读数可视化：指标卡 → 置信区间总览 → 分布箱型图 → 进组趋势（口径同下方读数表，只换呈现）──
  const ctrl0 = e.groups.find(g => g.role === "control"), base = e.groups.find(g => g.id === v.base) || ctrl0;
  const others = e.groups.filter(g => g.id !== base.id), trt = others.find(g => g.id === TRT[e.id]) || others[0];
  const ratioT = sortTables((v.tables || []).filter(t => t.kind === "ratio" && t.rows.some(r => r.type === "total")));
  if (ratioT.length) {
    const gbtn = (id, gs, cur, tag) => `<div class="seg small" id="${id}">${gs.map(g => `<button type="button" data-g="${esc(g.id)}" class="${g.id === cur ? "on" : ""}">${esc(gLab(g))}${tag && g.role === "control" ? "（对照）" : ""}</button>`).join("")}</div>`;
    h += `<section class="xviz"><div class="card-h"><h2>读数可视化（全窗口累计）</h2>${e.groups.length > 2 ? `<div class="controls"><span class="muted">基准组</span>${gbtn("xBase", e.groups, base.id, true)}</div>` : ""}</div>`;
    if (base.id !== ctrl0.id) h += `<div class="notice warn"><b>基准已换成 ${esc(gLab(base))}</b>指标卡、区间图、逐日趋势、读数表都按「其他组 vs 该组」比；结论提要和分流检查仍按对照组 ${esc(gLab(ctrl0))}。<button type="button" class="btn" id="xBaseReset">换回对照组</button></div>`;
    h += `<div class="card-h sub"><h3>指标卡</h3>${others.length > 1 ? gbtn("xTrt", others, trt.id) : `<span class="muted">${esc(gLab(trt))} vs ${esc(gLab(base))}</span>`}</div>`;
    h += `<div class="xcards">${expCards(ratioT, base, trt)}</div>`;
    h += `<div class="card-h sub"><h3>置信区间总览</h3></div><div class="chart" id="xForest"></div>
      <p class="foot">横轴 = 相对变化（实验 ÷ 基准 − 1），所有指标同一刻度；线 = 95% 置信区间，点 = 估计值；灰带 = 可测下限（MDE，α 0.05 / power 0.8）。区间整段离开 0 线 = 显著，再看是否出了灰带：在带内的显著不引用幅度。越低越好的指标（如无结果率）往右是变差、标红。</p>`;
    const dk = v.dist_keys || [], dpend = (e.pending || []).includes("@dist");
    if (v.dist) {
      for (const k of dk) h += `<div class="card-h sub"><h3>分布：${esc(metricName(k))}</h3><button type="button" class="btn copy" data-copy="dist_${k}">复制表格</button></div><div class="chart" id="xBox_${k}"></div><p class="foot" id="xBoxNote_${k}"></p>`;
      h += `<div class="card-h sub"><h3>进组趋势</h3><div class="seg small" id="xEntrySeg">${[["new", "每天新进组"], ["cum", "累计进组"]].map(([k, l]) => `<button type="button" data-k="${k}" class="${(S.entry || "cum") === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
        <div class="chart" id="xEntry"></div><p class="foot">设备按窗口内第一次带组号的日期计入（认组口径同读数：当天结果页事件带 exp_id；跨组设备剔除）。累计到最后一天 = 上方分流检查的各组设备数。两组曲线应基本重合；某天某组明显偏离 = 那天分流或上报出了问题。</p>`;
    } else h += `<p class="muted">${dpend ? "分布箱型图 / 进组趋势补算中…" : "分布箱型图 / 进组趋势还没算：" + act("点「重跑」只补这部分（不重跑已出结果）") + "。"}</p>`;
    h += `</section>`;
  }
  if (v.pos) {
    const tiers = [["all", "全量"], ["site", "站内"], ["tb", "Toolbar"]];
    h += `<div class="card-h sub"><h3>Top20 位置 CTR（PV，排兜底）</h3><div class="controls"><div class="seg small" id="xPosTier">${tiers.map(([k, l]) => `<button type="button" data-k="${k}" class="${(S.posTier || "all") === k ? "on" : ""}">${l}</button>`).join("")}</div><button type="button" class="btn copy" data-copy="posExp">复制表格</button></div></div>
      <div class="chart" id="xPos"></div>
      <p class="foot">按位置统计卡片曝光 / 点击 PV（排除兜底推荐），按事件自带的组号分组，跨组事件不出；和漏斗页「Top20 位置 CTR」同口径。位置 1~20。</p>`;
  } else if (v.tables && v.tables.length) {
    h += `<p class="muted">Top20 位置 CTR 还没算：${act("点「重跑」只补这部分")}（加指标 / 新实验会在日更补算时带上）。</p>`;
  }
  const TB = expTables(e, v.tables || [], base.id);
  h += cupedHTML(e, v, base) + segHTML(e, v, base);
  if (TB.overview.rows.length) {
    h += `<div class="card-h"><h2>总览（全窗口累计）</h2><div class="controls"><button type="button" class="btn copy" data-copy="ov">复制表格</button><button type="button" class="btn copy" data-copy="allTables">复制全部表格</button></div></div>`;
    h += tableHTML(TB.overview, "otab");
  }
  TB.items.forEach((x, i) => {
    h += `<section class="mtable"><div class="card-h"><h2>${esc(x.title)}</h2><button type="button" class="btn copy" data-copy="t${i}">复制表格</button></div>`;
    h += `<p class="foot">${esc(x.note)}${x.waits.length ? ` <span class="pill running">补算中：${esc(x.waits.join("、"))}</span>` : ""}</p>`;
    h += x.rows.length ? tableHTML(x, "dtab2") : `<p class="muted">${x.waits.length ? "补算中…" : e.error ? "补算失败（原因见顶部），" + act("点「重跑」只补这部分") : "暂无数据"}</p>`;
    if (x.chart) h += `<details class="trend" data-i="${i}"><summary>趋势图</summary>${x.src.kind === "ratio" ? `<div class="seg small tmode">${[["diff", "差值 + 95% CI"], ["value", "各组取值"]].map(([k, l]) => `<button type="button" data-k="${k}" class="${(S.tmode || "diff") === k ? "on" : ""}">${l}</button>`).join("")}</div>` : ""}<div class="chart" id="tch${i}"></div></details>`;
    h += `</section>`;
    COPY["t" + i] = () => ({ caption: `${e.name} · ${mktText(e)} · ${x.title}`, header: x.header, rows: x.rows });
  });
  h += ltHTML(e, v, base);
  COPY.ov = () => ({ caption: `${e.name} · ${mktText(e)} · 总览（${toISO(e.start)} ~ ${toISO(e.last_end || e.end)}）`, header: TB.overview.header, rows: TB.overview.rows });
  COPY.allTables = () => ({ sections: [{ caption: `${e.name} · ${mktText(e)} · 总览`, header: TB.overview.header, rows: TB.overview.rows },
    ...TB.items.filter(x => x.rows.length).map(x => ({ caption: x.title, header: x.header, rows: x.rows }))] });
  if (TB.items.length) h += `<p class="foot">判读：p &lt; 0.05（|z| ≥ 1.96）为显著，按指标方向标正向 / 负向；标准误按设备聚类（delta method）。
    累计行的显著另做两层修正，结果写在判读后的括号里，原判读不变：① 多次看数（第 3 / 7 / 14 天各看一次）按成组序贯检验给每个节点单独的 |z| 阈值，收益侧早期严（O'Brien-Fleming 型，第 3 天约 4.2、第 7 天约 2.8、第 14 天约 2.0），变差侧各节点接近（Pocock 型，约 2.2~2.4），整体误报率控制在 5%；② 同一行上北极星以外的指标 × 实验组一起做 Benjamini-Hochberg（FDR 5%），北极星是事先指定的主指标不进修正。判止损提醒只认修正后仍显著的。Δ：百分比指标为百分点差（pp），数值指标为相对差（%）。
    累计行 = 设备在该时段内取并集（点击 → 3s 类：时段内点过卡 × 时段内有过 3s 播放，不要求同一天），人均 VV 用上线前 7 天单设备单天 P99 封顶；逐日行单位 = 设备-天。MDE = α 0.05 / power 0.8 下本窗口可测出的最小差异。看板只给数据，采纳与否由人判断。</p>`;
  $("#expDetail").innerHTML = h;
  const draw = d => drawTrend(e, TB.items[+d.dataset.i], "tch" + d.dataset.i, base.id);
  $$("#expDetail details.trend").forEach(d => d.addEventListener("toggle", () => { if (d.open) draw(d); }));
  $$("#expDetail .tmode button").forEach(b => b.onclick = () => {
    S.tmode = b.dataset.k;
    $$("#expDetail .tmode button").forEach(x => x.classList.toggle("on", x.dataset.k === S.tmode));
    $$("#expDetail details.trend[open]").forEach(draw);
  });
  if (ratioT.length) {
    forest($("#xForest"), e, ratioT, base);
    if (v.dist) {
      for (const k of v.dist_keys || []) boxPlot($("#xBox_" + k), $("#xBoxNote_" + k), e, k, v.dist, base);
      entryChart($("#xEntry"), e, v.dist);
      $$("#xEntrySeg button").forEach(b => b.onclick = () => { S.entry = b.dataset.k; $$("#xEntrySeg button").forEach(x => x.classList.toggle("on", x === b)); entryChart($("#xEntry"), e, v.dist); });
    }
    const setBase = id => { if (id === ctrl0.id) delete BASE[e.id]; else BASE[e.id] = id; openExp(e.id, true); };
    $$("#xBase button").forEach(b => b.onclick = () => setBase(b.dataset.g));
    if ($("#xBaseReset")) $("#xBaseReset").onclick = () => setBase(ctrl0.id);
    $$("#xTrt button").forEach(b => b.onclick = () => { TRT[e.id] = b.dataset.g; renderExp(e); });
  }
  if ($("#xPos")) {
    const drawPos = () => {
      const r = posChart($("#xPos"), e, v.pos, S.posTier || "all");
      if (r) COPY.posExp = () => posTable(r, S.posTier || "all");
    };
    drawPos();
    $$("#xPosTier button").forEach(b => b.onclick = () => {
      S.posTier = b.dataset.k;
      $$("#xPosTier button").forEach(x => x.classList.toggle("on", x === b));
      drawPos();
    });
  }
  $$("#segDim button").forEach(b => b.onclick = () => { S.segDim = b.dataset.k; renderExp(e); });
  $$("#segMet button").forEach(b => b.onclick = () => { S.segMet = b.dataset.k; renderExp(e); });
  const busy = ["queued", "running"].includes(e.status) || (e.pending || []).length;
  $("#editExp").disabled = !!busy; $("#editExp").title = busy ? "跑完再编辑" : "加指标 / 加 By天 只补算新增部分";
  $("#editExp").onclick = () => openNewExp(null, e);
  const cs = $("#copySum");
  if (cs) cs.onclick = async () => { try { await navigator.clipboard.writeText(SUM_TEXT); flash(cs, "已复制", true); } catch (_) { flash(cs, "复制失败", false); } };
  // 分段链接：从左侧列表切过实验后 hash 可能已等于目标（不触发 hashchange），直接打开另一段
  $$("#expDetail [data-seg]").forEach(a => a.onclick = ev => { ev.preventDefault(); history.replaceState(null, "", a.getAttribute("href")); openExp(+a.dataset.seg); });
  $("#rerun").onclick = async () => { await api(`/api/experiments/${e.id}/rerun`, { method: "POST" }); openExp(e.id); };
  $("#delExp").onclick = async () => { if (!confirm(`删除「${e.name}」及其结果？`)) return; await api(`/api/experiments/${e.id}`, { method: "DELETE" }); CUR_EXP = null; $("#expDetail").innerHTML = ""; loadExpList(); };
}

// ─────────── 实验读数表（对齐飞书回收文档：纵向逐日 → 前 N 天累计 → 累计；横向 对照 | 实验 | Δ | z | 判读）───────────
const sgn = (v, d) => v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : v < 0 ? "" : "+") + v.toFixed(d);
function expTables(e, tables, baseId) {
  const ctrl = e.groups.find(g => g.id === baseId) || e.groups.find(g => g.role === "control"), trts = e.groups.filter(g => g.id !== ctrl.id);
  const cname = ctrl.role === "control" ? "对照" : "基准";
  const lab = g => g.label && g.label !== g.id ? `${g.label} ${g.id}` : g.id, multi = trts.length > 1;
  const val = (t, x) => {
    if (t.kind !== "ratio") return x ? x.q.map(v => v == null ? "—" : Math.round(v)).join(" / ") : "—";
    if (!x || x.value == null) return "—";
    return `${t.unit !== "num" ? pct(x.value) : x.value.toFixed(2)}（${fmt(x.num)}/${fmt(x.den)}）`;
  };
  const delta = (t, c, xc, xt) => {
    if (t.kind !== "ratio") return xc && xt ? xt.q.map((v, i) => v == null || xc.q[i] == null ? "—" : sgn(v - xc.q[i], 0)).join(" / ") : "—";
    if (!c) return "—";
    return t.unit !== "num" ? sgn(c.diff * 100, 2) + "pp" : sgn(c.rel * 100, 1) + "%";
  };
  const cls = c => c && c.read ? (c.read === "显著正向" ? "up" : "down") : "";
  const adjT = c => c.adj && c.adj.ok === false ? "（修正后不显著）" : c.adj && c.adj.ok ? "（修正后仍显著）" : "";
  const read = (r) => { const xs = trts.filter(g => r.vs[g.id] && r.vs[g.id].read).map(g => (multi ? lab(g) + " " : "") + r.vs[g.id].read + adjT(r.vs[g.id]));
    return { v: xs.length ? xs.join("；") : "—", cls: xs.length === 1 ? cls(r.vs[trts.find(g => r.vs[g.id] && r.vs[g.id].read).id]) : "" }; };
  const items = tables.map(t => {
    const header = ["日期", `${cname} ${lab(ctrl)}`];
    for (const g of trts) header.push(`实验 ${lab(g)}`, multi ? `Δ（${g.label || g.id}）` : "Δ", "z");
    header.push("判读");
    const rows = t.rows.map(r => {
      const out = [{ v: r.label + (r.thin ? "（样本不足）" : ""), cls: r.type === "day" ? "" : "sum" }, val(t, r.groups[ctrl.id])];
      for (const g of trts) {
        const c = r.vs[g.id];
        out.push(val(t, r.groups[g.id]), { v: delta(t, c, r.groups[ctrl.id], r.groups[g.id]), cls: ("dl " + cls(c)).trim() },
          t.kind === "ratio" && c && c.z != null ? sgn(c.z, 2) : "—");
      }
      out.push(t.kind === "ratio" ? read(r) : "—");
      return out;
    });
    rows.types = t.rows.map(r => r.type);
    return { key: t.key, title: `${t.group}：${t.name}`, note: t.note, waits: t.waits, header, rows, src: t,
      chart: t.has_daily && t.rows.some(r => r.type === "day") };
  });
  // 总览：每个指标取「累计」行 + MDE
  const nStar = tables.filter(t => t.group === "北极星").length, pk = (tables.find(t => t.group === "北极星") || {}).key;
  const oh = ["指标", `${cname} ${lab(ctrl)}`];
  for (const g of trts) oh.push(`实验 ${lab(g)}`, multi ? `Δ（${g.label || g.id}）` : "Δ", "z", "MDE");
  oh.push("判读");
  const orows = [];
  for (const t of tables) {
    const r = t.rows.find(x => x.type === "total");
    if (!r) continue;
    const out = [`${t.group}：${t.name}${nStar > 1 && t.key === pk ? "（本实验主指标）" : ""}`, val(t, r.groups[ctrl.id])];
    for (const g of trts) {
      const c = r.vs[g.id];
      out.push(val(t, r.groups[g.id]), { v: delta(t, c, r.groups[ctrl.id], r.groups[g.id]), cls: ("dl " + cls(c)).trim() },
        t.kind === "ratio" && c && c.z != null ? sgn(c.z, 2) : "—",
        t.kind === "ratio" && c ? "±" + (t.unit !== "num" ? (c.mde_abs * 100).toFixed(2) + "pp" : (c.mde_rel * 100).toFixed(1) + "%") : "—");
    }
    out.push(t.kind === "ratio" ? read(r) : "—");
    orows.push(out);
  }
  return { items, overview: { header: oh, rows: orows } };
}
function tableHTML(t, klass) {
  const cell = (c, tag) => `<${tag}${c && c.cls ? ` class="${c.cls}"` : ""}>${esc(cellV(c))}</${tag}>`;
  return `<div class="scroll"><table class="${klass}"><thead><tr>${t.header.map(x => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>` +
    t.rows.map((r, i) => `<tr class="${t.rows.types ? t.rows.types[i] : ""}">${r.map(c => cell(c, "td")).join("")}</tr>`).join("") + `</tbody></table></div>`;
}
// 组的颜色跟着组走（按实验里登记的顺序），换基准 / 关线都不重新上色
const gColor = (e, id) => `var(--s${Math.min(e.groups.findIndex(g => g.id === id) + 1, 5)})`;
// 逐日趋势：差值模式 = 实验 − 基准（By天，单位 = 设备-天）+ 95% CI 带；取值模式 = 各组原值
function drawTrend(e, x, id, baseId) {
  const el = $("#" + id), t = x.src, base = e.groups.find(g => g.id === baseId) || e.groups.find(g => g.role === "control");
  const others = e.groups.filter(g => g.id !== base.id);
  const all = t.rows.filter(r => r.type === "day"), days = all.map(r => r.date);
  // 样本不足的天（后端 thin，如实验中途关停后的残留流量）不画点；表格照常列出
  const rows = all.map(r => r.thin ? { ...r, groups: {}, vs: {} } : r), note = thinNote(all.filter(r => r.thin).map(r => r.date));
  const lab = g => gLab(g) + (g.id === base.id ? "（基准）" : "");
  const title = `${e.name} · ${x.title}`;
  if (t.kind === "ratio" && (S.tmode || "diff") === "diff") {
    const pp = t.unit !== "num";
    const dv = c => c == null ? null : pp ? c.diff : c.rel, lo = c => c == null ? null : pp ? c.ci_low : c.rel_ci_low, hi = c => c == null ? null : pp ? c.ci_high : c.rel_ci_high;
    const series = others.map(g => { const cs = rows.map(r => r.vs[g.id]);
      return { name: `${gLab(g)} − ${gLab(base)}`, color: gColor(e, g.id), pts: cs.map(dv), band: [cs.map(lo), cs.map(hi)] }; });
    const yfmt = v => v == null ? "—" : pp ? sgn(v * 100, 2) + "pp" : sgn(v * 100, 1) + "%";
    lineChart(el, days, series, yfmt, true, null, { zero: true, key: id + ":diff:" + t.key, title: title + `（逐日差值，${pp ? "百分点" : "相对差"}，95% CI）`, note });
    return;
  }
  const pick = t.kind === "ratio" ? x2 => x2 ? x2.value : null : x2 => x2 && x2.q[0] != null ? x2.q[0] : null;
  const series = [base, ...others].map(g => ({ name: lab(g) + (t.kind === "ratio" ? "" : " P50"), color: gColor(e, g.id), pts: rows.map(r => pick(r.groups[g.id])) }));
  const yfmt = v => v == null ? "—" : t.kind !== "ratio" ? Math.round(v) + " ms" : t.unit !== "num" ? (v * 100).toFixed(v < 0.1 ? 2 : 1) + "%" : v.toFixed(2);
  lineChart(el, days, series, yfmt, true, null, { key: id + ":val:" + t.key, title: title + "（逐日取值）", note });
}
const thinNote = ds => ds.length ? `${ds.map(d => toISO(d).slice(5)).join("、")} 样本不足（设备数不到本组单日最高值的 30%），图上不画点；累计读数照常计入。` : "";

// ─────────── CUPED：用上线前同口径取值做协变量，只并列展示，不替代原读数 ───────────
function cupedHTML(e, v, base) {
  const cp = v.cuped, pend = (e.pending || []).includes("@cuped");
  if (!cp) return pend ? `<p class="muted">CUPED 方差缩减补算中…</p>` : "";
  if (cp.skip) return `<p class="muted">CUPED 未做：${esc(cp.skip)}。</p>`;
  const ctrl0 = e.groups.find(g => g.role === "control");
  if (base.id !== ctrl0.id) return "";
  const trts = e.groups.filter(g => g.role !== "control"), multi = trts.length > 1;
  const ts = sortTables((v.tables || []).filter(t => cp.metrics[t.key]));
  const header = ["指标"];
  for (const g of trts) header.push(...[multi ? `Δ 原（${g.label || g.id}）` : "Δ 原", "Δ CUPED", "z 原 → CUPED", "方差降幅", "判读（CUPED）"]);
  const rows = ts.map(t => {
    const out = [`${t.group}：${t.name}`];
    for (const g of trts) {
      const c = cp.metrics[t.key][g.id];
      if (!c) { out.push("—", "—", "—", "—", "—"); continue; }
      const cls = c.read ? (c.read === "显著正向" ? "up" : "down") : "";
      out.push(dFmt(t, t.unit !== "num" ? c.raw_diff : c.raw_rel), { v: dFmt(t, t.unit !== "num" ? c.diff : c.rel), cls: ("dl " + cls).trim() },
        `${sgn(c.raw_z, 2)} → ${sgn(c.z, 2)}`, (c.var_cut * 100).toFixed(0) + "%",
        { v: c.read ? c.read + (c.seq ? "" : "（未过多次看数阈值）") : "—", cls });
    }
    return out;
  });
  COPY.cuped = () => ({ caption: `${e.name} · ${mktText(e)} · CUPED 方差缩减（全窗口累计）`, header, rows });
  const asof = cp.stale ? `（截至 ${toISO(cp.d1).slice(5)}，第 ${cp.days} 天）` : "（全窗口累计）";
  return `<section class="mtable"><div class="card-h"><h2>CUPED 方差缩减${asof}</h2><button type="button" class="btn copy" data-copy="cuped">复制表格</button></div>` +
    tableHTML({ header, rows }, "dtab2") +
    `<p class="foot">${cp.stale ? `CUPED 只在第 3 / 7 / 14 天和冻结时重算（SG 一次约 20 分钟），这里是 ${toISO(cp.d1).slice(5)} 的读数，「Δ 原」同一截止日，和上方累计行可能不一致。` : ""}协变量 = 同一设备在上线前 ${toISO(cp.pre.p0 || "").slice(5)}~${toISO(cp.pre.p1 || "").slice(5)}（${Math.round((new Date(toISO(cp.pre.p1)) - new Date(toISO(cp.pre.p0))) / 864e5) + 1} 天${cp.pre.cut ? "；" + esc(cp.pre.cut) : ""}）的同口径取值，上线前没搜过的设备按 0 计入；调整系数 θ 两组合并估计。CUPED 不改变要估的量，只把与实验无关的个体差异扣掉，方差降多少取决于上线前后取值的相关程度：搜索是低频行为，结果页点击渗透率、点击 → 3s 一般只降 0~5%，人均 VV 能降 10~20%。原读数（上方表格）仍是回收文档口径，这里只作并列参考。</p></section>`;
}

// ─────────── 长期价值：每设备一个值（d0 后 7 天，App 全站口径），组内取设备均值 ───────────
const ltDelta = (u, c) => { const r = c.rel != null ? `（${sgn(c.rel * 100, 1)}%）` : "";
  return u === "pct" ? sgn(c.diff * 100, 2) + "pp" : u === "day" ? sgn(c.diff, 3) + " 天" + r : sgn(c.diff, 2) + r; };
const ltMde = (u, c) => u === "rev" ? "±" + c.mde_abs.toFixed(2) + (c.mde_rel != null ? `（±${(c.mde_rel * 100).toFixed(0)}%）` : "") : u === "pct" ? "±" + (c.mde_abs * 100).toFixed(2) + "pp" : u === "day" ? "±" + c.mde_abs.toFixed(3) + " 天" : "±" + c.mde_abs.toFixed(2);
const ltVal = (u, x) => !x || x.value == null ? "—" : u === "pct" ? pct(x.value) : x.value.toFixed(u === "day" ? 4 : 2);
// 收入两行：判读用 P99.9 封顶均值，不封顶均值写在括号里
function ltHTML(e, v, base) {
  const lt = v.lt, pend = (e.pending || []).includes("@lt");
  const head = s => `<section class="mtable"><div class="card-h"><h2>长期价值${s}</h2>${lt && !lt.immature ? `<button type="button" class="btn copy" data-copy="lt">复制表格</button>` : ""}</div>`;
  if (!lt) return pend ? `<p class="muted">长期价值补算中…</p>` : "";
  if (lt.immature) return head("") + `<p class="muted">未成熟：还没有进组满 7 天的设备（数据到 ${toISO(lt.cut).slice(5)}）。</p></section>`;
  const ctrl0 = e.groups.find(g => g.role === "control");
  if (base.id !== ctrl0.id) return "";
  const trts = e.groups.filter(g => g.role !== "control"), multi = trts.length > 1, lab = g => g.label && g.label !== g.id ? `${g.label} ${g.id}` : g.id;
  const header = ["指标（不含 d0，App 全站口径）", `对照 ${lab(ctrl0)}`];
  for (const g of trts) header.push(`实验 ${lab(g)}`, multi ? `Δ（${g.label || g.id}）` : "Δ", "z", "MDE", "判读");
  const rv = (m, x) => m.unit !== "rev" || !x ? ltVal(m.unit, x) : `${x.value.toFixed(2)}（不封顶 ${x.raw.toFixed(2)}）`;
  const rows = lt.metrics.map(m => {
    if (m.na) return [m.name + "（SG 计费口径，P99.9 封顶，单位美丝）", { v: m.na, cls: "muted" }, ...trts.flatMap(() => ["", "", "", "", ""])];
    const out = [m.unit === "rev" ? m.name + "（SG 计费口径，P99.9 封顶，单位美丝）" : m.name, rv(m, m.groups[ctrl0.id])];
    for (const g of trts) {
      const c = m.vs[g.id];
      if (!c) { out.push(rv(m, m.groups[g.id]), "—", "—", "—", "—"); continue; }
      const cls = c.read ? (c.read === "显著正向" ? "up" : "down") : "";
      const rd = c.stop ? "⚠ 判止损" : c.read ? c.read + (c.adj && c.adj.ok === false ? "（修正后不显著）" : c.adj && c.adj.ok ? "（修正后仍显著）" : "")
        : `未见变化（MDE ${ltMde(m.unit, c)}）`;
      out.push(rv(m, m.groups[g.id]), { v: ltDelta(m.unit, c), cls: ("dl " + cls).trim() }, sgn(c.z, 2), ltMde(m.unit, c), { v: rd, cls: c.stop ? "down" : cls });
    }
    return out;
  });
  const asof = `（已成熟设备 ${fmt(lt.n)} 台，进组截至 ${toISO(lt.entry_end).slice(5)}）`;
  COPY.lt = () => ({ caption: `${e.name} · ${mktText(e)} · 长期价值${asof}`, header, rows });
  const cp = lt.caps || {}, capTxt = cp.vv != null ? `VV ${fmt(cp.vv)}、3sVV ${fmt(cp.vv3)}、时长 ${(cp.dur / 60000).toFixed(1)} 分钟` : "—";
  const revTxt = cp.fee != null ? `收入 / 广告曝光 = 窗口内 5 个流内广告位的计费收入（billing_fee）/ 计费曝光（billing_view）之和，事件日志 client_info 关联广告侧 GAID、按北京日对齐，剔占位 GAID；单设备 7 日累计值按上线前 7 天 P99.9 封顶（收入 ${fmt(cp.fee)} 美丝、曝光 ${fmt(cp.imp)} 次），收入极稀疏，P99 封顶等于不封；判读用封顶值，不进止损；÷100000 = 美元。` : "";
  return head(asof) + tableHTML({ header, rows }, "dtab2") +
    `<p class="foot">${lt.stale ? `长期价值只在第 7 / 14 天和冻结时重算，这里是数据到 ${toISO(lt.d1).slice(5)} 的读数。` : ""}人群 = 读数表同一批认组设备（结果页事件带组号，跨组剔除），d0 = 首次带组号的日期，每台设备只算一次；只算 d0 + 7 不晚于 ${toISO(lt.cut).slice(5)} 的设备。窗口 = d0 之后第 1~7 天，不含 d0。
    大盘 LT7 = 窗口内出现在 App 活跃表的天数（0~7）；次留 / 7 留 = d0+1 / d0+7 当天是否活跃；活跃按 App 全站，不按 region 过滤。7 日 VV / 3sVV / 时长 = 窗口内 play_close 条数 / is_3s_valid_play 条数 / play_duration（0~60 分钟的单条）之和，不限 from；单设备 7 日累计值按上线前 7 天 P99 封顶（${capTxt}）。
    标准误按设备算；判读同读数表（p &lt; 0.05 + 多次看数阈值），全部指标 × 实验组一起做 BH。${revTxt}大盘 LT7 / 次留 / 7 留 显著变差且过了多次看数阈值标「⚠ 判止损」，同时推送提醒。</p></section>`;
}

// ─────────── 分群下钻：只用来解释效应来自哪些人，不进结论 ───────────
function segHTML(e, v, base) {
  const sg = v.seg, pend = (e.pending || []).includes("@seg");
  if (!sg || !sg.dims.length) return pend ? `<p class="muted">分群下钻补算中…</p>` : "";
  const ctrl0 = e.groups.find(g => g.role === "control");
  if (base.id !== ctrl0.id) return "";
  const dim = sg.dims.find(d => d.dim === S.segDim) || sg.dims[0];
  const mets = [...new Map(dim.metrics.map(m => [m.key, m])).values()];
  const mk = (mets.find(m => m.key === S.segMet) || mets[0] || {}).key;
  const ms = dim.metrics.filter(m => m.key === mk), multi = ms.length > 1;
  const seg = (id, opts, cur) => `<div class="seg small" id="${id}">${opts.map(([k, l]) => `<button type="button" data-k="${esc(k)}" class="${k === cur ? "on" : ""}">${esc(l)}</button>`).join("")}</div>`;
  const header = [dim.name, "设备数（对照 / 实验）"];
  for (const m of ms) header.push(multi ? `Δ（${(e.groups.find(g => g.id === m.grp) || {}).label || m.grp}）` : "Δ", "z", "判读");
  const vals = ms.length ? ms[0].cells.map(c => c.val) : [];
  const rows = vals.map((val, i) => {
    const c0 = ms[0].cells[i], out = [dim.labels[val] || val, `${fmt(c0.n[ctrl0.id])} / ${ms.map(m => fmt(m.cells[i].n[m.grp])).join(" / ")}`];
    for (const m of ms) {
      const c = m.cells[i];
      if (c.small || c.diff == null) { out.push(c.small ? "样本不足" : "—", "—", "—"); continue; }
      const cls = c.read && c.bh ? (c.read === "显著正向" ? "up" : "down") : "";
      out.push({ v: m.unit !== "num" ? sgn(c.diff * 100, 2) + "pp" : sgn((c.rel || 0) * 100, 1) + "%", cls: ("dl " + cls).trim() }, sgn(c.z, 2),
        { v: c.read ? (c.bh ? c.read : c.read + "（修正后不显著）") : "—", cls });
    }
    return out;
  });
  const het = ms.map(m => m.het ? `${multi ? ((e.groups.find(g => g.id === m.grp) || {}).label || m.grp) + "：" : ""}各群效应${m.het.p < 0.05 ? "<b>不一致</b>" : "无显著差异"}（Cochran Q = ${m.het.q.toFixed(1)}，df ${m.het.df}，p = ${m.het.p < 0.001 ? m.het.p.toExponential(1) : m.het.p.toFixed(3)}）` : "").filter(Boolean).join("；");
  COPY.seg = () => ({ caption: `${e.name} · ${mktText(e)} · 分群下钻：${dim.name} × ${(mets.find(m => m.key === mk) || {}).name}`, header, rows });
  return `<section class="mtable"><div class="card-h"><h2>分群下钻${sg.stale ? `（截至 ${toISO(sg.d1).slice(5)}）` : "（全窗口累计）"}</h2><div class="controls">${seg("segDim", sg.dims.map(d => [d.dim, d.name]), dim.dim)}<button type="button" class="btn copy" data-copy="seg">复制表格</button></div></div>
    <div class="controls seg-met">${seg("segMet", mets.map(m => [m.key, m.name]), mk)}</div>` + tableHTML({ header, rows }, "dtab2") +
    `<p class="foot">${sg.stale ? `分群只在第 3 / 7 / 14 天和冻结时重算，这里是 ${toISO(sg.d1).slice(5)} 的读数。` : ""}${het ? het + "。" : ""}设备按实验期窗口内的行为分群：国家 / 新老用户 / 入口取自窗口内的结果页加载（新用户 = 窗口内任一条 new_user = 1；入口按是否用过 Toolbar、站内划分）；搜索词类型取自中间页点词（手输 / 历史 / 热词，按次数取最多的一类，窗口内没在中间页点过词的单列）。分群取值是实验期行为，可能被实验本身改变，只用来解释效应来自哪些人，不进结论、不据此推全；同一维度内各群一起做 BH 修正，单组设备不足 ${fmt(sg.min_n)} 台的群不出读数。</p></section>`;
}

// ─────────── 读数可视化 ───────────
const TRT = {};   // TRT[实验 id] = 指标卡当前看的组
const gLab = g => g.label && g.label !== g.id ? `${g.label}·${g.id}` : g.id;
const ROLE_ORDER = { "北极星": 0, "过程": 1, "护栏": 2 };
const sortTables = ts => [...ts].sort((a, b) => (ROLE_ORDER[a.group] ?? 3) - (ROLE_ORDER[b.group] ?? 3));
const totalRow = t => t.rows.find(r => r.type === "total");
// 变化幅度：百分比指标用 pp（与读数表 Δ 一致），数值指标用相对差 %
const dFmt = (t, v) => v == null || !isFinite(v) ? "—" : t.unit !== "num" ? sgn(v * 100, 2) + "pp" : sgn(v * 100, 1) + "%";
const dOf = (t, c) => c ? (t.unit !== "num" ? [c.diff, c.ci_low, c.ci_high, c.mde_abs] : [c.rel, c.rel_ci_low, c.rel_ci_high, c.mde_rel]) : null;
const sigCls = c => c && c.read ? (c.read === "显著正向" ? "up" : "down") : "";

function expCards(ts, base, trt) {
  // 多个「北极星」组指标时（如 EU 3s 率：站内北极星 + 全量北极星），标出实验自己的主指标
  const nStar = ts.filter(t => t.group === "北极星").length, pk = (ts.find(t => t.group === "北极星") || {}).key;
  return ts.map(t => {
    const r = totalRow(t), c = r.vs[trt.id], d = dOf(t, c), gb = r.groups[base.id], gt = r.groups[trt.id];
    const v = x => x && x.value != null ? (t.unit !== "num" ? pct(x.value) : x.value.toFixed(2)) : "—";
    if (!d) return `<div class="xcard"><div class="k">${esc(t.name)}</div><div class="v">—</div><div class="s">暂无累计读数</div></div>`;
    const [est, lo, hi, mde] = d, cls = sigCls(c);
    // 小条刻度：覆盖区间与 ±MDE，0 居中
    const span = Math.max(Math.abs(lo), Math.abs(hi), mde || 0) * 1.15 || 1e-9, X = u => 50 + u / span * 50;
    const tag = !c.read ? `<span class="tag">不显著</span>` : `<span class="tag ${cls}">${esc(c.read)}</span>` +
      (Math.abs(c.diff) < c.mde_abs ? `<span class="tag">低于可测下限</span>` : "") +
      (c.adj && c.adj.ok === false ? `<span class="tag" title="${esc(c.adj.why)}">修正后不显著</span>` : c.adj && c.adj.ok ? `<span class="tag ${cls}">修正后仍显著</span>` : "");
    const lbl = t.lower_is_better ? "越低越好" : "";
    return `<div class="xcard${t.group === "北极星" ? " hero" : ""}" data-tip="${esc(`${t.name}：${gLab(trt)} ${v(gt)} vs ${gLab(base)} ${v(gb)}；Δ ${dFmt(t, est)}，95% CI [${dFmt(t, lo)}, ${dFmt(t, hi)}]，可测下限 ±${dFmt(t, mde).replace(/^[+-]/, "")}；z = ${sgn(c.z, 2)}`)}">
      <div class="k">${t.group === "北极星" ? "北极星 · " : ""}${esc(t.name)}${lbl ? ` <small class="muted">${lbl}</small>` : ""}</div>
      <div class="v ${cls}">${dFmt(t, est)}</div>
      <div class="ci-bar" aria-label="95% 置信区间 ${dFmt(t, lo)} 到 ${dFmt(t, hi)}">
        <span class="mde" style="left:${X(-mde)}%;width:${X(mde) - X(-mde)}%"></span><span class="z0"></span>
        <span class="rng ${cls}" style="left:${X(lo)}%;width:${Math.max(X(hi) - X(lo), 0.8)}%"></span><span class="pt ${cls}" style="left:${X(est)}%"></span></div>
      <div class="s">[${dFmt(t, lo)}, ${dFmt(t, hi)}] · ${v(gt)} vs ${v(gb)}</div>
      <div class="tags">${nStar > 1 && t.key === pk ? `<span class="tag">本实验主指标</span>` : ""}${tag}</div></div>`;
  }).join("");
}

// 置信区间总览：每个指标一行、每个实验组一条区间线；横轴统一用相对变化，指标间才可比
function forest(el, e, ts, base) {
  const others = e.groups.filter(g => g.id !== base.id), key = "forest:" + e.id, hid = HIDE[key] ||= new Set();
  const vis = others.filter(g => !hid.has(gLab(g)));
  const rowsD = ts.map(t => ({ t, cs: vis.map(g => ({ g, c: totalRow(t).vs[g.id] })) }));
  const vals = rowsD.flatMap(r => r.cs.filter(x => x.c && x.c.rel != null).flatMap(x => [x.c.rel_ci_low, x.c.rel_ci_high, x.c.mde_rel, -x.c.mde_rel]));
  const legend = others.map((g, i) => `<button type="button" class="lg${hid.has(gLab(g)) ? " off" : ""}" data-i="${i}" aria-pressed="${!hid.has(gLab(g))}" title="点击显示 / 隐藏这一组"><i style="background:${gColor(e, g.id)}"></i>${esc(gLab(g))} vs ${esc(gLab(base))}</button>`).join("");
  el.dataset.title = `${e.name} · 置信区间总览（相对变化，95% CI；灰带 = 可测下限）`;
  const W = Math.max(el.clientWidth || 640, 360), LW = Math.min(230, W * 0.36), R = 70, gh = 14, rh = Math.max(30, vis.length * gh + 14), T = 8, B = 26;
  const H = T + ts.length * rh + B;
  let s = `<svg width="${W}" height="${H}" role="img" aria-label="置信区间总览">`;
  if (!vals.length) s += `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">${vis.length ? "暂无读数" : "组都关掉了，点上方图例重新显示"}</text>`;
  else {
    const m = Math.max(...vals.map(Math.abs)) * 1.12, X = u => LW + (u + m) / (2 * m) * (W - LW - R);
    const step = niceMax(m / 2) / 2, ticks = [];
    for (let i = -Math.floor(m / step); i * step <= m + 1e-12; i++) ticks.push(i * step);
    const td = Number.isInteger(+(step * 100).toFixed(6)) ? 0 : (step * 1000) % 1 ? 2 : 1;
    s += ticks.map(u => `<line class="gl" x1="${X(u)}" x2="${X(u)}" y1="${T}" y2="${H - B}"/><text x="${X(u)}" y="${H - 8}" text-anchor="middle">${Math.abs(u) < 1e-12 ? "0" : sgn(u * 100, td)}%</text>`).join("");
    rowsD.forEach(({ t, cs }, i) => {
      const y0 = T + i * rh, cy = j => y0 + (rh - (cs.length - 1) * gh) / 2 + j * gh;
      if (i) s += `<line class="gl" x1="0" x2="${W}" y1="${y0}" y2="${y0}"/>`;
      s += `<text class="lab${t.group === "北极星" ? " ns" : ""}" x="0" y="${y0 + rh / 2 + 4}">${esc(t.name.length > 18 ? t.name.slice(0, 17) + "…" : t.name)}</text>`;
      const md = Math.max(...cs.map(x => x.c ? x.c.mde_rel || 0 : 0));
      if (md) s += `<rect class="mdeband" x="${X(-md)}" y="${y0 + 3}" width="${X(md) - X(-md)}" height="${rh - 6}" rx="3"/>`;
      cs.forEach(({ g, c }, j) => {
        if (!c || c.rel == null) return;
        const col = gColor(e, g.id), cls = sigCls(c), yy = cy(j);
        s += `<line class="ci" x1="${X(c.rel_ci_low)}" x2="${X(c.rel_ci_high)}" y1="${yy}" y2="${yy}" style="stroke:${col}"/>`;
        s += `<circle cx="${X(c.rel)}" cy="${yy}" r="4.5" fill="${col}" stroke="var(--card)" stroke-width="2"/>`;
        s += `<text class="val ${cls}" x="${W - R + 8}" y="${yy + 4}">${sgn(c.rel * 100, 1)}%</text>`;
        s += `<rect class="hit" x="${LW}" y="${yy - gh / 2}" width="${W - LW}" height="${gh}" fill="transparent" data-t="${esc(`<b>${esc(t.name)}</b><br><span class="sw" style="background:${col}"></span>${esc(gLab(g))} vs ${esc(gLab(base))}：${sgn(c.rel * 100, 2)}%<br>95% CI [${sgn(c.rel_ci_low * 100, 2)}%, ${sgn(c.rel_ci_high * 100, 2)}%]<br>可测下限 ±${(c.mde_rel * 100).toFixed(2)}% · ${c.read || "不显著"}${t.unit !== "num" ? `<br>绝对差 ${dFmt(t, c.diff)}` : ""}`)}"/>`;
      });
    });
    s += `<line class="zero" x1="${X(0)}" x2="${X(0)}" y1="${T}" y2="${H - B}"/>`;
  }
  el.innerHTML = chartTop(legend + staticLegend([["var(--grid)", "可测下限（MDE）", "box"]])) + s + `</svg>`;
  $$(".hit", el).forEach(r => { r.addEventListener("mousemove", ev => showTip(r.dataset.t, ev)); r.addEventListener("mouseleave", hideTip); });
  $$(".legend .lg[data-i]", el).forEach(b => b.onclick = () => { const n = gLab(others[+b.dataset.i]); hid.has(n) ? hid.delete(n) : hid.add(n); forest(el, e, ts, base); });
}

// 分布箱型图：每组按设备取值（分子 ÷ 分母，同一并集、同一封顶）的 P5 / P25 / P50 / P75 / P95，菱形 = 设备均值
function boxPlot(el, note, e, k, dist, base) {
  const gs = [base, ...e.groups.filter(g => g.id !== base.id)].filter(g => dist[g.id] && dist[g.id].metrics && dist[g.id].metrics[k]);
  const key = "box:" + e.id + ":" + k, hid = HIDE[key] ||= new Set(), vis = gs.filter(g => !hid.has(gLab(g)));
  const D = g => dist[g.id].metrics[k], f2 = v => v == null ? "—" : (+v).toFixed(Math.abs(v) < 10 ? 2 : 1);
  el.dataset.title = `${e.name} · ${metricName(k)} · 每设备取值分布`;
  const legend = gs.map((g, i) => `<button type="button" class="lg${hid.has(gLab(g)) ? " off" : ""}" data-i="${i}" aria-pressed="${!hid.has(gLab(g))}"><i style="background:${gColor(e, g.id)}"></i>${esc(gLab(g))}${g.id === base.id ? "（基准）" : ""}</button>`).join("");
  const short = g => { const t = g.label && g.label !== g.id ? g.label : g.id; return t.length > 14 ? t.slice(0, 13) + "…" : t; };
  const W = Math.max(el.clientWidth || 640, 360), LW = 120, R = 24, bh = 22, rh = 40, T = 6, B = 26, H = T + Math.max(vis.length, 1) * rh + B;
  const vals = vis.flatMap(g => [...D(g).q, D(g).avg]).filter(v => v != null);
  let s = `<svg width="${W}" height="${H}" role="img" aria-label="分布箱型图">`;
  if (!vals.length) s += `<text x="${W / 2}" y="${H / 2}" text-anchor="middle">${vis.length ? "无数据" : "组都关掉了，点上方图例重新显示"}</text>`;
  else {
    const mx = niceMax(Math.max(...vals)), X = v => LW + v / mx * (W - LW - R);
    s += [0, .25, .5, .75, 1].map(u => `<line class="gl" x1="${X(u * mx)}" x2="${X(u * mx)}" y1="${T}" y2="${H - B}"/><text x="${X(u * mx)}" y="${H - 8}" text-anchor="middle">${f2(u * mx)}</text>`).join("");
    vis.forEach((g, i) => {
      const [p5, p25, p50, p75, p95] = D(g).q, avg = D(g).avg, cy = T + i * rh + rh / 2, col = gColor(e, g.id);
      s += `<text class="lab" x="0" y="${cy + 4}">${esc(short(g))}</text>`;
      s += `<line class="whisk" x1="${X(p5)}" x2="${X(p95)}" y1="${cy}" y2="${cy}" style="stroke:${col}"/>`;
      s += [p5, p95].map(v => `<line class="whisk" x1="${X(v)}" x2="${X(v)}" y1="${cy - 6}" y2="${cy + 6}" style="stroke:${col}"/>`).join("");
      s += `<rect x="${X(p25)}" y="${cy - bh / 2}" width="${Math.max(X(p75) - X(p25), 2)}" height="${bh}" rx="3" class="boxr" style="fill:${col};stroke:${col}"/>`;
      s += `<line x1="${X(p50)}" x2="${X(p50)}" y1="${cy - bh / 2}" y2="${cy + bh / 2}" class="med"/>`;
      if (avg != null) s += `<path d="M${X(avg)},${cy - 6}l6,6l-6,6l-6,-6z" fill="var(--card)" stroke="${col}" stroke-width="2"/>`;
      s += `<rect class="hit" x="${LW}" y="${cy - rh / 2}" width="${W - LW}" height="${rh}" fill="transparent" data-t="${esc(`<b>${esc(gLab(g))}</b>（${fmt(D(g).n)} 台设备）<br>P5 ${f2(p5)} · P25 ${f2(p25)} · P50 ${f2(p50)}<br>P75 ${f2(p75)} · P95 ${f2(p95)}<br>设备均值 ${f2(avg)}`)}"/>`;
    });
  }
  el.innerHTML = chartTop(legend + staticLegend([["var(--muted)", "箱 = P25~P75，竖线 = P50，须 = P5~P95，◇ = 设备均值"]])) + s + `</svg>`;
  $$(".hit", el).forEach(r => { r.addEventListener("mousemove", ev => showTip(r.dataset.t, ev)); r.addEventListener("mouseleave", hideTip); });
  $$(".legend .lg[data-i]", el).forEach(b => b.onclick = () => { const n = gLab(gs[+b.dataset.i]); hid.has(n) ? hid.delete(n) : hid.add(n); boxPlot(el, note, e, k, dist, base); });
  // 说明：均值远高于中位数 = 少数重度用户在拉均值
  const hint = gs.map(g => { const x = D(g); return x.q[2] != null && x.avg != null ? `${gLab(g)} 均值 ${f2(x.avg)} / 中位数 ${f2(x.q[2])} / P95 ${f2(x.q[4])}` : ""; }).filter(Boolean);
  note.textContent = `每台进组设备（窗口内有分母的）一个取值，口径同读数（同一设备并集、同一 P99 封顶）。${hint.join("；")}。均值明显高于中位数 = 均值主要由少数重度设备拉动；读人均类指标的 Δ 时，再看两组的箱和 P95 是否一起移动。`;
  COPY["dist_" + k] = () => ({ caption: `${e.name} · ${metricName(k)} · 每设备取值分布`, header: ["组", "设备数", "P5", "P25", "P50", "P75", "P95", "设备均值"],
    rows: gs.map(g => [gLab(g) + (g.id === base.id ? "（基准）" : ""), fmt(D(g).n), ...D(g).q.map(f2), f2(D(g).avg)]) });
}

// 进组趋势：每天新进组 / 累计进组设备数，分组对比
function entryChart(el, e, dist) {
  const gs = e.groups.filter(g => dist[g.id] && dist[g.id].entry), days = [...new Set(gs.flatMap(g => Object.keys(dist[g.id].entry)))].map(Number).sort();
  const cum = (S.entry || "cum") === "cum";
  const series = gs.map(g => { let a = 0; return { name: gLab(g) + (g.role === "control" ? "（对照）" : ""), color: gColor(e, g.id),
    pts: days.map(d => { const n = dist[g.id].entry[d] || 0; a += n; return cum ? a : n; }) }; });
  lineChart(el, days, series, fmtShort, false, null, { key: "entry:" + e.id, title: `${e.name} · ${cum ? "累计进组设备数" : "每天新进组设备数"}` });
}

// ─────────── 指标库 ───────────
async function loadMetrics() {
  METRICS = await api("/api/metrics");
  $("#metricList").innerHTML = `<div class="scroll"><table><thead><tr><th>指标</th><th>类型</th><th></th></tr></thead><tbody>` + METRICS.map(m => `<tr>
    <td class="name">${esc(m.name)}<small class="muted">${esc(m.key)} · ${esc(m.note || "")}</small></td>
    <td>${m.kind === "quantile" ? "分位数" : "比率"}${m.preset ? ' <span class="pill">预置</span>' : ""}</td>
    <td class="acts"><button class="btn" data-edit="${m.key}">编辑</button>${m.preset ? (m.edited ? ` <span class="pill warn">已改</span>` : "") : ` <button class="btn danger" data-del="${m.key}">删除</button>`}</td></tr>`).join("") + `</tbody></table></div>`;
  $$("[data-del]").forEach(b => b.onclick = async () => {
    const used = await api(`/api/metrics/${b.dataset.del}/usage`);
    if (!confirm(used.length ? `「${b.dataset.del}」被 ${used.length} 个实验用到（${used.map(e => e.name).join("、")}），删了这些实验里就看不到它了。仍要删？` : `删除指标「${b.dataset.del}」？`)) return;
    await api(`/api/metrics/${b.dataset.del}`, { method: "DELETE" }); loadMetrics(); });
  $$("[data-edit]").forEach(b => b.onclick = () => editMetric(METRICS.find(m => m.key === b.dataset.edit)));
  const sideText = s => !s ? "" : `${[].concat(s.event).join("+")} ${s.agg || ""}${(s.filters || []).length ? " | " + s.filters.map(f => f.map(x => [].concat(x).join(",")).join(" ")).join(" AND ") : ""}`.trim();
  COPY.metrics = () => ({ caption: "搜索回收看板 · 指标库", header: ["key", "指标", "类型", "分组", "分子", "分母", "分子只算分母设备", "P99 封顶", "说明"],
    rows: METRICS.map(m => [m.key, m.name, m.kind === "quantile" ? "分位数" : "比率", m.group || "", sideText(m.kind === "quantile" ? m.source : m.numerator),
      m.kind === "quantile" ? `${m.column} @ ${(m.quantiles || []).join(",")}` : sideText(m.denominator),
      m.numerator_within_denominator ? "是" : "", m.cap ? "是" : "", m.note || ""]) });
  if (!$("#numBox").innerHTML) buildMetricForm();
  loadVersions();
}

// 「分开看」新版本列表：发版记录表里归在 Sprint 90 及以后的版本，按版本号精确匹配
async function loadVersions() {
  const c = await api("/api/versions"), r = c.report || {};
  const t = x => x ? new Date(x * 1000).toLocaleString("zh-CN", { hour12: false }) : null;
  let n = "";
  if (c.error) n += `<div class="notice warn"><b>版本同步失败</b>${esc(c.error)}；沿用${c.synced ? ` ${esc(t(c.fetched_at))} 的` : "初始"}列表。</div>`;
  if ((r.added || []).length) n += `<div class="notice info"><b>上次同步新增 ${r.added.length} 个</b>${r.added.map(esc).join("、")}${r.job_id ? `；已提交「分开看」补算（任务 #${r.job_id}，见任务日志）` : ""}。</div>`;
  if ((r.missing || []).length) n += `<div class="notice warn"><b>表里找不到</b>${r.missing.map(esc).join("、")}：仍保留在列表里，确认不要了再找看板维护人删。</div>`;
  if ((r.no_rollout || []).length) n += `<div class="notice warn"><b>没找到放量记录</b>${r.no_rollout.map(esc).join("、")}：渠道和首次放量日为空，等表里写了放量再同步。</div>`;
  const src = c.synced ? `${esc(t(c.fetched_at))} 从<a href="${esc(c.sheet_url)}" target="_blank" rel="noopener">发版记录表</a>同步` : `初始列表（${esc(c.fallback)}），还没点过「同步版本」`;
  const rows = c.versions.map(v => [v.v, v.ch || "", v.sprint ? "SP" + v.sprint : "", v.first ? toISO(v.first) : "", v.desc || ""]);
  $("#verBox").innerHTML = n + `<p class="muted">共 ${c.versions.length} 个；${src}。${META.readonly ? "" : "每个 Sprint 发版后点一次「同步版本」。"}</p>` +
    `<div class="scroll"><table><thead><tr><th>版本号</th><th>渠道</th><th>Sprint</th><th>首次放量</th><th>说明</th></tr></thead><tbody>` +
    rows.map(x => `<tr>${x.map(y => `<td>${esc(y)}</td>`).join("")}</tr>`).join("") + `</tbody></table></div>`;
  COPY.versions = () => ({ caption: "搜索回收看板 · 「分开看」新版本列表", header: ["版本号", "渠道", "Sprint", "首次放量", "说明"], rows });
}

async function syncVersions() {
  const b = $("#vSync"); b.disabled = true;
  try {
    const r = await api("/api/versions/sync", { method: "POST", body: {} });
    flash(b, r.ok ? (r.added.length ? `新增 ${r.added.length} 个` : "已是最新") : "同步失败", r.ok);
    await loadVersions();
  } catch (e) { flash(b, "同步失败", false); }
  finally { b.disabled = false; }
}

// 输入联想选择框：输入代码或中文名模糊匹配，点选（或 ↑↓ 回车）确认；多选的已选项显示为可删除的标签
// opts = [{ v, t }]（值 / 中文名）；返回 { get(), set(v) }，多选 get() 返回数组
let PICK_SEQ = 0;
function picker(el, opts, { multi = false, placeholder = "输入搜索，如 卡片 / card", value } = {}) {
  const id = "pk" + (++PICK_SEQ);
  el.classList.add("pick"); el.classList.toggle("multi", multi);
  el.innerHTML = `<div class="pick-box"><span class="chips"></span><input type="text" role="combobox" aria-expanded="false" aria-controls="${id}" aria-autocomplete="list" autocomplete="off" placeholder="${esc(placeholder)}"></div>
    <ul class="pick-list" id="${id}" role="listbox" hidden></ul><div class="pick-state"></div>`;
  const box = el.querySelector(".pick-box"), inp = el.querySelector("input"), list = el.querySelector("ul"), chips = el.querySelector(".chips"), state = el.querySelector(".pick-state");
  const name = v => (opts.find(o => o.v === v) || { t: "" }).t;
  let sel = [], hits = [], hi = 0;
  const score = (o, q) => {
    if (!q) return 1;
    const s = (o.v + " " + o.t).toLowerCase();
    if (o.v.toLowerCase().startsWith(q) || o.t.toLowerCase().startsWith(q)) return 4;
    if (s.includes(q)) return 3;
    let i = 0; for (const ch of s) if (ch === q[i]) i++;   // 子序列：car_cl → card_click
    return i === q.length ? 2 : 0;
  };
  const paint = () => {
    chips.innerHTML = multi ? sel.map(v => `<span class="chip" title="${esc(v)}">${esc(name(v) || v)}<button type="button" data-v="${esc(v)}" aria-label="移除 ${esc(name(v) || v)}">×</button></span>`).join("") : "";
    if (!multi) inp.value = sel.length ? `${name(sel[0]) || sel[0]}` : inp.value;
    state.innerHTML = sel.length ? `已选：${sel.map(v => `<code>${esc(v)}</code>`).join(" + ")}` : `<span class="warn-t">未选</span>`;
    el.classList.toggle("empty", !sel.length);
  };
  const open = () => {
    const q = (multi || !sel.length || inp.value !== (name(sel[0]) || sel[0]) ? inp.value : "").trim().toLowerCase();
    hits = opts.map(o => [o, score(o, q)]).filter(x => x[1]).sort((a, b) => b[1] - a[1]).map(x => x[0]);
    hi = Math.min(hi, Math.max(hits.length - 1, 0));
    list.innerHTML = hits.length ? hits.map((o, i) => `<li role="option" data-v="${esc(o.v)}" aria-selected="${sel.includes(o.v)}" class="${i === hi ? "hi" : ""}">
      <span class="ok">${sel.includes(o.v) ? "✓" : ""}</span><span class="t">${esc(o.t || o.v)}</span><code>${esc(o.v)}</code></li>`).join("") : `<li class="none">没有匹配项</li>`;
    list.hidden = false; inp.setAttribute("aria-expanded", "true");
  };
  const close = () => { list.hidden = true; inp.setAttribute("aria-expanded", "false"); if (!multi) paint(); else inp.value = ""; };
  const pick = v => {
    if (multi) { sel = sel.includes(v) ? sel.filter(x => x !== v) : [...sel, v]; inp.value = ""; paint(); open(); inp.focus(); el.dispatchEvent(new Event("change")); }
    else { sel = [v]; inp.value = ""; paint(); close(); el.dispatchEvent(new Event("change")); }
  };
  inp.addEventListener("focus", () => { if (!multi) inp.select(); open(); });
  inp.addEventListener("input", () => { hi = 0; open(); });
  // 输入框原生 change（失焦时）会冒泡到外层，和 pick() 自己派发的 change 撞车，导致重复加载
  inp.addEventListener("change", ev => ev.stopPropagation());
  inp.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); if (list.hidden) open(); hi = (hi + (ev.key === "ArrowDown" ? 1 : -1) + hits.length) % Math.max(hits.length, 1); open(); }
    else if (ev.key === "Enter") { ev.preventDefault(); if (!list.hidden && hits[hi]) pick(hits[hi].v); }
    else if (ev.key === "Escape") close();
    else if (ev.key === "Backspace" && multi && !inp.value && sel.length) { sel = sel.slice(0, -1); paint(); open(); el.dispatchEvent(new Event("change")); }
  });
  list.addEventListener("mousedown", ev => { const li = ev.target.closest("li[data-v]"); if (li) { ev.preventDefault(); pick(li.dataset.v); } });
  chips.addEventListener("click", ev => { const b = ev.target.closest("button[data-v]"); if (b) { sel = sel.filter(x => x !== b.dataset.v); paint(); el.dispatchEvent(new Event("change")); } });
  box.addEventListener("click", ev => { if (ev.target === box || ev.target === chips) inp.focus(); });
  inp.addEventListener("blur", () => setTimeout(() => { if (!el.contains(document.activeElement)) close(); }, 120));
  const api_ = { get: () => multi ? [...sel] : sel[0], set: v => { sel = v == null ? [] : [].concat(v).filter(x => opts.some(o => o.v === x)); inp.value = ""; paint(); } };
  api_.set(value); el._pick = api_;
  return api_;
}
const lbl = c => (META.labels || {})[c] || "";
const OP_TEXT = { "=": "等于", "!=": "不等于", in: "属于（逗号分隔）", not_in: "不属于（逗号分隔）", ">": "大于", ">=": "大于等于", "<": "小于", "<=": "小于等于", like: "模糊匹配（% 通配）", is_null: "为空", not_null: "不为空" };

function sideBox(el, title, withAgg) {
  el.innerHTML = `<div class="side-box"><h3>${title}</h3><div class="side-grid">
      <div class="lbl-box">事件（可多选，结果合并计数）<div class="ev"></div></div>
      ${withAgg ? `<div class="lbl-box">计数方式<div class="agg"></div></div>` : ""}
    </div><div class="flts"></div><button type="button" class="btn ghost addf">+ 筛选条件</button></div>`;
  picker(el.querySelector(".ev"), META.events.map(e => ({ v: e, t: lbl(e) })), { multi: true, placeholder: "输入搜索，如 卡片 / card_click" });
  if (withAgg) picker(el.querySelector(".agg"), [{ v: "uv", t: "UV（设备去重）" }, { v: "pv", t: "PV（条数）" }, ...META.num_cols.map(c => ({ v: "sum:" + c, t: "求和 " + (lbl(c) || c) }))],
    { placeholder: "输入搜索，如 UV / 求和", value: "uv" });
  el.querySelector(".addf").onclick = () => addFilter(el.querySelector(".flts"));
}
function addFilter(box, f) {
  const cols = [...META.str_cols, ...META.num_cols, ...META.bool_cols];
  const d = document.createElement("div"); d.className = "flt-row";
  d.innerHTML = `<div class="fc"></div>
    <select class="fo" aria-label="运算符">${Object.entries(OP_TEXT).map(([o, t]) => `<option value="${o}">${o}　${t}</option>`).join("")}</select>
    <input class="fv" aria-label="值" placeholder="值；in 用逗号分隔；可写 0.9*video_duration" size="30">
    <button type="button" class="btn danger" aria-label="删除这条筛选">×</button>`;
  picker(d.querySelector(".fc"), cols.map(c => ({ v: c, t: lbl(c) })), { placeholder: "字段，如 来源页 / from", value: f ? f[0] : undefined });
  const fo = d.querySelector(".fo"), fv = d.querySelector(".fv");
  fo.onchange = () => { fv.hidden = ["is_null", "not_null"].includes(fo.value); };
  if (f) { fo.value = f[1]; fv.value = Array.isArray(f[2]) ? f[2].join(",") : (f[2] ?? ""); fo.onchange(); }
  d.querySelector(".danger").onclick = () => d.remove();
  box.appendChild(d);
}
function readSide(el, withAgg) {
  const ev = el.querySelector(".ev")._pick.get();
  if (!ev.length) throw new Error(`${el.querySelector("h3").textContent}：还没选事件`);
  const filters = $$(".flts .flt-row", el).map(r => {
    const col = r.querySelector(".fc")._pick.get(), op = r.querySelector(".fo").value, v = r.querySelector(".fv").value.trim();
    if (!col) throw new Error(`${el.querySelector("h3").textContent}：有一条筛选条件没选字段`);
    return ["is_null", "not_null"].includes(op) ? [col, op] : [col, op, op.includes("in") && op !== "like" ? v.split(",").map(x => x.trim()).filter(Boolean) : v];
  });
  // 编辑含「任一满足」组合条件的指标时，原样带回那几条（表单只编辑普通条件）
  const keep = $("#mForm")._anyKeep, side = { numBox: "numerator", denBox: "denominator", srcBox: "source" }[el.id];
  if (keep && keep[side]) filters.push(...(keep[side].filters || []).filter(y => y[0] === "any"));
  const s = { event: ev.length === 1 ? ev[0] : ev, filters };
  if (withAgg) s.agg = el.querySelector(".agg")._pick.get() || "uv";
  return s;
}
function buildSides() {
  sideBox($("#numBox"), "分子", true);
  sideBox($("#denBox"), "分母", true);
  sideBox($("#srcBox"), "取值事件", false);
}
function buildMetricForm() {
  buildSides();
  picker($("#qCol"), META.num_cols.map(c => ({ v: c, t: lbl(c) })), { placeholder: "输入搜索，如 耗时 / use_time", value: "use_time" });
  const f = $("#mForm");
  f.kind.onchange = () => { const q = f.kind.value === "quantile";
    $$(".ratio-only", f).forEach(x => x.hidden = q); $$(".quant-only", f).forEach(x => x.hidden = !q); };
  $("#mPreview").onclick = async () => {
    try { const j = await api("/api/metrics/preview", { method: "POST", body: readMetric() }); $("#mSql").textContent = j.sql; $("#mSql").hidden = false; $("#mMsg").textContent = ""; }
    catch (e) { $("#mMsg").textContent = e.message; }
  };
  f.onsubmit = async ev => {
    ev.preventDefault();
    try {
      if (EDIT_METRIC) {
        const used = await api(`/api/metrics/${EDIT_METRIC}/usage`);
        if (used.length && !confirm(`「${EDIT_METRIC}」被 ${used.length} 个实验用到（${used.map(e => e.name).join("、")}）。\n改了口径会删掉这些实验里该指标的旧结果并自动补算；只改名字 / 分组 / 备注不重算。继续？`)) return;
        const j = await api(`/api/metrics/${EDIT_METRIC}`, { method: "PUT", body: readMetric() });
        $("#mMsg").textContent = !j.changed ? "已保存（口径没变，不用重算）" : j.rerun.length ? `已保存，口径变了：${j.rerun.map(r => `「${r.exp}」补算 #${r.job_id}`).join("、")}` : "已保存（口径变了；暂无实验用到）";
        endEditMetric(false);
      } else {
        await api("/api/metrics", { method: "POST", body: { key: f.key.value.trim(), ...readMetric() } }); $("#mMsg").textContent = "已保存";
      }
      loadMetrics();
    } catch (e) { $("#mMsg").textContent = e.message; }
  };
  $("#mCancel").onclick = () => endEditMetric(true);
  $("#mRestore").onclick = async () => {
    if (!confirm(`把「${EDIT_METRIC}」恢复成预置定义？用到它的实验会按预置口径补算。`)) return;
    try { const j = await api(`/api/metrics/${EDIT_METRIC}`, { method: "PUT", body: { restore: true } });
      $("#mMsg").textContent = j.changed ? `已恢复预置${j.rerun.length ? "，" + j.rerun.map(r => `「${r.exp}」补算 #${r.job_id}`).join("、") : ""}` : "已恢复预置（口径没变）";
      endEditMetric(false); loadMetrics(); } catch (e) { $("#mMsg").textContent = e.message; }
  };
}
let EDIT_METRIC = null;
function setSide(el, s) {
  if (!s) return;
  el.querySelector(".ev")._pick.set(s.event);
  if (el.querySelector(".agg")) el.querySelector(".agg")._pick.set(s.agg || "uv");
  const box = el.querySelector(".flts"); box.innerHTML = "";
  (s.filters || []).filter(x => x[0] !== "any").forEach(x => addFilter(box, x));   // 「任一满足」组合条件不进表单，保存时原样带回
}
function editMetric(m) {
  const f = $("#mForm");
  // 「任一满足」组合条件（如 3s = is_3s_valid_play 或 play_duration≥3000）表单拼不出来，只读不改
  const anyF = [m.numerator, m.denominator, m.source].some(x => x && (x.filters || []).some(y => y[0] === "any"));
  EDIT_METRIC = m.key;
  $("#mTitle").textContent = `编辑「${m.name}」`;
  f.key.value = m.key; f.key.readOnly = true;
  f.name.value = m.name; f.group.value = m.group || ""; f.note.value = m.note || "";
  f.kind.value = m.kind || "ratio"; f.kind.onchange(); f.kind.disabled = true;
  if (m.kind === "quantile") { setSide($("#srcBox"), m.source); $("#qCol")._pick.set(m.column); f.quantiles.value = (m.quantiles || []).join(","); }
  else { setSide($("#numBox"), m.numerator); setSide($("#denBox"), m.denominator);
    f.within.checked = !!m.numerator_within_denominator; f.cap.checked = m.cap === "p99_pre7d"; f.lower.checked = !!m.lower_is_better; }
  $("#mCancel").hidden = false; $("#mRestore").hidden = !(m.preset && m.edited);
  $("#mSubmit").textContent = "保存修改";
  $("#mMsg").textContent = anyF ? "这个指标含「任一满足」组合条件，表单里改不了那一条；其余字段可以改。" : "";
  if (anyF) $("#mForm")._anyKeep = { numerator: m.numerator, denominator: m.denominator, source: m.source };
  else $("#mForm")._anyKeep = null;
  $("#mSql").hidden = true;
  f.scrollIntoView({ behavior: "smooth", block: "start" });
}
function endEditMetric(clearMsg) {
  const f = $("#mForm");
  EDIT_METRIC = null; f.reset(); f.key.readOnly = false; f.kind.disabled = false; f._anyKeep = null; f.kind.onchange();
  buildSides();
  $("#mTitle").textContent = "自定义指标"; $("#mSubmit").textContent = "保存"; $("#mCancel").hidden = true; $("#mRestore").hidden = true;
  if (clearMsg) $("#mMsg").textContent = "";
}
function readMetric() {
  const f = $("#mForm");
  const d = { name: f.name.value.trim(), kind: f.kind.value, group: f.group.value.trim() || "自定义", note: f.note.value.trim() };
  if (d.kind === "ratio") {
    d.numerator = readSide($("#numBox"), true); d.denominator = readSide($("#denBox"), true);
    d.numerator_within_denominator = f.within.checked; d.lower_is_better = f.lower.checked;
    if (f.cap.checked) d.cap = "p99_pre7d";
  } else {
    d.source = readSide($("#srcBox"), false); d.column = $("#qCol")._pick.get();
    if (!d.column) throw new Error("分位数列还没选");
    d.quantiles = f.quantiles.value.split(",").map(Number).filter(x => x > 0 && x < 1);
  }
  return d;
}

// ─────────── 日志 ───────────
// 任务日志：每个任务能追到「谁触发 → 跑了哪几条 SQL → 每条的 KG 任务号 / 行数 / 耗时」，SQL 可复制去数仓复跑核对
const JOB_KIND = { funnel: "漏斗刷新", core_history: "核心指标补历史", experiment: "实验回收", exp_followup: "实验补算（累计行 / By天 / 分位数）", exp_quantile: "实验补算（分位数）", hotword: "热词收益刷新", rollout_obs: "推全观测取数" };
const JOB_ST = { queued: "排队", running: "运行中", done: "完成", failed: "失败", cancelled: "已取消" };
const JOB_SQL = {};
function jobTarget(j) {
  const p = (() => { try { return JSON.parse(j.params); } catch (_) { return {}; } })();
  if (p.exp_id != null) {
    const name = (EXP_NAMES[p.exp_id] || `实验 #${p.exp_id}（已删除）`);
    const inc = [p.only && p.only.length ? "指标 " + p.only.join("、") : "", p.only_daily && p.only_daily.length ? "By天 " + p.only_daily.join("、") : ""].filter(Boolean).join("；");
    return `<a href="#exp" data-exp="${p.exp_id}">${esc(name)}</a>${p.only ? ` · 增量补算${inc ? "：" + esc(inc) : ""}` : ""}`;
  }
  if (p.start) return `${esc((p.markets || []).join(" / "))} · ${toISO(p.start)} ~ ${toISO(p.end)}`;
  if (j.kind === "rollout_obs") return "各轮推全观测 + 全局累计：缺的周 / 封顶";
  return esc(j.params);
}
let EXP_NAMES = {};
let JOBS_T = null;
async function loadJobs() {
  clearTimeout(JOBS_T);
  const open = $$("#jobList .jdet:not([hidden])").map(r => +r.id.slice(2));
  const [js, exps] = await Promise.all([api("/api/jobs"), api("/api/experiments").catch(() => [])]);
  EXP_NAMES = Object.fromEntries(exps.map(e => [e.id, e.name]));
  $("#schedInfo").textContent = `每日 ${META.daily_at} 自动刷新近 14 天缺失 / 不完整的日期，并续跑未冻结的实验`;
  const dur = j => j.started_at ? Math.round(((j.finished_at || Date.now() / 1000) - j.started_at)) : null;
  const ds = s => s == null ? "" : s >= 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${s} 秒`;
  $("#jobList").innerHTML = js.length ? `<div class="scroll"><table class="jtab"><thead><tr><th>任务号</th><th>类型</th><th>对象</th><th>状态</th><th>提交时间</th><th>耗时</th><th>明细</th></tr></thead><tbody>` +
    js.map(j => `<tr class="jrow" data-id="${j.id}"><td>#${j.id}</td><td>${esc(JOB_KIND[j.kind] || j.kind)}</td><td class="tgt">${jobTarget(j)}</td>
      <td><span class="pill ${j.status}">${JOB_ST[j.status] || j.status}</span>${["queued", "running"].includes(j.status) ? ` <button type="button" class="btn copy danger" data-cancel="${j.id}" title="${j.status === "running" ? "停在当前这条查询：已落库的保留，没跑的不再跑；数仓侧已提交的那条查询会自己跑完，结果不再收" : "从队列里拿掉，不跑"}">取消</button>` : ""}</td>
      <td>${new Date(j.created_at * 1000).toLocaleString()}</td><td>${ds(dur(j))}</td>
      <td><button type="button" class="btn copy" data-q="${j.id}">${j.n_queries ? `${j.n_queries} 条 SQL` : "日志"} ▸</button></td></tr>
      <tr class="jdet" id="jd${j.id}" hidden><td colspan="7"></td></tr>
      ${j.error ? `<tr class="jerr"><td colspan="7"><div class="notice ${j.status === "cancelled" ? "info" : "crit"}"><b>${j.status === "cancelled" ? "已取消" : "错误"}</b>${esc(j.error.replace(/[│█▀▄╭╮╰╯─]+/g, " ").replace(/\s+/g, " ").slice(0, 400))}</div></td></tr>` : ""}`).join("") + `</tbody></table></div>
    <p class="foot">「明细」= 该任务实际提交给数仓的查询，含 KG 任务号（kg_execute_analysis 返回的 task_id，可在 KG 侧查执行记录）、返回行数、耗时；复制后可直接到 Kyuubi / KG 复跑核对。2026-10-20 之前跑完的任务没有记录 SQL，只有运行日志。</p>` : `<p class="muted">暂无任务</p>`;
  $$("#jobList [data-q]").forEach(b => b.onclick = () => toggleJob(+b.dataset.q, b, js.find(j => j.id === +b.dataset.q)));
  $$("#jobList [data-cancel]").forEach(b => b.onclick = async () => {
    const id = +b.dataset.cancel, j = js.find(x => x.id === id);
    if (!confirm(`取消任务 #${id}（${JOB_KIND[j.kind] || j.kind}）？${j.status === "running" ? "\n已经拉回来的数据保留，剩下的不再跑。" : ""}`)) return;
    b.disabled = true;
    try { const r = await api(`/api/jobs/${id}/cancel`, { method: "POST", body: {} }); flash(b, r.state === "cancelling" ? "取消中…" : "已取消", true); }
    catch (e) { flash(b, e.message, false); }
    setTimeout(loadJobs, 800);
  });
  $$("#jobList [data-exp]").forEach(a => a.onclick = () => { CUR_EXP = +a.dataset.exp; });
  for (const id of open) { const b = $(`#jobList [data-q="${id}"]`); if (b) b.click(); }
  if (js.some(j => ["queued", "running"].includes(j.status)) && location.hash === "#logs") JOBS_T = setTimeout(loadJobs, 4000);
}
async function toggleJob(id, btn, j) {
  const row = $("#jd" + id);
  if (!row.hidden) { row.hidden = true; btn.textContent = btn.textContent.replace("▾", "▸"); return; }
  const qs = await api(`/api/jobs/${id}/queries`);
  qs.forEach(q => JOB_SQL[q.id] = q.sql);
  row.querySelector("td").innerHTML = `<div class="jq">${qs.length ? "" : `<p class="muted">${j.status === "queued" ? "还没开始跑" : "这个任务没有 SQL 记录（本功能上线前跑完的任务不追溯）"}</p>`}${qs.map((q, i) => `<div class="jq-item">
      <div class="jq-h"><b>${i + 1}. ${esc(q.label)}</b>
        <span class="muted">KG 任务号 <code>${esc(q.kg_task_id || "同步返回，无任务号")}</code> · ${q.error ? `<span class="warn-t">失败</span>` : `${fmt(q.rows)} 行`} · ${q.seconds} 秒</span>
        <button type="button" class="btn copy" data-sql="${q.id}">复制 SQL</button></div>
      ${q.error ? `<div class="notice crit"><b>错误</b>${esc(q.error)}</div>` : ""}
      <details><summary class="muted">查看 SQL</summary><pre class="sql">${esc(q.sql)}</pre></details></div>`).join("")}
    <details ${qs.length ? "" : "open"}><summary class="muted">运行日志</summary><pre>${esc(j.log || "")}</pre></details></div>`;
  $$("[data-sql]", row).forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText(JOB_SQL[b.dataset.sql]); flash(b, "已复制", true); } catch (_) { flash(b, "复制失败", false); } });
  row.hidden = false; btn.textContent = btn.textContent.replace("▸", "▾");
}

// ─────────── 热词运营 ───────────
// 北极星 = 中间页热词 CTR + 结果页 CTR（UV）；其余都是观测（次数口径）
// HW.mks：选中的国家（空 = 四国全看）；多国时另出一条「所选合计」
const HW = { mks: [], trend: "mid", vol: "hot_uv", top: "dev", batch: "", flagOnly: false, sort: null, data: null };
const HW_MK = ["ALL", "RU", "ID", "TR", "BR"];
const hwName = m => m !== "ALL" ? m : HW.data && HW.data.markets.length < 4 ? HW.data.markets.join(" + ") + " 合计" : "四国合计";
const hwColor = m => m === "ALL" ? "var(--ink)" : `var(--s${HW_MK.indexOf(m)})`;
// 只读分享看不到写按钮：提示里「点 X」换成「找看板维护人」
const act = t => META && META.readonly ? "请找看板维护人处理" : t;
const relTxt = (a, b) => a == null || !b ? "" : sgnPct(a / b - 1);
const relCls = (a, b, lower) => { if (a == null || !b) return ""; const r = a / b - 1; return Math.abs(r) < 0.1 ? "" : (r > 0) !== !!lower ? "up" : "down"; };
const pct1 = v => pct(v, 1);

let HOT_SEQ = 0;
async function loadHot() {
  if (!$("#hStart").value) { $("#hEnd").value = toISO(META.yesterday); $("#hStart").value = toISO(20260923); }
  const seq = ++HOT_SEQ;
  try {
    const q = new URLSearchParams({ market: HW.mks.length ? HW.mks.join(",") : "ALL", start: toNum($("#hStart").value), end: toNum($("#hEnd").value) });
    const d = await api("/api/hotword?" + q);
    if (seq !== HOT_SEQ) return;
    HW.data = d;
    renderHot();
  } catch (e) { $("#hNotice").innerHTML = `<div class="notice crit"><b>读取失败</b>${esc(e.message)}</div>`; }
}

function renderHot() {
  const v = HW.data, mks = v.markets.length > 1 ? [...v.markets, "ALL"] : v.markets;
  // 「四国」= 全看；点国家 = 多选切换（再点取消），全选四国等于「四国」
  $("#hMkt").innerHTML = HW_MK.map(m => { const on = m === "ALL" ? !HW.mks.length : HW.mks.includes(m);
    return `<button data-m="${m}" class="${on ? "on" : ""}" aria-pressed="${on}">${m === "ALL" ? "四国" : m}</button>`; }).join("");
  $$("#hMkt button").forEach(b => b.onclick = () => { const m = b.dataset.m;
    HW.mks = m === "ALL" ? [] : HW.mks.includes(m) ? HW.mks.filter(x => x !== m) : HW_MK.filter(x => x === m || HW.mks.includes(x));
    if (HW.mks.length === 4) HW.mks = [];
    HW.batch = ""; loadHot(); });
  const ctx = `${hwName(v.market)} · ${toISO(v.start)} ~ ${toISO(v.end)}`;
  $("#hCtx").textContent = ctx + (v.last ? ` · 聚合表最新到 ${toISO(v.last)}（T+2 滚动）` : "");
  let n = "";
  const c = v.config;
  if (!c.fetched_at) n += `<div class="notice warn"><b>还没有运营配置</b>${act("点「同步运营配置」读飞书表")}；读不到时词级部分为空。</div>`;
  else if (c.error) n += `<div class="notice warn"><b>运营配置同步失败</b>沿用 ${new Date(c.fetched_at * 1000).toLocaleString("zh-CN")} 的快照：${esc(c.error)}</div>`;
  if (!v.last) n += `<div class="notice info"><b>暂无数据</b>${META.readonly ? "数据还没拉取，请找看板维护人。" : "点右上角「从数仓刷新」（全窗口约 5 分钟）。"}拉过一次后每天 ${META.daily_at} 自动续，并重拉最近 3 天。</div>`;
  if (v.job && v.job.status === "cancelled") n += `<div class="notice info"><b>上次刷新已取消</b>已拉回来的日期保留，没拉的日期要再点一次「从数仓刷新」（每日 ${META.daily_at} 也会自动补齐）。</div>`;
  if (v.job && v.job.status === "failed") n += `<div class="notice crit"><b>上次刷新失败</b>${esc((v.job.error || "").slice(0, 160))}</div>`;
  if (v.start > toNum($("#hStart").value)) n += `<div class="notice info"><b>起点已调到 ${toISO(v.start)}</b>0917–0922 聚合表整表还在填，偏低 10–18%，不取。</div>`;
  $("#hNotice").innerHTML = n;
  renderHotVerify(c);

  // 北极星卡：每市场一张，两个 CTR 并排
  const N = v.north;
  const card = m => { const x = N[m] || {};
    return `<div class="tile hw-nc${m === "ALL" ? " hero" : ""}"><div class="k">${hwName(m)}<span class="muted"> · ${x.days || 0} 天</span></div>
      <div class="hw-pair"><div><div class="v">${pct(x.mid_ctr)}</div><div class="s">中间页热词 CTR</div></div>
        <div><div class="v">${pct1(x.res_ctr)}</div><div class="s">结果页 CTR <span class="${relCls(x.res_ctr, x.act_ctr)}">${relTxt(x.res_ctr, x.act_ctr)}</span></div></div></div>
      <div class="s">日均 点热词 ${fmt(x.hot_uv)} / 中间页曝光 ${fmt(x.mid_uv)} 台；主动搜索结果页 CTR ${pct1(x.act_ctr)}</div></div>`; };
  $("#hNorth").innerHTML = mks.slice().sort((a, b) => (a === "ALL" ? -1 : b === "ALL" ? 1 : 0)).map(card).join("");
  const nh = ["市场", "天数", "中间页热词 CTR", "点热词设备（日均）", "中间页曝光设备（日均）", "结果页 CTR（热词）", "结果页 CTR（主动搜索）", "热词相对主动", "点换一批设备（日均）"];
  COPY.hNorth = () => ({ caption: ctx + " · 热词北极星（UV）", header: nh,
    rows: mks.map(m => { const x = N[m] || {}; return [hwName(m), x.days, pct(x.mid_ctr), fmt(x.hot_uv), fmt(x.mid_uv), pct1(x.res_ctr), pct1(x.act_ctr), relTxt(x.res_ctr, x.act_ctr), fmt(x.shuffle_uv)]; }) });

  // 结论
  const LV = { good: "结论", bad: "问题", mid: "待看", info: "提示" };
  $("#hConc").innerHTML = v.conclusions.length ? `<ul class="hw-conc">${v.conclusions.map(x =>
    `<li class="${x.lv}"><span class="tag ${x.lv === "good" ? "up" : x.lv === "bad" ? "down" : ""}">${x.m ? esc(x.m) + " · " : ""}${LV[x.lv]}</span><b>${esc(x.t)}</b><span class="muted">${esc(x.d)}</span></li>`).join("")}</ul>` : `<p class="muted">数据不够，出不了结论。</p>`;
  const R = v.rule;
  $("#hRule").textContent = `规则：结果页 CTR 与 3s 转化率都不低于主动搜索 ${R.gap * 100}% 以上 → 值得继续配；都低 ${R.gap * 100}% 以上 → 查选词和供给。` +
    `10+ 位搜索占比 < ${R.tail_share * 100}% → 每批 9 个够。问题词：「3s 转化低」= 搜索 ≥ ${R.low_eff_min} 次且 3s 转化率（3s 有效播放 ÷ 搜索次数）低于该市场主动搜索 ${Math.round((1 - R.low_eff) * 100)}% 以上；` +
    `在线 ≥ ${R.cold_days} 天且日均搜索 < ${R.cold_daily}；下线后残留搜索 ≥ ${R.residual}。`;

  // 逐日北极星
  $$("#hTrendSeg button").forEach(b => { b.classList.toggle("on", b.dataset.k === HW.trend); b.onclick = () => { HW.trend = b.dataset.k; renderHot(); }; });
  const key = HW.trend === "mid" ? "mid_ctr" : "res_ctr";
  lineChart($("#hTrend"), v.days, mks.map(m => ({ name: hwName(m), color: hwColor(m), pts: (v.series[m] || []).map(x => x ? x[key] : null) })),
    v => pct(v, HW.trend === "mid" ? 2 : 1), true, null,
    { key: `hTrend:${v.market}:${HW.trend}`, title: `${ctx} · ${HW.trend === "mid" ? "中间页热词 CTR" : "结果页 CTR"}（UV）逐日` });

  // 消费量逐日：CTR 是比率、波动小，量看得更直观
  const VOL = { hot_uv: ["点热词设备数", "UV，与北极星同源"], search: ["热词搜索次数", "次数，词级消费表"], p3: ["热词带来的 3s 有效播放", "次数，词级消费表"] };
  $$("#hVolSeg button").forEach(b => { b.classList.toggle("on", b.dataset.k === HW.vol); b.onclick = () => { HW.vol = b.dataset.k; renderHot(); }; });
  // 多国时合计线量级是单国的几倍，会把单国压平：合计默认关掉，点图例打开
  const vk = `hVol:${v.market}:${HW.vol}`;
  if (mks.length > 1 && !HIDE[vk]) HIDE[vk] = new Set([hwName("ALL")]);
  lineChart($("#hVol"), v.days, mks.map(m => ({ name: hwName(m), color: hwColor(m), pts: (v.series[m] || []).map(x => x ? x[HW.vol] : null) })),
    fmtShort, false, null, { key: vk, title: `${ctx} · ${VOL[HW.vol][0]}（${VOL[HW.vol][1]}）逐日` });
  const vh = ["日期", ...mks.map(hwName)], vrows = v.days.map((d, i) => [toISO(d), ...mks.map(m => { const x = (v.series[m] || [])[i]; return x ? fmt(x[HW.vol]) : "—"; })]);
  COPY.hVol = () => ({ caption: `${ctx} · ${VOL[HW.vol][0]}逐日`, header: vh, rows: vrows });

  // 首屏前三位逐日：只在单个国家时有（位置 = 运营表权重；换批日同一位置两个词合并）
  const TOP = { dev: ["点击设备数", fmtShort, "搜这个词的设备数（词 × 天去重）"], mid_rate: ["中间页点击率", x => pct(x, 2), "搜这个词的设备 ÷ 中间页曝光设备"],
    p3: ["3s 有效播放", fmtShort, "次数"], ctr: ["结果页卡片 CTR", x => pct(x, 1), "卡片点击次数 ÷ 卡片曝光次数（这项只有次数口径）"] };
  $("#hTopCard").hidden = !v.top;
  if (v.top) {
    $$("#hTopSeg button").forEach(b => { b.classList.toggle("on", b.dataset.k === HW.top); b.onclick = () => { HW.top = b.dataset.k; renderHot(); }; });
    const [tn, tf, tdesc] = TOP[HW.top], P = [1, 2, 3];
    const words = i => P.map(p => { const x = v.top[p][i]; return `第 ${p} 位：${x ? esc(x.words.join(" / ")) : "—"}`; }).join("<br>");
    lineChart($("#hTop"), v.days, P.map(p => ({ name: `第 ${p} 位`, color: `var(--s${p})`, pts: v.top[p].map(x => x ? x[HW.top] : null) })),
      tf, HW.top === "mid_rate" || HW.top === "ctr", i => `<b>${toISO(v.days[i])}</b><br><span class="muted">${words(i)}</span>`,
      { key: `hTop:${v.market}:${HW.top}`, title: `${ctx} · 首屏前三位 · ${tn}逐日` });
    $("#hTopFoot").textContent = `${tn} = ${tdesc}。位置按运营表的权重算（数仓点击事件没有位置字段）；换批日同一位置有新旧两个词，数字合并，悬停可看当天是哪个词。`;
    const th2 = ["日期", ...P.flatMap(p => [`第 ${p} 位 词`, `第 ${p} 位 ${tn}`])];
    const trows2 = v.days.map((d, i) => [toISO(d), ...P.flatMap(p => { const x = v.top[p][i]; return x ? [x.words.join(" / "), tf(x[HW.top])] : ["—", "—"]; })]);
    COPY.hTop = () => ({ caption: `${ctx} · 首屏前三位 · ${tn}逐日`, header: th2, rows: trows2 });
  }

  // 热词 vs 主动搜索
  const O = v.obs, oh = ["市场", "热词搜索次数", "日均", "占全部搜索", "3s 转化率 热词", "主动搜索", "卡片 CTR 热词", "主动搜索", "无结果率 热词", "主动搜索"];
  const orows = mks.map(m => { const o = O[m] || {};
    return [hwName(m), fmt(o.hot_search), fmt(o.daily_search), pct1(o.share),
      { v: pct1(o.eff_hot), cls: relCls(o.eff_hot, o.eff_act) }, pct1(o.eff_act),
      { v: pct(o.ctr_hot), cls: relCls(o.ctr_hot, o.ctr_act) }, pct(o.ctr_act),
      { v: pct(o.nr_hot), cls: relCls(o.nr_hot, o.nr_act, true) }, pct(o.nr_act)]; });
  $("#hObs").innerHTML = tableHTML({ header: oh, rows: orows }, "hw-t");
  COPY.hObs = () => ({ caption: ctx + " · 热词 vs 主动搜索（次数口径）", header: oh, rows: orows });

  // 位置分档
  const th = ["档位", "词数", "搜索次数", "搜索占比", "单词日均搜索", "每千可见设备搜索", "可见设备", "3s 转化率", "卡片 CTR"];
  const trows = v.tiers.map(t => [t.name, t.words, fmt(t.search), pct1(t.share), fmt(t.per_word_day), t.per_k_visible == null ? "—" : t.per_k_visible.toFixed(1), t.visible, pct1(t.eff), pct(t.ctr)]);
  $("#hTier").innerHTML = trows.length ? tableHTML({ header: th, rows: trows }, "hw-t") : `<p class="muted">没有配置词数据。</p>`;
  COPY.hTier = () => ({ caption: ctx + " · 按位置分档", header: th, rows: trows });

  // 词级汇总
  const bs = [...new Set(v.words.map(w => w.batch))];
  if (HW.batch && !bs.includes(HW.batch)) HW.batch = "";
  $("#hBatch").innerHTML = `<option value="">全部批次</option>` + bs.map(b => `<option${b === HW.batch ? " selected" : ""}>${esc(b)}</option>`).join("");
  $("#hBatch").onchange = ev => { HW.batch = ev.target.value; renderHotWords(ctx); if ($("#tab-hot .hw-daily").open) renderHotDaily(ctx); };
  $("#hFlagOnly").checked = HW.flagOnly;
  $("#hFlagOnly").onchange = ev => { HW.flagOnly = ev.target.checked; renderHotWords(ctx); };
  renderHotWords(ctx);

  // 逐日明细（展开时才画，行数多）
  const det = $("#tab-hot .hw-daily");
  det.ontoggle = () => det.open && renderHotDaily(ctx);
  if (det.open) renderHotDaily(ctx);
}

const HW_COLS = [
  ["batch", "批次", w => w.batch], ["market", "市场", w => w.market], ["weight", "权重", w => w.weight ?? "—"],
  ["word", "原词", w => w.word], ["zh", "中文", w => w.zh], ["live_days", "在线天数", w => w.live_days],
  ["search", "搜索次数", w => fmt(w.search)], ["daily_search", "日均搜索", w => fmt(w.daily_search)],
  ["play", "播放次数", w => fmt(w.play)], ["p3", "3s 有效播放", w => fmt(w.p3)], ["nr", "无结果次数", w => fmt(w.nr)],
  ["ce", "卡片曝光", w => fmt(w.ce)], ["cc", "卡片点击", w => fmt(w.cc)], ["de", "详情曝光", w => fmt(w.de)],
  ["eff", "3s 转化率", w => pct1(w.eff)], ["nr_rate", "无结果率", w => pct(w.nr_rate)], ["ctr", "卡片 CTR", w => pct(w.ctr)],
  ["residual", "下线后残留搜索", w => w.off && HW.data && w.off < HW.data.end ? fmt(w.residual) : "在线中"], ["flags", "标记", w => w.flags.join("、")],
];
function renderHotWords(ctx) {
  let ws = HW.data.words.filter(w => (!HW.batch || w.batch === HW.batch) && (!HW.flagOnly || w.flags.length));
  const sv = (w, k) => k === "flags" ? w.flags.length : w[k] ?? -Infinity;
  if (HW.sort) { const [k, dir] = HW.sort; ws = [...ws].sort((a, b) => { const x = sv(a, k), y = sv(b, k);
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir; }); }
  const head = HW_COLS.map(([k, t]) => `<th data-k="${k}" class="sortable" aria-sort="${HW.sort && HW.sort[0] === k ? (HW.sort[1] > 0 ? "ascending" : "descending") : "none"}">${t}${HW.sort && HW.sort[0] === k ? (HW.sort[1] > 0 ? " ▲" : " ▼") : ""}</th>`).join("");
  $("#hWords").innerHTML = ws.length ? `<div class="scroll hw-scroll"><table class="hw-t words"><thead><tr>${head}</tr></thead><tbody>` +
    ws.map(w => `<tr class="${w.flags.length ? "bad" : ""}">${HW_COLS.map(([k, , f]) => `<td class="${k === "flags" ? "flag" : ""}">${esc(f(w))}</td>`).join("")}</tr>`).join("") +
    `</tbody></table></div><p class="muted">${ws.length} 个词</p>` : `<p class="muted">没有符合条件的词。</p>`;
  $$("#hWords th[data-k]").forEach(th => th.onclick = () => { const k = th.dataset.k;
    HW.sort = HW.sort && HW.sort[0] === k ? [k, -HW.sort[1]] : [k, -1]; renderHotWords(ctx); });
  COPY.hWords = () => ({ caption: ctx + " · 词级汇总" + (HW.batch ? "（" + HW.batch + "）" : ""), header: HW_COLS.map(c => c[1]), rows: ws.map(w => HW_COLS.map(([, , f]) => f(w))) });
}

function renderHotDaily(ctx) {
  const rs = HW.data.word_daily.filter(r => !HW.batch || r.batch === HW.batch);
  const h = ["日期", "市场", "批次", "权重", "原词", "热词搜索次数", "播放次数", "3s 有效播放", "播放设备数", "无结果次数", "卡片曝光", "卡片点击", "详情曝光", "3s 转化率", "无结果率"];
  const rows = rs.map(r => [toISO(r.date), r.market, r.batch, r.weight ?? "—", r.word, fmt(r.search), fmt(r.play), fmt(r.p3), fmt(r.play_dev), fmt(r.nr), fmt(r.ce), fmt(r.cc), fmt(r.de), pct1(r.eff), pct(r.nr_rate)]);
  $("#hDaily").innerHTML = rows.length ? tableHTML({ header: h, rows }, "hw-t hwd").replace('<div class="scroll">', '<div class="scroll hw-scroll">') : `<p class="muted">无数据</p>`;
  COPY.hDaily = () => ({ caption: ctx + " · 词级逐日" + (HW.batch ? "（" + HW.batch + "）" : ""), header: h, rows });
}

function renderHotVerify(c) {
  const t = c.fetched_at ? new Date(c.fetched_at * 1000).toLocaleString("zh-CN", { hour12: false }) : null;
  const rep = c.report || [], warn = rep.filter(x => x.lv === "warn").length;
  $("#hVerifySum").textContent = !t ? "还没同步过" :
    `${t} 同步 · ${c.rows} 个词、${c.n_batches} 个批次 · ` + (warn ? `${warn} 项需要处理` : "全部通过");
  $("#hVerify").classList.toggle("has-warn", warn > 0);
  const LV = { ok: "通过", warn: "需处理", info: "确认" };
  $("#hVerifyBody").innerHTML = `<p class="foot">读的是 ${c.tabs.map(x => `<a href="${esc(x.url)}" target="_blank" rel="noopener">「${esc(x.name)}」</a>`).join("、")}。` +
    `看板按这张表认词、批次、在线日期和位置；核验 = 飞书表 ↔ 看板实际采用的配置 ↔ 数仓里用户实际搜的词，三方对一遍。每天 ${META.daily_at} 自动同步一次。</p>` +
    (rep.length ? `<ul class="hw-check">${rep.map(x => `<li class="${x.lv}"><span class="tag ${x.lv === "ok" ? "up" : x.lv === "warn" ? "down" : ""}">${LV[x.lv]}</span><b>${esc(x.t)}</b>` +
      (x.items.length ? `<details><summary class="muted">展开 ${x.n} 条${x.n > x.items.length ? `（显示前 ${x.items.length} 条）` : ""}</summary><ul>${x.items.map(i => `<li>${esc(i)}</li>`).join("")}</ul></details>` : "") + `</li>`).join("")}</ul>`
      : `<p class="muted">${act("点「同步运营配置」生成核验结果")}。</p>`);
}

async function refreshHot() {
  const b = $("#hRefresh"); b.disabled = true;
  try {
    const j = await api("/api/hotword/refresh", { method: "POST", body: { start: toNum($("#hStart").value), end: toNum($("#hEnd").value) } });
    $("#hNotice").innerHTML = `<div class="notice info"><b>已提交 #${j.job_id}</b>正在同步运营配置并从数仓拉数，完成后自动刷新…</div>`;
    await waitJob(j.job_id);
    loadHot();
  } catch (e) { $("#hNotice").innerHTML = `<div class="notice crit"><b>刷新失败</b>${esc(e.message)}</div>`; }
  finally { b.disabled = false; }
}

async function syncHotConfig() {
  const b = $("#hSync"); b.disabled = true;
  try {
    const r = await api("/api/hotword/refresh", { method: "POST", body: { config_only: true } });
    flash(b, r.ok ? "已同步，核验结果见下方" : "同步失败", r.ok);
    await loadHot();
    $("#hVerify").open = true;
    $("#hVerify").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { flash(b, "同步失败", false); }
  finally { b.disabled = false; }
}

// ─────────── 实验观测：排序实验按「轮次 × 机房」汇总（只读，数据 = 实验回收全窗口累计）───────────
const OBS = { k: {} };                // OBS.k[实验 id] = 折线图当前指标
const OB_DEC = { 推全: "go", 未采纳: "no", 已回退: "back", 已关闭: "back", 进行中: "run", 仅作对照: "no" };
async function loadObs() {
  const box = $("#obsBody");
  try { OBS.d = await api("/api/observe"); } catch (e) { box.innerHTML = `<div class="notice crit"><b>加载失败</b>${esc(e.message)}</div>`; return; }
  renderObs();
}
const obUnit = k => (((OBS.d.catalog || {})[k] || {}).unit || (OBS.d.metrics.find(m => m.key === k) || {}).unit || "pct");
const obVal = (k, v) => v == null ? "—" : obUnit(k) !== "num" ? pct(v) : v.toFixed(2);
// 相对值：百分比指标 Δpp，人均 VV Δ%（同实验回收读数表）
const obDelta = (k, c) => !c || c.diff == null ? "—" : obUnit(k) !== "num" ? sgn(c.diff * 100, 2) + "pp" : c.rel == null ? "—" : sgn(c.rel * 100, 1) + "%";
const obArrow = c => !c || c.diff == null ? "" : c.sig === "up" ? "▲" : c.sig === "down" ? "▼" : c.diff > 0 ? "↑" : c.diff < 0 ? "↓" : "→";
// 留存护栏（读实验回收的长期价值）：判止损 / 显著变好 / 显著变差未过序贯阈值 / 未见变化 / 未成熟
const obRet = r => {
  const it = r.items || [], one = x => `${x.name} ${x.delta}`;
  return r.state === "immature" ? "未成熟（第 7 天出）"
    : r.state === "stop" ? `⚠ 判止损（${it.map(one).join("，")}）`
    : r.state === "up" ? it.map(x => `${x.name} ▲ ${x.delta}`).join("，")
    : r.state === "down" ? `${it.map(x => `${x.name} ▼ ${x.delta}`).join("，")}（未过多次看数阈值，不判止损）`
    : `未见变化（LT7 ${it[0] ? it[0].delta : "—"}${r.mde ? `，MDE ${r.mde}` : ""}）`;
};
const obGroupName = (g, id) => g && g !== id && g !== "对照" ? `${g} · ${id}` : id;

function obCell(x, g) {
  const ms = x.spec && x.spec.length ? x.spec : OBS.d.metrics;
  const pip = k => { const c = g.metrics[k]; return `<i class="${!c ? "wait" : c.sig || ""}"></i>`; };
  const rows = ms.map(m => {
    const c = g.metrics[m.key];
    if (!c) return `<div class="ob-m"><span class="ob-mn">${esc(m.name)}</span><span class="ob-abs muted">回收中</span><span></span></div>`;
    return `<div class="ob-m"><span class="ob-mn">${esc(m.name)}</span>` +
      `<span class="ob-abs">${obVal(m.key, c.c.value)}<span class="ob-to" aria-label="到">→</span><b>${obVal(m.key, c.t.value)}</b></span>` +
      `<span class="ob-d ${c.sig || "ob-ns"}">${obArrow(c)} ${obDelta(m.key, c)}</span></div>`;
  }).join("");
  const sigText = ms.map(m => { const c = g.metrics[m.key]; return `${m.name}${!c ? "回收中" : c.sig === "up" ? "显著正向" : c.sig === "down" ? "显著负向" : "不显著"}`; }).join("，");
  const r = g.retention;
  const ret = `<div class="ob-m ob-ret"><span class="ob-mn">留存</span>${!r ? `<span class="ob-abs muted">回收中</span><span></span>` :
    `<span class="ob-d ${r.state === "stop" ? "down" : r.state === "up" ? "up" : "ob-ns"}">${esc(obRet(r))}</span>` +
    `<span class="ob-abs muted">${r.cut ? `数据到 ${toISO(r.cut).slice(5)}` : ""}</span>`}</div>`;
  return `<div class="ob-g">
    <div class="ob-gh"><span class="ob-sig" role="img" aria-label="${esc(sigText)}" title="${esc(sigText)}">${ms.map(m => pip(m.key)).join("")}</span>
      <span class="ob-gl">${esc(g.label)} <span class="muted">${esc(g.id)}</span></span>
      <span class="ob-dec ${OB_DEC[g.decision] || ""}">${esc(g.decision)}</span></div>
    ${g.note || g.hint ? `<p class="ob-note">${g.note ? esc(g.note) : ""}${g.note && g.hint ? "；" : ""}${g.hint ? `<span class="ob-hint">${esc(g.hint)}</span>` : ""}</p>` : ""}
    ${x.state === "missing" || x.state === "waiting" ? "" : rows + ret}</div>`;
}

function obExp(x, col, ri, xi) {
  const id = `obc_${ri}_${col.market}_${xi}`;
  // 分段实验的旧段（x.old）：标题前灰色标签 + 一句说明，整块弱化，不算本轮决策
  const head = `<div class="ob-xh"><h3>${x.tag ? `<span class="ob-tag">${esc(x.tag)}</span>` : ""}${esc(x.name)}</h3>${x.eid ? `<a href="#exp" class="ob-open" data-eid="${x.eid}">在具体实验回收中打开</a>` : ""}</div>` +
    (x.seg_note ? `<p class="ob-note ob-segnote">${esc(x.seg_note)}</p>` : "") +
    `<div class="ob-meta">对照 ${esc(obGroupName(x.ctrl.label, x.ctrl.id))}${x.window ? ` · ${toISO(x.window[0]).slice(5)} ~ ${toISO(x.window[1]).slice(5)}` : ""}</div>`;
  const cls = `ob-x${x.old ? " ob-old" : ""}`;
  if (x.state === "missing")
    return `<article class="${cls}">${head}${x.groups.map(g => obCell(x, g)).join("")}<p class="muted ob-empty">看板里还没有这个实验的回收</p></article>`;
  // 已建、开窗日还没到：不显示「回收中」，每日调度在有数后自动开跑
  if (x.state === "waiting")
    return `<article class="${cls}">${head}<p class="ob-wait">等待开窗（${toISO(x.opens).slice(5)} 起有数）</p>${x.groups.map(g => obCell(x, g)).join("")}</article>`;
  const ms = x.spec && x.spec.length ? x.spec : OBS.d.metrics;
  const k = OBS.k[x.eid] || (ms[0] && ms[0].key) || "ctr_ui";
  return `<article class="${cls}">${head}${x.groups.map(g => obCell(x, g)).join("")}
    <div class="ob-ch"><div class="seg small" data-obseg="${id}" role="group" aria-label="图表指标">${ms.map(m =>
      `<button type="button" data-k="${m.key}" class="${m.key === k ? "on" : ""}" aria-pressed="${m.key === k}">${esc(m.name)}</button>`).join("")}<button type="button" data-k="pos" class="${k === "pos" ? "on" : ""}" aria-pressed="${k === "pos"}">位置 CTR</button></div>
    <div class="chart" id="${id}"></div></div></article>`;
}

function obChart(el, x, col, k) {
  if (k === "pos") { obPosChart(el, x, col); return; }
  const ms = x.spec && x.spec.length ? x.spec : OBS.d.metrics;
  const d = x.daily[k] || { state: "none" }, m = ms.find(z => z.key === k) || {};
  if (d.state !== "ok") { el.innerHTML = `<p class="muted ob-empty">${d.state === "pending" ? "逐日数据回收中" : "无逐日数据"}</p>`; return; }
  const ids = [x.ctrl.id, ...x.groups.map(g => g.id)];
  const lab = id => id === x.ctrl.id ? `${obGroupName(x.ctrl.label, id)}（对照）` : obGroupName((x.groups.find(g => g.id === id) || {}).label, id);
  const series = ids.map((id, i) => ({ name: lab(id), color: `var(--s${Math.min(i + 1, 5)})`, pts: d.series[id] || [] }));
  const yfmt = v => v == null ? "—" : obUnit(k) !== "num" ? (v * 100).toFixed(2) + "%" : v.toFixed(2);
  lineChart(el, d.days, series, yfmt, true, null, { h: 190, key: `obs:${x.eid}:${k}`, title: `${col.label} · ${x.name} · ${m.name || k}（逐日）`, note: thinNote(d.thin || []) });
}

// 总览块里的位置 CTR：按需拉一次实验详情（@pos），不把 20 个位置的明细塞进 /api/observe
const POS_CACHE = {};
async function obPosChart(el, x, col) {
  if (!x.eid) { el.innerHTML = `<p class="muted ob-empty">看板里还没有这个实验的回收</p>`; return; }
  el.innerHTML = `<p class="muted ob-empty">位置 CTR 加载中…</p>`;
  try {
    const v = POS_CACHE[x.eid] || (POS_CACHE[x.eid] = await api(`/api/experiments/${x.eid}`));
    if (!v.view.pos) { el.innerHTML = `<p class="muted ob-empty">还没算位置 CTR（日更 / 重跑后补）</p>`; return; }
    posChart(el, v, v.view.pos, "all", `${col.label} · ${x.name} · Top20 位置 CTR（PV，排兜底，全量）`);
  } catch (err) {
    el.innerHTML = `<p class="muted ob-empty">${esc(err.message)}</p>`;
  }
}

// ── 推全观测：同一批不在试点里的国家，推全前后周均对比（AA）。显著 = 7 天对 7 天 |t| ≥ 2.18；不写 t、不写 CI ──
const roWk = w => w ? `${toISO(w[0]).slice(5)} ~ ${toISO(w[1]).slice(5)}` : "—";
const roSig = c => !c ? "—" : c.sig === "up" ? "显著正向" : c.sig === "down" ? "显著负向" : "不显著";
const roDelta = (k, c) => !c ? "—" : obUnit(k) !== "num" ? sgn(c.diff * 100, 2) + "pp" : c.rel == null ? "—" : sgn(c.rel * 100, 1) + "%";
const roArrow = c => !c ? "" : c.sig === "up" ? "▲" : c.sig === "down" ? "▼" : c.diff > 0 ? "↑" : c.diff < 0 ? "↓" : "→";
const roHead = b => b.base && b.obs ? `基线 <b>${roWk(b.base)}</b>${b.base_note ? `（${esc(b.base_note)}）` : ""}<span class="ob-to" aria-label="对比">→</span>观测 <b>${roWk(b.obs)}</b>${b.obs_note ? `（${esc(b.obs_note)}）` : ""}` : "";

function roUnit(u, k0) {
  if (u.state === "na") return `<div class="ob-g ob-textrow"><div class="ob-gh"><span class="ob-gl">${esc(u.label)}</span></div><p class="ob-note">${esc(u.note)}</p></div>`;
  const ms = OBS.d.metrics;
  const rows = ms.map(m => {
    const c = u.metrics && u.metrics[m.key];
    if (!c) return `<div class="ob-m"><span class="ob-mn">${esc(m.name)}</span><span class="ob-abs muted">取数中</span><span></span></div>`;
    return `<div class="ob-m"><span class="ob-mn">${esc(m.name)}</span>` +
      `<span class="ob-abs">${obVal(m.key, c.b)}<span class="ob-to" aria-label="到">→</span><b>${obVal(m.key, c.o)}</b></span>` +
      `<span class="ob-d ${c.sig || "ob-ns"}">${roArrow(c)} ${roDelta(m.key, c)}</span></div>`;
  }).join("");
  const pip = k => { const c = u.metrics && u.metrics[k]; return `<i class="${!c ? "wait" : c.sig || ""}"></i>`; };
  const sigText = ms.map(m => `${m.name}${roSig(u.metrics && u.metrics[m.key])}`).join("，");
  const ex = u.expose && u.expose[0] != null ? `<span class="ro-n">日均结果页曝光设备 ${fmt(u.expose[0])} → ${fmt(u.expose[1])}</span>` : "";
  return `<div class="ob-g"><div class="ob-gh"><span class="ob-sig" role="img" aria-label="${esc(sigText)}" title="${esc(sigText)}">${ms.map(m => pip(m.key)).join("")}</span>
    <span class="ob-gl">${esc(u.label)}</span></div>${ex}${rows}${u.note ? `<p class="ob-note ro-foot">${esc(u.note)}</p>` : ""}</div>`;
}

function roRows(b) {
  // 复制表格：周均（单元 × 指标）+ 逐日（日期 × 单元，每指标一节）
  const ms = OBS.d.metrics, ok = b.units.filter(u => u.state !== "na");
  const rows = [];
  for (const u of b.units) {
    if (u.state === "na") { rows.push([u.label, "—", "", "", "", u.note]); continue; }
    for (const m of ms) {
      const c = u.metrics && u.metrics[m.key];
      rows.push([u.label, m.name, c ? obVal(m.key, c.b) : "取数中", c ? obVal(m.key, c.o) : "取数中",
        c ? { v: roDelta(m.key, c), cls: c.sig || "" } : "—", roSig(c)]);
    }
  }
  const head = `${b.title} · 推全观测（基线 ${roWk(b.base)} vs 观测 ${roWk(b.obs)}，周均）`;
  const secs = [{ caption: head, header: ["单元", "指标", "基线", "观测周", "Δ", "显著性（|t| ≥ 2.18）"], rows }];
  const days = (ok.find(u => u.days) || {}).days || [];
  for (const m of ms) {
    const dr = days.map((d, i) => [toISO(d), ...ok.map(u => obVal(m.key, ((u.daily || {})[m.key] || [])[i]))]);
    if (dr.length) secs.push({ caption: `${b.title} · 推全观测逐日 · ${m.name}`, header: ["日期", ...ok.map(u => u.label)], rows: dr });
  }
  return { sections: secs };
}

function roChart(el, b, k) {
  const ok = b.units.filter(u => u.state !== "na" && u.daily), m = OBS.d.metrics.find(z => z.key === k);
  if (!ok.length) { el.innerHTML = `<p class="muted ob-empty">逐日数据取数中</p>`; return; }
  const days = ok[0].days;
  const series = ok.map((u, i) => ({ name: u.label, color: `var(--s${Math.min(i + 1, 5)})`, pts: u.daily[k] }));
  const yfmt = v => v == null ? "—" : obUnit(k) !== "num" ? (v * 100).toFixed(2) + "%" : v.toFixed(2);
  lineChart(el, days, series, yfmt, true, i => `<b>${toISO(days[i])}</b> <span class="muted">${i < 7 ? "基线" : "观测周"}</span>`,
    { h: 220, split: 7, key: `ro:${b.id}:${k}`, title: `${b.title} · 推全观测 · ${m.name}（逐日，左 = 基线 ${roWk(b.base)}，右 = 观测 ${roWk(b.obs)}）` });
}

// 单元卡片按机房分行：同机房几张一行（第一轮 SGP 拆 BR / TR / 其余），只有一张的相邻机房并成一行；每行等分铺满
function roGrid(units, card) {
  const groups = [];
  for (const u of units) { const g = groups.find(x => x[0].market === u.market); g ? g.push(u) : groups.push([u]); }
  const rows = [];
  for (const g of groups) { const last = rows[rows.length - 1]; if (g.length === 1 && last && last.solo) last.us.push(g[0]); else rows.push({ solo: g.length === 1, us: [...g] }); }
  return `<div class="ro-units">${rows.map(r => `<div class="ro-row">${r.us.map(card).join("")}</div>`).join("")}</div>`;
}

// 一块推全观测：每轮下面一块（inRound），页面顶部全局累计一块
function roBlock(b, inRound) {
  if (!b) return "";
  const h = inRound ? `<h3 class="ro-t">推全观测</h3>` : `<h2 class="ro-t" id="ro_global">${esc(b.title)}</h2>`;
  if (b.state === "not_rolled") return `<div class="ro"><div class="ro-h">${h}<span class="ob-wait ro-st">未推全</span></div></div>`;
  if (b.state === "no_base") return `<div class="ro"><div class="ro-h">${h}<span class="ob-wait ro-st">上一轮未推全，没有基线周</span></div></div>`;
  const cid = `roc_${b.id}`, k = OBS.k[cid] || "ctr_ui";
  COPY["ro_" + b.id] = () => roRows(b);
  const top = `<div class="ro-h">${h}<span class="ro-wk">${roHead(b)}</span>${b.state !== "waiting" ? `<button type="button" class="btn copy" data-copy="ro_${b.id}">复制表格</button>` : ""}</div>` +
    (b.note ? `<p class="ob-cfg">${esc(b.note)}</p>` : "");
  if (b.state === "waiting")
    return `<div class="ro">${top}<p class="ob-wait ro-st">等待推全后整周（${toISO(b.opens).slice(5)} 起有数）</p>
      ${roGrid(b.units, u => u.state === "na" ? roUnit(u) : `<div class="ob-g ob-textrow"><div class="ob-gh"><span class="ob-gl">${esc(u.label)}</span></div><p class="ob-note">观测周 ${roWk(b.obs)} 过完后出数</p></div>`)}</div>`;
  return `<div class="ro">${top}${b.state === "pending" ? `<p class="ob-wait ro-st">部分单元取数中（每日 ${esc(META.daily_at)} 调度自动补齐）</p>` : ""}
    ${roGrid(b.units, u => roUnit(u))}
    <div class="ob-ch"><div class="seg small" data-roseg="${cid}" role="group" aria-label="逐日走势指标">${OBS.d.metrics.map(m =>
      `<button type="button" data-k="${m.key}" class="${m.key === k ? "on" : ""}" aria-pressed="${m.key === k}">${esc(m.name)}</button>`).join("")}</div>
    <div class="chart" id="${cid}"></div></div></div>`;
}

function roWire(b) {
  const el = b && $(`#roc_${b.id}`);
  if (!el) return;
  roChart(el, b, OBS.k[el.id] || "ctr_ui");
  $$(`[data-roseg="${el.id}"] button`).forEach(x => x.onclick = () => {
    OBS.k[el.id] = x.dataset.k;
    $$(`[data-roseg="${el.id}"] button`).forEach(y => { const on = y === x; y.classList.toggle("on", on); y.setAttribute("aria-pressed", on); });
    roChart(el, b, x.dataset.k);
  });
}

const OB_RAIL = [["launch", "上线"], ["full", "满窗"], ["judge", "判读"], ["rollout", "推全"]];
function obRows(r) {
  // 复制表格：每组每指标一行
  const rows = [];
  for (const col of r.markets) {
    for (const x of col.exps) for (const g of x.groups) for (const m of (x.spec && x.spec.length ? x.spec : OBS.d.metrics)) {
      const c = g.metrics[m.key];
      rows.push([col.label, (x.tag ? `［${x.tag}］` : "") + x.name, obGroupName(g.label, g.id), g.decision + (g.hint ? `（${g.hint}）` : ""), m.name,
        ...(w => [c ? obVal(m.key, c.c.value) : w, c ? obVal(m.key, c.t.value) : w])(x.state === "waiting" ? "等待开窗" : "回收中"),
        c ? { v: obDelta(m.key, c), cls: c.sig || "" } : "—", c ? (c.sig === "up" ? "显著正向" : c.sig === "down" ? "显著负向" : "不显著") : "—",
        x.state === "waiting" ? "等待开窗" : !g.retention ? "回收中" : obRet(g.retention) + (g.retention.cut ? `（数据到 ${toISO(g.retention.cut).slice(5)}）` : "")]);
    }
    for (const n of col.notes) rows.push([col.label, n.split("｜")[0], "", "推全" + (n.split("｜")[1] ? `（${n.split("｜")[1]}）` : ""), "", "", "", "", "", ""]);
  }
  return { caption: `搜索排序实验 · ${r.name}（上线 ${toISO(r.launch)}，全窗口累计）`, header: ["机房", "实验", "实验组", "文档决策", "指标", "对照", "实验", "Δ", "显著性", "留存"], rows };
}

function renderObs() {
  const d = OBS.d, today = addDays(META.yesterday, 1);
  const html = d.rounds.map((r, ri) => {
    const rail = OB_RAIL.map(([k, l]) => `<li class="${r[k] && r[k] <= today ? "done" : ""}"><span>${l}</span><b>${r[k] ? toISO(r[k]).slice(5) : "待定"}</b></li>`).join("");
    const cols = r.markets.map(col => {
      let body = col.exps.map((x, xi) => obExp(x, col, ri, xi)).join("");
      // 「名称｜说明」：没单独开实验的推全项，说明写依据哪组读数
      body += col.notes.map(n => { const [t, why] = n.split("｜");
        return `<div class="ob-g ob-textrow"><div class="ob-gh"><span class="ob-gl">${esc(t)}</span><span class="ob-dec go">推全</span></div><p class="ob-note">${esc(why || "无对应实验，无数据")}</p></div>`; }).join("");
      if (col.plan.length && !col.exps.length)
        body += `<div class="ob-plan"><ul>${col.plan.map(p => `<li>${esc(p)}</li>`).join("")}</ul><p class="muted">等待分桶号，建好回收实验后自动出现</p></div>`;
      return `<div class="ob-col"><div class="ob-colh"><h3>${esc(col.label)}</h3></div><p class="ob-cfg">${esc(col.config)}</p>${body}</div>`;
    }).join("");
    COPY["obs_r" + r.no] = () => obRows(r);
    return `<section class="card ob-round" aria-labelledby="obr${r.no}">
      <div class="card-h ob-rh"><div class="ob-rt"><h2 id="obr${r.no}">${esc(r.name)}</h2><ol class="ob-rail">${rail}</ol>${r.note ? `<span class="ob-rnote">${esc(r.note)}</span>` : ""}</div>
        <button type="button" class="btn copy" data-copy="obs_r${r.no}">复制表格</button></div>
      <div class="ob-cols">${cols}</div>${roBlock(r.rollout_obs, true)}</section>`;
  }).join("");
  const ro = d.rollout || {}, g = ro.global;
  const glob = g ? `<section class="card ob-round ro-global" aria-labelledby="ro_global">${roBlock(g, false)}
    <p class="foot ro-cav">口径：同一批不在试点里的国家，周均 = 7 天日值平均，Δ = 周均差，显著 = 7 天对 7 天两样本 |t| ≥ 2.18；剔除当周带 holdback的设备。${esc(ro.caveat || "")}</p></section>` : "";
  const roSecs = b => b && b.state !== "waiting" && COPY["ro_" + b.id] ? COPY["ro_" + b.id]().sections : [];
  COPY.obsAll = () => ({ sections: [...roSecs(g), ...d.rounds.flatMap(r => [obRows(r), ...roSecs(r.rollout_obs)]).filter(x => x.rows.length)] });
  $("#obsBody").innerHTML = glob + html;
  roWire(g);
  d.rounds.forEach(r => roWire(r.rollout_obs));
  d.rounds.forEach((r, ri) => r.markets.forEach(col => col.exps.forEach((x, xi) => {
    const el = $(`#obc_${ri}_${col.market}_${xi}`);
    if (!el) return;
    obChart(el, x, col, OBS.k[x.eid] || x.primary || (x.spec && x.spec[0] && x.spec[0].key) || "ctr_ui");
    $$(`[data-obseg="${el.id}"] button`).forEach(b => b.onclick = () => {
      OBS.k[x.eid] = b.dataset.k;
      $$(`[data-obseg="${el.id}"] button`).forEach(y => { const on = y === b; y.classList.toggle("on", on); y.setAttribute("aria-pressed", on); });
      obChart(el, x, col, b.dataset.k);
    });
  })));
  $$("#obsBody .ob-open").forEach(a => a.onclick = () => { CUR_EXP = +a.dataset.eid; });
  renderObsSync(d.sync || {});
}

// 实验配置核验：规划文档 ↔ 看板回收实验 ↔ 代码里的决策标签（同热词「运营配置核验」）
function renderObsSync(c) {
  const t = c.fetched_at ? new Date(c.fetched_at * 1000).toLocaleString("zh-CN", { hour12: false }) : null;
  const rep = c.report || [], warn = rep.filter(x => x.lv === "warn").length;
  let n = "";
  if (c.error) n += `<div class="notice warn"><b>实验配置同步失败</b>${esc(c.error)}${t ? `；沿用 ${esc(t)} 的快照` : ""}。${act("可再点「同步实验配置」重试")}</div>`;
  $("#oNotice").innerHTML = n;
  $("#oVerifySum").textContent = !t ? "还没同步过" : `${t} 同步 · ` + (warn ? `${warn} 项需要处理` : "全部通过");
  $("#oVerify").classList.toggle("has-warn", warn > 0);
  const LV = { ok: "通过", warn: "需处理", info: "确认" };
  const fixes = x => (x.fixes || []).length && !META.readonly ? `<ul class="ob-fix">${x.fixes.map((f, i) =>
    `<li><span><b>${esc(f.name)}</b>（文档「${esc(f.doc)}」）：${esc(f.diffs.join("；"))}</span><button type="button" class="btn" data-ofix="${i}">按文档更新并重跑</button></li>`).join("")}</ul>` :
    (x.dec_fixes || []).length && !META.readonly ? `<ul class="ob-fix">${x.dec_fixes.map((f, i) =>
    `<li><span><b>${esc(f.name)}</b>：看板「${esc(f.now)}」→ 文档「${esc(f.want)}」（推全进度表：${esc(f.src)}）</span><button type="button" class="btn" data-odec="${i}">按文档改</button></li>`).join("")}</ul>` : "";
  $("#oVerifyBody").innerHTML = `<p class="foot">读的是 <a href="${esc(c.doc_url)}" target="_blank" rel="noopener">《搜索结果排序实验规划 v2》</a>的实验进度表、万象分桶表（不含常驻 holdback）和推全进度表。` +
    `文档有、看板没有的回收实验自动建；和看板已有实验对不上的只列出来不改；决策标签和推全进度表对不上的也只列出来，点「按文档改」才改。每天 ${esc(c.daily_at || META.daily_at)} 自动同步一次。</p>` +
    (rep.length ? `<ul class="hw-check">${rep.map(x => `<li class="${x.lv}"><span class="tag ${x.lv === "ok" ? "up" : x.lv === "warn" ? "down" : ""}">${LV[x.lv]}</span><b>${esc(x.t)}</b>` +
      (fixes(x) || (x.items.length ? `<details><summary class="muted">展开 ${x.n} 条${x.n > x.items.length ? `（显示前 ${x.items.length} 条）` : ""}</summary><ul>${x.items.map(i => `<li>${esc(i)}</li>`).join("")}</ul></details>` : "")) + `</li>`).join("")}</ul>`
      : `<p class="muted">${act("点「同步实验配置」生成核验结果")}。</p>`);
  const fx = rep.flatMap(x => x.fixes || []);
  const dfx = rep.flatMap(x => x.dec_fixes || []);
  $$("#oVerifyBody [data-odec]").forEach(b => b.onclick = async () => {
    const f = dfx[+b.dataset.odec];
    if (!confirm(`${f.name}\n决策标签「${f.now}」改成「${f.want}」（只改标签，不重跑数据），确定？`)) return;
    b.disabled = true;
    try {
      await api("/api/observe/decision", { method: "POST", body: { no: f.no, market: f.market, gid: f.gid, want: f.want } });
      flash(b, "已改", true);
      setTimeout(loadObs, 600);
    } catch (e) { flash(b, e.message.slice(0, 40), false); b.disabled = false; }
  });
  $$("#oVerifyBody [data-ofix]").forEach(b => b.onclick = async () => {
    const f = fx[+b.dataset.ofix];
    if (!confirm(`「${f.name}」按文档更新：\n${f.diffs.join("\n")}\n\n会清掉这个实验已有结果并整体重跑，确定？`)) return;
    b.disabled = true;
    try {
      const j = await api(`/api/experiments/${f.eid}`, { method: "PUT", body: f.body });
      flash(b, j.job_id ? `已提交 #${j.job_id}` : j.mode === "full" ? "已更新，等待开窗" : "已更新", true);
      await api("/api/observe/sync", { method: "POST", body: {} }).catch(() => null);
      setTimeout(loadObs, 900);
    } catch (e) { flash(b, e.message.slice(0, 40), false); b.disabled = false; }
  });
}

async function syncObsConfig() {
  const b = $("#oSync"); b.disabled = true;
  try {
    const r = await api("/api/observe/sync", { method: "POST", body: {} });
    flash(b, r.ok ? "已同步，核验结果见下方" : "同步失败", r.ok);
    await loadObs();
    $("#oVerify").open = true;
    $("#oVerify").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { flash(b, "同步失败", false); }
  finally { b.disabled = false; }
}

// ─────────── 启动 ───────────
// 顶部健康提醒：kg 登录态缺失 / 近 24 小时有任务失败（同一任务之后重跑成功就不再提醒）
async function loadHealth() {
  let h;
  try { h = await api("/api/health"); } catch (_) { return; }
  const KIND = { funnel: "漏斗刷新", experiment: "实验回收", exp_followup: "实验补算", exp_quantile: "实验补算", hotword: "热词收益刷新", rollout_obs: "推全观测取数" };
  let out = "";
  if (h.net_down)
    out += `<div class="notice crit"><b>KG 连不上</b>${esc(h.net_down)}。多半是公司 VPN 没连或本机 DNS 断了；看板已暂停提交跑数，恢复后 1 分钟内自动补跑，不用手动重跑。</div>`;
  if (!h.kg_session || h.login_suspect)
    out += `<div class="notice crit"><b>KG 登录态${h.kg_session ? "可能已过期" : "缺失"}</b>数仓取数会失败。在终端运行配置的查询客户端完成登录，再到「任务日志」重跑失败的任务。</div>`;
  const fe = h.failed_exps || [];
  if (fe.length)
    out += `<div class="notice crit"><b>${fe.length} 个实验处于失败状态</b>${fe.map(e => `<a href="#exp" data-exp="${e.id}">${esc(e.name)}</a>（${/取消/.test(e.error) ? "已取消，已停止每日续跑" : e.partial ? "部分补算失败" : "跑数失败，已停止每日续跑"}）`).join("；")}。打开实验点「重跑」；这条提醒在重跑成功前一直显示。</div>`;
  // 手动关掉的失败提醒：记住这批任务号（存本浏览器），之后有新的失败任务会重新出现
  let shut = [];
  try { shut = JSON.parse(localStorage.getItem("hideFails") || "[]"); } catch (_) { }
  const fails = h.fails.filter(f => !shut.includes(f.id));
  if (fails.length)
    out += `<div class="notice warn"><b>近 24 小时 ${fails.length} 个任务失败</b>${fails.slice(0, 3).map(f => `#${f.id} ${KIND[f.kind] || f.kind}：${esc(f.error.replace(/[│█▀▄╭╮╰╯─]+/g, " ").replace(/\s+/g, " ").slice(0, 90))}`).join("；")}${fails.length > 3 ? " 等" : ""}。<a href="#logs">去任务日志</a>
      <button type="button" class="notice-x" id="hideFails" aria-label="关闭这条提醒" title="关闭；之后有新的失败任务会再提醒">×</button></div>`;
  $("#health").innerHTML = out;
  if ($("#hideFails")) $("#hideFails").onclick = () => {
    try { localStorage.setItem("hideFails", JSON.stringify([...new Set([...shut, ...h.fails.map(f => f.id)])].slice(-200))); } catch (_) { }
    loadHealth();
  };
  $$("#health [data-exp]").forEach(a => a.onclick = () => { CUR_EXP = +a.dataset.exp; if (location.hash === "#exp") loadExpList(); });
}

// 搜索业务月度 LTV（只 SG，月度静态数，见 config.SEARCH_LTV）：漏斗页一张表，收益总览顶部一行参照
function renderLtv() {
  const L = META.search_ltv || [], usd = v => "$" + fmt(v), doc = `<a href="${esc(META.search_ltv_doc)}" target="_blank" rel="noopener">飞书《搜索实验指标》附录</a>`;
  const header = ["月份", "人群 / 窗口", "搜索业务月收入", "占 App 流内广告收入", "每搜索用户月 LTV", "搜索播放占比"];
  const rows = L.map(x => [x.month, `${toISO(x.d0).slice(5)} 搜索用户 ${fmt(x.n_coh)} 台 / ${toISO(x.w0).slice(5)} ~ ${toISO(x.w1).slice(5)}`,
    `${usd(x.rev)}（补占位 GAID ≈ ${usd(x.rev_adj)}）`, `${(x.share * 100).toFixed(2)}%（App ${usd(x.app_rev)}）`,
    `${x.ltv} 美丝 / 人（每千人 $${(x.ltv / 100).toFixed(2)}）`, `${(x.svv_share * 100).toFixed(2)}%`]);
  COPY.ltv = () => ({ caption: "SGP 搜索业务月度 LTV", header, rows });
  $("#fLtv").innerHTML = rows.length ? tableHTML({ header, rows }, "") +
    `<p class="foot">搜索业务收入 = 设备日流内 5 个广告位计费收入 × 当日搜索播放占比（搜索播放 from 同漏斗口径），按月加总；每搜索用户月 LTV = 月初 D0 当天有搜索结果页事件的设备，D0+1 ~ D0+30 的分摊收入 ÷ 设备数，不含 D0 当天（当天单日约 ${L.length ? L[L.length - 1].ltv_d0 : "—"} 美丝 / 人）。` +
    `每搜索用户月 LTV 不封顶、被少数高收入设备主导，只看量级，不解读月度涨跌；月收入可看趋势。是归因不是增量；假设搜索流与推荐流广告密度相同。只有 SGP 能算（RU / EU 的 client_info 不是 GAID）。口径见 ${doc}。</p>` : `<p class="muted">暂无</p>`;
  const x = L[L.length - 1];
  $("#oLtv").innerHTML = x ? `量级参照：SGP 搜索业务月收入 ≈ ${usd(x.rev)}（${x.month}，占 App 流内广告收入 ${(x.share * 100).toFixed(2)}%），每搜索用户月 LTV ${x.ltv} 美丝。只作量级参照，不换算实验收益；口径见 ${doc}。` : "";
}

(async function init() {
  META = await api("/api/meta");
  renderLtv();
  if (!META.readonly) { loadHealth(); setInterval(loadHealth, 60000); }   // 健康提醒只给维护人，只读分享不轮询
  // 局域网访问 = 只读：body.ro 隐藏所有写操作（服务端同样拒绝写请求，这里只是不让人点到报错）
  document.body.classList.toggle("ro", !!META.readonly);
  $("#sideFoot").innerHTML = META.readonly
    ? `只读分享<br>数据每日 ${META.daily_at} 更新`
    : `${META.kg_session ? "kg 登录态 ✓" : '<span class="flag">kg 未登录</span>'}<br>每日 ${META.daily_at} 自动刷新` +
      (META.share ? `<br><button type="button" class="btn copy" id="shareBtn" title="${esc(META.share.url)}">复制局域网只读链接</button>` : "");
  if ($("#shareBtn")) $("#shareBtn").onclick = async ev => {
    try { await navigator.clipboard.writeText(META.share.url); flash(ev.target, "已复制", true); }
    catch (e) { prompt("复制这个链接发给同事（同一内网可打开，只读）：", META.share.url); }
  };
  $("#mktSeg").innerHTML = `<button data-m="ALL" class="${S.market === "ALL" ? "on" : ""}" title="RU + SGP + EU 三个机房按天相加">全机房</button>` +
    META.market_order.map(k => `<button data-m="${k}" class="${k === S.market ? "on" : ""}" title="${esc(META.markets[k])}">${k}</button>`).join("");
  // 国家多选：不选 = 整机房（RU 默认只算 region=RU）；选几个 = 这几个国家合计；每换一种组合要先拉一次数
  const setRegOpts = () => {
    $("#fReg").hidden = S.market === "ALL";          // 全机房 = 三个机房相加，不按国家选（要看国家先选机房）
    const opts = (META.regions[S.market] || []).map(c => ({ v: c, t: REG_NAME(c) }));
    picker($("#fReg"), opts, { multi: true, placeholder: S.region.length ? "加国家" : (S.market === "RU" ? "RU（默认），可多选国家" : "整机房，可多选国家"), value: S.region });
    $("#fReg").onchange = () => { S.region = $("#fReg")._pick.get(); $("#fReg input").placeholder = S.region.length ? "加国家" : (S.market === "RU" ? "RU（默认），可多选国家" : "整机房，可多选国家"); loadFunnel(); };
  };
  setRegOpts();
  $$("#mktSeg button").forEach(b => b.onclick = () => { S.market = b.dataset.m; S.region = []; $$("#mktSeg button").forEach(x => x.classList.toggle("on", x === b)); setRegOpts(); loadFunnel(); });
  $$("#tierSeg button").forEach(b => b.onclick = () => { S.tier = b.dataset.tier; $$("#tierSeg button").forEach(x => x.classList.toggle("on", x === b)); FUNNEL && renderFunnel(toNum($("#fStart").value), toNum($("#fEnd").value)); });
  $("#fEnd").value = toISO(META.yesterday); $("#fStart").value = toISO(addDays(META.yesterday, -6));
  $("#fStart").onchange = $("#fEnd").onchange = loadFunnel;
  $("#fRefresh").onclick = refreshFunnel;
  $("#hRefresh").onclick = refreshHot;
  $("#hSync").onclick = syncHotConfig;
  $("#oSync").onclick = syncObsConfig;
  $("#vSync").onclick = syncVersions;
  $("#hStart").onchange = $("#hEnd").onchange = loadHot;
  $("#newExpBtn").onclick = ev => openNewExp(ev);
  $("#expCancel").onclick = () => { $("#newExp").hidden = true; EDIT_EXP = null; };
  $("#addGrp").onclick = () => addGrpRow();
  $("#grpPaste").onpaste = onGroupPaste;
  $("#grpPaste").onchange = ev => ev.target.value.trim() && renderParsed(parseGroupTable(tsvGrid(ev.target.value)));
  $("#expForm").onsubmit = submitExp;
  $("#expMkt").onchange = renderRegions;
  $("#regList").onchange = syncRegions;
  $("#regClr").onclick = () => { $$("[name=rg]").forEach(x => x.checked = false); syncRegions(); };
  $("#regQ").oninput = () => { const q = $("#regQ").value.trim().toUpperCase(); $$("#regList label").forEach(l => l.hidden = !!q && !l.dataset.k.toUpperCase().includes(q)); };
  document.addEventListener("click", ev => { const dd = $("#regDD"); if (dd.open && !dd.contains(ev.target)) dd.open = false; });
  document.addEventListener("mouseover", ev => { const t = ev.target.closest("[data-tip]"); if (t) showTip(esc(t.dataset.tip), ev); });
  document.addEventListener("mouseout", ev => { if (ev.target.closest("[data-tip]")) hideTip(); });
  route();
})();
