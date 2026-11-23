"""Tests for event-rate helpers (synthetic tracks; no network)."""

import sqlite3

import pytest

from app.db import create_schema
from app.scoring.event_rates import densify_tracks, poisson_interval


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    create_schema(c)
    c.executemany("INSERT INTO storm_tracks VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("AL092017", "HARVEY", "2017-08-25T18:00:00Z", "HU", 27.8, -96.8, 115),
        ("AL092017", "HARVEY", "2017-08-26T00:00:00Z", "HU", 28.2, -97.1, 105),
    ])
    return c


def test_densified_points_keep_real_dates(conn):
    """Regression: timestamps must not collapse to 1970 (pandas datetime resolution)."""
    pts = densify_tracks(conn, step_km=10)
    assert set(pts["date"]) == {"2017-08-25", "2017-08-26"}
    assert len(pts) > 2


def test_interpolated_wind_between_fixes(conn):
    pts = densify_tracks(conn, step_km=10)
    assert pts["wind_kt"].between(105, 115).all()


def test_poisson_interval_contains_rate():
    lo, hi = poisson_interval(30, 30, 0.9)
    assert lo < 1.0 < hi
    assert poisson_interval(0, 30, 0.9)[0] == 0
