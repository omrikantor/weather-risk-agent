# Weather Risk Intelligence Agent — Implementation Plan

> Built with Claude Code. This plan is part of the "AI session" deliverable.

## Decisions log (made before this plan)

| Decision | Choice | Why |
|---|---|---|
| Architecture | **Approach A+**: one tool-calling agent; all scoring is deterministic Python over a SQLite store | Flexible enough for follow-ups and multi-part questions; reproducible numbers; fits in about 1 day |
| Rejected: B (intent parser + router) | Not used as the core. Its discipline is kept: strict schemas, and evals that test structure and numbers separately | Too rigid for open-ended follow-ups |
| Rejected: C (LangGraph multi-agent) | Not used. Noted in DESIGN.md as "how this would scale" | One agent with good tools is enough for this scope. Extra agents add latency, cost, failure modes and demo risk |
| Orchestration | Plain OpenAI Python SDK loop (no framework) | Transparent; JSON-schema enforcement is direct; easy to debug live |
| Borrowed from C | MCP server, code-based answer validator ("critic as code"), browser voice, scheduled alerts | Cheap to add, valuable, and good to talk through in the interview |
| LLM | **OpenAI `gpt-6.2-sol`** (switched from Claude Sonnet 5.5 because of Anthropic's prepay tiers; $10 of OpenAI credit bought) | Mid-tier price ($2 / $10 per 1M tokens, $0.10 cached input), strict function calling + structured outputs. **One model for development, evals and demo**: tuning on a cheaper model and demoing on another would mean demoing an untested configuration. Expected spend with automatic prompt caching is about $6–8 |
| Storage | SQLite (single file, committed snapshot) | About 90k rows; no server needed; ships with the container |
| Deployment | Docker → Render (or Railway) public URL, with a local run as backup | URL is "preferred"; interviewers can try it themselves |

## Priority tiers (cut line if time runs short)

- **MUST:** Phases 0–8, 11, 12, 13 (core agent, API, UI, evals, deploy, docs, transcript)
- **SHOULD:** Phase 9 (alerts, a listed bonus) and Phase 10 (MCP server)
- **COULD:** voice in Phase 7, LLM-as-judge in Phase 8, NWS live alerts tool

Cost-saving order: Phases 2–4 use no LLM calls at all. Build and test the deterministic core first.

---

## Phase 0 — Accounts, keys, tools (≈45 min) — *you do this*

1. **Local tools:** Python 3.12+ (`python --version`), Git (`git --version`), a GitHub account, and VS Code (optional).
2. **OpenAI API key**
   1. Go to https://platform.openai.com and sign up or log in.
   2. Billing: buy credits ($10 bought; expected spend on `gpt-6.2-sol` with prompt caching is about $6–8).
   3. Settings → Limits: set a **monthly budget / usage limit**. This protects you once the URL is public.
   4. API keys → Create new secret key (name it `weather-risk-agent`) → copy it **once**.
   5. Paste it **only** into the local `.env` file (created in Phase 1). Never paste it in chat or commit it.
3. **GitHub:** create an empty repo, e.g. `weather-risk-agent` (public or private; public is easier to share).
4. **Render:** sign up at https://render.com with GitHub. Nothing to configure yet.
5. **Webhook target for the alert demo:** open https://webhook.site and copy your unique URL. A Slack incoming webhook also works if you have a workspace.
6. No keys are needed for the data sources: Open-Meteo, FEMA NRI and NWS are all public.

**Done when:** you have an API key in hand, a spend limit set, an empty GitHub repo and a Render account.

---

## Phase 1 — Repository skeleton & config (≈45 min)

**Target structure**
```
weather-risk-agent/
├── README.md                  # how to run locally / deployed, demo questions
├── DESIGN.md                  # architecture, repo structure, storage, scoring, assumptions
├── PLAN.md                    # this plan
├── DECISIONS.md               # short log of design decisions + why
├── requirements.txt
├── .env.example               # OPENAI_API_KEY=, LLM_MODEL=, APP_ACCESS_CODE=, ADMIN_TOKEN=, ALERT_WEBHOOK_URL=
├── .gitignore                 # .env, .venv, data/raw/, __pycache__, evals/results/*.json (keep latest)
├── Dockerfile
├── data/
│   ├── hubs.yaml              # hub list: id, name, city, state, lat, lon, county_fips, region
│   ├── raw/                   # downloaded API responses / NRI CSV (gitignored cache)
│   └── risk.db                # SQLite snapshot (committed)
├── app/
│   ├── config.py              # pydantic-settings: env vars, paths, model name
│   ├── db.py                  # SQLite connection + schema creation
│   ├── ingest/
│   │   ├── open_meteo.py      # daily history per hub
│   │   ├── fema_nri.py        # county hazard scores
│   │   ├── nws_alerts.py      # live active alerts (optional tool + alerts)
│   │   └── build_db.py        # CLI: python -m app.ingest.build_db
│   ├── scoring/
│   │   ├── weights.yaml       # thresholds, hazard weights, tier cutoffs, version
│   │   ├── metrics.py         # per-hub climate metrics from daily data
│   │   └── engine.py          # normalization, sub-scores, composite, tiers, breakdown
│   ├── tools/
│   │   ├── schemas.py         # Pydantic input models for every tool (strict)
│   │   └── registry.py        # tool functions + OpenAI function-tool definitions
│   ├── agent/
│   │   ├── llm_client.py      # the ONLY module that talks to OpenAI
│   │   ├── prompts.py         # system prompt
│   │   ├── answer_schema.py   # FinalAnswer Pydantic model (the LLM→code contract)
│   │   ├── validator.py       # deterministic "critic": numbers must come from tool results
│   │   ├── sessions.py        # conversation memory per session_id
│   │   └── agent.py           # the tool-use loop
│   ├── alerts/
│   │   ├── monitor.py         # recompute → diff vs last scores → alert rows
│   │   └── notifier.py        # POST to webhook
│   ├── api/
│   │   └── main.py            # FastAPI app, routes, static UI, scheduler start
│   └── mcp_server.py          # MCP server exposing the same tools
├── web/
│   ├── index.html             # chat UI
│   ├── app.js                 # fetch /api/chat, render answer cards, voice
│   └── styles.css
├── evals/
│   ├── cases.yaml             # 15–20 eval cases
│   ├── oracle.py              # ground truth computed directly from DB/engine
│   ├── run_evals.py           # python -m evals.run_evals
│   └── results/               # timestamped JSON reports
├── tests/                     # pytest, no LLM calls
│   ├── test_metrics.py
│   ├── test_engine.py
│   ├── test_tools.py
│   └── test_validator.py
└── docs/
    └── ai-session/            # exported Claude Code transcript (mandatory deliverable)
```

**Steps**
1. `git init`, add the GitHub remote, create the folders above.
2. Virtual env: `python -m venv .venv` → `.venv\Scripts\Activate.ps1`.
3. `requirements.txt`: `openai`, `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `httpx`, `pyyaml`, `pandas`, `apscheduler`, `mcp`, `pytest`, `python-dotenv`.
4. `app/config.py` reads `.env`: `OPENAI_API_KEY`, `LLM_MODEL=gpt-6.2-sol`, `LLM_REASONING_EFFORT=medium`, `APP_ACCESS_CODE`, `ADMIN_TOKEN`, `ALERT_WEBHOOK_URL`, `DB_PATH`.
5. Copy `.env.example` to `.env` and paste the key in (you do this).
6. First commit: "skeleton".

**Done when:** `python -c "import app.config"` works and the repo is pushed.

---

## Phase 2 — Hub list & data ingestion (≈2.5 h, no LLM cost)

### 2.1 Hub list (`data/hubs.yaml`): ~24 hubs, an explicit assumption
Region = US Census Bureau regions (documented definition of "Midwest").
- **Midwest:** Chicago IL, Minneapolis MN, Detroit MI, Indianapolis IN, Columbus OH, Kansas City MO, St. Louis MO, Omaha NE
- **South:** Dallas TX, Houston TX, Miami FL, Atlanta GA, Memphis TN, Louisville KY, Charlotte NC, New Orleans LA
- **West:** Denver CO, Phoenix AZ, Los Angeles CA, Seattle WA, Salt Lake City UT
- **Northeast:** Newark NJ (NYC metro), Boston MA, Philadelphia PA

Each entry has `id` (slug, e.g. `dallas`), display name, lat/lon (a distribution-area point near the city), `county_fips` and `region`. Get the county FIPS once with the FCC Area API (`https://geo.fcc.gov/api/census/area?lat=..&lon=..`) and hard-code it.

### 2.2 SQLite schema (`app/db.py`)
- `hubs(id PK, name, state, lat, lon, county_fips, region)`
- `daily_weather(hub_id, date, tmax_c, tmin_c, precip_mm, snowfall_cm, wind_gust_kmh, weather_code, PK(hub_id,date))`
- `nri_hazards(hub_id, hazard, risk_score, risk_rating, source_version)`, where hazard ∈ winter_weather, ice_storm, cold_wave, hurricane, coastal_flood, riverine_flood, tornado, hail, strong_wind, heat_wave
- `hub_metrics(hub_id, metric, value, period_start, period_end)`
- `risk_scores(hub_id, computed_at, scoring_version, composite, winter, hurricane, flood, convective, heat, tier, details_json)`, which keeps history so the alert job can diff
- `alerts(id, hub_id, created_at, kind, old_value, new_value, message, delivered)`
- `ingest_log(source, fetched_at, params, status)`, for data freshness shown in answers

### 2.3 Open-Meteo history (`ingest/open_meteo.py`)
- Endpoint: `https://archive-api.open-meteo.com/v1/archive`
- Params: `latitude, longitude, start_date=2016-01-01, end_date=2025-12-31, timezone=auto, daily=temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,wind_gusts_10m_max,weather_code`
- One request per hub for all 10 years. Cache the raw JSON in `data/raw/`, add a polite delay, and retry with backoff.
- Also fetch 2026-01-01 → (today − 5 days) for the "current year" refresh used by alerts.
- Note in the docs: ERA5-based reanalysis grid (~9–25 km), not station data. Free for non-commercial use; production would use the commercial plan.

### 2.4 FEMA National Risk Index (`ingest/fema_nri.py`)
- Download the county-level NRI table (CSV) from FEMA's NRI data resources page once and store it in `data/raw/`.
- Pick each hazard's **risk score** column (e.g. `WNTW_RISKS`, `ISTM_RISKS`, `CWAV_RISKS`, `HRCN_RISKS`, `CFLD_RISKS`, `RFLD_RISKS`, `TRND_RISKS`, `HAIL_RISKS`, `SWND_RISKS`, `HWAV_RISKS`). **Verify the exact names against the NRI data dictionary at download time.**
- Join on `county_fips`. NRI scores are national percentiles (0–100). Missing values (e.g. no coastal flood inland) are stored as 0 and the substitution is recorded.

### 2.5 `build_db.py` CLI
`python -m app.ingest.build_db [--refresh]` creates the schema, loads hubs, ingests Open-Meteo + NRI, then calls the scoring engine (Phase 3) and writes `risk_scores`.

**Done when:** `risk.db` holds about 24 × 3,650 daily rows plus NRI rows for every hub, and a quick SQL query of Denver's 2025 snow days returns a sensible number.

---

## Phase 3 — Metrics & deterministic scoring engine (≈2.5 h, no LLM cost)

### 3.1 Thresholds (`scoring/weights.yaml`, versioned, e.g. `scoring_version: 1.0`)
| Metric | Definition |
|---|---|
| `snow_day` | snowfall ≥ 1 cm (a trace day is ≥ 0.1 cm; both are reported for the Denver-style question) |
| `heavy_snow_day` | snowfall ≥ 10 cm |
| `ice_day` | weather_code ∈ {56, 57, 66, 67} (freezing drizzle/rain; a model-derived proxy) |
| `extreme_cold_day` | tmin ≤ −15 °C |
| `heavy_rain_day` | precip ≥ 50 mm |
| `max_3day_precip` | max rolling 3-day precip sum per year |
| `damaging_wind_day` | gust ≥ 80 km/h |
| `extreme_heat_day` | tmax ≥ 38 °C (100 °F) |
| `disruption_day` | any of: snow ≥ 5 cm, ice_day, gust ≥ 80 km/h, precip ≥ 50 mm, tmax ≥ 40 °C |

### 3.2 Metrics (`scoring/metrics.py`)
- Annual averages over 2016–2025 per hub, plus per-year values (so "last year" questions work).
- Generic function `day_fraction(hub, metric, start, end)` returns count, total days and percentage. This powers "% of days with snowfall".

### 3.3 Sub-scores (0–100) (`scoring/engine.py`)
Each hazard family = `w_nri × NRI component + w_climate × climate component`
- The NRI component is the mean of the relevant NRI hazard scores (already national percentiles).
- The climate component is the percentile rank of the relevant metrics **among our hubs** (relative), averaged.

| Family | NRI inputs | Climate inputs | w_nri / w_climate |
|---|---|---|---|
| Winter | winter_weather, ice_storm, cold_wave | snow_days, heavy_snow_days, ice_days, extreme_cold_days | 0.5 / 0.5 |
| Hurricane | hurricane, coastal_flood | damaging_wind_days | 0.8 / 0.2 (10 years is too short a window for hurricanes) |
| Flood | riverine_flood, coastal_flood | heavy_rain_days, max_3day_precip | 0.5 / 0.5 |
| Convective | tornado, hail, strong_wind | damaging_wind_days | 0.6 / 0.4 |
| Heat | heat_wave | extreme_heat_days | 0.5 / 0.5 |

### 3.4 Composite = Weather Disruption Risk Index (WDRI)
- Weights (an assumption, editable): winter 0.25, flood 0.20, hurricane 0.20, convective 0.20, heat 0.15.
- Tiers: High ≥ 60, Elevated 40–60, Low < 40.
- Companion KPI: **expected disruption days per year** (from `disruption_day`). It's business-friendly ("Dallas loses about N days/yr to weather").
- `breakdown(hub)` returns sub-scores, each one's weighted contribution, the top drivers (sorted), the raw metrics behind them and the NRI inputs. This powers the "why is Dallas high?" question.

### 3.5 Unit tests (`tests/test_metrics.py`, `tests/test_engine.py`)
Synthetic data checks: thresholds count correctly; weights sum to 1; scores stay in 0–100; ranking is deterministic; the breakdown's contributions sum to the composite.

**Done when:** `pytest` passes and `python -m app.scoring.engine --print` shows a ranked table that passes a sanity check (e.g. Minneapolis is high on winter, Miami/Houston/New Orleans are high on hurricane). **Look at the real results.** If Dallas isn't actually "high", the agent must say so (and that becomes an eval case).

---

## Phase 4 — Tools layer (≈1.5 h, no LLM cost)

Every tool is a plain Python function with a **Pydantic input model**. The OpenAI function-tool definitions are generated from these models with `strict: true` and `additionalProperties: false` (OpenAI strict mode also requires every property to be listed in `required`; optional fields become nullable). `hub_id` and `region` are **enums**, so the model cannot invent a hub.

| Tool | Input | Returns |
|---|---|---|
| `list_hubs` | `region?` | hubs with region and state |
| `rank_hubs` | `hazard` (composite/winter/hurricane/flood/convective/heat), `region?`, `top_n` | ordered list: score, tier, rank, disruption days |
| `compare_hubs` | `hub_ids[2..5]`, `hazards[]` | side-by-side sub-scores + key metrics + NRI inputs |
| `get_hub_risk_breakdown` | `hub_id` | composite, tier, contributions, top drivers, raw metrics |
| `get_weather_stat` | `hub_id`, `metric` (enum), `start_date`, `end_date` | count, total_days, pct, threshold used, data source + coverage |
| `get_methodology` | — | thresholds, weights, data sources, data window, assumptions |
| `get_active_alerts` *(COULD)* | `hub_id` | live NWS alerts (`api.weather.gov/alerts/active?point=lat,lon`, generic User-Agent) |

- Each tool result includes `source`, `data_window` and `scoring_version`, so answers can cite them.
- Errors are returned as structured `{error, hint}` and sent to the model as `is_error: true` tool results.
- `tests/test_tools.py`: every tool runs on the real DB, and its output is JSON-serializable and stable.

**Done when:** every tool can be called from a Python REPL and returns correct, cited output.

---

## Phase 5 — Agent core (≈3 h, first LLM spend)

### 5.1 `llm_client.py` (the only module that imports `openai`)
- Uses the OpenAI **Responses API**: `client.responses.create(model=settings.LLM_MODEL, instructions=system_prompt, input=history, tools=[function tools, strict], reasoning={"effort": settings.LLM_REASONING_EFFORT}, text={"format": {"type": "json_schema", "name": "final_answer", "schema": FinalAnswer schema, "strict": True}})`. **Verify the exact parameter shapes against the installed `openai` SDK (3.57.0) docs** before writing the loop.
- **Prompt caching:** OpenAI caches long identical prompt prefixes automatically. Keep the system prompt and tool list byte-stable and first, and check `usage.input_tokens_details.cached_tokens > 0`.
- **Refusal handling:** with structured outputs the model can return a `refusal` instead of JSON. Detect it and return a clean `out_of_scope` answer. Also handle `incomplete` responses (e.g. max output tokens).
- Log usage and estimated cost per call (input/output/cached/reasoning tokens), which feeds cost reporting in the evals.
- **Budget controls ($10 credit):** max 8 steps per turn; trim old tool results from history; full eval runs only at milestones (`--repeat 1`), `--case` while debugging; hard usage limit in OpenAI billing settings.
- `tool_choice="auto"` plus prompt instructions. The final answer is enforced through the `json_schema` text format. **Verify early** that structured output composes with the tool loop on `gpt-6.2-sol`. Fallback if it doesn't: a strict `submit_answer` function tool whose parameters *are* the FinalAnswer schema.
- Provider isolation: the rest of the code only sees neutral types (`ToolCall`, `LLMResponse`), so swapping providers again means changing this one file.

### 5.2 FinalAnswer schema (`answer_schema.py`), the LLM → code contract
```
FinalAnswer:
  question_type: ranking | comparison | statistic | explanation | methodology | clarification | out_of_scope
  answer_markdown: str                # the human-readable answer
  hubs: [ {hub_id, rank?, composite_score?, tier?, key_drivers: [str]} ]
  figures: [ {label, value: number, unit, source_tool} ]   # every number cited in the answer
  assumptions: [str]
  uncertainty: {level: low|medium|high, reasons: [str]}
  data_sources: [str]
  suggested_followups: [str]
```

### 5.3 System prompt (`prompts.py`)
- Role: a weather-risk analyst assistant for hub investment prioritization.
- Hard rules: **never compute or estimate numbers yourself; every number must come from a tool result**; use `get_methodology` to explain scoring; say which assumptions apply; state uncertainty; if a hub or region isn't in the network, say so; push back on false premises ("Dallas is not actually in the top tier, here's why"); short answers with clear structure.
- Definitions the model should use: "last year" = the previous full calendar year relative to today (inject today's date per turn as a short developer message after the stable prefix, so it doesn't break prompt caching), and the Census region definitions.

### 5.4 The loop (`agent.py`)
```
run_turn(session_id, user_message):
  history = sessions.get(session_id) + user_message
  for step in range(MAX_STEPS=8):
      resp = llm.call(history)
      append resp.content (full blocks) to history
      if resp has function_call items:
          run ALL function calls (parallel ok) → append one function_call_output per call_id
          continue
      if resp is a final message:
          answer = FinalAnswer.parse(resp)
          ok, problems = validator.check(answer, tool_results_this_turn)
          if not ok and not retried: append system feedback, retry once
          break
  return answer, trace (tool calls + args + timings + usage)
```

### 5.5 Validator (`validator.py`), "critic as code"
1. Pydantic schema validation.
2. Every `figures[].value` must match a number in this turn's tool results (or in earlier turns' results for follow-ups), within a rounding tolerance.
3. Every `hub_id` exists.
4. Ranking answers must keep the engine's ordering.
5. On failure: one retry with the specific problems; otherwise return the answer with `validation_warnings`.
Tests: `tests/test_validator.py` (fabricated number → caught; rounded number → accepted).

### 5.6 Sessions (`sessions.py`)
In-memory dict `session_id → messages` with a TTL. Cap the history by trimming old tool_result payloads beyond N turns. Optionally persist to SQLite. Follow-ups such as "what about Chicago?" work because the model sees the history.

**Done when:** a CLI `python -m app.agent.agent "Which Midwest hubs are most exposed to winter disruption?"` returns valid FinalAnswer JSON that passes the validator, and all 4 sample questions plus a follow-up work.

---

## Phase 6 — API (≈1 h)

FastAPI (`app/api/main.py`):
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chat` | `{session_id?, message}` → `{session_id, answer: FinalAnswer, trace, usage, latency_ms}` |
| GET | `/api/hubs` | hub list |
| GET | `/api/scores` | current scores table (no LLM) |
| GET | `/api/hubs/{id}/breakdown` | deterministic breakdown (no LLM) |
| GET | `/api/alerts` | recent score-change alerts |
| POST | `/api/admin/recompute` | `ADMIN_TOKEN` header; refresh + rescore + alert check |
| GET | `/health` | liveness + data freshness |
| GET | `/` | serves `web/` static UI |

- Access control: `X-Access-Code` header must equal `APP_ACCESS_CODE` (protects your credits).
- Simple per-IP rate limit (e.g. 20 requests/min, in-memory).
- CORS: same origin only.
- OpenAPI docs at `/docs`, handy to show in the interview.

**Done when:** `uvicorn app.api.main:app --reload` and a `curl` POST to `/api/chat` return a valid answer.

---

## Phase 7 — Chat UI (+ voice) (≈1.5 h)

Plain HTML/JS, no build step (`web/`):
- Access-code prompt (stored in `localStorage`) and a chat thread with `session_id` kept across messages.
- Each answer renders as a card: the markdown answer; a small table of hubs/scores; a collapsible "How I got this" section (tool calls + args, assumptions, uncertainty badge, data sources); and suggested follow-up chips you can click.
- Sidebar or tab: the current risk leaderboard from `/api/scores` (no LLM).
- **Voice (COULD):** a mic button using the browser Web Speech API (`SpeechRecognition`, Chrome/Edge) and a "read aloud" toggle using `speechSynthesis`. No backend change.
- Loading state, error display and a "New chat" button.

**Done when:** all four sample questions plus follow-ups work end to end in the browser.

---

## Phase 8 — Evaluation set & runner (≈2 h)

### 8.1 `evals/cases.yaml` (15–20 cases)
| Category | Example | Checks |
|---|---|---|
| Ranking | Midwest winter exposure | top-3 hub set ⊆ oracle top-4; ordering consistent; region filter respected |
| Comparison | Miami vs Houston, hurricane & flood | both hubs present; figures match oracle; correct "higher" direction |
| Statistic | % of 2025 days with snowfall in Denver | value within ±0.5 pp of oracle; threshold stated |
| Explanation | Why is Dallas high? | top drivers match engine breakdown top-2; or correctly disputes the premise |
| Follow-up (multi-turn) | "…and what about Chicago?" | resolves context; correct hub |
| Methodology | "How do you score hubs?" | mentions weights + sources |
| False premise | "Why is Seattle the riskiest hub?" | does not agree; cites actual rank |
| Out of scope | "Weather in Paris tomorrow?" | `question_type=out_of_scope`; no fabricated figures |
| Unknown hub | "Risk for our Boise hub?" | says it's not in the network |

### 8.2 Checks (deterministic, from `evals/oracle.py`, which calls the engine/DB directly)
`schema_valid`, `validator_passed`, `expected_tools_called`, `figures_match_oracle`, `ranking_matches`, `must_mention` / `must_not_mention`, `question_type`.
*COULD:* an LLM-as-judge (cheap model, e.g. `gpt-6-luna`) scoring explanation clarity 1–5 with a rubric.

### 8.3 Runner
`python -m evals.run_evals [--case ID] [--category X] [--repeat N]` prints a pass/fail table plus per-category pass rate, average latency, tokens and estimated cost, and writes `evals/results/<timestamp>.json`.
Run it, fix the failures (prompt or tool descriptions), and re-run. Keep the final report in the repo and mention the pass rate in the README.

**Done when:** the runner works end to end and the final pass rate is recorded (honest, including failures).

---

## Phase 9 — Score-change alerts (bonus, SHOULD) (≈1.5 h)

1. `alerts/monitor.py`: refresh recent Open-Meteo days (+ NWS active alerts if built) → recompute scores → compare with the last stored `risk_scores` row per hub → create an alert when |Δcomposite| ≥ 3 points **or** the tier changes (thresholds in `weights.yaml`).
2. `alerts/notifier.py`: POST JSON (`hub, old, new, delta, tier change, top changed driver`) to `ALERT_WEBHOOK_URL` (webhook.site or Slack), and mark the alert delivered.
3. Scheduling: APScheduler daily job started in FastAPI's lifespan, plus `POST /api/admin/recompute` for on-demand and external-cron triggers.
4. Demo honesty: climate scores move slowly, so add `?simulate_delta=hub:+8` to the admin endpoint to demonstrate the pipeline, **clearly labeled as a simulation**. NWS active alerts give genuine short-term changes.
5. A UI "Alerts" panel lists recent alerts.

**Done when:** triggering a recompute (simulated) shows an alert in webhook.site and in the UI.

---

## Phase 10 — MCP server (SHOULD) (≈1 h)

1. `app/mcp_server.py` uses the official `mcp` Python SDK (`FastMCP`) and registers the **same** tool functions from `app/tools/registry.py`, with no duplicate logic. Optionally it also exposes `ask_agent(question)`, which calls the full agent.
2. Runs over stdio: `python -m app.mcp_server`.
3. README: a Claude Desktop `claude_desktop_config.json` snippet and a screenshot of Claude Desktop answering through these tools.
4. Interview point: analysts can use the risk tools from any MCP-capable AI client they already use, and the scoring stays deterministic.

**Done when:** Claude Desktop (or MCP Inspector) lists the tools and calls `rank_hubs` successfully.

---

## Phase 11 — Deploy (≈1 h) — *partly you (clicks in Render)*

1. `Dockerfile`: `python:3.12-slim` → install requirements → copy `app/`, `web/`, `data/risk.db`, `data/hubs.yaml` → `CMD uvicorn app.api.main:app --host 0.0.0.0 --port $PORT`.
2. Test locally: `docker build -t wra .` and `docker run -p 8000:8000 --env-file .env wra`.
3. Push to GitHub. **Check that `.env` is not committed.**
4. Render → New Web Service → connect the repo → Docker runtime → set env vars: `OPENAI_API_KEY`, `LLM_MODEL=gpt-6.2-sol`, `APP_ACCESS_CODE`, `ADMIN_TOKEN`, `ALERT_WEBHOOK_URL`.
5. Smoke-test the URL: `/health`, the UI and the four sample questions.
6. Known limits (documented): the free tier sleeps, so open the URL a few minutes before the interview; the filesystem is ephemeral, so the committed DB snapshot is the source and is rebuilt by `build_db` if needed; the scheduler only runs while the app is awake, so production would use an external cron hitting `/api/admin/recompute`.

**Done when:** the public URL answers the sample questions.

---

## Phase 12 — Documentation (≈1.5 h)

**README.md**: what it is (2 lines); live URL + access-code note; quickstart (venv, `.env`, `build_db`, `uvicorn`, open browser); run tests; run evals (+ latest pass rate); MCP usage; sample questions; known limitations.

**DESIGN.md** (short, 2–4 pages):
1. **System architecture**: a Mermaid diagram (Browser UI → FastAPI → Agent loop ⇄ OpenAI API; Agent → Tools → Scoring engine → SQLite; Ingest → Open-Meteo / FEMA NRI / NWS → SQLite; Scheduler → Monitor → Webhook; MCP server → Tools), plus the lifecycle of one request.
2. **Repository structure**: the tree above with a one-liner per module.
3. **Data storage choice**: SQLite and why (size, zero-ops, ships in the container, SQL for analysts); the schema; when to move to Postgres (multi-writer, multi-tenant, history at scale).
4. **Scoring methodology (the KPI)**: thresholds, sub-scores, weights, tiers, disruption days; why there's a hybrid of NRI and observed climate.
5. **LLM contract**: tool schemas (strict), the FinalAnswer schema, the validator, retry policy, prompt caching.
6. **Evaluation**: approach, categories, latest results.
7. **Assumptions, uncertainty, scope**: invented hub list and coordinates; Census regions; 2016–2025 window; reanalysis grid ≠ station data; NRI is county-level and includes exposure and social factors; scores are *relative* within the network; the hurricane signal relies on NRI; no hub business value (throughput/revenue) is used, though in reality priority = hazard × business value (a natural next step); weights are judgment calls.
8. **What I'd do next**: business-value weighting, station data (NOAA GHCN), Storm Events history, Postgres, multi-agent/LangGraph only if workflows grow, auth/SSO, observability.

**DECISIONS.md**: the decision log from the top of this plan, with one line each on the alternatives considered.

---

## Phase 13 — Final QA, demo prep, submission (≈1 h)

1. Fresh-clone test: follow the README from zero in a new folder and confirm it works.
2. `pytest` all green; final `run_evals`, committed.
3. Rehearse a demo script (about 5 min): the leaderboard → Midwest winter question → "what about Chicago?" follow-up → Miami vs Houston → Denver snow % → "Why is Dallas high?" → "How do you score?" → the "How I got this" panel → trigger a simulated alert → MCP in Claude Desktop → voice question.
4. **Export the full Claude Code session** (this conversation, including the planning) into `docs/ai-session/` (transcript export from the app; the raw JSONL is also under `~/.claude/projects/...`). **Mandatory deliverable.**
5. Final commit and push. Submit: the GitHub link, the live URL + access code, and a note pointing to DESIGN.md, the eval results and the AI session.

---

## Time budget (≈18 h total)

| Phase | Est. |
|---|---|
| 0 Accounts | 0.75 h |
| 1 Skeleton | 0.75 h |
| 2 Ingestion | 2.5 h |
| 3 Scoring | 2.5 h |
| 4 Tools | 1.5 h |
| 5 Agent | 3 h |
| 6 API | 1 h |
| 7 UI + voice | 1.5 h |
| 8 Evals | 2 h |
| 9 Alerts | 1.5 h |
| 10 MCP | 1 h |
| 11 Deploy | 1 h |
| 12 Docs | 1.5 h |
| 13 QA & submit | 1 h |
