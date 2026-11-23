"""Seasonal outlook: pure monthly statistics + the tool on the built database."""

import pytest

from app.config import get_settings
from app.scoring.seasonal import monthly_rows
from app.tools.registry import execute

COLS = ["hub", "hazard", "month", "years", "k", "p", "lo", "hi", "mean_days", "severe", "longrecord"]


def rows_by(rows):
    return {(r[1], r[2]): dict(zip(COLS, r)) for r in rows}


def test_monthly_rows_counts_years_and_days():
    dates = {"winter": {"2010-01-05", "2010-01-06", "2011-01-20"}, "hurricane": {"2017-09-10"}}
    r = rows_by(monthly_rows("x", dates, {"winter": 0.5, "hurricane": 0.4}, ["1960-09-01", "2017-09-10"],
                             (2006, 2025), (1950, 2025), 0.9))
    jan = r[("winter", 1)]
    assert jan["k"] == 2 and jan["years"] == 20 and jan["p"] == 0.1
    assert jan["mean_days"] == pytest.approx(3 / 20) and jan["severe"] == pytest.approx(3 / 20 * 0.5)
    assert jan["lo"] < 0.1 < jan["hi"] and jan["longrecord"] is None
    sep = r[("hurricane", 9)]
    assert sep["k"] == 1 and sep["longrecord"] == pytest.approx(2 / 76)
    assert r[("winter", 7)]["k"] == 0 and r[("winter", 7)]["severe"] == 0
    assert len(r) == 5 * 12


needs_db = pytest.mark.skipif(not get_settings().db_file.exists(), reason="database not built")


@needs_db
def test_outlook_tool():
    out = execute("get_seasonal_outlook", {"month": 9, "region": None, "top_n": 5})
    assert out["month_name"] == "September" and len(out["hubs"]) == 5
    vals = [h["expected_severe_days_this_month"] for h in out["hubs"]]
    assert vals == sorted(vals, reverse=True)
    h = out["hubs"][0]
    assert 0 <= h["p_at_least_one_warning_day"] <= 1 and h["years_observed"] == 20
    assert "not a forecast" in out["note"]


@needs_db
def test_outlook_defaults_to_current_month_and_filters_region():
    out = execute("get_seasonal_outlook", {"month": None, "region": "Midwest", "top_n": None})
    assert 1 <= out["month"] <= 12 and len(out["hubs"]) == 5
    assert all(h["region"] == "Midwest" for h in out["hubs"])
    assert "error" in execute("get_seasonal_outlook", {"month": 13, "region": None, "top_n": None})


@needs_db
def test_breakdown_has_monthly_pattern():
    out = execute("get_hub_risk_breakdown", {"hub_id": "jacksonville"})
    assert len(out["monthly_severe_days_jan_to_dec"]) == 12 and out["peak_month"]
