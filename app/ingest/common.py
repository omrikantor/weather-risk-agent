"""Shared helpers for ingestion: HTTP with retries, raw-file cache, ingest log."""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Some public APIs (for example api.weather.gov) require a descriptive User-Agent.
USER_AGENT = "weather-risk-agent/0.1 (research prototype)"


class RateLimitError(RuntimeError):
    """Raised when an API says we exceeded a quota we can't wait out quickly."""


def http_client(timeout: float = 120) -> httpx.Client:
    """HTTP client with a generous timeout (some archive downloads are large) that follows redirects."""
    return httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def get_with_retry(client: httpx.Client, url: str, params: dict | None = None,
                   attempts: int = 5, base_delay: float = 5.0) -> httpx.Response:
    """GET with exponential backoff on network errors, 429 and 5xx."""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            r = client.get(url, params=params)
            if r.status_code == 429:
                reason = r.text[:300]
                # Open-Meteo returns 429 with a reason like "Hourly API request limit exceeded"
                if "Hourly" in reason or "Daily" in reason:
                    raise RateLimitError(reason)
                last_err = RuntimeError(f"429: {reason}")
            elif r.status_code >= 500:
                last_err = RuntimeError(f"{r.status_code}: {r.text[:200]}")
            else:
                r.raise_for_status()
                return r
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_err = e
        time.sleep(base_delay * (2 ** i))   # wait 5, 10, 20, 40 seconds between attempts
    raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last_err}")


def read_json_cache(path: Path) -> dict | None:
    """Return a cached download from data/raw, or None if it has not been downloaded yet."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def write_json_cache(path: Path, data: dict) -> None:
    """Save a download to data/raw so rebuilds do not need the network."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def now_iso() -> str:
    """Current UTC time as ISO text, to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_ingest(conn: sqlite3.Connection, source: str, status: str, rows: int | None = None,
               params: dict | None = None, note: str | None = None) -> None:
    """Record one ingestion step in ingest_log (used for data freshness in /health)."""
    conn.execute(
        "INSERT INTO ingest_log (source, fetched_at, params, status, rows, note) VALUES (?, ?, ?, ?, ?, ?)",
        (source, now_iso(), json.dumps(params) if params else None, status, rows, note),
    )
    conn.commit()
