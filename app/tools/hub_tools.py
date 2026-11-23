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
SOURCES = "National Weather Service warnings (2006-2025) and FEMA disaster declarations (1996-2025)"
EVIDENCE_SOURCES = ("Measured weather at airport stations (NOAA, 1996-2025), NOAA hurricane tracks (1950-2025) and "
                    "NOAA severe-storm reports (1996-2025)")


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def severe_range(hz: dict) -> tuple[float, float]:
    """Conservative 90% range for severe days: (warning-days low x share low, warning-days high x share high).
    Combining the two intervals this way is wider than a joint interval, i.e. it errs on the cautious side."""
    w_lo, w_hi = (x or 0.0 for x in hz["warning_days_range"])
    s = hz["severity_share"]
    return w_lo * s["lo"], w_hi * s["hi"]


def _latest(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql(
        "SELECT r.hub_id, h.name, h.state, h.region, r.composite, r.winter, r.flood, r.convective, r.hurricane, "
        "r.heat, r.tier, r.scoring_version, r.computed_at, r.details_json FROM risk_scores r JOIN hubs h "
        "ON h.id = r.hub_id WHERE r.computed_at = (SELECT MAX(computed_at) FROM risk_scores)", conn)
    details = df["details_json"].map(json.loads)
    for h in HAZARDS:
        df[f"routine_{h}"] = details.map(lambda d, h=h: d["hazards"][h]["warning_days_per_year"] or 0.0)
        df[f"routine_{h}_lo"] = details.map(lambda d, h=h: d["hazards"][h]["warning_days_range"][0] or 0.0)
        df[f"routine_{h}_hi"] = details.map(lambda d, h=h: d["hazards"][h]["warning_days_range"][1] or 0.0)
        lo, hi = zip(*details.map(lambda d, h=h: severe_range(d["hazards"][h])))
        df[f"{h}_lo"], df[f"{h}_hi"] = list(lo), list(hi)
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
    prefix = "" if severe else "routine_"
    hazards = HAZARDS if args.hazard == "all" else [args.hazard]
    costs = {h: 1.0 for h in HAZARDS}
    if args.day_costs:
        costs.update({h: v for h, v in args.day_costs.model_dump().items() if v is not None})
    for col in ("", "_lo", "_hi"):
        df["value" + col] = sum(df[f"{prefix}{h}{col}"] * costs[h] for h in hazards)
    # Ties share a rank (1, 2, 2, 4 ...); order within a tie is by hub id only for display.
    df["rank"] = df["value"].round(6).rank(ascending=False, method="min").astype(int)
    df = df.sort_values(["rank", "hub_id"]).reset_index(drop=True)
    n_all = len(df)
    if args.top_n:
        df = df.head(args.top_n)
    default_view = severe and args.hazard == "all" and not args.day_costs
    tier_key = "tier" if default_view else "overall_tier"
    rows = [{"rank": int(r.rank), "hub_id": r.hub_id, "name": r.name, "state": r.state, "region": r.region,
             "value_days_per_year": _r(r.value), "range_90pct": [_r(r.value_lo), _r(r.value_hi)],
             "no_exposure_in_data": bool(r.value == 0), tier_key: r.tier, "confidence": r.confidence}
            for r in df.itertuples()]
    ties = df[df.duplicated("rank", keep=False)]["rank"].nunique()
    notes = ["Hubs with equal values share a rank."] if ties else []
    notes.append("tier = High/Elevated/Low third of all 47 hubs by total severe disruption days (same measure "
                 "as this ranking)." if default_view else
                 "overall_tier refers to total severe disruption days across all hazards, NOT to this ranking's "
                 "measure; don't read it as this hazard's level.")
    notes.append("range_90pct combines the uncertainty of warning-day rates and FEMA severity shares (conservative).")
    return {"measure": args.measure, "definition": KPI_DEFINITIONS[args.measure], "hazard": args.hazard,
            "region": args.region, "hubs_considered": n_all, "ranking": rows,
            "day_costs": args.day_costs.model_dump() if args.day_costs else None, "notes": notes,
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
    lo, hi = severe_range(hz)
    return {"severe_days_per_year": _r(hz["severe_days_per_year"]),
            "severe_days_range_90pct": [_r(lo), _r(hi)],
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
        vals = {o["hub_id"]: o["hazards"][h]["severe_days_per_year"] or 0.0 for o in out}
        top = max(vals.values())
        leaders = [k for k, v in vals.items() if v == top]
        if top == 0:
            verdict = "none: no severe exposure in the data for any of these hubs"
        elif len(leaders) > 1:
            verdict = f"tie: {', '.join(leaders)}"
        else:
            verdict = leaders[0]
        ranks[h] = {"most_exposed_by_severe_days": verdict, "values": vals,
                    "ranges_90pct": {o["hub_id"]: o["hazards"][h]["severe_days_range_90pct"] for o in out}}
    return {"hubs": out, "by_hazard": ranks, "definitions": KPI_DEFINITIONS,
            "note": "If two hubs' 90% ranges overlap heavily, treat the difference as uncertain.",
            "source": SOURCES, "evidence_sources": EVIDENCE_SOURCES}


@_with_conn
def get_hub_risk_breakdown(args: s.HubBreakdownInput, conn) -> dict:
    hub = args.hub_id.value
    d = latest_breakdown(conn, hub)
    name, state, region, fips = conn.execute("SELECT name, state, region, county_fips FROM hubs WHERE id=?",
                                             (hub,)).fetchone()
    decl = conn.execute("SELECT title, incident_type, begin_date, end_date FROM fema_declarations "
                        "WHERE county_fips=? ORDER BY begin_date DESC LIMIT 8", (fips,)).fetchall()
    st = d["station"]
    ranges = [severe_range(d["hazards"][h]) for h in HAZARDS]
    monthly = [r[0] for r in conn.execute("SELECT SUM(severe_days) FROM hub_monthly WHERE hub_id=? GROUP BY month "
                                          "ORDER BY month", (hub,)).fetchall()]
    peak = max(range(len(monthly)), key=lambda i: monthly[i]) if monthly else None
    return {
        "hub_id": hub, "name": name, "state": state, "region": region,
        "rank": d["rank"], "of": d["of"], "tier": d["tier"],
        "severe_days_per_year": _r(d["severe_days_per_year"]),
        "severe_days_range_90pct": [_r(sum(r[0] for r in ranges)), _r(sum(r[1] for r in ranges))],
        "routine_days_per_year": _r(d["routine_total"]),
        "routine_rank": d["routine_rank"],
        "contributions": [{"hazard": c["hazard"], "severe_days_per_year": _r(c["severe_days_per_year"]),
                           "severe_days_range_90pct": [_r(x) for x in severe_range(d["hazards"][c["hazard"]])],
                           "share_of_total": _r(c["share_of_total"], 3),
                           "warning_days_per_year": _r(c["warning_days_per_year"]),
                           "severity_share": _r(c["severity_share"], 3)} for c in d["contributions"]],
        "hazards": {h: _hazard_summary(d, h) for h in HAZARDS},
        "monthly_severe_days_jan_to_dec": [_r(v, 3) for v in monthly],
        "peak_month": MONTHS[peak] if peak is not None else None,
        "recent_fema_major_disasters_in_county": [
            {"title": t, "type": ty, "begin": b, "end": e} for t, ty, b, e in decl],
        "station": {"id": st["station_id"], "name": st["station_name"], "distance_km": st["distance_km"],
                    "measures_gusts_and_weather_events": st["gust_measured"]} if st else None,
        "confidence": d["confidence"], "confidence_reasons": d["confidence_reasons"],
        "definitions": KPI_DEFINITIONS, "source": SOURCES, "evidence_sources": EVIDENCE_SOURCES,
        "scoring_version": d["scoring_version"],
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


GAP_FILL_METHOD = ("Per day: the station's measured value if present, otherwise Open-Meteo's modeled value "
                   "(available from 2016); days with neither are excluded. (Hub scoring instead switches a whole "
                   "variable to Open-Meteo 2016-2025 when station coverage over 1996-2025 is below 80%, so for a "
                   "few warm-climate stations' snowfall the two can differ slightly.)")


@_with_conn
def get_weather_stat(args: s.WeatherStatInput, conn) -> dict:
    """Day counts / totals for any date range.

    Gap filling differs on purpose from the scoring engine: for an arbitrary date range,
    filling day by day keeps as much measured data as possible; the engine's whole-variable
    fallback is about consistency over a 30-year window. The output states which was used.
    """
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
                "source": f"National Weather Service warnings ({wp['first_year']}-{wp['last_year']})",
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
                "source": "Weather events observed at the airport station (NOAA)",
                **({"station": st_info} if st_info else {}),
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
    res = {**base, "days_with_data": n, "sources": sources, "method": GAP_FILL_METHOD,
           **({"station": st_info} if st_info else {})}
    if metric in TOTALS:
        res.update({"total": _r(combined.sum(), 1), "unit": "mm" if var == "precip_mm" else "cm"})
    else:
        count = int(combined.dropna().map(DAY_FLAGS[metric][1]).sum())
        res.update({"count": count, "percent_of_days": _r(100 * count / n) if n else None,
                    "definition": DAY_DEFINITIONS[metric]})
    res["source"] = ("Measured weather at the airport station (NOAA)" if sources["open_meteo_days"] == 0 else
                     "Measured weather at the airport station (NOAA), with a few missing days estimated from "
                     "modeled data (Open-Meteo)") if sources["station_days"] else \
        "Modeled weather data (Open-Meteo reanalysis); no station measurements for this period"
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
            "Severe-day ranges combine the warning-rate and severity-share ranges conservatively (wider than a "
            "joint interval); hurricane shares rest on few events.",
            "Weather statistics for a date range fill missing station days with Open-Meteo day by day; hub "
            "scoring evidence switches a whole variable to Open-Meteo when station coverage is below 80%.",
            "Past frequency does not fully predict the future (climate trends).",
        ],
        "params_version": p["params_version"], "scoring_version": d.get("scoring_version"),
    }


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
HAZARD_LABELS = {"winter": "winter storms / ice", "flood": "flooding", "convective": "severe thunderstorms / wind",
                 "hurricane": "hurricanes / tropical storms", "heat": "extreme heat"}


@_with_conn
def get_seasonal_outlook(args: s.SeasonalOutlookInput, conn) -> dict:
    """Which hubs historically face the most disruption in a given calendar month, and why."""
    month = args.month or date.today().month
    q = ("SELECT m.*, h.name, h.state, h.region, h.county_fips FROM hub_monthly m JOIN hubs h ON h.id = m.hub_id "
         "WHERE m.month = ?" + (" AND h.region = ?" if args.region else ""))
    df = pd.read_sql(q, conn, params=(month, args.region) if args.region else (month,))
    totals = df.groupby("hub_id")["severe_days"].sum().round(6)
    rank = totals.rank(ascending=False, method="min").astype(int)
    order = sorted(totals.index, key=lambda h: (rank[h], h))[: (args.top_n or 5)]
    decl = pd.read_sql("SELECT county_fips, title, incident_type, begin_date FROM fema_declarations", conn)
    decl = decl[decl["begin_date"].str[5:7] == f"{month:02d}"].sort_values("begin_date", ascending=False)

    hubs = []
    for hub in order:
        rows = df[df["hub_id"] == hub].set_index("hazard")
        top = rows["severe_days"].idxmax() if rows["severe_days"].max() > 0 else rows["mean_warning_days"].idxmax()
        t = rows.loc[top]
        other = [{"hazard": z, "p_at_least_one_warning_day": _r(rows.loc[z, "p_warning"], 2)}
                 for z in rows.index if z != top and rows.loc[z, "p_warning"] >= 0.3]
        past = decl[decl["county_fips"] == t["county_fips"]].head(3)
        item = {
            "rank": int(rank[hub]), "hub_id": hub, "name": t["name"], "state": t["state"], "region": t["region"],
            "expected_severe_days_this_month": _r(totals[hub], 3),
            "top_hazard": top, "top_hazard_label": HAZARD_LABELS[top],
            "p_at_least_one_warning_day": _r(t["p_warning"], 2),
            "p_range_90pct": [_r(t["p_lo"], 2), _r(t["p_hi"], 2)],
            "years_with_warning": int(t["years_with_warning"]), "years_observed": int(t["years"]),
            "avg_warning_days_this_month": _r(t["mean_warning_days"], 2),
            "other_likely_hazards": other,
            "past_fema_disasters_this_month": [{"title": r.title, "type": r.incident_type, "year": r.begin_date[:4]}
                                               for r in past.itertuples()],
        }
        if top == "hurricane" and pd.notna(t["longrecord_p"]):
            item["p_storm_within_150km_1950_2025"] = _r(t["longrecord_p"], 2)
        hubs.append(item)

    summary = {}
    for z in HAZARDS:
        zr = df[df["hazard"] == z]
        likely = zr[zr["p_warning"] >= 0.5]
        best = zr.sort_values(["p_warning", "hub_id"], ascending=[False, True]).head(1)
        summary[z] = {"hubs_with_warning_in_most_years": int(len(likely)),
                      "most_frequent_hub": None if best.empty or best["p_warning"].iloc[0] == 0 else best["hub_id"].iloc[0],
                      "its_p": None if best.empty else _r(best["p_warning"].iloc[0], 2)}
    return {"month": month, "month_name": MONTHS[month - 1], "region": args.region, "hubs": hubs,
            "hazard_summary": summary,
            "definition": "p_at_least_one_warning_day = share of years 2006-2025 in which the hub had at least one "
                          "NWS warning day for the hazard in this month; expected_severe_days_this_month = average "
                          "warning days in this month x the hazard's FEMA severity share, summed over hazards.",
            "note": "Historical frequency (climatology), not a forecast.",
            "source": SOURCES}


def find_nearest_hubs(args: s.FindNearestHubsInput, conn=None) -> dict:
    return _find_nearest_hubs(args.location, conn)
