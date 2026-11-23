"""NOAA NHC HURDAT2 best-track data (Atlantic + NE Pacific).

File format: a header line per storm ("AL092017, HARVEY, 61,") followed by that many
fix lines ("20170825, 1800,  , HU, 27.8N,  96.8W, 115, ..."). Fixes are every 6 hours
(plus landfall/peak entries). Positions are N/W with hemisphere letters.
"""

import sqlite3

from app.config import get_settings
from app.ingest.common import get_with_retry, http_client, log_ingest
from app.scoring.params import get_params


def _parse_coord(token: str) -> float:
    """'27.8N' becomes 27.8 and '96.8W' becomes -96.8 (south and west are negative)."""
    token = token.strip()
    value = float(token[:-1])
    return -value if token[-1] in "SW" else value


def parse(text: str, first_year: int) -> list[tuple]:
    """Parse a HURDAT2 file into (storm_id, name, time, status, lat, lon, wind) rows from first_year onward."""
    rows, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        header = [t.strip() for t in lines[i].split(",")]
        # A storm header has the number of fix lines that follow in its third field.
        if len(header) < 3 or not header[2].isdigit():
            i += 1
            continue
        storm_id, name, n = header[0], header[1], int(header[2])
        year = int(storm_id[-4:])
        for line in lines[i + 1: i + 1 + n]:
            t = [x.strip() for x in line.split(",")]
            if year < first_year:
                continue
            d, hhmm, status = t[0], t[1].zfill(4), t[3]
            dt = f"{d[:4]}-{d[4:6]}-{d[6:8]}T{hhmm[:2]}:{hhmm[2:]}:00Z"
            wind = float(t[6]) if t[6] not in ("", "-999", "-99") else None   # -999 and -99 mean unknown
            rows.append((storm_id, name, dt, status, _parse_coord(t[4]), _parse_coord(t[5]), wind))
        i += 1 + n
    return rows


def ingest(conn: sqlite3.Connection, refresh: bool = False) -> dict:
    """Download (or read cached) Atlantic and Northeast Pacific files and replace storm_tracks."""
    p = get_params()["event_rates"]["hurricane"]
    raw_dir = get_settings().raw_dir / "hurdat2"
    raw_dir.mkdir(parents=True, exist_ok=True)
    all_rows = []
    with http_client() as c:
        for url in p["source_files"]:
            cache = raw_dir / url.rsplit("/", 1)[-1]
            if refresh or not cache.exists():
                cache.write_bytes(get_with_retry(c, url).content)
            all_rows += parse(cache.read_text(encoding="latin-1"), p["first_year"])
    conn.execute("DELETE FROM storm_tracks")
    # A few storms have two fixes with the same timestamp (e.g. landfall marker); keep the first.
    conn.executemany("INSERT OR IGNORE INTO storm_tracks VALUES (?, ?, ?, ?, ?, ?, ?)", all_rows)
    conn.commit()
    storms = conn.execute("SELECT COUNT(DISTINCT storm_id) FROM storm_tracks").fetchone()[0]
    log_ingest(conn, "hurdat2", "ok", rows=len(all_rows),
               params={"files": p["source_files"], "first_year": p["first_year"]}, note=f"storms={storms}")
    return {"fixes": len(all_rows), "storms": storms}
