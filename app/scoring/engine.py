"""Scoring engine.

KPIs (no chosen hazard weights):
- Routine disruption days/yr per hazard = days covered by an NWS warning for that hazard at the
  hub (county or forecast zone), 2006-2025. NWS criteria are set locally, so a warning means
  conditions expected to be dangerous *for that place*.
- Severity share per hazard = share of warning days (pooled over all hubs) that fall inside a
  FEMA major-disaster incident period of a matching type in the hub's county.
- Severe disruption days/yr = sum over hazards of routine days x severity share
  -> the primary ranking (resilience investment targets severe events).
Evidence kept for explanations: measured threshold days (station data 1996-2025), HURDAT2 storm
counts and named storms, SPC report days, descriptive climate metrics.

Usage:
    python -m app.scoring.engine --print
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

# The five hazard groups used everywhere (tools, UI, prompts). Each NWS warning type maps to exactly one group
# in params.yaml, so a warning day is never counted under two hazards.
HAZARDS = ["winter", "flood", "convective", "hurricane", "heat"]


# --------------------------------------------------------------------------- pure functions


def composite_days(hazard_days: dict[str, float], day_costs: dict[str, float] | None = None) -> float:
    """Total days per year across hazards, each hazard's days multiplied by its cost per day.

    Without analyst input every cost is 1 (default_day_costs in params.yaml), so the total is a plain sum and
    no hazard is weighted by our judgment. An analyst can pass day_costs (for example hurricane 3) to say that a
    hurricane day costs three times as much as an average day for their business.
    """
    costs = day_costs or get_params()["scoring"]["default_day_costs"]
    return float(sum((hazard_days.get(h) or 0.0) * costs.get(h, 1) for h in HAZARDS))


def assign_tiers(values: pd.Series) -> pd.Series:
    """Label hubs High, Elevated or Low as the top, middle and bottom third of the network.

    Thirds are used because the measure has no external scale, so fixed cut-offs would be arbitrary. Hubs with
    equal values are ordered by hub id so the same data always gives the same tiers.
    """
    order = values.reset_index().sort_values([values.name, values.index.name], ascending=[False, True])
    labels = np.empty(len(order), dtype=object)
    # array_split handles network sizes that are not divisible by 3 (47 hubs gives groups of 16, 16 and 15).
    for label, idx in zip(("High", "Elevated", "Low"), np.array_split(np.arange(len(order)), 3)):
        labels[idx] = label
    return pd.Series(labels, index=order[values.index.name].to_numpy()).reindex(values.index)


def confidence(st_info: dict | None, infos: dict, rules: dict) -> tuple[str, list[str]]:
    """Rule-based data confidence for one hub: high, medium or low, plus the reasons.

    Each data weakness found adds one reason: no station, a distant station, a station without gust and
    weather-event records, or model data replacing station data. Zero reasons is high, one is medium and two or
    more is low. Rules come from params.yaml (confidence_rules), so the label is reproducible.
    """
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


# --------------------------------------------------------------------------- per hub (pass 1)


def _event_row(conn, hub_id, hazard, radius):
    """One precomputed storm or report rate (see event_rates.py) for a hub, hazard and search radius."""
    return conn.execute("SELECT events, rate_per_year, rate_lo, rate_hi, first_year, last_year, details_json "
                        "FROM hub_event_rates WHERE hub_id=? AND hazard=? AND radius_km=?",
                        (hub_id, hazard, radius)).fetchone()


def hub_evidence(conn: sqlite3.Connection, hub_id: str) -> dict:
    """Collect everything known about one hub except the pooled severity shares.

    The severity shares need the warning dates of all hubs, so scoring runs in two passes: this function runs
    once per hub (pass 1), then severity_shares() pools the results (pass 2). The warning dates are returned in
    the private key _warning_dates for pass 2 and removed before storing.
    """
    p = get_params()
    sp, ep, wp = p["scoring"], p["event_rates"], p["warnings"]
    level = ep["confidence_level"]

    # Main measure: days with an NWS warning at the hub.
    # A hub is linked to its NWS forecast zone and its county (hub_ugc). Zone and county products are counted
    # as issued. Storm-based warnings (thunderstorm, tornado, flash flood) were stored under the special code
    # POINT:<hub_id> only when their polygon contains the hub, so a warning for the far side of a large county
    # is not counted.
    ugc = conn.execute("SELECT ugc, kind, timezone FROM hub_ugc WHERE hub_id=?", (hub_id,)).fetchall()
    tz = ugc[0][2] if ugc else "UTC"   # local time zone, so a warning day is a local calendar day
    codes = [u[0] for u in ugc] + [f"POINT:{hub_id}"]
    warns = pd.read_sql(f"SELECT phenomena, issue, expire FROM nws_warnings WHERE ugc IN "
                        f"({','.join('?' * len(codes))})", conn, params=codes)
    wdays = m.warning_days(warns, tz, wp["phenomena"], (wp["first_year"], wp["last_year"]))
    n_days = len(pd.date_range(f"{wp['first_year']}-01-01", f"{wp['last_year']}-12-31"))

    # Supporting evidence: days above fixed weather thresholds, from station data with model data as backup.
    # These do not affect the ranking; they help explain it (for example "about 9 heavy snow days a year").
    series, infos, wt, st_info = m.load_hub_daily(conn, hub_id)
    torn = _event_row(conn, hub_id, "tornado", ep["default_radius_km"]["convective"])
    tornado_dates = set(json.loads(torn[6]).get("dates", [])) if torn else set()
    flags = m.disruption_flags(series, wt, tornado_dates, sp["thresholds"])

    hazards = {}
    for h in HAZARDS:
        # Warning days per year with an exact Poisson 90% range (rate of events over 20 years of days).
        r = m.rate_with_interval(len(wdays[h]), n_days, level)
        hazards[h] = {"warning_days_per_year": r["per_year"], "warning_days_range": [r["lo"], r["hi"]],
                      "warning_days_total": r["events"], "warning_window": [wp["first_year"], wp["last_year"]],
                      "warning_types": wp["phenomena"][h]}
        if h in flags:
            t = m.summarize_flags(flags[h], level)
            var = {"winter": "snow_cm", "flood": "precip_mm", "convective": "gust_kmh", "heat": "tmax_c"}[h]
            hazards[h]["threshold_days"] = {"per_year": t["per_year"], "range": [t["lo"], t["hi"]],
                                            "rule": sp["disruption_rules"][h], "source": infos[var].source,
                                            "window": list(infos[var].window)}

    # Hurricane track record within the search radius (HURDAT2, 1950 to 2025): storms per decade, hurricane
    # strength storms, chance of at least one storm in 10 years (Poisson: 1 - exp(-rate x 10)) and notable storms.
    hr = _event_row(conn, hub_id, "hurricane_days", ep["default_radius_km"]["hurricane"])
    storms = _event_row(conn, hub_id, "hurricane", ep["default_radius_km"]["hurricane"])
    hu = _event_row(conn, hub_id, "hurricane_strength", ep["default_radius_km"]["hurricane"])
    hazards["hurricane"]["storm_record"] = {
        "radius_km": ep["default_radius_km"]["hurricane"], "window": [storms[4], storms[5]],
        "storm_days_per_year": hr[1], "storms_per_decade": storms[1] * 10,
        "storms_per_decade_range": [storms[2] * 10, storms[3] * 10],
        "hurricane_strength_per_decade": hu[1] * 10, "p_storm_within_10y": float(1 - np.exp(-storms[1] * 10)),
        "notable_storms": json.loads(hu[6])["notable_storms"] or json.loads(storms[6])["notable_storms"]}

    # Descriptive climate figures used in explanations (snow days, extreme cold days, annual snowfall, storm
    # report days and so on). They are also stored in hub_metrics.
    desc = {}
    for name, s in m.descriptive_flags(series, wt, sp["descriptive_thresholds"]).items():
        st = m.summarize_flags(s, level)
        desc[name] = {"per_year": st["per_year"], "range": [st["lo"], st["hi"]], "years_observed": st["years"]}
    desc["max_3day_precip_mm"] = {"value": m.max_3day_precip_mean(series["precip_mm"])}
    snow = series["snow_cm"].copy()
    snow.index = pd.to_datetime(snow.index)
    # Only years with at least 300 days of data count toward the average, so a year with missing winter
    # months does not pull the average down.
    desc["annual_snowfall_cm"] = {"value": float(snow.groupby(snow.index.year).sum(min_count=300).mean())}
    for h in ("tornado", "hail", "wind"):
        r = _event_row(conn, hub_id, h, ep["default_radius_km"]["convective"])
        desc[f"{h}_report_days"] = {"per_year": r[1], "range": [r[2], r[3]],
                                    "radius_km": ep["default_radius_km"]["convective"], "window": [r[4], r[5]]}

    conf, reasons = confidence(st_info, infos, sp["confidence_rules"])
    return {"hub_id": hub_id, "hazards": hazards, "descriptive": desc, "station": st_info,
            "variables": {k: vars(v) for k, v in infos.items()}, "nws_ugc": [u[0] for u in ugc],
            "confidence": conf, "confidence_reasons": reasons, "_warning_dates": wdays}


# --------------------------------------------------------------------------- severity shares (pooled)


def declarations_for_hazard(decl: pd.DataFrame, hazard: str) -> pd.DataFrame:
    """Declarations matching a hazard by FEMA incident type OR by title (FEMA often files winter
    disasters under 'Severe Storm' / 'Flood'); patterns in params.yaml -> declarations."""
    dp = get_params()["declarations"]
    by_type = decl["incident_type"].isin(dp["incident_types"][hazard])
    pattern = dp.get("title_patterns", {}).get(hazard)
    by_title = decl["title"].fillna("").str.contains(pattern, case=False, regex=True) if pattern else False
    return decl[by_type | by_title]


def severity_shares(conn: sqlite3.Connection, evidence: dict[str, dict]) -> dict[str, dict]:
    """For each hazard, the share of warning days that fell inside a matching FEMA major disaster (pass 2).

    k counts warning days inside the incident period of a matching declaration in the hub's own county, and n
    counts all warning days, both summed over every hub. Pooling across hubs is needed because one hub alone has
    too few disasters for a stable share. The result includes an exact binomial 90% range.
    """
    p = get_params()
    decl = pd.read_sql("SELECT county_fips, incident_type, title, begin_date, end_date FROM fema_declarations", conn)
    county = dict(conn.execute("SELECT id, county_fips FROM hubs").fetchall())
    k, n = {h: 0 for h in HAZARDS}, {h: 0 for h in HAZARDS}
    for hub_id, ev in evidence.items():
        dc = decl[decl["county_fips"] == county[hub_id]]
        for h in HAZARDS:
            dates = ev["_warning_dates"][h]
            inc = declarations_for_hazard(dc, h)
            k[h] += m.dates_in_incidents(dates, inc)
            n[h] += len(dates)
    return {h: m.share_with_interval(k[h], n[h], p["event_rates"]["confidence_level"]) for h in HAZARDS}


# --------------------------------------------------------------------------- all hubs


def score_all(conn: sqlite3.Connection, day_costs: dict | None = None, store: bool = True) -> pd.DataFrame:
    """Score every hub and, when store is True, save a new scoring run to risk_scores and hub_metrics.

    Returns one row per hub with disaster-level days per hazard, the total ("composite"), the warning-day total
    ("routine_total"), ranks and tiers, sorted from most to least exposed. The same database and parameters
    always produce the same output.
    """
    p = get_params()
    hub_ids = [r[0] for r in conn.execute("SELECT id FROM hubs ORDER BY id")]
    evidence = {h: hub_evidence(conn, h) for h in hub_ids}   # pass 1: per hub
    shares = severity_shares(conn, evidence)                  # pass 2: pooled over all hubs

    rows = {}
    for h, ev in evidence.items():
        ev.pop("_warning_dates")   # only needed for the severity shares; too large to store
        severe = {}
        for z in HAZARDS:
            hz = ev["hazards"][z]
            hz["severity_share"] = shares[z]
            # Disaster-level days per year = warning days per year x the hazard's severity share.
            hz["severe_days_per_year"] = (hz["warning_days_per_year"] or 0.0) * shares[z]["share"]
            severe[z] = hz["severe_days_per_year"]
        routine = {z: ev["hazards"][z]["warning_days_per_year"] or 0.0 for z in HAZARDS}
        rows[h] = {**severe, "composite": composite_days(severe, day_costs),
                   "routine_total": composite_days(routine, day_costs), "confidence": ev["confidence"]}
    df = pd.DataFrame(rows).T
    df.index.name = "hub_id"
    for c in HAZARDS + ["composite", "routine_total"]:
        df[c] = df[c].astype(float)
    df["tier"] = assign_tiers(df["composite"])
    # method="min" gives equal values the same rank (two hubs tied for 3rd are both 3, the next is 5).
    df["rank"] = df["composite"].rank(ascending=False, method="min").astype(int)
    df["routine_rank"] = df["routine_total"].rank(ascending=False, method="min").astype(int)

    if store:
        # Each run is appended with its time and method version; readers always use the latest run per hub.
        # The full evidence for each hub is kept as JSON so tools can explain a score without recomputing it.
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        version = f"{p['scoring']['kpi_version']}/params-{p['params_version']}"
        conn.executemany(
            "INSERT INTO risk_scores (hub_id, computed_at, scoring_version, composite, winter, hurricane, flood, "
            "convective, heat, tier, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(h, now, version, float(r.composite), float(r.winter), float(r.hurricane), float(r.flood),
              float(r.convective), float(r.heat), r.tier,
              json.dumps({**evidence[h], "rank": int(r["rank"]), "of": len(df),
                          "routine_total": float(r.routine_total), "routine_rank": int(r.routine_rank)},
                         default=float))
             for h, r in df.iterrows()])
        conn.execute("DELETE FROM hub_metrics")
        conn.executemany(
            "INSERT INTO hub_metrics (hub_id, metric, period, value) VALUES (?, ?, ?, ?)",
            [(h, name, f"{p['scoring']['window'][0]}-{p['scoring']['window'][1]}", v.get("per_year", v.get("value")))
             for h, ev in evidence.items() for name, v in ev["descriptive"].items()])
        conn.commit()
    return df.sort_values("composite", ascending=False)


def latest_breakdown(conn: sqlite3.Connection, hub_id: str) -> dict | None:
    """The latest stored score of one hub with its evidence, plus each hazard's contribution to the total.

    Returns None when the hub has never been scored.
    """
    row = conn.execute("SELECT computed_at, scoring_version, composite, tier, details_json FROM risk_scores "
                       "WHERE hub_id = ? ORDER BY id DESC LIMIT 1", (hub_id,)).fetchone()
    if not row:
        return None
    d = json.loads(row[4])
    total = row[2] or 1.0   # avoids division by zero for a hub with no disaster-level days at all
    d["severe_days_per_year"], d["tier"], d["computed_at"], d["scoring_version"] = row[2], row[3], row[0], row[1]
    d["contributions"] = sorted(
        [{"hazard": h, "severe_days_per_year": d["hazards"][h]["severe_days_per_year"],
          "share_of_total": d["hazards"][h]["severe_days_per_year"] / total if total else 0.0,
          "warning_days_per_year": d["hazards"][h]["warning_days_per_year"],
          "severity_share": d["hazards"][h]["severity_share"]["share"]} for h in HAZARDS],
        key=lambda x: -x["severe_days_per_year"])
    return d


def main() -> None:
    """Command line entry point: score all hubs and optionally print the ranking."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="print the ranking")
    args = ap.parse_args()
    conn = get_connection()
    df = score_all(conn)
    if args.print:
        pd.set_option("display.width", 170)
        print(df[["rank", "tier", "composite"] + HAZARDS + ["routine_total", "routine_rank", "confidence"]]
              .round(2).to_string())
        r = json.loads(conn.execute("SELECT details_json FROM risk_scores ORDER BY id DESC LIMIT 1").fetchone()[0])
        print("\nSeverity shares:", {h: round(r["hazards"][h]["severity_share"]["share"], 3) for h in HAZARDS})


if __name__ == "__main__":
    main()
