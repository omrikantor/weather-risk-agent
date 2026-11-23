/* Weather Risk Agent UI. Plain JS, no build step.
   Security: model output (markdown) is rendered with marked + DOMPurify; everything else is set via textContent. */
"use strict";

const $ = (id) => document.getElementById(id);
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];
const HAZARDS = {
  winter: { label: "Winter storms / ice", short: "Winter" },
  flood: { label: "Flooding", short: "Flood" },
  convective: { label: "Thunderstorms / wind", short: "Storms" },
  hurricane: { label: "Hurricanes / tropical", short: "Hurricane" },
  heat: { label: "Extreme heat", short: "Heat" },
};
const ICON = {
  winter: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v20M4.9 6l14.2 12M19.1 6 4.9 18M9 4l3 3 3-3M9 20l3-3 3 3"/></svg>',
  flood: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 4 2 2-2 4-2M2 18c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 4 2 2-2 4-2M12 3v4"/></svg>',
  convective: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>',
  hurricane: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="2.5"/><path d="M12 3c-5 0-8 3-8 6M12 21c5 0 8-3 8-6M20 9c0-3-3-6-8-6M4 15c0 3 3 6 8 6"/></svg>',
  heat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
};
const SPARK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg>';
const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
const CHEVRON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 18l6-6-6-6"/></svg>';
const QTYPE = { ranking: "Ranking", comparison: "Comparison", statistic: "Statistic", explanation: "Explanation",
  methodology: "Methodology", stand_in: "Stand-in hub", clarification: "Clarification", out_of_scope: "Out of scope" };
const MEASURE = { severe_disruption_days: "Severe disruption days", routine_disruption_days: "Warning days",
  weather_statistic: "Weather statistic" };
const SAMPLES = [
  "Which hubs in the Midwest are most exposed to winter disruption?",
  "Compare Miami and Houston in terms of hurricane and flood exposure.",
  "What percentage of days in Denver last year had snowfall?",
  "Why is the Dallas hub's weather disruption risk high?",
  "What's the weather risk for a hub in Tampa?",
  "What should we watch for this month?",
];

const storage = makeStorage();   // localStorage can throw (private mode); wrapped
const urlDev = new URLSearchParams(location.search).get("dev");
if (urlDev !== null) storage.set("wra_dev", urlDev === "0" ? "0" : "1");
const state = {
  code: storage.get("wra_code") || "", sessionId: null, busy: false, dev: storage.get("wra_dev") === "1",
  month: new Date().getMonth() + 1, measure: "severe_disruption_days", hazard: "all", region: "",
};

/* ---------------- helpers ---------------- */
function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;            // only used with static, trusted SVG strings
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "style") n.setAttribute("style", v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) n.append(c instanceof Node ? c : String(c));
  return n;
}
/* replaceChildren() would render null/false as text; skip them. */
function fill(node, ...kids) { node.replaceChildren(...kids.flat().filter((k) => k != null && k !== false)); }
const fmt = (v, d = 2) => (v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 }));
const pct = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);
function hzChip(h, text) { return el("span", { class: `hz-chip hz-${h}`, html: ICON[h] }, text || HAZARDS[h].label); }
function renderMarkdown(md) {
  const html = DOMPurify.sanitize(marked.parse(md || "", { gfm: true, breaks: false }));
  const div = el("div", { class: "md" }); div.innerHTML = html;
  div.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
  div.querySelectorAll("table").forEach((t) => { const w = el("div", { class: "table-wrap" }); t.replaceWith(w); w.append(t); });
  return div;
}
function prettyKey(k) {
  return k.replace(/_/g, " ").replace(/\(([^)]*)\)/, " ($1)").replace(/\bper year\b/, "/ yr")
    .replace(/^p storm within 150km next 10y$/, "chance of a storm within 150 km in 10 years")
    .replace(/km\b/g, " km").replace(/\s+/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/* ---------------- API ---------------- */
class ApiError extends Error { constructor(status, detail) { super(detail); this.status = status; } }
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.code) headers["X-Access-Code"] = state.code;
  let res;
  try { res = await fetch(path, { ...opts, headers }); }
  catch { throw new ApiError(0, "Can't reach the server. Check your connection and try again."); }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (res.status === 401) { showCodeModal("That code didn't work. Please try again."); throw new ApiError(401, "Access code required."); }
  if (!res.ok) throw new ApiError(res.status, (body && body.detail) || `Request failed (${res.status}).`);
  return body;
}

/* ---------------- status / access ---------------- */
async function loadHealth() {
  try {
    const h = await (await fetch("/health")).json();
    const ok = h.status === "ok" && h.llm_configured;
    $("status-dot").className = "dot " + (ok ? "ok" : "bad");
    const b = h.budget;
    $("status-text").textContent = !ok ? "Assistant unavailable"
      : state.dev ? `Online · $${fmt(b.remaining_usd, 2)} budget left today` : "Online";
    $("status").title = state.dev ? `Model ${h.llm_model} · scores ${h.scoring_version} · computed ${h.scores_computed_at}`
      : `Data updated ${String(h.scores_computed_at || "").slice(0, 10)}`;
    return h;
  } catch {
    $("status-dot").className = "dot bad"; $("status-text").textContent = "Offline"; return null;
  }
}
function showCodeModal(msg = "") {
  $("code-error").textContent = msg; $("code-modal").classList.add("open");
  setTimeout(() => $("code-input").focus(), 50);
}
$("code-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  state.code = $("code-input").value.trim();
  storage.set("wra_code", state.code);
  try {
    await api("/api/hubs");
    $("code-modal").classList.remove("open");
    loadAll();
  } catch (err) { if (err.status !== 401) $("code-error").textContent = err.message; }
});

/* ---------------- outlook ---------------- */
async function loadOutlook() {
  $("m-label").textContent = MONTHS[state.month - 1];
  $("outlook-title").textContent = state.month === new Date().getMonth() + 1
    ? `Where disruption usually hits in ${MONTHS[state.month - 1]}` : `${MONTHS[state.month - 1]} outlook`;
  const grid = $("outlook-grid");
  grid.replaceChildren(...Array.from({ length: 5 }, () => el("div", { class: "card skeleton", style: "height:178px" })));
  try {
    const o = await api(`/api/outlook?month=${state.month}&top_n=5`);
    grid.replaceChildren(...o.hubs.map((h) => outlookCard(h, o.month_name)));
    const foot = $("outlook-foot");
    const likely = Object.entries(o.hazard_summary).filter(([, v]) => v.hubs_with_warning_in_most_years > 0);
    foot.replaceChildren(
      el("span", { class: "faint", style: "font-size:12px" }, "Across the network: "),
      ...(likely.length ? likely.map(([z, v]) => hzChip(z, `${v.hubs_with_warning_in_most_years} hub${v.hubs_with_warning_in_most_years > 1 ? "s" : ""} usually warned`))
        : [el("span", { class: "faint", style: "font-size:12px" }, "no hazard is warned in most years this month")]),
      el("span", { style: "flex:1" }),
      el("button", { class: "chip", onclick: () => ask(`What should we watch for in ${o.month_name}?`) },
        `Ask the agent about ${o.month_name}`));
  } catch (err) {
    grid.replaceChildren(el("div", { class: "muted" }, err.message));
  }
}
function outlookCard(h, monthName) {
  const z = h.top_hazard;
  const events = h.past_fema_disasters_this_month.map((e) => `${titleCase(e.title || e.type)} ${e.year}`);
  const card = el("button", { class: `card ocard hz-${z}`, onclick: () => openHub(h.hub_id),
    "aria-label": `${h.name}: ${HAZARDS[z].label}, ${pct(h.p_at_least_one_warning_day)} of ${monthName}s` },
    el("div", { class: "rank" }, `#${h.rank} this month`),
    el("div", { class: "hub" }, h.name.replace(/ \(.*\)/, ""), " ", el("small", {}, h.state)),
    hzChip(z, HAZARDS[z].short),
    el("div", { style: "margin-top:10px" }, el("div", { class: "pct num" }, pct(h.p_at_least_one_warning_day)),
      el("div", { class: "pct-label" }, `of ${monthName}s had a ${HAZARDS[z].short.toLowerCase()} warning (${h.years_with_warning} of ${h.years_observed} yrs)`)),
    el("div", { class: "meta" }, el("span", {}, "Severe days"), el("span", { class: "num" }, fmt(h.expected_severe_days_this_month, 2))),
    events.length ? el("div", { class: "events", title: events.join(" · ") }, events.join(" · ")) : null);
  return card;
}
function titleCase(s) { return String(s || "").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\bAnd\b/g, "and"); }

/* ---------------- leaderboard ---------------- */
async function loadBoard() {
  const list = $("lb-list");
  list.replaceChildren(...Array.from({ length: 8 }, () => el("div", { class: "skeleton", style: "height:40px;margin:6px 0" })));
  const q = new URLSearchParams({ measure: state.measure, hazard: state.hazard });
  if (state.region) q.set("region", state.region);
  try {
    const r = await api(`/api/scores?${q}`);
    const max = Math.max(...r.ranking.map((x) => x.range_90pct[1] || x.value_days_per_year), 0.0001);
    const unit = state.measure === "severe_disruption_days" ? "Expected severe-disruption days per year"
      : "Days per year with a weather warning";
    $("lb-caption").textContent = `${unit} · ${r.hubs_considered} hubs. Dot = best estimate, bar = likely range.`;
    list.replaceChildren(...r.ranking.map((x) => {
      const tier = x.tier || x.overall_tier;
      const [lo, hi] = x.range_90pct;
      return el("button", { class: `lb-row${x.no_exposure_in_data ? " zero" : ""}`, onclick: () => openHub(x.hub_id) },
        el("span", { class: "lb-rank" }, x.rank),
        el("span", { class: "lb-name" }, x.name, el("small", {}, `${x.state} · ${x.region}`)),
        el("span", { class: "lb-bar", "aria-hidden": "true" }, el("span", { class: "track" }),
          el("span", { class: "range", style: `left:${(lo / max) * 100}%;width:${Math.max(((hi - lo) / max) * 100, 1)}%` }),
          el("span", { class: "point", style: `left:${(x.value_days_per_year / max) * 100}%` })),
        el("span", { class: "lb-val" }, fmt(x.value_days_per_year, 2),
          el("small", { class: `tier ${tier}`, title: x.tier ? "Tier" : "Overall tier (total severe days)" }, tier)));
    }));
  } catch (err) { list.replaceChildren(el("div", { class: "muted" }, err.message)); }
}
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
  state.measure = b.dataset.measure; loadBoard();
}));
$("f-hazard").addEventListener("change", (e) => { state.hazard = e.target.value; loadBoard(); });
$("f-region").addEventListener("change", (e) => { state.region = e.target.value; loadBoard(); });

/* ---------------- hub drawer ---------------- */
async function openHub(hubId) {
  $("drawer").classList.add("open"); $("scrim").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false");
  document.body.classList.remove("show-board");
  $("drawer-title").replaceChildren(el("div", { class: "skeleton", style: "height:24px;width:60%" }));
  $("drawer-body").replaceChildren(el("div", { class: "skeleton", style: "height:300px" }));
  try { renderHub(await api(`/api/hubs/${encodeURIComponent(hubId)}/breakdown`)); }
  catch (err) { $("drawer-body").replaceChildren(el("div", { class: "muted" }, err.message)); }
}
function closeDrawer() {
  $("drawer").classList.remove("open"); $("scrim").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true");
}
$("drawer-close").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);

function renderHub(d) {
  $("drawer-title").replaceChildren(
    el("div", { class: "eyebrow" }, `${d.region} · ${d.state}`),
    el("h2", {}, d.name, " ", el("span", { class: `pill ${d.tier}`, style: "vertical-align:middle" }, d.tier)));
  const total = d.severe_days_per_year || 0;
  const contrib = d.contributions;
  const months = d.monthly_severe_days_jan_to_dec || [];
  const mMax = Math.max(...months, 0.0001);
  const peakIdx = months.indexOf(Math.max(...months));

  const evidence = HAZARD_ORDER.filter((z) => (d.hazards[z].warning_days_per_year || 0) > 0).map((z) => {
    const hz = d.hazards[z];
    const ev = Object.entries(hz.evidence).filter(([k, v]) => !["notable_storms", "record"].includes(k) && v != null);
    return el("div", { class: "card", style: "padding:12px 14px;margin-bottom:10px" },
      el("div", { style: "display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:8px" },
        hzChip(z), el("span", { class: "faint num", style: "font-size:12px;text-align:right",
          title: `${fmt(hz.warning_days_per_year)} warning days/yr × ${pct(hz.severity_share)} of such days were part of a declared disaster` },
          `${fmt(hz.warning_days_per_year, 1)} warning days/yr · ${fmt(hz.severe_days_per_year)} severe`)),
      el("div", { class: "kv" }, ...ev.flatMap(([k, v]) => [el("span", {}, EVIDENCE_LABELS[k] || prettyKey(k)),
        el("span", {}, typeof v !== "number" ? String(v) : k.startsWith("p_") ? pct(v) : fmt(v))])),
      z === "hurricane" && hz.evidence.notable_storms && hz.evidence.notable_storms.length
        ? el("div", { class: "faint", style: "font-size:12px;margin-top:8px" },
          "Notable storms within 150 km: " + hz.evidence.notable_storms.map((s) => s.storm).join(", ")) : null);
  });

  fill($("drawer-body"),
    el("div", { class: "kpis" },
      el("div", { class: "kpi" }, el("div", { class: "v" }, `#${d.rank}`), el("div", { class: "l" }, `of ${d.of} hubs`)),
      el("div", { class: "kpi" }, el("div", { class: "v" }, fmt(total)), el("div", { class: "l" }, `severe-disruption days / yr (likely ${fmt(d.severe_days_range_90pct[0])}–${fmt(d.severe_days_range_90pct[1])})`)),
      el("div", { class: "kpi" }, el("div", { class: "v" }, fmt(d.routine_days_per_year, 1)), el("div", { class: "l" }, "warning days / yr"))),
    el("div", { class: "block" }, el("h4", {}, "What drives the score"),
      el("div", { class: "stack" }, ...contrib.filter((c) => c.severe_days_per_year > 0).map((c) =>
        el("span", { class: `hz-${c.hazard}`, style: `width:${(c.share_of_total || 0) * 100}%;background:var(--hz)`, title: HAZARDS[c.hazard].label }))),
      el("div", { class: "legend" }, ...contrib.map((c) => el("div", { class: `hz-${c.hazard}` }, el("i"),
        el("span", {}, HAZARDS[c.hazard].label), el("span", { class: "num muted" }, `${fmt(c.severe_days_per_year)} days`),
        el("span", { class: "num faint", style: "width:42px;text-align:right" }, pct(c.share_of_total)))))),
    months.length ? el("div", { class: "block" }, el("h4", {}, `Seasonal pattern · peak in ${d.peak_month}`),
      el("div", { class: "months" }, ...months.map((v, i) => el("div", { class: `b${i === peakIdx ? " peak" : ""}`,
        style: `height:${Math.max((v / mMax) * 100, 2)}%`, title: `${MONTHS[i]}: ${fmt(v, 3)} severe days` }))),
      el("div", { class: "month-labels" }, ...MONTHS.map((m) => el("span", {}, m[0])))) : null,
    el("div", { class: "block" }, el("h4", {}, "The evidence, hazard by hazard"), ...evidence),
    d.recent_fema_major_disasters_in_county.length ? el("div", { class: "block" }, el("h4", {}, "Recent FEMA major disasters (county)"),
      ...d.recent_fema_major_disasters_in_county.map((e) => el("div", { class: "event" },
        el("span", {}, titleCase(e.title || e.type)), el("span", { class: "faint num" }, e.begin.slice(0, 7))))) : null,
    el("div", { class: "block" }, el("h4", {}, "How reliable is this?"),
      el("div", { class: "kv" },
        el("span", {}, "Confidence"), el("span", {}, d.confidence[0].toUpperCase() + d.confidence.slice(1)),
        d.station ? el("span", {}, "Weather measured at") : null,
        d.station ? el("span", {}, `${titleCase(d.station.name)} (${fmt(d.station.distance_km, 0)} km away)`) : null),
      d.confidence_reasons.length ? el("ul", { class: "muted", style: "font-size:12.5px;margin:8px 0 0;padding-left:18px" },
        ...d.confidence_reasons.map((r) => el("li", {}, r))) : null),
    el("button", { class: "btn primary", style: "width:100%;justify-content:center;height:40px",
      onclick: () => { closeDrawer(); ask(`Why is the ${d.name} hub's weather disruption risk ${d.tier.toLowerCase()}?`); } },
      `Ask the agent about ${d.name.replace(/ \(.*\)/, "")}`));
}
const HAZARD_ORDER = ["hurricane", "flood", "convective", "winter", "heat"];
const EVIDENCE_LABELS = {
  "tropical_storms_within_150km_per_decade": "Tropical storms passing within 150 km (per decade)",
  "hurricanes_within_150km_per_decade": "Hurricanes passing within 150 km (per decade)",
  "p_storm_within_150km_next_10y": "Chance of a storm within 150 km in the next 10 years",
  "heavy_rain_days_per_year(>=50mm)": "Very heavy rain days per year (50 mm+)",
  "avg_annual_max_3day_rain_mm": "Typical worst 3-day rainfall each year (mm)",
  "tornado_report_days_per_year(EF1+,50km)": "Days with a damaging tornado nearby (per year)",
  "hail_report_days_per_year(>=1in,50km)": "Days with large hail nearby (per year)",
  "wind_report_days_per_year(>=50kt,50km)": "Days with damaging wind nearby (per year)",
  "damaging_gust_days_per_year(>=93km/h)": "Days with gusts of 93 km/h+ at the airport (per year)",
  "thunder_days_per_year(observed)": "Thunderstorm days per year",
  "snow_days_per_year(>=1cm)": "Snow days per year (1 cm+)",
  "heavy_snow_days_per_year(>=10cm)": "Heavy snow days per year (10 cm+)",
  "ice_days_per_year(observed)": "Freezing rain / sleet days per year",
  "annual_snowfall_cm": "Average annual snowfall (cm)",
  "extreme_cold_days_per_year(<=-15C)": "Very cold days per year (−15 °C or below)",
  "extreme_heat_days_per_year(>=38C)": "Very hot days per year (38 °C / 100 °F+)",
};

/* ---------------- methodology ---------------- */
$("method-btn").addEventListener("click", async () => {
  $("method-modal").classList.add("open");
  const body = $("method-body");
  body.replaceChildren(el("div", { class: "skeleton", style: "height:240px" }));
  try {
    const m = await api("/api/methodology");
    const shareMax = Math.max(...Object.values(m.severity_shares).map((s) => s.share), 0.0001);
    fill(body,
      el("div", { class: "block" }, el("h4", {}, "Key measures"),
        ...Object.entries(m.kpis).map(([k, v]) => el("p", { style: "margin:0 0 8px" }, el("strong", {}, MEASURE[k] || k), " — ", v))),
      el("div", { class: "block" }, el("h4", {}, "Severity share by hazard (share of warning days inside a FEMA major disaster)"),
        ...Object.entries(m.severity_shares).map(([z, s]) => el("div", { class: `share-row hz-${z}` },
          el("span", {}, HAZARDS[z].label), el("span", { class: "share-bar" }, el("span", { style: `width:${(s.share / shareMax) * 100}%` })),
          el("span", { class: "num", style: "text-align:right" }, pct(s.share))))),
      el("div", { class: "block" }, el("h4", {}, "Data sources"), el("ul", { class: "muted" }, ...m.data_sources.map((x) => el("li", {}, x)))),
      el("div", { class: "block" }, el("h4", {}, "Assumptions and limitations"),
        el("ul", { class: "muted" }, ...m.assumptions_and_limitations.map((x) => el("li", {}, x)))),
      el("label", { class: "dev-toggle" },
        el("input", { type: "checkbox", ...(state.dev ? { checked: true } : {}), onchange: (e) => setDev(e.target.checked) }),
        el("span", {}, "Show technical details on new answers (tool calls, number tracing, cost) — for developers")),
      ...(state.dev ? [el("p", { class: "faint", style: "font-size:12px;margin:8px 0 0" },
        `Scoring ${m.scoring_version} · parameters ${m.params_version}`)] : []));
  } catch (err) { body.replaceChildren(el("div", { class: "muted" }, err.message)); }
});
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest(".modal").classList.remove("open")));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeDrawer(); $("method-modal").classList.remove("open"); document.body.classList.remove("show-board");
});

/* ---------------- chat ---------------- */
function renderSamples() {
  $("samples").replaceChildren(...SAMPLES.map((q) => el("button", { class: "sample", onclick: () => ask(q), html: SPARK }, q)));
}
function scrollToBottom() { const s = $("scroll"); s.scrollTop = s.scrollHeight; }

async function ask(text) {
  text = (text || "").trim();
  if (!text || state.busy) return;
  state.busy = true; $("send").disabled = true;
  $("welcome").style.display = "none";
  if (!$("outlook").classList.contains("collapsed")) toggleOutlook(true);
  $("thread").append(el("div", { class: "msg-user" }, text));
  updateNewChat();
  const t0 = Date.now();
  const secs = el("span", { class: "num" }, "0s");
  const thinking = el("div", { class: "card thinking" }, el("div", { class: "pulse" }, el("span"), el("span"), el("span")),
    el("span", {}, "Analyzing — choosing tools, querying data, verifying every number… "), secs);
  const timer = setInterval(() => { secs.textContent = `${Math.round((Date.now() - t0) / 1000)}s`; }, 500);
  $("thread").append(thinking); scrollToBottom();
  try {
    const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text, session_id: state.sessionId }) });
    state.sessionId = r.session_id;
    thinking.replaceWith(answerCard(r));
    loadHealth();
  } catch (err) {
    thinking.replaceWith(errorCard(err, text));
  } finally {
    clearInterval(timer); state.busy = false; $("send").disabled = false; scrollToBottom();
  }
}

function errorCard(err, text) {
  let msg = err.message;
  if (err.status === 429 && /budget/i.test(msg)) msg = "Today's question budget has been used. The outlook, leaderboard and hub details still work.";
  const retry = err.status !== 401 && !(err.status === 429 && /budget/i.test(err.message));
  return el("div", { class: "card msg-ai" }, el("div", { class: "warnbox", style: "margin:0" }, msg),
    retry ? el("div", { class: "confirm" }, el("button", { class: "btn", onclick: (e) => { e.target.closest(".card").remove(); ask(text); } }, "Retry")) : null);
}

const CONFIDENCE = { low: ["High confidence", "unc-low"], medium: ["Medium confidence", "unc-medium"],
  high: ["Low confidence", "unc-high"] };  // keyed by the answer's uncertainty level

function answerCard(r) {
  const a = r.answer;
  const unc = a.uncertainty || { level: "medium", reasons: [] };
  const [confLabel, confClass] = CONFIDENCE[unc.level] || CONFIDENCE.medium;
  const showConf = !["out_of_scope", "clarification"].includes(a.question_type);
  const card = el("article", { class: "card msg-ai" },
    el("div", { class: "ai-head" },
      el("div", { class: "ai-avatar", html: SPARK }),
      showConf ? el("span", { class: `badge ${confClass}`, title: (unc.reasons || []).join("\n") }, confLabel) : null,
      state.dev ? el("span", { class: "badge" }, QTYPE[a.question_type] || a.question_type) : null,
      state.dev && MEASURE[a.measure_used] ? el("span", { class: "badge" }, MEASURE[a.measure_used]) : null),
    renderMarkdown(a.answer_markdown));

  if (r.validation_warnings && r.validation_warnings.length) {
    card.append(el("div", { class: "warnbox" }, el("strong", {}, "Some numbers couldn't be verified against the data: "),
      r.validation_warnings.join(" · ")));
  }
  if (a.awaiting_user_confirmation) {
    const answerOnce = (text) => (e) => {
      if (state.busy) return;
      e.currentTarget.parentElement.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      ask(text);
    };
    card.append(el("div", { class: "confirm" },
      el("button", { class: "btn primary", onclick: answerOnce("Yes, please analyze it.") }, "Yes, analyze it"),
      el("button", { class: "btn", onclick: answerOnce("No, thanks.") }, "No")));
  }
  if (a.suggested_followups && a.suggested_followups.length && !a.awaiting_user_confirmation) {
    card.append(el("div", { class: "followups" }, ...a.suggested_followups.slice(0, 3).map((q) => el("button", { class: "chip", onclick: () => ask(q) }, q))));
  }
  if (a.question_type !== "out_of_scope") card.append(sourcesPanel(r));
  if (state.dev) card.append(provenance(r));
  return card;
}

/* Client-facing: what the answer is based on and how reliable it is. */
function sourcesPanel(r) {
  const a = r.answer;
  const figs = (a.figures || []).filter((f) => f.source_tool !== "user question").length;
  const unc = a.uncertainty || { level: "medium", reasons: [] };
  const verified = r.validation_warnings && r.validation_warnings.length
    ? el("div", { class: "check warn" }, "Some figures could not be verified against the data (see the note above).")
    : figs ? el("div", { class: "check" }, el("span", { html: CHECK }), `All ${figs} figures in this answer were checked against the underlying data.`)
      : null;
  return el("details", { class: "prov" },
    el("summary", { html: CHEVRON }, "Sources & reliability"),
    el("div", { class: "prov-grid" },
      verified,
      (a.data_sources || []).length ? el("div", {}, el("h4", {}, "Based on"), el("ul", {}, ...a.data_sources.map((x) => el("li", {}, x)))) : null,
      el("div", {}, el("h4", {}, `Confidence: ${(CONFIDENCE[unc.level] || CONFIDENCE.medium)[0].replace(" confidence", "").toLowerCase()}`),
        (unc.reasons || []).length ? el("ul", {}, ...unc.reasons.map((x) => el("li", {}, x))) : null),
      (a.assumptions || []).length ? el("div", {}, el("h4", {}, "Assumptions"), el("ul", {}, ...a.assumptions.map((x) => el("li", {}, x)))) : null,
      el("p", { class: "faint", style: "font-size:12px;margin:0" },
        "Figures are historical frequencies, not forecasts. See Methodology for how scores are calculated.")));
}

/* Developer view (Methodology → "Show technical details", or ?dev=1): tools, number tracing, cost. */
function provenance(r) {
  const a = r.answer, u = r.usage;
  const tools = r.trace.length ? r.trace.map((t) => el("div", { class: "tool-call" },
    el("span", {}, el("b", {}, t.tool), "(", Object.entries(t.arguments || {}).filter(([, v]) => v != null)
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", "), ")"),
    el("span", { class: "faint" }, t.error ? `error: ${t.error}` : `${t.ms} ms`)))
    : [el("div", { class: "faint", style: "font-size:13px" }, "No tools needed — answered from the conversation so far.")];
  const figs = a.figures || [];
  return el("details", { class: "prov dev" },
    el("summary", { html: CHEVRON }, `Technical details · ${r.trace.length} tool call${r.trace.length === 1 ? "" : "s"} · ${figs.length} number${figs.length === 1 ? "" : "s"} traced`),
    el("div", { class: "prov-grid" },
      el("div", {}, el("h4", {}, "Tools called"), ...tools),
      figs.length ? el("div", {}, el("h4", {}, "Every number, traced to its source"),
        el("table", { class: "trace-table" }, el("tbody", {}, ...figs.map((f) => el("tr", {},
          el("td", {}, f.value_in_answer),
          el("td", {}, el("div", { class: "src" }, f.field ? `${f.source_tool} → ${f.field}` : f.source_tool),
            f.match !== "exact or rounded" ? el("div", { class: "faint", style: "font-size:11.5px" }, f.match) : null)))))) : null,
      el("div", {}, el("h4", {}, "Classification"), el("div", { class: "perf" },
        el("span", {}, "type ", el("b", {}, a.question_type)), el("span", {}, "measure ", el("b", {}, a.measure_used)),
        el("span", {}, "uncertainty ", el("b", {}, a.uncertainty?.level)))),
      el("div", {}, el("h4", {}, "Performance"), el("div", { class: "perf" },
        el("span", {}, el("b", {}, `${(r.latency_ms / 1000).toFixed(1)}s`), " response"),
        el("span", {}, el("b", {}, u.calls), " model calls"),
        el("span", {}, el("b", {}, fmt(u.input_tokens, 0)), " in / ", el("b", {}, fmt(u.output_tokens, 0)), " out tokens"),
        el("span", {}, el("b", {}, `$${u.cost_usd.toFixed(4)}`), " cost"),
        r.retried ? el("span", {}, el("b", {}, "1"), " validation retry") : null))));
}

/* ---------------- composer / header ---------------- */
const input = $("input");
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
  input.style.overflowY = input.scrollHeight > 160 ? "auto" : "hidden";
});
if (window.matchMedia("(max-width: 640px)").matches) input.placeholder = "Ask about hub risk…";
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
$("composer").addEventListener("submit", (e) => { e.preventDefault(); const t = input.value; input.value = ""; input.style.height = "auto"; ask(t); });
$("new-chat").addEventListener("click", async () => {
  if (state.busy) return;
  if (state.sessionId) { try { await api("/api/chat/reset", { method: "POST", body: JSON.stringify({ session_id: state.sessionId }) }); } catch { /* ignore */ } }
  state.sessionId = null; $("thread").replaceChildren(); $("welcome").style.display = ""; toggleOutlook(false);
  updateNewChat(); $("scroll").scrollTo({ top: 0, behavior: "smooth" }); focusComposer();
});
function updateNewChat() { $("new-chat").hidden = $("thread").children.length === 0; }
function focusComposer() {
  const c = $("composer"); c.classList.remove("flash"); void c.offsetWidth; c.classList.add("flash"); input.focus();
}
function setDev(on) {
  state.dev = on; storage.set("wra_dev", on ? "1" : "0");
  loadHealth();
  $("dev-flag").hidden = !on;
}
function toggleOutlook(collapse) {
  const o = $("outlook");
  const c = collapse ?? !o.classList.contains("collapsed");
  o.classList.toggle("collapsed", c);
  $("outlook-toggle").innerHTML = c
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 15l-6-6-6 6"/></svg>';
  $("outlook-toggle").setAttribute("aria-label", c ? "Expand outlook" : "Collapse outlook");
}
$("outlook-toggle").addEventListener("click", () => toggleOutlook());
$("m-prev").addEventListener("click", () => { state.month = ((state.month + 10) % 12) + 1; toggleOutlook(false); loadOutlook(); });
$("m-next").addEventListener("click", () => { state.month = (state.month % 12) + 1; toggleOutlook(false); loadOutlook(); });
$("board-toggle").addEventListener("click", () => document.body.classList.toggle("show-board"));

/* ---------------- boot ---------------- */
function loadAll() { loadOutlook(); loadBoard(); }
(async function boot() {
  renderSamples();
  updateNewChat();
  $("dev-flag").hidden = !state.dev;
  const h = await loadHealth();
  if (h && h.access_code_required && !state.code) { showCodeModal(); return; }
  loadAll();
})();

function makeStorage() {
  return {
    get(k) { try { return window.localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
}
