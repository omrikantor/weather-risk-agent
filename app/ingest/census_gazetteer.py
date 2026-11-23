"""US Census Bureau Gazetteer files: every US place (city/town/CDP) and county centroid.

Used to locate places that are not hubs (e.g. "Tampa, FL") without any live API call.
A place's county is approximated as the nearest county centroid in the same state.
"""

import io
import re
import sqlite3
import zipfile

import numpy as np
import pandas as pd

from app.config import get_settings
from app.db import create_schema
from app.ingest.common import get_with_retry, http_client, log_ingest

YEAR = 2026
BASE = f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{YEAR}_Gazetteer"
PLACES_URL = f"{BASE}/{YEAR}_Gaz_place_national.zip"
COUNTIES_URL = f"{BASE}/{YEAR}_Gaz_counties_national.zip"

# Census place-name suffixes ("Tampa city", "Brandon CDP") -> stripped for matching.
SUFFIX_RE = re.compile(
    r"\s+(city and borough|consolidated government \(balance\)|metropolitan government \(balance\)|"
    r"unified government \(balance\)|metro government \(balance\)|\(balance\)|city|town|village|"
    r"borough|CDP|municipality|comunidad|zona urbana|urban county|plantation|corporation)$",
    re.IGNORECASE,
)


def _download_table(url: str, cache_name: str, refresh: bool) -> pd.DataFrame:
    cache = get_settings().raw_dir / cache_name
    if refresh or not cache.exists():
        with http_client() as c:
            content = get_with_retry(c, url).content
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(content)
    with zipfile.ZipFile(cache) as z:
        name = next(n for n in z.namelist() if n.endswith(".txt"))
        raw = z.read(name).decode("latin-1")
    sep = "|" if raw.split("\n", 1)[0].count("|") > raw.split("\n", 1)[0].count("\t") else "\t"
    df = pd.read_csv(io.StringIO(raw), sep=sep, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return df


def clean_place_name(name: str) -> str:
    """'Tampa city' -> 'Tampa'; 'Nashville-Davidson metropolitan government (balance)' -> 'Nashville-Davidson'."""
    prev = None
    while prev != name:
        prev, name = name, SUFFIX_RE.sub("", name).strip()
    return name


def nearest_county(places: pd.DataFrame, counties: pd.DataFrame) -> pd.Series:
    """For each place, the nearest county centroid within the same state (equirectangular distance)."""
    out = pd.Series(index=places.index, dtype=object)
    for state, grp in places.groupby("state"):
        cs = counties[counties["state"] == state]
        if cs.empty:
            continue
        plat, plon = np.radians(grp["lat"].to_numpy())[:, None], np.radians(grp["lon"].to_numpy())[:, None]
        clat, clon = np.radians(cs["lat"].to_numpy())[None, :], np.radians(cs["lon"].to_numpy())[None, :]
        x = (clon - plon) * np.cos((plat + clat) / 2)
        y = clat - plat
        idx = np.argmin(x * x + y * y, axis=1)
        out.loc[grp.index] = cs["county_fips"].to_numpy()[idx]
    return out


def ingest(conn: sqlite3.Connection, refresh: bool = False) -> dict:
    cdf = _download_table(COUNTIES_URL, f"{YEAR}_Gaz_counties_national.zip", refresh)
    counties = pd.DataFrame({
        "county_fips": cdf["GEOID"].str.zfill(5),
        "name": cdf["NAME"],
        "state": cdf["USPS"],
        "lat": cdf["INTPTLAT"].astype(float),
        "lon": cdf["INTPTLONG"].astype(float),
    })

    pdf = _download_table(PLACES_URL, f"{YEAR}_Gaz_place_national.zip", refresh)
    places = pd.DataFrame({
        "geoid": pdf["GEOID"],
        "full_name": pdf["NAME"],
        "state": pdf["USPS"],
        "lat": pdf["INTPTLAT"].astype(float),
        "lon": pdf["INTPTLONG"].astype(float),
        "aland_sqmi": pd.to_numeric(pdf["ALAND_SQMI"], errors="coerce"),
    })
    places["name"] = places["full_name"].map(clean_place_name)
    places["county_fips"] = nearest_county(places, counties)

    conn.execute("DELETE FROM counties")
    # Recreate places so schema changes (new columns) apply to existing databases.
    conn.execute("DROP TABLE IF EXISTS places")
    create_schema(conn)
    conn.executemany("INSERT INTO counties VALUES (?, ?, ?, ?, ?)", counties.itertuples(index=False))
    conn.executemany(
        "INSERT INTO places (geoid, name, full_name, state, lat, lon, aland_sqmi, county_fips) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [tuple(None if pd.isna(v) else v for v in row) for row in
         places[["geoid", "name", "full_name", "state", "lat", "lon", "aland_sqmi", "county_fips"]]
         .itertuples(index=False)],
    )
    conn.commit()
    log_ingest(conn, "census_gazetteer", "ok", rows=len(places) + len(counties),
               params={"year": YEAR}, note=f"places={len(places)}, counties={len(counties)}")
    return {"places": len(places), "counties": len(counties)}
