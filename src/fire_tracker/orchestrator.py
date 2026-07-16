"""
Fire orchestrator — runs all scrapers, deduplicates, persists to SQLite.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path

from fire_tracker.database import FireDatabase
from fire_tracker.scrapers import (
    FeuxDeForetFrScraper,
    FogosPtScraper,
    FidiasClmScraper,
    IncendiscatCatScraper,
    IncendiosCyLScraper,
    InfocaAndaluciaScraper,
)

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).resolve().parents[2] / 'data' / 'fires.db'
_DEDUP_RADIUS_M = 500
_DEDUP_TIME_HOURS = 3


class FireOrchestrator:
    def __init__(self, db_path: str | Path = _DB_PATH):
        self.db = FireDatabase(db_path)
        self.scrapers = [
            InfocaAndaluciaScraper(),
            FeuxDeForetFrScraper(),
            IncendiscatCatScraper(),
            FogosPtScraper(),
            IncendiosCyLScraper(),
            FidiasClmScraper(),
        ]

    def run(self) -> dict:
        start = datetime.now(timezone.utc)
        all_incidents: list = []
        stats = {'sources': {}, 'total_raw': 0, 'total_after_dedup': 0,
                 'errors': []}

        for scraper in self.scrapers:
            try:
                incidents = scraper.fetch()
                stats['sources'][scraper.source] = len(incidents)
                all_incidents.extend(incidents)
            except Exception as e:
                logger.exception('Error in scraper %s', scraper.source)
                stats['errors'].append({'source': scraper.source, 'error': str(e)})

        stats['total_raw'] = len(all_incidents)

        deduped = self._deduplicate(all_incidents)
        stats['total_after_dedup'] = len(deduped)

        upserted = 0
        for incident in deduped:
            try:
                self.db.upsert(incident.to_dict())
                upserted += 1
            except Exception as e:
                logger.exception('Error persisting %s:%s',
                                 incident.source, incident.external_id)
                stats['errors'].append({
                    'source': incident.source,
                    'external_id': incident.external_id,
                    'error': str(e),
                })

        stats['upserted'] = upserted
        stats['total_in_db'] = self.db.count()
        stats['duration_s'] = (datetime.now(timezone.utc) - start).total_seconds()

        self.db.purge_extinguished(older_than_days=7)

        logger.info(
            'Orchestrator: %d raw -> %d dedup -> %d upserted (DB: %d total) in %.1fs',
            stats['total_raw'], stats['total_after_dedup'],
            stats['upserted'], stats['total_in_db'], stats['duration_s'],
        )
        return stats

    def _deduplicate(self, incidents: list) -> list:
        if len(incidents) < 2:
            return incidents

        kept: list = []
        discarded = set()

        for i, inc_i in enumerate(incidents):
            if i in discarded:
                continue
            for j, inc_j in enumerate(incidents):
                if j <= i or j in discarded:
                    continue
                if self._are_same(inc_i, inc_j):
                    merged = self._merge(inc_i, inc_j)
                    inc_i = merged
                    discarded.add(j)
            kept.append(inc_i)

        return kept

    @staticmethod
    def _are_same(a, b) -> bool:
        if a.source == b.source and a.external_id == b.external_id:
            return True
        if a.country != b.country:
            return False
        dist = FireOrchestrator._haversine(a.latitude, a.longitude,
                                           b.latitude, b.longitude)
        if dist > _DEDUP_RADIUS_M:
            return False
        if a.detection_date and b.detection_date:
            delta_h = abs((a.detection_date - b.detection_date).total_seconds()) / 3600
            return delta_h <= _DEDUP_TIME_HOURS
        return True

    @staticmethod
    def _merge(a, b):
        for field in ('municipality', 'province', 'region', 'fire_type',
                      'area_ha', 'source_url'):
            if not getattr(a, field) and getattr(b, field):
                setattr(a, field, getattr(b, field))

        if b.resources and not a.resources:
            a.resources = b.resources
        a.raw_data = {'merged': [a.raw_data, b.raw_data]}
        return a

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2) -> float:
        if None in (lat1, lon1, lat2, lon2):
            return float('inf')
        R = 6_371_000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a_val = (math.sin(dphi / 2) ** 2
                 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a_val), math.sqrt(1 - a_val))


def run_orchestrator():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    orch = FireOrchestrator()
    stats = orch.run()
    print(f'\nSummary:')
    print(f'  Sources:')
    for src, count in stats['sources'].items():
        print(f'    {src}: {count} incidents')
    print(f'  Total raw: {stats["total_raw"]}')
    print(f'  After dedup: {stats["total_after_dedup"]}')
    print(f'  Upserted: {stats["upserted"]}')
    print(f'  Total DB: {stats["total_in_db"]}')
    print(f'  Duration: {stats["duration_s"]:.1f}s')
    if stats['errors']:
        print(f'  Errors: {len(stats["errors"])}')
    return stats


if __name__ == '__main__':
    run_orchestrator()
