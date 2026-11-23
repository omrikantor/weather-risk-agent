"""Live warning monitor: alerts when a hub's current weather risk changes.

The risk scores describe 20 years of history and change only when the data is rebuilt, so alerting on score
changes would almost never fire. What changes day to day is whether a hub is under an active National Weather
Service warning right now. This module checks every hub against the NWS active-alerts API, compares the result
with the previous check, records each warning that starts or ends at a hub, and posts a summary to a webhook.

Rules that keep the alerts trustworthy:
- Only real warnings are tracked: status "Actual", VTEC significance W, and only the warning types our scoring
  counts (params.yaml, warnings.phenomena). Watches, advisories and statements are ignored.
- Each hub is queried by its point, so a warning counts only when its area contains the hub, the same idea as
  the storm-polygon rule in scoring.
- A warning is identified by its VTEC event id (office.type.W.number). The id stays the same when NWS updates
  or extends a warning, so an update is not reported as a new warning.
- If the request for a hub fails, that hub's state is left unchanged, so an API error never looks like
  "all warnings ended".
- The first check on a database where the monitor has never run is a baseline: warnings already active are
  recorded as "active_at_start" instead of "issued", so a fresh start or redeploy does not announce them as new.

Run one check from the command line:  python -m app.alerts.monitor
In production a scheduler calls POST /api/admin/alerts/check every 15 minutes (.github/workflows/alerts.yml).
"""

import json
import logging
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Callable

import httpx

from app.config import get_settings
from app.db import create_schema, get_connection
from app.ingest.common import USER_AGENT, now_iso
from app.scoring.params import get_params

log = logging.getLogger("alerts")

ACTIVE_ALERTS_URL = "https://api.weather.gov/alerts/active"
FETCH_WORKERS = 8         # parallel requests: 47 hubs take a few seconds without overloading the API
FETCH_TIMEOUT_S = 15

# A VTEC code describes one warning event, for example /O.EXT.KJAX.CF.W.0004.000000T0000Z-261126T0000Z/:
# product class O (operational, not a test), action EXT (extended), issuing office KJAX, warning type CF
# (coastal flood), significance W (warning) and event number 0004, followed by the valid time range.
VTEC_RE = re.compile(r"/O\.(?P<action>[A-Z]{3})\.(?P<office>[A-Z]{4})\.(?P<phen>[A-Z]{2})\.(?P<sig>[A-Z])\."
                     r"(?P<etn>\d{4})\.")
# Actions that announce the end of a warning; such a message means the warning is no longer active.
ENDED_ACTIONS = {"CAN", "EXP"}

# Only one check may run at a time (the scheduler and a manual call could overlap).
_RUN_LOCK = threading.Lock()


@dataclass(frozen=True)
class ActiveWarning:
    """One NWS warning active at a hub."""
    event_key: str        # stable id, e.g. KJAX.CF.W.0004
    hazard: str           # our hazard group
    event: str            # NWS name, e.g. "Coastal Flood Warning"
    headline: str | None
    ends: str | None      # ISO time with the issuing office's UTC offset


# --------------------------------------------------------------------------- pure functions


def warning_types() -> dict[str, str]:
    """NWS warning type code to our hazard group, from params.yaml (the same mapping scoring uses)."""
    return {code: hazard for hazard, codes in get_params()["warnings"]["phenomena"].items() for code in codes}


def parse_active(features: list[dict], types: dict[str, str]) -> dict[str, ActiveWarning]:
    """Keep the real, still-active warnings of tracked types from an NWS alerts response (GeoJSON features)."""
    out: dict[str, ActiveWarning] = {}
    for feature in features:
        p = feature.get("properties") or {}
        if p.get("status") != "Actual":            # skip exercises, tests and drafts
            continue
        for vtec in (p.get("parameters") or {}).get("VTEC") or []:
            m = VTEC_RE.search(vtec)
            if not m or m["sig"] != "W" or m["phen"] not in types or m["action"] in ENDED_ACTIONS:
                continue
            key = f"{m['office']}.{m['phen']}.W.{m['etn']}"
            out[key] = ActiveWarning(key, types[m["phen"]], p.get("event") or m["phen"], p.get("headline"),
                                     p.get("ends") or p.get("expires"))
    return out


def summary_text(alerts: list[dict], checked_at: str) -> str:
    """Plain-text webhook message listing the alerts of one check (readable in Slack or any webhook viewer)."""
    verbs = {"issued": "issued", "ended": "ended", "active_at_start": "active when monitoring started"}
    lines = [f"Hub weather alerts (check at {checked_at[:16].replace('T', ' ')} UTC)"]
    for a in alerts:
        until = f", until {a['ends'][:16].replace('T', ' ')} local time" if a["kind"] != "ended" and a["ends"] else ""
        lines.append(f"- {a['hub_name']}, {a['state']}: {a['event']} {verbs[a['kind']]}{until}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- network


def fetch_active(lat: float, lon: float) -> list[dict]:
    """Active NWS alerts whose area contains the point. Raises on network or HTTP errors."""
    with httpx.Client(timeout=FETCH_TIMEOUT_S, headers={"User-Agent": USER_AGENT,
                                                        "Accept": "application/geo+json"}) as client:
        r = client.get(ACTIVE_ALERTS_URL, params={"point": f"{lat:.4f},{lon:.4f}"})
        r.raise_for_status()
        return r.json().get("features", [])


def post_webhook(url: str, payload: dict) -> None:
    """POST a JSON message. The "text" field is what Slack incoming webhooks display. Raises on failure."""
    r = httpx.post(url, json=payload, timeout=FETCH_TIMEOUT_S)
    r.raise_for_status()


# --------------------------------------------------------------------------- the check


def run_check(conn: sqlite3.Connection, fetch: Callable[[float, float], list[dict]] | None = None,
              post: Callable[[str, dict], None] | None = None, webhook_url: str | None = None,
              checked_at: str | None = None) -> dict:
    """Check every hub once, record what changed, send the webhook, and return a summary.

    fetch and post default to the real NWS request and webhook POST; tests pass recorded responses instead.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "a check is already running"}
    try:
        return _run_check(conn, fetch or fetch_active, post or post_webhook, webhook_url, checked_at or now_iso())
    finally:
        _RUN_LOCK.release()


def _run_check(conn, fetch, post, webhook_url, checked_at) -> dict:
    create_schema(conn)   # the alert tables may be missing in an older database file
    types = warning_types()
    hubs = conn.execute("SELECT id, name, state, lat, lon FROM hubs ORDER BY id").fetchall()
    baseline = conn.execute("SELECT COUNT(*) FROM monitor_runs").fetchone()[0] == 0

    def safe_fetch(hub):
        # Returns the features, or None when the request failed (the hub is then skipped this round).
        try:
            return fetch(hub[3], hub[4])
        except Exception as e:  # noqa: BLE001 - any failure only skips this hub
            log.warning("alert check failed for %s: %s", hub[0], e)
            return None

    with ThreadPoolExecutor(FETCH_WORKERS) as pool:
        responses = list(pool.map(safe_fetch, hubs))

    new_alerts, failed = [], []
    for hub, features in zip(hubs, responses):
        hub_id, name, state = hub[0], hub[1], hub[2]
        if features is None:
            failed.append(hub_id)
            continue
        current = parse_active(features, types)
        previous = {r[0]: ActiveWarning(*r) for r in conn.execute(
            "SELECT event_key, hazard, event, headline, ends FROM active_warnings WHERE hub_id=?", (hub_id,))}

        for key in current.keys() - previous.keys():          # started since the last check
            w = current[key]
            conn.execute("INSERT INTO active_warnings VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (hub_id, w.event_key, w.hazard, w.event, w.headline, w.ends, checked_at))
            new_alerts.append(_record(conn, checked_at, hub, "active_at_start" if baseline else "issued", w))
        for key in previous.keys() - current.keys():          # no longer active
            conn.execute("DELETE FROM active_warnings WHERE hub_id=? AND event_key=?", (hub_id, key))
            new_alerts.append(_record(conn, checked_at, hub, "ended", previous[key]))
        for key in current.keys() & previous.keys():          # still active: keep the latest end time
            w = current[key]
            conn.execute("UPDATE active_warnings SET headline=?, ends=? WHERE hub_id=? AND event_key=?",
                         (w.headline, w.ends, hub_id, key))

    webhook = _deliver(conn, new_alerts, webhook_url, post, checked_at)
    counts = {kind: sum(a["kind"] == kind for a in new_alerts) for kind in ("issued", "ended", "active_at_start")}
    summary = {"checked_at": checked_at, "baseline": baseline, "hubs_checked": len(hubs) - len(failed),
               "hubs_failed": failed, **counts,
               "active_now": conn.execute("SELECT COUNT(*) FROM active_warnings").fetchone()[0], "webhook": webhook}
    conn.execute("INSERT INTO monitor_runs (checked_at, hubs_checked, hubs_failed, summary_json) VALUES (?, ?, ?, ?)",
                 (checked_at, summary["hubs_checked"], json.dumps(failed), json.dumps(summary)))
    conn.commit()
    log.info("alert check: %s", summary)
    return summary


def _record(conn, checked_at: str, hub: tuple, kind: str, w: ActiveWarning) -> dict:
    """Insert one alert row and return it as a dict (with hub name and state, for the webhook text)."""
    cur = conn.execute("INSERT INTO hub_alerts (created_at, hub_id, kind, event_key, hazard, event, headline, ends) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (checked_at, hub[0], kind, w.event_key, w.hazard, w.event, w.headline, w.ends))
    return {"id": cur.lastrowid, "hub_id": hub[0], "hub_name": hub[1], "state": hub[2], "kind": kind,
            **asdict(w)}


def _deliver(conn, alerts: list[dict], url: str | None, post, checked_at: str) -> str:
    """Send one webhook message for all alerts of this check and record the delivery result on each row."""
    if not alerts:
        return "nothing to send"
    if not url:
        return "not configured"
    payload = {"text": summary_text(alerts, checked_at), "alerts": alerts}
    ids = [(a["id"],) for a in alerts]
    try:
        post(url, payload)
    except Exception as e:  # noqa: BLE001 - delivery failure is recorded, never raised
        # Record the error type and HTTP status only. The full error text contains the webhook URL, which is a
        # secret and would otherwise appear in API responses and in the public scheduler logs.
        status = getattr(getattr(e, "response", None), "status_code", None)
        error = f"{type(e).__name__}" + (f" (HTTP {status})" if status else "")
        conn.executemany("UPDATE hub_alerts SET delivery_error=? WHERE id=?", [(error, i) for (i,) in ids])
        return f"failed: {error}"
    conn.executemany("UPDATE hub_alerts SET delivered=1 WHERE id=?", ids)
    return "sent"


# --------------------------------------------------------------------------- reading


def recent(conn: sqlite3.Connection, limit: int = 50) -> dict:
    """Warnings active now, the most recent alerts, and the last check (for GET /api/alerts)."""
    create_schema(conn)
    active = [dict(zip(("hub_id", "hub_name", "state", "event_key", "hazard", "event", "headline", "ends",
                        "first_seen"), r)) for r in conn.execute(
        "SELECT a.hub_id, h.name, h.state, a.event_key, a.hazard, a.event, a.headline, a.ends, a.first_seen "
        "FROM active_warnings a JOIN hubs h ON h.id = a.hub_id ORDER BY a.hub_id, a.first_seen")]
    history = [dict(zip(("created_at", "hub_id", "hub_name", "state", "kind", "hazard", "event", "ends",
                         "delivered", "delivery_error"), r)) for r in conn.execute(
        "SELECT a.created_at, a.hub_id, h.name, h.state, a.kind, a.hazard, a.event, a.ends, a.delivered, "
        "a.delivery_error FROM hub_alerts a JOIN hubs h ON h.id = a.hub_id ORDER BY a.id DESC LIMIT ?", (limit,))]
    last = conn.execute("SELECT summary_json FROM monitor_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"active_warnings": active, "recent_alerts": history, "last_check": json.loads(last[0]) if last else None,
            "note": "Live NWS warnings at each hub's location, checked every 15 minutes. Separate from the "
                    "historical risk scores, which do not change with today's weather."}


def main() -> None:
    """Run one check against the live NWS API and print the summary."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = get_connection()
    try:
        print(json.dumps(run_check(conn, webhook_url=get_settings().alert_webhook_url or None), indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
