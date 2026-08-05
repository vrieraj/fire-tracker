"""One-off cleanup of historical xmonitor fires (created by the pre-v2 pipeline).

Applies the monitor v2 rules to the legacy rows:
  1. Reverse-geocode each fire to fix municipality/province/region/country.
  2. Fires in regions now covered by an official scraper -> extinguished
     (they duplicate official sources).
  3. Fires with no recent FRP and no recent activity -> extinguished
     (v2 lifecycle).
  4. Fires with live FRP evidence -> keep active, verified='frp', relocate
     to the FRP centroid and link the nearest EFFIS perimeter.
  5. Cluster survivors by DEDUP_RADIUS_M / DEDUP_WINDOW_HOURS, keeping the
     most recent fire and extinguishing the duplicates.

Usage:
    python scripts/xmonitor_cleanup.py            # dry run (no writes)
    python scripts/xmonitor_cleanup.py --apply    # apply to production DB
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_root = Path(__file__).resolve().parents[1]
load_dotenv(_root / '.env')
if str(_root / 'src') not in sys.path:
    sys.path.insert(0, str(_root / 'src'))

from fire_tracker.database import FireDatabase
from fire_tracker.frp_locator import locate_fire
from fire_tracker.geo import haversine
from fire_tracker.monitor import db_find_perimeter
from fire_tracker.xmonitor_config import (
    DEDUP_RADIUS_M,
    DEDUP_WINDOW_HOURS,
    EXPIRE_NO_TWEET_HOURS,
    is_monitored_region,
)

logger = logging.getLogger(__name__)

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _to_dt(value: str | None) -> datetime:
    if not value:
        return _EPOCH
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return _EPOCH


def _recent_hours(fire: dict) -> float:
    """Hours since the fire's last activity (last_updated as proxy)."""
    return (datetime.now(timezone.utc) - _to_dt(fire.get('last_updated'))).total_seconds() / 3600.0


def main() -> None:
    ap = argparse.ArgumentParser(description='Clean up legacy xmonitor fires')
    ap.add_argument('--apply', action='store_true', help='Apply changes (default: dry run)')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        datefmt='%H:%M:%S')

    db = FireDatabase()
    fires = db.get_fires_by_source('xmonitor')
    logger.info('xmonitor fires in DB: %d (%s)', len(fires), 'APPLY' if args.apply else 'DRY RUN')

    stats = {'covered_by_official': 0, 'stale': 0, 'keep': 0, 'merged': 0,
             'unclassified': 0, 'frp_relocated': 0, 'perimeters_linked': 0}

    # ── Pass 1: enrich + lifecycle ────────────────────────────────────────
    kept: list[dict] = []
    loc_cache: dict[tuple, object] = {}
    for i, fire in enumerate(fires, 1):
        lat, lon = fire.get('latitude'), fire.get('longitude')
        if lat is None or lon is None:
            stats['unclassified'] += 1
            continue

        cache_key = (round(lat, 4), round(lon, 4))
        loc = loc_cache.get(cache_key)
        if loc is None:
            loc = locate_fire(lat=lat, lon=lon, db_path=str(_root / 'data' / 'fires.db'))
            loc_cache[cache_key] = loc
        if i % 10 == 0:
            logger.info('Processed %d/%d fires (%d unique coords cached)',
                        i, len(fires), len(loc_cache))

        if loc is None or not loc.region:
            stats['unclassified'] += 1
            logger.warning('Unclassified (no region): %s', fire['external_id'])
            continue

        updates = {
            'municipality': loc.municipality,
            'province': loc.province or '',
            'region': loc.region,
            'country': loc.country or 'ES',
        }

        if not is_monitored_region(loc.region):
            stats['covered_by_official'] += 1
            logger.info('Covered by official source (%s): %s', loc.region, fire['external_id'])
            if args.apply:
                db.set_fire_status('xmonitor', fire['external_id'], 'extinguished')
            continue

        has_frp = loc.source == 'frp'
        recent = _recent_hours(fire) < EXPIRE_NO_TWEET_HOURS
        if not has_frp and not recent:
            stats['stale'] += 1
            logger.info('Stale (no FRP, no activity): %s', fire['external_id'])
            if args.apply:
                db.set_fire_status('xmonitor', fire['external_id'], 'extinguished')
            continue

        stats['keep'] += 1
        fire = dict(fire)
        fire.update(updates)
        if has_frp:
            stats['frp_relocated'] += 1
            fire['latitude'] = loc.latitude
            fire['longitude'] = loc.longitude
            fire['verified'] = 'frp'
            perimeter = db_find_perimeter(db, loc.latitude, loc.longitude)
            if perimeter:
                stats['perimeters_linked'] += 1
                raw = fire.get('raw_data') or {}
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError:
                        raw = {}
                raw['perimeter_id'] = perimeter.get('id')
                raw['perimeter_area_ha'] = perimeter.get('area_ha')
                raw['perimeter_commune'] = perimeter.get('commune')
                raw['perimeter_province'] = perimeter.get('province')
                fire['raw_data'] = raw

        if args.apply:
            db.upsert(fire)
        kept.append(fire)

    # ── Pass 2: cluster survivors (10 km / 48 h) ─────────────────────────
    kept.sort(key=lambda f: _to_dt(f.get('last_updated')), reverse=True)
    handled: set[str] = set()
    for seed in kept:
        if seed['external_id'] in handled:
            continue
        seed_dt = _to_dt(seed.get('last_updated'))
        for other in kept:
            if other['external_id'] in handled or other['external_id'] == seed['external_id']:
                continue
            other_dt = _to_dt(other.get('last_updated'))
            if abs((seed_dt - other_dt).total_seconds()) > DEDUP_WINDOW_HOURS * 3600:
                continue
            if haversine(seed['latitude'], seed['longitude'],
                         other['latitude'], other['longitude']) <= DEDUP_RADIUS_M:
                stats['merged'] += 1
                handled.add(other['external_id'])
                logger.info('Duplicate of %s (%.0f m): %s',
                            seed['external_id'],
                            haversine(seed['latitude'], seed['longitude'],
                                      other['latitude'], other['longitude']),
                            other['external_id'])
                if args.apply:
                    db.set_fire_status('xmonitor', other['external_id'], 'extinguished')
        handled.add(seed['external_id'])

    print('\n--- Cleanup summary ---')
    for key, value in stats.items():
        print(f' {key:20s} {value}')
    print(f' {"remaining active":20s} {len(kept) - stats["merged"]}')
    if not args.apply:
        print('\nDRY RUN: no changes written. Use --apply to write.')


if __name__ == '__main__':
    main()
