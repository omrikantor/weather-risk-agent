"""API tests with FastAPI's TestClient and a scripted fake LLM (no OpenAI calls)."""

import pytest
from fastapi.testclient import TestClient

from app.agent.agent import Agent
from app.agent.llm_client import LLMUnavailableError
from app.api.guards import DailyBudget, RateLimiter
from app.config import get_settings
from tests.test_agent import FakeLLM, answer, resp

pytestmark = pytest.mark.skipif(not get_settings().db_file.exists(), reason="database not built")
CODE = {"X-Access-Code": "test-code"}


@pytest.fixture
def make_client(monkeypatch):
    def _make(responses=(), agent=None, **env):
        monkeypatch.setenv("APP_ACCESS_CODE", "test-code")
        monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        get_settings.cache_clear()
        from app.api.main import app
        app.state.agent = agent or Agent(llm=FakeLLM(list(responses) or [resp(text=answer("ok"))] * 20))
        return TestClient(app)
    yield _make
    get_settings.cache_clear()


def test_access_code_required(make_client):
    with make_client() as c:
        assert c.get("/api/hubs").status_code == 401
        assert c.get("/api/hubs", headers={"X-Access-Code": "wrong"}).status_code == 401
        assert c.get("/api/hubs", headers=CODE).status_code == 200
        assert c.get("/health").status_code == 200          # open
        assert c.get("/").status_code == 200                # static page open


def test_chat_round_trip_and_session(make_client):
    with make_client([resp(text=answer("first")), resp(text=answer("second"))]) as c:
        r1 = c.post("/api/chat", json={"message": "q1"}, headers=CODE).json()
        assert r1["answer"]["answer_markdown"] == "first" and r1["session_id"]
        r2 = c.post("/api/chat", json={"message": "q2", "session_id": r1["session_id"]}, headers=CODE).json()
        assert r2["session_id"] == r1["session_id"] and r2["answer"]["answer_markdown"] == "second"
        assert c.post("/api/chat/reset", json={"session_id": r1["session_id"]}, headers=CODE).status_code == 200


def test_chat_input_validation(make_client):
    with make_client() as c:
        assert c.post("/api/chat", json={"message": ""}, headers=CODE).status_code == 422
        assert c.post("/api/chat", json={"message": "x" * 2001}, headers=CODE).status_code == 422


def test_rate_limit(make_client):
    with make_client(CHAT_RATE_LIMIT_PER_MIN=2) as c:
        codes = [c.post("/api/chat", json={"message": "q"}, headers=CODE).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


def test_daily_budget_blocks_chat_but_not_data(make_client):
    with make_client(DAILY_BUDGET_USD=0.0015) as c:        # fake responses cost $0.001 each
        assert c.post("/api/chat", json={"message": "q"}, headers=CODE).status_code == 200
        assert c.post("/api/chat", json={"message": "q"}, headers=CODE).status_code == 200
        r = c.post("/api/chat", json={"message": "q"}, headers=CODE)
        assert r.status_code == 429 and "budget" in r.json()["detail"]
        assert c.get("/api/scores", headers=CODE).status_code == 200
        assert c.get("/health").json()["budget"]["remaining_usd"] == 0


def test_admin_can_raise_budget(make_client):
    with make_client(DAILY_BUDGET_USD=1) as c:
        assert c.post("/api/admin/budget", json={"daily_budget_usd": 5}).status_code == 401
        r = c.post("/api/admin/budget", json={"daily_budget_usd": 5}, headers={"X-Admin-Token": "admin-token"})
        assert r.status_code == 200 and r.json()["cap_usd"] == 5


def test_llm_outage_returns_502(make_client):
    class Down:
        store = None

        def run_turn(self, *a, **k):
            raise LLMUnavailableError("down")
    with make_client(agent=Down()) as c:
        r = c.post("/api/chat", json={"message": "q"}, headers=CODE)
        assert r.status_code == 502 and "temporarily unavailable" in r.json()["detail"]


def test_data_endpoints(make_client):
    with make_client() as c:
        s = c.get("/api/scores", params={"hazard": "winter", "region": "Midwest", "top_n": 3}, headers=CODE).json()
        assert len(s["ranking"]) == 3 and all(r["region"] == "Midwest" for r in s["ranking"])
        assert c.get("/api/scores", params={"region": "Mars"}, headers=CODE).status_code == 422
        assert c.get("/api/hubs/dallas/breakdown", headers=CODE).json()["hub_id"] == "dallas"
        assert c.get("/api/hubs/atlantis/breakdown", headers=CODE).status_code == 404
        assert "severity_shares" in c.get("/api/methodology", headers=CODE).json()
        h = c.get("/health").json()
        assert h["status"] == "ok" and h["hubs_scored"] == 47 and "OPENAI" not in str(h)


def test_ui_and_assets_are_served(make_client):
    with make_client() as c:
        page = c.get("/")
        assert page.status_code == 200 and "Weather Risk Agent" in page.text
        for asset in ("/app.js", "/styles.css", "/vendor/marked.min.js", "/vendor/purify.min.js",
                      "/fonts/inter-latin-wght-normal.woff2"):
            assert c.get(asset).status_code == 200, asset


def test_outlook_endpoint(make_client):
    with make_client() as c:
        o = c.get("/api/outlook", params={"month": 9, "top_n": 3}, headers=CODE).json()
        assert o["month_name"] == "September" and len(o["hubs"]) == 3
        assert all(d["title"] for h in o["hubs"] for d in h["past_fema_disasters_this_month"])
        assert c.get("/api/outlook", params={"month": 0}, headers=CODE).status_code == 422


def test_guards_units():
    rl = RateLimiter(2)
    assert [rl.allow("ip", 0), rl.allow("ip", 1), rl.allow("ip", 2), rl.allow("ip", 61)] == [True, True, False, True]
    b = DailyBudget(1.0)
    b.add(0.6)
    assert b.available()
    b.add(0.5)
    assert not b.available() and b.status()["remaining_usd"] == 0
