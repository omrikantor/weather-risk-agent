"""NOAA GHCN-Daily station observations for each hub (Tier B).

Station rule (params.yaml -> stations): nearest USW (first-order, usually a major airport)
station within max_distance_km whose records for required_elements are at least
min_completeness complete over first_year..last_year. Candidates come from NOAA's station
inventory; completeness is checked on the downloaded data, and the next-nearest candidate
is tried if a station falls short. Data come from the NCEI Access Data Service (no token).
"""

import io
import json
import sqlite3
import time

import pandas as pd

from app.config import get_settings
from app.geo import haversine_km
from app.ingest.common import get_with_retry, http_client, log_ingest
from app.scoring.params import get_params

WT_COLUMNS = {"WT03": "wt_thunder", "WT04": "wt_ice_pellets", "WT05": "wt_hail",
              "WT06": "wt_glaze", "WT17": "wt_freezing_rain"}


def _cached_text(url: str, name: str, refresh: bool) -> str:
    path = get_settings().raw_dir / "ghcn" / name
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with http_client(timeout=300) as c:
            path.write_bytes(get_with_retry(c, url).content)
    return path.read_text(encoding="latin-1")


def load_candidates(refresh: bool = False) -> pd.DataFrame:
    """USW stations whose inventory covers the window for all required elements."""
    sp = get_params()["stations"]
    rows = []
    for line in _cached_text(sp["stations_url"], "ghcnd-stations.txt", refresh).splitlines():
        sid = line[0:11]
        if not sid.startswith(sp["station_prefix"]):   # e.g. USW00094846 = US + W (first-order) network
            continue
        rows.append({"station_id": sid, "lat": float(line[12:20]), "lon": float(line[21:30]),
                     "name": line[41:71].strip()})
    stations = pd.DataFrame(rows)

    inv = pd.read_fwf(
        io.StringIO(_cached_text(sp["inventory_url"], "ghcnd-inventory.txt", refresh)),
        colspecs=[(0, 11), (31, 35), (36, 40), (41, 45)], names=["station_id", "element", "first", "last"],
    )
    inv = inv[inv["station_id"].str.startswith(sp["station_prefix"])]
    ok = None
    for el in sp["required_elements"]:
        covers = inv[(inv["element"] == el) & (inv["first"] <= sp["first_year"]) & (inv["last"] >= sp["last_year"])]
        ids = set(covers["station_id"])
        ok = ids if ok is None else ok & ids
    return stations[stations["station_id"].isin(ok)].reset_index(drop=True)


def fetch_station(station_id: str, refresh: bool = False) -> pd.DataFrame:
    sp = get_settings()
    p = get_params()["stations"]
    cache = sp.raw_dir / "ghcn" / "daily" / f"{station_id}.csv"
    if refresh or not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        params = {
            "dataset": "daily-summaries", "stations": station_id,
            "startDate": f"{p['first_year']}-01-01", "endDate": f"{p['last_year']}-12-31",
            "dataTypes": ",".join(p["elements"]), "format": "csv", "units": "metric",
            "includeAttributes": "false",
        }
        with http_client(timeout=300) as c:
            cache.write_bytes(get_with_retry(c, p["data_url"], params).content)
        time.sleep(1)
    return pd.read_csv(cache, dtype={"STATION": str})


def completeness(df: pd.DataFrame) -> dict:
    p = get_params()["stations"]
    days = len(pd.date_range(f"{p['first_year']}-01-01", f"{p['last_year']}-12-31"))
    out = {}
    for el in ("PRCP", "SNOW", "TMAX", "TMIN", "WSF5"):
        out[el] = round(float(df[el].notna().sum()) / days, 4) if el in df.columns else 0.0
    out["any_WT"] = round(float(df[[c for c in WT_COLUMNS if c in df.columns]].notna().any(axis=1).sum()) / days, 4) \
        if any(c in df.columns for c in WT_COLUMNS) else 0.0
    return out


def to_rows(station_id: str, df: pd.DataFrame) -> list[tuple]:
    def col(name):
        return df[name] if name in df.columns else pd.Series([None] * len(df))

    out = pd.DataFrame({
        "station_id": station_id,
        "date": df["DATE"],
        "prcp_mm": col("PRCP"),
        "snow_cm": col("SNOW") / 10.0 if "SNOW" in df.columns else None,   # service returns mm
        "tmax_c": col("TMAX"),
        "tmin_c": col("TMIN"),
        "gust_kmh": col("WSF5") * 3.6 if "WSF5" in df.columns else None,   # m/s -> km/h
    })
    for wt, name in WT_COLUMNS.items():
        out[name] = col(wt).notna().astype(int) if wt in df.columns else 0
    return [tuple(None if pd.isna(v) else v for v in r) for r in out.itertuples(index=False)]


def ingest(conn: sqlite3.Connection, refresh: bool = False) -> dict:
    p = get_params()["stations"]
    cands = load_candidates(refresh)
    hubs = conn.execute("SELECT id, lat, lon FROM hubs ORDER BY id").fetchall()
    conn.execute("DELETE FROM hub_stations")
    chosen, problems = {}, []
    for hub_id, lat, lon in hubs:
        d = cands.assign(distance_km=haversine_km(lat, lon, cands["lat"], cands["lon"]))
        d = d[d["distance_km"] <= p["max_distance_km"]].sort_values("distance_km")
        picked = None
        for tried, st in enumerate(d.itertuples(), 1):
            df = fetch_station(st.station_id, refresh)
            comp = completeness(df)
            if all(comp[el] >= p["min_completeness"] for el in p["required_elements"]):
                picked = (st, df, comp, tried)
                break
        if picked is None:
            problems.append(hub_id)
            continue
        st, df, comp, tried = picked
        conn.execute("DELETE FROM station_daily WHERE station_id = ?", (st.station_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO station_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", to_rows(st.station_id, df))
        conn.execute("INSERT INTO hub_stations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (hub_id, st.station_id, st.name, st.lat, st.lon, round(float(st.distance_km), 1),
                      json.dumps(comp), tried))
        conn.commit()
        chosen[hub_id] = st.station_id
        print(f"  {hub_id}: {st.station_id} {st.name} ({st.distance_km:.1f} km) {comp}")
    log_ingest(conn, "ghcn_daily", "ok" if not problems else "partial", rows=len(chosen),
               params={k: p[k] for k in ("max_distance_km", "first_year", "last_year", "min_completeness")},
               note=f"no qualifying station: {problems}" if problems else None)
    return {"hubs_with_station": len(chosen), "no_station": problems}
