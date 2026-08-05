"""
Shared geographic helpers.

Pure-Python implementations of haversine distance, point-in-polygon and
point-to-polygon distance used by the database and monitor modules.
"""

from __future__ import annotations

import math

_EARTH_RADIUS_M = 6_371_000.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two coordinates."""
    if None in (lat1, lon1, lat2, lon2):
        return float('inf')
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    return _EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bbox(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Bounding box (lat_min, lat_max, lon_min, lon_max) around a point."""
    lat_deg = radius_m / 111_000.0
    lon_deg = radius_m / (111_000.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - lat_deg, lat + lat_deg, lon - lon_deg, lon + lon_deg


def _local_xy(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    """Equirectangular projection to meters relative to a reference point."""
    x = (lon - ref_lon) * 111_000.0 * math.cos(math.radians(ref_lat))
    y = (lat - ref_lat) * 111_000.0
    return x, y


def point_in_polygon(lat: float, lon: float, rings: list) -> bool:
    """Ray-casting point-in-polygon over GeoJSON rings ([lon, lat] nodes)."""
    if not rings or not rings[0]:
        return False
    ring = rings[0]
    n = len(ring)
    inside = False
    px, py = lon, lat
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _point_to_segment_m(x: float, y: float, a: tuple, b: tuple) -> float:
    """Distance from point (x, y) to segment ab in projected meters."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(x - ax, y - ay)
    t = ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(x - cx, y - cy)


def point_to_polygon_distance_m(lat: float, lon: float, rings: list) -> float:
    """Min distance in meters from a point to a polygon (0 if inside)."""
    if not rings or not rings[0]:
        return float('inf')
    ring = rings[0]
    ref_lat, ref_lon = ring[0][1], ring[0][0]
    if point_in_polygon(lat, lon, rings):
        return 0.0
    x, y = _local_xy(lat, lon, ref_lat, ref_lon)
    best = float('inf')
    n = len(ring)
    for i in range(n):
        a = _local_xy(ring[i][1], ring[i][0], ref_lat, ref_lon)
        b = _local_xy(ring[(i + 1) % n][1], ring[(i + 1) % n][0], ref_lat, ref_lon)
        best = min(best, _point_to_segment_m(x, y, a, b))
    return best
