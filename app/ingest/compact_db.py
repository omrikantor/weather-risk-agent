"""Shrink the database for committing/deploying. Data the app reads is unchanged.

1. Keep only the latest scoring run.
2. Rebuild the large keyed tables as WITHOUT ROWID (rows live in the primary-key index,
   removing SQLite's duplicate index).
3. Empty the raw event tables used only to recompute event rates (severe_reports,
   storm_tracks). The computed rates are kept, and a full rebuild repopulates these
   from the cached raw files.
4. VACUUM.

Usage: python -m app.ingest.compact_db
"""

import re

from app.config import get_settings
from app.db import get_connection

# Tables with a natural primary key, where WITHOUT ROWID roughly halves the storage.
WITHOUT_ROWID = ["daily_weather", "station_daily", "nri_county", "hub_metrics", "hub_event_rates", "nws_warnings",
                 "fema_declarations", "hub_ugc", "hub_monthly", "hub_hazard_season"]
# Raw tables the running app never reads (only the rates computed from them).
RAW_ONLY = ["severe_reports", "storm_tracks"]


def rebuild_without_rowid(conn, table: str) -> None:
    """Copy a table into a WITHOUT ROWID version with the same columns, then swap it in."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    if "WITHOUT ROWID" in sql.upper():
        return
    new_sql = re.sub(rf"CREATE TABLE\s+(IF NOT EXISTS\s+)?{table}", f"CREATE TABLE {table}_new", sql, count=1)
    conn.execute(new_sql.rstrip().rstrip(";") + " WITHOUT ROWID")
    conn.execute(f"INSERT INTO {table}_new SELECT * FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")


def main() -> None:
    """Compact the database in place and print the size before and after."""
    path = get_settings().db_file
    before = path.stat().st_size
    conn = get_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    latest = conn.execute("SELECT MAX(computed_at) FROM risk_scores").fetchone()[0]
    conn.execute("DELETE FROM risk_scores WHERE computed_at < ?", (latest,))
    for t in WITHOUT_ROWID:
        rebuild_without_rowid(conn, t)
    for t in RAW_ONLY:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"kept scoring run {latest}; size {before / 1e6:.1f} MB -> {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
