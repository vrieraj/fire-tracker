"""
FIDIAS_CLM — Castilla-La Mancha fire tracking.

Combines:
1. FIDIAS (fidias.castillalamancha.es) — fire list with municipality/province/date
2. incendiosespaña.es/api/fires — satellite GPS coordinates (FIDIAS_CLM)
3. Nominatim — municipality geocoding as fallback
"""

from __future__ import annotations

import csv
import logging
import re
import time
from datetime import datetime, timezone
from io import StringIO
from math import radians, sin, cos, sqrt, atan2
from typing import Any

import requests

from fire_tracker.scrapers.base import FireIncident, FireScraper

logger = logging.getLogger(__name__)

_FIDIAS_URL = 'https://fidias.castillalamancha.es/consulta/forms/fidif001.php'
_FIDIAS_API_URL = 'https://incendiosespaña.es/api/fires'
_NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
_NOMINATIM_UA = 'FireTracker/0.1 (fidias-clm-scraper)'
_MATCH_RADIUS_M = 5_000

_STATUS_MAP = {
    'Activo': 'active',
    'Controlado': 'controlled',
    'Estabilizado': 'stabilized',
    'Extinguido': 'extinguished',
    'Falsa Alarma': 'false_alarm',
}

_HTML_ENTITY_MAP = {
    '&aacute;': 'á', '&Aacute;': 'Á',
    '&eacute;': 'é', '&Eacute;': 'É',
    '&iacute;': 'í', '&Iacute;': 'Í',
    '&oacute;': 'ó', '&Oacute;': 'Ó',
    '&uacute;': 'ú', '&Uacute;': 'Ú',
    '&ntilde;': 'ñ', '&Ntilde;': 'Ñ',
    '&nbsp;': ' ',
}


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _decode_html(text: str) -> str:
    return re.sub(r'&[a-zA-Z]+;', lambda m: _HTML_ENTITY_MAP.get(m.group(0), m.group(0)), text)


class FidiasClmScraper(FireScraper):
    source = 'fidias_clm'

    def fetch(self) -> list[FireIncident]:
        fidias_fires = self._fetch_fidias()
        if not fidias_fires:
            logger.warning('fidias_clm: no FIDIAS data')
            return []

        satellite_fires = self._fetch_satellite()
        self._geocode_fires(fidias_fires)
        incidents = self._match_and_enrich(fidias_fires, satellite_fires)

        logger.info('fidias_clm: %d fires (%d satellite, %d geocoded)',
                     len(incidents),
                     sum(1 for i in incidents if i.resources.get('source') == 'satellite'),
                     sum(1 for i in incidents if i.resources.get('source') == 'geocoded'))
        return incidents

    def _fetch_fidias(self) -> list[dict[str, Any]]:
        try:
            resp = self._get(_FIDIAS_URL, params={'auth': 'ANONIMO'})
            resp.raise_for_status()
        except Exception as e:
            logger.error('fidias_clm FIDIAS fetch error: %s', e)
            return []

        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', resp.text, re.DOTALL)
        fires = []

        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) < 6:
                continue

            code = re.sub(r'<[^>]+>', '', cells[0]).strip()
            if not code or not code.isdigit():
                continue

            province = _decode_html(re.sub(r'<[^>]+>', '', cells[1])).strip()
            municipality = _decode_html(re.sub(r'<[^>]+>', '', cells[2])).strip()
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

    def _fetch_satellite(self) -> list[dict[str, Any]]:
        for days in (1, 3, 7):
            try:
                resp = self._get(_FIDIAS_API_URL, params={'days': days})
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

                if fires:
                    return fires
            except Exception as e:
                logger.debug('fidias_clm satellite fetch error (days=%d): %s', days, e)

        return []

    def _geocode_fires(self, fires: list[dict]) -> None:
        unique_munis = set((f['municipality'], f['province']) for f in fires)
        geocoded: dict[str, tuple[float, float]] = {}

        for muni, prov in unique_munis:
            query = f'{muni}, {prov}, Castilla-La Mancha, Spain'
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
                    coords = (float(results[0]['lat']), float(results[0]['lon']))
                    geocoded[f'{muni}_{prov}'] = coords
            except Exception as e:
                logger.debug('Geocode error for %s: %s', query, e)
            time.sleep(1.1)

        for fire in fires:
            key = f'{fire["municipality"]}_{fire["province"]}'
            if key in geocoded:
                fire['lat'], fire['lon'] = geocoded[key]

    def _match_and_enrich(
        self,
        fidias_fires: list[dict],
        satellite_fires: list[dict],
    ) -> list[FireIncident]:
        incidents = []

        by_municipality: dict[str, list[dict]] = {}
        for fire in fidias_fires:
            key = f'{fire["municipality"]}_{fire["province"]}'
            by_municipality.setdefault(key, []).append(fire)

        for fires in by_municipality.values():
            fires.sort(key=lambda f: f['detection'])

        used_satellite: set[int] = set()

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
                    lat, lon = best_sat['latitude'], best_sat['longitude']
                    source_type = 'satellite'
                    status_raw = best_sat.get('instrument', '')
                    if best_sat_idx is not None:
                        used_satellite.add(best_sat_idx)
                else:
                    lat = fire.get('lat', 0)
                    lon = fire.get('lon', 0)
                    source_type = 'geocoded'
                    status_raw = ''

                if lat == 0 and lon == 0:
                    continue

                status = _STATUS_MAP.get(status_raw.strip(),
                                         self._status_normalize(status_raw, self.source)) \
                    if status_raw else 'active'

                detection = self._parse_datetime(fire['detection'])
                extinction = self._parse_datetime(fire['extinction']) if fire.get('extinction') else None

                incident = FireIncident(
                    source=self.source,
                    external_id=fire['code'],
                    source_url='https://fidias.castillalamancha.es/',
                    latitude=lat,
                    longitude=lon,
                    municipality=fire['municipality'],
                    province=fire['province'],
                    region='Castilla-La Mancha',
                    country='ES',
                    status=status,
                    fire_type='forestal',
                    detection_date=detection,
                    extinction_date=extinction,
                    resources={'source': source_type, 'order': idx + 1},
                    raw_data=fire,
                )
                incidents.append(incident)

        return incidents

    @staticmethod
    def _parse_datetime(date_str: str | None) -> datetime | None:
        if not date_str:
            return None
        try:
            dt = datetime.strptime(date_str, '%d/%m/%Y')
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
