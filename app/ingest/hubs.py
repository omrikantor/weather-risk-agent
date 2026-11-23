"""Load the hub list from data/hubs.yaml; fill missing county FIPS via the FCC Area API."""

import sqlite3
import time
from pathlib import Path

import yaml

from app.config import get_settings
from app.ingest.common import get_with_retry, http_client, log_ingest

FCC_AREA_URL = "https://geo.fcc.gov/api/census/area"
REQUIRED = ("id", "name", "state", "region", "lat", "lon")
REGIONS = {"Midwest", "South", "West", "Northeast"}   # US Census Bureau regions


def hubs_file() -> Path:
    """The hub list is configuration, not code: a client's real sites would replace this file."""
    return get_settings().data_dir / "hubs.yaml"


def load_hubs(path: Path | None = None) -> list[dict]:
    """Read and validate hubs.yaml: required fields present, a known region, and unique ids."""
    data = yaml.safe_load((path or hubs_file()).read_text(encoding="utf-8"))
    hubs = data["hubs"]
    ids = set()
    for h in hubs:
        missing = [k for k in REQUIRED if k not in h]
        if missing:
            raise ValueError(f"hub {h.get('id')} missing fields {missing}")
        if h["region"] not in REGIONS:
            raise ValueError(f"hub {h['id']} has unknown region {h['region']}")
        if h["id"] in ids:
            raise ValueError(f"duplicate hub id {h['id']}")
        ids.add(h["id"])
    return hubs


def lookup_county(lat: float, lon: float) -> tuple[str, str, str]:
    """Return (county_fips, county_name, state_code) for a point using the FCC Area API."""
    with http_client(timeout=30) as c:
        r = get_with_retry(c, FCC_AREA_URL, {"lat": lat, "lon": lon, "format": "json"}, base_delay=2)
    results = r.json().get("results", [])
    if not results:
        raise ValueError(f"FCC Area API returned no county for {lat},{lon}")
    first = results[0]
    return first["county_fips"], first["county_name"], first["state_code"]


def fill_missing_fips(path: Path | None = None) -> int:
    """Add county_fips/county_name to hubs.yaml entries that lack them. Returns how many were filled."""
    path = path or hubs_file()
    text = path.read_text(encoding="utf-8")
    hubs = load_hubs(path)
    filled = 0
    for h in hubs:
        if h.get("county_fips"):
            continue
        fips, cname, state = lookup_county(h["lat"], h["lon"])
        # Safety check: a coordinate typo that lands in another state stops the build instead of silently
        # linking the hub to the wrong county.
        if state != h["state"]:
            raise ValueError(f"hub {h['id']}: point resolves to state {state}, expected {h['state']}")
        # Rewrite the single-line flow mapping for this hub in place (keeps comments/layout).
        old_line_start = "  - {id: " + h["id"] + ","
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith(old_line_start):
                lines[i] = line.rstrip()[:-1] + f", county_fips: '{fips}', county_name: \"{cname}\"}}"
                break
        text = "\n".join(lines) + "\n"
        filled += 1
        time.sleep(0.3)
    if filled:
        path.write_text(text, encoding="utf-8")
    return filled


def load_hubs_into_db(conn: sqlite3.Connection, hubs: list[dict]) -> int:
    """Insert or update every hub from hubs.yaml and remove hubs that are no longer listed."""
    for h in hubs:
        if not h.get("county_fips"):
            raise ValueError(f"hub {h['id']} has no county_fips; run fill_missing_fips first")
    # Upsert (a plain DELETE would violate the daily_weather foreign key on rebuilds).
    conn.executemany(
        "INSERT INTO hubs (id, name, state, region, lat, lon, county_fips, county_name) "
        "VALUES (:id, :name, :state, :region, :lat, :lon, :county_fips, :county_name) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, state=excluded.state, region=excluded.region, "
        "lat=excluded.lat, lon=excluded.lon, county_fips=excluded.county_fips, county_name=excluded.county_name",
        [{**h, "county_fips": str(h["county_fips"]).zfill(5), "county_name": h.get("county_name")} for h in hubs],
    )
    # Hubs removed from hubs.yaml are removed from the database together with their weather rows.
    keep = [h["id"] for h in hubs]
    placeholders = ",".join("?" * len(keep))
    conn.execute(f"DELETE FROM daily_weather WHERE hub_id NOT IN ({placeholders})", keep)
    conn.execute(f"DELETE FROM hubs WHERE id NOT IN ({placeholders})", keep)
    conn.commit()
    log_ingest(conn, "hubs.yaml", "ok", rows=len(hubs))
    return len(hubs)
