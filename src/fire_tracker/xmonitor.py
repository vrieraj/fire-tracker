"""
X.com fire mention monitor using Playwright.

Searches for #IF hashtags and official emergency account posts
to detect wildfires in Spanish regions without official scrapers.

Requires X_AUTH_TOKEN and X_CT0 in environment or .env.

Setup:
    1. Log into x.com in your browser
    2. F12 → Application → Cookies → x.com
    3. Copy auth_token and ct0 values to .env
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Load .env
_root = Path(__file__).resolve().parents[2]
from dotenv import load_dotenv
load_dotenv(_root / '.env')

# Only #IF hashtag — the standard for fire reports in Spain
SEARCH_QUERIES = ["#IF"]

# Official fire/emergency accounts — only regions WITHOUT scrapers
OFFICIAL_ACCOUNTS = [
    # Aragón
    "112Arago", "IIFFAragon",
    # Madrid
    "112cmadrid", "bomberos_infoma", "AT_Brif", "BBFFMadrid",
    # Comunidad Valenciana
    "emergenciascv",
    # País Vasco + Navarra + La Rioja
    "112euskadi", "112_na", "BBFFLaRioja", "MAmbienteRioja",
    # Extremadura
    "PLANINFOEX", "JuntaEx112",
    # Cantabria
    "112CantabriaV2",
    # Asturias
    "112asturias",
    # Baleares
    "Emergencies_112",
    # Murcia
    "112Murcia",
]


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


def get_x_auth_token() -> str | None:
    return os.environ.get("X_AUTH_TOKEN")


def get_x_ct0() -> str | None:
    return os.environ.get("X_CT0")


def extract_if_municipality(text: str) -> str:
    """Extract municipality name from #IF hashtag."""
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

    return ""


def _parse_tweet_from_json(data: dict) -> XTweet | None:
    """Parse a tweet dict from X API response into XTweet."""
    try:
        tweet_id = str(data.get("rest_id", ""))
        if not tweet_id:
            return None

        core = data.get("core", {}).get("user_results", {}).get("result", {})
        legacy = data.get("legacy", {})
        created_at_str = legacy.get("created_at", "")

        created_at = datetime.now(timezone.utc)
        if created_at_str:
            try:
                created_at = datetime.strptime(
                    created_at_str, "%a %b %d %H:%M:%S %z %Y"
                )
            except ValueError:
                pass

        text = legacy.get("full_text", "")
        author_handle = core.get("legacy", {}).get("screen_name", "")
        author_name = core.get("legacy", {}).get("name", "")

        urls = []
        for u in legacy.get("entities", {}).get("urls", []):
            url = u.get("expanded_url") or u.get("url", "")
            if url:
                urls.append(url)

        return XTweet(
            tweet_id=tweet_id,
            text=text,
            author_handle=author_handle,
            author_name=author_name,
            created_at=created_at,
            likes=legacy.get("favorite_count", 0),
            retweets=legacy.get("retweet_count", 0),
            replies=legacy.get("reply_count", 0),
            language=legacy.get("lang", ""),
            urls=urls,
            location_mention=_extract_location(text),
        )
    except Exception as e:
        logger.debug("Failed to parse tweet: %s", e)
        return None


def _extract_tweets_from_response(data: dict, tweets: list[XTweet], hours_back: int):
    """Extract tweets from X API JSON response."""
    cutoff = datetime.now(timezone.utc).timestamp() - (hours_back * 3600)

    try:
        instructions = (
            data.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )

        for instruction in instructions:
            entries = instruction.get("entries", [])
            for entry in entries:
                content = entry.get("content", {})
                item_content = content.get("itemContent", {})
                tweet_results = item_content.get("tweet_results", {})
                result = tweet_results.get("result", {})

                if result.get("__typename") == "Tweet":
                    parsed = _parse_tweet_from_json(result)
                    if parsed and parsed.created_at.timestamp() >= cutoff:
                        if not any(t.tweet_id == parsed.tweet_id for t in tweets):
                            tweets.append(parsed)
                elif result.get("__typename") == "TimelineTimelineItem":
                    inner = result.get("content", {}).get("tweetResult", {}).get("result", {})
                    parsed = _parse_tweet_from_json(inner)
                    if parsed and parsed.created_at.timestamp() >= cutoff:
                        if not any(t.tweet_id == parsed.tweet_id for t in tweets):
                            tweets.append(parsed)
    except Exception as e:
        logger.debug("Error extracting tweets from response: %s", e)


async def _extract_from_dom(page, hours_back: int) -> list[XTweet]:
    """Fallback: extract tweets directly from DOM elements."""
    tweets = []

    try:
        # Wait for tweet articles to appear
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=10000)
    except Exception:
        logger.info("No tweet articles found in DOM")
        return tweets

    try:
        tweet_elements = await page.query_selector_all('article[data-testid="tweet"]')
        logger.info("Found %d tweet elements in DOM", len(tweet_elements))

        for elem in tweet_elements[:30]:
            try:
                # Extract tweet text
                text_elem = await elem.query_selector('[data-testid="tweetText"]')
                text = await text_elem.inner_text() if text_elem else ""

                # Extract author handle
                handle_elem = await elem.query_selector('a[href*="/"] div[dir="ltr"] span')
                author_handle = ""
                if handle_elem:
                    handle_text = await handle_elem.inner_text()
                    if handle_text.startswith("@"):
                        author_handle = handle_text[1:]

                # Extract tweet ID from time link
                time_elem = await elem.query_selector('time')
                tweet_id = ""
                created_at = datetime.now(timezone.utc)
                if time_elem:
                    datetime_str = await time_elem.get_attribute("datetime")
                    parent_link = await time_elem.evaluate(
                        "el => el.closest('a')?.href || ''"
                    )
                    if parent_link:
                        match = re.search(r'/status/(\d+)', parent_link)
                        if match:
                            tweet_id = match.group(1)
                    if datetime_str:
                        try:
                            created_at = datetime.fromisoformat(
                                datetime_str.replace("Z", "+00:00")
                            )
                        except ValueError:
                            pass

                if not tweet_id or not text:
                    continue

                # Filter by time
                cutoff = datetime.now(timezone.utc).timestamp() - (hours_back * 3600)
                if created_at.timestamp() < cutoff:
                    continue

                parsed = XTweet(
                    tweet_id=tweet_id,
                    text=text,
                    author_handle=author_handle,
                    author_name="",
                    created_at=created_at,
                    location_mention=_extract_location(text),
                )
                tweets.append(parsed)

            except Exception as e:
                logger.debug("Error parsing DOM tweet element: %s", e)
                continue

    except Exception as e:
        logger.warning("DOM extraction failed: %s", e)

    return tweets


async def search_fire_tweets(
    *,
    limit_per_query: int = 20,
    hours_back: int = 2,
) -> list[XTweet]:
    """
    Search X for fire-related tweets in Spain.

    1. Search #IF hashtag (primary signal for fire reports)
    2. Search official emergency accounts (from:handle) in batches

    Uses a single browser instance for all queries to minimize memory.
    """
    from playwright.async_api import async_playwright

    auth_token = get_x_auth_token()
    ct0 = get_x_ct0()

    if not auth_token or not ct0:
        logger.warning("X_AUTH_TOKEN or X_CT0 not set in environment")
        return []

    all_tweets: dict[str, XTweet] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-extensions',
                '--disable-background-networking',
                '--single-process',
            ],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )

        # Set cookies
        await context.add_cookies([
            {"name": "auth_token", "value": auth_token, "domain": ".x.com", "path": "/"},
            {"name": "ct0", "value": ct0, "domain": ".x.com", "path": "/"},
        ])

        page = await context.new_page()

        try:
            # 1. #IF hashtag search
            tweets = await _search_x_com_page(page, "#IF", hours_back=hours_back)
            for tweet in tweets[:limit_per_query]:
                if tweet.tweet_id not in all_tweets:
                    all_tweets[tweet.tweet_id] = tweet

            # 2. Official accounts — batched with OR
            handles = OFFICIAL_ACCOUNTS
            for i in range(0, len(handles), 6):
                batch = handles[i:i+6]
                query = " OR ".join(f"from:{h}" for h in batch)
                tweets = await _search_x_com_page(page, query, hours_back=hours_back)
                for tweet in tweets[:10]:
                    if tweet.tweet_id not in all_tweets:
                        all_tweets[tweet.tweet_id] = tweet
        finally:
            await browser.close()

    result = list(all_tweets.values())
    logger.info("Found %d unique fire tweets", len(result))
    return result


async def _search_x_com_page(page, query: str, hours_back: int = 2) -> list[XTweet]:
    """Search X.com on an existing page instance."""
    tweets: list[XTweet] = []
    captured_responses = []

    async def handle_response(response):
        url = response.url
        if "SearchTimeline" in url or "Search" in url:
            try:
                body = await response.json()
                captured_responses.append(body)
            except Exception:
                pass

    page.on("response", handle_response)

    search_url = f"https://x.com/search?q={urllib.parse.quote(query)}&src=typed_query&f=live"
    logger.info("Searching X: '%s'", query[:80])

    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(5000)

        # Scroll to load more
        for _ in range(2):
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(1500)

    except Exception as e:
        logger.warning("Navigation failed: %s", e)
        return tweets

    # Parse captured API responses
    cutoff = datetime.now(timezone.utc).timestamp() - (hours_back * 3600)
    for resp_data in captured_responses:
        _extract_tweets_from_response(resp_data, tweets, hours_back)

    # DOM fallback
    if not tweets:
        tweets = await _extract_from_dom(page, hours_back)

    # Remove listener for next search
    page.remove_listener("response", handle_response)

    return tweets


def search_fire_tweets_sync(
    *,
    limit_per_query: int = 20,
    hours_back: int = 2,
) -> list[XTweet]:
    """Synchronous wrapper for search_fire_tweets."""
    import asyncio
    return asyncio.run(search_fire_tweets(
        limit_per_query=limit_per_query,
        hours_back=hours_back,
    ))
