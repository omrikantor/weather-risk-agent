"""Offline tests for the eval checks (no OpenAI calls)."""

import pytest
import yaml

from app.config import get_settings
from evals.checks import args_match, check_turn, get_path, oracle, value_in_text
from evals.run_evals import CASES_FILE

needs_db = pytest.mark.skipif(not get_settings().db_file.exists(), reason="database not built")


def test_get_path():
    obj = {"hubs": [{"hazards": {"flood": {"x": 1.5}}}], "ranking": [{"hub_id": "a", "v": 1}, {"hub_id": "b", "v": 2}]}
    assert get_path(obj, "hubs[0].hazards.flood.x") == 1.5
    assert get_path(obj, "ranking[hub_id=b].v") == 2


def test_value_in_text_rounding_and_percent():
    assert value_in_text(4.93, "snow on 4.9% of days")
    assert value_in_text(0.164, "a 16.4% share")
    assert not value_in_text(4.93, "snow on 5.4% of days")


def test_args_match():
    assert args_match({"hazard": "winter", "region": "Midwest"}, {"hazard": "winter", "region": "Midwest", "top_n": 3})
    assert args_match({"location": "~New York"}, {"location": "New York, NY"})
    assert not args_match({"hub_id": "dallas"}, {"hub_id": "houston"})


def fake_result(text, tools=(), qtype="ranking", confirm=False, warnings=()):
    return {"answer": {"answer_markdown": text, "question_type": qtype, "awaiting_user_confirmation": confirm},
            "trace": [{"tool": n, "arguments": a} for n, a in tools], "validation_warnings": list(warnings),
            "stopped_reason": "answered"}


@needs_db
def test_order_and_tool_checks_pass_on_a_correct_answer():
    turn = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"][0]["turns"][0]
    names = [h["name"] for h in oracle(turn["order"])["ranking"][:3]]
    text = f"1. {names[0]} ... 2. {names[1]} ... 3. {names[2]}"
    res = check_turn(turn, fake_result(text, [("rank_hubs", {"hazard": "winter", "region": "Midwest"})]))
    assert all(ok for _, ok, _ in res), res


@needs_db
def test_wrong_order_and_dashes_fail():
    turn = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"][0]["turns"][0]
    names = [h["name"] for h in oracle(turn["order"])["ranking"][:3]]
    text = f"{names[2]} — then {names[1]} and {names[0]}"
    failed = {c for c, ok, _ in check_turn(turn, fake_result(text)) if not ok}
    assert {"order", "no_dashes", "tool:rank_hubs"} <= failed


def test_all_cases_are_well_formed():
    cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 15 and len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        assert c["turns"] and all("message" in t for t in c["turns"])
