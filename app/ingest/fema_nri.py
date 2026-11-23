"""FEMA National Risk Index (NRI), county level, for all US counties.

Source: FEMA's official NRI Counties feature service on ArcGIS Online (owner
FEMA_NationalRiskIndex). The hazards.fema.gov bulk-download site refused connections
from our environment, so we page through the same table via the ArcGIS REST API.

Why frequency, not "risk score": the NRI hazard *risk* score scales with exposed
property and population, so large urban counties score high on almost every hazard
(e.g. Dallas County scores ~99 for winter weather). For a logistics hub, what matters
is how often a hazard occurs, so scoring uses the national percentile of the NRI
annualized event frequency (AFREQ). Risk score and loss-rate percentile are stored
for reference.
"""

import sqlite3

import pandas as pd

from app.config import get_settings
from app.ingest.common import get_with_retry, http_client, log_ingest

SERVICE_URL = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "National_Risk_Index_Counties/FeatureServer/0/query"
)

# NRI hazard prefix -> our hazard key
HAZARDS = {
    "WNTW": "winter_weather",
    "ISTM": "ice_storm",
    "CWAV": "cold_wave",
    "HRCN": "hurricane",
    "CFLD": "coastal_flood",
    "IFLD": "inland_flood",
    "TRND": "tornado",
    "HAIL": "hail",
    "SWND": "strong_wind",
    "HWAV": "heat_wave",
}
FIELDS = ["STCOFIPS", "COUNTY", "STATEABBRV", "NRI_VER"] + [
    f"{p}_{s}" for p in HAZARDS for s in ("AFREQ", "RISKS", "RISKR", "ALR_NPCTL")
]
PAGE_SIZE = 2000


def raw_cache_path():
    return get_settings().raw_dir / "nri_counties.csv"


def download(refresh: bool = False) -> pd.DataFrame:
    cache = raw_cache_path()
    if cache.exists() and not refresh:
        return pd.read_csv(cache, dtype={"STCOFIPS": str})
    rows, offset = [], 0
    with http_client() as c:
        while True:
            r = get_with_retry(c, SERVICE_URL, {
                "where": "1=1",
                "outFields": ",".join(FIELDS),
                "returnGeometry": "false",
                "orderByFields": "STCOFIPS",
                "resultOffset": offset,
                "resultRecordCount": PAGE_SIZE,
                "f": "json",
            }).json()
            if "error" in r:
                raise RuntimeError(f"NRI query error: {r['error']}")
            feats = r.get("features", [])
            rows += [f["attributes"] for f in feats]
            if len(feats) < PAGE_SIZE and not r.get("exceededTransferLimit"):
                break
            offset += len(feats)
    df = pd.DataFrame(rows)
    df["STCOFIPS"] = df["STCOFIPS"].astype(str).str.zfill(5)
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, index=False)
    return df


def to_long(df: pd.DataFrame) -> pd.DataFrame:
    """One row per county x hazard, with a national frequency percentile (NULL frequency = 0)."""
    parts = []
    for prefix, key in HAZARDS.items():
        afreq = df[f"{prefix}_AFREQ"]
        part = pd.DataFrame({
            "county_fips": df["STCOFIPS"],
            "hazard": key,
            "afreq": afreq,
            "freq_pctl": afreq.fillna(0).rank(pct=True, method="average") * 100,
            "alr_npctl": df[f"{prefix}_ALR_NPCTL"],
            "risk_score": df[f"{prefix}_RISKS"],
            "risk_rating": df[f"{prefix}_RISKR"],
            "nri_version": df["NRI_VER"],
        })
        # A hazard that never occurs in a county gets percentile 0, not the tie-average rank.
        part.loc[afreq.fillna(0) == 0, "freq_pctl"] = 0.0
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def ingest(conn: sqlite3.Connection, refresh: bool = False) -> dict:
    df = download(refresh=refresh)
    long = to_long(df)
    conn.execute("DELETE FROM nri_county")
    conn.executemany(
        "INSERT INTO nri_county (county_fips, hazard, afreq, freq_pctl, alr_npctl, risk_score, risk_rating, nri_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [tuple(None if pd.isna(v) else v for v in row) for row in long.itertuples(index=False)],
    )
    conn.commit()
    version = df["NRI_VER"].dropna().iloc[0] if df["NRI_VER"].notna().any() else None
    log_ingest(conn, "fema_nri", "ok", rows=len(long),
               params={"counties": len(df), "hazards": list(HAZARDS.values())}, note=f"NRI version: {version}")
    return {"counties": len(df), "rows": len(long), "version": version}
