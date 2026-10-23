"use strict";
// 页面结构全部来自 /api/meta（= dashboard.yaml）：范围、细分、档位、漏斗步骤、核心指标、节点节奏。
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = v => v == null || isNaN(v) ? "—" : Math.round(v).toLocaleString("en-US");
const pct = (v, d = 2) => v == null || !isFinite(v) ? "—" : (v * 100).toFixed(d) + "%";
const toISO = n => { const s = String(n); return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}`; };
const toNum = s => s.replaceAll("-", "");
const addDays = (n, k) => { const d = new Date(toISO(n) + "T00:00:00"); d.setDate(d.getDate() + k);
  return +`${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`; };
const ratio = (a, b) => a != null && b ? a / b : null;

async function api(path, opt = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opt, body: opt.body ? JSON.stringify(opt.body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

let META = null;
const S = { scope: null, sub: "", tier: null, trend: "rates" };
const viewKey = () => S.sub ? `${S.scope}:${S.sub}` : S.scope;
const scopeLabel = k => (META.scopes.find(s => s.key === k) || { label: k }).label;
const subLabel = v => (META.subscope && META.subscope.labels[v]) || v;
const viewText = () => scopeLabel(S.scope) + (S.sub ? ` · ${subLabel(S.sub)}（${S.sub}）` : "");
const tierLabel = k => (META.tiers.find(t => t.key === k) || { label: k }).label;
const stepLabel = k => ([...META.steps, ...META.extra_steps].find(s => s.key === k) || { label: k }).label;

// ─────────── 一键复制表格（text/html 粘到在线文档成表格，text/plain TSV 粘 Excel 按列拆分）───────────
const COPY = {};
const cellV = c => c && typeof c === "object" ? c.v : c;
const tsvCell = c => String(cellV(c) ?? "").replace(/[\t\r\n]+/g, " ");
const CELL_STYLE = { up: "color:#1d7a52;font-weight:700", down: "color:#c0392b;font-weight:700" };
function copyPayload(t) {
  const secs = t.sections || [t];
  const td = (c, tag) => `<${tag} style="border:1px solid #ccc;padding:4px 8px;${(c && CELL_STYLE[c.cls]) || ""}">${esc(cellV(c))}</${tag}>`;
  const tsv = secs.map(x => [x.caption, x.header.map(tsvCell).join("\t"), ...x.rows.map(r => r.map(tsvCell).join("\t"))].filter(y => y != null).join("\n")).join("\n\n");
  const html = secs.map(x => (x.caption ? `<p><b>${esc(x.caption)}</b></p>` : "") + `<table style="border-collapse:collapse"><thead><tr>${x.header.map(h => td(h, "th")).join("")}</tr></thead>` +
    `<tbody>${x.rows.map(r => `<tr>${r.map(v => td(v, "td")).join("")}</tr>`).join("")}</tbody></table>`).join("<p></p>");
  return { tsv, html, n: secs.reduce((a, x) => a + x.rows.length, 0) };
}
async function copyTable(key, btn) {
  const t = COPY[key] && COPY[key]();
  if (!t || !(t.sections || [t]).some(x => x.rows.length)) { flash(btn, "暂无数据", false); return; }
  const { tsv, html, n } = copyPayload(t);
  try {
    await navigator.clipboard.write([new ClipboardItem({ "text/html": new Blob([html], { type: "text/html" }), "text/plain": new Blob([tsv], { type: "text/plain" }) })]);
  } catch (e) {
    const box = document.createElement("div"); box.style.cssText = "position:fixed;left:-9999px;top:0"; box.innerHTML = html; document.body.appendChild(box);
    const rg = document.createRange(); rg.selectNodeContents(box); const sel = getSelection(); sel.removeAllRanges(); sel.addRange(rg);
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

function route() {
  const t = (location.hash || "#funnel").slice(1);
  $$(".tab").forEach(s => s.hidden = s.id !== "tab-" + t);
  $$("nav a").forEach(a => a.classList.toggle("on", a.dataset.tab === t));
  ({ funnel: loadFunnel, exp: loadExpList, metrics: loadMetrics, logs: loadJobs })[t]?.();
}
addEventListener("hashchange", route);

// ─────────── 日常漏斗 ───────────
// flow[d] 每天一行：uv__步骤__档（独立去重）、pv__步骤__档、in__步骤__档（包含式到达）
let FUNNEL = null;
const fu = (r, k, t) => r ? r[`uv__${k}__${t}`] : null;
const fin = (r, k, t) => r ? r[`in__${k}__${t}`] : null;

async function loadFunnel() {
  const d0 = toNum($("#fStart").value), d1 = toNum($("#fEnd").value);
  try {
    // 「较上一周」：取紧挨着的前一段同长窗口
    const len = Math.round((new Date(toISO(d1)) - new Date(toISO(d0))) / 864e5) + 1;
    const q = (a, b) => api(`/api/funnel?view=${encodeURIComponent(viewKey())}&start=${a}&end=${b}`);
    const [cur, prev] = await Promise.all([q(d0, d1), q(addDays(+d0, -len), addDays(+d0, -1))]);
    FUNNEL = cur; FUNNEL.prev = { flow: prev.flow || {}, status: prev.status || {}, d0: addDays(+d0, -len), d1: addDays(+d0, -1) };
  } catch (e) { $("#fNotice").innerHTML = `<div class="notice crit"><b>读取失败</b>${esc(e.message)}</div>`; return; }
  renderFunnel(+d0, +d1);
}

function renderFunnel(d0, d1) {
  const days = [];
  for (let d = d0; d <= d1; d = addDays(d, 1)) days.push(d);
  const fl = FUNNEL.flow;
  const have = days.filter(d => fl[d]);
  const bad = days.filter(d => FUNNEL.status[d] && !FUNNEL.status[d].complete);
  const missing = days.filter(d => !FUNNEL.status[d]);
  let n = "";
  if (!have.length) n += `<div class="notice info"><b>暂无数据</b>${esc(viewText())} 这个窗口还没拉过，点右上角「从数据源刷新」。拉过一次后每天 ${META.daily_at} 自动续。</div>`;
  else if (missing.length) n += `<div class="notice info"><b>部分日期未拉取</b>${missing.map(toISO).join("、")}</div>`;
  if (bad.length) n += `<div class="notice warn"><b>数据可能不完整</b>${bad.map(d => toISO(d) + "：" + esc(FUNNEL.status[d].note)).join("；")}。系统会在 ${META.daily_at} 后定时重试。</div>`;
  $("#fNotice").innerHTML = n;
  const good = have.filter(d => !bad.includes(d)), use = good.length ? good : have;
  const avg = f => { const xs = use.map(d => f(fl[d])).filter(v => v != null); return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null; };
  // 核心指标：KPI 用独立去重 UV（窗口内逐日比值的日均分子 ÷ 日均分母）；第一张是北极星大卡
  const tiles = META.kpis.map(k => {
    const num = avg(r => fu(r, k.num, S.tier)), den = k.den ? avg(r => fu(r, k.den, S.tier)) : null;
    return [k.name, k.den ? pct(ratio(num, den)) : fmt(num), k.note || (k.den ? `${stepLabel(k.num)} UV ÷ ${stepLabel(k.den)} UV` : "日均"), k];
  });
  const dauXs = use.map(d => FUNNEL.dau[d]).filter(Boolean), dau = dauXs.length ? dauXs.reduce((a, b) => a + b, 0) / dauXs.length : null;
  if (META.has_dau[S.scope]) tiles.push(["渗透率", dau ? pct(avg(r => fu(r, META.anchor, S.tier)) / dau) : "—", `${stepLabel(META.anchor)} UV ÷ 活跃设备`, null]);
  const k0 = META.kpis[0], ns = days.map(d => fl[d] ? (k0.den ? ratio(fu(fl[d], k0.num, S.tier), fu(fl[d], k0.den, S.tier)) : fu(fl[d], k0.num, S.tier)) : null);
  const [hero, ...rest] = tiles;
  $("#fTiles").innerHTML = `<div class="tile hero"><div><div class="k">${esc(hero[0])}</div><div class="v">${hero[1]}</div><div class="s">${esc(hero[2])}</div></div>${spark(days, ns, !!k0.den)}</div>` +
    rest.map(([k, v, s]) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="s">${esc(s)}</div></div>`).join("");
  const ctx = `${viewText()} · ${tierLabel(S.tier)} · ${toISO(d0)} ~ ${toISO(d1)}`;
  $("#fCtx").textContent = `${ctx}，共 ${days.length} 天`;
  COPY.tiles = () => ({ caption: ctx + "（日均）", header: ["指标", "值", "说明"], rows: have.length ? tiles.map(t => t.slice(0, 3)) : [] });
  renderFlow(days, ctx, bad);
  renderTrend(days);
  renderBreakdown(ctx);
  renderTable(days, bad, ctx);
}

// 北极星卡片里的逐日小折线：只标首尾日期，不画坐标轴
function spark(days, pts, isPct) {
  const xs = pts.map((v, i) => [i, v]).filter(([, v]) => v != null);
  if (xs.length < 2) return "";
  const W = 168, H = 52, P = 6, lo = Math.min(...xs.map(x => x[1])), hi = Math.max(...xs.map(x => x[1])), span = hi - lo || hi * 0.05 || 1;
  const x = i => P + i * (W - 2 * P) / Math.max(1, days.length - 1), y = v => P + 4 + (H - 2 * P - 12) * (1 - (v - lo) / span);
  const d = xs.map(([i, v], j) => (j ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1)).join("");
  const [i0, v0] = xs[0], [i1, v1] = xs[xs.length - 1], f = isPct ? (v => pct(v, 1)) : fmt;
  return `<svg class="spark" width="${W}" height="${H}" role="img" aria-label="逐日走势：${f(v0)} → ${f(v1)}"><path class="ln" d="${d}"/>` +
    `<circle class="dot" cx="${x(i1)}" cy="${y(v1)}" r="3"/><text x="${P}" y="${H - 1}">${toISO(days[i0]).slice(5)}</text><text x="${W - P}" y="${H - 1}" text-anchor="end">${toISO(days[i1]).slice(5)}</text></svg>`;
}

// ─────────── 包含式 UV 漏斗 ───────────
// 单位 = 设备-天：到达第 k 步要求当天走过前面所有步，所以每步转化 ≤ 100%；档位 skip 的步视为通过
const sgnPp = v => v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(2) + "pp";
const sgnPct = v => v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
const wowCls = v => { const m = /^([+-]?\d+\.\d+)pp$/.exec(v || ""); return !m ? "" : +m[1] >= 1 ? "wup" : +m[1] <= -1 ? "wdn" : ""; };
function flowAvg(days, bad, f) {
  const fl = FUNNEL.flow, have = days.filter(d => fl[d]), good = have.filter(d => !bad.includes(d)), use = good.length ? good : have;
  const xs = use.map(d => f(fl[d])).filter(v => v != null);
  return { v: xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null, n: use.length };
}
function renderFlow(days, ctx, bad) {
  const steps = META.steps, tiers = META.tiers.map(t => t.key), CORE = new Set(META.core);
  const skipOf = t => new Set((META.tiers.find(x => x.key === t) || {}).skip || []);
  const tbSkip = (t, k) => skipOf(t).has(k);
  const A = Object.fromEntries(tiers.map(t => [t, steps.map(s => flowAvg(days, bad, r => fin(r, s.key, t)).v)]));
  const n = flowAvg(days, bad, r => fin(r, steps[0].key, tiers[0])).n;
  if (!n) {
    const msg = `<p class="muted">这个窗口还没有漏斗数据，点右上角「从数据源刷新」。</p>`;
    $("#fFlow").innerHTML = msg; $("#fSteps").innerHTML = msg; $("#fFlowDaily").innerHTML = msg;
    COPY.flow = COPY.steps = COPY.flowDaily = () => ({ header: [], rows: [] });
    return;
  }
  const iA = Math.max(0, steps.findIndex(s => s.key === META.anchor)), aLab = steps[iA].label;
  const P = FUNNEL.prev, pdays = [];
  for (let d = P.d0; d <= P.d1; d = addDays(d, 1)) pdays.push(d);
  const full = (ds, fl, st) => ds.every(d => fl[d] && (!st[d] || st[d].complete));
  const curFull = full(days, FUNNEL.flow, FUNNEL.status), wow = curFull && full(pdays, P.flow, P.status);
  const PA = wow ? Object.fromEntries(tiers.map(t => [t, steps.map(s => pdays.reduce((a, d) => a + (fin(P.flow[d], s.key, t) || 0), 0) / pdays.length)])) : null;
  const wowCell = (t, i) => {
    if (!wow || tbSkip(t, steps[i].key)) return "—";
    if (!i) return sgnPct(ratio(A[t][0], PA[t][0]) - 1);
    const now = ratio(A[t][i], A[t][i - 1]), was = ratio(PA[t][i], PA[t][i - 1]);
    return now == null || was == null ? "—" : sgnPp(now - was);
  };
  const vsA = (v, t, i) => i < iA ? "—" : i === iA ? "100%" : pct(ratio(v, A[t][iA]), 1);
  const header = ["段", "环节"];
  for (const t of tiers) header.push(`${tierLabel(t)} UV`, "较上一步", "较上一周", `较${aLab}`);
  const rows = steps.map((s, i) => {
    const r = [i && steps[i - 1].segment === s.segment ? "" : s.segment, { v: s.label, cls: CORE.has(s.key) ? "core" : "" }];
    for (const t of tiers) {
      const v = A[t][i];
      r.push(fmt(v), tbSkip(t, s.key) ? "视为通过" : i ? pct(ratio(v, A[t][i - 1]), 1) : "—", wowCell(t, i), vsA(v, t, i));
    }
    return r;
  });
  const span = steps.map((s, i) => i && steps[i - 1].segment === s.segment ? 0 : steps.slice(i).findIndex(x => x.segment !== s.segment) >>> 0 || steps.length - i);
  const body = rows.map((r, i) => `<tr class="${span[i] ? "seg-start" : ""}">` + (span[i] ? `<td class="segc" rowspan="${span[i]}">${esc(cellV(r[0]))}</td>` : "") +
    r.slice(1).map((c, j) => { const k = [j === 0 ? "lab" : "", j === 0 && CORE.has(steps[i].key) ? "core" : "", j % 4 === 1 ? "ts" : "", j % 4 === 3 ? "wow " + wowCls(cellV(c)) : ""].filter(Boolean).join(" ").trim();
      return `<td${k ? ` class="${k}"` : ""}>${esc(cellV(c))}</td>`; }).join("") + "</tr>").join("");
  const unions = META.tiers.filter(t => t.union.length).map(t => `${t.label} = ${t.union.map(tierLabel).join("、")}任一路径到达（不是相加）`);
  const skips = META.tiers.filter(t => t.skip.length).map(t => `${t.label}没有「${t.skip.map(stepLabel).join("、")}」这一步，视为通过`);
  $("#fFlow").innerHTML = `<div class="scroll"><table class="ftab"><thead><tr>${header.map(x => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>` +
    `<p class="foot"><span class="core-key">粗体带竖条</span> = 核心链路。「较上一周」= 本窗口「较上一步」转化 − 上一段同长窗口（${toISO(P.d0).slice(5)}~${toISO(P.d1).slice(5)}），单位 pp；第一行为 UV 环比；|Δ| ≥ 1pp 着色，只作描述、不做显著性。` +
    (wow ? "" : ` <b class="warn-t">本列暂不可算：</b>${curFull ? `上一段（${toISO(P.d0)} ~ ${toISO(P.d1)}）有缺失或不完整的日期。<button type="button" class="btn" id="fillPrev">补同周期AA变化</button>（只拉上一段，当前窗口不动）` : "本窗口有缺失或不完整的日期，先点「从数据源刷新」补齐"}。`) +
    ` 单位 = 设备-天，${n} 天日均；到达第 k 步 = 当天也走过前面所有步，每步转化 ≤ 100%。${[...unions, ...skips].join("；")}</p>`;
  const fp = $("#fillPrev");
  if (fp) fp.onclick = () => fillPrev(fp, P.d0, P.d1);
  COPY.flow = () => ({ caption: ctx + "（UV 转化漏斗，设备-天，日均）", header, rows });
  // 条形图：各档并排、共用一个刻度（第一档第一步 = 满格）
  const max = Math.max(...A[tiers[0]].filter(v => v != null), 1);
  const stepRows = steps.map(s => [s.label]);
  $("#fSteps").innerHTML = `<div class="steps3">` + tiers.map(t => `<div class="steps"><h3>${esc(tierLabel(t))}</h3>` + steps.map((s, i) => {
    const v = A[t][i], gap = i && steps[i - 1].segment !== s.segment ? " gap" : "", mid = i > iA ? pct(ratio(v, A[t][iA]), 1) : null;
    const ret = i ? (tbSkip(t, s.key) ? "视为通过" : pct(ratio(v, A[t][i - 1]), 1)) : "";
    const tipTxt = `${tierLabel(t)} · ${s.label}：${fmt(v)}` + (i && !tbSkip(t, s.key) ? `；较上一步 ${pct(ratio(v, A[t][i - 1]), 1)}` : "") + (mid ? `；较${aLab} ${mid}` : "");
    stepRows[i].push(fmt(v), ret, vsA(v, t, i));
    return `<div class="row${gap}${CORE.has(s.key) ? " core" : ""}" data-tip="${esc(tipTxt)}"><div class="lbl">${esc(s.label)}</div><div class="track"><div class="fill" style="width:${v ? v / max * 100 : 0}%"></div></div><div class="val">${fmt(v)}</div><div class="rate">${ret}</div></div>`;
  }).join("") + `</div>`).join("") + `</div><p class="foot">各档共用一个刻度，条长可跨档比较；右列 = 较上一步，深色条 = 核心链路。鼠标悬停看较${esc(aLab)}。</p>`;
  COPY.steps = () => ({ caption: ctx + "（UV 转化漏斗，设备-天，日均）", header: ["环节", ...tiers.flatMap(t => [`${tierLabel(t)} UV`, "较上一步", `较${aLab}`])], rows: stepRows });
  // 逐日转化
  const fl = FUNNEL.flow, cs = steps.filter(s => CORE.has(s.key)), c0 = cs[0], c1 = cs[cs.length - 1];
  const dh = ["日期", `${steps[0].label} UV`, ...steps.slice(1).map(s => s.label), ...(c0 && c1 && c0 !== c1 ? [`核心链路（${c1.label} ÷ ${c0.label}）`] : [])];
  const drows = days.map(d => {
    const r = fl[d];
    if (!r) return [toISO(d), { v: "未拉取", cls: "muted" }, ...dh.slice(2).map(() => "")];
    return [toISO(d) + (bad.includes(d) ? " ⚠" : ""), fmt(fin(r, steps[0].key, S.tier)),
      ...steps.slice(1).map((s, i) => tbSkip(S.tier, s.key) ? "—" : pct(ratio(fin(r, s.key, S.tier), fin(r, steps[i].key, S.tier)), 1)),
      ...(dh.length > steps.length + 1 ? [pct(ratio(fin(r, c1.key, S.tier), fin(r, c0.key, S.tier)), 1)] : [])];
  });
  $("#fFlowDaily").innerHTML = tableHTML({ header: dh, rows: drows }, "") + `<p class="foot">各环节列 = 该步 ÷ 上一步（设备-天包含式）。</p>`;
  COPY.flowDaily = () => ({ caption: ctx + "（逐日转化，设备-天包含式）", header: dh, rows: drows.filter(r => cellV(r[1]) !== "未拉取") });
}

async function fillPrev(btn, d0, d1) {
  btn.disabled = true;
  try {
    const ids = [];
    for (let s = d0; s <= d1; s = addDays(s, 31))
      ids.push((await api("/api/refresh", { method: "POST", body: { views: [viewKey()], start: s, end: Math.min(addDays(s, 30), d1) } })).job_id);
    btn.textContent = `已提交 #${ids.join(" / #")}，拉完自动刷新…`;
    for (const id of ids) await waitJob(id);
    loadFunnel();
  } catch (e) { btn.disabled = false; btn.textContent = "补数失败：" + e.message; }
}

// 逐日趋势：KPI（独立去重口径）+ 核心链路相邻两步的包含式转化，UV 一组
function trendCfg() {
  const cs = META.steps.filter(s => META.core.includes(s.key));
  const rates = META.kpis.filter(k => k.den).slice(0, 1).map(k => [k.name, (r, t) => ratio(fu(r, k.num, t), fu(r, k.den, t))]);
  for (let i = 1; i < cs.length && rates.length < 4; i++) {
    const a = cs[i - 1], b = cs[i];
    rates.push([`${b.label} ÷ ${a.label}`, (r, t) => ratio(fin(r, b.key, t), fin(r, a.key, t))]);
  }
  return { rates: { label: "核心转化", series: rates, fmt: v => pct(v, 1) },
    uv: { label: "UV", series: cs.slice(0, 5).map(s => [s.label, (r, t) => fu(r, s.key, t)]), fmt: fmtShort } };
}
function fmtShort(v) { if (v == null) return "—"; const a = Math.abs(v);
  return a >= 1e6 ? (v / 1e6).toFixed(1) + "M" : a >= 1e3 ? (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k" : String(Math.round(v)); }
function renderTrend(days) {
  const T = trendCfg();
  $("#trendSeg").innerHTML = Object.entries(T).map(([k, v]) => `<button data-k="${k}" class="${S.trend === k ? "on" : ""}">${v.label}</button>`).join("");
  $$("#trendSeg button").forEach(b => b.onclick = () => { S.trend = b.dataset.k; renderTrend(days); });
  const cfg = T[S.trend] || T[S.trend = "rates"], fl = FUNNEL.flow;
  const series = cfg.series.map(([name, f], i) => ({ name, color: `var(--s${i + 1})`, pts: days.map(d => fl[d] ? f(fl[d], S.tier) : null) })).filter(se => se.pts.some(v => v != null));
  lineChart($("#fTrend"), days, series, cfg.fmt, false, null, { key: "fTrend:" + S.trend,
    title: `${viewText()} · ${tierLabel(S.tier)} · 逐日趋势（${cfg.label}，${toISO(days[0])} ~ ${toISO(days[days.length - 1])}）` });
  $("#trendFoot").textContent = META.kpis[0].den ? `「${META.kpis[0].name}」为独立去重 UV 口径（与实验回收一致）；其余转化为设备-天包含式。` : "";
  COPY.trend = () => {
    const cols = Object.entries(T).flatMap(([k, c]) => c.series.map(([name, f]) => [name + (k === "uv" ? " UV" : ""), f, k === "rates" ? pct : fmt]));
    return { caption: viewText() + " · " + tierLabel(S.tier) + "（逐日趋势）", header: ["日期", ...cols.map(c => c[0])],
      rows: days.filter(d => fl[d]).map(d => [toISO(d), ...cols.map(([, f, fm]) => fm(f(fl[d], S.tier)))]) };
  };
}

// ─────────── 图表公共：图例开关 + 复制图片 ───────────
// HIDE[图表键] = 被图例关掉的线名；重绘（换档 / 轮询刷新）时保留
const HIDE = {};
const chartTop = legend => `<div class="chart-top"><div class="legend">${legend}</div>` +
  `<button type="button" class="btn copy" data-img title="复制成 PNG，可直接粘到在线文档">复制图片</button></div>`;
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
    const ymin = opt.zero ? -zm : fit ? (lo >= 0 ? Math.max(0, lo - pad) : lo - pad) : Math.min(0, lo), ymax = opt.zero ? zm : fit ? hi + pad : niceMax(hi);
    const x = i => L + (days.length === 1 ? (W - L - R) / 2 : i * (W - L - R) / (days.length - 1));
    const y = v => T + (H - T - B) * (1 - (v - ymin) / (ymax - ymin));
    const ticks = [0, .25, .5, .75, 1].map(t => ymin + t * (ymax - ymin));
    const every = Math.ceil(days.length / Math.max(1, Math.floor((W - L - R) / 56)));
    s += ticks.map(t => `<line class="gl" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text x="${L - 6}" y="${y(t) + 4}" text-anchor="end">${yfmt(t)}</text>`).join("");
    s += `<line class="ax" x1="${L}" x2="${W - R}" y1="${y(ymin)}" y2="${y(ymin)}"/>`;
    if (opt.zero && ymin < 0 && ymax > 0) s += `<line class="zero" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>`;
    s += days.map((d, i) => i % every ? "" : `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${String(d).slice(4, 6)}-${String(d).slice(6)}</text>`).join("");
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
    for (const se of vis) {
      let path = "", pen = false;
      se.pts.forEach((v, i) => { if (v == null) { pen = false; return; } path += (pen ? "L" : "M") + x(i).toFixed(1) + "," + y(v).toFixed(1); pen = true; });
      s += `<path class="line" d="${path}" style="stroke:${se.color}"/>`;
      if (days.length <= 14) s += se.pts.map((v, i) => v == null ? "" : `<circle cx="${x(i)}" cy="${y(v)}" r="3.5" fill="${se.color}" stroke="var(--card)" stroke-width="2"/>`).join("");
    }
    s += `<line class="cross" y1="${T}" y2="${H - B}" visibility="hidden"/><rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" class="hit"/></svg>`;
    el.innerHTML = chartTop(legend) + s;
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

// 复制图片：把图表 SVG（含标题、显示中的图例项）栅格化成 2 倍 PNG 写进剪贴板，在线文档里直接粘贴
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
    `<p class="muted">浏览器不让直接写剪贴板（局域网 http 访问时常见）：在图片上右键 →「复制图片」，再粘到在线文档。</p><img src="${src}" alt="${esc(title)}"></div>`;
  d.onclick = ev => { if (ev.target === d || ev.target.closest("button")) d.remove(); };
  document.body.appendChild(d);
}
document.addEventListener("click", ev => { const b = ev.target.closest("[data-img]"); if (b) copyChartImage(b); });
function niceMax(v) { if (v <= 0) return 1; const p = 10 ** Math.floor(Math.log10(v)); const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p; }

async function waitJob(id, onTick) {
  for (;;) {
    await new Promise(r => setTimeout(r, 4000));
    const j = (await api("/api/jobs")).find(x => x.id === id);
    if (onTick) onTick(j);
    if (!j || j.status === "done") return j;
    if (j.status === "failed" || j.status === "cancelled") throw new Error(j.error || "任务失败，见任务日志");
  }
}


function tableHTML(t, klass) {
  const cell = (c, tag) => `<${tag}${c && c.cls ? ` class="${c.cls}"` : ""}>${esc(cellV(c))}</${tag}>`;
  return `<div class="scroll"><table class="${klass}"><thead><tr>${t.header.map(x => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>` +
    t.rows.map((r, i) => `<tr class="${t.rows.types ? t.rows.types[i] : ""}">${r.map(c => cell(c, "td")).join("")}</tr>`).join("") + `</tbody></table></div>`;
}


// 分桶转化（如位置 CTR）：窗口累计 PV；union 档 = 成员档相加（成员档的 where 应互斥）
function renderBreakdown(ctx) {
  const card = $("#bdCard"); card.hidden = !META.breakdown;
  if (!META.breakdown) return;
  $("#bdTitle").textContent = META.breakdown_label || "分桶转化";
  const t = META.tiers.find(x => x.key === S.tier), mem = new Set(t && t.union.length ? t.union : [S.tier]);
  const agg = {};
  for (const p of FUNNEL.breakdown) {
    if (!mem.has(p.tier)) continue;
    const a = (agg[p.b] ||= { e: 0, c: 0 }); a.e += p.expose || 0; a.c += p.click || 0;
  }
  const ps = Object.keys(agg).map(Number).sort((a, b) => a - b), ctr = ps.map(p => agg[p].e ? agg[p].c / agg[p].e : 0);
  COPY.bd = () => ({ caption: ctx + `（${META.breakdown_label || "分桶转化"}，窗口累计 PV）`, header: ["分桶", "曝光 PV", "点击 PV", "转化"],
    rows: ps.map((p, i) => [p, fmt(agg[p].e), fmt(agg[p].c), pct(ctr[i])]) });
  const el = $("#fBd");
  if (!ps.length) { el.innerHTML = `<p class="muted">无数据</p>`; return; }
  const W = Math.max(el.clientWidth || 700, 340), H = 200, L = 46, R = 10, T = 18, B = 24;
  const ymax = niceMax(Math.max(...ctr)), bw = (W - L - R) / ps.length, y = v => T + (H - T - B) * (1 - v / ymax);
  let s = `<svg width="${W}" height="${H}" role="img" aria-label="${esc(META.breakdown_label || "分桶转化")}">`;
  s += [0, .5, 1].map(k => `<line class="gl" x1="${L}" x2="${W - R}" y1="${y(k * ymax)}" y2="${y(k * ymax)}"/><text x="${L - 6}" y="${y(k * ymax) + 4}" text-anchor="end">${pct(k * ymax, 0)}</text>`).join("");
  ps.forEach((p, i) => {
    const x0 = L + i * bw + 1, w = Math.max(bw - 2, 2), y0 = y(ctr[i]), r = Math.min(4, w / 2, y(0) - y0);
    s += `<path d="M${x0},${y(0)} V${y0 + r} q0,-${r} ${r},-${r} H${x0 + w - r} q${r},0 ${r},${r} V${y(0)} Z" fill="var(--s1)"/>`;
    s += `<text x="${x0 + w / 2}" y="${H - 8}" text-anchor="middle">${p}</text>`;
    if (i < 3) s += `<text class="lab" x="${x0 + w / 2}" y="${y0 - 4}" text-anchor="middle">${pct(ctr[i], 1)}</text>`;
    s += `<rect x="${L + i * bw}" y="${T}" width="${bw}" height="${H - T - B}" fill="transparent" data-i="${i}" class="hit"/>`;
  });
  el.innerHTML = s + `<line class="ax" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/></svg>`;
  $$(".hit", el).forEach(r => {
    r.addEventListener("mousemove", ev => { const i = +r.dataset.i, p = ps[i];
      showTip(`<b>分桶 ${p}</b><br>转化 ${pct(ctr[i])}<br>曝光 ${fmt(agg[p].e)} · 点击 ${fmt(agg[p].c)}`, ev); });
    r.addEventListener("mouseleave", hideTip);
  });
}

// 原始 UV：各步独立去重（含不进链路的补充步骤），相邻两步不可相除
function renderTable(days, bad, ctx) {
  const steps = [...META.steps, ...META.extra_steps], fl = FUNNEL.flow;
  const header = ["日期", ...steps.map(s => s.label + " UV")];
  const rows = [], prev = [], ck = META.completeness_step;
  let h = `<table><thead><tr>${header.map(x => `<th>${esc(x)}</th>`).join("")}</tr></thead><tbody>`;
  for (const d of days) {
    const r = fl[d];
    if (!r) {
      h += `<tr><td>${toISO(d)}</td><td colspan="${steps.length}" class="muted" style="text-align:left">未拉取</td></tr>`;
      rows.push([toISO(d), ...steps.map(() => ""), "未拉取"]); continue;
    }
    const a = fu(r, ck, S.tier), p = prev.length ? prev[prev.length - 1] : null, jump = p && a && (a / p > 2 || p / a > 2);
    prev.push(a);
    const cells = steps.map(s => fmt(fu(r, s.key, S.tier)));
    h += `<tr class="${bad.includes(d) ? "bad" : ""}"><td>${toISO(d)}${bad.includes(d) ? " ⚠" : ""}${jump ? ' <span class="flag" title="与前一日相差超过 2 倍">台阶</span>' : ""}</td>` + cells.map(c => `<td>${c}</td>`).join("") + "</tr>";
    rows.push([toISO(d), ...cells, [bad.includes(d) ? "数据可能不完整" : "", jump ? "台阶（与前一日相差 >2 倍）" : ""].filter(Boolean).join("；")]);
  }
  $("#fTable").innerHTML = h + "</tbody></table>";
  COPY.daily = () => ({ caption: ctx + "（原始 UV，各步独立去重）", header: [...header, "备注"], rows: rows.some(r => r[r.length - 1] !== "未拉取") ? rows : [] });
}

async function refreshFunnel() {
  const b = $("#fRefresh"); b.disabled = true;
  try {
    const j = await api("/api/refresh", { method: "POST", body: { views: [viewKey()], start: toNum($("#fStart").value), end: toNum($("#fEnd").value) } });
    $("#fNotice").innerHTML = `<div class="notice info"><b>已提交 #${j.job_id}</b>正在从数据源拉数，完成后自动刷新…</div>`;
    await waitJob(j.job_id);
    loadFunnel();
  } catch (e) { $("#fNotice").innerHTML = `<div class="notice crit"><b>刷新失败</b>${esc(e.message)}</div>`; }
  finally { b.disabled = false; }
}

// ─────────── 实验 ───────────
let METRICS = [];
let CUR_EXP = null;
const STAGES = ["查生效", "判止损", "中期读数", "出结论"];
const ROLE_NAME = { north_star: "北极星", process: "过程", guardrail: "护栏" };
const expScopeText = e => scopeLabel(e.scope) + (e.subs && e.subs.length ? ` · ${e.subs.map(subLabel).join("/")}` : "");
const statusText = s => ({ queued: "排队中", running: "跑数中", active: "续跑中", frozen: "已冻结", failed: "失败" }[s] || s);

async function loadExpList() {
  METRICS = await api("/api/metrics");
  const list = await api("/api/experiments");
  $("#expList").innerHTML = list.length ? list.map(e => `<div class="item ${e.id === CUR_EXP ? "on" : ""}" data-id="${e.id}">
      <div class="t">${esc(e.name)}</div>
      <div class="m">${esc(expScopeText(e))} · ${toISO(e.start)} ~ ${toISO(e.end)} · <span class="pill ${e.status}">${statusText(e.status)}</span></div>
      ${e.stage ? `<div class="stg"><span class="stage s-${STAGES.indexOf(e.stage.stage) + 1}">${esc(e.stage.stage)}</span>${e.stage.day ? `第 ${e.stage.day} 天` : ""}${e.stage.next ? ` · 下一节点 ${esc(e.stage.next)}` : ""} · ${e.last_end ? `数据到 ${toISO(e.last_end).slice(5)}` : "还没跑数"}</div>` : ""}
    </div>`).join("") : `<p class="muted">还没有实验，点右上角「新建回收」。</p>`;
  $$("#expList .item").forEach(it => it.onclick = () => openExp(+it.dataset.id));
  if (!CUR_EXP && list.length) CUR_EXP = list[0].id;
  if (CUR_EXP && list.some(e => e.id === CUR_EXP)) openExp(CUR_EXP);
}

// 细分（如国家 / 地区）多选：选项来自 dashboard.yaml 的 subscope.values（白名单）
function renderSubs() {
  const sub = META.subscope;
  $("#subBox").hidden = !sub;
  if (!sub) return;
  $("#subLbl").textContent = sub.label; $("#subQ").value = "";
  $("#subList").innerHTML = sub.values.map(v => `<label data-k="${esc(v + " " + subLabel(v))}"><input type="checkbox" name="sb" value="${esc(v)}"><span class="code">${esc(v)}</span>${esc(subLabel(v))}</label>`).join("");
  syncSubs();
}
function syncSubs() {
  const sel = $$("[name=sb]:checked").map(x => x.value);
  $("#subSum").textContent = sel.length ? sel.map(v => `${v} ${subLabel(v)}`).join("、") : "全部（不细分）";
  $("#subHint").textContent = sel.length ? `已选 ${sel.length} 个；分组、P99 封顶、全部指标都只算这些${META.subscope.label}` : `可单选或多选，共 ${META.subscope.values.length} 个`;
}

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
  $("#expScope").innerHTML = META.scopes.map(s => `<option value="${esc(s.key)}">${esc(s.label)}</option>`).join("");
  renderSubs();
  $("#grpRows").innerHTML = ""; addGrpRow("control"); addGrpRow("treatment");
  $("#expMetrics").innerHTML = METRICS.map(m => { const on = m.preset && m.kind !== "quantile";
    return `<div class="mk-row"><label><input type="checkbox" name="mk" value="${m.key}" ${on ? "checked" : ""}> ${esc(m.name)} <span class="pill">${ROLE_NAME[m.role || "process"]}</span>${m.preset ? "" : ' <span class="pill">自定义</span>'}</label>
      <label class="daily" title="除全窗口读数外，再按天出一份趋势"><input type="checkbox" name="dk" value="${m.key}" ${on ? "" : "disabled"}> By天</label></div>`; }).join("");
  $$("[name=mk]").forEach(x => x.onchange = () => { const d = x.closest(".mk-row").querySelector("[name=dk]"); d.disabled = !x.checked; if (!x.checked) d.checked = false; });
  $("#dkAll").onclick = () => { const ds = $$("[name=dk]:not(:disabled)"); const all = ds.every(d => d.checked); ds.forEach(d => d.checked = !all); };
  $("#expForm [name=end]").value = toISO(META.yesterday);
  $("#launchHint").textContent = `实验配置生效当天；P99 封顶取这天之前 ${META.cap_pre_days} 天。留空 = 开窗日`;
  if (e) fillExpForm(e);
  f.scrollIntoView({ behavior: "smooth", block: "start" });
}
function fillExpForm(e) {
  const f = $("#expForm");
  f.name.value = e.name; f.scope.value = e.scope;
  (e.subs || []).forEach(v => { const x = $$("[name=sb]").find(i => i.value === v); if (x) x.checked = true; });
  if (META.subscope) syncSubs();
  f.start.value = toISO(e.start); f.end.value = toISO(e.end); f.launch.value = e.launch ? toISO(e.launch) : "";
  $("#grpRows").innerHTML = "";
  e.groups.forEach(g => { addGrpRow(g.role); const r = $$("#grpRows .grp-row").pop();
    r.querySelector("[name=gid]").value = g.id; r.querySelector("[name=glabel]").value = g.label && g.label !== g.id ? g.label : ""; r.querySelector("[name=gw]").value = g.weight || 1; });
  $$("[name=mk]").forEach(x => { x.checked = e.metric_keys.includes(x.value); x.onchange(); });
  $$("[name=dk]").forEach(x => { x.checked = !x.disabled && (e.daily_keys || []).includes(x.value); });
}
function addGrpRow(role) {
  const d = document.createElement("div"); d.className = "grp-row";
  d.innerHTML = `<input name="gid" placeholder="分组号，如 G100" size="18" required aria-label="分组号">
    <input name="glabel" placeholder="备注（如 新排序）" size="14" aria-label="备注">
    <label class="muted">流量占比 <input name="gw" type="number" min="0.01" step="any" value="1" style="width:5em"></label>
    <div class="seg small"><button type="button" data-r="control">对照</button><button type="button" data-r="treatment">实验</button></div>
    <button type="button" class="btn danger" title="删除这一组" aria-label="删除这一组">×</button>`;
  const setRole = r => { d.dataset.role = r; $$(".seg button", d).forEach(b => b.classList.toggle("on", b.dataset.r === r)); };
  $$(".seg button", d).forEach(b => b.onclick = () => setRole(b.dataset.r));
  if (role) setRole(role);
  d.querySelector(".danger").onclick = () => d.remove();
  $("#grpRows").appendChild(d);
}

async function submitExp(ev) {
  ev.preventDefault();
  const f = ev.target, msg = $("#expMsg");
  const groups = $$(".grp-row", f).map(r => ({ id: r.querySelector("[name=gid]").value.trim(), label: r.querySelector("[name=glabel]").value.trim(),
    weight: +r.querySelector("[name=gw]").value || 1, role: r.dataset.role }));
  if (groups.some(g => !g.role)) { msg.textContent = "每个组都要选「对照 / 实验」"; return; }
  const body = { name: f.name.value, scope: f.scope.value, start: toNum(f.start.value), end: f.end.value ? toNum(f.end.value) : null,
    launch: f.launch.value ? toNum(f.launch.value) : null, groups, subs: $$("[name=sb]:checked", f).map(x => x.value),
    metric_keys: $$("[name=mk]:checked", f).map(x => x.value), daily_keys: $$("[name=dk]:checked", f).map(x => x.value) };
  try {
    if (EDIT_EXP) {
      const j = await api(`/api/experiments/${EDIT_EXP.id}`, { method: "PUT", body });
      EDIT_NOTE = { id: EDIT_EXP.id, text: { full: "改了范围 / 细分 / 分组 / 日期：已按新口径整体重跑。", none: "已保存（没有新增指标或 By天，不用跑数）。",
        incremental: `已保存，只补算新增部分：${[...(j.add || []).map(metricName), ...(j.add_daily || []).map(k => metricName(k) + " By天")].join("、")}。已出结果保留。` }[j.mode] };
      CUR_EXP = EDIT_EXP.id;
    } else {
      CUR_EXP = (await api("/api/experiments", { method: "POST", body })).id;
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
  const e = await api(expUrl(id));
  renderExp(e);
  if ((["queued", "running"].includes(e.status) || (e.pending || []).length) && !quiet) pollExp(id);
}
async function pollExp(id) {
  for (;;) {
    await new Promise(r => setTimeout(r, 5000));
    if (CUR_EXP !== id) return;
    const e = await api(expUrl(id));
    renderExp(e);
    if (!["queued", "running"].includes(e.status) && !(e.pending || []).length) { loadExpList(); return; }
  }
}

let SUM_TEXT = "";
function renderExp(e) {
  const v = e.view, C = META.cadence, alpha = META.alpha;
  const name = id => { const g = e.groups.find(x => x.id === id); return g ? `${g.label && g.label !== g.id ? esc(g.label) + " · " : ""}${esc(g.id)}` : esc(id); };
  let h = `<div class="bar"><div><h1>${esc(e.name)}</h1><div class="muted">${esc(expScopeText(e))} · ${toISO(e.start)} ~ ${toISO(e.end)}${e.last_end ? ` · 已跑到 ${toISO(e.last_end)}` : ""} · <span class="pill ${e.status}">${statusText(e.status)}</span></div></div>
    <div class="controls"><button class="btn" id="editExp">编辑</button><button class="btn" id="rerun">重跑</button><button class="btn danger" id="delExp">删除</button></div></div>`;
  if (EDIT_NOTE && EDIT_NOTE.id === e.id) h += `<div class="notice info"><b>已编辑</b>${esc(EDIT_NOTE.text)}</div>`;
  if (e.status === "failed") h += `<div class="notice crit"><b>失败</b>${esc(e.error)}</div>`;
  else if (e.error) h += `<div class="notice warn"><b>部分失败</b>${esc(e.error.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").slice(0, 300))}<br>点「重跑」只补失败的部分，已出结果不动。</div>`;
  if (["queued", "running"].includes(e.status)) h += `<div class="notice info"><b>跑数中</b>先算上线前 P99，再一次扫描算全部比率指标，比率指标出来就先展示；累计行、By天、分位数排在后面补算。进度见「任务日志」。</div>`;
  else if ((e.pending || []).length) h += `<div class="notice info"><b>补算中</b>比率指标已出，可以先看；${e.pending.map(k => esc(k.startsWith("@") ? "累计行 / By天" : metricName(k.replace(/@.*/, "")))).filter((x, i, a) => a.indexOf(x) === i).join("、")} 在后台补算，完成后自动刷新。</div>`;
  const sm = v.summary;
  if (sm) {
    const gname = id => { const g = e.groups.find(x => x.id === id); return g ? (g.label && g.label !== g.id ? `${g.label} ${g.id}` : g.id) : id; };
    const head = `${e.name}（${expScopeText(e)}，${toISO(e.start).slice(5)}~${toISO(e.last_end || e.end).slice(5)}，${e.last_end ? Math.round((new Date(toISO(e.last_end)) - new Date(toISO(e.start))) / 864e5) + 1 + " 天" : "未跑数"}）`;
    const blocks = sm.state === "ok" ? sm.groups.map(g => ({ title: sm.groups.length > 1 ? `实验 ${gname(g.grp)} vs 对照` : "", lines: g.lines })) : [{ title: "", lines: [sm.text] }];
    const sh = sm.head, stageLine = sh ? `第 ${sh.day} 天 · ${sh.stage}${sh.note ? `（${sh.note}）` : ""}${sh.next ? ` · 下一节点：${sh.next}` : ""}` : "";
    h += `<div class="card summary"><div class="card-h"><h2>结论提要</h2><button type="button" class="btn copy" id="copySum">复制结论</button></div>
      <p class="sum-head">${esc(head)}</p>${stageLine ? `<p class="sum-stage">${STAGES.map((x, i) => `<span class="${x === sh.stage ? "on" : i < STAGES.indexOf(sh.stage) ? "done" : ""}">${x}</span>`).join("")}<small>${esc(stageLine)}</small></p>` : ""}${blocks.map(b => `${b.title ? `<p class="sum-sub">${esc(b.title)}</p>` : ""}<ol>${b.lines.map(l => `<li>${esc(l)}</li>`).join("")}</ol>`).join("")}
      <p class="foot">节点：第 ${C.check} 天查生效（分流 / 进组）→ 第 ${C.stop_loss} 天判止损（前 ${C.stop_loss} 天累计，北极星或护栏的置信区间整体落在变差一侧才提示止损）→ 第 ${C.interim} 天中期读数 → 第 ${C.final} 天出结论（读当前累计行）。显著 = p &lt; ${alpha}；显著但 |Δ| 小于可测下限（MDE）的不引用幅度；逐日同向天数不作判据。看板不给采纳建议。</p></div>`;
    SUM_TEXT = [head, stageLine, ...blocks.flatMap(b => [b.title, ...b.lines.map((l, i) => `${i + 1}. ${l}`)])].filter(Boolean).join("\n");
  }
  if (v.srm) {
    const s = v.srm, bad = s.p != null && s.p < META.srm_p, eq = e.groups.every(g => (g.weight || 1) === (e.groups[0].weight || 1));
    h += `<div class="notice ${bad ? "crit" : "info"}"><b>${bad ? "⚠ SRM 不通过" : "分流检查"}</b>各组设备数 ${Object.entries(s.counts).map(([g, n]) => name(g) + "：" + fmt(n)).join("，")}；
      跨组冲突设备 ${fmt(s.conflict)} 台（已剔除）；卡方 p = ${s.p == null ? "—" : s.p.toExponential(2)}${bad ? "。分流不均，以下读数不可信。" : eq ? "（等比假设）" : "（按填写的流量占比）"}</div>`;
  }
  const lz = e.launch || e.start, pre = d => toISO(addDays(lz, d)).slice(5), N = META.cap_pre_days;
  if (Object.keys(e.caps || {}).length) h += `<p class="foot">P99 封顶（上线日 ${toISO(lz).slice(5)} 之前 ${N} 天 ${pre(-N)}~${pre(-1)}，写死）：${Object.entries(e.caps).map(([k, c]) => esc(metricName(k.replace("@day", ""))) + (k.endsWith("@day") ? "（By天，单设备单天）" : "") + " = " + fmt(c)).join("；")}</p>`;
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
        <div class="chart" id="xEntry"></div><p class="foot">设备按窗口内第一次带组号的日期计入（认组口径同读数：当天认组事件带组号；跨组设备剔除）。累计到最后一天 = 上方分流检查的各组设备数。两组曲线应基本重合；某天某组明显偏离 = 那天分流或上报出了问题。</p>`;
    } else h += `<p class="muted">${dpend ? "分布箱型图 / 进组趋势补算中…" : "分布箱型图 / 进组趋势还没算：点「重跑」只补这部分（不重跑已出结果）。"}</p>`;
    h += `</section>`;
  }
  const TB = expTables(e, v.tables || [], base.id);
  if (TB.overview.rows.length) {
    h += `<div class="card-h"><h2>总览（全窗口累计）</h2><div class="controls"><button type="button" class="btn copy" data-copy="ov">复制表格</button><button type="button" class="btn copy" data-copy="allTables">复制全部表格</button></div></div>`;
    h += tableHTML(TB.overview, "otab");
  }
  TB.items.forEach((x, i) => {
    h += `<section class="mtable"><div class="card-h"><h2>${esc(x.title)}</h2><button type="button" class="btn copy" data-copy="t${i}">复制表格</button></div>`;
    h += `<p class="foot">${esc(x.note)}${x.waits.length ? ` <span class="pill running">补算中：${esc(x.waits.join("、"))}</span>` : ""}</p>`;
    h += x.rows.length ? tableHTML(x, "dtab2") : `<p class="muted">${x.waits.length ? "补算中…" : e.error ? "补算失败（原因见顶部），点「重跑」只补这部分" : "暂无数据"}</p>`;
    if (x.chart) h += `<details class="trend" data-i="${i}"><summary>趋势图</summary>${x.src.kind === "ratio" ? `<div class="seg small tmode">${[["diff", "差值 + 95% CI"], ["value", "各组取值"]].map(([k, l]) => `<button type="button" data-k="${k}" class="${(S.tmode || "diff") === k ? "on" : ""}">${l}</button>`).join("")}</div>` : ""}<div class="chart" id="tch${i}"></div></details>`;
    h += `</section>`;
    COPY["t" + i] = () => ({ caption: `${e.name} · ${expScopeText(e)} · ${x.title}`, header: x.header, rows: x.rows });
  });
  COPY.ov = () => ({ caption: `${e.name} · ${expScopeText(e)} · 总览（${toISO(e.start)} ~ ${toISO(e.last_end || e.end)}）`, header: TB.overview.header, rows: TB.overview.rows });
  COPY.allTables = () => ({ sections: [{ caption: `${e.name} · ${expScopeText(e)} · 总览`, header: TB.overview.header, rows: TB.overview.rows },
    ...TB.items.filter(x => x.rows.length).map(x => ({ caption: x.title, header: x.header, rows: x.rows }))] });
  if (TB.items.length) h += `<p class="foot">判读：p &lt; ${alpha} 为显著，按指标方向标正向 / 负向；标准误按设备聚类（delta method）。Δ：百分比指标为百分点差（pp），数值指标为相对差（%）。
    累计行 = 设备在该时段内取并集（分子、分母不要求同一天），封顶指标用上线前 ${N} 天单设备单天 P99；逐日行单位 = 设备-天。MDE = 本窗口在 α ${alpha} / power 0.8 下可测出的最小差异。看板只给数据，采纳与否由人判断。</p>`;
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
  const busy = ["queued", "running"].includes(e.status) || (e.pending || []).length;
  $("#editExp").disabled = !!busy; $("#editExp").title = busy ? "跑完再编辑" : "加指标 / 加 By天 只补算新增部分";
  $("#editExp").onclick = () => openNewExp(null, e);
  const cs = $("#copySum");
  if (cs) cs.onclick = async () => { try { await navigator.clipboard.writeText(SUM_TEXT); flash(cs, "已复制", true); } catch (_) { flash(cs, "复制失败", false); } };
  $("#rerun").onclick = async () => { await api(`/api/experiments/${e.id}/rerun`, { method: "POST" }); openExp(e.id); };
  $("#delExp").onclick = async () => { if (!confirm(`删除「${e.name}」及其结果？`)) return; await api(`/api/experiments/${e.id}`, { method: "DELETE" }); CUR_EXP = null; $("#expDetail").innerHTML = ""; loadExpList(); };
}

// ─────────── 实验读数表：纵向 逐日 → 前 N 天累计 → 累计；横向 对照 | 实验 | Δ | z | 判读 ───────────
const sgn = (v, d) => v == null || !isFinite(v) ? "—" : (v >= 0 ? "+" : "") + v.toFixed(d);
function expTables(e, tables, baseId) {
  const ctrl = e.groups.find(g => g.id === baseId) || e.groups.find(g => g.role === "control"), trts = e.groups.filter(g => g.id !== ctrl.id);
  const cname = ctrl.role === "control" ? "对照" : "基准";
  const lab = g => g.label && g.label !== g.id ? `${g.label} ${g.id}` : g.id, multi = trts.length > 1;
  const val = (t, x) => {
    if (t.kind !== "ratio") return x ? x.q.map(v => v == null ? "—" : +v.toPrecision(4)).join(" / ") : "—";
    if (!x || x.value == null) return "—";
    return `${t.unit !== "num" ? pct(x.value) : x.value.toFixed(2)}（${fmt(x.num)}/${fmt(x.den)}）`;
  };
  const delta = (t, c, xc, xt) => {
    if (t.kind !== "ratio") return xc && xt ? xt.q.map((v, i) => v == null || xc.q[i] == null ? "—" : sgn(v - xc.q[i], 0)).join(" / ") : "—";
    if (!c) return "—";
    return t.unit !== "num" ? sgn(c.diff * 100, 2) + "pp" : sgn(c.rel * 100, 1) + "%";
  };
  const cls = c => c && c.read ? (c.read === "显著正向" ? "up" : "down") : "";
  const read = r => { const xs = trts.filter(g => r.vs[g.id] && r.vs[g.id].read);
    return { v: xs.length ? xs.map(g => (multi ? lab(g) + " " : "") + r.vs[g.id].read).join("；") : "—", cls: xs.length === 1 ? cls(r.vs[xs[0].id]) : "" }; };
  const items = tables.map(t => {
    const header = ["日期", `${cname} ${lab(ctrl)}`];
    for (const g of trts) header.push(`实验 ${lab(g)}`, multi ? `Δ（${g.label || g.id}）` : "Δ", "z");
    header.push("判读");
    const rows = t.rows.map(r => {
      const out = [{ v: r.label, cls: r.type === "day" ? "" : "sum" }, val(t, r.groups[ctrl.id])];
      for (const g of trts) {
        const c = r.vs[g.id];
        out.push(val(t, r.groups[g.id]), { v: delta(t, c, r.groups[ctrl.id], r.groups[g.id]), cls: cls(c) }, t.kind === "ratio" && c && c.z != null ? sgn(c.z, 2) : "—");
      }
      out.push(t.kind === "ratio" ? read(r) : "—");
      return out;
    });
    rows.types = t.rows.map(r => r.type);
    return { key: t.key, title: `${t.group}：${t.name}`, note: t.note, waits: t.waits, header, rows, src: t, chart: t.has_daily && t.rows.some(r => r.type === "day") };
  });
  // 总览：每个指标取「累计」行 + MDE；按 北极星 → 过程 → 护栏 排
  const order = { north_star: 0, process: 1, guardrail: 2 };
  const oh = ["指标", `${cname} ${lab(ctrl)}`];
  for (const g of trts) oh.push(`实验 ${lab(g)}`, multi ? `Δ（${g.label || g.id}）` : "Δ", "z", "MDE");
  oh.push("判读");
  const orows = [];
  for (const t of [...tables].sort((a, b) => order[a.role] - order[b.role])) {
    const r = t.rows.find(x => x.type === "total");
    if (!r) continue;
    const out = [`${t.group}：${t.name}`, val(t, r.groups[ctrl.id])];
    for (const g of trts) {
      const c = r.vs[g.id];
      out.push(val(t, r.groups[g.id]), { v: delta(t, c, r.groups[ctrl.id], r.groups[g.id]), cls: cls(c) },
        t.kind === "ratio" && c && c.z != null ? sgn(c.z, 2) : "—",
        t.kind === "ratio" && c && c.mde_abs != null ? "±" + (t.unit !== "num" ? (c.mde_abs * 100).toFixed(2) + "pp" : c.mde_rel != null ? (c.mde_rel * 100).toFixed(1) + "%" : "—") : "—");
    }
    out.push(t.kind === "ratio" ? read(r) : "—");
    orows.push(out);
  }
  return { items, overview: { header: oh, rows: orows } };
}
// 组的颜色跟着组走（按实验里登记的顺序），换基准 / 关线都不重新上色
const gColor = (e, id) => `var(--s${Math.min(e.groups.findIndex(g => g.id === id) + 1, 5)})`;
// 逐日趋势：差值模式 = 实验 − 基准（By天，单位 = 设备-天）+ 95% CI 带；取值模式 = 各组原值
function drawTrend(e, x, id, baseId) {
  const el = $("#" + id), t = x.src, base = e.groups.find(g => g.id === baseId) || e.groups.find(g => g.role === "control");
  const others = e.groups.filter(g => g.id !== base.id);
  const rows = t.rows.filter(r => r.type === "day"), days = rows.map(r => r.date);
  const lab = g => gLab(g) + (g.id === base.id ? "（基准）" : "");
  const title = `${e.name} · ${x.title}`;
  if (t.kind === "ratio" && (S.tmode || "diff") === "diff") {
    const pp = t.unit !== "num";
    const dv = c => c == null ? null : pp ? c.diff : c.rel, lo = c => c == null ? null : pp ? c.ci_low : c.rel_ci_low, hi = c => c == null ? null : pp ? c.ci_high : c.rel_ci_high;
    const series = others.map(g => { const cs = rows.map(r => r.vs[g.id]);
      return { name: `${gLab(g)} − ${gLab(base)}`, color: gColor(e, g.id), pts: cs.map(dv), band: [cs.map(lo), cs.map(hi)] }; });
    const yfmt = v => v == null ? "—" : pp ? sgn(v * 100, 2) + "pp" : sgn(v * 100, 1) + "%";
    lineChart(el, days, series, yfmt, true, null, { zero: true, key: id + ":diff:" + t.key, title: title + `（逐日差值，${pp ? "百分点" : "相对差"}，95% CI）` });
    return;
  }
  const pick = t.kind === "ratio" ? x2 => x2 ? x2.value : null : x2 => x2 && x2.q[0] != null ? x2.q[0] : null;
  const series = [base, ...others].map(g => ({ name: lab(g) + (t.kind === "ratio" ? "" : " P50"), color: gColor(e, g.id), pts: rows.map(r => pick(r.groups[g.id])) }));
  const yfmt = v => v == null ? "—" : t.kind !== "ratio" ? Math.round(v) + " ms" : t.unit !== "num" ? (v * 100).toFixed(v < 0.1 ? 2 : 1) + "%" : v.toFixed(2);
  lineChart(el, days, series, yfmt, true, null, { key: id + ":val:" + t.key, title: title + "（逐日取值）" });
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
  return ts.map(t => {
    const r = totalRow(t), c = r.vs[trt.id], d = dOf(t, c), gb = r.groups[base.id], gt = r.groups[trt.id];
    const v = x => x && x.value != null ? (t.unit !== "num" ? pct(x.value) : x.value.toFixed(2)) : "—";
    if (!d) return `<div class="xcard"><div class="k">${esc(t.name)}</div><div class="v">—</div><div class="s">暂无累计读数</div></div>`;
    const [est, lo, hi, mde] = d, cls = sigCls(c);
    // 小条刻度：覆盖区间与 ±MDE，0 居中
    const span = Math.max(Math.abs(lo), Math.abs(hi), mde || 0) * 1.15 || 1e-9, X = u => 50 + u / span * 50;
    const tag = !c.read ? `<span class="tag">不显著</span>` : `<span class="tag ${cls}">${esc(c.read)}</span>` +
      (Math.abs(c.diff) < c.mde_abs ? `<span class="tag">低于可测下限</span>` : "");
    const lbl = t.lower_is_better ? "越低越好" : "";
    return `<div class="xcard${t.group === "北极星" ? " hero" : ""}" data-tip="${esc(`${t.name}：${gLab(trt)} ${v(gt)} vs ${gLab(base)} ${v(gb)}；Δ ${dFmt(t, est)}，95% CI [${dFmt(t, lo)}, ${dFmt(t, hi)}]，可测下限 ±${dFmt(t, mde).replace(/^[+-]/, "")}；z = ${sgn(c.z, 2)}`)}">
      <div class="k">${t.group === "北极星" ? "北极星 · " : ""}${esc(t.name)}${lbl ? ` <small class="muted">${lbl}</small>` : ""}</div>
      <div class="v ${cls}">${dFmt(t, est)}</div>
      <div class="ci-bar" aria-label="95% 置信区间 ${dFmt(t, lo)} 到 ${dFmt(t, hi)}">
        <span class="mde" style="left:${X(-mde)}%;width:${X(mde) - X(-mde)}%"></span><span class="z0"></span>
        <span class="rng ${cls}" style="left:${X(lo)}%;width:${Math.max(X(hi) - X(lo), 0.8)}%"></span><span class="pt ${cls}" style="left:${X(est)}%"></span></div>
      <div class="s">[${dFmt(t, lo)}, ${dFmt(t, hi)}] · ${v(gt)} vs ${v(gb)}</div>
      <div class="tags">${tag}</div></div>`;
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
        s += `<rect class="hit" x="${LW}" y="${yy - gh / 2}" width="${W - LW}" height="${gh}" fill="transparent" data-t="${esc(`<b>${t.name}</b><br><span class="sw" style="background:${col}"></span>${gLab(g)} vs ${gLab(base)}：${sgn(c.rel * 100, 2)}%<br>95% CI [${sgn(c.rel_ci_low * 100, 2)}%, ${sgn(c.rel_ci_high * 100, 2)}%]<br>可测下限 ±${(c.mde_rel * 100).toFixed(2)}% · ${c.read || "不显著"}${t.unit !== "num" ? `<br>绝对差 ${dFmt(t, c.diff)}` : ""}`)}"/>`;
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
      s += `<rect class="hit" x="${LW}" y="${cy - rh / 2}" width="${W - LW}" height="${rh}" fill="transparent" data-t="${esc(`<b>${gLab(g)}</b>（${fmt(D(g).n)} 台设备）<br>P5 ${f2(p5)} · P25 ${f2(p25)} · P50 ${f2(p50)}<br>P75 ${f2(p75)} · P95 ${f2(p95)}<br>设备均值 ${f2(avg)}`)}"/>`;
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
  $("#metricList").innerHTML = `<table><thead><tr><th>指标</th><th>类型</th><th></th></tr></thead><tbody>` + METRICS.map(m => `<tr>
    <td class="name" style="text-align:left;white-space:normal">${esc(m.name)}<br><small class="muted">${esc(m.key)} · ${esc(m.note || "")}</small></td>
    <td>${m.kind === "quantile" ? "分位数" : "比率"}${m.preset ? ' <span class="pill">预置</span>' : ""}</td>
    <td class="acts"><button class="btn" data-edit="${m.key}">编辑</button>${m.preset ? (m.edited ? ` <span class="pill warn">已改</span>` : "") : ` <button class="btn danger" data-del="${m.key}">删除</button>`}</td></tr>`).join("") + `</tbody></table>`;
  $$("[data-del]").forEach(b => b.onclick = async () => {
    const used = await api(`/api/metrics/${b.dataset.del}/usage`);
    if (!confirm(used.length ? `「${b.dataset.del}」被 ${used.length} 个实验用到（${used.map(e => e.name).join("、")}），删了这些实验里就看不到它了。仍要删？` : `删除指标「${b.dataset.del}」？`)) return;
    await api(`/api/metrics/${b.dataset.del}`, { method: "DELETE" }); loadMetrics(); });
  $$("[data-edit]").forEach(b => b.onclick = () => editMetric(METRICS.find(m => m.key === b.dataset.edit)));
  const sideText = s => !s ? "" : `${[].concat(s.event).join("+")} ${s.agg || ""}${(s.filters || []).length ? " | " + s.filters.map(f => f.map(x => [].concat(x).join(",")).join(" ")).join(" AND ") : ""}`.trim();
  COPY.metrics = () => ({ caption: META.title + " · 指标库", header: ["key", "指标", "类型", "角色", "分子", "分母", "分子只算分母设备", "P99 封顶", "说明"],
    rows: METRICS.map(m => [m.key, m.name, m.kind === "quantile" ? "分位数" : "比率", ROLE_NAME[m.role || "process"], sideText(m.kind === "quantile" ? m.source : m.numerator),
      m.kind === "quantile" ? `${m.column} @ ${(m.quantiles || []).join(",")}` : sideText(m.denominator),
      m.numerator_within_denominator ? "是" : "", m.cap ? "是" : "", m.note || ""]) });
  if (!$("#numBox").innerHTML) buildMetricForm();
}

// 输入联想选择框：输入代码或中文名模糊匹配，点选（或 ↑↓ 回车）确认；多选的已选项显示为可删除的标签
// opts = [{ v, t }]（值 / 中文名）；返回 { get(), set(v) }，多选 get() 返回数组
let PICK_SEQ = 0;
function picker(el, opts, { multi = false, placeholder = "输入搜索", value } = {}) {
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
    let i = 0; for (const ch of s) if (ch === q[i]) i++;   // 子序列匹配：缩写也能命中
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
    if (multi) { sel = sel.includes(v) ? sel.filter(x => x !== v) : [...sel, v]; inp.value = ""; paint(); open(); inp.focus(); }
    else { sel = [v]; inp.value = ""; paint(); close(); el.dispatchEvent(new Event("change")); }
  };
  inp.addEventListener("focus", () => { if (!multi) inp.select(); open(); });
  inp.addEventListener("input", () => { hi = 0; open(); });
  inp.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); if (list.hidden) open(); hi = (hi + (ev.key === "ArrowDown" ? 1 : -1) + hits.length) % Math.max(hits.length, 1); open(); }
    else if (ev.key === "Enter") { ev.preventDefault(); if (!list.hidden && hits[hi]) pick(hits[hi].v); }
    else if (ev.key === "Escape") close();
    else if (ev.key === "Backspace" && multi && !inp.value && sel.length) { sel = sel.slice(0, -1); paint(); open(); }
  });
  list.addEventListener("mousedown", ev => { const li = ev.target.closest("li[data-v]"); if (li) { ev.preventDefault(); pick(li.dataset.v); } });
  chips.addEventListener("click", ev => { const b = ev.target.closest("button[data-v]"); if (b) { sel = sel.filter(x => x !== b.dataset.v); paint(); } });
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
  picker(el.querySelector(".ev"), META.events.map(e => ({ v: e, t: lbl(e) })), { multi: true, placeholder: "输入搜索事件名" });
  if (withAgg) picker(el.querySelector(".agg"), [{ v: "uv", t: "UV（设备去重）" }, { v: "pv", t: "PV（条数）" }, ...META.num_cols.map(c => ({ v: "sum:" + c, t: "求和 " + (lbl(c) || c) }))],
    { placeholder: "输入搜索，如 UV / 求和", value: "uv" });
  el.querySelector(".addf").onclick = () => addFilter(el.querySelector(".flts"));
}
function addFilter(box, f) {
  const cols = [...META.str_cols, ...META.num_cols, ...META.bool_cols];
  const d = document.createElement("div"); d.className = "flt-row";
  d.innerHTML = `<div class="fc"></div>
    <select class="fo" aria-label="运算符">${Object.entries(OP_TEXT).map(([o, t]) => `<option value="${o}">${o}　${t}</option>`).join("")}</select>
    <input class="fv" aria-label="值" placeholder="值；in 用逗号分隔；可写 0.9*某数值列" size="30">
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
  // 「任一满足」组合条件（如 A 列为真 或 B 列 > 阈值）表单拼不出来，只读不改
  const anyF = [m.numerator, m.denominator, m.source].some(x => x && (x.filters || []).some(y => y[0] === "any"));
  EDIT_METRIC = m.key;
  $("#mTitle").textContent = `编辑「${m.name}」`;
  f.key.value = m.key; f.key.readOnly = true;
  f.name.value = m.name; f.role.value = m.role || "process"; f.note.value = m.note || "";
  f.kind.value = m.kind || "ratio"; f.kind.onchange(); f.kind.disabled = true;
  if (m.kind === "quantile") { setSide($("#srcBox"), m.source); $("#qCol")._pick.set(m.column); f.quantiles.value = (m.quantiles || []).join(","); }
  else { setSide($("#numBox"), m.numerator); setSide($("#denBox"), m.denominator);
    f.within.checked = !!m.numerator_within_denominator; f.cap.checked = m.cap === "p99_pre"; f.lower.checked = !!m.lower_is_better; }
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
  const d = { name: f.name.value.trim(), kind: f.kind.value, role: f.role.value, note: f.note.value.trim() };
  if (d.kind === "ratio") {
    d.numerator = readSide($("#numBox"), true); d.denominator = readSide($("#denBox"), true);
    d.numerator_within_denominator = f.within.checked; d.lower_is_better = f.lower.checked;
    if (f.cap.checked) d.cap = "p99_pre";
  } else {
    d.source = readSide($("#srcBox"), false); d.column = $("#qCol")._pick.get();
    if (!d.column) throw new Error("分位数列还没选");
    d.quantiles = f.quantiles.value.split(",").map(Number).filter(x => x > 0 && x < 1);
  }
  return d;
}


// ─────────── 日志 ───────────
// 任务日志：每个任务能追到「谁触发 → 跑了哪几条 SQL → 每条的任务号 / 行数 / 耗时」，SQL 可复制去数据源复跑核对
const JOB_KIND = { funnel: "漏斗刷新", experiment: "实验回收", exp_followup: "实验补算（累计行 / By天 / 分位数）" };
const JOB_ST = { queued: "排队", running: "运行中", done: "完成", failed: "失败", cancelled: "已取消" };
const JOB_SQL = {};
const cleanErr = (s, n) => String(s || "").replace(/[│█▀▄╭╮╰╯─]+/g, " ").replace(/\s+/g, " ").slice(0, n);
let EXP_NAMES = {};
function jobTarget(j) {
  const p = (() => { try { return JSON.parse(j.params); } catch (_) { return {}; } })();
  if (p.exp_id != null) {
    const name = EXP_NAMES[p.exp_id] || `实验 #${p.exp_id}（已删除）`;
    const inc = [p.only && p.only.length ? "指标 " + p.only.map(metricName).join("、") : "", p.only_daily && p.only_daily.length ? "By天 " + p.only_daily.map(metricName).join("、") : ""].filter(Boolean).join("；");
    return `<a href="#exp" data-exp="${p.exp_id}">${esc(name)}</a>${p.only ? ` · 增量补算${inc ? "：" + esc(inc) : ""}` : ""}`;
  }
  if (p.start) return `${esc((p.views || []).map(v => { const [a, b] = v.split(":"); return scopeLabel(a) + (b ? " · " + subLabel(b) : ""); }).join(" / ") || "全部范围")} · ${toISO(p.start)} ~ ${toISO(p.end)}`;
  return esc(j.params);
}
async function loadJobs() {
  const [js, exps] = await Promise.all([api("/api/jobs"), api("/api/experiments").catch(() => [])]);
  if (!METRICS.length) METRICS = await api("/api/metrics").catch(() => []);
  EXP_NAMES = Object.fromEntries(exps.map(e => [e.id, e.name]));
  $("#schedInfo").textContent = `每日 ${META.daily_at} 自动刷新近 ${META.backfill_days} 天缺失 / 不完整的日期，并续跑未冻结的实验`;
  const dur = j => j.started_at ? Math.round((j.finished_at || Date.now() / 1000) - j.started_at) : null;
  const ds = s => s == null ? "" : s >= 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${s} 秒`;
  $("#jobList").innerHTML = js.length ? `<div class="scroll"><table class="jtab"><thead><tr><th>任务号</th><th>类型</th><th>对象</th><th>状态</th><th>提交时间</th><th>耗时</th><th>明细</th></tr></thead><tbody>` +
    js.map(j => `<tr class="jrow" data-id="${j.id}"><td>#${j.id}</td><td>${esc(JOB_KIND[j.kind] || j.kind)}</td><td class="tgt">${jobTarget(j)}</td>
      <td><span class="pill ${j.status}">${JOB_ST[j.status] || j.status}</span>${["queued", "running"].includes(j.status) ? ` <button type="button" class="btn copy danger" data-cancel="${j.id}" title="${j.status === "running" ? "停在当前这条查询：已落库的保留，没跑的不再跑" : "从队列里拿掉，不跑"}">取消</button>` : ""}</td>
      <td>${new Date(j.created_at * 1000).toLocaleString()}</td><td>${ds(dur(j))}</td>
      <td><button type="button" class="btn copy" data-q="${j.id}">${j.n_queries ? `${j.n_queries} 条 SQL` : "日志"} ▸</button></td></tr>
      <tr class="jdet" id="jd${j.id}" hidden><td colspan="7"></td></tr>
      ${j.error ? `<tr class="jerr"><td colspan="7"><div class="notice ${j.status === "cancelled" ? "info" : "crit"}"><b>${j.status === "cancelled" ? "已取消" : "错误"}</b>${esc(cleanErr(j.error, 400))}</div></td></tr>` : ""}`).join("") + `</tbody></table></div>
    <p class="foot">「明细」= 该任务实际提交给数据源的查询，含任务号（自定义命令通道返回 task_id 时显示）、返回行数、耗时；复制后可直接到数据源复跑核对。</p>` : `<p class="muted">暂无任务</p>`;
  $$("#jobList [data-q]").forEach(b => b.onclick = () => toggleJob(+b.dataset.q, b, js.find(j => j.id === +b.dataset.q)));
  // 取消：排队中的直接拿掉；运行中的停在当前这条查询，已落库的保留
  $$("#jobList [data-cancel]").forEach(b => b.onclick = async () => {
    const id = +b.dataset.cancel, j = js.find(x => x.id === id);
    if (!confirm(`取消任务 #${id}（${JOB_KIND[j.kind] || j.kind}）？${j.status === "running" ? "\n已经拉回来的数据保留，剩下的不再跑。" : ""}`)) return;
    b.disabled = true;
    try { const r = await api(`/api/jobs/${id}/cancel`, { method: "POST", body: {} }); flash(b, r.state === "cancelling" ? "取消中…" : "已取消", true); }
    catch (e) { flash(b, e.message, false); }
    setTimeout(loadJobs, 800);
  });
  $$("#jobList [data-exp]").forEach(a => a.onclick = () => { CUR_EXP = +a.dataset.exp; });
  if (js.some(j => ["queued", "running"].includes(j.status)) && location.hash === "#logs") setTimeout(loadJobs, 4000);
}
async function toggleJob(id, btn, j) {
  const row = $("#jd" + id);
  if (!row.hidden) { row.hidden = true; btn.textContent = btn.textContent.replace("▾", "▸"); return; }
  const qs = await api(`/api/jobs/${id}/queries`);
  qs.forEach(q => JOB_SQL[q.id] = q.sql);
  row.querySelector("td").innerHTML = `<div class="jq">${qs.length ? "" : `<p class="muted">${j.status === "queued" ? "还没开始跑" : "这个任务没有 SQL 记录"}</p>`}${qs.map((q, i) => `<div class="jq-item">
      <div class="jq-h"><b>${i + 1}. ${esc(q.label)}</b>
        <span class="muted">${q.task_id ? `任务号 <code>${esc(q.task_id)}</code> · ` : ""}${q.error ? `<span class="warn-t">失败</span>` : `${fmt(q.rows)} 行`} · ${q.seconds == null ? "—" : (+q.seconds).toFixed(1)} 秒</span>
        <button type="button" class="btn copy" data-sql="${q.id}">复制 SQL</button></div>
      ${q.error ? `<div class="notice crit"><b>错误</b>${esc(q.error)}</div>` : ""}
      <details><summary class="muted">查看 SQL</summary><pre class="sql">${esc(q.sql)}</pre></details></div>`).join("")}
    <details ${qs.length ? "" : "open"}><summary class="muted">运行日志</summary><pre>${esc(j.log || "")}</pre></details></div>`;
  $$("[data-sql]", row).forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText(JOB_SQL[b.dataset.sql]); flash(b, "已复制", true); } catch (_) { flash(b, "复制失败", false); } });
  row.hidden = false; btn.textContent = btn.textContent.replace("▸", "▾");
}

// ─────────── 启动 ───────────
// 顶部健康提醒：凭据缺失 / 疑似失效、当前失败的实验（常驻）、近 24 小时失败且未被后续成功覆盖的任务
const SRC_NAME = { duckdb: "本地 DuckDB 文件", sqlalchemy: "数据库连接（.env 里的连接串）", command: "取数命令" };
async function loadHealth() {
  let h;
  try { h = await api("/api/health"); } catch (_) { return; }
  let out = "";
  if (!h.auth_ok || h.login_suspect)
    out += `<div class="notice crit"><b>${SRC_NAME[META.source_type] || "数据源"}${h.auth_ok ? "凭据可能已失效" : "凭据未配置"}</b>取数会失败。${esc(h.auth_hint || "按 README「配置数据源」检查 .env / 登录态后重启看板")}，再到「任务日志」重跑失败的任务。</div>`;
  const fe = h.failed_exps || [];
  if (fe.length)
    out += `<div class="notice crit"><b>${fe.length} 个实验处于失败状态</b>${fe.map(e => `<a href="#exp" data-exp="${e.id}">${esc(e.name)}</a>（${/取消/.test(e.error) ? "已取消，已停止每日续跑" : e.partial ? "部分补算失败" : "跑数失败，已停止每日续跑"}）`).join("；")}。打开实验点「重跑」；这条提醒在重跑成功前一直显示。</div>`;
  // 手动关掉的失败提醒：记住这批任务号（存本浏览器），之后有新的失败任务会重新出现
  let shut = [];
  try { shut = JSON.parse(localStorage.getItem("hideFails") || "[]"); } catch (_) { }
  const fails = h.fails.filter(f => !shut.includes(f.id));
  if (fails.length)
    out += `<div class="notice warn"><b>近 24 小时 ${fails.length} 个任务失败</b>${fails.slice(0, 3).map(f => `#${f.id} ${JOB_KIND[f.kind] || f.kind}：${esc(cleanErr(f.error, 90))}`).join("；")}${fails.length > 3 ? " 等" : ""}。<a href="#logs">去任务日志</a>
      <button type="button" class="notice-x" id="hideFails" aria-label="关闭这条提醒" title="关闭；之后有新的失败任务会再提醒">×</button></div>`;
  $("#health").innerHTML = out;
  if ($("#hideFails")) $("#hideFails").onclick = () => {
    try { localStorage.setItem("hideFails", JSON.stringify([...new Set([...shut, ...h.fails.map(f => f.id)])].slice(-200))); } catch (_) { }
    loadHealth();
  };
  $$("#health [data-exp]").forEach(a => a.onclick = () => { CUR_EXP = +a.dataset.exp; if (location.hash === "#exp") loadExpList(); });
}

function rerenderFunnel() { if (FUNNEL) renderFunnel(+toNum($("#fStart").value), +toNum($("#fEnd").value)); }
function setSubPicker() {
  const el = $("#fSub"), sub = META.subscope;
  el.hidden = !sub;
  if (!sub) return;
  picker(el, [{ v: "", t: `全部${sub.label}` }, ...sub.values.map(v => ({ v, t: subLabel(v) }))], { placeholder: `搜${sub.label}`, value: S.sub });
  el.onchange = () => { S.sub = el._pick.get() || ""; loadFunnel(); };
}

(async function init() {
  META = await api("/api/meta");
  document.title = META.title; $("#brandTitle").textContent = META.title;
  S.scope = META.scopes[0].key; S.tier = META.tiers[0].key;
  loadHealth(); setInterval(loadHealth, 60000);
  $("#sideFoot").innerHTML = `${META.auth_ok ? "数据源凭据 ✓" : '<span class="flag">数据源凭据未配置</span>'}<br>每日 ${META.daily_at} 自动刷新`;
  $("#scopeSeg").innerHTML = META.scopes.map(s => `<button data-s="${esc(s.key)}" class="${s.key === S.scope ? "on" : ""}">${esc(s.label)}</button>`).join("");
  $$("#scopeSeg button").forEach(b => b.onclick = () => { S.scope = b.dataset.s; S.sub = ""; $$("#scopeSeg button").forEach(x => x.classList.toggle("on", x === b)); setSubPicker(); loadFunnel(); });
  $("#scopeSeg").hidden = META.scopes.length < 2;
  setSubPicker();
  $("#tierSeg").innerHTML = META.tiers.map(t => `<button data-tier="${esc(t.key)}" class="${t.key === S.tier ? "on" : ""}">${esc(t.label)}</button>`).join("");
  $("#tierSeg").hidden = META.tiers.length < 2;
  $$("#tierSeg button").forEach(b => b.onclick = () => { S.tier = b.dataset.tier; $$("#tierSeg button").forEach(x => x.classList.toggle("on", x === b)); rerenderFunnel(); });
  $("#fEnd").value = toISO(META.yesterday); $("#fStart").value = toISO(addDays(META.yesterday, -6));
  $("#fStart").onchange = $("#fEnd").onchange = loadFunnel;
  $("#fRefresh").onclick = refreshFunnel;
  $("#funnelFoot").textContent = (META.notes && META.notes.funnel_foot) || "";
  $("#newExpBtn").onclick = ev => openNewExp(ev);
  $("#expCancel").onclick = () => { $("#newExp").hidden = true; EDIT_EXP = null; };
  $("#addGrp").onclick = () => addGrpRow();
  $("#expForm").onsubmit = submitExp;
  if (META.subscope) {
    $("#subList").onchange = syncSubs;
    $("#subClr").onclick = () => { $$("[name=sb]").forEach(x => x.checked = false); syncSubs(); };
    $("#subQ").oninput = () => { const q = $("#subQ").value.trim().toUpperCase(); $$("#subList label").forEach(l => l.hidden = !!q && !l.dataset.k.toUpperCase().includes(q)); };
    document.addEventListener("click", ev => { const dd = $("#subDD"); if (dd.open && !dd.contains(ev.target)) dd.open = false; });
  }
  document.addEventListener("mouseover", ev => { const t = ev.target.closest("[data-tip]"); if (t) showTip(esc(t.dataset.tip), ev); });
  document.addEventListener("mouseout", ev => { if (ev.target.closest("[data-tip]")) hideTip(); });
  let rz; addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if (location.hash === "#funnel" || !location.hash) rerenderFunnel(); }, 200); });
  route();
})();
