"""NOAA Storm Prediction Center (SPC) severe-weather database: tornado, hail, wind reports.

Files are pinned in params.yaml. One row per report with start (and for tornadoes, end)
coordinates and magnitude. We keep reports that (a) fall in the configured years,
(b) meet the magnitude thresholds and (c) lie within the largest tested radius of any
hub. The full national files stay cached in data/raw/spc.

Known limitations (documented): reports cluster where people live (population bias),
and tornadoes with an unknown rating (-9) are not counted as EF1+.
"""

import io
import sqlite3
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import get_settings
from app.geo import point_to_segment_km
from app.ingest.common import get_with_retry, log_ingest
from app.scoring.params import get_params

import httpx

# The SPC web server rejects some non-browser user agents (HTTP 403).
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def _download(url: str, refresh: bool) -> Path:
    """Download one national report file (or use the cache) and return its local path."""
    path = get_settings().raw_dir / "spc" / url.rsplit("/", 1)[-1]
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=300, follow_redirects=True, headers={"User-Agent": BROWSER_UA}) as c:
            path.write_bytes(get_with_retry(c, url).content)
    return path


def _read(path: Path) -> pd.DataFrame:
    """Read a report file; some SPC files are zipped CSVs, others plain CSVs."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            return pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), low_memory=False)
    return pd.read_csv(path, low_memory=False)


def ingest(conn: sqlite3.Connection, refresh: bool = False) -> dict:
    """Replace severe_reports with qualifying reports near any hub, for each hazard file."""
    p = get_params()["event_rates"]
    cp = p["convective"]
    max_radius = max(p["radii_km"]["convective"])
    hubs = conn.execute("SELECT lat, lon FROM hubs").fetchall()

    conn.execute("DELETE FROM severe_reports")
    summary = {}
    for hazard, url in cp["source_files"].items():
        df = _read(_download(url, refresh))
        df = df[(df["yr"] >= cp["first_year"]) & (df["yr"] <= cp["last_year"])]
        df = df[df["mag"] >= cp["min_magnitude"][hazard]]
        # Tornado end points of 0 mean "not recorded"; for those, and for hail and wind, only the start counts.
        elat = df["elat"].where(df["elat"] != 0) if hazard == "tornado" else pd.Series(np.nan, index=df.index)
        elon = df["elon"].where(df["elon"] != 0) if hazard == "tornado" else pd.Series(np.nan, index=df.index)
        # Keep a report if it lies within the largest radius of at least one hub (the rest are never used).
        near = np.zeros(len(df), dtype=bool)
        for lat, lon in hubs:
            near |= point_to_segment_km(lat, lon, df["slat"], df["slon"], elat, elon) <= max_radius
        kept = df[near]
        conn.executemany(
            "INSERT INTO severe_reports (hazard, date, slat, slon, elat, elon, magnitude) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (hazard, d, float(a), float(b), None if pd.isna(c) else float(c), None if pd.isna(e) else float(e), float(m))
                for d, a, b, c, e, m in zip(kept["date"], kept["slat"], kept["slon"], elat[near], elon[near], kept["mag"])
            ],
        )
        summary[hazard] = {"qualifying_national": int(len(df)), "kept_near_hubs": int(near.sum())}
    conn.commit()
    log_ingest(conn, "spc_reports", "ok", rows=sum(v["kept_near_hubs"] for v in summary.values()),
               params={"files": cp["source_files"], "years": [cp["first_year"], cp["last_year"]],
                       "min_magnitude": cp["min_magnitude"], "max_radius_km": max_radius},
               note=str(summary))
    return summary
