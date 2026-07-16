"""
incendiscat.cat — REST API with HMAC-SHA256 authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone

from fire_tracker.scrapers.base import FireIncident, FireScraper

logger = logging.getLogger(__name__)

_API_BASE = 'https://incendiscat.cat/backend/api.php'
_HMAC_KEY = b'b19736034b642fc5a5f779e09f47f6287acb94e4160d1894'
_HMAC_WINDOW = 120

_STATUS_MAP = {
    'actiu': 'active',
    'controlat': 'controlled',
    'estabilitzat': 'stabilized',
    'extingit': 'extinguished',
}


class IncendiscatCatScraper(FireScraper):
    source = 'incendiscat.cat'

    def fetch(self) -> list[FireIncident]:
        incidents = []
        token = self._generate_token()

        for timespan_ms in [86_400_000, 259_200_000, 604_800_000]:
            try:
                headers = {'X-IC-Token': token}
                resp = self._get(
                    _API_BASE,
                    params={'timespan': timespan_ms},
                    headers=headers,
                )
                if resp.status_code == 403:
                    logger.warning(
                        'incendiscat.cat HMAC rejected (403) for timespan=%d ms',
                        timespan_ms,
                    )
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error('incendiscat.cat fetch error (timespan=%d ms): %s',
                             timespan_ms, e)
                continue

            if not isinstance(data, list):
                logger.warning(
                    'incendiscat.cat unexpected response type: %s', type(data)
                )
                continue

            for item in data:
                fire_id = str(item.get('id', ''))
                if not fire_id:
                    continue

                lat_str = item.get('latitude', '')
                lon_str = item.get('longitude', '')
                try:
                    lat = float(lat_str)
                    lon = float(lon_str)
                except (TypeError, ValueError):
                    continue

                raw_status = item.get('status', '')
                status = _STATUS_MAP.get(
                    raw_status,
                    self._status_normalize(raw_status, self.source),
                )

                raw_type = item.get('type', '')
                fire_type = 'forestal' if 'forestal' in raw_type.lower() else None

                detection = None
                ts = item.get('when_timestamp')
                if ts:
                    try:
                        detection = datetime.fromtimestamp(
                            int(ts) / 1000, tz=timezone.utc
                        )
                    except (ValueError, OSError):
                        pass

                ops = item.get('ops')
                resources = {'vehicles': ops} if ops is not None else None

                source_url = f'https://incendiscat.cat/detail.php?id={fire_id}'

                incidents.append(FireIncident(
                    source=self.source,
                    external_id=fire_id,
                    source_url=source_url,
                    latitude=lat,
                    longitude=lon,
                    municipality=item.get('where_geolocation'),
                    province=None,
                    region='Catalunya',
                    country='ES',
                    status=status,
                    fire_type=fire_type,
                    detection_date=detection,
                    resources=resources,
                    raw_data=item,
                ))
            break

        logger.info('incendiscat.cat: %d incidents', len(incidents))
        return incidents

    @staticmethod
    def _generate_token() -> str:
        w = int(time.time() / _HMAC_WINDOW)
        message = f'ic:{w}'.encode('ascii')
        signature = hmac.new(_HMAC_KEY, message, hashlib.sha256).hexdigest()
        return signature[:32]
