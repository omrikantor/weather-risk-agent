# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which US distribution hubs are most exposed to weather disruption, and therefore where to invest in resilience first. Rankings and statistics come from deterministic Python code over public data (NWS warnings, FEMA disaster declarations, NOAA station and storm records, Census places). The language model (OpenAI `gpt-6.2-sol`) chooses which tools to call and explains the results, and code checks that every number in an answer came from a tool before it is shown.

What you can do in the web app:

- Ask questions in a chat, for example "Which Midwest hubs are most exposed to winter disruption?", "Compare Miami and Houston for hurricanes and flooding", "What percentage of days in Denver last year had snowfall?" or "Why is the Dallas hub high?".
- See this month's seasonal outlook (which hubs historically face the most disruption in the current month).
- Browse the leaderboard of all 47 hubs by hazard and region, and open any hub to see the evidence behind its score.

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
python -m evals.run_evals              # 20 cases, about $0.30
python -m evals.run_evals --repeat 2   # consistency run
python -m evals.run_evals --case dallas_why
```

The cases in [evals/cases.yaml](evals/cases.yaml) cover rankings, comparisons, statistics, explanations, timing, methodology, stand-in hubs, place names, false premises, follow-ups and guardrails. Checks are deterministic: expected tool calls and arguments, expected figures and ranking order (computed by calling the same tools directly, never hard-coded), required phrases, confirmation behavior, plus validation and plain-text checks on every answer. The latest report is [evals/results/eval-latest.json](evals/results/eval-latest.json).

## Rebuild the data (optional)

```powershell
python -m app.ingest.build_db
python -m app.ingest.compact_db
```
