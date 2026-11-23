"""Agent tests with a scripted fake LLM: no network, no cost, deterministic."""

import json
import time

import pytest

from app.agent.agent import Agent
from app.agent.answer_schema import final_answer_schema
from app.agent.llm_client import LLMResponse, ToolCall, Usage
from app.agent.sessions import TRIMMED, Session, SessionStore
from app.agent.validator import collect_numbers, traceable, validate
from app.config import get_settings
from tests.test_tools import check_strict


def answer(text="ok", figures=(), hubs=(), qtype="statistic", confirm=False) -> str:
    return json.dumps({
        "question_type": qtype, "answer_markdown": text, "measure_used": "weather_statistic", "hubs": list(hubs),
        "figures": [{"label": l, "value": v, "unit": "u", "source_tool": "t"} for l, v in figures],
        "assumptions": [], "uncertainty": {"level": "low", "reasons": []}, "data_sources": [],
        "awaiting_user_confirmation": confirm, "suggested_followups": []})


# ------------------------------------------------------------------ validator


def test_final_answer_schema_is_strict():
    check_strict(final_answer_schema())


def test_traceable_rounding_and_percent():
    pool = {4.93, 0.164}
    assert traceable(4.9, 1, pool) and traceable(5, 0, pool) and traceable(16.4, 1, pool)
    assert not traceable(5.2, 1, pool)


def test_numbers_collected_from_keys_and_strings():
    pool = collect_numbers({"tropical_storms_within_150km_per_decade": 4.34, "definition": "snowfall >= 1 cm"})
    assert {150.0, 4.34, 1.0} <= pool


def test_fabricated_figure_and_text_number_are_problems():
    tools = [{"percent_of_days": 4.93}]
    v = validate(answer("Denver had snow on 7.5% of days", figures=[("pct", 7.5)]), tools, "q", [])
    assert not v.ok and len(v.problems) == 2


def test_valid_answer_passes_and_years_small_ints_ignored():
    tools = [{"percent_of_days": 4.93, "count": 18}]
    v = validate(answer("In 2025, 18 days (4.9%) - top 3", figures=[("pct", 4.93), ("days", 18)]), tools, "q", [])
    assert v.ok, v.problems


def test_number_from_user_question_is_allowed():
    v = validate(answer("Here are the top 15 hubs"), [], "show the top 15 hubs", [])
    assert v.ok


def test_rank_mismatch_is_a_problem():
    turn = [("rank_hubs", {"ranking": [{"hub_id": "dallas", "rank": 14}]})]
    hub = {"hub_id": "dallas", "rank": 3, "severe_days_per_year": None, "tier": None, "key_drivers": []}
    v = validate(answer(hubs=[hub], qtype="ranking"), [turn[0][1]], "q", turn)
    assert any("rank" in p for p in v.problems)


def test_stand_in_requires_confirmation():
    turn = [("find_nearest_hubs", {"status": "matched", "recommended_hub_id": "orlando"})]
    assert not validate(answer(qtype="stand_in"), [turn[0][1]], "q", turn).ok
    assert validate(answer(qtype="stand_in", confirm=True), [turn[0][1]], "q", turn).ok


def test_invalid_json_is_a_problem():
    assert not validate("not json", [], "q", []).ok


# ------------------------------------------------------------------ sessions


def test_trim_replaces_old_tool_outputs_only():
    s = Session("x")
    for i in range(5):
        s.start_turn()
        s.items += [{"role": "user", "content": f"q{i}"}, {"type": "function_call", "call_id": f"c{i}"},
                    {"type": "function_call_output", "call_id": f"c{i}", "output": "{}"}]
    s.trim(keep_turns=3)
    outs = [i["output"] for i in s.items if i.get("type") == "function_call_output"]
    assert outs[:2] == [TRIMMED, TRIMMED] and outs[2:] == ["{}"] * 3
    assert sum(1 for i in s.items if i.get("type") == "function_call") == 5


def test_sessions_expire():
    store = SessionStore(ttl=0)
    s = store.get_or_create(None)
    s.last_used = time.time() - 1
    assert store.get_or_create(s.id) is not s


# ------------------------------------------------------------------ agent loop with a fake LLM


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def create(self, instructions, items, tools, schema):
        self.calls += 1
        r = self.responses.pop(0)
        return r


def resp(text=None, calls=(), refusal=None, status="completed"):
    items = [{"type": "function_call", "call_id": c.call_id, "name": c.name, "arguments": c.arguments}
             for c in calls] + ([{"type": "message", "content": [{"type": "output_text", "text": text}]}] if text else [])
    return LLMResponse(list(calls), text, refusal, status, None, items, Usage(10, 0, 5, 0, 0.001, 1))


needs_db = pytest.mark.skipif(not get_settings().db_file.exists(), reason="database not built")


@needs_db
def test_tool_call_then_answer():
    fake = FakeLLM([resp(calls=[ToolCall("c1", "list_hubs", '{"region": "West"}')]),
                    resp(text=answer("There are 12 West hubs", figures=[("hubs", 12)]))])
    r = Agent(llm=fake).run_turn("How many West hubs?")
    assert r.answer["answer_markdown"] == "There are 12 West hubs" and not r.validation_warnings
    assert [t.tool for t in r.trace] == ["list_hubs"] and r.steps == 2 and r.usage.calls == 2


@needs_db
def test_failed_validation_retries_once_then_returns_warnings():
    bad = answer("It is 99.9 days", figures=[("x", 99.9)])
    fake = FakeLLM([resp(text=bad), resp(text=bad)])
    r = Agent(llm=fake).run_turn("q")
    assert r.retried and r.validation_warnings and fake.calls == 2


@needs_db
def test_retry_can_fix_the_answer():
    fake = FakeLLM([resp(calls=[ToolCall("c1", "list_hubs", '{"region": "West"}')]),
                    resp(text=answer("There are 13 West hubs", figures=[("hubs", 13.5)])),
                    resp(text=answer("There are 12 West hubs", figures=[("hubs", 12)]))])
    r = Agent(llm=fake).run_turn("How many West hubs?")
    assert r.retried and not r.validation_warnings


def test_refusal_becomes_out_of_scope():
    r = Agent(llm=FakeLLM([resp(refusal="no")])).run_turn("something unsafe")
    assert r.answer["question_type"] == "out_of_scope" and r.stopped_reason == "refusal"


@needs_db
def test_step_limit():
    loop = [resp(calls=[ToolCall(f"c{i}", "list_hubs", '{"region": null}')]) for i in range(3)]
    r = Agent(llm=FakeLLM(loop), max_steps=3).run_turn("q")
    assert r.stopped_reason == "max_steps"


@needs_db
def test_follow_up_uses_same_session():
    fake = FakeLLM([resp(text=answer("first")), resp(text=answer("second"))])
    agent = Agent(llm=fake)
    r1 = agent.run_turn("q1")
    r2 = agent.run_turn("q2", r1.session_id)
    assert r1.session_id == r2.session_id
    users = [i["content"] for i in agent.store.get_or_create(r1.session_id).items if i.get("role") == "user"]
    assert users == ["q1", "q2"]
