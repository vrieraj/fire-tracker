"""
EFFIS burnt area perimeter updater — standalone for GitHub Actions.

Downloads MODIS monthly burnt area perimeters from Copernicus EFFIS,
filters for ES/PT/FR, and upserts into Supabase.

Usage:
    python -m fire_tracker.effis_perimeters_update
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

_root = Path(__file__).resolve().parents[2]
load_dotenv(_root / '.env')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def run_perimeter_update() -> dict:
    """Download EFFIS perimeters and update the database."""
    from fire_tracker.effis_perimeters import download_shapefile_zip, parse_shapefile
    from fire_tracker.database import FireDatabase

    db = FireDatabase(_root / 'data' / 'fires.db')

    # Download Shapefile
    zip_bytes = download_shapefile_zip()
    if zip_bytes is None:
        return {'status': 'error', 'error': 'download failed'}

    # Parse and filter
    perimeters = parse_shapefile(zip_bytes)
    if not perimeters:
        return {'status': 'ok', 'perimeters_fetched': 0, 'upserted': 0, 'deleted': 0}

    # Get existing IDs for diff
    existing_ids = db.get_perimeter_ids()
    new_ids = {p['id'] for p in perimeters}

    # Delete perimeters no longer in the download
    deleted = 0
    for old_id in existing_ids - new_ids:
        db.delete_perimeter(old_id)
        deleted += 1

    # Upsert all perimeters from download
    upserted = db.upsert_perimeters(perimeters)

    total_db = len(db.get_perimeter_ids())
    stats = {
        'status': 'ok',
        'perimeters_fetched': len(perimeters),
        'upserted': upserted,
        'deleted': deleted,
        'total_in_db': total_db,
    }
    logger.info(
        'EFFIS: fetched=%d, upserted=%d, deleted=%d, total=%d',
        len(perimeters), upserted, deleted, total_db,
    )
    return stats


def main():
    stats = run_perimeter_update()
    print("\n--- EFFIS Perimeter Update ---")
    print(f"  Status:      {stats['status']}")
    print(f"  Fetched:     {stats.get('perimeters_fetched', 0)}")
    print(f"  Upserted:    {stats.get('upserted', 0)}")
    print(f"  Deleted:     {stats.get('deleted', 0)}")
    print(f"  Total in DB: {stats.get('total_in_db', 0)}")
    if stats.get('error'):
        print(f"  Error:       {stats['error']}")
    sys.exit(0 if stats['status'] == 'ok' else 1)


if __name__ == '__main__':
    main()
