(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = v => v == null ? "-" : "R$ " + Number(v).toFixed(2);
const pct = (v, d = 0) => v == null ? "-" : (Number(v) * 100).toFixed(d) + "%";
const num = (v, d = 1) => v == null ? "-" : Number(v).toFixed(d);
const CARRIER_VAR = { POSTALBR: "--s1", RAPIDOSUL: "--s2", TRANSNORDESTE: "--s3", AMAZONLOG: "--s4", CENTRALEXPRESS: "--s5", ECOFREIGHT: "--s6", MEGACARGO: "--s7" };
const cvar = id => `var(${CARRIER_VAR[id] || "--text-3"})`;
const CRIT = ["cost", "eta", "late_risk", "reliability", "capacity"];
const CRIT_LABEL = { cost: "Cost", eta: "Delivery time", late_risk: "Late risk", reliability: "Reliability", capacity: "Capacity" };
const SEV = { normal: ["✓", "Normal"], warning: ["▲", "Warning"], high_risk: ["◆", "High risk"], critical: ["✖", "Critical"] };
const GRID = { RR: [3, 0], AP: [5, 0], AM: [2, 1], PA: [4, 1], MA: [5, 1], CE: [6, 1], RN: [7, 1], AC: [1, 2], RO: [2, 2], MT: [3, 2], TO: [4, 2], PI: [5, 2], PE: [6, 2], PB: [7, 2], MS: [3, 3], GO: [4, 3], DF: [5, 3], BA: [6, 3], AL: [7, 3], SP: [4, 4], MG: [5, 4], ES: [6, 4], SE: [7, 4], PR: [4, 5], RJ: [5, 5], SC: [4, 6], RS: [4, 7] };
const STATES = Object.keys(GRID).sort();

const S = { token: sessionStorage.getItem("of_token"), me: null, tab: null, timer: null, story: null };

// ---------------------------------------------------------------- api
async function api(method, path, body, headers = {}) {
  const r = await fetch(path, { method, headers: { "Content-Type": "application/json", ...(S.token ? { Authorization: "Bearer " + S.token } : {}), ...headers }, body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (r.status === 401 && S.me) { logout(); throw new Error("Session expired"); }
  if (!r.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || r.statusText);
  return data;
}
const get = p => api("GET", p), post = (p, b, h) => api("POST", p, b || {}, h);
function toast(msg, bad = false) {
  const t = document.createElement("div"); t.className = "toast" + (bad ? " bad" : ""); t.textContent = msg;
  $("#toast").appendChild(t); setTimeout(() => t.remove(), bad ? 6000 : 3500);
}
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
const chip = (sev) => { const [i, l] = SEV[sev] || SEV.normal; return `<span class="chip ${esc(sev)}"><i aria-hidden="true">${i}</i>${l}</span>`; };
const cdot = id => `<span class="dot" style="background:${cvar(id)}"></span>`;
const tip = t => `data-tip="${esc(t)}"`;

// ------------------------------------------------------------ tooltip
const tipEl = $("#tip");
document.addEventListener("mouseover", e => {
  const t = e.target.closest("[data-tip]");
  if (!t) return tipEl.classList.add("hidden");
  tipEl.textContent = t.dataset.tip; tipEl.classList.remove("hidden");
});
document.addEventListener("mousemove", e => { tipEl.style.left = Math.min(e.clientX + 14, innerWidth - 270) + "px"; tipEl.style.top = e.clientY + 14 + "px"; });

// --------------------------------------------------------------- shell
const TABS = [
  ["tower", "Control Tower", ["admin", "ops_manager", "analyst"]],
  ["decide", "Order Decision", ["admin", "ops_manager", "seller"]],
  ["story", "Demo Story", ["admin", "ops_manager"]],
  ["whatif", "What-If Simulator", ["admin", "ops_manager", "analyst"]],
  ["ships", "Shipments", ["admin", "ops_manager", "analyst", "seller"]],
  ["ops", "Operations", ["admin", "ops_manager"]],
  ["data", "Data & Evidence", ["admin", "ops_manager", "analyst"]],
  ["audit", "Audit & Models", ["admin", "analyst"]],
];
function buildTabs() {
  $("#tabs").innerHTML = TABS.filter(t => t[2].includes(S.me.role)).map(t => `<button class="tab" data-tab="${t[0]}">${t[1]}</button>`).join("");
  $("#who").textContent = `${S.me.username} · ${S.me.role}`;
}
$("#tabs").addEventListener("click", e => { const b = e.target.closest("[data-tab]"); if (b) show(b.dataset.tab); });
function show(tab) {
  clearInterval(S.timer); S.timer = null; S.tab = tab;
  document.querySelectorAll("#tabs .tab").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  ({ tower: vTower, decide: vDecide, story: vStory, whatif: vWhatIf, ships: vShips, ops: vOps, data: vData, audit: vAudit })[tab]();
}
const view = () => $("#view");
const head = (title, sub, extra = "") => `<header class="top"><div><h2>${esc(title)}</h2><div class="sub">${esc(sub)}</div></div><div class="grow"></div>${extra}</header>`;
const wrap = async (fn) => { try { await fn(); } catch (e) { toast(e.message, true); } };

// -------------------------------------------------------------- login
$("#loginForm").addEventListener("submit", async e => {
  e.preventDefault(); $("#loginErr").textContent = "";
  try {
    const r = await api("POST", "/auth/login", { username: $("#u").value, password: $("#p").value });
    S.token = r.access_token; sessionStorage.setItem("of_token", S.token); await boot();
  } catch (err) { $("#loginErr").textContent = err.message; }
});
function logout() { S.token = null; S.me = null; sessionStorage.removeItem("of_token"); clearInterval(S.timer); $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }
$("#logout").onclick = logout;
$("#themeBtn").onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = cur === "dark" ? "light" : "dark";
  try { localStorage.setItem("of_theme", document.documentElement.dataset.theme); } catch (_) { /* storage may be blocked */ }
};
try { const t = localStorage.getItem("of_theme"); if (t) document.documentElement.dataset.theme = t; } catch (_) { /* ignore */ }

async function boot() {
  if (!S.token) return;
  try { S.me = await get("/auth/me"); } catch (_) { return logout(); }
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  buildTabs(); show(TABS.find(t => t[2].includes(S.me.role))[0]);
}

// ------------------------------------------------------- control tower
function seqColor(t) { const p = Math.round(Math.max(0, Math.min(1, t)) * 100); return `color-mix(in srgb, var(--seq-700) ${p}%, var(--seq-100))`; }
function tileMap(geo, metric) {
  const val = g => metric === "orders" ? g.orders : metric === "risk" ? (g.late_rate_pct ?? g.avg_risk_pct) : g.avg_cost;
  const by = Object.fromEntries(geo.map(g => [g.state, g]));
  const vals = geo.map(val); const max = Math.max(...vals, 1e-9), min = Math.min(...vals, 0);
  let cells = ""; const grid = {};
  for (const [st, [c, r]] of Object.entries(GRID)) grid[r + "," + c] = st;
  for (let r = 0; r < 8; r++) for (let c = 1; c < 8 + 1; c++) {
    const st = grid[r + "," + c]; if (c > 7) continue;
    if (!st) { cells += `<div class="tile empty"></div>`; continue; }
    const g = by[st];
    if (!g) { cells += `<div class="tile" style="background:var(--surface-2);color:var(--text-3)" ${tip(st + ": no orders")}>${st}</div>`; continue; }
    const t = (val(g) - min) / (max - min || 1);
    cells += `<div class="tile" tabindex="0" style="background:${seqColor(0.08 + 0.92 * t)};color:${t > .45 ? "#fff" : "var(--navy)"}" ${tip(`${st}\nOrders: ${g.orders}\nAvg freight: ${money(g.avg_cost)}\nLate risk: ${g.avg_risk_pct}%${g.late_rate_pct != null ? "\nRealised late: " + g.late_rate_pct + "%" : ""}`)}>${st}</div>`;
  }
  return `<div class="tiles" style="grid-template-columns:repeat(7,1fr)">${cells}</div><div class="ramp">low<span class="sw" style="background:${seqColor(.08)}"></span><span class="sw" style="background:${seqColor(.5)}"></span><span class="sw" style="background:${seqColor(1)}"></span>high · ${metric === "orders" ? "orders" : metric === "risk" ? "late %" : "avg freight"}</div>`;
}
async function vTower() {
  view().innerHTML = head("Control Tower", "Network health, at-risk shipments and where cost and delay concentrate", `<button class="btn ghost" id="rf">Refresh</button>`) + `<div id="tw">Loading…</div>`;
  $("#rf").onclick = () => wrap(load);
  async function load() {
    const [k, cars, routes, geo, sellers, evs] = await Promise.all([get("/v1/control-tower"), get("/v1/control-tower/carriers"), get("/v1/control-tower/routes"), get("/v1/control-tower/geo"), get("/v1/control-tower/sellers"), get("/v1/control-tower/events")]);
    const hs = k.network_health >= 80 ? "normal" : k.network_health >= 60 ? "warning" : "critical";
    const kp = (l, v, d = "", cls = "") => `<div class="card kpi ${cls}"><div class="l">${l}</div><div class="v">${v}</div><div class="d">${d}</div></div>`;
    const metric = window.__tileMetric || "risk";
        const rt = (title, list, col) => `<div><b>${title}</b><table><tbody>${list.map(r => `<tr><td class="mono">${esc(r.route)}</td><td class="n">${col(r)}</td><td class="n" style="color:var(--text-3)">${r.orders} ord</td></tr>`).join("") || `<tr><td>No data</td></tr>`}</tbody></table></div>`;
    $("#tw").innerHTML = `
    <div class="grid g-kpi">
      ${kp("Network health", k.network_health + `<span style="font-size:14px">/100</span> ` + chip(hs), `${k.carriers_healthy}/${k.carriers_total} carriers healthy`, "health")}
      ${kp("Active shipments", k.active_shipments, `${k.total_shipments} total · ${k.delivered} delivered`)}
      ${kp("At-risk shipments", k.at_risk_shipments, `${k.warning_shipments} warnings`)}
      ${kp("Expected on-time", k.expected_on_time_pct == null ? "-" : k.expected_on_time_pct + "%", k.realised_on_time_pct == null ? "no deliveries yet" : `realised ${k.realised_on_time_pct}%`)}
      ${kp("Avg freight cost", money(k.avg_freight_cost), "per shipment")}
      ${kp("Avg predicted ETA", k.avg_predicted_eta_days == null ? "-" : k.avg_predicted_eta_days + " d", "active shipments")}
      ${kp("Carrier SLA breaches", k.carrier_sla_breaches, "promise exceeded")}
      ${kp("Estimated savings", money(k.estimated_savings.amount), `${k.estimated_savings.pct_of_baseline}% vs median feasible plan`)}
      ${kp("Re-optimisations", k.reoptimizations, `${k.queued_bookings} bookings queued`)}
      ${kp("Review score (est.)", k.expected_review_score ?? "-", "from delivered on-time/late mix")}
    </div>
    <div class="grid g-main" style="margin-top:14px;grid-template-columns:minmax(0,1.9fr) minmax(0,1fr)">
      <div class="card"><h3>Carrier performance</h3><div class="hint">Colour identifies the carrier everywhere in the product.</div>
        <div class="scroll"><table><thead><tr><th>Carrier</th><th class="n">Active</th><th class="n">On-time</th><th class="n">Avg cost</th><th class="n">Actual days</th><th class="n">Promise err.</th><th class="n">Fail</th><th class="n">Capacity</th><th>Health</th></tr></thead><tbody>
        ${cars.map(c => `<tr><td>${cdot(c.carrier_id)}${esc(c.name)}</td><td class="n">${c.active_shipments}</td><td class="n">${c.on_time_pct == null ? "-" : c.on_time_pct + "%"}</td>
          <td class="n">${money(c.avg_cost)}</td>
          <td class="n">${num(c.avg_actual_days, 2)}</td><td class="n">${num(c.promise_error_days, 2)}</td><td class="n">${c.failure_rate_pct}%</td><td class="n">${c.capacity_free_pct}%</td>
          <td>${c.outage || c.breaker !== "closed" ? `<span class="chip critical"><i>✖</i>${c.outage ? "Outage" : esc(c.breaker)}</span>` : c.capacity_free_pct <= 0 ? `<span class="chip warning"><i>▲</i>Full</span>` : `<span class="chip normal"><i>✓</i>OK</span>`}</td></tr>`).join("")}
        </tbody></table></div></div>
      <div class="card"><h3>Geographic view (destination)</h3><div class="hint">Cross-state share: <b>${routes.cross_state_share_pct}%</b> of shipments</div>
        <div class="seg" id="tm" role="group" aria-label="Map metric">${[["orders", "Orders"], ["risk", "Delay"], ["cost", "Cost"]].map(([m, l]) => `<button data-m="${m}" class="${m === metric ? "on" : ""}">${l}</button>`).join("")}</div>
        <div style="margin-top:10px">${tileMap(geo, metric)}</div></div>
    </div>
    <div class="card" style="margin-top:14px"><h3>Route performance</h3><div class="grid g2" style="grid-template-columns:repeat(auto-fit,minmax(230px,1fr))">
      ${rt("Most delayed", routes.most_delayed, r => (r.late_rate_pct ?? r.avg_risk_pct) + "%")}
      ${rt("Most expensive", routes.most_expensive, r => money(r.avg_cost))}
      ${rt("Highest volume", routes.highest_volume, r => r.orders)}
      ${rt("Worst overall", routes.worst, r => `${money(r.avg_cost)} · ${(r.late_rate_pct ?? r.avg_risk_pct)}%`)}</div></div>
    <div class="grid g-main" style="margin-top:14px">
      <div class="card"><h3>Seller performance</h3><div class="scroll"><table><thead><tr><th>Seller</th><th class="n">Orders</th><th class="n">Late rate</th><th class="n">Fulfilment (d)</th><th>Risk</th></tr></thead><tbody>
        ${sellers.slice(0, 8).map(s => `<tr><td>${esc(s.seller_id)}</td><td class="n">${s.orders}</td><td class="n">${s.late_rate_pct == null ? "-" : s.late_rate_pct + "%"}</td><td class="n">${s.avg_fulfilment_days}</td><td>${chip(s.risk_level === "high" ? "high_risk" : s.risk_level === "medium" ? "warning" : "normal").replace(/Normal|Warning|High risk/, s.risk_level)}</td></tr>`).join("") || "<tr><td>No sellers yet</td></tr>"}</tbody></table></div></div>
      <div class="card"><h3>Live event feed</h3><div class="scroll" style="max-height:260px">${evs.map(e => `<div style="padding:4px 0;border-bottom:1px solid var(--grid)"><b>${esc(e.type)}</b> <span class="sub">${e.shipment_id ? "#" + e.shipment_id : ""} ${e.payload?.severity ? chip(e.payload.severity) : ""} ${esc(e.payload?.from ? e.payload.from + " → " + e.payload.to : e.payload?.action || e.payload?.kind || "")}</span></div>`).join("") || "<div class='sub'>No events yet. Seed demo data in Operations.</div>"}</div></div>
    </div>`;
    $("#tm").onclick = e => { const b = e.target.closest("[data-m]"); if (b) { window.__tileMetric = b.dataset.m; wrap(load); } };
  }
  await wrap(load);
  S.timer = setInterval(() => { if (S.tab === "tower") wrap(load); }, 8000);
}

// ---------------------------------------------------- order decision
function weightSliders(prefix, init = { cost: .3, eta: .2, late_risk: .25, reliability: .15, capacity: .1 }) {
  return `<div class="sliders" id="${prefix}">${CRIT.map(k => `<div class="row"><span>${CRIT_LABEL[k]}</span><input type="range" min="0" max="100" value="${Math.round(init[k] * 100)}" data-k="${k}" aria-label="${CRIT_LABEL[k]} weight"><b>${Math.round(init[k] * 100)}</b></div>`).join("")}</div>`;
}
function readWeights(id) {
  const w = {}; document.querySelectorAll(`#${id} input`).forEach(i => w[i.dataset.k] = +i.value / 100); return w;
}
function bindSliders(id) { $(`#${id}`).addEventListener("input", e => { if (e.target.dataset.k) e.target.nextElementSibling.textContent = e.target.value; }); }
function scatter(opts, recId, pareto) {
  const W = 520, H = 250, m = { l: 46, r: 14, t: 12, b: 34 };
  const xs = opts.map(o => o.cost), ys = opts.map(o => o.predicted_days);
  const x0 = Math.min(...xs) * .9, x1 = Math.max(...xs) * 1.08, y0 = Math.max(0, Math.min(...ys) * .85), y1 = Math.max(...ys) * 1.12 || 1;
  const X = v => m.l + (v - x0) / (x1 - x0 || 1) * (W - m.l - m.r), Y = v => H - m.b - (v - y0) / (y1 - y0 || 1) * (H - m.t - m.b);
  const grid = [0, .25, .5, .75, 1].map(f => { const yy = Y(y0 + f * (y1 - y0)); return `<line class="axis" x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}"/><text x="${m.l - 6}" y="${yy + 4}" text-anchor="end">${(y0 + f * (y1 - y0)).toFixed(1)}</text>`; }).join("");
  const xt = [0, .5, 1].map(f => `<text x="${X(x0 + f * (x1 - x0))}" y="${H - 12}" text-anchor="middle">R$${(x0 + f * (x1 - x0)).toFixed(0)}</text>`).join("");
  const pts = opts.map(o => { const isRec = o.option_id === recId; const sh = o.service[0].toUpperCase();
    return `<g ${tip(`${o.carrier_id} ${o.service} from ${o.origin_id}\n${money(o.cost)} · ${o.predicted_days}d · risk ${pct(o.late_risk)}${pareto.includes(o.option_id) ? "\nPareto-optimal" : ""}`)}>
      ${isRec ? `<circle cx="${X(o.cost)}" cy="${Y(o.predicted_days)}" r="11" fill="none" stroke="var(--text)" stroke-width="2"/>` : ""}
      <circle cx="${X(o.cost)}" cy="${Y(o.predicted_days)}" r="6.5" fill="${cvar(o.carrier_id)}" stroke="var(--surface)" stroke-width="2"/>
      <text x="${X(o.cost) + 10}" y="${Y(o.predicted_days) - 8}" style="font-size:10px">${esc(o.carrier_id.slice(0, 4))}·${sh}</text></g>`; }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Cost versus predicted delivery days for each option">${grid}${xt}<text x="${W / 2}" y="${H - 1}" text-anchor="middle" style="font-size:10px">Freight cost</text><text transform="rotate(-90)" x="${-H / 2}" y="12" text-anchor="middle" style="font-size:10px">Predicted days</text>${pts}</svg>
  <div class="legend">${[...new Set(opts.map(o => o.carrier_id))].map(c => `<span>${cdot(c)}${esc(c)}</span>`).join("")}<span>◯ recommended · label = carrier·service</span></div>`;
}
function decisionHTML(d) {
  const r = d.recommended, f = d.funnel, ex = d.explanation;
  const fs = [[f.carriers_connected, "carriers connected"], [f.carriers_serving_route, "serve this route"], [f.carriers_meeting_sla, "meet SLA"], [f.feasible_plans, "feasible plans"]];
  return `<div class="funnel">${fs.map((s, i) => `<div class="fstep"><b>${s[0]}</b><span>${s[1]}</span></div>${i < 3 ? '<span class="arrow">→</span>' : ""}`).join("")}</div>
  <div class="grid g-main">
    <div class="card"><h3>Ranked options · ${esc(d.profile)} policy</h3><div class="hint">Weights: ${CRIT.map(k => `${CRIT_LABEL[k]} ${Math.round(d.weights[k] * 100)}%`).join(" · ")}</div>
      <div class="scroll"><table><thead><tr><th>#</th><th>Plan</th><th class="n">Cost</th><th class="n">Promise</th><th class="n">Pred. ETA</th><th class="n">Late risk</th><th class="n">Reliab.</th><th class="n">Score</th></tr></thead><tbody>
      ${d.alternatives.map((o, i) => `<tr class="${o.option_id === r.option_id ? "rec" : ""}"><td>${i + 1}</td><td>${cdot(o.carrier_id)}${esc(o.carrier_id)} <span class="sub">${esc(o.service)} · ${esc(o.origin_id)}</span> ${d.pareto_ids.includes(o.option_id) ? `<span class="chip" ${tip("Pareto-optimal: nothing beats it on cost, time and risk together")}>P</span>` : ""}</td>
      <td class="n">${money(o.cost)}</td><td class="n">${o.promised_days}d</td><td class="n">${o.predicted_days}d</td><td class="n">${pct(o.late_risk)}</td><td class="n">${pct(o.reliability)}</td><td class="n">${o.score.toFixed(3)}</td></tr>`).join("")}</tbody></table></div>
      <div style="margin-top:12px">${scatter(d.alternatives, r.option_id, d.pareto_ids)}</div></div>
    <div class="grid">
      <div class="card" style="border-color:var(--accent)"><h3>${esc(ex.headline)}</h3><div class="hint">${esc(ex.funnel)}</div><b>Why was it selected?</b>
        <ul class="why">${ex.reasons.map(x => `<li>${esc(x)}</li>`).join("")}</ul>
        <div class="hint" style="margin-top:10px">Decision latency: ${d.latency_ms} ms (optimiser) · ID ${d.decision_id ?? "-"}</div>
        ${d.decision_id && S.me.role !== "analyst" ? `<button class="btn" id="bookBtn" style="margin-top:6px">Book this plan</button><div id="bookOut" style="margin-top:8px"></div>` : ""}</div>
      <div class="card"><h3>Excluded plans (${ex.excluded.length})</h3><details><summary class="sub">Show why they were removed</summary><div class="scroll" style="max-height:220px"><table><tbody>${ex.excluded.map(e => `<tr><td class="mono">${esc(e.origin)}|${esc(e.carrier)}${e.service ? "|" + esc(e.service) : ""}</td><td>${esc(e.stage)}</td><td>${esc(e.reason)}</td></tr>`).join("")}</tbody></table></div></details></div>
    </div></div>`;
}
function vDecide() {
  const st = STATES.map(s => `<option ${s === "RJ" ? "selected" : ""}>${s}</option>`).join("");
  view().innerHTML = head("Order Decision", "Feasibility → prediction → optimisation → explanation, for one order") + `
  <div class="card"><form class="form" id="of">
    <div><label>Order ID</label><input id="o_id" value="ORD-${Math.floor(Math.random() * 900000 + 100000)}" required></div>
    <div><label>Destination state</label><select id="o_dest">${st}</select></div>
    <div><label>Weight (kg)</label><input id="o_w" type="number" step="0.1" min="0.1" value="2.1"></div>
    <div><label>Customer SLA (days)</label><input id="o_sla" type="number" step="1" min="1" value="4" placeholder="none"></div>
    <div><label>Fulfil from</label><select id="o_org"><option value="">Any node with stock</option>${["SP-1", "SP-2", "RJ-1", "MG-1", "PR-1", "SC-1", "DF-1", "BA-1"].map(n => `<option>${n}</option>`).join("")}</select></div>
    <div><label>Priority</label><select id="o_pri"><option>normal</option><option>high</option></select></div>
    <div><label>Policy</label><select id="o_prof"><option value="">Active policy</option><option>balanced</option><option>economy</option><option>express</option><option>reliability</option><option>peak_season</option><option value="custom">Custom weights…</option></select></div>
    <div><label>Max late risk (%)</label><input id="o_mr" type="number" min="1" max="100" placeholder="optional"></div>
    <div style="align-self:end"><button class="btn" style="width:100%">Decide</button></div></form>
    <div id="cw" class="hidden" style="margin-top:8px;max-width:520px">${weightSliders("cws")}</div></div>
  <div id="dres" style="margin-top:14px"></div>`;
  bindSliders("cws");
  $("#o_prof").onchange = e => $("#cw").classList.toggle("hidden", e.target.value !== "custom");
  $("#of").onsubmit = e => { e.preventDefault(); wrap(async () => {
    const prof = $("#o_prof").value;
    const order = { order_id: $("#o_id").value, customer_state: $("#o_dest").value, weight_kg: +$("#o_w").value, sla_days: $("#o_sla").value ? +$("#o_sla").value : null, priority: $("#o_pri").value, seller_hint: "SP" };
    if ($("#o_org").value) order.allowed_origins = [$("#o_org").value];
    const body = { order, profile: prof && prof !== "custom" ? prof : null, custom_weights: prof === "custom" ? readWeights("cws") : null, max_late_risk: $("#o_mr").value ? +$("#o_mr").value / 100 : null };
    const d = await post("/v1/decisions", body); $("#dres").innerHTML = decisionHTML(d);
    const bb = $("#bookBtn"); if (bb) bb.onclick = () => wrap(async () => {
      bb.disabled = true; const key = uuid();
      const r = await post("/v1/shipments", { order_id: order.order_id, decision_id: d.decision_id }, { "Idempotency-Key": key });
      $("#bookOut").innerHTML = r.status === "booked" ? `<span class="chip normal"><i>✓</i>Booked</span> <span class="mono">${esc(r.tracking_code)}</span> via ${esc(r.carrier_id)}${r.fallbacks_tried?.length ? ` <span class="sub">(after ${r.fallbacks_tried.length} fallback)</span>` : ""}<div class="mono sub">${esc(r.label || "")}</div>` : `<span class="chip warning"><i>▲</i>Queued</span> ${esc(r.reason || "")}`;
    });
  }); };
}

// ---------------------------------------------------------- demo story
const STORY_STEPS = ["Customer order", "Coverage funnel", "Trade-offs", "Selection & why", "Booking", "Disruption", "Re-optimisation"];
function vStory() {
  S.story = { step: 0 }; renderStory();
}
async function renderStory() {
  const s = S.story; const id = "DEMO-" + (s.oid ||= String(Date.now()).slice(-7));
  const nav = `<div class="step-nav">${STORY_STEPS.map((n, i) => `<span class="pill ${i === s.step ? "on" : ""}">${i + 1}. ${n}</span>`).join("")}</div>`;
  let body = "";
  const next = (label, id2) => `<button class="btn" id="${id2 || "sn"}">${label}</button>`;
  if (s.step === 0) body = `<div class="card"><h3>The story</h3><p>A customer in <b>Rio de Janeiro</b> orders a 2.1 kg item held in <b>São Paulo</b>. They were promised delivery within 4 days.<br>Instead of ranking carriers by price, Olist Flow decides what is best <i>under current network conditions</i> - and re-decides when conditions change.</p>${next("▶ Place the order", "st0")}</div>`;
  if (s.step === 1) body = `<div class="card"><h3>Order ${esc(id)} · São Paulo → Rio de Janeiro</h3><p>${esc(s.d.explanation.funnel)}</p>${decisionFunnel(s.d)}${next("Compare the trade-offs →")}</div>`;
  if (s.step === 2) { const alts = s.d.alternatives; const cheap = [...alts].sort((a, b) => a.cost - b.cost)[0], fast = [...alts].sort((a, b) => a.predicted_days - b.predicted_days)[0], rec = s.d.recommended;
    const card = (t, o, note) => `<div class="card"><h3>${t}</h3><div>${cdot(o.carrier_id)}<b>${esc(o.carrier_id)}</b> · ${esc(o.service)}</div><div class="kpi" style="padding:6px 0"><div class="v">${money(o.cost)}</div></div><div class="sub">ETA ${o.predicted_days}d · late risk ${pct(o.late_risk)} · reliability ${pct(o.reliability)}</div><p class="sub">${note}</p></div>`;
    body = `<div class="grid g3">${card("Cheapest", cheap, cheap.option_id === rec.option_id ? "Also the recommended plan." : `Saves money but carries ${pct(cheap.late_risk)} risk of missing the SLA.`)}${card("Recommended", rec, "Best trade-off for the active policy.")}${card("Fastest", fast, fast.option_id === rec.option_id ? "Also the recommended plan." : `Fastest at ${money(fast.cost)}, ${Math.round((fast.cost / rec.cost - 1) * 100)}% more than the recommendation.`)}</div><div style="margin-top:14px">${next("Why this one? →")}</div>`; }
  if (s.step === 3) body = `<div class="card" style="border-color:var(--accent)"><h3>${esc(s.d.explanation.headline)}</h3><b>The system explains why it was selected</b><ul class="why">${s.d.explanation.reasons.map(x => `<li>${esc(x)}</li>`).join("")}</ul>${next("Book the shipment →")}</div>`;
  if (s.step === 4) body = `<div class="card"><h3>Booked</h3><p><span class="chip normal"><i>✓</i>Booked</span> tracking <span class="mono">${esc(s.b.tracking_code)}</span> with ${cdot(s.b.carrier_id)}<b>${esc(s.b.carrier_id)}</b>. Sending the same request again returns this shipment - it cannot double-book (idempotency key + one active shipment per order).</p>${next("⚡ Something goes wrong…")}</div>`;
  if (s.step === 5) body = `<div class="card"><h3>Event: ${esc(s.b.carrier_id)} capacity becomes unavailable</h3><p>The carrier's pickup capacity is exhausted before our parcel is collected. A static ranking would leave the parcel stuck.</p>${next("Re-optimise…", "st5")}</div>`;
  if (s.step === 6) { const c = s.rc;
    body = c ? `<div class="card" style="border-color:var(--accent)"><h3>Re-optimised: ${cdot(c.from.carrier)}${esc(c.from.carrier)} → ${cdot(c.to.carrier)}${esc(c.to.carrier)}</h3><p>Current plan is no longer feasible. <b>${esc(c.to.carrier)}</b> (${esc(c.to.service)} from ${esc(c.to.origin)}) selected as next best feasible option at ${money(c.to.cost)}. Old carrier's booking was ${"cancelled or queued for cancellation"}.</p><ul class="why">${c.explanation.reasons.slice(0, 5).map(x => `<li>${esc(x)}</li>`).join("")}</ul><p class="sub"><b>This is the point:</b> no fixed carrier ranking - a system that decides from the current state of the network and re-decides when it changes.</p><button class="btn ghost" id="stRestore">Restore carrier &amp; restart</button></div>` : `<div class="card"><h3>No automatic change</h3><p>The system could not find a better plan.</p><button class="btn ghost" id="stRestore">Restore carrier &amp; restart</button></div>`; }
  view().innerHTML = head("Demo Story", "A guided run through the real pipeline: nothing on this page is mocked") + nav + body;
  const go = fn => () => wrap(async () => { await fn(); renderStory(); });
  const adv = () => { s.step++; };
  if ($("#st0")) $("#st0").onclick = go(async () => { s.d = await post("/v1/decisions", { order: { order_id: id, customer_state: "RJ", weight_kg: 2.1, sla_days: 4, allowed_origins: ["SP-1"], seller_hint: "SP", seller_id: "S001" }, profile: "balanced" }); adv(); });
  if ($("#sn")) $("#sn").onclick = go(async () => { if (s.step === 3) { s.b = await post("/v1/shipments", { order_id: id, decision_id: s.d.decision_id }, { "Idempotency-Key": uuid() }); if (s.b.status !== "booked") throw new Error("Booking queued: " + (s.b.reason || "")); } adv(); });
  if ($("#st5")) $("#st5").onclick = go(async () => { const r = await post("/v1/ops/disrupt", { carrier_id: s.b.carrier_id, kind: "capacity" }); s.rc = r.reoptimized.find(x => x.order_id === id) || null; adv(); });
  if ($("#stRestore")) $("#stRestore").onclick = go(async () => { await post("/v1/ops/disrupt", { carrier_id: s.b.carrier_id, kind: "clear" }); S.story = { step: 0 }; });
}
function decisionFunnel(d) { const f = d.funnel; const fs = [[f.carriers_connected, "carriers connected"], [f.carriers_serving_route, "serve this route"], [f.carriers_meeting_sla, "meet SLA"], [f.feasible_plans, "feasible plans"]]; return `<div class="funnel">${fs.map((x, i) => `<div class="fstep"><b>${x[0]}</b><span>${x[1]}</span></div>${i < 3 ? '<span class="arrow">→</span>' : ""}`).join("")}</div>`; }

// -------------------------------------------------------------- what-if
function mixBars(mix, total) {
  const ids = Object.keys(mix); if (!ids.length) return "<div class='sub'>No shipments</div>";
  return ids.map(c => `<div style="display:grid;grid-template-columns:150px 1fr 46px;gap:8px;align-items:center;margin:3px 0;white-space:nowrap" ${tip(`${c}: ${mix[c]} orders (${(mix[c] / total * 100).toFixed(0)}%)`)}><span>${cdot(c)}${esc(c)}</span><span><span class="bar" style="width:${Math.max(2, mix[c] / total * 100)}%;background:${cvar(c)}"></span></span><span class="n" style="text-align:right">${mix[c]}</span></div>`).join("");
}
function dlt(v, unit, goodDown = true) { if (v == null) return "-"; const cls = v === 0 ? "" : ((v > 0) === goodDown ? "up" : "down"); return `<span class="delta ${cls}">${v > 0 ? "+" : ""}${v}${unit}</span>`; }
async function vWhatIf() {
  const presets = await get("/v1/simulate/presets").catch(() => ({}));
  view().innerHTML = head("What-If Simulator", "Digital-twin: test a policy or a shock before changing anything in production") + `
  <div class="grid g-main"><div class="card"><h3>Ready-made scenarios</h3><div class="hint">Compared against a Balanced-policy baseline on the same synthetic order stream.</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">${Object.entries(presets).map(([k, l]) => `<button class="btn ghost sm" data-p="${k}">${esc(l)}</button>`).join("")}</div>
    <div style="margin-top:14px"><label>Orders in window</label><input id="wn" type="number" min="50" max="3000" value="500" style="max-width:140px"></div></div>
  <div class="card"><h3>Custom policy trade-off</h3><div class="hint">"What if cost mattered more?"</div>${weightSliders("wws")}<button class="btn sm" id="wcustom" style="margin-top:8px">Simulate these weights</button></div></div>
  <div id="wres" style="margin-top:14px"></div>`;
  bindSliders("wws");
  const run = (promise) => wrap(async () => { $("#wres").innerHTML = "<div class='card'>Simulating…</div>"; const r = await promise(); $("#wres").innerHTML = whatIfHTML(r); });
  view().onclick = e => { const b = e.target.closest("[data-p]"); if (b) run(() => post(`/v1/simulate/preset/${b.dataset.p}?n_orders=${+$("#wn").value || 500}`)); };
  $("#wcustom").onclick = () => run(async () => { const r = await post("/v1/simulate/what-if", { scenario: { name: "Custom weights", custom_weights: readWeights("wws"), n_orders: +$("#wn").value || 500 } }); r.label = "Custom policy weights"; return r; });
}
function whatIfHTML(r) {
  const b = r.baseline, a = r.scenario, i = r.impact;
  const row = (l, x, y, f = v => v) => `<tr><td>${l}</td><td class="n">${f(x)}</td><td class="n">${f(y)}</td></tr>`;
  return `<div class="card"><h3>${esc(r.label || a.scenario)}: impact vs baseline</h3><div class="grid g-kpi">
    <div class="card kpi"><div class="l">Avg freight cost</div><div class="v">${dlt(i.avg_cost_pct, "%")}</div><div class="d">${money(b.avg_cost)} → ${money(a.avg_cost)}</div></div>
    <div class="card kpi"><div class="l">Late rate</div><div class="v">${dlt(i.late_rate_pts, " pts")}</div><div class="d">${pct(b.expected_late_rate, 1)} → ${pct(a.expected_late_rate, 1)}</div></div>
    <div class="card kpi"><div class="l">Avg delivery time</div><div class="v">${dlt(i.avg_days_delta, " d")}</div><div class="d">${b.avg_predicted_days}d → ${a.avg_predicted_days}d</div></div>
    <div class="card kpi"><div class="l">Unserved orders</div><div class="v">${dlt(i.unserved_delta, "")}</div><div class="d">capacity exhausted</div></div>
    <div class="card kpi"><div class="l">Review score (est.)</div><div class="v">${dlt(i.expected_review_delta, "", false)}</div><div class="d">${b.expected_review} → ${a.expected_review}</div></div></div></div>
  <div class="grid g2" style="margin-top:14px"><div class="card"><h3>Baseline</h3><div class="hint">${esc(b.profile)} · ${b.orders} orders</div>${mixBars(b.carrier_mix, b.served || 1)}</div>
  <div class="card"><h3>Scenario</h3><div class="hint">${esc(a.profile)} · ${a.orders} orders · strategy ${esc(a.strategy)}</div>${mixBars(a.carrier_mix, a.served || 1)}</div></div>
  <div class="card" style="margin-top:14px"><h3>Detail</h3><table><thead><tr><th></th><th class="n">Baseline</th><th class="n">Scenario</th></tr></thead><tbody>
  ${row("Total freight cost", b.total_cost, a.total_cost, money)}${row("On-time rate", b.on_time_rate, a.on_time_rate, v => pct(v, 1))}${row("Served / total", `${b.served}/${b.orders}`, `${a.served}/${a.orders}`)}
  ${row("Peak carrier utilisation", Math.max(0, ...Object.values(b.carrier_utilisation)), Math.max(0, ...Object.values(a.carrier_utilisation)), v => pct(v))}</tbody></table>
  <div class="hint" style="margin-top:8px">Unserved orders are counted as late. Simulated on synthetic orders shaped like the Olist data; treat absolute values as directional, deltas as the signal.</div></div>`;
}

// ------------------------------------------------------------ shipments
async function vShips() {
  view().innerHTML = head("Shipments", "Live tracking and exception status", `<div class="seg" id="sf">${[["", "All"], ["1", "Active"], ["risk", "At risk"]].map(([v, l]) => `<button data-f="${v}" class="${v === (window.__sf ?? "") ? "on" : ""}">${l}</button>`).join("")}</div>`) + `<div class="grid g-main"><div class="card scroll" id="sl">Loading…</div><div class="card" id="sd"><div class="sub">Select a shipment to see its timeline.</div></div></div>`;
  $("#sf").onclick = e => { const b = e.target.closest("[data-f]"); if (b) { window.__sf = b.dataset.f; vShips(); } };
  async function load() {
    const f = window.__sf ?? ""; const q = f === "1" ? "?active=true" : "";
    let items = (await get("/v1/shipments" + q + (q ? "&" : "?") + "limit=100")).items;
    if (f === "risk") items = items.filter(s => ["high_risk", "critical"].includes(s.severity));
    $("#sl").innerHTML = `<table><thead><tr><th>#</th><th>Order</th><th>Route</th><th>Carrier</th><th>Status</th><th>Health</th><th class="n">Cost</th><th class="n">Risk</th></tr></thead><tbody>${items.map(s => `<tr class="click" data-id="${s.shipment_id}"><td>${s.shipment_id}</td><td class="mono">${esc(s.order_id)}</td><td class="mono">${esc(s.seller_state)}→${esc(s.dest_state)}</td><td>${cdot(s.carrier_id)}${esc(s.carrier_id)}</td><td>${esc(s.status)}${s.replan_count ? ` <span class="chip" ${tip("Re-optimised " + s.replan_count + "×")}>↻${s.replan_count}</span>` : ""}</td><td>${chip(s.severity)}</td><td class="n">${money(s.cost)}</td><td class="n">${pct(s.late_risk)}</td></tr>`).join("") || "<tr><td>No shipments</td></tr>"}</tbody></table>`;
  }
  $("#sl").onclick = e => { const tr = e.target.closest("[data-id]"); if (tr) wrap(() => detail(+tr.dataset.id)); };
  async function detail(id) {
    const s = await get("/v1/shipments/" + id);
    const evClass = t => /Failed|Exception/.test(t) ? "x" : /Reoptimized|Disruption/.test(t) ? "r" : "";
    $("#sd").innerHTML = `<h3>Shipment #${s.shipment_id} ${chip(s.severity)}</h3><div class="sub">${esc(s.order_id)} · ${cdot(s.carrier_id)}${esc(s.carrier_id)} ${esc(s.service)} from ${esc(s.origin_id)} · <span class="mono">${esc(s.tracking_code)}</span></div>
      <p>Promised <b>${s.promised_days}d</b> · predicted <b>${s.predicted_days}d</b>${s.revised_eta ? ` · revised ETA <b>${s.revised_eta}d</b>` : ""} · late risk <b>${pct(s.late_risk)}</b></p>
      ${s.explanation ? `<details><summary class="sub">Why this plan</summary><ul class="why">${s.explanation.reasons.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>` : ""}
      <div class="timeline" style="margin-top:10px">${s.events.map(e => `<div class="ev ${evClass(e.type)}"><b>${esc(e.type)}</b> <span class="sub">day ${num(e.day, 1)} ${e.payload?.findings ? "· " + esc(e.payload.findings.join(", ")) : ""}${e.payload?.action ? "· " + esc(e.payload.action) : ""}${e.payload?.from ? "· " + esc(e.payload.from + " → " + e.payload.to) : ""}</span></div>`).join("")}</div>
      ${["admin", "ops_manager"].includes(S.me.role) && s.active ? `<button class="btn ghost sm" id="ro">Re-optimise now</button>` : ""}`;
    const ro = $("#ro"); if (ro) ro.onclick = () => wrap(async () => { const r = await post(`/v1/shipments/${id}/reoptimize`); toast(r.changed ? `Re-planned: ${r.from.carrier} → ${r.to.carrier}` : r.reason); await load(); await detail(id); });
  }
  await wrap(load); S.timer = setInterval(() => { if (S.tab === "ships") wrap(load); }, 8000);
}

// ----------------------------------------------------------- operations
async function vOps() {
  const [pol, cars] = await Promise.all([get("/v1/policy"), get("/v1/ops/carriers")]);
  const w = pol.effective_weights;
  view().innerHTML = head("Operations", "Change the decision policy, inject disruptions, advance simulated time") + `
  <div class="grid g2"><div class="card"><h3>Decision policy</h3><div class="hint">Active: <b>${esc(pol.active.profile)}</b>. Applies to new orders that don't specify their own policy.</div>
    <label>Preset</label><select id="pp">${Object.keys(pol.profiles).map(p => `<option ${p === pol.active.profile ? "selected" : ""}>${p}</option>`).join("")}<option value="custom" ${pol.active.profile === "custom" ? "selected" : ""}>custom</option></select>
    <div style="margin-top:8px">${weightSliders("pws", w)}</div><button class="btn" id="psave" style="margin-top:8px">Save policy</button></div>
  <div class="card"><h3>Simulation clock &amp; queues</h3><div class="hint">Advance time to ingest tracking events, classify exceptions and trigger automatic actions.</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">${[0.5, 1, 2, 4].map(d => `<button class="btn ghost sm" data-t="${d}">+${d} day${d > 1 ? "s" : ""}</button>`).join("")}<button class="btn ghost sm" id="pq">Process retry queue</button></div><div id="tickOut" class="sub" style="margin-top:8px"></div>
    ${S.me.role === "admin" ? `<hr style="border:0;border-top:1px solid var(--border);margin:14px 0"><b>Seed demo data</b><div style="display:flex;gap:8px;margin-top:6px"><input id="sn" type="number" min="1" max="400" value="80" style="max-width:100px"><button class="btn sm" id="seed">Generate orders &amp; run 4 days</button></div>` : ""}</div></div>
  <div class="card" style="margin-top:14px"><h3>Carrier disruptions</h3><div class="hint">Fault injection on the simulated carrier APIs. Parcels not yet picked up are re-planned automatically.</div>
    <div class="form" style="max-width:640px"><div><label>Carrier</label><select id="dc">${cars.map(c => `<option value="${c.carrier_id}">${esc(c.name)}</option>`).join("")}</select></div>
    <div><label>Event</label><select id="dk"><option value="capacity">Capacity full</option><option value="outage">API outage</option><option value="latency">High latency (timeouts)</option><option value="stall">Tracking stalled</option><option value="clear">Clear all faults</option></select></div><div style="align-self:end"><button class="btn danger" id="dgo">Inject</button></div></div>
    <div id="dOut" style="margin:10px 0"></div>
    <table><thead><tr><th>Carrier</th><th>Breaker</th><th class="n">Capacity</th><th class="n">Load</th><th>State</th></tr></thead><tbody>${cars.map(c => `<tr><td>${cdot(c.carrier_id)}${esc(c.name)}</td><td>${esc(c.breaker)}</td><td class="n">${c.capacity}</td><td class="n">${c.load}</td><td>${c.outage || c.breaker !== "closed" ? `<span class="chip critical"><i>✖</i>Down</span>` : c.capacity === 0 ? `<span class="chip warning"><i>▲</i>No capacity</span>` : `<span class="chip normal"><i>✓</i>OK</span>`}</td></tr>`).join("")}</tbody></table></div>`;
  bindSliders("pws");
  $("#pp").onchange = e => { const p = pol.profiles[e.target.value]; if (p) document.querySelectorAll("#pws input").forEach(i => { i.value = Math.round(p[i.dataset.k] * 100); i.nextElementSibling.textContent = i.value; }); };
  $("#psave").onclick = () => wrap(async () => { const sel = $("#pp").value; const custom = readWeights("pws"); const preset = pol.profiles[sel]; const same = preset && CRIT.every(k => Math.abs(preset[k] - custom[k]) < .011);
    await api("PUT", "/v1/policy", same ? { profile: sel } : { profile: "custom", custom_weights: custom }); toast("Policy saved"); vOps(); });
  view().onclick = e => { const b = e.target.closest("[data-t]"); if (b) wrap(async () => { const r = await post("/v1/ops/tick", { days: +b.dataset.t }); $("#tickOut").textContent = `Day ${r.sim_day}: ${r.events} events, ${r.delivered} delivered, ${r.escalations} escalations, ${r.reoptimized} re-optimised`; }); };
  $("#pq").onclick = () => wrap(async () => { const r = await post("/v1/ops/queue/process"); toast(`Queue: ${r.bookings_done} booked, ${r.cancellations_done} cancellations`); });
  if ($("#seed")) $("#seed").onclick = () => wrap(async () => { toast("Seeding…"); const r = await post("/v1/ops/seed-demo", { orders: +$("#sn").value, advance_days: 4, seed: Math.floor(Math.random() * 1e6) }); toast(`${r.booked} orders booked`); });
  $("#dgo").onclick = () => wrap(async () => { const r = await post("/v1/ops/disrupt", { carrier_id: $("#dc").value, kind: $("#dk").value }); $("#dOut").innerHTML = `<span class="chip ${r.reoptimized.length ? "warning" : "normal"}"><i>${r.reoptimized.length ? "▲" : "✓"}</i>${r.reoptimized.length} shipment(s) re-optimised</span> ${r.reoptimized.map(x => `<span class="mono">#${x.shipment_id} ${esc(x.from.carrier)}→${esc(x.to.carrier)}</span>`).join(" ")}`; setTimeout(vOps, 1800); });
}

// ------------------------------------------------------- data & evidence
function timelineChart(tl, mean) {
  const W = 760, H = 190, px = 30, py = 22, bw = (W - 2 * px) / tl.length, max = Math.max(...tl.map(t => t.late), 0.01);
  const y = v => H - py - (H - 2 * py) * v / max;
  const bars = tl.map((t, i) => `<rect x="${(px + i * bw + 1).toFixed(1)}" y="${y(t.late).toFixed(1)}" width="${(bw - 2).toFixed(1)}" height="${(H - py - y(t.late)).toFixed(1)}" rx="2" fill="${t.late > 2 * mean ? "var(--serious)" : "var(--navy)"}" opacity=".9" tabindex="0" ${tip(`${t.ym}\n${t.n} orders\nLate: ${(t.late * 100).toFixed(1)}%\nMedian delivery: ${t.total_p50} d`)}></rect>`).join("");
  const labels = tl.map((t, i) => i % 3 === 0 ? `<text x="${(px + i * bw + bw / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="10" fill="var(--text-3)">${t.ym.slice(2)}</text>` : "").join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto" role="img" aria-label="Late-delivery rate by month">${bars}${labels}<line x1="${px}" x2="${W - px}" y1="${y(mean).toFixed(1)}" y2="${y(mean).toFixed(1)}" stroke="var(--text-2)" stroke-dasharray="4 3"/><text x="${W - px}" y="${(y(mean) - 4).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--text-2)">mean ${(mean * 100).toFixed(1)}%</text><text x="${px - 4}" y="${py + 4}" text-anchor="end" font-size="10" fill="var(--text-3)">${(max * 100).toFixed(0)}%</text></svg>`;
}
async function vData() {
  view().innerHTML = head("Data & Evidence", "What the real Olist data says, where the platform uses it, and what it cannot tell us") + `<div id="dv">Loading…</div>`;
  const e = await get("/v1/data/evidence");
  if (!e.active) { $("#dv").innerHTML = `<div class="card"><h3>Running on synthetic physics</h3><p>${esc(e.reason)}.</p><p class="mono">${esc(e.how_to_enable)}</p></div>`; return; }
  const h = e.headline, n = e.network, b = e.benchmark, pv = e.provenance, q = pv.quality;
  const kp = (l, v, d = "") => `<div class="card kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="d">${d}</div></div>`;
  const kv = (k, v) => `<tr><td>${k}</td><td class="n">${v ?? "-"}</td></tr>`;
  let bench = "<div class='sub'>Benchmark was not built into this artifact.</div>";
  if (b) {
    const l = b.late, t = b.eta, sp = b.split;
    bench = `<div class="hint">Train: orders before ${esc(sp.train_until)} (${sp.n_train.toLocaleString()}). Test: ${esc(sp.test_from)} → ${esc(sp.test_to)} (${sp.n_test.toLocaleString()}), chronological.</div>
    <table><thead><tr><th>Metric</th><th class="n">Model</th><th>Baseline</th></tr></thead><tbody>
      <tr><td>ETA error (MAE)</td><td class="n"><b>${t.mae_days_model} d</b></td><td>${t.mae_days_olist_estimate} d Olist estimate · ${t.mae_days_train_median} d constant</td></tr>
      <tr><td>Late-risk ROC-AUC</td><td class="n"><b>${l.auc_model}</b></td><td>${l.auc_route_history} route history · 0.50 chance</td></tr>
      <tr><td>Brier score</td><td class="n"><b>${l.brier_model}</b></td><td>${l.brier_constant} constant</td></tr>
      <tr><td>Riskiest decile late rate</td><td class="n"><b>${pct(l.top_decile_late_rate, 1)}</b></td><td>${pct(l.base_rate_test, 1)} overall (${l.top_decile_lift}× lift)</td></tr></tbody></table>
    <p class="sub" style="margin-top:8px">ETA is the strong result: Olist's own estimate is ${t.olist_estimate_bias_days} days too conservative on the hold-out. Late-risk is weak (AUC ${l.auc_model}, Brier barely above a constant)${l.auc_route_history < 0.5 ? "; route history is below chance on the hold-out because the Feb–Mar 2018 lateness burst does not repeat" : ""}. Carrier-level accuracy cannot be measured from this data.</p>`;
  }
  $("#dv").innerHTML = `
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(190px,1fr))">
    ${kp("Real orders analysed", h.orders_analysed.toLocaleString(), `${esc(h.date_from)} → ${esc(h.date_to)}`)}
    ${kp("Cross-state orders", pct(h.cross_state_share, 1), `+${h.freight_premium_order_pct}% freight vs same-state`)}
    ${kp("Late vs customer promise", pct(h.late_rate, 1), `median ${h.late_days_median} d late when late`)}
    ${kp("Promise vs reality", n.sla_padding_ratio + "×", `${n.promised_p50} d promised · ${n.total_p50} d median`)}
    ${kp("Review score", `${e.review.on_time.toFixed(2)} → ${e.review.late.toFixed(2)}`, "on-time → late delivery")}
    ${kp("Freight share of value", h.freight_share_of_value_pct + "%", `avg freight ${money(h.avg_freight)}`)}
  </div>
  <div class="card" style="margin-top:14px"><h3>What the data says</h3><div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">
    ${e.insights.map(i => `<div><div class="sub">${esc(i.title)}</div><div style="font-size:20px;font-weight:700">${esc(i.value)}</div><div class="sub">${esc(i.detail)}</div></div>`).join("")}</div></div>
  <div class="grid g-main" style="margin-top:14px">
    <div class="card"><h3>Late-delivery rate by month</h3><div class="hint">Highlighted: months above twice the mean (${esc(e.timeline.filter(t => t.late > 2 * h.late_rate).map(t => t.ym).join(", ") || "none")}). Nov 2017 (Black Friday) also stands out.</div>${timelineChart(e.timeline, h.late_rate)}</div>
    <div class="card"><h3>Late rate by destination state</h3><div style="margin-top:10px">${tileMap(e.states, "risk")}</div></div>
  </div>
  <div class="grid g2" style="margin-top:14px">
    <div class="card"><h3>Busiest real routes</h3><div class="scroll"><table><thead><tr><th>Route</th><th class="n">Orders</th><th class="n">km</th><th class="n">Transit p50/p90</th><th class="n">Late</th><th class="n">Freight p50</th></tr></thead><tbody>
      ${e.top_routes.map(r => `<tr><td>${esc(r.route)}</td><td class="n">${r.orders.toLocaleString()}</td><td class="n">${Math.round(r.km ?? 0)}</td><td class="n">${r.transit_p50} / ${r.transit_p90} d</td><td class="n">${r.late_pct}%</td><td class="n">${money(r.freight_p50)}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="card"><h3>Real-data benchmark</h3>${bench}</div>
  </div>
  <div class="grid g2" style="margin-top:14px">
    <div class="card"><h3>Where the data is used</h3><ul>${e.how_used.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>
    <div class="card"><h3>What it cannot tell us</h3><ul>${e.limits.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>
  </div>
  <div class="card" style="margin-top:14px"><h3>Provenance ${e.integrity_ok ? `<span class="chip normal"><i>✓</i>Artifact checksum verified</span>` : `<span class="chip critical"><i>✖</i>Checksum mismatch</span>`}</h3>
    <div class="hint">Built ${esc(pv.built_at)} · ETL v${esc(pv.etl_version)} · checksum <span class="mono">${esc(pv.checksum.slice(0, 16))}</span> · late = ${esc(pv.late_definition)}</div>
    <div class="grid g2"><div class="scroll" style="max-height:260px"><table><thead><tr><th>Source file</th><th class="n">Rows</th><th>SHA-256</th></tr></thead><tbody>${Object.values(pv.source_files).map(f => `<tr><td class="mono">${esc(f.file)}</td><td class="n">${f.rows.toLocaleString()}</td><td class="mono">${esc(f.sha256.slice(0, 12))}…</td></tr>`).join("")}</tbody></table></div>
    <table><tbody>${kv("Raw orders", q.orders_raw.toLocaleString())}${kv("Analysed", q.orders_analysed.toLocaleString())}${kv("Multi-seller dropped", q.multi_seller_dropped.toLocaleString())}${kv("Not delivered dropped", q.not_delivered_dropped.toLocaleString())}${kv("Payments reconcile", q.payment_reconciles_pct + "%")}${kv("Time-factor clipped flow", pct(e.diagnostics.time_factor_clipped_flow_share, 1))}</tbody></table></div></div>`;
}

// ---------------------------------------------------------- audit/models
async function vAudit() {
  const [m, a, v] = await Promise.all([get("/v1/models"), get("/v1/audit?limit=60"), get("/v1/audit/verify")]);
  const o = m.offline_evaluation, f = m.online_feedback;
  const kv = (k, v2) => `<tr><td>${k}</td><td class="n">${v2 ?? "-"}</td></tr>`;
  view().innerHTML = head("Audit & Models", "Every decision is explainable after the fact; every prediction is measured against reality") + `
  <div class="grid g2"><div class="card"><h3>Prediction quality (simulated carriers, hold-out)</h3><table><tbody>${kv("ETA MAE (model)", o.eta_mae_days + " d")}${kv("ETA MAE (carrier promise)", o.carrier_promise_mae_days + " d")}${kv("Late-risk AUC", o.late_auc)}${kv("Brier score", o.late_brier)}${kv("Base late rate", pct(o.base_late_rate, 1))}${kv("Top-decile late rate", `${pct(o.top_decile_late_rate, 1)} (${o.top_decile_lift}× lift)`)}${kv("Precision / recall @ F1 threshold " + o.risk_threshold_f1, `${pct(o.late_precision)} / ${pct(o.late_recall)}`)}</tbody></table></div>
  <div class="card"><h3>Feedback loop (online, delivered shipments)</h3>${f.samples ? `<table><tbody>${kv("Delivered samples", f.samples)}${kv("ETA MAE (model)", f.eta_mae_days + " d")}${kv("ETA MAE (carrier promise)", f.carrier_promise_mae_days + " d")}${kv("Predicted vs actual late rate", `${pct(f.predicted_late_rate, 1)} vs ${pct(f.actual_late_rate, 1)}`)}${kv("Calibration gap", f.calibration_gap_pts + " pts")}${kv("Brier", f.brier)}</tbody></table>` : "<div class='sub'>No delivered shipments yet: seed demo data and advance the clock.</div>"}</div></div>
  <div class="card" style="margin-top:14px"><h3>Audit trail ${v.valid ? `<span class="chip normal"><i>✓</i>Chain verified · ${v.entries} entries</span>` : `<span class="chip critical"><i>✖</i>Chain broken at #${v.broken_at}</span>`}</h3><div class="hint">Hash-chained log: editing any past entry breaks verification.</div>
  <div class="scroll" style="max-height:420px"><table><thead><tr><th>#</th><th>Actor</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead><tbody>${a.map(r => `<tr><td>${r.id}</td><td>${esc(r.actor)}</td><td>${esc(r.action)}</td><td class="mono">${esc(r.entity)}:${esc(r.entity_id)}</td><td class="mono" style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" ${tip(JSON.stringify(r.details, null, 1))}>${esc(JSON.stringify(r.details))}</td></tr>`).join("")}</tbody></table></div></div>`;
}

boot();
})();
