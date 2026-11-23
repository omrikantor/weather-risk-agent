"""Unit tests for the scoring functions, on small synthetic data (no database, no network)."""

import numpy as np
import pandas as pd
import pytest

from app.scoring import metrics as m
from app.scoring.engine import HAZARDS, assign_tiers, composite_days, confidence
from app.scoring.params import get_params

TH = get_params()["scoring"]["thresholds"]
DTH = get_params()["scoring"]["descriptive_thresholds"]


def days(start, n):
    return pd.date_range(start, periods=n).strftime("%Y-%m-%d")


def series(values, start="2020-01-01"):
    return pd.Series(values, index=days(start, len(values)), dtype=float)


# ------------------------------------------------------------------ combine_variable


def test_station_preferred_and_gaps_filled_from_open_meteo():
    st = series([1.0, np.nan, 3.0, 4.0])
    om = series([9.0, 2.0, 9.0, 9.0])
    s, info = m.combine_variable(st, om, ("2020-01-01", "2020-01-04"), ("2020-01-01", "2020-01-04"), 0.8)
    assert list(s) == [1.0, 2.0, 3.0, 4.0]
    assert info.source == "station+open_meteo"
    assert info.filled_share == 0.25


def test_low_coverage_falls_back_to_open_meteo_window():
    st = series([1.0] + [np.nan] * 9)
    om = series([np.nan] * 5 + [5.0] * 5)
    s, info = m.combine_variable(st, om, ("2020-01-01", "2020-01-10"), ("2020-01-06", "2020-01-10"), 0.8)
    assert info.source == "open_meteo"
    assert list(s.index) == list(days("2020-01-06", 5))
    assert info.notes


def test_unusable_station_uses_open_meteo():
    st = series([100.0] * 4)
    om = series([1.0] * 4)
    s, info = m.combine_variable(st, om, ("2020-01-01", "2020-01-04"), ("2020-01-01", "2020-01-04"), 0.8,
                                 station_usable=False)
    assert info.source == "open_meteo" and list(s) == [1.0] * 4


# ------------------------------------------------------------------ rates


def test_rate_is_events_per_observed_year():
    r = m.rate_with_interval(10, int(365.25 * 5), 0.9)   # 1826 whole days ~ 5 years
    assert r["per_year"] == pytest.approx(2.0, rel=1e-3)
    assert r["lo"] < 2.0 < r["hi"]


def test_zero_events_has_zero_lower_bound_and_positive_upper_bound():
    r = m.rate_with_interval(0, 3653, 0.9)
    assert r["per_year"] == 0 and r["lo"] == 0 and r["hi"] > 0


def test_missing_days_are_excluded_not_counted_as_zero():
    flags = series([1.0, np.nan, np.nan, 0.0])
    r = m.summarize_flags(flags, 0.9)
    assert r["events"] == 1 and r["years"] == pytest.approx(2 / 365.25, abs=0.01)


# ------------------------------------------------------------------ disruption rules


def make_series(snow, prcp, tmax, gust):
    return {"snow_cm": series(snow), "precip_mm": series(prcp), "tmax_c": series(tmax),
            "tmin_c": series([0.0] * len(snow)), "gust_kmh": series(gust)}


def test_disruption_thresholds_are_inclusive_and_exclusive_at_the_boundary():
    s = make_series(snow=[4.9, 5.0, 0, 0], prcp=[49.9, 50.0, 0, 0], tmax=[37.9, 38.0, 0, 0],
                    gust=[92.9, 93.0, 0, 0])
    f = m.disruption_flags(s, None, set(), TH)
    for h in ("winter", "flood", "heat", "convective"):
        assert list(f[h])[:2] == [0.0, 1.0], h


def test_ice_flag_makes_a_winter_day_without_snow():
    s = make_series(snow=[0, 0], prcp=[0, 0], tmax=[0, 0], gust=[0, 0])
    wt = pd.DataFrame({c: [0, 0] for c in m.WT_FLAGS}, index=days("2020-01-01", 2))
    wt.loc[wt.index[1], "wt_glaze"] = 1
    assert list(m.disruption_flags(s, wt, set(), TH)["winter"]) == [0.0, 1.0]


def test_tornado_date_makes_a_convective_day():
    s = make_series(snow=[0, 0], prcp=[0, 0], tmax=[0, 0], gust=[10, 10])
    f = m.disruption_flags(s, None, {"2020-01-02"}, TH)
    assert list(f["convective"]) == [0.0, 1.0]


def test_missing_deciding_variable_gives_missing_flag():
    s = make_series(snow=[np.nan], prcp=[np.nan], tmax=[np.nan], gust=[np.nan])
    f = m.disruption_flags(s, None, set(), TH)
    assert all(np.isnan(f[h].iloc[0]) for h in f)


def test_descriptive_snow_day_threshold():
    s = make_series(snow=[0.05, 0.1, 0.99, 1.0], prcp=[0] * 4, tmax=[0] * 4, gust=[0] * 4)
    d = m.descriptive_flags(s, None, DTH)
    assert list(d["trace_snow_day"]) == [0, 1, 1, 1]
    assert list(d["snow_day"]) == [0, 0, 0, 1]


def test_max_3day_precip_mean():
    idx = pd.date_range("2020-01-01", "2020-12-31").strftime("%Y-%m-%d")
    p = pd.Series(0.0, index=idx)
    p.iloc[10:13] = [10, 20, 30]
    assert m.max_3day_precip_mean(p) == pytest.approx(60.0)


# ------------------------------------------------------------------ warnings and severity


PHEN = {"winter": ["WS"], "heat": ["EH"], "flood": [], "convective": [], "hurricane": []}


def test_warning_spanning_midnight_covers_both_local_dates():
    w = pd.DataFrame({"phenomena": ["WS"], "issue": ["2021-02-14T03:00:00Z"], "expire": ["2021-02-15T06:00:00Z"]})
    # 03:00Z = 21:00 previous day in Chicago (UTC-6)
    d = m.warning_days(w, "America/Chicago", PHEN, (2006, 2025))
    assert d["winter"] == {"2021-02-13", "2021-02-14", "2021-02-15"}


def test_overlapping_warnings_count_a_day_once_and_unmapped_codes_ignored():
    w = pd.DataFrame({"phenomena": ["EH", "EH", "SV"],
                      "issue": ["2020-07-01T15:00:00Z", "2020-07-01T18:00:00Z", "2020-07-02T18:00:00Z"],
                      "expire": ["2020-07-01T23:00:00Z", "2020-07-01T23:30:00Z", "2020-07-02T19:00:00Z"]})
    d = m.warning_days(w, "UTC", PHEN, (2006, 2025))
    assert d["heat"] == {"2020-07-01"} and d["winter"] == set()


def test_mixed_iso_formats_are_parsed():
    w = pd.DataFrame({"phenomena": ["WS", "WS"], "issue": ["2016-02-25T03:58Z", "2016-03-01T12:00:00Z"],
                      "expire": ["2016-02-25T09:00Z", "2016-03-01T13:00:00Z"]})
    assert m.warning_days(w, "UTC", PHEN, (2006, 2025))["winter"] == {"2016-02-25", "2016-03-01"}


def test_warning_days_outside_window_excluded():
    w = pd.DataFrame({"phenomena": ["WS"], "issue": ["2005-12-31T12:00:00Z"], "expire": ["2006-01-01T12:00:00Z"]})
    assert m.warning_days(w, "UTC", PHEN, (2006, 2025))["winter"] == {"2006-01-01"}


def test_dates_in_incidents():
    inc = pd.DataFrame({"begin_date": ["2020-01-05"], "end_date": ["2020-01-07"]})
    assert m.dates_in_incidents({"2020-01-04", "2020-01-05", "2020-01-07", "2020-01-08"}, inc) == 2


def test_share_with_interval():
    s = m.share_with_interval(10, 100, 0.9)
    assert s["share"] == 0.1 and s["lo"] < 0.1 < s["hi"]
    assert m.share_with_interval(0, 50, 0.9)["lo"] == 0.0
    assert m.share_with_interval(0, 0, 0.9)["share"] == 0.0


# ------------------------------------------------------------------ composite, tiers, confidence


def test_composite_is_sum_of_hazard_days_with_default_costs():
    days_ = {"winter": 5.0, "flood": 1.0, "convective": 2.0, "hurricane": 0.5, "heat": None}
    assert composite_days(days_) == pytest.approx(8.5)


def test_composite_with_client_costs():
    days_ = {h: 1.0 for h in HAZARDS}
    assert composite_days(days_, {"winter": 1, "flood": 1, "convective": 1, "hurricane": 3, "heat": 1}) == 7.0


def test_tiers_split_into_thirds_deterministically():
    v = pd.Series([9, 8, 7, 6, 5, 4, 3], index=pd.Index(list("abcdefg"), name="hub_id"), name="composite")
    t = assign_tiers(v)
    assert list(t) == ["High", "High", "High", "Elevated", "Elevated", "Low", "Low"]
    assert list(assign_tiers(v)) == list(t)


def test_tier_ties_broken_by_hub_id():
    v = pd.Series([1, 1, 1], index=pd.Index(["c", "a", "b"], name="hub_id"), name="composite")
    assert dict(assign_tiers(v)) == {"a": "High", "b": "Elevated", "c": "Low"}


def test_confidence_levels():
    rules = get_params()["scoring"]["confidence_rules"]
    good = {"distance_km": 5, "gust_measured": True}
    info = {"precip_mm": m.VariableInfo("station", ("a", "b"), 1.0, 0.0)}
    assert confidence(good, info, rules)[0] == "high"
    far_no_gust = {"distance_km": 40, "gust_measured": False}
    assert confidence(far_no_gust, info, rules)[0] == "low"
