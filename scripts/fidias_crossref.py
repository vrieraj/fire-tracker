#!/usr/bin/env python3
"""
FIDIAS CLM cross-reference script.

Combines:
1. FIDIAS HTML scraping (fire list with municipality/province/date)
2. incendiosespaña.es satellite data (GPS coordinates)
3. Nominatim geocoding (municipality centers as fallback)

Output: data/fidias_enriched.json
"""

from __future__ import annotations

import csv
import json
import logging
import re
import time
from datetime import datetime
from io import StringIO
from math import radians, sin, cos, sqrt, atan2
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_FIDIAS_URL = 'https://fidias.castillalamancha.es/consulta/forms/fidif001.php'
_FIDIAS_API_URL = 'https://incendiosespaña.es/api/fires'
_NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
_NOMINATIM_UA = 'FireTracker/0.1 (fidias-crossref-script)'
_DEDUP_RADIUS_M = 10_000


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def fetch_fidias_fires() -> list[dict[str, Any]]:
    resp = requests.get(
        _FIDIAS_URL,
        params={'auth': 'ANONIMO'},
        headers={'User-Agent': _NOMINATIM_UA},
        timeout=30,
    )
    resp.raise_for_status()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', resp.text, re.DOTALL)
    fires = []

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 6:
            continue

        code = re.sub(r'<[^>]+>', '', cells[0]).strip()
        if not code or not code.isdigit():
            continue

        def decode(text: str) -> str:
            return re.sub(
                r'&[a-zA-Z]+;',
                lambda m: {
                    '&aacute;': 'á', '&Aacute;': 'Á',
                    '&eacute;': 'é', '&Eacute;': 'É',
                    '&iacute;': 'í', '&Iacute;': 'Í',
                    '&oacute;': 'ó', '&Oacute;': 'Ó',
                    '&uacute;': 'ú', '&Uacute;': 'Ú',
                    '&ntilde;': 'ñ', '&Ntilde;': 'Ñ',
                    '&nbsp;': ' ',
                }.get(m.group(0), m.group(0)),
                re.sub(r'<[^>]+>', '', text)
            ).strip()

        province = decode(cells[1])
        municipality = decode(cells[2])
        detection = re.sub(r'<[^>]+>', '', cells[4]).strip()
        extinction = re.sub(r'<[^>]+>', '', cells[5]).strip()

        fires.append({
            'code': code,
            'province': province,
            'municipality': municipality,
            'detection': detection,
            'extinction': extinction if extinction != '---' else None,
        })

    return fires


def fetch_satellite_fires(days: int = 7) -> list[dict[str, Any]]:
    resp = requests.get(
        _FIDIAS_API_URL,
        params={'days': days},
        headers={'User-Agent': _NOMINATIM_UA},
        timeout=30,
    )
    resp.raise_for_status()

    reader = csv.DictReader(StringIO(resp.text))
    fires = []

    for row in reader:
        if row.get('satellite') != 'FIDIAS_CLM':
            continue

        try:
            lat = float(row.get('latitude', 0))
            lon = float(row.get('longitude', 0))
        except (TypeError, ValueError):
            continue

        if lat == 0 and lon == 0:
            continue

        fires.append({
            'latitude': lat,
            'longitude': lon,
            'acq_date': row.get('acq_date', ''),
            'acq_time': row.get('acq_time', '0000'),
            'instrument': row.get('instrument', ''),
        })

    return fires


def geocode_municipality(municipality: str, province: str) -> tuple[float, float] | None:
    muni_clean = municipality.split(',')[0].strip()
    query = f'{muni_clean}, {province}, Castilla-La Mancha, Spain'

    try:
        resp = requests.get(
            _NOMINATIM_URL,
            params={'q': query, 'format': 'json', 'limit': 1, 'countrycodes': 'es'},
            headers={'User-Agent': _NOMINATIM_UA},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]['lat']), float(results[0]['lon'])
    except Exception as e:
        logger.debug('Geocode error for %s: %s', query, e)

    return None


def match_fires(
    fidias_fires: list[dict],
    satellite_fires: list[dict],
) -> list[dict[str, Any]]:
    enriched = []

    by_municipality: dict[str, list[dict]] = {}
    for fire in fidias_fires:
        key = f'{fire["municipality"]}_{fire["province"]}'
        by_municipality.setdefault(key, []).append(fire)

    for key, fires in by_municipality.items():
        fires.sort(key=lambda f: f['detection'])

    used_satellite: set[int] = set()
    _MATCH_RADIUS_M = 5_000

    for key, fires in by_municipality.items():
        for idx, fire in enumerate(fires):
            fire_date_parts = fire['detection'].split('/')
            fire_date = f'{fire_date_parts[2]}-{fire_date_parts[1]}-{fire_date_parts[0]}' if len(fire_date_parts) == 3 else ''

            best_sat = None
            best_dist = float('inf')
            best_sat_idx = None

            for sat_idx, sat in enumerate(satellite_fires):
                if sat_idx in used_satellite:
                    continue

                sat_date = sat.get('acq_date', '')
                if fire_date and sat_date and fire_date != sat_date:
                    continue

                if fire.get('lat') and fire.get('lon'):
                    dist = _haversine(fire['lat'], fire['lon'], sat['latitude'], sat['longitude'])
                else:
                    dist = float('inf')

                if dist < best_dist:
                    best_dist = dist
                    best_sat = sat
                    best_sat_idx = sat_idx

            if best_sat and best_dist < _MATCH_RADIUS_M:
                fire['latitude'] = best_sat['latitude']
                fire['longitude'] = best_sat['longitude']
                fire['source'] = 'satellite'
                fire['status'] = best_sat.get('instrument', 'unknown')
                if best_sat_idx is not None:
                    used_satellite.add(best_sat_idx)
                logger.info('Match satellite: %s -> %.4f,%.4f (%.0fm)',
                           fire['municipality'], fire['latitude'], fire['longitude'], best_dist)
            else:
                fire['latitude'] = fire.get('lat', 0)
                fire['longitude'] = fire.get('lon', 0)
                fire['source'] = 'geocoded'
                fire['status'] = 'active'

            fire['order'] = idx + 1
            enriched.append(fire)

    return enriched


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    print('1. Scraping FIDIAS CLM...')
    fidias = fetch_fidias_fires()
    print(f'   {len(fidias)} fires found')

    print('2. Downloading satellite data (incendiosespaña.es)...')
    satellite = fetch_satellite_fires(days=7)
    print(f'   {len(satellite)} FIDIAS_CLM points')

    print('3. Geocoding municipalities...')
    geocoded: dict[str, tuple[float, float]] = {}
    unique_munis = set((f['municipality'], f['province']) for f in fidias)

    for muni, prov in unique_munis:
        coords = geocode_municipality(muni, prov)
        if coords:
            geocoded[f'{muni}_{prov}'] = coords
            print(f'   {muni} ({prov}): {coords[0]:.4f}, {coords[1]:.4f}')
        time.sleep(1.1)

    for fire in fidias:
        key = f'{fire["municipality"]}_{fire["province"]}'
        if key in geocoded:
            fire['lat'], fire['lon'] = geocoded[key]

    print('4. Matching fires...')
    enriched = match_fires(fidias, satellite)

    output_path = Path(__file__).resolve().parent.parent / 'data' / 'fidias_enriched.json'
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2)

    print(f'\nResult: {len(enriched)} enriched fires')
    print(f'Saved to: {output_path}')


if __name__ == '__main__':
    main()
