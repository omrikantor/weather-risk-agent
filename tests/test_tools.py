"""Tool tests against the built database (skipped if it doesn't exist)."""

import json

import pytest

from app.config import get_settings
from app.tools.registry import TOOLS, execute, openai_tools

pytestmark = pytest.mark.skipif(not get_settings().db_file.exists(), reason="database not built")


def check_strict(node, path="$"):
    """Every object: all properties required, additionalProperties false; no $ref."""
    if isinstance(node, dict):
        assert "$ref" not in node, path
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, path
            assert set(node.get("required", [])) == set(node.get("properties", {})), path
        for k, v in node.items():
            check_strict(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            check_strict(v, f"{path}[{i}]")


def test_all_tool_schemas_are_strict():
    specs = openai_tools()
    assert {s["name"] for s in specs} == set(TOOLS)
    for spec in specs:
        assert spec["strict"] is True
        check_strict(spec["parameters"])


def run(name, **args):
    out = execute(name, args)
    json.dumps(out)                     # must be JSON-serializable
    assert "error" not in out, out
    return out


def test_list_hubs_region_filter():
    out = run("list_hubs", region="Midwest")
    assert out["count"] == 13 and all(h["region"] == "Midwest" for h in out["hubs"])


def test_rank_hubs_region_and_hazard():
    out = run("rank_hubs", measure="severe_disruption_days", hazard="winter", region="Midwest", top_n=3,
              day_costs=None)
    vals = [r["value_days_per_year"] for r in out["ranking"]]
    assert len(vals) == 3 and vals == sorted(vals, reverse=True)
    assert all(r["region"] == "Midwest" for r in out["ranking"])


def test_rank_hubs_day_costs_change_the_total():
    base = run("rank_hubs", measure="severe_disruption_days", hazard="all", region=None, top_n=None, day_costs=None)
    costs = {"winter": None, "flood": None, "convective": None, "hurricane": 3, "heat": None}
    weighted = run("rank_hubs", measure="severe_disruption_days", hazard="all", region=None, top_n=None,
                   day_costs=costs)
    b = {r["hub_id"]: r["value_days_per_year"] for r in base["ranking"]}
    w = {r["hub_id"]: r["value_days_per_year"] for r in weighted["ranking"]}
    assert w["miami"] > b["miami"] and w["denver"] == b["denver"]


def test_rank_ties_share_a_rank_and_zero_is_flagged():
    out = run("rank_hubs", measure="severe_disruption_days", hazard="hurricane", region=None, top_n=None,
              day_costs=None)
    zeros = [r for r in out["ranking"] if r["value_days_per_year"] == 0]
    assert len(zeros) > 1 and len({r["rank"] for r in zeros}) == 1
    assert all(r["no_exposure_in_data"] for r in zeros)
    assert "overall_tier" in out["ranking"][0] and "tier" not in out["ranking"][0]


def test_default_ranking_uses_tier_label():
    out = run("rank_hubs", measure="severe_disruption_days", hazard="all", region=None, top_n=3, day_costs=None)
    assert "tier" in out["ranking"][0]


def test_ranges_contain_values():
    out = run("rank_hubs", measure="severe_disruption_days", hazard="all", region=None, top_n=None, day_costs=None)
    for r in out["ranking"]:
        lo, hi = r["range_90pct"]
        assert lo <= r["value_days_per_year"] <= hi


def test_compare_reports_no_exposure_and_ties():
    out = run("compare_hubs", hub_ids=["denver", "chicago"], hazards=["hurricane"])
    assert out["by_hazard"]["hurricane"]["most_exposed_by_severe_days"].startswith("none")


def test_compare_hubs():
    out = run("compare_hubs", hub_ids=["miami", "houston"], hazards=["hurricane", "flood"])
    assert [h["hub_id"] for h in out["hubs"]] == ["miami", "houston"]
    assert set(out["by_hazard"]) == {"hurricane", "flood"}
    assert out["hubs"][0]["hazards"]["hurricane"]["evidence"]["notable_storms"]


def test_breakdown_contributions_sum_to_total():
    out = run("get_hub_risk_breakdown", hub_id="dallas")
    total = sum(c["severe_days_per_year"] for c in out["contributions"])
    assert total == pytest.approx(out["severe_days_per_year"], abs=0.02)


def test_weather_stat_snow_days_denver_2025():
    out = run("get_weather_stat", hub_id="denver", metric="snow_day", start_date="2025-01-01", end_date="2025-12-31")
    assert out["days_in_period"] == 365 and 0 <= out["percent_of_days"] <= 100
    assert out["sources"]["station_days"] + out["sources"]["open_meteo_days"] == out["days_with_data"]


def test_weather_stat_warning_days():
    out = run("get_weather_stat", hub_id="buffalo", metric="warning_days_winter", start_date="2022-01-01",
              end_date="2022-12-31")
    assert out["count"] > 0


def test_methodology():
    out = run("get_methodology")
    assert set(out["severity_shares"]) == {"winter", "flood", "convective", "hurricane", "heat"}


def test_find_nearest_hubs_tool():
    assert run("find_nearest_hubs", location="Tampa, FL")["status"] == "matched"


@pytest.mark.parametrize("name,args", [
    ("compare_hubs", {"hub_ids": ["miami"], "hazards": None}),
    ("get_hub_risk_breakdown", {"hub_id": "atlantis"}),
    ("rank_hubs", {"measure": "severe_disruption_days", "hazard": "all", "region": None, "top_n": 0,
                   "day_costs": None}),
    ("get_weather_stat", {"hub_id": "denver", "metric": "snow_day", "start_date": "2025-12-31",
                          "end_date": "2025-01-01"}),
    ("no_such_tool", {}),
    ("rank_hubs", {"measure": "severe_disruption_days", "hazard": "hurricane", "region": None, "top_n": None,
                   "day_costs": {"winter": None, "flood": None, "convective": None, "hurricane": 3, "heat": None}}),
])
def test_invalid_arguments_return_errors(name, args):
    out = execute(name, args)
    assert "error" in out
