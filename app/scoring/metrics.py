"""Daily hub weather (station first, Open-Meteo as backup) and day-based metrics.

Pure functions (combine_variable, disruption_flags, rate_with_interval, descriptive flags)
take pandas objects and are unit-tested on synthetic data. load_hub_daily() wraps the
database.
"""

import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import chi2

from app.scoring.params import get_params

DAYS_PER_YEAR = 365.25   # average year length including leap years, used to turn day counts into rates

# Our variable name -> (column in station_daily, column in daily_weather from Open-Meteo)
VARIABLES = {
    "precip_mm": ("prcp_mm", "precip_mm"),
    "snow_cm": ("snow_cm", "snowfall_cm"),
    "tmax_c": ("tmax_c", "tmax_c"),
    "tmin_c": ("tmin_c", "tmin_c"),
    "gust_kmh": ("gust_kmh", "wind_gust_kmh"),
}
# Observed weather-event flags from GHCN-Daily (WT03 thunder, WT04 ice pellets, WT05 hail, WT06 glaze,
# WT17 freezing rain). Observers record them inconsistently, so they only add evidence and never remove it.
WT_FLAGS = ["wt_thunder", "wt_ice_pellets", "wt_hail", "wt_glaze", "wt_freezing_rain"]


@dataclass
class VariableInfo:
    """Where one daily weather variable came from for a hub, so answers can state the source and its quality."""
    source: str              # station | station+open_meteo | open_meteo
    window: tuple[str, str]  # dates actually used
    coverage: float          # share of window days with a value
    filled_share: float      # share of used values that came from Open-Meteo
    notes: list[str] = field(default_factory=list)


def combine_variable(station: pd.Series, open_meteo: pd.Series, window: tuple[str, str],
                     om_window: tuple[str, str], min_coverage: float,
                     station_usable: bool = True) -> tuple[pd.Series, VariableInfo]:
    """Station value where present, else Open-Meteo. If coverage stays below min_coverage
    (or the station can't be used for this variable), use Open-Meteo alone over its window."""
    days = pd.date_range(*window).strftime("%Y-%m-%d")
    st = station.reindex(days) if station_usable else pd.Series(np.nan, index=days)
    om = open_meteo.reindex(days)
    combined = st.combine_first(om)          # station value when present, otherwise the model value
    coverage = float(combined.notna().mean())
    if station_usable and coverage >= min_coverage:
        # filled = share of the used values that had to come from the model instead of the station.
        filled = float((st.isna() & om.notna()).sum() / max(combined.notna().sum(), 1))
        src = "station" if filled == 0 else "station+open_meteo"
        return combined, VariableInfo(src, window, round(coverage, 4), round(filled, 4))
    # Not enough station data: use the model alone, but only over the years the model covers (2016 onward),
    # so missing years are not silently treated as days without weather.
    om_days = pd.date_range(*om_window).strftime("%Y-%m-%d")
    om_only = open_meteo.reindex(om_days)
    note = "station not usable for this variable" if not station_usable else \
        f"station coverage {coverage:.0%} < {min_coverage:.0%}; Open-Meteo used"
    return om_only, VariableInfo("open_meteo", om_window, round(float(om_only.notna().mean()), 4), 1.0, [note])


def rate_with_interval(k: int, n_days: int, level: float) -> dict:
    """Events per year from k events over n_days observed days, with an exact Poisson interval.

    The exact (Garwood) interval uses the chi-square distribution and is valid for small counts, including zero:
    0 events in 20 years still gives an upper bound above zero, because "not seen yet" is not "impossible".
    """
    years = n_days / DAYS_PER_YEAR if n_days else 0.0
    if years == 0:
        return {"per_year": None, "lo": None, "hi": None, "events": k, "years": 0.0}
    a = 1 - level   # for a 90% interval, 5% is left out on each side
    lo = 0.0 if k == 0 else chi2.ppf(a / 2, 2 * k) / 2
    hi = chi2.ppf(1 - a / 2, 2 * k + 2) / 2
    return {"per_year": k / years, "lo": lo / years, "hi": hi / years, "events": int(k), "years": round(years, 2)}


def disruption_flags(series: dict[str, pd.Series], wt: pd.DataFrame | None, tornado_dates: set[str],
                     th: dict) -> dict[str, pd.Series]:
    """Per-day flags (1.0 yes, 0.0 no, NaN unknown) for days above fixed weather thresholds.

    These are supporting evidence only. A missing measurement gives NaN rather than 0, so days without data are
    left out of the rate instead of being counted as calm days.
    """
    snow, prcp, tmax, gust = (series[k] for k in ("snow_cm", "precip_mm", "tmax_c", "gust_kmh"))

    def flag(cond: pd.Series, base: pd.Series) -> pd.Series:
        return cond.astype(float).where(base.notna())

    # Ice day: the station observed glaze, freezing rain or ice pellets (only from stations that record events).
    ice = pd.Series(0.0, index=snow.index)
    if wt is not None and not wt.empty:
        icy = wt.reindex(snow.index)[["wt_glaze", "wt_freezing_rain", "wt_ice_pellets"]].fillna(0).sum(axis=1) > 0
        ice = icy.astype(float)
    winter = flag((snow >= th["disruption_snow_cm"]) | (ice > 0), snow)

    # Convective day: a damaging gust at the station, or a tornado report within the search radius that day.
    torn = pd.Series(gust.index.isin(list(tornado_dates)), index=gust.index)
    convective = flag((gust >= th["disruption_gust_kmh"]) | torn, gust)
    return {
        "winter": winter,
        "flood": flag(prcp >= th["disruption_precip_mm"], prcp),
        "convective": convective,
        "heat": flag(tmax >= th["disruption_heat_c"], tmax),
    }


def descriptive_flags(series: dict[str, pd.Series], wt: pd.DataFrame | None, th: dict) -> dict[str, pd.Series]:
    """Per-day flags for metrics shown in answers (not used for ranking)."""
    snow, prcp, tmax, tmin, gust = (series[k] for k in ("snow_cm", "precip_mm", "tmax_c", "tmin_c", "gust_kmh"))

    def flag(cond, base):
        return cond.astype(float).where(base.notna())

    out = {
        "snow_day": flag(snow >= th["snow_day_cm"], snow),
        "trace_snow_day": flag(snow >= th["trace_snow_cm"], snow),
        "heavy_snow_day": flag(snow >= th["heavy_snow_cm"], snow),
        "extreme_cold_day": flag(tmin <= th["extreme_cold_c"], tmin),
        "heavy_rain_day": flag(prcp >= th["heavy_rain_mm"], prcp),
        "extreme_heat_day": flag(tmax >= th["extreme_heat_c"], tmax),
        "damaging_gust_day": flag(gust >= th["damaging_gust_kmh"], gust),
    }
    if wt is not None and not wt.empty:
        w = wt.reindex(snow.index).fillna(0)
        out["ice_day"] = ((w[["wt_glaze", "wt_freezing_rain", "wt_ice_pellets"]].sum(axis=1) > 0)).astype(float)
        out["thunder_day"] = (w["wt_thunder"] > 0).astype(float)
    return out


def summarize_flags(flags: pd.Series, level: float) -> dict:
    """Turn daily flags into a rate per year, counting only days that have data."""
    valid = flags.dropna()
    return rate_with_interval(int(valid.sum()), int(len(valid)), level)


def max_3day_precip_mean(prcp: pd.Series) -> float | None:
    """Average over years of each year's largest 3-day precipitation total (years with >= 80% data)."""
    s = prcp.copy()
    s.index = pd.to_datetime(s.index)
    roll = s.rolling(3, min_periods=3).sum()
    per_year = roll.groupby(roll.index.year).max()
    ok = s.notna().groupby(s.index.year).mean() >= 0.8
    vals = per_year[ok.reindex(per_year.index, fill_value=False)].dropna()
    return float(vals.mean()) if len(vals) else None


def warning_days(warnings: pd.DataFrame, tz: str, phenomena: dict[str, list[str]],
                 years: tuple[int, int]) -> dict[str, set[str]]:
    """Local calendar dates covered by at least one warning, per hazard.

    warnings: columns phenomena, issue, expire (UTC ISO strings). A warning covers every local
    date from its issue time to its expire time, inclusive.
    """
    out = {h: set() for h in phenomena}
    if warnings.empty:
        return out
    code_to_hazard = {c: h for h, codes in phenomena.items() for c in codes}
    w = warnings[warnings["phenomena"].isin(code_to_hazard)].copy()   # drop types we do not use (advisories)

    # Convert UTC times to the hub's local calendar date. The two IEM endpoints use slightly different ISO
    # formats ("T03:58Z" and "T03:58:00Z"), so the parser is told to accept any ISO 8601 form.
    def to_local_date(s):
        return pd.to_datetime(s, utc=True, format="ISO8601").dt.tz_convert(tz).dt.normalize().dt.tz_localize(None)

    w["start"], w["stop"] = to_local_date(w["issue"]), to_local_date(w["expire"])
    w["stop"] = w[["start", "stop"]].max(axis=1)   # guards against an expire time recorded before the issue time
    # A set of dates per hazard means overlapping warnings on the same day count as one warning day.
    lo, hi = str(years[0]), str(years[1])
    for code, s, e in zip(w["phenomena"], w["start"], w["stop"]):
        for d in pd.date_range(s, e).strftime("%Y-%m-%d"):
            if lo <= d[:4] <= hi:
                out[code_to_hazard[code]].add(d)
    return out


def dates_in_incidents(dates: set[str], incidents: pd.DataFrame) -> int:
    """How many dates fall inside any incident period (columns begin_date, end_date, ISO dates)."""
    if not dates or incidents.empty:
        return 0
    b, e = incidents["begin_date"].to_numpy(), incidents["end_date"].to_numpy()
    # ISO dates (YYYY-MM-DD) compare correctly as text, so no date parsing is needed here.
    return sum(bool(((b <= d) & (e >= d)).any()) for d in dates)


def share_with_interval(k: int, n: int, level: float) -> dict:
    """Binomial share k/n with an exact (Clopper-Pearson) interval.

    Exact intervals stay valid for small k (for example 0 heat disasters out of 2,053 warning days), where the
    usual normal approximation would give a range below zero.
    """
    from scipy.stats import beta
    if n == 0:
        return {"share": 0.0, "lo": 0.0, "hi": 0.0, "k": 0, "n": 0}
    a = 1 - level
    lo = 0.0 if k == 0 else float(beta.ppf(a / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - a / 2, k + 1, n - k))
    return {"share": k / n, "lo": lo, "hi": hi, "k": int(k), "n": int(n)}


# --------------------------------------------------------------------------- database


def load_hub_daily(conn: sqlite3.Connection, hub_id: str) -> tuple[dict, dict, pd.DataFrame | None, dict]:
    """Return (series per variable, VariableInfo per variable, weather-type flags or None, station info)."""
    sp = get_params()["scoring"]
    window = (f"{sp['window'][0]}-01-01", f"{sp['window'][1]}-12-31")
    om_window = ("2016-01-01", f"{sp['window'][1]}-12-31")

    st_row = conn.execute("SELECT station_id, station_name, distance_km, gust_measured FROM hub_stations "
                          "WHERE hub_id = ?", (hub_id,)).fetchone()
    station = pd.read_sql("SELECT * FROM station_daily WHERE station_id = ?", conn,
                          params=(st_row[0],)).set_index("date") if st_row else pd.DataFrame()
    om = pd.read_sql("SELECT * FROM daily_weather WHERE hub_id = ?", conn, params=(hub_id,)).set_index("date")

    series, infos = {}, {}
    for var, (st_col, om_col) in VARIABLES.items():
        st_s = station[st_col] if st_col in station else pd.Series(dtype=float)
        # A station that does not measure gusts (gust_measured = 0) cannot be used for gusts; a missing gust
        # there means "not measured", not "calm".
        usable = bool(st_row) and not (var == "gust_kmh" and not st_row[3])
        series[var], infos[var] = combine_variable(st_s, om[om_col], window, om_window, sp["min_coverage"], usable)

    # Weather-type flags are only trusted from full-instrument stations (gust_measured = 1).
    wt = station[WT_FLAGS] if (st_row and st_row[3] and not station.empty) else None
    st_info = {"station_id": st_row[0], "station_name": st_row[1], "distance_km": st_row[2],
               "gust_measured": bool(st_row[3])} if st_row else None
    return series, infos, wt, st_info
