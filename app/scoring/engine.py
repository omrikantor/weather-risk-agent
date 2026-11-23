"""Scoring engine: expected disruption days per year (the KPI), per hazard and in total.

For each hub and hazard, a day counts as disrupted when the hazard's rule (params.yaml ->
scoring.disruption_rules) is met. Station-based hazards (winter, flood, convective, heat)
use 1996-2025 daily data; hurricanes use storm days from HURDAT2 (1950-2025). The composite
is the sum of per-hazard expected days, optionally multiplied by a cost per day (default 1).
There are no subjective hazard weights.

Usage:
    python -m app.scoring.engine --print     # compute, store, print the ranking
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.db import get_connection
from app.scoring import metrics as m
from app.scoring.params import get_params

HAZARDS = ["winter", "flood", "convective", "hurricane", "heat"]


# --------------------------------------------------------------------------- pure functions


def composite_days(hazard_days: dict[str, float], day_costs: dict[str, float] | None = None) -> float:
    costs = day_costs or get_params()["scoring"]["default_day_costs"]
    return float(sum((hazard_days.get(h) or 0.0) * costs.get(h, 1) for h in HAZARDS))


def assign_tiers(values: pd.Series) -> pd.Series:
    """Top / middle / bottom third of hubs (ties broken by hub id for determinism)."""
    order = values.reset_index().sort_values([values.name, values.index.name], ascending=[False, True])
    labels = np.empty(len(order), dtype=object)
    for label, idx in zip(("High", "Elevated", "Low"), np.array_split(np.arange(len(order)), 3)):
        labels[idx] = label
    return pd.Series(labels, index=order[values.index.name].to_numpy()).reindex(values.index)


def confidence(st_info: dict | None, infos: dict, rules: dict) -> tuple[str, list[str]]:
    reasons = []
    if st_info is None:
        reasons.append("no qualifying weather station; modeled data only")
    else:
        if st_info["distance_km"] > rules["station_far_km"]:
            reasons.append(f"station is {st_info['distance_km']:.0f} km from the hub")
        if rules["gust_not_measured"] and not st_info["gust_measured"]:
            reasons.append("no measured gusts or weather events at the station (modeled gusts used; ice not observed)")
    if rules["variable_from_open_meteo"]:
        fell_back = [v for v, i in infos.items() if i.source == "open_meteo" and v != "gust_kmh"]
        if fell_back:
            reasons.append(f"modeled (Open-Meteo 2016-2025) data used for: {', '.join(fell_back)}")
    level = ["high", "medium", "low"][min(len(reasons), 2)]
    return level, reasons


# --------------------------------------------------------------------------- per hub


def score_hub(conn: sqlite3.Connection, hub_id: str) -> dict:
    p = get_params()
    sp, ep = p["scoring"], p["event_rates"]
    level = ep["confidence_level"]
    series, infos, wt, st_info = m.load_hub_daily(conn, hub_id)

    def event_row(hazard, radius):
        r = conn.execute("SELECT events, rate_per_year, rate_lo, rate_hi, first_year, last_year, details_json "
                         "FROM hub_event_rates WHERE hub_id=? AND hazard=? AND radius_km=?",
                         (hub_id, hazard, radius)).fetchone()
        return r

    torn = event_row("tornado", ep["default_radius_km"]["convective"])
    tornado_dates = set(json.loads(torn[6]).get("dates", [])) if torn else set()

    flags = m.disruption_flags(series, wt, tornado_dates, sp["thresholds"])
    hazards = {}
    for h, s in flags.items():
        stats = m.summarize_flags(s, level)
        base_var = {"winter": "snow_cm", "flood": "precip_mm", "convective": "gust_kmh", "heat": "tmax_c"}[h]
        info = infos[base_var]
        hazards[h] = {"days_per_year": stats["per_year"], "range": [stats["lo"], stats["hi"]],
                      "events": stats["events"], "years_observed": stats["years"],
                      "rule": sp["disruption_rules"][h], "source": info.source,
                      "window": list(info.window), "coverage": info.coverage, "notes": info.notes}

    hr = event_row("hurricane_days", ep["default_radius_km"]["hurricane"])
    storms = event_row("hurricane", ep["default_radius_km"]["hurricane"])
    hu = event_row("hurricane_strength", ep["default_radius_km"]["hurricane"])
    hazards["hurricane"] = {
        "days_per_year": hr[1], "range": [hr[2], hr[3]], "events": hr[0], "years_observed": hr[5] - hr[4] + 1,
        "rule": sp["disruption_rules"]["hurricane"], "source": "NOAA HURDAT2", "window": [str(hr[4]), str(hr[5])],
        "storms_per_decade": storms[1] * 10, "hurricane_strength_per_decade": hu[1] * 10,
        "p_storm_10y": 1 - np.exp(-storms[1] * 10),
        "notable_storms": json.loads(hu[6])["notable_storms"] or json.loads(storms[6])["notable_storms"],
    }

    # Overlap: share of hazard-days in 1996-2025 that coincide with another hazard on the same date.
    w0, w1 = sp["window"]
    hur_dates = {d for d in json.loads(hr[6]).get("dates", []) if str(w0) <= d[:4] <= str(w1)}
    day_sets = {h: set(s[s == 1].index) for h, s in flags.items()}
    day_sets["hurricane"] = hur_dates
    total = sum(len(v) for v in day_sets.values())
    union = len(set().union(*day_sets.values()))
    overlap_share = round(1 - union / total, 4) if total else 0.0

    desc = {}
    for name, s in m.descriptive_flags(series, wt, sp["descriptive_thresholds"]).items():
        st = m.summarize_flags(s, level)
        desc[name] = {"per_year": st["per_year"], "range": [st["lo"], st["hi"]], "years_observed": st["years"]}
    desc["max_3day_precip_mm"] = {"value": m.max_3day_precip_mean(series["precip_mm"])}
    snow = series["snow_cm"].copy()
    snow.index = pd.to_datetime(snow.index)
    desc["annual_snowfall_cm"] = {"value": float(snow.groupby(snow.index.year).sum(min_count=300).mean())}
    for h in ("tornado", "hail", "wind"):
        r = event_row(h, ep["default_radius_km"]["convective"])
        desc[f"{h}_report_days"] = {"per_year": r[1], "range": [r[2], r[3]],
                                    "radius_km": ep["default_radius_km"]["convective"], "window": [r[4], r[5]]}

    conf, reasons = confidence(st_info, infos, sp["confidence_rules"])
    return {"hub_id": hub_id, "hazards": hazards, "overlap_share": overlap_share, "descriptive": desc,
            "station": st_info, "variables": {k: vars(v) for k, v in infos.items()},
            "confidence": conf, "confidence_reasons": reasons}


# --------------------------------------------------------------------------- all hubs


def score_all(conn: sqlite3.Connection, day_costs: dict | None = None, store: bool = True) -> pd.DataFrame:
    p = get_params()
    hub_ids = [r[0] for r in conn.execute("SELECT id FROM hubs ORDER BY id")]
    results = {h: score_hub(conn, h) for h in hub_ids}
    df = pd.DataFrame({
        h: {**{z: r["hazards"][z]["days_per_year"] or 0.0 for z in HAZARDS},
            "composite": composite_days({z: r["hazards"][z]["days_per_year"] for z in HAZARDS}, day_costs),
            "confidence": r["confidence"]}
        for h, r in results.items()
    }).T
    df.index.name = "hub_id"
    df["composite"] = df["composite"].astype(float)
    df["tier"] = assign_tiers(df["composite"])
    df["rank"] = df["composite"].rank(ascending=False, method="min").astype(int)
    if store:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        version = f"{p['scoring']['scoring_version']}/params-{p['params_version']}"
        conn.executemany(
            "INSERT INTO risk_scores (hub_id, computed_at, scoring_version, composite, winter, hurricane, flood, "
            "convective, heat, tier, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(h, now, version, float(row.composite), float(row.winter), float(row.hurricane), float(row.flood),
              float(row.convective), float(row.heat), row.tier,
              json.dumps({**results[h], "rank": int(row["rank"]), "of": len(df)}, default=float))
             for h, row in df.iterrows()])
        conn.execute("DELETE FROM hub_metrics")
        conn.executemany(
            "INSERT INTO hub_metrics (hub_id, metric, period, value) VALUES (?, ?, ?, ?)",
            [(h, name, f"{p['scoring']['window'][0]}-{p['scoring']['window'][1]}", v.get("per_year", v.get("value")))
             for h, r in results.items() for name, v in r["descriptive"].items()])
        conn.commit()
    return df.sort_values("composite", ascending=False)


def latest_breakdown(conn: sqlite3.Connection, hub_id: str) -> dict | None:
    row = conn.execute("SELECT computed_at, scoring_version, composite, tier, details_json FROM risk_scores "
                       "WHERE hub_id = ? ORDER BY id DESC LIMIT 1", (hub_id,)).fetchone()
    if not row:
        return None
    d = json.loads(row[4])
    total = row[2] or 1.0
    d["composite_days_per_year"] = row[2]
    d["tier"], d["computed_at"], d["scoring_version"] = row[3], row[0], row[1]
    d["contributions"] = sorted(
        [{"hazard": h, "days_per_year": d["hazards"][h]["days_per_year"] or 0.0,
          "share": (d["hazards"][h]["days_per_year"] or 0.0) / total} for h in HAZARDS],
        key=lambda x: -x["days_per_year"])
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="print the ranking")
    args = ap.parse_args()
    conn = get_connection()
    df = score_all(conn)
    if args.print:
        pd.set_option("display.width", 160)
        print(df[["rank", "tier", "composite"] + HAZARDS + ["confidence"]].round(2).to_string())


if __name__ == "__main__":
    main()
