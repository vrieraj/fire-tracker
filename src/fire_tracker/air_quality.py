from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

_STATIONS_INDEX_URL = (
    'https://dis2datalake.blob.core.windows.net/'
    'airquality-derivated/AQI-noRunningMeans/content/index.json'
)
_STATIONS_DATA_URL = (
    'https://dis2datalake.blob.core.windows.net/'
    'airquality-derivated/AQI-noRunningMeans/content/'
)
_AQI_MAP_URL = (
    'https://dis2datalake.blob.core.windows.net/'
    'airquality-derivated/AQI-noRunningMeans/map/'
)

_COUNTRIES = ('ES', 'PT', 'FR')

_AQI_BANDS = {
    1: {'name': 'Good', 'color': '#50f0e6'},
    2: {'name': 'Fair', 'color': '#50ccaa'},
    3: {'name': 'Moderate', 'color': '#f0e641'},
    4: {'name': 'Poor', 'color': '#ff5050'},
    5: {'name': 'Very poor', 'color': '#960032'},
    6: {'name': 'Extremely poor', 'color': '#7D2181'},
}

_TIMEOUT = 15
_CACHE_TTL = 3600

_cache = {'stations': None, 'stations_ts': 0.0}


def _get_station_metadata() -> list[dict]:
    now = time.time()
    if _cache['stations'] and (now - _cache['stations_ts']) < _CACHE_TTL:
        return _cache['stations']

    try:
        resp = requests.get(_STATIONS_INDEX_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        index = resp.json()
        files = [f for f in index.get('contents', []) if f.startswith('raw_stations.json')]
        if not files:
            logger.error('No raw_stations.json files found in index')
            return []
        files.sort()
        latest = files[-1]
        url = _STATIONS_DATA_URL + latest
        resp2 = requests.get(url, timeout=_TIMEOUT)
        resp2.raise_for_status()
        stations = resp2.json()
        _cache['stations'] = stations
        _cache['stations_ts'] = now
        return stations
    except Exception as e:
        logger.error('Failed to fetch station metadata: %s', e)
        if _cache['stations']:
            return _cache['stations']
        return []


def _get_aqi_map() -> dict:
    now = datetime.now(timezone.utc)
    hour_key = now.strftime('%Y-%m-%dT%H')
    url = _AQI_MAP_URL + f'{hour_key}.json'
    try:
        resp = requests.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error('Failed to fetch AQI map for %s: %s', hour_key, e)
        return {}


def fetch_aqi_stations() -> dict:
    stations = _get_station_metadata()
    if not stations:
        return _empty_geojson('No station metadata available')

    aqi_map = _get_aqi_map()
    now_iso = datetime.now(timezone.utc).isoformat()

    features = []
    for st in stations:
        code: str = st.get('code', '')
        country = code[:2] if len(code) >= 2 else ''
        if country not in _COUNTRIES:
            continue

        lon = st.get('lon')
        lat = st.get('lat')
        if lon is None or lat is None:
            continue

        aqi_val = aqi_map.get(code)
        band_id = int(aqi_val) if aqi_val is not None and aqi_val > 0 else 0
        band = _AQI_BANDS.get(band_id)

        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [lon, lat],
            },
            'properties': {
                'code': code,
                'name': st.get('name', ''),
                'station_type': st.get('station_type', ''),
                'area_classification': st.get('area_classification', ''),
                'municipality': st.get('municipality', ''),
                'altitude': st.get('altitude'),
                'aqi': aqi_val,
                'band_id': band_id,
                'band_name': band['name'] if band else 'No data',
                'color': band['color'] if band else '#6f6f6f',
                'network': st.get('network', ''),
            },
        })

    return {
        'type': 'FeatureCollection',
        'features': features,
        'metadata': {
            'source': 'European Environment Agency — Air Quality Index',
            'source_url': 'https://airindex.eea.europa.eu/AQI/index.html',
            'station_count': len(features),
            'timestamp': now_iso,
            'countries': list(_COUNTRIES),
        },
    }


def _empty_geojson(reason: str) -> dict:
    return {
        'type': 'FeatureCollection',
        'features': [],
        'metadata': {
            'source': 'European Environment Agency — Air Quality Index',
            'error': reason,
        },
    }
