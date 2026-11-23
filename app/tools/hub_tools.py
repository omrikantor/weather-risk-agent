"""Deterministic tool functions the agent calls. Every number an answer uses comes from here.

Each result carries `source`, data windows and `scoring_version` so answers can cite them.
"""

import json
import sqlite3
from datetime import date

import pandas as pd

from app.db import get_connection
from app.scoring import metrics as m
from app.scoring.engine import HAZARDS, latest_breakdown
from app.scoring.params import get_params
from app.tools import schemas as s
from app.tools.locate import find_nearest_hubs as _find_nearest_hubs

KPI_DEFINITIONS = {
    "severe_disruption_days": "Expected days per year with an NWS warning that is part of a federally declared "
                              "major disaster (warning days x the hazard's FEMA severity share). Primary ranking.",
    "routine_disruption_days": "Expected days per year with an NWS warning for the hazard at the hub "
                               "(locally calibrated thresholds).",
}
SOURCES = "NWS warnings (IEM archive, 2006-2025); FEMA disaster declarations (OpenFEMA, 1996-2025)"


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def _latest(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT r.hub_id, h.name, h.state, h.region, r.composite, r.winter, r.flood, r.convective, r.hurricane, "
        "r.heat, r.tier, r.scoring_version, r.computed_at, r.details_json FROM risk_scores r JOIN hubs h "
        "ON h.id = r.hub_id WHERE r.computed_at = (SELECT MAX(computed_at) FROM risk_scores)", conn)
    details = df["details_json"].map(json.loads)
    for h in HAZARDS:
        df[f"routine_{h}"] = details.map(lambda d, h=h: d["hazards"][h]["warning_days_per_year"] or 0.0)
    df["routine_total"] = details.map(lambda d: d["routine_total"])
    df["confidence"] = details.map(lambda d: d["confidence"])
    return df.drop(columns="details_json")


def _with_conn(fn):
    def wrapper(args, conn=None):
        own = conn is None
        conn = conn or get_connection()
        try:
            return fn(args, conn)
        finally:
            if own:
                conn.close()
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


# --------------------------------------------------------------------------- tools


@_with_conn
def list_hubs(args: s.ListHubsInput, conn) -> dict:
    q = "SELECT id, name, state, region FROM hubs" + (" WHERE region = ?" if args.region else "") + " ORDER BY id"
    rows = conn.execute(q, (args.region,) if args.region else ()).fetchall()
    return {"hubs": [{"hub_id": r[0], "name": r[1], "state": r[2], "region": r[3]} for r in rows],
            "count": len(rows), "note": "The hub network is an assumption of this prototype (47 US cities); "
                                        "regions are US Census Bureau regions."}


@_with_conn
def rank_hubs(args: s.RankHubsInput, conn) -> dict:
    df = _latest(conn)
    if args.region:
        df = df[df["region"] == args.region]
    severe = args.measure == "severe_disruption_days"
    if args.hazard == "all":
        cols = HAZARDS if severe else [f"routine_{h}" for h in HAZARDS]
        costs = {h: 1.0 for h in HAZARDS}
        if args.day_costs:
            costs.update({h: v for h, v in args.day_costs.model_dump().items() if v is not None})
        df["value"] = sum(df[c] * costs[h] for c, h in zip(cols, HAZARDS))
    else:
        df["value"] = df[args.hazard] if severe else df[f"routine_{args.hazard}"]
    df = df.sort_values(["value", "hub_id"], ascending=[False, True]).reset_index(drop=True)
    n_all = len(df)
    if args.top_n:
        df = df.head(args.top_n)
    rows = [{"rank": i + 1, "hub_id": r.hub_id, "name": r.name, "state": r.state, "region": r.region,
             "value_days_per_year": _r(r.value), "network_tier": r.tier, "confidence": r.confidence}
            for i, r in enumerate(df.itertuples())]
    return {"measure": args.measure, "definition": KPI_DEFINITIONS[args.measure], "hazard": args.hazard,
            "region": args.region, "hubs_considered": n_all, "ranking": rows,
            "day_costs": args.day_costs.model_dump() if args.day_costs else None,
            "note": "network_tier = High/Elevated/Low third of all 47 hubs by total severe disruption days.",
            "source": SOURCES, "scoring_version": df["scoring_version"].iloc[0] if len(df) else None}


def _evidence(d: dict, hazard: str) -> dict:
    """Key supporting metrics for one hazard from a hub breakdown."""
    desc, hz = d["descriptive"], d["hazards"][hazard]

    def per_year(k):
        return _r(desc.get(k, {}).get("per_year"))

    if hazard == "winter":
        ev = {"snow_days_per_year(>=1cm)": per_year("snow_day"), "heavy_snow_days_per_year(>=10cm)":
              per_year("heavy_snow_day"), "ice_days_per_year(observed)": per_year("ice_day"),
              "annual_snowfall_cm": _r(desc["annual_snowfall_cm"]["value"], 1),
              "extreme_cold_days_per_year(<=-15C)": per_year("extreme_cold_day")}
    elif hazard == "flood":
        ev = {"heavy_rain_days_per_year(>=50mm)": per_year("heavy_rain_day"),
              "avg_annual_max_3day_rain_mm": _r(desc["max_3day_precip_mm"]["value"], 1)}
    elif hazard == "convective":
        ev = {"tornado_report_days_per_year(EF1+,50km)": per_year("tornado_report_days"),
              "hail_report_days_per_year(>=1in,50km)": per_year("hail_report_days"),
              "wind_report_days_per_year(>=50kt,50km)": per_year("wind_report_days"),
              "damaging_gust_days_per_year(>=93km/h)": per_year("damaging_gust_day"),
              "thunder_days_per_year(observed)": per_year("thunder_day")}
    elif hazard == "hurricane":
        sr = hz["storm_record"]
        ev = {"tropical_storms_within_150km_per_decade": _r(sr["storms_per_decade"]),
              "hurricanes_within_150km_per_decade": _r(sr["hurricane_strength_per_decade"]),
              "p_storm_within_150km_next_10y": _r(sr["p_storm_within_10y"]),
              "notable_storms": sr["notable_storms"][:5], "record": "NOAA HURDAT2 1950-2025"}
    else:
        ev = {"extreme_heat_days_per_year(>=38C)": per_year("extreme_heat_day")}
    return ev


def _hazard_summary(d: dict, h: str) -> dict:
    hz = d["hazards"][h]
    ss = hz["severity_share"]
    return {"severe_days_per_year": _r(hz["severe_days_per_year"]),
            "warning_days_per_year": _r(hz["warning_days_per_year"]),
            "warning_days_range_90pct": [_r(x) for x in hz["warning_days_range"]],
            "severity_share": _r(ss["share"], 3), "severity_share_range_90pct": [_r(ss["lo"], 3), _r(ss["hi"], 3)],
            "evidence": _evidence(d, h)}


@_with_conn
def compare_hubs(args: s.CompareHubsInput, conn) -> dict:
    hazards = args.hazards or HAZARDS
    out, ranks = [], {}
    for hub in args.hub_ids:
        d = latest_breakdown(conn, hub.value)
        name, state = conn.execute("SELECT name, state FROM hubs WHERE id=?", (hub.value,)).fetchone()
        out.append({"hub_id": hub.value, "name": name, "state": state, "overall_rank": d["rank"], "of": d["of"],
                    "tier": d["tier"], "severe_days_total": _r(d["severe_days_per_year"]),
                    "routine_days_total": _r(d["routine_total"]), "confidence": d["confidence"],
                    "hazards": {h: _hazard_summary(d, h) for h in hazards}})
    for h in hazards:
        best = max(out, key=lambda o: (o["hazards"][h]["severe_days_per_year"] or 0))
        ranks[h] = {"most_exposed_by_severe_days": best["hub_id"],
                    "values": {o["hub_id"]: o["hazards"][h]["severe_days_per_year"] for o in out}}
    return {"hubs": out, "by_hazard": ranks, "definitions": KPI_DEFINITIONS, "source": SOURCES,
            "evidence_sources": "GHCN-Daily airport stations 1996-2025 (Open-Meteo fills gaps); NOAA HURDAT2; NOAA SPC"}


@_with_conn
def get_hub_risk_breakdown(args: s.HubBreakdownInput, conn) -> dict:
    hub = args.hub_id.value
    d = latest_breakdown(conn, hub)
    name, state, region, fips = conn.execute("SELECT name, state, region, county_fips FROM hubs WHERE id=?",
                                             (hub,)).fetchone()
    decl = conn.execute("SELECT title, incident_type, begin_date, end_date FROM fema_declarations "
                        "WHERE county_fips=? ORDER BY begin_date DESC LIMIT 8", (fips,)).fetchall()
    st = d["station"]
    return {
        "hub_id": hub, "name": name, "state": state, "region": region,
        "rank": d["rank"], "of": d["of"], "tier": d["tier"],
        "severe_days_per_year": _r(d["severe_days_per_year"]), "routine_days_per_year": _r(d["routine_total"]),
        "routine_rank": d["routine_rank"],
        "contributions": [{"hazard": c["hazard"], "severe_days_per_year": _r(c["severe_days_per_year"]),
                           "share_of_total": _r(c["share_of_total"], 3),
                           "warning_days_per_year": _r(c["warning_days_per_year"]),
                           "severity_share": _r(c["severity_share"], 3)} for c in d["contributions"]],
        "hazards": {h: _hazard_summary(d, h) for h in HAZARDS},
        "recent_fema_major_disasters_in_county": [
            {"title": t, "type": ty, "begin": b, "end": e} for t, ty, b, e in decl],
        "station": {"id": st["station_id"], "name": st["station_name"], "distance_km": st["distance_km"],
                    "measures_gusts_and_weather_events": st["gust_measured"]} if st else None,
        "confidence": d["confidence"], "confidence_reasons": d["confidence_reasons"],
        "definitions": KPI_DEFINITIONS, "source": SOURCES, "scoring_version": d["scoring_version"],
        "computed_at": d["computed_at"],
    }


DAY_FLAGS = {  # metric -> (variable, test) on the combined daily series
    "snow_day": ("snow_cm", lambda v: v >= 1.0), "trace_snow_day": ("snow_cm", lambda v: v >= 0.1),
    "heavy_snow_day": ("snow_cm", lambda v: v >= 10), "freezing_day": ("tmin_c", lambda v: v <= 0),
    "extreme_cold_day": ("tmin_c", lambda v: v <= -15), "rain_day": ("precip_mm", lambda v: v >= 1),
    "heavy_rain_day": ("precip_mm", lambda v: v >= 50), "extreme_heat_day": ("tmax_c", lambda v: v >= 38),
    "damaging_gust_day": ("gust_kmh", lambda v: v >= 93),
}
DAY_DEFINITIONS = {
    "snow_day": "snowfall >= 1 cm", "trace_snow_day": "snowfall >= 0.1 cm", "heavy_snow_day": "snowfall >= 10 cm",
    "freezing_day": "daily low <= 0 C", "extreme_cold_day": "daily low <= -15 C", "rain_day": "precipitation >= 1 mm",
    "heavy_rain_day": "precipitation >= 50 mm", "extreme_heat_day": "daily high >= 38 C (100 F)",
    "damaging_gust_day": "peak gust >= 93 km/h (58 mph)",
}
TOTALS = {"total_precipitation_mm": "precip_mm", "total_snowfall_cm": "snow_cm"}


@_with_conn
def get_weather_stat(args: s.WeatherStatInput, conn) -> dict:
    hub, metric = args.hub_id.value, args.metric
    start, end = args.start_date.isoformat(), args.end_date.isoformat()
    days = pd.date_range(start, end).strftime("%Y-%m-%d")
    base = {"hub_id": hub, "metric": metric, "start_date": start, "end_date": end, "days_in_period": len(days)}

    if metric.startswith("warning_days_"):
        hazard = metric.removeprefix("warning_days_")
        wp = get_params()["warnings"]
        ugc = conn.execute("SELECT ugc, timezone FROM hub_ugc WHERE hub_id=?", (hub,)).fetchall()
        codes = [u[0] for u in ugc] + [f"POINT:{hub}"]
        w = pd.read_sql(f"SELECT phenomena, issue, expire FROM nws_warnings WHERE ugc IN ({','.join('?' * len(codes))})",
                        conn, params=codes)
        wd = m.warning_days(w, ugc[0][1], wp["phenomena"], (wp["first_year"], wp["last_year"]))[hazard]
        lo_ok, hi_ok = f"{wp['first_year']}-01-01", f"{wp['last_year']}-12-31"
        covered = [d for d in days if lo_ok <= d <= hi_ok]
        count = sum(d in wd for d in covered)
        return {**base, "count": count, "days_with_data": len(covered),
                "percent_of_days": _r(100 * count / len(covered)) if covered else None,
                "definition": f"days with an NWS warning of types {wp['phenomena'][hazard]} at the hub",
                "source": f"NWS warnings via IEM archive ({wp['first_year']}-{wp['last_year']})",
                "caveat": None if len(covered) == len(days) else "part of the period is outside the warning archive window"}

    st = conn.execute("SELECT station_id, station_name, distance_km, gust_measured FROM hub_stations WHERE hub_id=?",
                      (hub,)).fetchone()
    station = pd.read_sql("SELECT * FROM station_daily WHERE station_id=? AND date BETWEEN ? AND ?", conn,
                          params=(st[0], start, end)).set_index("date") if st else pd.DataFrame()
    om = pd.read_sql("SELECT * FROM daily_weather WHERE hub_id=? AND date BETWEEN ? AND ?", conn,
                     params=(hub, start, end)).set_index("date")
    st_info = {"station": st[1], "station_id": st[0], "distance_km": st[2]} if st else None

    if metric in ("ice_day", "thunder_day"):
        if not st or not st[3]:
            return {**base, "count": None, "error": "This hub's station does not record weather events; "
                                                    "ice/thunder days are unavailable."}
        cols = ["wt_glaze", "wt_freezing_rain", "wt_ice_pellets"] if metric == "ice_day" else ["wt_thunder"]
        obs = station.reindex(days)
        has = obs["prcp_mm"].notna() | obs["tmax_c"].notna()       # station reported that day
        count = int((obs.loc[has, cols].fillna(0).sum(axis=1) > 0).sum())
        n = int(has.sum())
        return {**base, "count": count, "days_with_data": n, "percent_of_days": _r(100 * count / n) if n else None,
                "definition": "observed " + ("glaze, freezing rain or ice pellets" if metric == "ice_day" else "thunder"),
                "source": "NOAA GHCN-Daily weather-type flags", **({"station": st_info} if st_info else {}),
                "caveat": "Flags are recorded inconsistently; a missing flag doesn't prove the event didn't happen."}

    var = DAY_FLAGS[metric][0] if metric in DAY_FLAGS else TOTALS[metric]
    st_col, om_col = m.VARIABLES[var]
    st_s = station[st_col].reindex(days) if st_col in station else pd.Series(index=days, dtype=float)
    if var == "gust_kmh" and st and not st[3]:
        st_s = pd.Series(index=days, dtype=float)               # no measured gusts at this station
    om_s = om[om_col].reindex(days) if om_col in om else pd.Series(index=days, dtype=float)
    combined = st_s.combine_first(om_s)
    n = int(combined.notna().sum())
    sources = {"station_days": int(st_s.notna().sum()), "open_meteo_days": int((st_s.isna() & om_s.notna()).sum())}
    res = {**base, "days_with_data": n, "sources": sources, **({"station": st_info} if st_info else {})}
    if metric in TOTALS:
        res.update({"total": _r(combined.sum(), 1), "unit": "mm" if var == "precip_mm" else "cm"})
    else:
        count = int(combined.dropna().map(DAY_FLAGS[metric][1]).sum())
        res.update({"count": count, "percent_of_days": _r(100 * count / n) if n else None,
                    "definition": DAY_DEFINITIONS[metric]})
    res["source"] = ("NOAA GHCN-Daily station (measured)" if sources["open_meteo_days"] == 0 else
                     "NOAA GHCN-Daily station, gaps filled with Open-Meteo (modeled)") if sources["station_days"] else \
        "Open-Meteo (modeled reanalysis)"
    if n < 0.8 * len(days):
        res["caveat"] = f"Data available for only {n} of {len(days)} days."
    return res


@_with_conn
def get_methodology(args: s.MethodologyInput, conn) -> dict:
    p = get_params()
    details, version = conn.execute("SELECT details_json, scoring_version FROM risk_scores ORDER BY id DESC LIMIT 1"
                                    ).fetchone()
    d = {**json.loads(details), "scoring_version": version}
    shares = {h: {"share": _r(d["hazards"][h]["severity_share"]["share"], 3),
                  "range_90pct": [_r(d["hazards"][h]["severity_share"]["lo"], 3),
                                  _r(d["hazards"][h]["severity_share"]["hi"], 3)],
                  "warning_days_pooled": d["hazards"][h]["severity_share"]["n"]} for h in HAZARDS}
    return {
        "kpis": KPI_DEFINITIONS,
        "severity_shares": shares,
        "warning_types_by_hazard": p["warnings"]["phenomena"],
        "fema_incident_types_by_hazard": p["declarations"]["incident_types"],
        "tiers": "High / Elevated / Low = top / middle / bottom third of the 47 hubs by severe disruption days.",
        "confidence": "Per hub, lowered by: station > 25 km away, no measured gusts/weather events, or modeled "
                      "data used for a variable.",
        "data_sources": [
            "NWS warnings, IEM VTEC archive 2006-2025 (zone/county products by hub zone; storm-based polygons only "
            "when the polygon contains the hub)",
            "FEMA disaster declarations (OpenFEMA), major disasters 1996-2025",
            "NOAA GHCN-Daily airport stations 1996-2025 (measured), Open-Meteo reanalysis fills gaps (2016+)",
            "NOAA HURDAT2 hurricane tracks 1950-2025; NOAA SPC tornado/hail/wind reports 1996-2025",
            "FEMA National Risk Index (county hazard profiles, used only to pick stand-in hubs)",
            "Census Gazetteer + population estimates (place lookup)",
        ],
        "assumptions_and_limitations": [
            "The 47-hub network and city-center coordinates are assumptions; a client would supply real sites.",
            "Regions are US Census Bureau regions.",
            "Every severe disruption day counts the same unless an analyst supplies day costs.",
            "NWS warnings are forecasts (some false alarms/misses) and their criteria changed over time.",
            "FEMA declarations are partly policy-driven; heat is essentially never declared, so heat has a severity "
            "share of 0 (no evidence of disaster-level heat), not 'heat never disrupts'.",
            "Severity shares are pooled across our 47 hubs.",
            "Past frequency does not fully predict the future (climate trends).",
        ],
        "params_version": p["params_version"], "scoring_version": d.get("scoring_version"),
    }


def find_nearest_hubs(args: s.FindNearestHubsInput, conn=None) -> dict:
    return _find_nearest_hubs(args.location, conn)
