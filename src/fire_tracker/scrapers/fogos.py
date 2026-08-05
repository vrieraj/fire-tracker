"""
fogos.pt — Portugal fire tracking REST API.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fire_tracker.scrapers.base import FireIncident, FireScraper

logger = logging.getLogger(__name__)

_FOGOS_ACTIVE_URL = 'https://source.fogos.pt/v2/incidents/active'

_STATUS_MAP = {
    3: 'active',
    4: 'active',
    5: 'active',
    6: 'active',
    7: 'controlled',
    8: 'extinguished',
    9: 'stabilized',
    10: 'extinguished',
    11: 'false_alarm',
    12: 'false_alarm',
}


class FogosPtScraper(FireScraper):
    source = 'fogos.pt'

    def fetch(self) -> list[FireIncident]:
        params = {'geojson': '1'}
        try:
            resp = self._get(_FOGOS_ACTIVE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error('fogos.pt fetch error: %s', e)
            return []

        incidents = []
        features = data.get('features', []) if isinstance(data, dict) else data

        if not isinstance(features, list):
            features = data.get('data', [])
            if not isinstance(features, list):
                logger.warning('fogos.pt unexpected response format')
                return []

        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get('properties', feat)
            if not isinstance(props, dict):
                continue

            geom = feat.get('geometry') if feat.get('type') == 'Feature' else None
            if isinstance(geom, dict):
                coords = geom.get('coordinates')
            else:
                coords = None
            if not isinstance(coords, list) or len(coords) < 2:
                coords = [None, None]

            fire_id = str(props.get('id', props.get('_id', '')) or '')
            if not fire_id:
                continue

            lat = props.get('lat', coords[1])
            lon = props.get('lng', coords[0])
            if lat is None and coords:
                try:
                    lon, lat = float(coords[0]), float(coords[1])
                except (TypeError, IndexError, ValueError):
                    continue
            if lat is None or lon is None:
                continue

            status_code = props.get('statusCode')
            status = _STATUS_MAP.get(status_code, 'unknown')

            natureza = props.get('natureza') or ''
            fire_type = 'forestal' if 'incendio' in str(natureza).lower() else None

            detection = None
            dt = props.get('dateTime')
            if dt:
                ts = dt.get('sec') if isinstance(dt, dict) else dt
                try:
                    detection = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                except (ValueError, OSError, TypeError):
                    pass

            resources = {
                'aerial': props.get('aerial'),
                'man': props.get('man'),
                'terrain': props.get('terrain'),
            }

            area_ha = None
            area_data = props.get('icnf')
            if isinstance(area_data, dict):
                burn_area = area_data.get('burnArea')
                if isinstance(burn_area, dict):
                    area_ha = burn_area.get('total')

            source_url = props.get('url') or f'https://fogos.pt/pt/fogo/{fire_id}/detalhe'

            incidents.append(FireIncident(
                source=self.source,
                external_id=fire_id,
                source_url=source_url,
                latitude=lat,
                longitude=lon,
                municipality=props.get('concelho') or props.get('location'),
                province=props.get('district'),
                region=props.get('regiao'),
                country='PT',
                status=status,
                fire_type=fire_type,
                detection_date=detection,
                area_ha=area_ha,
                resources=resources if any(v is not None for v in resources.values()) else None,
                raw_data=props,
            ))

        logger.info('fogos.pt: %d active incidents', len(incidents))
        return incidents
