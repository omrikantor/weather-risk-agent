"""FastAPI backend: the chat endpoint (agent), data endpoints (tools, no LLM), health, static UI.

Run locally:  uvicorn app.api.main:app --reload
"""

import logging
import threading
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent.agent import Agent
from app.agent.llm_client import LLMUnavailableError
from app.api.guards import DailyBudget, RateLimiter, code_matches
from app.config import REPO_ROOT, get_settings
from app.db import get_connection
from app.tools import schemas as s
from app.tools.hub_tools import get_hub_risk_breakdown, get_methodology, list_hubs, rank_hubs
from app.tools.locate import find_nearest_hubs

log = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)


class ResetRequest(BaseModel):
    session_id: str = Field(max_length=64)


class BudgetRequest(BaseModel):
    daily_budget_usd: float = Field(gt=0, le=50)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.budget = DailyBudget(settings.daily_budget_usd)
    app.state.limiter = RateLimiter(settings.chat_rate_limit_per_min)
    app.state.session_locks = defaultdict(threading.Lock)
    if getattr(app.state, "agent", None) is None:            # tests inject a fake agent
        app.state.agent = Agent() if settings.openai_api_key else None
    if not settings.app_access_code:
        log.warning("APP_ACCESS_CODE is empty: the API is open (OK for local development only)")
    find_nearest_hubs("Tampa, FL")                            # warm place index and hazard profiles
    yield


app = FastAPI(title="Weather Risk Intelligence Agent", version="1.0", lifespan=lifespan,
              description="Ranks US logistics hubs by weather-disruption exposure and answers questions about it.")
origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- dependencies


def require_access(x_access_code: str | None = Header(default=None)) -> None:
    expected = get_settings().app_access_code
    if expected and not code_matches(x_access_code, expected):
        raise HTTPException(401, "Missing or wrong access code (X-Access-Code header).")


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_token
    if not expected:
        raise HTTPException(403, "Admin endpoints are disabled (ADMIN_TOKEN not set).")
    if not code_matches(x_admin_token, expected):
        raise HTTPException(401, "Missing or wrong admin token (X-Admin-Token header).")


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")            # behind Render's proxy
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


# --------------------------------------------------------------------------- chat


@app.post("/api/chat", dependencies=[Depends(require_access)], tags=["chat"])
def chat(body: ChatRequest, request: Request) -> dict:
    """Ask the agent. Pass the returned session_id back for follow-up questions."""
    st = request.app.state
    if st.agent is None:
        raise HTTPException(503, "The AI service is not configured (OPENAI_API_KEY missing).")
    if not st.limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many questions in the last minute; please wait a moment.")
    if not st.budget.available():
        raise HTTPException(429, "Today's question budget has been used. The ranking and hub data endpoints "
                                 "still work; please contact the app owner to raise the budget.")
    session_id = body.session_id or uuid.uuid4().hex[:12]
    request_id = uuid.uuid4().hex[:8]
    with st.session_locks[session_id]:                       # one turn at a time per session
        try:
            result = st.agent.run_turn(body.message, session_id)
        except LLMUnavailableError as e:
            log.error("request %s: LLM unavailable: %s", request_id, e)
            raise HTTPException(502, f"The AI service is temporarily unavailable; please try again "
                                     f"(request {request_id}).") from e
    st.budget.add(result.usage.cost_usd)
    log.info("chat request=%s session=%s latency_ms=%s cost=%.4f tools=%s warnings=%s", request_id,
             result.session_id, result.latency_ms, result.usage.cost_usd, [t.tool for t in result.trace],
             len(result.validation_warnings))
    return {**result.to_dict(), "request_id": request_id}


@app.post("/api/chat/reset", dependencies=[Depends(require_access)], tags=["chat"])
def reset(body: ResetRequest, request: Request) -> dict:
    agent = request.app.state.agent
    if agent is not None:
        agent.store.reset(body.session_id)
    return {"reset": body.session_id}


# --------------------------------------------------------------------------- data (no LLM)


def _tool(fn, args):
    conn = get_connection()
    try:
        out = fn(args, conn)
    finally:
        conn.close()
    if isinstance(out, dict) and out.get("error"):
        raise HTTPException(400, out["error"])
    return out


@app.get("/api/scores", dependencies=[Depends(require_access)], tags=["data"])
def scores(measure: str = Query("severe_disruption_days"), hazard: str = Query("all"),
           region: str | None = Query(None), top_n: int | None = Query(None)) -> dict:
    """Leaderboard (same function the agent uses)."""
    try:
        args = s.RankHubsInput(measure=measure, hazard=hazard, region=region, top_n=top_n, day_costs=None)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _tool(rank_hubs, args)


@app.get("/api/hubs", dependencies=[Depends(require_access)], tags=["data"])
def hubs(region: str | None = Query(None)) -> dict:
    try:
        args = s.ListHubsInput(region=region)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _tool(list_hubs, args)


@app.get("/api/hubs/{hub_id}/breakdown", dependencies=[Depends(require_access)], tags=["data"])
def breakdown(hub_id: str) -> dict:
    try:
        args = s.HubBreakdownInput(hub_id=hub_id)
    except ValueError as e:
        raise HTTPException(404, f"Unknown hub '{hub_id}'") from e
    return _tool(get_hub_risk_breakdown, args)


@app.get("/api/methodology", dependencies=[Depends(require_access)], tags=["data"])
def methodology() -> dict:
    return _tool(get_methodology, s.MethodologyInput())


# --------------------------------------------------------------------------- admin + health


@app.post("/api/admin/budget", dependencies=[Depends(require_admin)], tags=["admin"])
def set_budget(body: BudgetRequest, request: Request) -> dict:
    """Change the daily OpenAI spend cap at runtime (e.g. raise it on interview day)."""
    request.app.state.budget.cap_usd = body.daily_budget_usd
    log.info("daily budget changed to $%.2f", body.daily_budget_usd)
    return request.app.state.budget.status()


@app.get("/health", tags=["admin"])
def health(request: Request) -> dict:
    settings = get_settings()
    conn = get_connection()
    try:
        scored = conn.execute("SELECT MAX(computed_at), MAX(scoring_version), COUNT(DISTINCT hub_id) FROM risk_scores "
                              "WHERE computed_at = (SELECT MAX(computed_at) FROM risk_scores)").fetchone()
        sources = dict(conn.execute("SELECT source, MAX(fetched_at) FROM ingest_log WHERE status = 'ok' "
                                    "GROUP BY source").fetchall())
        db_ok = True
    except Exception as e:  # noqa: BLE001
        scored, sources, db_ok = (None, None, 0), {"error": str(e)}, False
    finally:
        conn.close()
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "hubs_scored": scored[2],
            "scores_computed_at": scored[0], "scoring_version": scored[1], "data_fetched_at": sources,
            "llm_model": settings.llm_model, "llm_configured": request.app.state.agent is not None,
            "access_code_required": bool(settings.app_access_code), "budget": request.app.state.budget.status()}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    request_id = uuid.uuid4().hex[:8]
    log.exception("unhandled error request=%s path=%s", request_id, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"Internal error (request {request_id})."})


# Static UI last, so /api and /health take precedence.
WEB_DIR = Path(REPO_ROOT) / "web"
WEB_DIR.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
