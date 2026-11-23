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


# --------------------------------------------------------------------------- per hub (pass 1)


def _event_row(conn, hub_id, hazard, radius):
    return conn.execute("SELECT events, rate_per_year, rate_lo, rate_hi, first_year, last_year, details_json "
                        "FROM hub_event_rates WHERE hub_id=? AND hazard=? AND radius_km=?",
                        (hub_id, hazard, radius)).fetchone()


def hub_evidence(conn: sqlite3.Connection, hub_id: str) -> dict:
    """Everything for one hub except the pooled severity shares."""
    p = get_params()
    sp, ep, wp = p["scoring"], p["event_rates"], p["warnings"]
    level = ep["confidence_level"]

    # Routine disruption: NWS warning days.
    ugc = conn.execute("SELECT ugc, kind, timezone FROM hub_ugc WHERE hub_id=?", (hub_id,)).fetchall()
    tz = ugc[0][2] if ugc else "UTC"
    codes = [u[0] for u in ugc] + [f"POINT:{hub_id}"]   # zone/county products + polygons containing the hub
    warns = pd.read_sql(f"SELECT phenomena, issue, expire FROM nws_warnings WHERE ugc IN "
                        f"({','.join('?' * len(codes))})", conn, params=codes)
    wdays = m.warning_days(warns, tz, wp["phenomena"], (wp["first_year"], wp["last_year"]))
    n_days = len(pd.date_range(f"{wp['first_year']}-01-01", f"{wp['last_year']}-12-31"))

    # Evidence: measured threshold days (station-first daily data).
    series, infos, wt, st_info = m.load_hub_daily(conn, hub_id)
    torn = _event_row(conn, hub_id, "tornado", ep["default_radius_km"]["convective"])
    tornado_dates = set(json.loads(torn[6]).get("dates", [])) if torn else set()
    flags = m.disruption_flags(series, wt, tornado_dates, sp["thresholds"])

    hazards = {}
    for h in HAZARDS:
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

    hr = _event_row(conn, hub_id, "hurricane_days", ep["default_radius_km"]["hurricane"])
    storms = _event_row(conn, hub_id, "hurricane", ep["default_radius_km"]["hurricane"])
    hu = _event_row(conn, hub_id, "hurricane_strength", ep["default_radius_km"]["hurricane"])
    hazards["hurricane"]["storm_record"] = {
        "radius_km": ep["default_radius_km"]["hurricane"], "window": [storms[4], storms[5]],
        "storm_days_per_year": hr[1], "storms_per_decade": storms[1] * 10,
        "storms_per_decade_range": [storms[2] * 10, storms[3] * 10],
        "hurricane_strength_per_decade": hu[1] * 10, "p_storm_within_10y": float(1 - np.exp(-storms[1] * 10)),
        "notable_storms": json.loads(hu[6])["notable_storms"] or json.loads(storms[6])["notable_storms"]}

    desc = {}
    for name, s in m.descriptive_flags(series, wt, sp["descriptive_thresholds"]).items():
        st = m.summarize_flags(s, level)
        desc[name] = {"per_year": st["per_year"], "range": [st["lo"], st["hi"]], "years_observed": st["years"]}
    desc["max_3day_precip_mm"] = {"value": m.max_3day_precip_mean(series["precip_mm"])}
    snow = series["snow_cm"].copy()
    snow.index = pd.to_datetime(snow.index)
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


def severity_shares(conn: sqlite3.Connection, evidence: dict[str, dict]) -> dict[str, dict]:
    p = get_params()
    types = p["declarations"]["incident_types"]
    decl = pd.read_sql("SELECT county_fips, incident_type, begin_date, end_date FROM fema_declarations", conn)
    county = dict(conn.execute("SELECT id, county_fips FROM hubs").fetchall())
    k, n = {h: 0 for h in HAZARDS}, {h: 0 for h in HAZARDS}
    for hub_id, ev in evidence.items():
        dc = decl[decl["county_fips"] == county[hub_id]]
        for h in HAZARDS:
            dates = ev["_warning_dates"][h]
            inc = dc[dc["incident_type"].isin(types[h])]
            k[h] += m.dates_in_incidents(dates, inc)
            n[h] += len(dates)
    return {h: m.share_with_interval(k[h], n[h], p["event_rates"]["confidence_level"]) for h in HAZARDS}


# --------------------------------------------------------------------------- all hubs


def score_all(conn: sqlite3.Connection, day_costs: dict | None = None, store: bool = True) -> pd.DataFrame:
    p = get_params()
    hub_ids = [r[0] for r in conn.execute("SELECT id FROM hubs ORDER BY id")]
    evidence = {h: hub_evidence(conn, h) for h in hub_ids}
    shares = severity_shares(conn, evidence)

    rows = {}
    for h, ev in evidence.items():
        ev.pop("_warning_dates")
        severe = {}
        for z in HAZARDS:
            hz = ev["hazards"][z]
            hz["severity_share"] = shares[z]
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
    df["rank"] = df["composite"].rank(ascending=False, method="min").astype(int)
    df["routine_rank"] = df["routine_total"].rank(ascending=False, method="min").astype(int)

    if store:
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
    row = conn.execute("SELECT computed_at, scoring_version, composite, tier, details_json FROM risk_scores "
                       "WHERE hub_id = ? ORDER BY id DESC LIMIT 1", (hub_id,)).fetchone()
    if not row:
        return None
    d = json.loads(row[4])
    total = row[2] or 1.0
    d["severe_days_per_year"], d["tier"], d["computed_at"], d["scoring_version"] = row[2], row[3], row[0], row[1]
    d["contributions"] = sorted(
        [{"hazard": h, "severe_days_per_year": d["hazards"][h]["severe_days_per_year"],
          "share_of_total": d["hazards"][h]["severe_days_per_year"] / total if total else 0.0,
          "warning_days_per_year": d["hazards"][h]["warning_days_per_year"],
          "severity_share": d["hazards"][h]["severity_share"]["share"]} for h in HAZARDS],
        key=lambda x: -x["severe_days_per_year"])
    return d


def main() -> None:
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
