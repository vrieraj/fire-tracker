"""
FRP-based fire locator.

Given a municipality name or coordinates, searches FRP satellite detections
in the area to pinpoint the exact fire location. Falls back to geocoded
coordinates if no FRP data is available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fire_tracker.database import FireDatabase
from fire_tracker.weather import geocode, Location

logger = logging.getLogger(__name__)

# Default bbox radius around a point (km)
_DEFAULT_RADIUS_KM = 15

# FRP confidence threshold
_MIN_CONFIDENCE = 0.3


@dataclass
class FireLocation:
    """Resolved fire location with confidence info."""
    latitude: float
    longitude: float
    municipality: str
    province: str
    region: str
    country: str
    source: str  # "frp" or "geocode"
    frp_count: int = 0
    frp_max_mw: float = 0.0
    confidence: str = "low"  # low, medium, high


def _km_to_degrees(radius_km: float, lat: float) -> tuple[float, float]:
    """Convert radius in km to approximate degree offsets."""
    lat_deg = radius_km / 111.0
    lon_deg = radius_km / (111.0 * abs(max(min(lat, 89), -89) * 0.01745 + 0.01745))
    # Simplified: 1 degree lat ≈ 111km, 1 degree lon ≈ 111*cos(lat) km
    import math
    lon_deg = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat_deg, lon_deg


def _get_bbox(lat: float, lon: float, radius_km: float = _DEFAULT_RADIUS_KM) -> dict:
    """Get bounding box around a point."""
    import math
    lat_deg = radius_km / 111.0
    lon_deg = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return {
        'lat_min': lat - lat_deg,
        'lat_max': lat + lat_deg,
        'lon_min': lon - lon_deg,
        'lon_max': lon + lon_deg,
    }


def _compute_centroid(detections: list[dict]) -> tuple[float, float]:
    """Compute weighted centroid of FRP detections (weighted by FRP)."""
    if not detections:
        return 0.0, 0.0

    total_frP = sum(d.get('frp_mw', 1) for d in detections)
    if total_frP == 0:
        total_frP = 1

    lat = sum(d['latitude'] * d.get('frp_mw', 1) for d in detections) / total_frP
    lon = sum(d['longitude'] * d.get('frp_mw', 1) for d in detections) / total_frP
    return round(lat, 6), round(lon, 6)


def locate_fire(
    municipality: str = "",
    province: str = "",
    lat: float | None = None,
    lon: float | None = None,
    *,
    db_path: str | Path | None = None,
    radius_km: float = _DEFAULT_RADIUS_KM,
    hours: int = 24,
) -> FireLocation | None:
    """
    Locate a fire using FRP data and geocoding.

    Steps:
    1. If no coordinates, geocode the municipality
    2. Search FRP detections within radius
    3. If FRP found, use centroid as exact location
    4. If no FRP, use geocoded coordinates

    Args:
        municipality: Municipality name (for geocoding)
        province: Province name (for geocoding)
        lat: Latitude (if known)
        lon: Longitude (if known)
        db_path: Path to SQLite database
        radius_km: Search radius around point
        hours: How far back to look for FRP

    Returns:
        FireLocation or None if geocoding fails
    """
    # Step 1: Get coordinates
    if lat is None or lon is None:
        if not municipality:
            logger.warning("No coordinates or municipality provided")
            return None

        query = f"{municipality}, {province}" if province else municipality
        locations = geocode(query, limit=1)
        if not locations:
            logger.warning("Geocoding failed for '%s'", query)
            return None

        loc = locations[0]
        lat = loc.latitude
        lon = loc.longitude
        municipality = loc.name or municipality
        province = loc.admin1 or province
        region = getattr(loc, 'admin2', '') or ""
        country = loc.country_code or "ES"
    else:
        # Reverse geocode to get location name
        from fire_tracker.api.app import reverse_geocode
        # We can't import from app.py directly, use nominatim
        import requests as _req
        try:
            r = _req.get(
                f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json",
                headers={"User-Agent": "FireTracker/1.0"},
                timeout=10,
            )
            data = r.json()
            addr = data.get('address', {})
            municipality = municipality or addr.get('city', addr.get('town', addr.get('village', '')))
            province = province or addr.get('state', addr.get('county', ''))
            region = addr.get('autonomous_community', '')
            country = addr.get('country_code', 'ES')
        except Exception:
            region = ""
            country = "ES"

    # Step 2: Search FRP in bbox
    bbox = _get_bbox(lat, lon, radius_km)
    frp_detections = []

    if db_path:
        try:
            db = FireDatabase(db_path)
            all_detections = db.get_frp_detections(hours=hours)
            frp_detections = [
                d for d in all_detections
                if (bbox['lat_min'] <= d['latitude'] <= bbox['lat_max']
                    and bbox['lon_min'] <= d['longitude'] <= bbox['lon_max']
                    and d.get('confidence', 0) >= _MIN_CONFIDENCE)
            ]
        except Exception as e:
            logger.warning("FRP query failed: %s", e)

    # Step 3: Resolve location
    if frp_detections:
        centroid_lat, centroid_lon = _compute_centroid(frp_detections)
        max_frp = max(d.get('frp_mw', 0) for d in frp_detections)
        count = len(frp_detections)

        if count >= 5:
            conf = "high"
        elif count >= 2:
            conf = "medium"
        else:
            conf = "low"

        logger.info(
            "FRP found: %d detections, centroid=(%.4f, %.4f), max_frp=%.1f MW",
            count, centroid_lat, centroid_lon, max_frp,
        )

        return FireLocation(
            latitude=centroid_lat,
            longitude=centroid_lon,
            municipality=municipality,
            province=province,
            region=region,
            country=country,
            source="frp",
            frp_count=count,
            frp_max_mw=max_frp,
            confidence=conf,
        )
    else:
        logger.info("No FRP detections in bbox, using geocoded coords")
        return FireLocation(
            latitude=lat,
            longitude=lon,
            municipality=municipality,
            province=province,
            region=region,
            country=country,
            source="geocode",
            frp_count=0,
            confidence="low",
        )
