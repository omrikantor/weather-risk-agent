"""Seasonal (climatological) outlook per hub x hazard x calendar month.

For each month: in how many of the years 2006-2025 the hub had at least one NWS warning day for
the hazard (with an exact binomial range), the average number of warning days, and the expected
severe disruption days (average warning days x the hazard's FEMA severity share). Hurricanes also
get a long-record frequency from HURDAT2 storm days within 150 km, 1950-2025.

This is historical frequency ("how often it happened in this month"), not a forecast.

    python -m app.scoring.seasonal
"""

import json
import sqlite3
from collections import defaultdict

import pandas as pd

from app.db import create_schema, get_connection
from app.scoring import metrics as m
from app.scoring.engine import HAZARDS
from app.scoring.params import get_params


def hub_warning_dates(conn: sqlite3.Connection, hub_id: str) -> dict[str, set[str]]:
    wp = get_params()["warnings"]
    ugc = conn.execute("SELECT ugc, timezone FROM hub_ugc WHERE hub_id=?", (hub_id,)).fetchall()
    codes = [u[0] for u in ugc] + [f"POINT:{hub_id}"]
    w = pd.read_sql(f"SELECT phenomena, issue, expire FROM nws_warnings WHERE ugc IN ({','.join('?' * len(codes))})",
                    conn, params=codes)
    return m.warning_days(w, ugc[0][1] if ugc else "UTC", wp["phenomena"], (wp["first_year"], wp["last_year"]))


def monthly_rows(hub_id: str, dates: dict[str, set[str]], shares: dict[str, float],
                 storm_dates: list[str], years: tuple[int, int], storm_years: tuple[int, int],
                 level: float) -> list[tuple]:
    """Pure function: rows for hub_monthly from warning dates per hazard."""
    n_years = years[1] - years[0] + 1
    n_storm_years = storm_years[1] - storm_years[0] + 1
    storm_by_month = defaultdict(set)
    for d in storm_dates:
        if storm_years[0] <= int(d[:4]) <= storm_years[1]:
            storm_by_month[int(d[5:7])].add(d[:4])
    rows = []
    for hazard in HAZARDS:
        by_month_year = defaultdict(lambda: defaultdict(int))
        for d in dates.get(hazard, ()):
            by_month_year[int(d[5:7])][d[:4]] += 1
        for month in range(1, 13):
            k = len(by_month_year[month])
            total_days = sum(by_month_year[month].values())
            ci = m.share_with_interval(k, n_years, level)
            mean_days = total_days / n_years
            longrecord = len(storm_by_month[month]) / n_storm_years if hazard == "hurricane" else None
            rows.append((hub_id, hazard, month, n_years, k, ci["share"], ci["lo"], ci["hi"], mean_days,
                         mean_days * shares.get(hazard, 0.0), longrecord))
    return rows


def compute(conn: sqlite3.Connection) -> dict:
    p = get_params()
    wp, hp = p["warnings"], p["event_rates"]["hurricane"]
    radius = p["event_rates"]["default_radius_km"]["hurricane"]
    details = json.loads(conn.execute("SELECT details_json FROM risk_scores ORDER BY id DESC LIMIT 1").fetchone()[0])
    shares = {h: details["hazards"][h]["severity_share"]["share"] for h in HAZARDS}
    create_schema(conn)
    conn.execute("DELETE FROM hub_monthly")
    total = 0
    for (hub_id,) in conn.execute("SELECT id FROM hubs ORDER BY id").fetchall():
        dates = hub_warning_dates(conn, hub_id)
        row = conn.execute("SELECT details_json FROM hub_event_rates WHERE hub_id=? AND hazard='hurricane_days' "
                           "AND radius_km=?", (hub_id, radius)).fetchone()
        storm_dates = json.loads(row[0]).get("dates", []) if row else []
        rows = monthly_rows(hub_id, dates, shares, storm_dates, (wp["first_year"], wp["last_year"]),
                            (hp["first_year"], hp["last_year"]), p["event_rates"]["confidence_level"])
        conn.executemany("INSERT INTO hub_monthly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        total += len(rows)
    conn.commit()
    return {"rows": total}


if __name__ == "__main__":
    print(compute(get_connection()))
