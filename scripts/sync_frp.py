#!/usr/bin/env python3
"""
Sync FRP detections from LSA SAF to SQLite database.

Run before the monitor so locate_fire() can find satellite detections.

Usage:
    python scripts/sync_frp.py
    python scripts/sync_frp.py --hours 12
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[1]
if str(_root / 'src') not in sys.path:
    sys.path.insert(0, str(_root / 'src'))

from fire_tracker.database import FireDatabase
from fire_tracker.frp import fetch_frp

logger = logging.getLogger(__name__)

_DB_PATH = _root / 'data' / 'fires.db'


def sync_frp(hours: int = 24) -> int:
    """Fetch FRP from LSA SAF, store in DB. Returns number of rows inserted."""
    db = FireDatabase(_DB_PATH)

    existing = db.get_frp_detections(hours=hours)
    if existing:
        logger.info("FRP DB already has %d detections (last %dh)", len(existing), hours)
        return 0

    logger.info("Fetching FRP detections from LSA SAF...")
    result = fetch_frp()
    features = result.get('features', [])
    if not features:
        logger.warning("No FRP features returned")
        return 0

    db_rows = []
    for f in features:
        p = f['properties']
        db_rows.append({
            'longitude': f['geometry']['coordinates'][0],
            'latitude': f['geometry']['coordinates'][1],
            'frp_mw': p['frp_mw'],
            'confidence': p['confidence'],
            'frp_uncertainty': p['frp_uncertainty'],
            'pixel_size_km2': p['pixel_size_km2'],
            'acquisition_time': p['acquisition_time'],
            'bt_mir': p['bt_mir'],
            'bt_tir': p['bt_tir'],
        })

    inserted = db.insert_frp_detections(db_rows)
    logger.info("Stored %d FRP detections in DB", inserted)
    return inserted


def main():
    parser = argparse.ArgumentParser(description='Sync FRP detections from LSA SAF')
    parser.add_argument('--hours', type=int, default=24, help='Hours back (default: 24)')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    n = sync_frp(hours=args.hours)
    print(f"Inserted {n} FRP detections")


if __name__ == '__main__':
    main()
