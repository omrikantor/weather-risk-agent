# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which US distribution hubs are most exposed to weather disruption, and therefore where to invest in resilience first. Rankings and statistics come from deterministic Python code over public data (NWS warnings, FEMA disaster declarations, NOAA station and storm records, Census places). The language model (OpenAI `gpt-6.2-sol`) chooses which tools to call and explains the results, and code checks that every number in an answer came from a tool before it is shown.

What you can do in the web app:

- Ask questions in a chat, for example "Which Midwest hubs are most exposed to winter disruption?", "Compare Miami and Houston for hurricanes and flooding", "What percentage of days in Denver last year had snowfall?" or "Why is the Dallas hub high?".
- See this month's seasonal outlook (which hubs historically face the most disruption in the current month).
- Browse the leaderboard of all 47 hubs by hazard and region, and open any hub to see the evidence behind its score.
- See live warnings: every 15 minutes the app checks which hubs are under an active National Weather Service warning. The header shows how many, affected hubs are marked in the leaderboard and hub panel, and each new or ended warning can be posted to Slack (see Deploy).
- Use voice: click the microphone to speak a question (Chrome, Edge and Safari), check the text and press Enter. Turn on "Read aloud" to hear answers, or click the speaker icon on any answer.

**Live demo:** https://weather-risk-agent-gz90.onrender.com (the access code is provided with the submission). The free hosting plan sleeps when idle, so the first visit can take about a minute.

## Documentation

| Document | Content |
|---|---|
| [DESIGN.md](DESIGN.md) | Architecture, repository structure, data sources and storage choice, how risk is measured, the model and code contract, evaluation, assumptions and limitations |
| [DECISIONS.md](DECISIONS.md) | Decision log: the options considered, what was chosen and why, including the methods we rejected after seeing the data |
| [PLAN.md](PLAN.md) | The implementation plan written at the start of the project |
| [docs/ai-session/](docs/ai-session/) | The full AI session transcript |

## Quickstart (local)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then put your OPENAI_API_KEY in .env
```

Requires Python 3.12. The database (`data/risk.db`) is committed, so no data download is needed to run the app. Settings are described in `.env.example`; only `OPENAI_API_KEY` is required for chat.

## Run the app

```powershell
uvicorn app.api.main:app --reload
```

Open http://127.0.0.1:8000 for the web app. If `APP_ACCESS_CODE` is set in `.env`, the page asks for it once. Add `?dev=1` to the URL (or use "Show technical details" in Methodology) to see tool calls, number tracing, tokens and cost for each answer.

- API docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health
- Every `/api/*` call needs the header `X-Access-Code: <APP_ACCESS_CODE from .env>` (if the code is set).

| Method | Path | Purpose | LLM |
|---|---|---|---|
| POST | `/api/chat` | `{message, session_id?}` -> answer, traced figures, sources, tool trace, cost | yes |
| POST | `/api/chat/reset` | Clear a session | no |
| GET | `/api/scores?measure=&hazard=&region=&top_n=` | Leaderboard | no |
| GET | `/api/outlook?month=&region=&top_n=` | Seasonal outlook for a month (default: current month) | no |
| GET | `/api/hubs` | Hub list | no |
| GET | `/api/hubs/{hub_id}/breakdown` | One hub's risk breakdown | no |
| GET | `/api/methodology` | KPI definitions, severity shares, sources, limitations | no |
| POST | `/api/admin/budget` | Change the daily OpenAI spend cap at runtime (`X-Admin-Token`) | no |
| POST | `/api/admin/alerts/check` | Run one live warning check for all hubs (`X-Admin-Token`) | no |
| GET | `/api/alerts?limit=` | Warnings active at hubs now, recent alerts, last check | no |
| GET | `/health` | Database, data freshness, model, budget | no |

Protection for a public URL: access code, 10 chat messages/minute per IP, and a daily OpenAI spend cap (`DAILY_BUDGET_USD`, default $3, adjustable at runtime).

## Chat from the command line

```powershell
python -m app.agent.cli --interactive --trace
```

## Tests

```powershell
python -m pytest tests -q
```

Tests never call OpenAI (a scripted fake LLM is used).

## Evaluation (real model)

```powershell
python -m evals.run_evals              # 21 cases, about $0.30
python -m evals.run_evals --repeat 2   # consistency run
python -m evals.run_evals --case dallas_why
```

The cases in [evals/cases.yaml](evals/cases.yaml) cover rankings, comparisons, statistics, explanations, timing, methodology, stand-in hubs, place names, false premises, follow-ups, live warnings and guardrails. Checks are deterministic: expected tool calls and arguments, expected figures and ranking order (computed by calling the same tools directly, never hard-coded), required phrases, confirmation behavior, plus validation and plain-text checks on every answer. The latest report is [evals/results/eval-latest.json](evals/results/eval-latest.json).

## Rebuild the data (optional)

```powershell
python -m app.ingest.build_db
python -m app.ingest.compact_db
```

## Deploy (Render)

The repository includes a `Dockerfile` and a Render Blueprint (`render.yaml`). The image contains the committed database, so the service starts without downloading data.

1. In Render, choose New, then Blueprint, and connect this GitHub repository. Render reads `render.yaml` and creates one Docker web service.
2. Enter the three secrets when asked: `OPENAI_API_KEY`, `APP_ACCESS_CODE` (the code users type in the web app) and `ADMIN_TOKEN` (for changing the daily budget at runtime). The model, reasoning effort, daily budget and rate limit are already set in `render.yaml`.
3. Wait for the build to finish and open the service URL. `/health` should report the database and model.

On Render's free plan the service sleeps after 15 minutes without traffic, and the first request after that takes about a minute. Open the URL shortly before a demo.

### Live warning alerts (optional)

Every 15 minutes `.github/workflows/alerts.yml` asks the deployed app to check all hubs for NWS warnings that started or ended. To turn it on:

1. In the GitHub repository, open Settings, then Secrets and variables, then Actions. Add the secret `ADMIN_TOKEN` (the same value as on Render) and the variable `APP_URL` (the Render URL).
2. Optionally, in Render's Environment settings add `ALERT_WEBHOOK_URL` (a Slack incoming webhook, or a free test URL from webhook.site) to receive each alert as a message. Without it, alerts are still recorded and listed at `/api/alerts`.
3. To test immediately, open the Actions tab, choose "Hub alert check" and click "Run workflow".

A single check can also be run locally: `python -m app.alerts.monitor`.

To run the same image locally:

```powershell
docker build -t weather-risk-agent .
docker run --env-file .env -p 8000:8000 weather-risk-agent
```
