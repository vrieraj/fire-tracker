"""Fire tracking scrapers — one per data source."""

from fire_tracker.scrapers.base import FireIncident, FireScraper
from fire_tracker.scrapers.infoca import InfocaAndaluciaScraper
from fire_tracker.scrapers.feuxdeforet import FeuxDeForetFrScraper
from fire_tracker.scrapers.incendiscat import IncendiscatCatScraper
from fire_tracker.scrapers.fogos import FogosPtScraper
from fire_tracker.scrapers.incyl import IncendiosCyLScraper
from fire_tracker.scrapers.fidias_clm import FidiasClmScraper

__all__ = [
    'FireIncident',
    'FireScraper',
    'InfocaAndaluciaScraper',
    'FeuxDeForetFrScraper',
    'IncendiscatCatScraper',
    'FogosPtScraper',
    'IncendiosCyLScraper',
    'FidiasClmScraper',
]
