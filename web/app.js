/* Weather Risk Agent UI. Plain JavaScript, no framework and no build step.

   Layout: seasonal outlook and chat in the main column, leaderboard on the right, a hub detail drawer, and the
   Methodology window. Every number on screen comes from the API, which uses the same tool functions as the agent.

   Security: model output (markdown) is rendered with marked and then sanitized with DOMPurify; everything else
   is inserted as text, never as HTML. The only HTML strings inserted directly are the fixed SVG icons below. */
"use strict";

const $ = (id) => document.getElementById(id);
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];
/* Display names for the five hazard groups (long for chips and legends, short for cards). */
const HAZARDS = {
  winter: { label: "Winter storms / ice", short: "Winter" },
  flood: { label: "Flooding", short: "Flood" },
  convective: { label: "Thunderstorms / wind", short: "Storms" },
  hurricane: { label: "Hurricanes / tropical", short: "Hurricane" },
  heat: { label: "Extreme heat", short: "Heat" },
};
/* Inline SVG icons per hazard (static strings, safe to insert as HTML). */
const ICON = {
  winter: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v20M4.9 6l14.2 12M19.1 6 4.9 18M9 4l3 3 3-3M9 20l3-3 3 3"/></svg>',
  flood: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 4 2 2-2 4-2M2 18c2 0 2-2 4-2s2 2 4 2 2-2 4-2 2 2 4 2 2-2 4-2M12 3v4"/></svg>',
  convective: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>',
  hurricane: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="2.5"/><path d="M12 3c-5 0-8 3-8 6M12 21c5 0 8-3 8-6M20 9c0-3-3-6-8-6M4 15c0 3 3 6 8 6"/></svg>',
  heat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
};
const SPARK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/></svg>';
const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
const SPEAKER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>';
const WARN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>';
const CHEVRON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 18l6-6-6-6"/></svg>';
const QTYPE = { ranking: "Ranking", comparison: "Comparison", statistic: "Statistic", explanation: "Explanation",
  methodology: "Methodology", stand_in: "Stand-in hub", clarification: "Clarification", out_of_scope: "Out of scope" };
const MEASURE = { severe_disruption_days: "Disaster-level days", routine_disruption_days: "Warning days",
  weather_statistic: "Weather statistic" };
/* Sample questions on the welcome screen (the assignment's questions plus two features). */
const SAMPLES = [
  "Which hubs in the Midwest are most exposed to winter disruption?",
  "Compare Miami and Houston in terms of hurricane and flood exposure.",
  "What percentage of days in Denver last year had snowfall?",
  "Why is the Dallas hub's weather disruption risk high?",
  "What's the weather risk for a hub in Tampa?",
  "What should we watch for this month?",
];

const storage = makeStorage();   // localStorage can throw (private mode), so every access is wrapped
/* ?dev=1 turns the developer view on (and ?dev=0 off); the choice is remembered in this browser. */
const urlDev = new URLSearchParams(location.search).get("dev");
if (urlDev !== null) storage.set("wra_dev", urlDev === "0" ? "0" : "1");
/* All UI state in one place: access code, current conversation, view options and leaderboard filters. */
const state = {
  code: storage.get("wra_code") || "", sessionId: null, busy: false, dev: storage.get("wra_dev") === "1",
  speak: storage.get("wra_speak") === "1",
  live: null, liveByHub: {},   // last /api/alerts answer, and its active warnings grouped by hub id
  month: new Date().getMonth() + 1, measure: "severe_disruption_days", hazard: "all", region: "",
};

/* ---------------- helpers ---------------- */
/* Create an element: el("div", { class: "x", onclick: fn }, child1, "text").
   Attributes starting with "on" become event listeners; null and false children are skipped. */
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
/* Format a number with at most d decimals, or "n/a" when missing. */
const fmt = (v, d = 2) => (v == null ? "n/a" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 }));
/* Readable frequency: 0.36 per year becomes "once every 2.8 yrs", 1.4 stays "1.4 days/yr". */
function oncePer(v) {
  if (!v) return "none in the data";
  return v < 1 ? `once every ${fmt(1 / v, 1)} yrs` : `${fmt(v)} days/yr`;
}
function monthYear(iso) { const [y, m] = iso.split("-"); return `${MONTHS[+m - 1].slice(0, 3)} ${y}`; }
const pct = (v) => (v == null ? "n/a" : `${Math.round(v * 100)}%`);
function hzChip(h, text) { return el("span", { class: `hz-chip hz-${h}`, html: ICON[h] }, text || HAZARDS[h].label); }
/* Model text to safe HTML: markdown to HTML, sanitized; links open in a new tab; wide tables scroll. */
function renderMarkdown(md) {
  const html = DOMPurify.sanitize(marked.parse(md || "", { gfm: true, breaks: false }));
  const div = el("div", { class: "md" }); div.innerHTML = html;
  div.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
  div.querySelectorAll("table").forEach((t) => { const w = el("div", { class: "table-wrap" }); t.replaceWith(w); w.append(t); });
  return div;
}
/* Fallback label for an evidence field without an entry in EVIDENCE_LABELS. */
function prettyKey(k) {
  return k.replace(/_/g, " ").replace(/\(([^)]*)\)/, " ($1)").replace(/\bper year\b/, "/ yr")
    .replace(/^p storm within 150km next 10y$/, "chance of a storm within 150 km in 10 years")
    .replace(/km\b/g, " km").replace(/\s+/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/* ---------------- API ---------------- */
class ApiError extends Error { constructor(status, detail) { super(detail); this.status = status; } }
/* Call the API with the access code. A 401 reopens the access-code dialog; other errors carry the
   server's message so it can be shown to the user. */
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
/* Status dot in the header (online, unavailable or offline), from /health. */
async function loadHealth() {
  try {
    const h = await (await fetch("/health")).json();
    const ok = h.status === "ok" && h.llm_configured;
    $("status-dot").className = "dot " + (ok ? "ok" : "bad");
    const b = h.budget;
    $("status-text").textContent = !ok ? "Assistant unavailable"
      : state.dev ? `Online \u00b7 $${fmt(b.remaining_usd, 2)} budget left today` : "Online";
    $("status").title = state.dev ? `Model ${h.llm_model} \u00b7 scores ${h.scoring_version} \u00b7 computed ${h.scores_computed_at}`
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
/* Access code form: save the code, test it with a cheap request, then load the data views. */
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
/* Seasonal outlook: the 5 hubs with the most expected disruption in the selected month, plus a network line. */
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
/* One outlook card; clicking it opens the hub panel. */
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
    el("div", { class: "meta" }, el("span", {}, "Disaster-level days"), el("span", { class: "num" }, fmt(h.expected_severe_days_this_month, 2))),
    events.length ? el("div", { class: "events", title: events.join(" \u00b7 ") }, events.join(" \u00b7 ")) : null);
  return card;
}
function titleCase(s) { return String(s || "").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase()).replace(/\bAnd\b/g, "and"); }

/* ---------------- leaderboard ---------------- */
/* Leaderboard: each row shows the rank, the best estimate (dot) and the 90% range (bar) on a shared scale. */
async function loadBoard() {
  const list = $("lb-list");
  list.replaceChildren(...Array.from({ length: 8 }, () => el("div", { class: "skeleton", style: "height:40px;margin:6px 0" })));
  const q = new URLSearchParams({ measure: state.measure, hazard: state.hazard });
  if (state.region) q.set("region", state.region);
  try {
    const r = await api(`/api/scores?${q}`);
    const max = Math.max(...r.ranking.map((x) => x.range_90pct[1] || x.value_days_per_year), 0.0001);
    const unit = state.measure === "severe_disruption_days" ? "Disaster-level days per year (part of a federally declared disaster)"
      : "Days per year with a weather warning";
    $("lb-caption").textContent = `${unit} \u00b7 ${r.hubs_considered} hubs. Dot = best estimate, bar = likely range.`;
    list.replaceChildren(...r.ranking.map((x) => {
      const tier = x.tier || x.overall_tier;
      const [lo, hi] = x.range_90pct;
      return el("button", { class: `lb-row${x.no_exposure_in_data ? " zero" : ""}`, onclick: () => openHub(x.hub_id) },
        el("span", { class: "lb-rank" }, x.rank),
        el("span", { class: "lb-name" }, x.name, liveMark(x.hub_id), el("small", {}, `${x.state} \u00b7 ${x.region}`)),
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
/* Hub drawer: open with a loading placeholder, then fill it from /api/hubs/{id}/breakdown. */
async function openHub(hubId) {
  $("drawer").classList.add("open"); $("scrim").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false");
  document.body.classList.remove("show-board"); syncBoardButton();
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

/* Build the hub panel: headline figures, what drives the score, the monthly pattern, evidence per hazard,
   recent FEMA disasters and data reliability. */
function renderHub(d) {
  $("drawer-title").replaceChildren(
    el("div", { class: "eyebrow" }, `${d.region} \u00b7 ${d.state}`),
    el("h2", {}, d.name, " ", el("span", { class: `pill ${d.tier}`, style: "vertical-align:middle" }, d.tier)));
  const total = d.severe_days_per_year || 0;
  const contrib = d.contributions;
  const months = d.monthly_severe_days_jan_to_dec || [];
  const mMax = Math.max(...months, 0.0001);
  const peakIdx = months.indexOf(Math.max(...months));

  const evidence = HAZARD_ORDER.filter((z) => (d.hazards[z].warning_days_per_year || 0) > 0).map((z) => {
    const hz = d.hazards[z];
    const ev = Object.entries(hz.evidence).filter(([k, v]) => !["notable_storms", "record"].includes(k) && v != null);
    const s = typeof hz.season === "object" ? hz.season : null;
    return el("div", { class: "card", style: "padding:12px 14px;margin-bottom:10px" },
      el("div", { style: "display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:8px" },
        hzChip(z), el("span", { class: "faint num", style: "font-size:12px;text-align:right",
          title: `${pct(hz.severity_share)} of warning days for this hazard were part of a federally declared disaster` },
          `${fmt(hz.warning_days_per_year, 1)} warning days/yr \u00b7 disaster-level ${oncePer(hz.severe_days_per_year)}`)),
      s ? el("div", { class: "season" },
        el("div", {}, el("span", { class: "faint" }, "When "), el("b", {}, s.typical_dates.replace(" - ", " to ")),
          el("span", { class: "faint" }, ` \u00b7 peak ${s.peak_month}`)),
        el("div", {}, el("b", {}, `about ${fmt(s.events_per_year, 1)}`), el("span", { class: "faint" }, " events/yr \u00b7 usually "),
          el("b", {}, `${s.typical_event_days} day${s.typical_event_days === 1 ? "" : "s"}`),
          el("span", { class: "faint" }, ` \u00b7 longest ${s.longest_event_days} days (${monthYear(s.longest_event_start)})`))) : null,
      el("div", { class: "kv" }, ...ev.flatMap(([k, v]) => [el("span", {}, EVIDENCE_LABELS[k] || prettyKey(k)),
        el("span", {}, typeof v !== "number" ? String(v) : k.startsWith("p_") ? pct(v) : fmt(v))])),
      z === "hurricane" && hz.evidence.notable_storms && hz.evidence.notable_storms.length
        ? el("div", { class: "faint", style: "font-size:12px;margin-top:8px" },
          "Notable storms within 150 km: " + hz.evidence.notable_storms.map((s) => s.storm).join(", ")) : null);
  });

  // Live warnings at this hub, shown first and labeled as separate from the historical score.
  const live = liveWarningsFor(d.hub_id);
  fill($("drawer-body"),
    live.length ? el("div", { class: "warnbox live-box", role: "status" },
      el("strong", {}, "Active now (National Weather Service): "),
      live.map((w) => `${w.event}${w.ends ? ` until ${localTime(w.ends)}` : ""}`).join("; "),
      el("div", { class: "faint", style: "font-size:12px;margin-top:4px" },
        "Live weather, not part of the historical risk score below.")) : null,
    el("div", { class: "kpis" },
      el("div", { class: "kpi" }, el("div", { class: "v" }, `#${d.rank}`), el("div", { class: "l" }, `of ${d.of} hubs`)),
      el("div", { class: "kpi" }, el("div", { class: "v" }, fmt(total)), el("div", { class: "l" }, `disaster-level days / yr (likely ${fmt(d.severe_days_range_90pct[0])} to ${fmt(d.severe_days_range_90pct[1])})`)),
      el("div", { class: "kpi" }, el("div", { class: "v" }, fmt(d.routine_days_per_year, 1)), el("div", { class: "l" }, "warning days / yr"))),
    el("div", { class: "block" }, el("h4", {}, "What drives the score"),
      el("div", { class: "stack" }, ...contrib.filter((c) => c.severe_days_per_year > 0).map((c) =>
        el("span", { class: `hz-${c.hazard}`, style: `width:${(c.share_of_total || 0) * 100}%;background:var(--hz)`, title: HAZARDS[c.hazard].label }))),
      el("div", { class: "legend" }, ...contrib.map((c) => el("div", { class: `hz-${c.hazard}` }, el("i"),
        el("span", {}, HAZARDS[c.hazard].label), el("span", { class: "num muted" }, oncePer(c.severe_days_per_year)),
        el("span", { class: "num faint", style: "width:42px;text-align:right" }, pct(c.share_of_total)))))),
    months.length ? el("div", { class: "block" }, el("h4", {}, `Disaster-level days by month \u00b7 peak in ${d.peak_month}`),
      el("div", { class: "months" }, ...months.map((v, i) => el("div", { class: `b${i === peakIdx ? " peak" : ""}`,
        style: `height:${Math.max((v / mMax) * 100, 2)}%`, title: `${MONTHS[i]}: ${fmt(v, 3)} disaster-level days (average)` }))),
      el("div", { class: "month-labels" }, ...MONTHS.map((m, i) => el("span", { title: m }, i + 1)))) : null,
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
/* Order of hazards in the evidence list (most severe per day first). */
const HAZARD_ORDER = ["hurricane", "flood", "convective", "winter", "heat"];
/* Client-friendly labels for the tool's technical evidence field names. */
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
  "extreme_cold_days_per_year(<=-15C)": "Very cold days per year (-15 \u00b0C or below)",
  "extreme_heat_days_per_year(>=38C)": "Very hot days per year (38 \u00b0C / 100 \u00b0F+)",
};

/* ---------------- methodology ---------------- */
/* Methodology window: definitions, severity shares, sources, limitations and the developer view switch. */
$("method-btn").addEventListener("click", async () => {
  $("method-modal").classList.add("open");
  const body = $("method-body");
  body.replaceChildren(el("div", { class: "skeleton", style: "height:240px" }));
  try {
    const m = await api("/api/methodology");
    const shareMax = Math.max(...Object.values(m.severity_shares).map((s) => s.share), 0.0001);
    fill(body,
      el("div", { class: "block" }, el("h4", {}, "Key measures"),
        ...Object.entries(m.kpis).map(([k, v]) => el("p", { style: "margin:0 0 8px" }, el("strong", {}, MEASURE[k] || k), ": ", v))),
      el("div", { class: "block" }, el("h4", {}, "Severity share by hazard (share of warning days inside a FEMA major disaster)"),
        ...Object.entries(m.severity_shares).map(([z, s]) => el("div", { class: `share-row hz-${z}` },
          el("span", {}, HAZARDS[z].label), el("span", { class: "share-bar" }, el("span", { style: `width:${(s.share / shareMax) * 100}%` })),
          el("span", { class: "num", style: "text-align:right" }, pct(s.share))))),
      el("div", { class: "block" }, el("h4", {}, "Data sources"), el("ul", { class: "muted" }, ...m.data_sources.map((x) => el("li", {}, x)))),
      el("div", { class: "block" }, el("h4", {}, "Assumptions and limitations"),
        el("ul", { class: "muted" }, ...m.assumptions_and_limitations.map((x) => el("li", {}, x)))),
      el("label", { class: "dev-toggle" },
        el("input", { type: "checkbox", ...(state.dev ? { checked: true } : {}), onchange: (e) => setDev(e.target.checked) }),
        el("span", {}, "Show technical details on new answers (tool calls, number tracing, cost). For developers.")),
      ...(state.dev ? [el("p", { class: "faint", style: "font-size:12px;margin:8px 0 0" },
        `Scoring ${m.scoring_version} \u00b7 parameters ${m.params_version}`)] : []));
  } catch (err) { body.replaceChildren(el("div", { class: "muted" }, err.message)); }
});
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest(".modal").classList.remove("open")));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeDrawer(); $("method-modal").classList.remove("open"); $("live-modal").classList.remove("open"); document.body.classList.remove("show-board"); syncBoardButton();
});

/* ---------------- chat ---------------- */
/* Sample question buttons on the welcome screen. */
function renderSamples() {
  $("samples").replaceChildren(...SAMPLES.map((q) => el("button", { class: "sample", onclick: () => ask(q), html: SPARK }, q)));
}
function scrollToBottom() { const s = $("scroll"); s.scrollTop = s.scrollHeight; }

/* Send a question: show it in the thread, show a progress card with a timer, then replace it with the answer
   (or an error card with Retry). Only one question can be in flight at a time. */
async function ask(text) {
  text = (text || "").trim();
  if (!text || state.busy) return;
  state.busy = true; $("send").disabled = true;
  voice.stopListening(); voice.stopSpeaking();
  $("welcome").style.display = "none";
  if (!$("outlook").classList.contains("collapsed")) toggleOutlook(true);
  $("thread").append(el("div", { class: "msg-user" }, text));
  updateNewChat();
  const t0 = Date.now();
  const secs = el("span", { class: "num" }, "0s");
  const thinking = el("div", { class: "card thinking" }, el("div", { class: "pulse" }, el("span"), el("span"), el("span")),
    el("span", {}, "Working on your answer. This usually takes 10 to 20 seconds. "), secs);
  const timer = setInterval(() => { secs.textContent = `${Math.round((Date.now() - t0) / 1000)}s`; }, 500);
  $("thread").append(thinking); scrollToBottom();
  try {
    const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text, session_id: state.sessionId }) });
    state.sessionId = r.session_id;
    const card = answerCard(r);
    thinking.replaceWith(card);
    // The server no longer held this conversation (it expired or the server restarted), so the answer was
    // given without the earlier context. Say so above the answer instead of failing silently.
    if (r.session_expired) {
      card.before(el("div", { class: "session-note", role: "status" },
        "Your earlier conversation expired, so this answer starts a new one. Please repeat any details it needs."));
    }
    if (state.speak) voice.speakCard(card);
    loadHealth();
  } catch (err) {
    thinking.replaceWith(errorCard(err, text));
  } finally {
    clearInterval(timer); state.busy = false; $("send").disabled = false; scrollToBottom();
  }
}

/* Error card; budget and access errors get no Retry button because retrying cannot help. */
function errorCard(err, text) {
  let msg = err.message;
  if (err.status === 429 && /budget/i.test(msg)) msg = "Today's question budget has been used. The outlook, leaderboard and hub details still work.";
  const retry = err.status !== 401 && !(err.status === 429 && /budget/i.test(err.message));
  return el("div", { class: "card msg-ai" }, el("div", { class: "warnbox", style: "margin:0" }, msg),
    retry ? el("div", { class: "confirm" }, el("button", { class: "btn", onclick: (e) => { e.target.closest(".card").remove(); ask(text); } }, "Retry")) : null);
}

/* The answer reports uncertainty; the badge shows the inverse as confidence (low uncertainty = high confidence). */
const CONFIDENCE = { low: ["High confidence", "unc-low"], medium: ["Medium confidence", "unc-medium"],
  high: ["Low confidence", "unc-high"] };  // keyed by the answer's uncertainty level

/* Answer card: confidence badge, the answer, any verification warning, confirm buttons for a stand-in
   recommendation, follow-up suggestions, the sources panel and (developer view) technical details. */
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
      state.dev && MEASURE[a.measure_used] ? el("span", { class: "badge" }, MEASURE[a.measure_used]) : null,
      voice.canSpeak ? el("button", { class: "btn ghost icon-btn read-btn", type: "button", "aria-label": "Read this answer aloud",
        title: "Read aloud", html: SPEAKER, onclick: (e) => voice.toggleCard(e.currentTarget.closest(".card")) }) : null),
    renderMarkdown(a.answer_markdown));

  if (r.validation_warnings && r.validation_warnings.length) {
    card.append(el("div", { class: "warnbox" }, el("strong", {}, "Some numbers couldn't be verified against the data: "),
      r.validation_warnings.join(" \u00b7 ")));
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

/* Developer view (Methodology, then "Show technical details", or ?dev=1): tools, number tracing, cost. */
function provenance(r) {
  const a = r.answer, u = r.usage;
  const tools = r.trace.length ? r.trace.map((t) => el("div", { class: "tool-call" },
    el("span", {}, el("b", {}, t.tool), "(", Object.entries(t.arguments || {}).filter(([, v]) => v != null)
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", "), ")"),
    el("span", { class: "faint" }, t.error ? `error: ${t.error}` : `${t.ms} ms`)))
    : [el("div", { class: "faint", style: "font-size:13px" }, "No tools needed. Answered from the conversation so far.")];
  const figs = a.figures || [];
  return el("details", { class: "prov dev" },
    el("summary", { html: CHEVRON }, `Technical details \u00b7 ${r.trace.length} tool call${r.trace.length === 1 ? "" : "s"} \u00b7 ${figs.length} number${figs.length === 1 ? "" : "s"} traced`),
    el("div", { class: "prov-grid" },
      el("div", {}, el("h4", {}, "Tools called"), ...tools),
      figs.length ? el("div", {}, el("h4", {}, "Every number, traced to its source"),
        el("table", { class: "trace-table" }, el("tbody", {}, ...figs.map((f) => el("tr", {},
          el("td", {}, f.value_in_answer),
          el("td", {}, el("div", { class: "src" }, f.field ? `${f.source_tool}.${f.field}` : f.source_tool),
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
/* The text box grows with its content up to 160 px, then scrolls. */
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
  input.style.overflowY = input.scrollHeight > 160 ? "auto" : "hidden";
});
if (window.matchMedia("(max-width: 640px)").matches) input.placeholder = "Ask about hub risk";
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
$("composer").addEventListener("submit", (e) => { e.preventDefault(); voice.stopListening(); const t = input.value; input.value = ""; input.style.height = "auto"; ask(t); });
/* New chat: stop voice, forget the conversation on the server, and return to the welcome screen. */
$("new-chat").addEventListener("click", async () => {
  if (state.busy) return;
  if (state.sessionId) { try { await api("/api/chat/reset", { method: "POST", body: JSON.stringify({ session_id: state.sessionId }) }); } catch { /* ignore */ } }
  voice.stopSpeaking(); voice.stopListening();
  state.sessionId = null; $("thread").replaceChildren(); $("welcome").style.display = ""; toggleOutlook(false);
  updateNewChat(); $("scroll").scrollTo({ top: 0, behavior: "smooth" }); focusComposer();
});
function updateNewChat() { $("new-chat").hidden = $("thread").children.length === 0; }
function focusComposer() {
  const c = $("composer"); c.classList.remove("flash"); void c.offsetWidth; c.classList.add("flash"); input.focus();
}
/* Turn the developer view on or off (applies to new answers). */
function setDev(on) {
  state.dev = on; storage.set("wra_dev", on ? "1" : "0");
  loadHealth();
  $("dev-flag").hidden = !on;
}
/* Collapse or expand the outlook (it collapses automatically when a conversation starts). */
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
/* Leaderboard button: on wide screens it hides or shows the leaderboard column (open by default, choice remembered);
   on narrow screens it opens the leaderboard as a full-screen panel. */
const wideScreen = window.matchMedia("(min-width: 1101px)");
function syncBoardButton() {
  const visible = wideScreen.matches ? !document.body.classList.contains("hide-board") : document.body.classList.contains("show-board");
  const b = $("board-toggle");
  b.setAttribute("aria-pressed", visible ? "true" : "false");
  b.setAttribute("aria-label", visible ? "Hide leaderboard" : "Show leaderboard");
  b.title = visible ? "Hide leaderboard" : "Show leaderboard";
}
document.body.classList.toggle("hide-board", storage.get("wra_board") === "hidden");
$("board-toggle").addEventListener("click", () => {
  if (wideScreen.matches) {
    const hide = !document.body.classList.contains("hide-board");
    document.body.classList.toggle("hide-board", hide);
    storage.set("wra_board", hide ? "hidden" : "shown");
  } else {
    document.body.classList.toggle("show-board");
  }
  syncBoardButton();
});
wideScreen.addEventListener("change", () => { document.body.classList.remove("show-board"); syncBoardButton(); });
syncBoardButton();

/* ---------------- voice (browser Web Speech API, no server involvement) ----------------
   Speech to text: SpeechRecognition (Chrome, Edge, Safari). The transcript goes into the input box and is NOT sent
   automatically, so the user can check place names first. Hidden when the browser does not support it.
   Text to speech: speechSynthesis (all modern browsers). Reads a plain-text version of an answer. */
const voice = (() => {
  const SR = () => window.SpeechRecognition || window.webkitSpeechRecognition;  // looked up on use
  const synth = () => window.speechSynthesis;
  const canListen = !!SR() && window.isSecureContext;
  const canSpeak = !!synth() && typeof window.SpeechSynthesisUtterance === "function";
  const hint = $("composer-hint");
  const defaultHint = hint.textContent;
  let rec = null;          // active recognition session
  let hintTimer = null;
  let speakingCard = null; // answer card currently being read

  function setHint(text, isError = false) {
    clearTimeout(hintTimer);
    hint.textContent = text; hint.classList.toggle("hint-error", isError);
    if (isError) hintTimer = setTimeout(() => setHint(defaultHint), 6000);
  }
  function setMic(on) {
    $("mic").classList.toggle("listening", on);
    $("mic").setAttribute("aria-pressed", on ? "true" : "false");
    $("mic").setAttribute("aria-label", on ? "Stop listening" : "Speak your question");
    $("mic").title = on ? "Stop listening" : "Speak your question";
  }
  const MIC_ERRORS = {
    "not-allowed": "Microphone access is blocked. Allow it in the browser's site settings to use voice.",
    "service-not-allowed": "Voice input is not available in this browser.",
    "audio-capture": "No microphone was found.",
    "no-speech": "Didn't catch that. Click the mic and try again.",
    "network": "Voice input needs an internet connection.",
    "language-not-supported": "Voice input is not available for US English in this browser.",
  };

  function startListening() {
    stopSpeaking();
    const Rec = SR(); const r = new Rec();
    r.lang = "en-US"; r.interimResults = true; r.continuous = false; r.maxAlternatives = 1;
    const base = input.value.trim() ? input.value.trim() + " " : "";
    r.onresult = (e) => {
      if (rec !== r) return;
      let text = "";
      for (const res of e.results) text += res[0].transcript;
      input.value = base + text.trimStart();
      input.dispatchEvent(new Event("input"));
    };
    r.onerror = (e) => {
      if (rec === r && e.error !== "aborted") setHint(MIC_ERRORS[e.error] || "Voice input stopped. Please try again.", true);
    };
    r.onend = () => {
      if (rec !== r) return;
      rec = null; setMic(false);
      if (!hint.classList.contains("hint-error")) setHint(input.value.trim() ? "Check the text, then press Enter to send" : defaultHint);
      input.focus(); input.setSelectionRange(input.value.length, input.value.length);
    };
    rec = r; setMic(true); setHint("Listening. Click the mic again to stop.");
    try { r.start(); } catch { rec = null; setMic(false); setHint("Voice input could not start. Please try again.", true); }
  }
  function stopListening() {
    if (!rec) return;
    const r = rec; rec = null; setMic(false); setHint(defaultHint);
    try { r.abort(); } catch { /* already stopped */ }
  }

  /* Plain text for speech: one sentence per paragraph, list item and table row; no markdown symbols. */
  function speechText(root) {
    const out = [];
    const walk = (n) => {
      if (n.nodeType === 3) { out.push(n.textContent); return; }
      if (n.nodeType !== 1) return;
      if (n.tagName === "TR") {
        out.push(" " + [...n.cells].map((c) => c.textContent.trim()).filter(Boolean).join(", ") + ". ");
        return;
      }
      n.childNodes.forEach(walk);
      if (/^(P|LI|H[1-6]|UL|OL|TABLE|BLOCKQUOTE|BR)$/.test(n.tagName)) out.push(". ");
    };
    walk(root);
    return out.join("")
      .replace(/\/yr\b/g, " per year").replace(/\s+/g, " ")
      .replace(/([.!?:;,])\s*(\.\s*)+/g, "$1 ").replace(/\s+\./g, ".").replace(/(\.\s*){2,}/g, ". ").trim();
  }
  /* Chrome cuts off long utterances after about 15 seconds, so speak a few sentences at a time. */
  function chunks(text) {
    const parts = text.split(/(?<=[.!?])\s+/);  // split only at sentence ends, never inside numbers like 1.91
    const out = [];
    for (const p of parts) {
      const s = p.trim(); if (!s) continue;
      if (out.length && (out[out.length - 1] + " " + s).length < 220) out[out.length - 1] += " " + s; else out.push(s);
    }
    return out;
  }
  function pickVoice() {
    const vs = synth().getVoices().filter((v) => v.lang === "en-US" || v.lang === "en_US");
    return vs.find((v) => /natural|aria|jenny|google us english|samantha/i.test(v.name)) || vs[0] || null;
  }
  function markCard(card, on) {
    const b = card && card.querySelector(".read-btn");
    if (!b) return;
    b.classList.toggle("on", on);
    b.setAttribute("aria-label", on ? "Stop reading" : "Read this answer aloud");
    b.title = on ? "Stop reading" : "Read aloud";
  }
  function speakCard(card) {
    if (!canSpeak || !card) return;
    const md = card.querySelector(".md");
    if (!md) return;
    stopSpeaking();
    const pieces = chunks(speechText(md));
    if (!pieces.length) return;
    speakingCard = card; markCard(card, true);
    const v = pickVoice();
    pieces.forEach((text, i) => {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = "en-US"; if (v) u.voice = v;
      if (i === pieces.length - 1) u.onend = () => { if (speakingCard === card) { speakingCard = null; markCard(card, false); } };
      synth().speak(u);
    });
  }
  function stopSpeaking() {
    if (!canSpeak) return;
    if (speakingCard) { markCard(speakingCard, false); speakingCard = null; }
    synth().cancel();
  }
  function toggleCard(card) { if (speakingCard === card) stopSpeaking(); else speakCard(card); }
  function setAutoSpeak(on) {
    state.speak = on; storage.set("wra_speak", on ? "1" : "0");
    const b = $("speak-toggle");
    b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
    b.title = on ? "Reading new answers aloud (click to turn off)" : "Read new answers aloud";
    if (!on) stopSpeaking();
  }

  if (canListen) {
    $("mic").hidden = false;
    $("mic").addEventListener("click", () => (rec ? rec.stop() : startListening()));
  }
  if (canSpeak) {
    $("speak-toggle").hidden = false;
    $("speak-toggle").addEventListener("click", () => setAutoSpeak(!state.speak));
    setAutoSpeak(state.speak);
    synth().getVoices();  // starts loading the voice list (Chrome loads it asynchronously)
  }
  window.addEventListener("pagehide", () => { stopSpeaking(); stopListening(); });
  return { canListen, canSpeak, speakCard, toggleCard, stopSpeaking, stopListening, speechText };
})();


/* ---------------- boot ---------------- */
/* Live warnings load before the leaderboard so its rows can show the warning marker. loadLive never throws. */
function loadAll() { loadOutlook(); loadLive().then(loadBoard); }
(async function boot() {
  renderSamples();
  updateNewChat();
  $("dev-flag").hidden = !state.dev;
  const h = await loadHealth();
  if (h && h.access_code_required && !state.code) { showCodeModal(); return; }
  loadAll();
})();

/* Keep-alive: the free hosting plan puts the server to sleep after about 15 minutes without requests, and a
   sleeping server loses its conversations. While this page is open it checks /health every 10 minutes, which
   keeps the server awake and the status dot current. /health never calls the AI model, so this costs nothing. */
const KEEP_ALIVE_MS = 10 * 60 * 1000;
setInterval(() => {
  loadHealth();
  if (state.live) loadLive().then(loadBoard);   // refresh live warnings once the page has loaded them before
}, KEEP_ALIVE_MS);

/* ---------------- live warnings ---------------- */
/* Live NWS warnings come from GET /api/alerts, filled by the warning monitor every 15 minutes
   (app/alerts/monitor.py). They are shown separately from the historical risk scores, which do not change
   with today's weather. */

/* Format an NWS end time in the hub's own time zone, as NWS states it:
   "2026-11-26T16:00:00-08:00" becomes "Nov 26, 16:00 local time". */
function localTime(iso) {
  const [day, time] = String(iso).split("T");
  const [, m, d] = day.split("-");
  return `${MONTHS[+m - 1].slice(0, 3)} ${+d}, ${(time || "").slice(0, 5)} local time`;
}
function liveWarningsFor(hubId) { return state.liveByHub[hubId] || []; }

/* Small warning icon after a hub name when the hub is under a warning now (null otherwise). */
function liveMark(hubId) {
  const w = liveWarningsFor(hubId);
  return w.length ? el("span", { class: "live-mark", title: `Active now: ${w.map((x) => x.event).join(", ")}`,
    html: WARN_ICON }) : null;
}

/* Fetch the live warnings and update the header button. Errors are swallowed: the rest of the page works
   without live data, and the button simply stays hidden. */
async function loadLive() {
  try {
    const r = await api("/api/alerts?limit=20");
    state.live = r;
    state.liveByHub = {};
    for (const w of r.active_warnings) (state.liveByHub[w.hub_id] ||= []).push(w);
    const hubs = Object.keys(state.liveByHub).length;
    const b = $("live-btn");
    b.hidden = false;
    b.classList.toggle("active", hubs > 0);
    $("live-count").hidden = hubs === 0;
    $("live-count").textContent = hubs;
    $("live-text").textContent = !r.last_check ? "Live warnings: first check pending"
      : hubs ? `${hubs === 1 ? "hub" : "hubs"} under a warning now` : "No live warnings";
  } catch { /* leave the button as it was */ }
}

/* The live warnings window: one row per active warning (click opens the hub), the last check time and a note
   on what this data is. */
function openLive() {
  const r = state.live;
  if (!r) return;
  const rows = r.active_warnings.map((w) => el("button", { class: "live-row", onclick: () => {
    $("live-modal").classList.remove("open"); openHub(w.hub_id); } },
    el("span", { class: "live-hub" }, w.hub_name.replace(/ \(.*\)/, ""), el("small", {}, w.state)),
    hzChip(w.hazard, w.event),
    el("span", { class: "faint num" }, w.ends ? `until ${localTime(w.ends)}` : "")));
  const last = r.last_check ? `Last checked ${r.last_check.checked_at.slice(0, 16).replace("T", " ")} UTC, `
    + `${r.last_check.hubs_checked} hubs answered.` : "The first check has not run yet.";
  fill($("live-body"),
    rows.length ? el("div", { class: "live-list" }, ...rows)
      : el("p", { class: "muted" }, "No hub is under an active weather warning right now."),
    el("p", { class: "faint", style: "font-size:12.5px;margin:14px 0 0" }, `${last} ${r.note}`));
  $("live-modal").classList.add("open");
}
$("live-btn").addEventListener("click", openLive);

/* localStorage wrapper that never throws (private browsing can block storage). */
function makeStorage() {
  return {
    get(k) { try { return window.localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
}
