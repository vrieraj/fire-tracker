"""
FRP satellite data updater — standalone for GitHub Actions.

Downloads FRP-PIXEL data from LSA SAF for last 2 hours,
inserts new detections into Supabase.

Usage:
    python -m fire_tracker.frp_update
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

_root = Path(__file__).resolve().parents[2]
load_dotenv(_root / '.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def run_frp_update(hours: int = 3) -> dict:
    from fire_tracker.frp import _list_csv_urls, _download_csv, _parse_csv
    from fire_tracker.database import FireDatabase

    db = FireDatabase(_root / 'data' / 'fires.db')
    now = datetime.now(timezone.utc)

    all_urls = []
    for h in range(hours):
        d = now - timedelta(hours=h)
        all_urls.extend(_list_csv_urls(d))
    logger.info('FRP: %d CSV files to check (last %dh)', len(all_urls), hours)

    seen = set()
    detections = []

    def fetch_one(url):
        csv_text = _download_csv(url)
        if csv_text:
            return _parse_csv(csv_text)
        return []

    batch_size = 30
    for i in range(0, len(all_urls), batch_size):
        batch = all_urls[i:i+batch_size]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(fetch_one, u): u for u in batch}
            for f in as_completed(futures, timeout=30):
                try:
                    for d in f.result(timeout=10):
                        key = (round(d.longitude, 4), round(d.latitude, 4), d.acquisition_time)
                        if key not in seen:
                            seen.add(key)
                            detections.append(d)
                except Exception:
                    pass

    if detections:
        db_rows = [{
            'longitude': d.longitude, 'latitude': d.latitude,
            'frp_mw': d.frp_mw, 'confidence': d.confidence,
            'frp_uncertainty': d.frp_uncertainty, 'pixel_size_km2': d.pixel_size_km2,
            'acquisition_time': d.acquisition_time.isoformat(),
            'bt_mir': d.bt_mir, 'bt_tir': d.bt_tir,
        } for d in detections]
        inserted = db.insert_frp_detections(db_rows)
    else:
        inserted = 0

    # Purge old detections (>7 days)
    deleted = db.purge_frp_detections(hours=168)
    total_db = db.count_frp_detections(hours=168)
    stats = {
        'detections_fetched': len(detections),
        'inserted': inserted,
        'deleted': deleted,
        'detections_in_db': total_db,
    }
    logger.info('FRP: fetched=%d, inserted=%d, total_24h=%d',
                len(detections), inserted, total_db)
    return stats


def main():
    stats = run_frp_update()
    print(f"\n--- FRP Update ---")
    print(f"  Fetched:     {stats['detections_fetched']}")
    print(f"  Inserted:    {stats['inserted']}")
    print(f"  Deleted (>7d): {stats['deleted']}")
    print(f"  Total (7d):  {stats['detections_in_db']}")


if __name__ == '__main__':
    main()
