"""find_nearest_hubs: locate a US place offline and recommend a stand-in hub.

Steps (all deterministic; parameters in params.yaml, section stand_in):
1. Normalize the query and extract an optional state ("Tampa, FL", "tampa florida").
2. If it names one of our hubs, return status "is_hub" (unless another major place has the same name).
3. Resolve the place from the Census places table, trying in order: exact name, known name pattern (alias),
   name prefix, then close spelling (typos). With several matches, exactly one "major place" (population at
   or above the threshold) is used; otherwise return status "ambiguous" with the options.
4. If the nearest hub is farther than the network's widest gap between neighboring hubs, return status
   "outside_coverage".
5. Take the N nearest hubs, compare the FEMA hazard-frequency profile of the place's county with each hub's
   county (similarity 0 to 100), and recommend the best of
   w_similarity x similarity + w_proximity x proximity.
The function returns a result for every input; the agent explains it and asks before analyzing a stand-in.
"""

import difflib
import re
import sqlite3
from functools import lru_cache

import numpy as np
import pandas as pd

from app.db import get_connection
from app.geo import haversine_km
from app.scoring.params import get_params

# State names and postal codes, used to read the state from free text.
STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}
STATE_CODES = set(STATES.values())


def normalize(text: str) -> str:
    """Lowercase, expand "St." and "Ft.", drop punctuation and parentheses, so names compare reliably."""
    t = text.lower().strip()
    t = re.sub(r"\bst\.?\s+", "saint ", t)          # "St. Louis" and "st louis" both become "saint louis"
    t = re.sub(r"\bft\.?\s+", "fort ", t)
    t = re.sub(r"\([^)]*\)", " ", t)                # drop parentheticals, e.g. "(NYC metro)"
    t = re.sub(r"[^a-z0-9\s,-]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" ,")


def split_state(query: str) -> tuple[str, str | None]:
    """Split a query into place name and state: 'tampa, fl', 'tampa fl' and 'tampa florida' all give
    ('tampa', 'FL'). Longer state names are tried first so "west virginia" is not read as "virginia"."""
    q = normalize(query)
    for name, code in sorted(STATES.items(), key=lambda kv: -len(kv[0])):
        if q.endswith(" " + name) or q.endswith("," + name):
            return q[: -len(name)].strip(" ,"), code
    m = re.match(r"^(.*?)[,\s]+([a-z]{2})$", q)
    if m and m.group(2).upper() in STATE_CODES:
        return m.group(1).strip(" ,"), m.group(2).upper()
    return q.replace(",", " ").strip(), None


@lru_cache   # built once per process (32,000 places), then every lookup is a dictionary access
def _place_index(db_path: str) -> tuple[pd.DataFrame, dict, dict]:
    """All places plus two lookup tables: normalized name to rows, and alternative name to rows."""
    conn = sqlite3.connect(db_path)
    places = pd.read_sql("SELECT geoid, name, full_name, state, lat, lon, population, aland_sqmi, county_fips "
                         "FROM places", conn)
    conn.close()
    places["key"] = places["name"].map(normalize)
    exact: dict[str, list[int]] = {}
    alias: dict[str, list[int]] = {}
    for i, key in enumerate(places["key"]):
        exact.setdefault(key, []).append(i)
        alts = set()
        if key.startswith("urban "):
            alts.add(key[6:])                        # "Urban Honolulu" is found as "honolulu"
        if "-" in key:
            alts.add(key.split("-")[0].strip())      # "Nashville-Davidson" is found as "nashville"
        if key.endswith(" city") and len(key) > 5:
            alts.add(key[:-5])                       # "Boise City" is found as "boise"
        for a in alts:
            alias.setdefault(a, []).append(i)
    return places, exact, alias


@lru_cache
def _profiles(db_path: str) -> tuple[pd.DataFrame, list[str]]:
    """County hazard profiles: FEMA annualized frequency per hazard, scaled to 0 to 1.

    Each hazard is divided by its national high percentile (profile_scale_percentile) and capped at 1, so
    hazards with very different units (tornadoes per year, heat-wave days) can be compared on one scale and a
    few extreme counties do not dominate.
    """
    sp = get_params()["stand_in"]
    conn = sqlite3.connect(db_path)
    nri = pd.read_sql("SELECT county_fips, hazard, afreq FROM nri_county", conn)
    conn.close()
    hazards = sp["profile_hazards"]
    wide = nri.pivot(index="county_fips", columns="hazard", values="afreq").reindex(columns=hazards).fillna(0.0)
    scale = wide.quantile(sp["profile_scale_percentile"] / 100).replace(0, np.nan)
    return (wide / scale).clip(upper=1.0).fillna(0.0), hazards


def network_widest_gap_km(hubs: pd.DataFrame) -> float:
    """Largest distance from any hub to its nearest neighboring hub (the network's sparsest point)."""
    lat, lon = hubs["lat"].to_numpy(), hubs["lon"].to_numpy()
    d = haversine_km(lat[:, None], lon[:, None], lat[None, :], lon[None, :])
    np.fill_diagonal(d, np.inf)
    return float(d.min(axis=1).max())


def _hubs(conn) -> pd.DataFrame:
    """All hubs with a normalized name for matching."""
    h = pd.read_sql("SELECT id, name, state, lat, lon, county_fips FROM hubs", conn)
    h["key"] = h["name"].map(normalize)
    return h


def _pick(places: pd.DataFrame, idx: list[int], state: str | None, major: int) -> tuple[str, pd.DataFrame]:
    """Decide among matching places: "none", "one" (a clear answer) or "many" (ask the user).

    A single match within the given state is clear. With several matches, exactly one major place (population
    at or above the threshold, 50,000 by default) is taken as the one meant, the way "Dallas" means Dallas, TX.
    Otherwise the options are returned largest first.
    """
    cand = places.iloc[sorted(set(idx))]
    if state:
        cand = cand[cand["state"] == state]
    if cand.empty:
        return "none", cand
    if len(cand) == 1:
        return "one", cand
    majors = cand[cand["population"].fillna(0) >= major]
    if len(majors) == 1:
        return "one", majors
    ranked = cand.assign(pop=cand["population"].fillna(0)).sort_values(["pop", "aland_sqmi"], ascending=False)
    return "many", ranked


def find_nearest_hubs(location: str, conn: sqlite3.Connection | None = None) -> dict:
    """Resolve a free-text US place and recommend a stand-in hub (see the module description for the steps).

    Returns a dict whose status is one of: is_hub, ambiguous, not_found, outside_coverage or matched.
    """
    sp = get_params()["stand_in"]
    own = conn is None
    conn = conn or get_connection()
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    try:
        name, state = split_state(location)
        result = {"query": location, "parsed": {"name": name, "state": state}}

        hubs = _hubs(conn)
        places, exact, alias = _place_index(db_path)
        hit = hubs[(hubs["key"] == name) & ((hubs["state"] == state) if state else True)]
        if len(hit) == 1:
            h = hit.iloc[0]
            # A bare hub name is the hub only if no other major place shares the name (for example "Portland"
            # could be the Portland, OR hub or Portland, ME).
            namesakes = places.iloc[exact.get(name, []) + alias.get(name, [])]
            others = namesakes[(namesakes["state"] != h["state"]) &
                               (namesakes["population"].fillna(0) >= sp["major_place_population"])]
            if state or others.empty:
                return {**result, "status": "is_hub", "hub_id": h["id"],
                        "message": f"{h['name']}, {h['state']} is one of our hubs; use its hub_id directly."}
            options = [{"name": h["name"], "state": h["state"], "hub_id": h["id"], "note": "one of our hubs"}] + [
                {"name": r.full_name, "state": r.state, "population": int(r.population)}
                for r in others.sort_values("population", ascending=False).drop_duplicates("geoid").head(4).itertuples()]
            return {**result, "status": "ambiguous", "options": options,
                    "message": "The name matches one of our hubs and other major places; ask which one is meant."}
        # Try exact names, then aliases, then names that start with the query ("san antonio" finds
        # "san antonio city" style entries); stop at the first kind of match that gives a result.
        status, cand = "none", None
        for lookup in (exact.get(name, []), alias.get(name, []),
                       [i for k, v in exact.items() if k.startswith(name + " ") for i in v]):
            if lookup:
                status, cand = _pick(places, lookup, state, sp["major_place_population"])
                if status != "none":
                    break
        if status == "none":
            # Spelling mistakes: closest names above a similarity cutoff (0.85 by default).
            close = difflib.get_close_matches(name, list(exact), n=5, cutoff=sp["fuzzy_cutoff"])
            if close:
                status, cand = _pick(places, [i for k in close for i in exact[k]], state, sp["major_place_population"])
                result["note"] = f"interpreted as '{close[0]}' (closest name match)"
        if status == "none":
            return {**result, "status": "not_found",
                    "message": "No US place with that name was found. Ask the user to check the spelling or add the state."}
        if status == "many":
            options = [{"name": r.full_name, "state": r.state,
                        "population": None if pd.isna(r.population) else int(r.population)}
                       for r in cand.head(5).itertuples()]
            return {**result, "status": "ambiguous", "options": options,
                    "message": "Several places match; ask the user which one (or which state) they mean."}

        place = cand.iloc[0]
        # The resolved place may be a hub city written differently (for example "Saint Louis" for St. Louis):
        # same state, within 15 km and the same first word means it is the hub.
        same = hubs[(hubs["state"] == place["state"]) &
                    (haversine_km(place["lat"], place["lon"], hubs["lat"], hubs["lon"]) <= 15) &
                    (hubs["key"].map(lambda k: k.split()[0]) == normalize(place["name"]).split()[0])]
        if len(same) == 1:
            return {**result, "status": "is_hub", "hub_id": same.iloc[0]["id"],
                    "message": f"{place['full_name']}, {place['state']} is served by our "
                               f"{same.iloc[0]['name']} hub; use its hub_id directly."}

        prof, hazards = _profiles(db_path)
        d = haversine_km(place["lat"], place["lon"], hubs["lat"].to_numpy(), hubs["lon"].to_numpy())
        near = hubs.assign(distance_km=d).sort_values(["distance_km", "id"]).head(sp["candidates"])
        # Coverage limit computed from the network itself, so it adapts when hubs are added.
        coverage_km = network_widest_gap_km(hubs)
        if near["distance_km"].iloc[0] > coverage_km:
            n0 = near.iloc[0]
            return {**result, "status": "outside_coverage",
                    "place": {"name": place["full_name"], "state": place["state"]},
                    "nearest_hub": {"hub_id": n0["id"], "hub_name": n0["name"],
                                    "distance_km": round(float(n0["distance_km"]))},
                    "coverage_km": round(coverage_km),
                    "message": f"This place is outside the hub network's coverage: its nearest hub is "
                               f"{round(float(n0['distance_km']))} km away, more than the network's widest gap between "
                               f"neighboring hubs ({round(coverage_km)} km), so no hub is a meaningful stand-in."}
        p_vec = prof.loc[place["county_fips"]] if place["county_fips"] in prof.index else None

        candidates = []
        for h in near.itertuples():
            h_vec = prof.loc[h.county_fips]
            if p_vec is not None:
                diff = (h_vec - p_vec)
                # 100 = identical profiles; each point lost is an average gap of 0.01 across the 10 hazards.
                similarity = round(100 * (1 - float(diff.abs().mean())), 1)
                top = diff.abs().sort_values(ascending=False).head(2).index
                differences = [{"hazard": z, "hub_level": round(float(h_vec[z]), 2),
                                "place_level": round(float(p_vec[z]), 2),
                                "hub_is": "higher" if diff[z] > 0 else "lower"} for z in top if abs(diff[z]) > 0.05]
            else:
                similarity, differences = None, []
            # Proximity: 100 at the same spot, falling linearly to 0 at proximity_zero_km (500 km).
            proximity = round(100 * max(0.0, 1 - h.distance_km / sp["proximity_zero_km"]), 1)
            combined = round(sp["w_similarity"] * (similarity or 0) + sp["w_proximity"] * proximity, 1)
            candidates.append({"hub_id": h.id, "hub_name": h.name, "state": h.state,
                               "distance_km": round(float(h.distance_km)), "similarity": similarity,
                               "proximity": proximity, "combined": combined, "largest_differences": differences})

        # Highest combined score; candidates within tie_margin of it count as tied, and the closest of those wins.
        # County-level profiles are coarse while distance is exact, so distance decides near-ties.
        top = max(c["combined"] for c in candidates)
        best = min((c for c in candidates if top - c["combined"] <= sp["tie_margin"]),
                   key=lambda c: (c["distance_km"], c["hub_id"]))
        caveats = ["The place's county is approximated by the nearest county center; hazard profiles are "
                   "county-level FEMA National Risk Index frequencies."]
        if best["distance_km"] > sp["far_caveat_km"]:
            caveats.insert(0, f"The recommended stand-in is {best['distance_km']} km away; local weather can differ "
                              f"substantially at that distance.")
        if p_vec is None:
            caveats.append("No FEMA hazard profile for this county; recommendation is by distance only.")
        return {
            **result, "status": "matched",
            "place": {"name": place["full_name"], "state": place["state"], "lat": float(place["lat"]),
                      "lon": float(place["lon"]), "county_fips": place["county_fips"],
                      "population": None if pd.isna(place["population"]) else int(place["population"])},
            "candidates": candidates, "recommended_hub_id": best["hub_id"],
            "far_away": best["distance_km"] > sp["far_caveat_km"], "caveats": caveats,
            "method": f"Nearest {sp['candidates']} hubs; recommendation = {sp['w_similarity']} x hazard-profile "
                      f"similarity + {sp['w_proximity']} x proximity (0 at {sp['proximity_zero_km']} km).",
        }
    finally:
        if own:
            conn.close()
