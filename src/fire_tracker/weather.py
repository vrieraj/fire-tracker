"""
Geocoding and elevation module.

Uses Nominatim for forward/reverse geocoding and Open-Meteo for elevation lookup.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
_NOMINATIM_REVERSE_URL = 'https://nominatim.openstreetmap.org/reverse'
_ELEVATION_URL = 'https://api.open-meteo.com/v1/elevation'
_UA = 'fire-tracker/1.0'
_TIMEOUT = 10

_last_nominatim_ts = 0.0


def _respect_rate_limit():
    global _last_nominatim_ts
    elapsed = time.time() - _last_nominatim_ts
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_nominatim_ts = time.time()


@dataclass
class Location:
    """Geocoded location with coordinates and metadata."""

    name: str
    latitude: float
    longitude: float
    elevation: float = 0.0
    municipality: str = ''
    province: str = ''
    region: str = ''
    country: str = ''


def _short_name(addr: dict) -> str:
    """Best-effort short place name from a Nominatim address block."""
    for key in ('municipality', 'town', 'city', 'village', 'hamlet', 'locality'):
        value = addr.get(key)
        if value:
            return value
    return ''


def _province(addr: dict) -> str:
    """Province/state-district from a Nominatim address block."""
    for key in ('province', 'state_district'):
        value = addr.get(key)
        if value:
            return value
    return addr.get('county', '')


def _region(addr: dict) -> str:
    """Region/autonomous community from a Nominatim address block."""
    for key in ('state', 'region'):
        value = addr.get(key)
        if value:
            return value
    return addr.get('county', '')


def geocode(query: str) -> Location | None:
    """Forward geocode using Nominatim."""
    _respect_rate_limit()
    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={'q': query, 'format': 'jsonv2', 'limit': 1, 'addressdetails': 1},
            headers={'User-Agent': _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        logger.error('Geocoding error: %s', e)
        return None

    if not results:
        return None

    r = results[0]
    lat, lon = float(r['lat']), float(r['lon'])
    elevation = get_elevation(lat, lon)
    addr = r.get('address', {}) or {}
    name = _short_name(addr) or r.get('display_name', query)

    return Location(
        name=name,
        latitude=lat,
        longitude=lon,
        elevation=elevation,
        municipality=name,
        province=_province(addr),
        region=_region(addr),
        country=addr.get('country_code', '').upper(),
    )


def reverse_geocode(latitude: float, longitude: float) -> Location | None:
    """Reverse geocode coordinates to location name using Nominatim."""
    _respect_rate_limit()
    try:
        resp = requests.get(
            _NOMINATIM_REVERSE_URL,
            params={'lat': latitude, 'lon': longitude, 'format': 'jsonv2'},
            headers={'User-Agent': _UA},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error('Reverse geocoding error: %s', e)
        return None

    addr = data.get('address', {}) or {}
    name = _short_name(addr) or data.get('display_name', '')

    return Location(
        name=name,
        latitude=latitude,
        longitude=longitude,
        elevation=get_elevation(latitude, longitude),
        municipality=name,
        province=_province(addr),
        region=_region(addr),
        country=addr.get('country_code', '').upper(),
    )


def get_elevation(latitude: float, longitude: float) -> float:
    """Get elevation using Open-Meteo API."""
    try:
        resp = requests.get(
            _ELEVATION_URL,
            params={'latitude': latitude, 'longitude': longitude},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        elevations = data.get('elevation', [])
        return float(elevations[0]) if elevations else 0.0
    except Exception:
        return 0.0
