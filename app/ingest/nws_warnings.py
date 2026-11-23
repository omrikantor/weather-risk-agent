"""NWS warnings per hub (county + forecast zone) and FEMA major-disaster declarations.

- Each hub's NWS county and forecast-zone codes (UGC) and time zone come from the NWS
  points API (api.weather.gov), once.
- Warnings come from the Iowa Environmental Mesonet VTEC archive (by UGC); only warnings
  (significance W) for the mapped phenomena are stored.
- Declarations come from OpenFEMA; only major disasters (DR) with a mapped incident type.
"""

import json
import sqlite3
import time

import pandas as pd

from app.config import get_settings
from app.ingest.common import get_with_retry, http_client, log_ingest
from app.scoring.params import get_params


def _cache(name: str):
    p = get_settings().raw_dir / "nws" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def hub_geography(conn: sqlite3.Connection, refresh: bool = False) -> int:
    wp = get_params()["warnings"]
    rows = []
    with http_client(timeout=60) as c:
        for hub_id, lat, lon in conn.execute("SELECT id, lat, lon FROM hubs ORDER BY id").fetchall():
            cache = _cache(f"points_{hub_id}.json")
            if refresh or not cache.exists():
                r = get_with_retry(c, wp["points_url"].format(lat=round(lat, 4), lon=round(lon, 4)))
                cache.write_text(r.text, encoding="utf-8")
                time.sleep(0.5)
            props = json.loads(cache.read_text(encoding="utf-8"))["properties"]
            tz = props["timeZone"]
            rows.append((hub_id, props["county"].rsplit("/", 1)[-1], "county", tz))
            rows.append((hub_id, props["forecastZone"].rsplit("/", 1)[-1], "zone", tz))
    conn.execute("DELETE FROM hub_ugc")
    conn.executemany("INSERT OR REPLACE INTO hub_ugc VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    return len(rows)


def ingest_warnings(conn: sqlite3.Connection, refresh: bool = False) -> dict:
    """Zone/county products by UGC; polygon products by hub point (stored with ugc = 'POINT:<hub_id>')."""
    wp = get_params()["warnings"]
    mapped = {code for codes in wp["phenomena"].values() for code in codes}
    polygon = set(wp["polygon_phenomena"])
    ugcs = [r[0] for r in conn.execute("SELECT DISTINCT ugc FROM hub_ugc ORDER BY ugc")]
    conn.execute("DELETE FROM nws_warnings")
    total, point_total = 0, 0
    with http_client(timeout=180) as c:
        for hub_id, lat, lon in conn.execute("SELECT id, lat, lon FROM hubs ORDER BY id").fetchall():
            cache = _cache(f"sbw_{hub_id}_{wp['first_year']}_{wp['last_year']}.json")
            if refresh or not cache.exists():
                r = get_with_retry(c, wp["polygon_url"], {"lat": lat, "lon": lon, "sdate": f"{wp['first_year']}-01-01",
                                                          "edate": f"{wp['last_year']}-12-31"})
                cache.write_text(r.text, encoding="utf-8")
                time.sleep(0.5)
            events = json.loads(cache.read_text(encoding="utf-8")).get("sbws", [])
            rows = [(f"POINT:{hub_id}", e.get("wfo"), e["phenomena"], e["significance"], e.get("name"),
                     e.get("eventid"), e["issue"], e["expire"])
                    for e in events if e.get("significance") == wp["significance"]
                    and e.get("phenomena") in (mapped & polygon) and e.get("issue") and e.get("expire")]
            conn.executemany("INSERT OR IGNORE INTO nws_warnings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            point_total += len(rows)
    wanted = mapped - polygon
    with http_client(timeout=180) as c:
        for ugc in ugcs:
            cache = _cache(f"vtec_{ugc}_{wp['first_year']}_{wp['last_year']}.json")
            if refresh or not cache.exists():
                r = get_with_retry(c, wp["archive_url"], {"ugc": ugc, "sdate": f"{wp['first_year']}-01-01",
                                                          "edate": f"{wp['last_year']}-12-31"})
                cache.write_text(r.text, encoding="utf-8")
                time.sleep(0.5)
            events = json.loads(cache.read_text(encoding="utf-8")).get("events", [])
            rows = [(ugc, e.get("wfo"), e["phenomena"], e["significance"], e.get("name"), e.get("eventid"),
                     e["issue"], e["expire"])
                    for e in events if e.get("significance") == wp["significance"] and e.get("phenomena") in wanted
                    and e.get("issue") and e.get("expire")]
            conn.executemany("INSERT OR IGNORE INTO nws_warnings VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            total += len(rows)
    conn.commit()
    log_ingest(conn, "nws_warnings", "ok", rows=total + point_total,
               params={"years": [wp["first_year"], wp["last_year"]], "phenomena": wp["phenomena"],
                       "polygon_phenomena": wp["polygon_phenomena"]},
               note=f"ugcs={len(ugcs)}, zone/county warnings={total}, polygon warnings at hub points={point_total}")
    return {"ugcs": len(ugcs), "zone_county_warnings": total, "polygon_warnings_at_hubs": point_total}


def ingest_declarations(conn: sqlite3.Connection, refresh: bool = False) -> dict:
    dp = get_params()["declarations"]
    cache = get_settings().raw_dir / f"fema_declarations_{dp['first_year']}plus.csv"
    if refresh or not cache.exists():
        sel = ("disasterNumber,declarationType,declarationTitle,incidentType,incidentBeginDate,incidentEndDate,"
               "fipsStateCode,fipsCountyCode")
        rows, skip = [], 0
        with http_client(timeout=180) as c:
            while True:
                j = get_with_retry(c, dp["url"], {
                    "$select": sel, "$top": 10000, "$skip": skip, "$format": "json",
                    "$filter": f"incidentBeginDate ge '{dp['first_year']}-01-01T00:00:00.000z'"}).json()
                batch = j["DisasterDeclarationsSummaries"]
                rows += batch
                if len(batch) < 10000:
                    break
                skip += 10000
        pd.DataFrame(rows).to_csv(cache, index=False)
    d = pd.read_csv(cache, dtype={"fipsStateCode": str, "fipsCountyCode": str})
    types = {t for ts in dp["incident_types"].values() for t in ts}
    d = d[(d["declarationType"] == dp["declaration_type"]) & d["incidentType"].isin(types)]
    d = d[d["fipsCountyCode"].fillna("000") != "000"]      # statewide records have no county
    d["county_fips"] = d["fipsStateCode"].str.zfill(2) + d["fipsCountyCode"].str.zfill(3)
    d["begin"] = pd.to_datetime(d["incidentBeginDate"]).dt.strftime("%Y-%m-%d")
    d["end"] = pd.to_datetime(d["incidentEndDate"].fillna(d["incidentBeginDate"])).dt.strftime("%Y-%m-%d")
    d = d[d["begin"].str[:4].astype(int) <= dp["last_year"]]
    hub_counties = {r[0] for r in conn.execute("SELECT county_fips FROM hubs")}
    d = d[d["county_fips"].isin(hub_counties)]           # only hub counties are used
    title = d["declarationTitle"] if "declarationTitle" in d else None
    conn.execute("DELETE FROM fema_declarations")
    conn.executemany("INSERT OR IGNORE INTO fema_declarations VALUES (?, ?, ?, ?, ?, ?)",
                     zip(d["disasterNumber"], d["county_fips"], d["incidentType"],
                         title if title is not None else [None] * len(d), d["begin"], d["end"]))
    conn.commit()
    log_ingest(conn, "fema_declarations", "ok", rows=len(d), params={"type": dp["declaration_type"]})
    return {"county_declarations": int(len(d)), "disasters": int(d["disasterNumber"].nunique())}


def ingest(conn: sqlite3.Connection, refresh: bool = False) -> dict:
    return {"ugc_rows": hub_geography(conn, refresh), **ingest_warnings(conn, refresh),
            **ingest_declarations(conn, refresh)}
