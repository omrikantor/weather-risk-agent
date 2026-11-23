"""Small, dependency-free geometry helpers (numpy-vectorized)."""

import numpy as np

EARTH_RADIUS_KM = 6371.0   # mean Earth radius


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km; works on scalars or numpy arrays (broadcasting)."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def point_to_segment_km(lat0, lon0, slat, slon, elat, elon):
    """Distance from a point to line segments (start->end), arrays allowed for the segments.

    Uses a local equirectangular projection centred on the point, accurate for the
    short distances we care about (<= a few hundred km). If a segment has no end point
    (NaN), the distance to its start point is returned.
    """
    slat, slon = np.asarray(slat, float), np.asarray(slon, float)
    elat = np.where(np.isnan(np.asarray(elat, float)), slat, elat)
    elon = np.where(np.isnan(np.asarray(elon, float)), slon, elon)
    # Convert degrees to km around the point: one degree of latitude is about 111 km everywhere, one degree of
    # longitude shrinks with the cosine of the latitude.
    k = np.pi / 180 * EARTH_RADIUS_KM
    coslat = np.cos(np.radians(lat0))
    ax, ay = (slon - lon0) * k * coslat, (slat - lat0) * k
    bx, by = (elon - lon0) * k * coslat, (elat - lat0) * k
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    # t is how far along the segment (0 = start, 1 = end) the closest point lies; zero-length segments use t = 0.
    t = np.where(seg2 > 0, np.clip(-(ax * dx + ay * dy) / np.where(seg2 > 0, seg2, 1), 0, 1), 0)
    px, py = ax + t * dx, ay + t * dy
    return np.sqrt(px * px + py * py)
