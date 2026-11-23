"""FastAPI backend: the chat endpoint (agent), data endpoints (tools, no model), health and the static UI.

The data endpoints call the same tool functions as the agent, so the leaderboard and chat answers always agree.
Every /api route needs the access code when APP_ACCESS_CODE is set.

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
from app.alerts import monitor
from app.agent.llm_client import LLMUnavailableError
from app.api.guards import DailyBudget, RateLimiter, code_matches
from app.config import REPO_ROOT, get_settings
from app.db import get_connection
from app.tools import schemas as s
from app.tools.hub_tools import (get_hub_risk_breakdown, get_methodology, get_seasonal_outlook, list_hubs,
                                 rank_hubs)
from app.tools.locate import find_nearest_hubs

log = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class ChatRequest(BaseModel):
    """Body of POST /api/chat. Length limits keep one request from sending a huge prompt."""
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)


class ResetRequest(BaseModel):
    """Body of POST /api/chat/reset."""
    session_id: str = Field(max_length=64)


class BudgetRequest(BaseModel):
    """Body of POST /api/admin/budget. The upper limit stops a typo from allowing unlimited spend."""
    daily_budget_usd: float = Field(gt=0, le=50)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start-up: create the shared guards and the agent, and warm the place index so the first request is fast."""
    settings = get_settings()
    app.state.budget = DailyBudget(settings.daily_budget_usd)
    app.state.limiter = RateLimiter(settings.chat_rate_limit_per_min)
    app.state.session_locks = defaultdict(threading.Lock)   # one lock per conversation (see chat below)
    if getattr(app.state, "agent", None) is None:            # tests inject a fake agent
        app.state.agent = Agent() if settings.openai_api_key else None
    if not settings.app_access_code:
        log.warning("APP_ACCESS_CODE is empty: the API is open (OK for local development only)")
    find_nearest_hubs("Tampa, FL")                            # warm place index and hazard profiles
    if settings.alert_check_on_startup:
        # A daemon thread, so start-up is not delayed by the 47 NWS requests and a slow API cannot block it.
        threading.Thread(target=_startup_alert_check, name="startup-alert-check", daemon=True).start()
    yield


def _startup_alert_check() -> None:
    """One live warning check right after start-up (see Settings.alert_check_on_startup). Failures are logged
    only: the scheduled check will try again within 15 minutes."""
    conn = get_connection()
    try:
        log.info("start-up alert check: %s", monitor.run_check(conn, webhook_url=get_settings().alert_webhook_url or None))
    except Exception:  # noqa: BLE001 - never let a background check crash the server
        log.exception("start-up alert check failed")
    finally:
        conn.close()


app = FastAPI(title="Weather Risk Intelligence Agent", version="1.0", lifespan=lifespan,
              description="Ranks US logistics hubs by weather-disruption exposure and answers questions about it.")
origins = [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------- dependencies


def require_access(x_access_code: str | None = Header(default=None)) -> None:
    """Dependency for /api routes: reject requests without the right access code (when one is configured)."""
    expected = get_settings().app_access_code
    if expected and not code_matches(x_access_code, expected):
        raise HTTPException(401, "Missing or wrong access code (X-Access-Code header).")


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Dependency for admin routes: they are disabled unless ADMIN_TOKEN is set, and need that token."""
    expected = get_settings().admin_token
    if not expected:
        raise HTTPException(403, "Admin endpoints are disabled (ADMIN_TOKEN not set).")
    if not code_matches(x_admin_token, expected):
        raise HTTPException(401, "Missing or wrong admin token (X-Admin-Token header).")


def client_ip(request: Request) -> str:
    """The client's IP for rate limiting. Behind Render's proxy the TCP peer is the proxy, so the original
    client address is read from the X-Forwarded-For header (first entry)."""
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


# --------------------------------------------------------------------------- chat


@app.post("/api/chat", dependencies=[Depends(require_access)], tags=["chat"])
def chat(body: ChatRequest, request: Request) -> dict:
    """Ask the agent. Pass the returned session_id back for follow-up questions.

    Checks run cheapest first: configuration, rate limit, then the daily budget, before any model call.
    """
    st = request.app.state
    if st.agent is None:
        raise HTTPException(503, "The AI service is not configured (OPENAI_API_KEY missing).")
    if not st.limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many questions in the last minute; please wait a moment.")
    if not st.budget.available():
        raise HTTPException(429, "Today's question budget has been used. The ranking and hub data endpoints "
                                 "still work; please contact the app owner to raise the budget.")
    session_id = body.session_id or uuid.uuid4().hex[:12]
    # The browser sent a conversation id the server no longer holds (it expired, or the server restarted after
    # sleeping or a redeploy). The turn still runs as a fresh conversation; the flag lets the UI say so, since
    # a follow-up question can no longer rely on earlier context.
    session_expired = bool(body.session_id) and not st.agent.store.exists(body.session_id)
    request_id = uuid.uuid4().hex[:8]
    # One turn at a time per conversation: two requests for the same session would otherwise interleave
    # their messages in the shared history.
    with st.session_locks[session_id]:
        try:
            result = st.agent.run_turn(body.message, session_id)
        except LLMUnavailableError as e:
            log.error("request %s: LLM unavailable: %s", request_id, e)
            raise HTTPException(502, f"The AI service is temporarily unavailable; please try again "
                                     f"(request {request_id}).") from e
    st.budget.add(result.usage.cost_usd)   # count the real cost of this turn against today's cap
    log.info("chat request=%s session=%s latency_ms=%s cost=%.4f tools=%s warnings=%s", request_id,
             result.session_id, result.latency_ms, result.usage.cost_usd, [t.tool for t in result.trace],
             len(result.validation_warnings))
    return {**result.to_dict(), "request_id": request_id, "session_expired": session_expired}


@app.post("/api/chat/reset", dependencies=[Depends(require_access)], tags=["chat"])
def reset(body: ResetRequest, request: Request) -> dict:
    """Forget a conversation (the New chat button)."""
    agent = request.app.state.agent
    if agent is not None:
        agent.store.reset(body.session_id)
    return {"reset": body.session_id}


# --------------------------------------------------------------------------- data (no LLM)


def _tool(fn, args):
    """Run a tool function for a data endpoint; a tool error becomes HTTP 400 with its message."""
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
    """Hub list, optionally for one region."""
    try:
        args = s.ListHubsInput(region=region)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _tool(list_hubs, args)


@app.get("/api/hubs/{hub_id}/breakdown", dependencies=[Depends(require_access)], tags=["data"])
def breakdown(hub_id: str) -> dict:
    """One hub's full explanation (the hub panel in the UI)."""
    try:
        args = s.HubBreakdownInput(hub_id=hub_id)
    except ValueError as e:
        raise HTTPException(404, f"Unknown hub '{hub_id}'") from e
    return _tool(get_hub_risk_breakdown, args)


@app.get("/api/outlook", dependencies=[Depends(require_access)], tags=["data"])
def outlook(month: int | None = Query(None), region: str | None = Query(None),
            top_n: int | None = Query(None)) -> dict:
    """Seasonal outlook for a calendar month (default: current month). Historical frequency, not a forecast."""
    try:
        args = s.SeasonalOutlookInput(month=month, region=region, top_n=top_n)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _tool(get_seasonal_outlook, args)


@app.get("/api/methodology", dependencies=[Depends(require_access)], tags=["data"])
def methodology() -> dict:
    """Definitions, severity shares, sources and limitations (the Methodology window in the UI)."""
    return _tool(get_methodology, s.MethodologyInput())


# --------------------------------------------------------------------------- admin + health


@app.post("/api/admin/budget", dependencies=[Depends(require_admin)], tags=["admin"])
def set_budget(body: BudgetRequest, request: Request) -> dict:
    """Change the daily OpenAI spend cap at runtime (e.g. raise it on interview day)."""
    request.app.state.budget.cap_usd = body.daily_budget_usd
    log.info("daily budget changed to $%.2f", body.daily_budget_usd)
    return request.app.state.budget.status()


@app.post("/api/admin/alerts/check", dependencies=[Depends(require_admin)], tags=["alerts"])
def check_alerts() -> dict:
    """Run one live warning check for every hub (called by the scheduler every 15 minutes).

    Records warnings that started or ended since the last check and posts them to ALERT_WEBHOOK_URL if set.
    Needs the admin token, because each call makes 47 requests to the NWS API and may send a webhook message.
    """
    conn = get_connection()
    try:
        return monitor.run_check(conn, webhook_url=get_settings().alert_webhook_url or None)
    finally:
        conn.close()


@app.get("/api/alerts", dependencies=[Depends(require_access)], tags=["alerts"])
def alerts(limit: int = Query(50, ge=1, le=500)) -> dict:
    """Warnings active at hubs now, recent alerts (issued and ended) and the result of the last check."""
    conn = get_connection()
    try:
        return monitor.recent(conn, limit)
    finally:
        conn.close()


@app.get("/health", tags=["admin"])
def health(request: Request) -> dict:
    """Service status for monitoring and Render's health check. Needs no access code and reveals no secrets."""
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
    """Any unexpected error: log the details on the server, return only a short message and a request id."""
    request_id = uuid.uuid4().hex[:8]
    log.exception("unhandled error request=%s path=%s", request_id, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"Internal error (request {request_id})."})


# Static UI last, so /api and /health take precedence.
WEB_DIR = Path(REPO_ROOT) / "web"
WEB_DIR.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
