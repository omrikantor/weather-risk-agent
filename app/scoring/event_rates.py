"""Long-term event rates around each hub (Tier A), for every configured radius.

Hurricanes (HURDAT2, 1950+): a storm counts if its track (densified between 6-hourly
fixes) passes within the radius while at tropical-storm strength or more. Storms that
were at hurricane strength at that point are counted separately.

Convective (SPC, 1996+): counted as report days, i.e. distinct dates with at least one
qualifying report within the radius (tornado distance is measured to the path segment).

Each rate gets an exact Poisson interval and P(at least one event in the horizon).

Usage:
    python -m app.scoring.event_rates          # compute and store
    python -m app.scoring.event_rates --test   # also print the radius test and FEMA cross-check
"""

import argparse
import json
import math
import sqlite3

import numpy as np
import pandas as pd
from scipy.stats import chi2, spearmanr

from app.db import get_connection
from app.geo import haversine_km, point_to_segment_km
from app.scoring.params import get_params


def poisson_interval(k: int, years: int, level: float) -> tuple[float, float]:
    """Exact Poisson range for a rate of k events in a number of years (same method as metrics.rate_with_interval)."""
    a = 1 - level
    lo = 0.0 if k == 0 else chi2.ppf(a / 2, 2 * k) / 2
    hi = chi2.ppf(1 - a / 2, 2 * k + 2) / 2
    return lo / years, hi / years


def densify_tracks(conn: sqlite3.Connection, step_km: float) -> pd.DataFrame:
    """Linearly interpolate each storm's track so consecutive points are at most step_km apart.

    HURDAT2 gives a position every 6 hours, which can be 150 km or more apart for a fast storm. Without extra
    points a storm could pass close to a hub between two fixes and be missed by the distance test.
    """
    t = pd.read_sql("SELECT storm_id, name, dt, status, lat, lon, wind_kt FROM storm_tracks ORDER BY storm_id, dt", conn)
    # Epoch seconds, independent of pandas' datetime resolution (pandas 3 may store s/us, not ns,
    # so astype("int64") // 10**9 would be wrong).
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    t["ts"] = (pd.to_datetime(t["dt"], utc=True) - epoch) // pd.Timedelta(seconds=1)
    out = []
    for sid, g in t.groupby("storm_id", sort=False):
        lat, lon, wind = g["lat"].to_numpy(), g["lon"].to_numpy(), g["wind_kt"].to_numpy()
        status, ts = g["status"].to_numpy(), g["ts"].to_numpy()
        for i in range(len(g)):
            out.append((sid, lat[i], lon[i], wind[i], status[i], ts[i]))
            if i + 1 < len(g):
                d = float(haversine_km(lat[i], lon[i], lat[i + 1], lon[i + 1]))
                n = int(d // step_km)
                for j in range(1, n + 1):
                    f = j / (n + 1)
                    w = np.nan if (np.isnan(wind[i]) or np.isnan(wind[i + 1])) else wind[i] + f * (wind[i + 1] - wind[i])
                    out.append((sid, lat[i] + f * (lat[i + 1] - lat[i]), lon[i] + f * (lon[i + 1] - lon[i]), w,
                                status[i], ts[i] + f * (ts[i + 1] - ts[i])))
    pts = pd.DataFrame(out, columns=["storm_id", "lat", "lon", "wind_kt", "status", "ts"])
    # Calendar date (UTC) of each track point; used to count storm-affected days.
    pts["date"] = pd.to_datetime(pts["ts"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    names = t.drop_duplicates("storm_id").set_index("storm_id")["name"]
    pts["name"] = pts["storm_id"].map(names)
    return pts


def hurricane_rates(conn, hubs, p) -> list[dict]:
    """Per hub and radius: storms per year at tropical-storm strength or more, storms per year at hurricane
    strength, and storm-affected days per year, with the most notable storms for explanations."""
    hp = p["hurricane"]
    years = hp["last_year"] - hp["first_year"] + 1
    pts = densify_tracks(conn, hp["interpolate_step_km"])
    # The last four characters of a HURDAT2 storm id are its year (for example AL092022).
    pts = pts[pts["storm_id"].str[-4:].astype(int).between(hp["first_year"], hp["last_year"])]
    wind = pts["wind_kt"].fillna(0).to_numpy()
    eligible = pts["status"].isin(hp["statuses"]).to_numpy() & (wind >= hp["min_wind_kt"])
    hurricane = (pts["status"] == "HU").to_numpy() & (wind >= hp["hurricane_wind_kt"])
    rows = []
    for hub_id, lat, lon in hubs:
        d = haversine_km(lat, lon, pts["lat"].to_numpy(), pts["lon"].to_numpy())   # distance to every track point
        for r in p["radii_km"]["hurricane"]:   # several radii are stored so the radius test can compare them
            near = d <= r
            for hazard, mask in (("hurricane", near & eligible), ("hurricane_strength", near & hurricane)):
                sel = pts[mask].assign(dist=d[mask])
                storms = sel.groupby("storm_id").agg(storm_name=("name", "first"), max_wind_kt=("wind_kt", "max"),
                                                    closest_km=("dist", "min"))
                notable = storms.sort_values("max_wind_kt", ascending=False).head(8)
                notable_list = [
                    {"storm": f"{str(row['storm_name']).title()} {sid[-4:]}",
                     "max_wind_kt_nearby": None if pd.isna(row["max_wind_kt"]) else int(row["max_wind_kt"]),
                     "closest_km": round(float(row["closest_km"]))}
                    for sid, row in notable.iterrows()
                ]
                rows.append(_rate_row(hub_id, hazard, r, hp["first_year"], hp["last_year"], len(storms), years, p,
                                      {"notable_storms": notable_list}))
            # Storm-affected days: distinct dates with a tropical-storm-strength storm within the radius.
            dates = sorted(pts.loc[near & eligible, "date"].unique())
            rows.append(_rate_row(hub_id, "hurricane_days", r, hp["first_year"], hp["last_year"], len(dates), years, p,
                                  {"unit": "storm days (UTC dates)", "dates": dates}))
    return rows


def convective_rates(conn, hubs, p) -> list[dict]:
    """Per hub and radius: days per year with at least one tornado, hail or wind report nearby (SPC).

    Days are counted rather than reports, because one storm often produces dozens of reports and the number of
    reports grows with population (more people to report), while the number of affected days does not.
    """
    cp = p["convective"]
    years = cp["last_year"] - cp["first_year"] + 1
    rep = pd.read_sql("SELECT hazard, date, slat, slon, elat, elon, magnitude FROM severe_reports", conn)
    rows = []
    for hazard in ("tornado", "hail", "wind"):
        h = rep[rep["hazard"] == hazard]
        for hub_id, lat, lon in hubs:
            d = point_to_segment_km(lat, lon, h["slat"], h["slon"], h["elat"], h["elon"])
            for r in p["radii_km"]["convective"]:
                dates = sorted(h.loc[d <= r, "date"].unique())
                details = {"unit": "report days"}
                if hazard == "tornado":
                    details["dates"] = dates   # used to combine with measured-gust days
                rows.append(_rate_row(hub_id, hazard, r, cp["first_year"], cp["last_year"], len(dates), years, p,
                                      details))
    return rows


def _rate_row(hub_id, hazard, radius, y0, y1, k, years, p, details) -> dict:
    """One row for hub_event_rates: the rate, its range, and the chance of at least one event in the horizon."""
    lo, hi = poisson_interval(k, years, p["confidence_level"])
    rate = k / years
    h = p["probability_horizon_years"]
    return {"hub_id": hub_id, "hazard": hazard, "radius_km": radius, "first_year": y0, "last_year": y1,
            "events": k, "rate_per_year": rate, "rate_lo": lo, "rate_hi": hi,
            "p_horizon": 1 - math.exp(-rate * h), "horizon_years": h,
            "params_version": get_params()["params_version"], "details_json": json.dumps(details)}


def compute(conn: sqlite3.Connection) -> dict:
    """Recompute all event rates and replace the hub_event_rates table."""
    p = get_params()["event_rates"]
    hubs = conn.execute("SELECT id, lat, lon FROM hubs ORDER BY id").fetchall()
    rows = hurricane_rates(conn, hubs, p) + convective_rates(conn, hubs, p)
    conn.execute("DELETE FROM hub_event_rates")
    conn.executemany(
        "INSERT INTO hub_event_rates VALUES (:hub_id, :hazard, :radius_km, :first_year, :last_year, :events, "
        ":rate_per_year, :rate_lo, :rate_hi, :p_horizon, :horizon_years, :params_version, :details_json)", rows)
    conn.commit()
    return {"rows": len(rows)}


def radius_test(conn: sqlite3.Connection) -> None:
    """Print how stable hub rankings are across radii (Spearman rank correlation), plus a FEMA cross-check.

    This is the data test used to choose the default radii: if rankings barely change between nearby radii,
    the exact radius is not driving the result.
    """
    p = get_params()["event_rates"]
    df = pd.read_sql("SELECT hub_id, hazard, radius_km, rate_per_year FROM hub_event_rates", conn)
    print("\nRadius test: Spearman rank correlation of hub rates between radii")
    for hazard, kind in (("hurricane", "hurricane"), ("hurricane_strength", "hurricane"),
                         ("tornado", "convective"), ("hail", "convective"), ("wind", "convective")):
        piv = df[df["hazard"] == hazard].pivot(index="hub_id", columns="radius_km", values="rate_per_year")
        radii = p["radii_km"][kind]
        pairs = [(radii[i], radii[i + 1]) for i in range(len(radii) - 1)] + [(radii[0], radii[-1])]
        txt = ", ".join(f"{a:g}-{b:g} km: {spearmanr(piv[a], piv[b]).statistic:.2f}" for a, b in pairs)
        print(f"  {hazard:<19} {txt}")

    fema = pd.read_sql(
        "SELECT h.id hub_id, n.hazard, n.afreq FROM hubs h JOIN nri_county n ON n.county_fips = h.county_fips", conn)
    print("\nCross-check vs FEMA NRI annualized frequency (hub counties), Spearman at default radius")
    for ours, theirs, kind in (("tornado", "tornado", "convective"), ("hail", "hail", "convective"),
                               ("wind", "strong_wind", "convective"), ("hurricane", "hurricane", "hurricane")):
        r = p["default_radius_km"][kind]
        a = df[(df.hazard == ours) & (df.radius_km == r)].set_index("hub_id")["rate_per_year"]
        b = fema[fema.hazard == theirs].set_index("hub_id")["afreq"].fillna(0)
        print(f"  {ours:<10} vs FEMA {theirs:<12} rho = {spearmanr(a, b.reindex(a.index)).statistic:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    conn = get_connection()
    print(compute(conn))
    if args.test:
        radius_test(conn)


if __name__ == "__main__":
    main()
