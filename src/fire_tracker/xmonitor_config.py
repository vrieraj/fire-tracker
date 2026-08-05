"""
Configuration for the X.com fire monitor (xmonitor v2).

Filtering rules:
- Only tweets carrying the official hashtag #IF{Municipality} are candidates.
- Only fires in Spanish regions WITHOUT an official scraper are monitored.
- Verification: FRP satellite evidence OR official emergency account OR
  corroboration from >= 2 independent tweets.
- Dedup: tweets of the same fire are merged into a single fire entity
  (radius + time window).
- Lifecycle: fires without FRP and without new tweets are auto-extinguished.
- Perimeter: fires are linked to EFFIS burnt-area perimeters when possible.
"""

from __future__ import annotations

# ── Dedup / fire entity clustering ────────────────────────────────────
# Merge tweets/locations of the same fire into one entity when within
# this radius and the existing fire was last updated within this window.
DEDUP_RADIUS_M = 10_000
DEDUP_WINDOW_HOURS = 48

# Cross-source rule: a candidate within this distance of an active fire
# from another source (INFOCA/fogos/...) is treated as already reported.
CROSS_SOURCE_RADIUS_M = 3_000

# ── Verification ──────────────────────────────────────────────────────
VERIFY_FRP_RADIUS_KM = 15
VERIFY_FRP_HOURS = 24
CORROBORATION_MIN_TWEETS = 2
CORROBORATION_RADIUS_M = 10_000
CORROBORATION_HOURS = 6

# ── Lifecycle / expiry ────────────────────────────────────────────────
EXPIRE_NO_FRP_HOURS = 8
EXPIRE_NO_TWEET_HOURS = 12

# ── EFFIS perimeter assignment ────────────────────────────────────────
PERIMETER_MATCH_KM = 5
PERIMETER_DATE_WINDOW_DAYS = 30

# ── Tweet search window (hours back) ──────────────────────────────────
SEARCH_HOURS_BACK = 6

# ── Official emergency accounts (Tier 1 verification) ─────────────────
TIER1_ACCOUNTS = {
    '112Arago',
    '112cmadrid',
    'emergenciascv',
    '112euskadi',
    '112_na',
    '112CantabriaV2',
    '112asturias',
    'Emergencies_112',
    '112Murcia',
    'PLANINFOEX',
    'JuntaEx112',
}

# ── Regions (CCAA) without an official scraper in the viewer ─────────
# Only these autonomous communities are monitored by xmonitor.
MONITORED_REGIONS = {
    'aragon',
    'madrid',
    'comunidad de madrid',
    'valencia',
    'comunitat valenciana',
    'comunidad valenciana',
    'extremadura',
    'euskadi',
    'pais vasco',
    'navarra',
    'la rioja',
    'cantabria',
    'asturias',
    'baleares',
    'illes balears',
    'islas baleares',
    'murcia',
    'region de murcia',
}


def normalize_region(region: str) -> str:
    """Normalize a region name for membership checks (lowercase, no accents)."""
    accents = {
        'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u',
        'Á': 'a', 'É': 'e', 'Í': 'i', 'Ó': 'o', 'Ú': 'u', 'ü': 'u', 'Ü': 'u',
        'ñ': 'n', 'Ñ': 'n', 'ç': 'c',
    }
    out = []
    for ch in (region or ''):
        out.append(accents.get(ch, ch))
    return ''.join(out).strip().lower()


def is_monitored_region(region: str) -> bool:
    """True if the region is an autonomous community without official source."""
    if not region:
        return False
    norm = normalize_region(region)
    return norm in MONITORED_REGIONS
