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


MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MIN_DAYS_FOR_SEASON = 5          # fewer warning days than this in 20 years: too few to describe a season
SEASON_COVERAGE = 0.8            # the season window covers >= 80% of warning days


def season_profile(dates: set[str], n_years: int) -> dict | None:
    """When a hazard happens and what its events look like, from its warning dates (pure function).

    - window: the shortest run of consecutive months (wrapping Dec->Jan) holding >= 80% of warning days
    - typical range: 10th-90th percentile dates of warning days inside that window
    - events: runs of consecutive warning days
    """
    if len(dates) < MIN_DAYS_FOR_SEASON:
        return None
    ds = sorted(pd.to_datetime(list(dates)))
    counts = [0] * 12
    for d in ds:
        counts[d.month - 1] += 1
    total = len(ds)
    best = None
    for length in range(1, 13):
        for start in range(12):
            s = sum(counts[(start + i) % 12] for i in range(length))
            if s >= SEASON_COVERAGE * total and (best is None or s > best[2]):
                best = (start, length, s)
        if best:
            break
    start, length, covered = best
    months = [(start + i) % 12 for i in range(length)]
    # Typical date range: day offset from the window start, so a Nov-Mar window doesn't break at New Year.
    start_doy = pd.Timestamp(2001, start + 1, 1).day_of_year
    offsets = sorted(((d.replace(year=2001) if not (d.month == 2 and d.day == 29) else d.replace(year=2001, day=28))
                      .day_of_year - start_doy) % 365 for d in ds if d.month - 1 in months)
    lo = offsets[int(0.1 * (len(offsets) - 1))]
    hi = offsets[int(round(0.9 * (len(offsets) - 1)))]
    to_md = lambda off: (pd.Timestamp(2001, 1, 1) + pd.Timedelta(days=(start_doy - 1 + off) % 365)).strftime("%b %d")  # noqa: E731
    # Events: runs of consecutive days.
    events, run_start, prev = [], ds[0], ds[0]
    for d in ds[1:]:
        if (d - prev).days > 1:
            events.append((run_start, (prev - run_start).days + 1))
            run_start = d
        prev = d
    events.append((run_start, (prev - run_start).days + 1))
    lengths = sorted(n for _, n in events)
    longest = max(events, key=lambda e: (e[1], e[0]))
    return {
        "usual_months": f"{MONTH_ABBR[months[0]]}-{MONTH_ABBR[months[-1]]}" if length > 1 else MONTH_ABBR[months[0]],
        "share_of_warning_days_in_usual_months": round(covered / total, 2),
        "peak_month": MONTH_ABBR[max(range(12), key=lambda i: (counts[i], -i))],
        "typical_dates": f"{to_md(lo)} - {to_md(hi)}",
        "events_per_year": round(len(events) / n_years, 1),
        "typical_event_days": int(lengths[len(lengths) // 2]),
        "longest_event_days": int(longest[1]),
        "longest_event_start": longest[0].strftime("%Y-%m-%d"),
        "warning_days_observed": total,
    }


def compute(conn: sqlite3.Connection) -> dict:
    p = get_params()
    wp, hp = p["warnings"], p["event_rates"]["hurricane"]
    radius = p["event_rates"]["default_radius_km"]["hurricane"]
    details = json.loads(conn.execute("SELECT details_json FROM risk_scores ORDER BY id DESC LIMIT 1").fetchone()[0])
    shares = {h: details["hazards"][h]["severity_share"]["share"] for h in HAZARDS}
    create_schema(conn)
    conn.execute("DELETE FROM hub_monthly")
    conn.execute("DELETE FROM hub_hazard_season")
    total = 0
    n_years = wp["last_year"] - wp["first_year"] + 1
    for (hub_id,) in conn.execute("SELECT id FROM hubs ORDER BY id").fetchall():
        dates = hub_warning_dates(conn, hub_id)
        conn.executemany("INSERT INTO hub_hazard_season VALUES (?, ?, ?)",
                         [(hub_id, h, json.dumps(season_profile(dates.get(h, set()), n_years))) for h in HAZARDS])
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
