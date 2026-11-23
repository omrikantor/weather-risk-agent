"""Daily weather history per hub from the Open-Meteo Historical (archive) API.

Data: ERA5-based reanalysis on a ~9-25 km grid (not station observations).
The free API is for non-commercial use; a production deployment would use a paid plan.
"""

import sqlite3
import time
from datetime import date, timedelta

from app.config import get_settings
from app.ingest.common import (
    RateLimitError,
    get_with_retry,
    http_client,
    log_ingest,
    read_json_cache,
    write_json_cache,
)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "snowfall_sum",
    "wind_gusts_10m_max",
    "weather_code",
]
COLUMN_MAP = {
    "temperature_2m_max": "tmax_c",
    "temperature_2m_min": "tmin_c",
    "precipitation_sum": "precip_mm",
    "snowfall_sum": "snowfall_cm",
    "wind_gusts_10m_max": "wind_gust_kmh",
    "weather_code": "weather_code",
}

HISTORY_START = date(2016, 1, 1)
HISTORY_END = date(2025, 12, 31)
ARCHIVE_LAG_DAYS = 5          # the archive trails real time by a few days
PAUSE_BETWEEN_REQUESTS_S = 3  # be polite to the free API


def fetch_hub(hub: dict, start: date, end: date, label: str, refresh: bool = False) -> dict:
    """Fetch (or read cached) daily data for one hub and period. Returns the raw API JSON."""
    cache = get_settings().raw_dir / "open_meteo" / f"{hub['id']}_{label}.json"
    if not refresh:
        cached = read_json_cache(cache)
        if cached is not None:
            return cached
    params = {
        "latitude": hub["lat"],
        "longitude": hub["lon"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "timezone": "auto",
        "daily": ",".join(DAILY_VARS),
    }
    with http_client() as c:
        data = get_with_retry(c, ARCHIVE_URL, params).json()
    if "daily" not in data:
        raise RuntimeError(f"Open-Meteo response for {hub['id']} has no daily data: {str(data)[:200]}")
    write_json_cache(cache, data)
    time.sleep(PAUSE_BETWEEN_REQUESTS_S)
    return data


def to_rows(hub_id: str, data: dict) -> list[tuple]:
    daily = data["daily"]
    cols = list(COLUMN_MAP)
    rows = []
    for i, day in enumerate(daily["time"]):
        vals = [daily[k][i] for k in cols]
        if vals[cols.index("weather_code")] is not None:
            vals[cols.index("weather_code")] = int(vals[cols.index("weather_code")])
        rows.append((hub_id, day, *vals))
    return rows


def store_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO daily_weather "
        "(hub_id, date, tmax_c, tmin_c, precip_mm, snowfall_cm, wind_gust_kmh, weather_code) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def ingest(conn: sqlite3.Connection, hubs: list[dict], refresh: bool = False,
           include_current_year: bool = True) -> dict:
    """Ingest history (2016-2025) and, optionally, the current year to date. Resumable via cache."""
    today = date.today()
    current_end = today - timedelta(days=ARCHIVE_LAG_DAYS)
    current_start = date(today.year, 1, 1)
    summary = {"hubs_ok": 0, "rows": 0, "missing_values": 0, "stopped_early": None}

    for n, hub in enumerate(hubs, 1):
        periods = [(HISTORY_START, HISTORY_END, "history")]
        if include_current_year and current_end > current_start:
            # Label by year so a refresh in a new year doesn't reuse an old cache file.
            periods.append((current_start, current_end, f"ytd{today.year}"))
        try:
            for start, end, label in periods:
                data = fetch_hub(hub, start, end, label, refresh=refresh and label != "history")
                rows = to_rows(hub["id"], data)
                store_rows(conn, rows)
                summary["rows"] += len(rows)
                summary["missing_values"] += sum(v is None for r in rows for v in r[2:])
        except RateLimitError as e:
            summary["stopped_early"] = f"rate limit at hub {n}/{len(hubs)} ({hub['id']}): {e}"
            log_ingest(conn, "open_meteo", "partial", rows=summary["rows"], note=summary["stopped_early"])
            print(f"  ! {summary['stopped_early']}\n  Re-run later; cached hubs are skipped.")
            return summary
        summary["hubs_ok"] += 1
        print(f"  [{n}/{len(hubs)}] {hub['id']}: ok")

    log_ingest(conn, "open_meteo", "ok", rows=summary["rows"],
               params={"history": f"{HISTORY_START}..{HISTORY_END}", "daily": DAILY_VARS},
               note=f"missing values: {summary['missing_values']}")
    return summary
