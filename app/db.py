"""SQLite connection and schema.

SQLite is the single store for this project. It is written only by the offline build (app/ingest) and read by
the running service, so a server database is not needed. Sizes after the build: 47 hubs, about 515,000
station-days and 187,000 model-days of weather, 13,000 NWS warnings, 470 FEMA declarations, FEMA hazard
frequencies for every US county and 32,000 Census places. See DESIGN.md section 4 for why SQLite was chosen.
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
    PRIMARY KEY (hub_id, date)
);

-- FEMA National Risk Index annualized event frequency, one row per county x hazard, for
-- ALL US counties. Used by find_nearest_hubs to compare a place's hazard profile with hubs'.
CREATE TABLE IF NOT EXISTS nri_county (
    county_fips   TEXT NOT NULL,
    hazard        TEXT NOT NULL,
    afreq         REAL,                    -- annualized frequency (events per year); NULL when not applicable
    nri_version   TEXT,
    PRIMARY KEY (county_fips, hazard)
);

-- Every US Census place (cities, towns and unincorporated communities), used to find non-hub places.
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

-- Per-hub long-term storm and report rates, one row per hub x hazard x search radius.
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

-- The GHCN-Daily weather station chosen for each hub by the fixed selection rule in params.yaml.
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

-- Daily measurements from the chosen stations (1996 to 2025).
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

-- NWS geography for each hub (from the NWS points API).
CREATE TABLE IF NOT EXISTS hub_ugc (
    hub_id      TEXT NOT NULL,
    ugc         TEXT NOT NULL,             -- e.g. TXC113 (county) or TXZ119 (forecast zone)
    kind        TEXT NOT NULL,             -- county | zone
    timezone    TEXT NOT NULL,             -- IANA name, used to convert warning times to local dates
    PRIMARY KEY (hub_id, ugc)
);

-- NWS warnings (VTEC) for hub counties and zones, from the Iowa Environmental Mesonet archive. Storm-based
-- warnings whose polygon contains a hub are stored with ugc = 'POINT:<hub_id>'.
CREATE TABLE IF NOT EXISTS nws_warnings (
    ugc         TEXT NOT NULL,
    wfo         TEXT,
    phenomena   TEXT NOT NULL,             -- e.g. WS, SV, EH
    significance TEXT NOT NULL,            -- W = warning
    name        TEXT,
    eventid     INTEGER,
    issue       TEXT NOT NULL,             -- UTC ISO
    expire      TEXT NOT NULL,
    PRIMARY KEY (ugc, wfo, phenomena, significance, eventid, issue)
);

-- FEMA major-disaster declarations (weather incident types), county level.
CREATE TABLE IF NOT EXISTS fema_declarations (
    disaster_number INTEGER NOT NULL,
    county_fips   TEXT NOT NULL,
    incident_type TEXT NOT NULL,
    title         TEXT,
    begin_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    PRIMARY KEY (disaster_number, county_fips)
);

-- Seasonal (climatological) outlook: per hub x hazard x calendar month, from NWS warning days
-- 2006-2025 (and HURDAT2 storm days 1950-2025 for hurricanes). Historical frequency, not a forecast.
CREATE TABLE IF NOT EXISTS hub_monthly (
    hub_id          TEXT NOT NULL,
    hazard          TEXT NOT NULL,
    month           INTEGER NOT NULL,      -- 1-12
    years           INTEGER NOT NULL,      -- years observed (warning archive)
    years_with_warning INTEGER NOT NULL,   -- years with >= 1 warning day in this month
    p_warning       REAL NOT NULL,         -- years_with_warning / years
    p_lo            REAL NOT NULL,         -- exact binomial 90% interval
    p_hi            REAL NOT NULL,
    mean_warning_days REAL NOT NULL,       -- average warning days in this month
    severe_days     REAL NOT NULL,         -- mean_warning_days x the hazard's severity share
    longrecord_p    REAL,                  -- hurricanes: share of years 1950-2025 with a storm day this month
    PRIMARY KEY (hub_id, hazard, month)
);

-- When each hazard happens at each hub and what its events look like (from NWS warning days 2006-2025).
CREATE TABLE IF NOT EXISTS hub_hazard_season (
    hub_id      TEXT NOT NULL,
    hazard      TEXT NOT NULL,
    profile_json TEXT,                     -- NULL when too few warning days to describe a season
    PRIMARY KEY (hub_id, hazard)
);

-- Descriptive climate figures per hub (snow days per year and so on), written by the scoring engine.
CREATE TABLE IF NOT EXISTS hub_metrics (
    hub_id        TEXT NOT NULL,
    metric        TEXT NOT NULL,
    period        TEXT NOT NULL,           -- e.g. "2016-2025" or "2025"
    value         REAL,
    PRIMARY KEY (hub_id, metric, period)
);

-- One row per hub per scoring run. composite = disaster-level days per year; details_json holds the full
-- evidence that the tools use to explain a score. Readers always take the latest row per hub.
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

-- Live warning monitor (app/alerts/monitor.py): the NWS warnings active at each hub at the last check.
-- This is the monitor's memory between checks; comparing it with a new check gives issued and ended warnings.
CREATE TABLE IF NOT EXISTS active_warnings (
    hub_id      TEXT NOT NULL,
    event_key   TEXT NOT NULL,             -- stable NWS event id: office.type.W.number, e.g. KJAX.CF.W.0004
    hazard      TEXT NOT NULL,             -- our hazard group (winter, flood, convective, hurricane, heat)
    event       TEXT NOT NULL,             -- NWS name, e.g. "Coastal Flood Warning"
    headline    TEXT,
    ends        TEXT,                      -- when NWS expects the warning to end (ISO time)
    first_seen  TEXT NOT NULL,             -- first check that saw it (UTC ISO)
    PRIMARY KEY (hub_id, event_key)
);

-- Alert history: one row each time a warning starts or ends at a hub, with webhook delivery status.
CREATE TABLE IF NOT EXISTS hub_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,             -- time of the check (UTC ISO)
    hub_id      TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- issued | ended | active_at_start (baseline of a fresh start)
    event_key   TEXT NOT NULL,
    hazard      TEXT NOT NULL,
    event       TEXT NOT NULL,
    headline    TEXT,
    ends        TEXT,
    delivered   INTEGER NOT NULL DEFAULT 0, -- 1 once posted to the webhook
    delivery_error TEXT                    -- why delivery failed, if it did
);
CREATE INDEX IF NOT EXISTS idx_hub_alerts_time ON hub_alerts(created_at);

-- One row per monitor check. An empty table means the monitor has never run on this database, so the next
-- check is a baseline (warnings already active are not announced as new).
CREATE TABLE IF NOT EXISTS monitor_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at  TEXT NOT NULL,             -- UTC ISO
    hubs_checked INTEGER NOT NULL,
    hubs_failed TEXT NOT NULL,             -- JSON list of hub ids whose request failed
    summary_json TEXT NOT NULL             -- counts and webhook status returned by the check
);

-- What was downloaded and when; /health reports the latest fetch per source as data freshness.
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
    """Open the database. The long timeout lets a reader wait while the build is writing instead of failing."""
    path = db_file or get_settings().db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=120)
    conn.row_factory = sqlite3.Row   # rows can be read by column name as well as by position
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Create any missing tables and indexes; existing tables and data are left untouched."""
    conn.executescript(SCHEMA)
    conn.commit()
