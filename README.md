# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which US distribution hubs are most exposed to weather disruption. Rankings and scores come from deterministic Python code; the LLM (OpenAI `gpt-6.2-sol`) chooses which tools to call and explains the results, and code verifies that every number in an answer came from a tool.

> Work in progress. See [PLAN.md](PLAN.md) for the implementation plan and decision log.

## Quickstart (local)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then put your OPENAI_API_KEY in .env
```

The database (`data/risk.db`) is committed, so no data download is needed to run the app.

## Run the API

```powershell
uvicorn app.api.main:app --reload
```

- API docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health
- Every `/api/*` call needs the header `X-Access-Code: <APP_ACCESS_CODE from .env>` (if the code is set).

| Method | Path | Purpose | LLM |
|---|---|---|---|
| POST | `/api/chat` | `{message, session_id?}` → answer, traced figures, sources, tool trace, cost | yes |
| POST | `/api/chat/reset` | Clear a session | no |
| GET | `/api/scores?measure=&hazard=&region=&top_n=` | Leaderboard | no |
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

Tests never call OpenAI (a scripted fake LLM is used). A smoke test against the real model: `python -m evals.smoke`.

## Rebuild the data (optional)

```powershell
python -m app.ingest.build_db
python -m app.ingest.compact_db
```
