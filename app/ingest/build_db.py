"""Build (or rebuild) the SQLite database from public sources.

Usage:
    python -m app.ingest.build_db                    # all steps, using cached raw data where present
    python -m app.ingest.build_db --refresh          # re-download sources
    python -m app.ingest.build_db --skip-weather     # all steps except Open-Meteo
    python -m app.ingest.build_db --only hurdat2 spc rates   # selected steps

Steps (in order): hubs, nri, census, weather, hurdat2, spc, ghcn, rates
"""

import argparse

from app.config import get_settings
from app.db import create_schema, get_connection
from app.ingest import census_gazetteer, fema_nri, ghcn, hubs, hurdat2, open_meteo, spc_reports
from app.scoring import event_rates

STEPS = ["hubs", "nri", "census", "weather", "hurdat2", "spc", "ghcn", "rates"]


def summarize(conn) -> None:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print("\nDatabase summary")
    print(f"  hubs:            {q('SELECT COUNT(*) FROM hubs')}")
    print(f"  daily_weather:   {q('SELECT COUNT(*) FROM daily_weather')} rows "
          f"({q('SELECT COUNT(DISTINCT hub_id) FROM daily_weather')} hubs), "
          f"{q('SELECT MIN(date) FROM daily_weather')} .. {q('SELECT MAX(date) FROM daily_weather')}")
    print(f"  nri_county:      {q('SELECT COUNT(*) FROM nri_county')} rows "
          f"({q('SELECT COUNT(DISTINCT county_fips) FROM nri_county')} counties)")
    print(f"  counties/places: {q('SELECT COUNT(*) FROM counties')} / {q('SELECT COUNT(*) FROM places')}")
    print(f"  storm_tracks:    {q('SELECT COUNT(*) FROM storm_tracks')} fixes "
          f"({q('SELECT COUNT(DISTINCT storm_id) FROM storm_tracks')} storms)")
    print(f"  severe_reports:  {q('SELECT COUNT(*) FROM severe_reports')}")
    print(f"  hub_stations:    {q('SELECT COUNT(*) FROM hub_stations')} hubs, "
          f"station_daily {q('SELECT COUNT(*) FROM station_daily')} rows")
    print(f"  hub_event_rates: {q('SELECT COUNT(*) FROM hub_event_rates')}")
    missing_wx = [r[0] for r in conn.execute(
        "SELECT h.id FROM hubs h LEFT JOIN daily_weather w ON w.hub_id = h.id GROUP BY h.id HAVING COUNT(w.date) = 0")]
    missing_st = [r[0] for r in conn.execute(
        "SELECT h.id FROM hubs h LEFT JOIN hub_stations s ON s.hub_id = h.id WHERE s.station_id IS NULL")]
    if missing_wx:
        print(f"  ! hubs without Open-Meteo weather: {missing_wx}")
    if missing_st:
        print(f"  ! hubs without a qualifying station: {missing_st}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="re-download sources instead of using the cache")
    ap.add_argument("--skip-weather", action="store_true", help="skip the Open-Meteo step")
    ap.add_argument("--only", nargs="+", choices=STEPS, help="run only these steps")
    args = ap.parse_args()
    steps = args.only or [s for s in STEPS if not (args.skip_weather and s == "weather")]

    print(f"Building {get_settings().db_file} - steps: {', '.join(steps)}")
    conn = get_connection()
    create_schema(conn)
    hub_list = None

    if "hubs" in steps:
        print("Hubs")
        filled = hubs.fill_missing_fips()
        if filled:
            print(f"  filled county FIPS for {filled} hubs (FCC Area API)")
        hub_list = hubs.load_hubs()
        print(f"  {hubs.load_hubs_into_db(conn, hub_list)} hubs loaded")
    if "nri" in steps:
        print("FEMA National Risk Index")
        print(f"  {fema_nri.ingest(conn, refresh=args.refresh)}")
    if "census" in steps:
        print("Census Gazetteer (places + counties)")
        print(f"  {census_gazetteer.ingest(conn, refresh=args.refresh)}")
    if "weather" in steps:
        print("Open-Meteo daily history")
        print(f"  {open_meteo.ingest(conn, hub_list or hubs.load_hubs(), refresh=args.refresh)}")
    if "hurdat2" in steps:
        print("NOAA HURDAT2 storm tracks")
        print(f"  {hurdat2.ingest(conn, refresh=args.refresh)}")
    if "spc" in steps:
        print("NOAA SPC severe reports")
        print(f"  {spc_reports.ingest(conn, refresh=args.refresh)}")
    if "ghcn" in steps:
        print("NOAA GHCN-Daily stations")
        print(f"  {ghcn.ingest(conn, refresh=args.refresh)}")
    if "rates" in steps:
        print("Hub event rates")
        print(f"  {event_rates.compute(conn)}")

    summarize(conn)
    conn.close()


if __name__ == "__main__":
    main()
