"""
FireIncident dataclass and FireScraper ABC.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30
_UA = 'FireTracker/0.1 (wildfire-tracking; https://github.com/vrieraj/fire-tracker)'


@dataclass
class FireIncident:
    """Normalized fire incident from any platform."""

    source: str
    external_id: str
    source_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    municipality: str | None = None
    province: str | None = None
    region: str | None = None
    country: str = 'ES'
    status: str = 'unknown'
    fire_type: str | None = None
    detection_date: datetime | None = None
    extinction_date: datetime | None = None
    area_ha: float | None = None
    resources: dict | None = None
    raw_data: dict = field(default_factory=dict)
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ('detection_date', 'extinction_date', 'last_updated'):
            if d[key] is not None:
                d[key] = d[key].isoformat()
        return d

    def geojson_feature(self) -> dict:
        if self.latitude is None or self.longitude is None:
            return {}
        return {
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [self.longitude, self.latitude],
            },
            'properties': {
                'id': f'{self.source}:{self.external_id}',
                'source': self.source,
                'external_id': self.external_id,
                'source_url': self.source_url,
                'municipality': self.municipality,
                'province': self.province,
                'region': self.region,
                'country': self.country,
                'status': self.status,
                'fire_type': self.fire_type,
                'detection_date': self.detection_date.isoformat() if self.detection_date else None,
                'extinction_date': self.extinction_date.isoformat() if self.extinction_date else None,
                'area_ha': self.area_ha,
                'resources': self.resources,
                'last_updated': self.last_updated.isoformat(),
            },
        }


class FireScraper(ABC):
    """Base class for fire scrapers."""

    source: str

    def _get(self, url: str, **kwargs) -> requests.Response:
        headers = kwargs.pop('headers', {})
        headers.setdefault('User-Agent', _UA)
        return requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT, **kwargs)

    @abstractmethod
    def fetch(self) -> list[FireIncident]:
        """Fetch active fires from the platform."""

    @staticmethod
    def _status_normalize(raw: str, source: str) -> str:
        """Normalize status to canonical value."""
        r = raw.strip().lower() if raw else ''
        if r in ('activo', 'active', 'declarado', 'attaque', 'em curso',
                 'actiu', 'signale', 'probable', 'signaled'):
            return 'active'
        if r in ('controlado', 'controlat', 'maitrise', 'fixe', 'controlled'):
            return 'controlled'
        if r in ('estabilizado', 'estabilitzat', 'stabilized'):
            return 'stabilized'
        if r in ('extinguido', 'extingit', 'eteint', 'extinguished',
                 'cloture', 'cerrada', 'conclusao'):
            return 'extinguished'
        if r in ('falsa_alarma', 'fausse_alerte', 'falso_alerta',
                 'anulado', 'douteux'):
            return 'false_alarm'
        return 'unknown'
