"""
feuxdeforet.fr — Public GeoJSON + detail page scraping.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

from fire_tracker.scrapers.base import FireIncident, FireScraper

logger = logging.getLogger(__name__)

_FDF_GEOJSON_URL = 'https://feuxdeforet.fr/fdf/cartographie/geojson?scope=web'

_STATUS_MAP = {
    'attaque': 'active',
    'fixe': 'controlled',
    'maitrise': 'controlled',
    'eteint': 'extinguished',
    'non_confirme': 'active',
}

_DETAIL_SCRAPE_DELAY = 0.3


class FeuxDeForetFrScraper(FireScraper):
    source = 'feuxdeforet.fr'

    def fetch(self) -> list[FireIncident]:
        try:
            import requests as _req
            s = _req.Session()
            s.headers.update({
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7',
                'Referer': 'https://feuxdeforet.fr/fdf/cartographie',
            })
            s.get('https://feuxdeforet.fr/', timeout=15)
            resp = s.get(_FDF_GEOJSON_URL, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error('feuxdeforet.fr fetch error: %s', e)
            return []

        geojson = data.get('data', data)
        features = geojson.get('features', []) if isinstance(geojson, dict) else []
        if isinstance(data, list):
            features = data

        incidents: list[FireIncident] = []
        for feat in features:
            if not isinstance(feat, dict):
                continue
            props = feat.get('properties', {})
            if not props:
                continue
            geom = feat.get('geometry', {})
            coords = geom.get('coordinates', [None, None]) if geom else [None, None]

            try:
                lon, lat = float(coords[0]), float(coords[1])
            except (TypeError, IndexError, ValueError):
                continue

            raw_etat = props.get('etat', props.get('statut', ''))
            status = _STATUS_MAP.get(raw_etat,
                                     self._status_normalize(raw_etat, self.source))

            fire_id = str(props.get('id', ''))
            detail_url = props.get('url')

            incidents.append(FireIncident(
                source=self.source,
                external_id=fire_id,
                source_url=detail_url,
                latitude=lat,
                longitude=lon,
                municipality=None,
                province=None,
                region=None,
                country='FR',
                status=status,
                fire_type='forestal',
                detection_date=None,
                raw_data=props,
            ))

        enriched = 0
        for inc in incidents:
            if inc.municipality:
                continue
            detail = self._scrape_detail(inc.source_url, inc.external_id)
            if detail:
                if detail.get('municipality'):
                    inc.municipality = detail['municipality']
                if detail.get('province'):
                    inc.province = detail['province']
                if detail.get('region'):
                    inc.region = detail['region']
                if detail.get('detection_date'):
                    inc.detection_date = detail['detection_date']
                enriched += 1
            time.sleep(_DETAIL_SCRAPE_DELAY)

        if enriched:
            logger.info('feuxdeforet.fr: %d incidents, %d enriched',
                        len(incidents), enriched)
        else:
            logger.info('feuxdeforet.fr: %d incidents', len(incidents))
        return incidents

    def _scrape_detail(self, url: str | None,
                       fire_id: str) -> dict | None:
        if not url:
            return None

        try:
            resp = self._get(url)
            if resp.status_code != 200:
                return None
            html = resp.text
        except Exception:
            return None

        m = re.search(
            r'<script\s+type="application/ld\+json">(.*?)</script>',
            html, re.DOTALL,
        )
        if not m:
            return None

        try:
            ld = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

        graph = ld.get('@graph', [ld]) if isinstance(ld, dict) else []

        detail = {}
        for node in graph:
            ntype = node.get('@type', '')
            if ntype == 'NewsArticle':
                date_str = node.get('datePublished')
                if date_str:
                    try:
                        detail['detection_date'] = datetime.fromisoformat(
                            date_str.replace('Z', '+00:00')
                        )
                    except (ValueError, Exception):
                        pass

                loc = node.get('contentLocation', {})
                if isinstance(loc, dict):
                    detail['municipality'] = loc.get('name')
                    addr = loc.get('address', {})
                    if isinstance(addr, dict):
                        detail['province'] = addr.get('addressRegion')

            elif ntype == 'BreadcrumbList':
                items = node.get('itemListElement', [])
                names = [it.get('name', '') for it in items]
                if len(names) >= 3:
                    region_candidates = names[1:-1]
                    if len(region_candidates) >= 2:
                        detail['region'] = region_candidates[0]
                        detail['province'] = (
                            detail.get('province') or region_candidates[1]
                        )
                    elif len(region_candidates) == 1:
                        detail['region'] = region_candidates[0]

        return detail if detail else None
