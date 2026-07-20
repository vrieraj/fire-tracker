"""
METAR station discovery and current observations.

Source: aviationweather.gov (NOAA) — free, no auth, JSON API.

Provides professional-grade aviation weather observations from ~100 stations
across Spain, Portugal, and France. Each METAR report includes temperature,
dewpoint, wind, pressure, visibility, and flight category.

Output format matches WU PWS station dicts for consistent frontend rendering.
"""

from __future__ import annotations

import time
from typing import Any

import requests

_BASE = "https://aviationweather.gov/api/data"

_HEADERS = {
    "User-Agent": "FireTracker/1.0 (wildfire weather aggregator)",
    "Accept": "application/json",
}

_TIMEOUT = 15

# Iberia + France bounding box (excludes North Africa, Italy, Switzerland, Germany)
_BBOX = "38,-12,47,7"


def fetch_metar_stations() -> list[dict]:
    """
    Fetch all METAR stations in ES/PT/FR with current observations.

    Returns a list of station dicts compatible with WU PWS format:
        stationId, platform, lat, lon, name, adm1, country, elev_m,
        temp_c, humidity_pct, windspeed_kmh, windgust_kmh, pressure_hpa,
        rain_daily_mm (None), distance_km (None)
    """
    # Step 1: Get station info
    stations = _fetch_station_info()
    if not stations:
        return []

    # Step 2: Get current METAR observations for all stations
    icao_list = [s["stationId"] for s in stations]
    observations = _fetch_metar_batch(icao_list)

    # Step 3: Merge station info with observations
    result = []
    for s in stations:
        icao = s["stationId"]
        obs = observations.get(icao)
        if obs is None:
            continue  # skip stations without current METAR

        s["temp_c"] = obs.get("temp")
        s["humidity_pct"] = _calc_humidity(obs.get("temp"), obs.get("dewp"))
        s["windspeed_kmh"] = _knots_to_kmh(obs.get("wspd"))
        s["windgust_kmh"] = _knots_to_kmh(obs.get("wgst"))
        s["winddir_avg"] = obs.get("wdir")
        s["pressure_hpa"] = obs.get("altim")
        s["rain_daily_mm"] = None  # METAR doesn't report rainfall
        s["distance_km"] = None
        s["fltCat"] = obs.get("fltCat", "")
        s["visibility"] = obs.get("visib", "")
        s["metar_raw"] = obs.get("rawOb", "")
        s["metar_time"] = obs.get("reportTime", "")
        result.append(s)

    return result


def _fetch_station_info() -> list[dict]:
    """Fetch station metadata for all METAR stations in the bbox."""
    url = f"{_BASE}/stationinfo?bbox={_BBOX}&format=json"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    stations = []
    for s in data:
        icao = s.get("icaoId", "")
        if not icao:
            continue

        # Determine country from ICAO prefix
        prefix = icao[:2]
        country_map = {
            "LE": ("ES", "España"),
            "LP": ("PT", "Portugal"),
            "LF": ("FR", "Francia"),
            "LS": ("CH", "Suiza"),
        }
        cc, country_name = country_map.get(prefix, ("", ""))

        # Determine region (adm1) from ICAO or state field
        state = s.get("state", "")

        stations.append({
            "stationId": icao,
            "iataId": s.get("iataId", ""),
            "platform": "METAR",
            "lat": s.get("lat"),
            "lon": s.get("lon"),
            "name": s.get("site", icao),
            "adm1": state,
            "country": cc,
            "country_name": country_name,
            "elev_m": s.get("elev"),
            "siteType": s.get("siteType", []),
        })

    return stations


def _fetch_metar_batch(icao_list: list[str]) -> dict[str, dict]:
    """
    Fetch current METAR for multiple stations in batches.
    aviationweather.gov allows up to 400 IDs per request.
    """
    observations = {}
    batch_size = 100  # conservative batch size

    for i in range(0, len(icao_list), batch_size):
        batch = icao_list[i:i + batch_size]
        ids_str = ",".join(batch)
        url = f"{_BASE}/metar?ids={ids_str}&format=json"

        try:
            r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            if r.status_code == 200:
                for obs in r.json():
                    icao = obs.get("icaoId")
                    if icao:
                        observations[icao] = obs
        except Exception:
            pass

        if i + batch_size < len(icao_list):
            time.sleep(0.5)  # respect rate limits

    return observations


def _calc_humidity(temp_c: float | None, dewp_c: float | None) -> float | None:
    """Calculate relative humidity from temperature and dewpoint (Magnus formula)."""
    if temp_c is None or dewp_c is None:
        return None
    a, b = 17.27, 237.7
    alpha_t = (a * temp_c) / (b + temp_c)
    alpha_td = (a * dewp_c) / (b + dewp_c)
    rh = 100 * (2.71828 ** (alpha_td - alpha_t))
    return round(min(max(rh, 0), 100), 0)


def _knots_to_kmh(knots: float | None) -> float | None:
    """Convert knots to km/h."""
    if knots is None:
        return None
    return round(knots * 1.852, 1)
