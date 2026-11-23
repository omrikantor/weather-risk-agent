"""Tests for find_nearest_hubs against the built database (skipped if it doesn't exist).

They check behavior and invariants, not that particular cities "look right".
"""

import pytest

from app.config import get_settings
from app.tools.locate import find_nearest_hubs, normalize, split_state

pytestmark = pytest.mark.skipif(not get_settings().db_file.exists(), reason="database not built")


def test_normalize_and_state_parsing():
    assert normalize("St. Louis") == "saint louis"
    assert split_state("Tampa, FL") == ("tampa", "FL")
    assert split_state("tampa florida") == ("tampa", "FL")
    assert split_state("Kansas City") == ("kansas city", None)
    assert split_state("Salt Lake City, Utah") == ("salt lake city", "UT")


@pytest.mark.parametrize("q,hub", [("Dallas", "dallas"), ("Boise", "boise"), ("St. Louis", "st_louis"),
                                   ("Newark, NJ", "newark"), ("dallas, tx", "dallas"),
                                   ("Oklahoma City", "oklahoma_city"), ("Salt Lake City", "salt_lake_city")])
def test_hub_names_are_recognized(q, hub):
    r = find_nearest_hubs(q)
    assert r["status"] == "is_hub" and r["hub_id"] == hub


def test_census_suffix_stripped_once():
    from app.ingest.census_gazetteer import clean_place_name
    assert clean_place_name("Kansas City city") == "Kansas City"
    assert clean_place_name("Tampa city") == "Tampa"
    assert clean_place_name("Nashville-Davidson metropolitan government (balance)") == "Nashville-Davidson"


@pytest.mark.parametrize("q", ["Portland", "Columbus", "Kansas City", "Newark"])
def test_hub_name_shared_with_other_major_place_is_ambiguous(q):
    r = find_nearest_hubs(q)
    assert r["status"] == "ambiguous"
    assert any(o.get("hub_id") for o in r["options"]) and len(r["options"]) >= 2


def test_hub_name_with_state_is_the_hub():
    assert find_nearest_hubs("Portland, OR")["hub_id"] == "portland"


def test_single_major_place_is_chosen():
    r = find_nearest_hubs("Tampa")
    assert r["status"] == "matched" and r["place"]["state"] == "FL"


def test_several_major_places_are_ambiguous():
    r = find_nearest_hubs("Springfield")
    assert r["status"] == "ambiguous" and len(r["options"]) >= 2


def test_state_resolves_ambiguity():
    r = find_nearest_hubs("Springfield, IL")
    assert r["status"] == "matched" and r["place"]["state"] == "IL"


def test_typo_is_tolerated():
    r = find_nearest_hubs("Tampaa, FL")
    assert r["status"] == "matched" and r["place"]["state"] == "FL" and "note" in r


def test_unknown_place():
    assert find_nearest_hubs("Xyzzyville")["status"] == "not_found"


def test_far_place_gets_caveat():
    r = find_nearest_hubs("Honolulu")
    assert r["status"] == "matched" and r["far_away"] is True


@pytest.mark.parametrize("q", ["Tampa, FL", "Spokane, WA", "Tulsa", "Fort Worth"])
def test_candidate_invariants(q):
    r = find_nearest_hubs(q)
    assert r["status"] == "matched"
    c = r["candidates"]
    assert len(c) == 3
    assert [x["distance_km"] for x in c] == sorted(x["distance_km"] for x in c)
    assert all(x["similarity"] is None or 0 <= x["similarity"] <= 100 for x in c)
    assert r["recommended_hub_id"] in {x["hub_id"] for x in c}
    assert r["far_away"] == (next(x for x in c if x["hub_id"] == r["recommended_hub_id"])["distance_km"] > 250)


def test_deterministic():
    assert find_nearest_hubs("Spokane, WA") == find_nearest_hubs("Spokane, WA")
