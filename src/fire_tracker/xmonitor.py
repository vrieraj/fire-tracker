"""
X.com fire mention monitor using twscrape.

Searches for #IF hashtags and official emergency account posts
to detect wildfires in Spanish regions without official scrapers.

Requires X_AUTH_TOKEN and X_CT0 in environment or .env.

Setup:
    1. Log into x.com in your browser
    2. F12 → Application → Cookies → x.com
    3. Copy auth_token and ct0 values to .env
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from twscrape import API, gather

logger = logging.getLogger(__name__)

# Load .env
_root = Path(__file__).resolve().parents[2]
from dotenv import load_dotenv
load_dotenv(_root / '.env')

# Only #IF hashtag — the standard for fire reports in Spain
SEARCH_QUERIES = {
    "es": ["#IF"],
}

# Official fire/emergency accounts — only regions WITHOUT scrapers
# Andalucía(INFOCA), Cataluña(incendiscat), CyL(INCyL), CLM(FIDIAS+FRP) have scrapers
OFFICIAL_ACCOUNTS = {
    "es": [
        # Aragón
        "112Arago",
        "IIFFAragon",
        # Madrid
        "112cmadrid",
        "bomberos_infoma",
        "AT_Brif",
        "BBFFMadrid",
        # Comunidad Valenciana
        "emergenciascv",
        # País Vasco + Navarra + La Rioja (agrupadas — pocas)
        "112euskadi",
        "112_na",
        "BBFFLaRioja",
        "MAmbienteRioja",
        # Extremadura
        "PLANINFOEX",
        "JuntaEx112",
        # Cantabria
        "112CantabriaV2",
        # Asturias
        "112asturias",
        # Baleares
        "Emergencies_112",
        # Murcia
        "112Murcia",
    ],
}


@dataclass
class XTweet:
    """Parsed tweet with fire-relevant information."""
    tweet_id: str
    text: str
    author_handle: str
    author_name: str
    created_at: datetime
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    language: str = ""
    urls: list[str] = field(default_factory=list)
    location_mention: str = ""
    raw: Any = None


def get_x_auth_token() -> str | None:
    return os.environ.get("X_AUTH_TOKEN")


def get_x_ct0() -> str | None:
    return os.environ.get("X_CT0")


def _build_api() -> API | None:
    """Build twscrape API with cookies from environment."""
    auth_token = get_x_auth_token()
    ct0 = get_x_ct0()

    if not auth_token or not ct0:
        logger.warning("X_AUTH_TOKEN or X_CT0 not set in environment")
        return None

    db_path = Path(__file__).resolve().parents[2] / "data" / "xmonitor_accounts.db"
    api = API(str(db_path))
    return api


async def _add_cookies(api: API) -> bool:
    """Add cookies to the API pool."""
    auth_token = get_x_auth_token()
    ct0 = get_x_ct0()

    if not auth_token or not ct0:
        return False

    cookies_str = f"auth_token={auth_token}; ct0={ct0}"
    try:
        await api.pool.add_account_cookies("fire_monitor", cookies_str)
        return True
    except Exception as e:
        logger.error("Failed to add cookies: %s", e)
        return False


def extract_if_municipality(text: str) -> str:
    """Extract municipality name from #IF hashtag.

    The #IF{Municipio} format is the standard way to report active wildfires
    on Spanish Twitter/X. Examples: #IFJaen, #IFCordoba, #IFVillanuevaDelRosario

    Returns the municipality name normalized.
    """
    match = re.search(r'#IF([A-ZÁÉÍÓÚÑ][a-záéíóúñA-ZÁÉÍÓÚÑ]*)', text)
    if match:
        municipality = match.group(1)
        words = re.sub(r'([a-záéíóúñ])([A-ZÁÉÍÓÚÑ])', r'\1 \2', municipality).split()
        if len(words) > 1:
            prepositions = {'de', 'del', 'la', 'las', 'los', 'el'}
            normalized = []
            for i, word in enumerate(words):
                if i > 0 and word.lower() in prepositions:
                    normalized.append(word.lower())
                else:
                    normalized.append(word)
            return ' '.join(normalized)
        return municipality
    return ""


def _extract_location(text: str) -> str:
    """Extract probable municipality name from tweet text."""
    if_municipality = extract_if_municipality(text)
    if if_municipality:
        return if_municipality

    match = re.search(r'\ben\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})', text)
    if match:
        return match.group(1)

    match = re.search(r'municipio\s+de\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})', text)
    if match:
        return match.group(1)

    match = re.search(r'provincia\s+de\s+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})', text)
    if match:
        return match.group(1)

    return ""


def _parse_tweet(tweet: Any) -> XTweet:
    """Parse a twscrape Tweet object into XTweet."""
    created = tweet.date
    if created and not created.tzinfo:
        created = created.replace(tzinfo=timezone.utc)

    urls = []
    if hasattr(tweet, 'urls') and tweet.urls:
        for u in tweet.urls:
            if hasattr(u, 'url'):
                urls.append(u.url)
            elif isinstance(u, str):
                urls.append(u)

    return XTweet(
        tweet_id=str(tweet.id),
        text=tweet.rawContent or "",
        author_handle=tweet.user.username if tweet.user else "",
        author_name=tweet.user.displayname if tweet.user else "",
        created_at=created or datetime.now(timezone.utc),
        likes=getattr(tweet, 'likeCount', 0) or 0,
        retweets=getattr(tweet, 'retweetCount', 0) or 0,
        replies=getattr(tweet, 'replyCount', 0) or 0,
        language=getattr(tweet, 'lang', '') or "",
        urls=urls,
        location_mention=_extract_location(tweet.rawContent or ""),
        raw=tweet,
    )


async def search_fire_tweets(
    *,
    limit_per_query: int = 20,
    hours_back: int = 2,
) -> list[XTweet]:
    """
    Search X for fire-related tweets in Spain.

    1. Search #IF hashtag (primary signal for fire reports)
    2. Search official emergency accounts (from:handle) in batches

    Total: ~4 API calls (manageable with 1 twscrape account per hour).
    """
    api = _build_api()
    if not api:
        return []

    if not await _add_cookies(api):
        return []

    all_tweets: dict[str, XTweet] = {}
    cutoff = datetime.now(timezone.utc).timestamp() - (hours_back * 3600)

    # 1. #IF hashtag search (1 API call)
    try:
        logger.info("Searching X: '#IF'")
        tweets = await gather(api.search("#IF", limit=limit_per_query))
        for tweet in tweets:
            parsed = _parse_tweet(tweet)
            if parsed.created_at.timestamp() < cutoff:
                continue
            if parsed.tweet_id not in all_tweets:
                all_tweets[parsed.tweet_id] = parsed
    except Exception as e:
        logger.warning("Search for #IF failed: %s", e)

    # 2. Official accounts — batched with OR (3 API calls for 17 accounts)
    handles = OFFICIAL_ACCOUNTS.get("es", [])
    for i in range(0, len(handles), 6):
        batch = handles[i:i+6]
        query = " OR ".join(f"from:{h}" for h in batch)
        try:
            logger.info("Searching X: '%s'", query[:80])
            tweets = await gather(api.search(query, limit=10))
            for tweet in tweets:
                parsed = _parse_tweet(tweet)
                if parsed.created_at.timestamp() < cutoff:
                    continue
                if parsed.tweet_id not in all_tweets:
                    all_tweets[parsed.tweet_id] = parsed
        except Exception as e:
            logger.warning("Official accounts batch failed: %s", e)
            continue

    result = list(all_tweets.values())
    logger.info("Found %d unique fire tweets", len(result))
    return result


def search_fire_tweets_sync(
    *,
    limit_per_query: int = 20,
    hours_back: int = 2,
) -> list[XTweet]:
    """Synchronous wrapper for search_fire_tweets."""
    return asyncio.run(search_fire_tweets(
        limit_per_query=limit_per_query,
        hours_back=hours_back,
    ))
