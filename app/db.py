"""SQLite connection and schema.

SQLite is the single store for this project: ~50 hubs x 10 years of daily weather
(~165k rows), FEMA NRI for all ~3.2k US counties, and Census place/county centroids.
"""

import sqlite3
from pathlib import Path

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS hubs (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    state         TEXT NOT NULL,
    region        TEXT NOT NULL,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    county_fips   TEXT NOT NULL,
    county_name   TEXT
);

CREATE TABLE IF NOT EXISTS daily_weather (
    hub_id         TEXT NOT NULL REFERENCES hubs(id),
    date           TEXT NOT NULL,          -- ISO date, local time of the hub
    tmax_c         REAL,
    tmin_c         REAL,
    precip_mm      REAL,
    snowfall_cm    REAL,
    wind_gust_kmh  REAL,
    weather_code   INTEGER,                -- WMO code (56/57/66/67 = freezing drizzle/rain)
    PRIMARY KEY (hub_id, date)
);

-- FEMA National Risk Index, one row per county x hazard, for ALL US counties.
-- freq_pctl (national percentile of annualized event frequency) is what scoring uses;
-- risk_score is kept for reference only (it scales with exposed property/population).
CREATE TABLE IF NOT EXISTS nri_county (
    county_fips   TEXT NOT NULL,
    hazard        TEXT NOT NULL,
    afreq         REAL,                    -- annualized frequency (events/year), NULL -> not applicable
    freq_pctl     REAL NOT NULL,           -- 0-100, national percentile of afreq (NULL treated as 0)
    alr_npctl     REAL,                    -- expected annual loss rate, national percentile
    risk_score    REAL,                    -- hazard type risk index score (exposure-driven)
    risk_rating   TEXT,
    nri_version   TEXT,
    PRIMARY KEY (county_fips, hazard)
);

CREATE TABLE IF NOT EXISTS counties (
    county_fips   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    state         TEXT NOT NULL,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS places (
    geoid         TEXT PRIMARY KEY,
    name          TEXT NOT NULL,           -- e.g. "Tampa" (Census suffix like "city" removed)
    full_name     TEXT NOT NULL,           -- e.g. "Tampa city"
    state         TEXT NOT NULL,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    aland_sqmi    REAL,                    -- land area
    population    INTEGER,                 -- Census estimate (incorporated places; NULL for CDPs)
    county_fips   TEXT                     -- nearest county centroid in the same state (approximation)
);
CREATE INDEX IF NOT EXISTS idx_places_name ON places(name);

-- NOAA HURDAT2 best tracks (Atlantic + NE Pacific), 6-hourly fixes, 1950+.
CREATE TABLE IF NOT EXISTS storm_tracks (
    storm_id    TEXT NOT NULL,             -- e.g. AL092017
    name        TEXT,
    dt          TEXT NOT NULL,             -- ISO datetime (UTC)
    status      TEXT,                      -- TS, HU, SS, TD, EX, ...
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    wind_kt     REAL,                      -- max sustained wind
    PRIMARY KEY (storm_id, dt)
);

-- NOAA SPC severe reports meeting the magnitude thresholds, kept only within the largest
-- tested radius of any hub (full national files stay cached in data/raw).
CREATE TABLE IF NOT EXISTS severe_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hazard      TEXT NOT NULL,             -- tornado | hail | wind
    date        TEXT NOT NULL,             -- local date of the report
    slat        REAL NOT NULL,
    slon        REAL NOT NULL,
    elat        REAL,                      -- tornado end point (NULL if unknown)
    elon        REAL,
    magnitude   REAL                       -- EF scale | hail inches | wind knots
);
CREATE INDEX IF NOT EXISTS idx_reports_hazard ON severe_reports(hazard, date);

-- Per-hub long-term event rates (Tier A), one row per hub x hazard x radius.
CREATE TABLE IF NOT EXISTS hub_event_rates (
    hub_id          TEXT NOT NULL,
    hazard          TEXT NOT NULL,         -- hurricane | hurricane_strength | tornado | hail | wind
    radius_km       REAL NOT NULL,
    first_year      INTEGER NOT NULL,
    last_year       INTEGER NOT NULL,
    events          INTEGER NOT NULL,      -- storms (hurricanes) or report days (convective)
    rate_per_year   REAL NOT NULL,
    rate_lo         REAL NOT NULL,         -- exact Poisson interval (confidence_level)
    rate_hi         REAL NOT NULL,
    p_horizon       REAL NOT NULL,         -- P(>= 1 event within horizon_years)
    horizon_years   INTEGER NOT NULL,
    params_version  TEXT NOT NULL,
    details_json    TEXT,                  -- e.g. notable storms
    PRIMARY KEY (hub_id, hazard, radius_km)
);

-- Tier B: the GHCN-Daily station chosen for each hub by the fixed selection rule.
CREATE TABLE IF NOT EXISTS hub_stations (
    hub_id          TEXT PRIMARY KEY,
    station_id      TEXT NOT NULL,
    station_name    TEXT,
    lat             REAL, lon REAL,
    distance_km     REAL NOT NULL,
    completeness_json TEXT NOT NULL,       -- per element share of days present, 1996-2025
    candidates_tried INTEGER NOT NULL,
    gust_measured   INTEGER NOT NULL       -- 0 = fallback station without peak-gust data
);

CREATE TABLE IF NOT EXISTS station_daily (
    station_id      TEXT NOT NULL,
    date            TEXT NOT NULL,
    prcp_mm         REAL,
    snow_cm         REAL,                  -- measured snowfall
    tmax_c          REAL,
    tmin_c          REAL,
    gust_kmh        REAL,                  -- WSF5: fastest 5-second wind
    wt_thunder      INTEGER,               -- WT03 observed (1) / not recorded (0)
    wt_ice_pellets  INTEGER,               -- WT04
    wt_hail         INTEGER,               -- WT05
    wt_glaze        INTEGER,               -- WT06
    wt_freezing_rain INTEGER,              -- WT17
    PRIMARY KEY (station_id, date)
);

CREATE TABLE IF NOT EXISTS hub_metrics (
    hub_id        TEXT NOT NULL,
    metric        TEXT NOT NULL,
    period        TEXT NOT NULL,           -- e.g. "2016-2025" or "2025"
    value         REAL,
    PRIMARY KEY (hub_id, metric, period)
);

CREATE TABLE IF NOT EXISTS risk_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hub_id          TEXT NOT NULL,
    computed_at     TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    composite       REAL NOT NULL,
    winter          REAL, hurricane REAL, flood REAL, convective REAL, heat REAL,
    tier            TEXT NOT NULL,
    details_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_hub ON risk_scores(hub_id, computed_at);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hub_id      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    old_value   REAL,
    new_value   REAL,
    message     TEXT NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    params      TEXT,
    status      TEXT NOT NULL,
    rows        INTEGER,
    note        TEXT
);
"""


def get_connection(db_file: Path | None = None) -> sqlite3.Connection:
    path = db_file or get_settings().db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
