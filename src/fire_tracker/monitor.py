"""
Fire monitor — Spain, only regions without an official scraper.

Pipeline v2:
  1. Search X for #IF{name} hashtags (required declaration signal) and
     official emergency accounts (corroboration).
  2. Candidates MUST carry the official hashtag #IF{Municipality}. They are
     geocoded and must fall inside a Spanish autonomous community WITHOUT an
     official scraper in the viewer (Aragón, Madrid, C.Valenciana, Extremadura,
     País Vasco, Navarra, La Rioja, Cantabria, Asturias, Baleares, Murcia).
  3. Verification (balanced): FRP satellite evidence | official Tier-1
     account | >= 2 independent tweets | inside a recent EFFIS perimeter.
     Non-verified candidates are stored hidden as 'unverified'.
  4. Dedup: tweets of the same fire merge into ONE fire entity
     (radius + time window). Unverified entities are upgraded when verified.
  5. Perimeter: fires are linked to EFFIS burnt-area perimeters.
  6. Lifecycle: fires without FRP (8 h) AND without tweets (12 h) expire.

Usage:
    python -m fire_tracker.monitor
    python -m fire_tracker.monitor --hours-back 6 --limit 30
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
from fire_tracker.geo import haversine
from fire_tracker.xmonitor import search_fire_tweets, XTweet, extract_if_municipality
from fire_tracker.frp_locator import locate_fire, FireLocation
from fire_tracker.xmonitor_config import (
    DEDUP_RADIUS_M,
    DEDUP_WINDOW_HOURS,
    CROSS_SOURCE_RADIUS_M,
    VERIFY_FRP_RADIUS_KM,
    CORROBORATION_MIN_TWEETS,
    CORROBORATION_RADIUS_M,
    CORROBORATION_HOURS,
    EXPIRE_NO_FRP_HOURS,
    EXPIRE_NO_TWEET_HOURS,
    PERIMETER_MATCH_KM,
    PERIMETER_DATE_WINDOW_DAYS,
    TIER1_ACCOUNTS,
    is_monitored_region,
    SEARCH_HOURS_BACK,
)

logger = logging.getLogger(__name__)

_DB_PATH = _root / 'data' / 'fires.db'

_IBERIA_BBOX = {'lat_min': 34.0, 'lat_max': 44.5, 'lon_min': -10.0, 'lon_max': 5.0}

_ACTIVE_STATUSES = ('active', 'controlled', 'stabilized', 'declarado')

# Verification precedence (higher wins when merging).
_VERIFIED_PRIORITY = {'frp': 3, 'official': 2, 'corroborated': 1, None: 0}


def _in_iberia(lat: float, lon: float) -> bool:
    b = _IBERIA_BBOX
    return b['lat_min'] <= lat <= b['lat_max'] and b['lon_min'] <= lon <= b['lon_max']


def _extract_if_hashtag(text: str) -> str | None:
    import re
    match = re.search(r'#IF([A-ZÁÉÍÓÚÑ][a-záéíóúñA-ZÁÉÍÓÚÑ]*)', text)
    if match:
        return f"#IF{match.group(1)}"
    return None


def _tweet_raw(tweet: XTweet) -> dict:
    return {
        'id': tweet.tweet_id,
        'text': tweet.text,
        'author': tweet.author_handle,
        'created_at': tweet.created_at.isoformat(),
    }


def _latest_tweet_time(raw: dict) -> datetime | None:
    """Newest tweet creation time from a fire's raw_data."""
    tweets = (raw or {}).get('tweets') or []
    latest = None
    for t in tweets:
        try:
            ts = datetime.fromisoformat(t.get('created_at', '').replace('Z', '+00:00'))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _verify_candidate(
    db: FireDatabase,
    cand: dict,
    candidates: list[dict],
) -> str | None:
    """Return verification method: 'frp' | 'official' | 'corroborated' | None."""
    loc: FireLocation = cand['loc']
    tweet: XTweet = cand['tweet']

    if loc.source == 'frp' and loc.frp_count >= 1:
        return 'frp'

    if tweet.author_handle in TIER1_ACCOUNTS:
        return 'official'

    count = 1
    for other in candidates:
        if other is cand:
            continue
        try:
            t1 = cand['tweet'].created_at
            t2 = other['tweet'].created_at
            if abs((t1 - t2).total_seconds()) > CORROBORATION_HOURS * 3600:
                continue
        except (TypeError, AttributeError):
            continue
        if haversine(
            loc.latitude, loc.longitude,
            other['loc'].latitude, other['loc'].longitude,
        ) <= CORROBORATION_RADIUS_M:
            count += 1
            if count >= CORROBORATION_MIN_TWEETS:
                return 'corroborated'

    if db.find_perimeter_near(
        loc.latitude, loc.longitude,
        PERIMETER_MATCH_KM, PERIMETER_DATE_WINDOW_DAYS,
    ):
        return 'corroborated'

    return None


def _resolve_fire(db: FireDatabase, cand: dict, verified: str | None):
    """Find the fire entity a candidate belongs to.

    Returns (existing_fire_or_None, action) where action is one of
    'merged', 'upgraded', 'cross_source', 'created',
    'merged_unverified', 'created_unverified'.
    """
    lat, lon = cand['loc'].latitude, cand['loc'].longitude

    other = db.find_active_fire_near(lat, lon, CROSS_SOURCE_RADIUS_M)
    if other and other['source'] != 'xmonitor':
        return None, 'cross_source'

    if verified:
        near = db.find_active_fire_near(
            lat, lon, DEDUP_RADIUS_M, source='xmonitor', hours=DEDUP_WINDOW_HOURS,
        )
        if near:
            return near, 'merged'
        hidden = db.find_fires_near(
            lat, lon, DEDUP_RADIUS_M,
            statuses=('unverified',), source='xmonitor', hours=DEDUP_WINDOW_HOURS,
        )
        if hidden:
            return hidden[0], 'upgraded'
        return None, 'created'

    hidden = db.find_fires_near(
        lat, lon, DEDUP_RADIUS_M,
        statuses=('unverified',), source='xmonitor', hours=DEDUP_WINDOW_HOURS,
    )
    if hidden:
        return hidden[0], 'merged_unverified'
    return None, 'created_unverified'


def _best_verified(a: str | None, b: str | None) -> str | None:
    return a if _VERIFIED_PRIORITY.get(a, 0) >= _VERIFIED_PRIORITY.get(b, 0) else b


def _chronology_url(text: str) -> str:
    import urllib.parse
    hashtag = _extract_if_hashtag(text)
    if not hashtag:
        return ""
    return f"https://x.com/search?q={urllib.parse.quote(hashtag)}&src=typed_query&f=live"


def _assemble_fire(
    db: FireDatabase,
    cand: dict,
    *,
    existing: dict | None,
    status: str,
    verified: str | None,
) -> dict:
    """Build (or merge into existing) the fire dict to upsert."""
    loc: FireLocation = cand['loc']
    tweet: XTweet = cand['tweet']
    now = datetime.now(timezone.utc).isoformat()

    raw = dict(existing.get('raw_data') or {}) if existing else {}
    tweets = list(raw.get('tweets') or [])
    ids = {t.get('id') for t in tweets}
    entry = _tweet_raw(tweet)
    if entry['id'] not in ids:
        tweets.append(entry)
    raw['tweets'] = tweets
    raw['tweet_ids'] = [t.get('id') for t in tweets]

    old_verified = (existing or {}).get('verified')
    final_verified = _best_verified(old_verified, verified)

    # Location: keep existing unless we now have FRP evidence.
    if existing and existing.get('latitude') is not None and existing.get('longitude') is not None:
        if not (verified == 'frp' and old_verified != 'frp'):
            lat, lon = existing['latitude'], existing['longitude']
        else:
            lat, lon = loc.latitude, loc.longitude
    else:
        lat, lon = loc.latitude, loc.longitude

    # Earliest detection date.
    detection_date = existing.get('detection_date') if existing else None
    tweet_iso = tweet.created_at.isoformat()
    if not detection_date or tweet_iso < detection_date:
        detection_date = tweet_iso

    perimeter = db_find_perimeter(db, lat, lon)
    if perimeter:
        raw['perimeter_id'] = perimeter.get('id')
        raw['perimeter_area_ha'] = perimeter.get('area_ha')
        raw['perimeter_commune'] = perimeter.get('commune')
        raw['perimeter_province'] = perimeter.get('province')

    raw['verification'] = final_verified
    raw['chronology_url'] = _chronology_url(tweet.text) or (existing or {}).get('raw_data', {}).get('chronology_url', '')
    raw['tweet_count'] = len(tweets)

    fire = {
        'source': 'xmonitor',
        'external_id': (existing or {}).get('external_id') or f"x_{tweet.tweet_id}",
        'source_url': f"https://x.com/{tweet.author_handle}/status/{tweet.tweet_id}",
        'latitude': lat,
        'longitude': lon,
        'municipality': loc.municipality or (existing or {}).get('municipality'),
        'province': loc.province or (existing or {}).get('province', ''),
        'region': loc.region or (existing or {}).get('region', ''),
        'country': loc.country or (existing or {}).get('country', 'ES'),
        'status': status,
        'fire_type': 'wildfire',
        'detection_date': detection_date,
        'last_updated': now,
        'verified': final_verified,
        'raw_data': raw,
    }
    return fire


def db_find_perimeter(db: FireDatabase, lat: float, lon: float):
    try:
        perimeters = db.find_perimeter_near(
            lat, lon, PERIMETER_MATCH_KM, PERIMETER_DATE_WINDOW_DAYS,
        )
        return perimeters[0] if perimeters else None
    except Exception as e:
        logger.debug('Perimeter lookup failed: %s', e)
        return None


def _process_tweets(db: FireDatabase, tweets: list[XTweet], stats: dict):
    # ── Stage 1: gather candidates (must carry #IF{Municipality}) ──
    candidates = []
    for tweet in tweets:
        try:
            municipality = extract_if_municipality(tweet.text)
            if not municipality:
                stats['no_hashtag'] += 1
                continue

            location = locate_fire(
                municipality=municipality,
                lat=None,
                lon=None,
                db_path=db.db_path,
            )
            if not location:
                stats['geocode_failures'] += 1
                logger.warning("Could not locate fire from tweet %s", tweet.tweet_id)
                continue

            if not _in_iberia(location.latitude, location.longitude):
                stats['outside_region'] = stats.get('outside_region', 0) + 1
                continue

            if not is_monitored_region(location.region):
                stats['unmonitored_region'] = stats.get('unmonitored_region', 0) + 1
                logger.debug(
                    "Unmonitored region: %s (%s)", location.region, location.municipality,
                )
                continue

            candidates.append({'tweet': tweet, 'loc': location})
        except Exception as e:
            logger.error("Error processing tweet %s: %s", tweet.tweet_id, e)
            continue

    # ── Stage 2: verify + persist each candidate ──
    for cand in candidates:
        try:
            tweet = cand['tweet']
            verified = _verify_candidate(db, cand, candidates)
            existing, action = _resolve_fire(db, cand, verified)

            if action == 'cross_source':
                stats['cross_source'] = stats.get('cross_source', 0) + 1
                logger.info(
                    "Already reported by another source: %s (@%s)",
                    cand['loc'].municipality, tweet.author_handle,
                )
                continue

            if verified:
                status = 'active'
            else:
                status = 'unverified'

            fire = _assemble_fire(
                db, cand, existing=existing, status=status, verified=verified,
            )
            db.upsert(fire)

            if action in ('created', 'created_unverified'):
                stats['new_fires'] += 1
                if verified:
                    logger.info(
                        "NEW FIRE: %s (%s) via @%s — verified=%s, FRP: %d detections, %.1f MW",
                        cand['loc'].municipality, cand['loc'].province,
                        tweet.author_handle, verified,
                        cand['loc'].frp_count, cand['loc'].frp_max_mw,
                    )
            elif action == 'merged':
                stats['duplicates'] += 1
                logger.debug("Merged tweet %s into existing fire %s",
                             tweet.tweet_id, fire['external_id'])
            elif action == 'upgraded':
                stats['upgraded'] = stats.get('upgraded', 0) + 1
                logger.info(
                    "UPGRADED fire %s from unverified (%s)",
                    fire['external_id'], verified,
                )
            elif action == 'merged_unverified':
                stats['duplicates'] += 1

            if not verified:
                stats['unverified'] = stats.get('unverified', 0) + 1

            if fire.get('raw_data', {}).get('perimeter_id'):
                stats['perimeters_linked'] = stats.get('perimeters_linked', 0) + 1

        except Exception as e:
            logger.error("Error saving candidate from tweet %s: %s",
                         cand['tweet'].tweet_id, e)
            continue


def _expire_stale(db: FireDatabase, stats: dict):
    """Mark extinguished active xmonitor fires with no FRP and no new tweets."""
    fires = db.get_fires_by_source('xmonitor', statuses=_ACTIVE_STATUSES)
    now = datetime.now(timezone.utc)
    for fire in fires:
        try:
            if fire.get('latitude') is None or fire.get('longitude') is None:
                continue
            has_frp = bool(db.get_frp_near(
                fire['latitude'], fire['longitude'],
                VERIFY_FRP_RADIUS_KM, EXPIRE_NO_FRP_HOURS,
            ))
            newest = _latest_tweet_time(fire.get('raw_data'))
            if newest is None:
                try:
                    newest = datetime.fromisoformat(
                        fire.get('last_updated', '').replace('Z', '+00:00')
                    )
                    if newest.tzinfo is None:
                        newest = newest.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    newest = now
            has_tweet = (now - newest).total_seconds() < EXPIRE_NO_TWEET_HOURS * 3600

            if not has_frp and not has_tweet:
                db.set_fire_status(
                    fire['source'], fire['external_id'], 'extinguished',
                )
                stats['expired'] = stats.get('expired', 0) + 1
                logger.info(
                    "EXPIRED fire %s (%s) — no FRP %sh, no tweets %sh",
                    fire['external_id'], fire.get('municipality'),
                    EXPIRE_NO_FRP_HOURS, EXPIRE_NO_TWEET_HOURS,
                )
        except Exception as e:
            logger.debug("Expiry check failed for %s: %s", fire.get('external_id'), e)
            continue


def run_monitor(*, hours_back: int = SEARCH_HOURS_BACK, limit_per_query: int = 20) -> dict:
    """
    Run one monitoring cycle — Spain, regions without official source only.

    Flow:
      1. Search #IF hashtags + official emergency accounts
      2. Verify, deduplicate (one fire entity per fire) and persist
      3. Auto-expire stale xmonitor fires
    """
    stats = {
        'new_fires': 0,
        'duplicates': 0,
        'geocode_failures': 0,
        'outside_region': 0,
        'tweets_found': 0,
        'no_hashtag': 0,
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

    _expire_stale(db, stats)

    logger.info(
        "Monitor complete: %d new, %d merged, %d unverified, %d expired",
        stats['new_fires'], stats['duplicates'],
        stats.get('unverified', 0), stats.get('expired', 0),
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description='Fire monitor — Spain (regions without scraper)')
    parser.add_argument('--hours-back', type=int, default=SEARCH_HOURS_BACK,
                        help=f'Hours back to search (default: {SEARCH_HOURS_BACK})')
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

    print("\n--- Monitor Results ---")
    print(f" tweets found:       {stats['tweets_found']}")
    print(f" no #IF hashtag:     {stats['no_hashtag']}")
    print(f" new fires:          {stats['new_fires']}")
    print(f" merged duplicates:  {stats['duplicates']}")
    print(f" upgraded:           {stats.get('upgraded', 0)}")
    print(f" unverified (hidden):{stats.get('unverified', 0)}")
    print(f" expired:            {stats.get('expired', 0)}")
    print(f" perimeters linked:  {stats.get('perimeters_linked', 0)}")
    print(f" outside region:     {stats.get('outside_region', 0)}")
    print(f" unmonitored region: {stats.get('unmonitored_region', 0)}")
    print(f" cross-source:       {stats.get('cross_source', 0)}")
    print(f" geocode failures:   {stats['geocode_failures']}")
    print(f" timestamp:          {stats['timestamp']}")


if __name__ == '__main__':
    main()
