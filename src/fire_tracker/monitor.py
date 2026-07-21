"""
Fire monitor — Spain-only.

Searches X.com for #IF hashtags and official emergency accounts,
locates fires with FRP, saves to DB.

Usage:
    python -m fire_tracker.monitor
    python -m fire_tracker.monitor --hours-back 4 --limit 30
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_root = Path(__file__).resolve().parents[2]
load_dotenv(_root / '.env')
if str(_root / 'src') not in sys.path:
    sys.path.insert(0, str(_root / 'src'))

from fire_tracker.database import FireDatabase
from fire_tracker.xmonitor import search_fire_tweets, XTweet, extract_if_municipality
from fire_tracker.frp_locator import locate_fire, FireLocation

logger = logging.getLogger(__name__)

_DB_PATH = _root / 'data' / 'fires.db'

_IBERIA_BBOX = {'lat_min': 34.0, 'lat_max': 44.5, 'lon_min': -10.0, 'lon_max': 5.0}


def _in_iberia(lat: float, lon: float) -> bool:
    b = _IBERIA_BBOX
    return b['lat_min'] <= lat <= b['lat_max'] and b['lon_min'] <= lon <= b['lon_max']


def _extract_if_hashtag(text: str) -> str | None:
    import re
    match = re.search(r'#IF([A-ZÁÉÍÓÚÑ][a-záéíóúñA-ZÁÉÍÓÚÑ]*)', text)
    if match:
        return f"#IF{match.group(1)}"
    return None


def _is_duplicate(db: FireDatabase, tweet: XTweet, location: FireLocation) -> bool:
    existing = db.get_fire("xmonitor", f"x_{tweet.tweet_id}")
    if existing:
        return True

    fires = db.get_active_fires()
    for f in fires:
        if f.get('municipality') and f['municipality'] == location.municipality:
            return True

    return False


def _add_fire(db: FireDatabase, tweet: XTweet, location: FireLocation) -> bool:
    import urllib.parse

    hashtag = _extract_if_hashtag(tweet.text)
    chronology_url = ""
    if hashtag:
        chronology_url = f"https://x.com/search?q={urllib.parse.quote(hashtag)}&src=typed_query&f=live"

    raw_data = {
        'tweet_text': tweet.text,
        'tweet_author': tweet.author_handle,
        'tweet_likes': tweet.likes,
        'tweet_retweets': tweet.retweets,
        'frp_count': location.frp_count,
        'frp_max_mw': location.frp_max_mw,
        'location_source': location.source,
        'location_confidence': location.confidence,
        'chronology_url': chronology_url,
    }

    fire = {
        'source': 'xmonitor',
        'external_id': f"x_{tweet.tweet_id}",
        'source_url': f"https://x.com/{tweet.author_handle}/status/{tweet.tweet_id}",
        'latitude': location.latitude,
        'longitude': location.longitude,
        'municipality': location.municipality,
        'province': location.province,
        'region': location.region,
        'country': location.country,
        'status': 'active',
        'fire_type': 'wildfire',
        'detection_date': tweet.created_at.isoformat(),
        'last_updated': datetime.now(timezone.utc).isoformat(),
        'raw_data': raw_data,
    }

    db.upsert(fire)
    return True


def _process_tweets(db: FireDatabase, tweets: list[XTweet], stats: dict):
    for tweet in tweets:
        try:
            municipality = extract_if_municipality(tweet.text)
            if not municipality:
                municipality = tweet.location_mention

            if not municipality:
                stats['geocode_failures'] += 1
                logger.debug("No municipality found in tweet %s", tweet.tweet_id)
                continue

            location = locate_fire(
                municipality=municipality,
                lat=None,
                lon=None,
                db_path=_DB_PATH,
            )

            if not location:
                stats['geocode_failures'] += 1
                logger.warning("Could not locate fire from tweet %s", tweet.tweet_id)
                continue

            if not _in_iberia(location.latitude, location.longitude):
                logger.debug("Outside Iberia: %s (%.2f, %.2f)", municipality, location.latitude, location.longitude)
                stats['outside_region'] = stats.get('outside_region', 0) + 1
                continue

            if _is_duplicate(db, tweet, location):
                stats['duplicates'] += 1
                logger.debug("Duplicate: tweet %s", tweet.tweet_id)
                continue

            _add_fire(db, tweet, location)
            stats['new_fires'] += 1
            logger.info(
                "NEW FIRE: %s (%s) via @%s — FRP: %d detections, %.1f MW",
                location.municipality, location.province,
                tweet.author_handle, location.frp_count, location.frp_max_mw,
            )

        except Exception as e:
            logger.error("Error processing tweet %s: %s", tweet.tweet_id, e)
            continue


def run_monitor(*, hours_back: int = 2, limit_per_query: int = 20) -> dict:
    """
    Run one monitoring cycle — Spain only.

    Flow:
      1. Search #IF hashtags + official emergency accounts
      2. Process each tweet → locate with FRP → save to DB
    """
    stats = {
        'new_fires': 0,
        'duplicates': 0,
        'geocode_failures': 0,
        'outside_region': 0,
        'tweets_found': 0,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    db = FireDatabase(_DB_PATH)

    import asyncio
    tweets = asyncio.run(search_fire_tweets(
        limit_per_query=limit_per_query,
        hours_back=hours_back,
    ))
    stats['tweets_found'] = len(tweets)
    logger.info("Found %d tweets", len(tweets))

    if tweets:
        _process_tweets(db, tweets, stats)

    logger.info(
        "Monitor complete: %d fires, %d duplicates, %d geocode failures",
        stats['new_fires'], stats['duplicates'], stats['geocode_failures'],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description='Fire monitor — Spain only')
    parser.add_argument('--hours-back', type=int, default=2,
                        help='Hours back to search (default: 2)')
    parser.add_argument('--limit', type=int, default=20,
                        help='Max tweets per query (default: 20)')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    stats = run_monitor(
        hours_back=args.hours_back,
        limit_per_query=args.limit,
    )

    print(f"\n--- Monitor Results ---")
    print(f" tweets found:    {stats['tweets_found']}")
    print(f" new fires:       {stats['new_fires']}")
    print(f" duplicates:      {stats['duplicates']}")
    print(f" outside region:  {stats.get('outside_region', 0)}")
    print(f" geocode failures: {stats['geocode_failures']}")
    print(f" timestamp:       {stats['timestamp']}")


if __name__ == '__main__':
    main()
