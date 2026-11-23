"""Tests for the live warning monitor, with recorded-style NWS responses (no network, temporary database)."""

import pytest

from app.alerts import monitor
from app.db import create_schema, get_connection

# Two test hubs; the fake fetch below answers by latitude.
HUBS = [("alpha", "Alpha", "FL", 30.0, -81.0), ("beta", "Beta", "TX", 29.0, -95.0)]


def feature(vtec: str, event: str = "Flood Warning", status: str = "Actual", ends: str = "2026-11-25T19:00:00-05:00"):
    """One alert in the shape the NWS API returns (only the fields the monitor reads)."""
    return {"properties": {"status": status, "event": event, "headline": f"{event} for the area",
                           "ends": ends, "expires": ends, "parameters": {"VTEC": [vtec]}}}


FLOOD_NEW = "/O.NEW.KJAX.FA.W.0001.261123T1800Z-261124T0000Z/"
FLOOD_EXT = "/O.EXT.KJAX.FA.W.0001.000000T0000Z-261125T0000Z/"   # same event, extended
WINTER_NEW = "/O.NEW.KHGX.WS.W.0007.261123T1800Z-261124T1200Z/"


@pytest.fixture
def conn(tmp_path):
    c = get_connection(tmp_path / "alerts.db")
    create_schema(c)
    c.executemany("INSERT INTO hubs (id, name, state, region, lat, lon, county_fips) VALUES (?, ?, ?, 'South', ?, ?, '00000')",
                  HUBS)
    c.commit()
    yield c
    c.close()


def fetcher(responses: dict):
    """Fake fetch: responses maps hub latitude to a feature list, or to an exception to raise."""
    def fetch(lat, lon):
        r = responses.get(lat, [])
        if isinstance(r, Exception):
            raise r
        return r
    return fetch


class Recorder:
    """Fake webhook that records what was posted, or fails when asked to."""

    def __init__(self, fail: Exception | None = None):
        self.payloads, self.fail = [], fail

    def __call__(self, url, payload):
        if self.fail:
            raise self.fail
        self.payloads.append(payload)


def check(conn, responses, post=None, url="https://example.test/hook", t="2026-11-23T18:00:00+00:00"):
    return monitor.run_check(conn, fetch=fetcher(responses), post=post or Recorder(), webhook_url=url, checked_at=t)


# --------------------------------------------------------------------------- parsing


def test_parse_keeps_only_real_active_warnings_of_tracked_types():
    types = monitor.warning_types()
    features = [
        feature(FLOOD_NEW),                                                    # kept
        feature("/O.NEW.KJAX.FA.A.0002.261123T1800Z-261124T0000Z/", "Flood Watch"),       # watch: ignored
        feature("/O.NEW.KJAX.CF.Y.0003.261123T1800Z-261124T0000Z/", "Coastal Flood Advisory"),  # advisory
        feature("/O.NEW.KJAX.FL.W.0004.261123T1800Z-261124T0000Z/", "Flood Warning"),     # river gauge: not tracked
        feature("/T.NEW.KJAX.FA.W.0005.261123T1800Z-261124T0000Z/"),           # test product class: ignored
        feature("/O.NEW.KJAX.FA.W.0006.261123T1800Z-261124T0000Z/", status="Exercise"),    # exercise: ignored
        feature("/O.CAN.KJAX.FA.W.0008.000000T0000Z-261124T0000Z/"),           # cancelled: no longer active
    ]
    active = monitor.parse_active(features, types)
    assert list(active) == ["KJAX.FA.W.0001"] and active["KJAX.FA.W.0001"].hazard == "flood"


def test_an_extension_keeps_the_same_event_key():
    types = monitor.warning_types()
    assert monitor.parse_active([feature(FLOOD_NEW)], types).keys() == \
        monitor.parse_active([feature(FLOOD_EXT)], types).keys()


# --------------------------------------------------------------------------- checks over time


def test_first_check_is_a_baseline_not_new_alerts(conn):
    hook = Recorder()
    s = check(conn, {30.0: [feature(FLOOD_NEW)]}, post=hook)
    assert s["baseline"] and s["active_at_start"] == 1 and s["issued"] == 0 and s["active_now"] == 1
    assert "active when monitoring started" in hook.payloads[0]["text"]


def test_new_warning_is_issued_then_ended(conn):
    check(conn, {})                                                     # baseline with nothing active
    hook = Recorder()
    s = check(conn, {29.0: [feature(WINTER_NEW, "Winter Storm Warning")]}, post=hook, t="2026-11-23T18:15:00+00:00")
    assert (s["issued"], s["ended"], s["active_now"], s["webhook"]) == (1, 0, 1, "sent")
    assert "Beta, TX: Winter Storm Warning issued" in hook.payloads[0]["text"]
    s = check(conn, {}, t="2026-11-23T18:30:00+00:00")                 # no longer listed by NWS
    assert (s["issued"], s["ended"], s["active_now"]) == (0, 1, 0)


def test_update_of_the_same_warning_is_not_a_new_alert(conn):
    check(conn, {})
    check(conn, {30.0: [feature(FLOOD_NEW)]}, t="2026-11-23T18:15:00+00:00")
    s = check(conn, {30.0: [feature(FLOOD_EXT, ends="2026-11-26T07:00:00-05:00")]}, t="2026-11-23T18:30:00+00:00")
    assert (s["issued"], s["ended"], s["active_now"]) == (0, 0, 1)
    assert conn.execute("SELECT ends FROM active_warnings").fetchone()[0] == "2026-11-26T07:00:00-05:00"


def test_failed_request_leaves_the_hub_unchanged(conn):
    check(conn, {})
    check(conn, {30.0: [feature(FLOOD_NEW)]}, t="2026-11-23T18:15:00+00:00")
    s = check(conn, {30.0: RuntimeError("NWS API unavailable")}, t="2026-11-23T18:30:00+00:00")
    assert s["hubs_failed"] == ["alpha"] and s["ended"] == 0 and s["active_now"] == 1


def test_webhook_failure_is_recorded_without_the_url(conn):
    class HTTPStatusError(Exception):
        response = type("R", (), {"status_code": 404})()

    check(conn, {})
    s = check(conn, {30.0: [feature(FLOOD_NEW)]}, post=Recorder(fail=HTTPStatusError("404 for url https://secret.test/x")),
              t="2026-11-23T18:15:00+00:00")
    assert s["webhook"] == "failed: HTTPStatusError (HTTP 404)" and "secret" not in s["webhook"]
    delivered, error = conn.execute("SELECT delivered, delivery_error FROM hub_alerts").fetchone()
    assert delivered == 0 and "secret" not in error


def test_without_a_webhook_alerts_are_still_recorded(conn):
    check(conn, {}, url=None)
    s = check(conn, {30.0: [feature(FLOOD_NEW)]}, url=None, t="2026-11-23T18:15:00+00:00")
    assert s["webhook"] == "not configured" and s["issued"] == 1
    r = monitor.recent(conn)
    assert r["active_warnings"][0]["hub_id"] == "alpha" and r["recent_alerts"][0]["kind"] == "issued"
    assert r["last_check"]["checked_at"] == "2026-11-23T18:15:00+00:00"
