"""
Weather data module — geocoding + Open-Meteo best model fetcher.

Uses Open-Meteo's "Best Match" endpoint which automatically selects
the best weather model for any location worldwide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

_GEOCODING_URL = 'https://geocoding-api.open-meteo.com/v1/search'
_FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
_ELEVATION_URL = 'https://api.open-meteo.com/v1/elevation'
_HTTP_TIMEOUT = 30
_UA = 'FireTracker/0.1 (https://github.com/vrieraj/fire-tracker)'


@dataclass
class Location:
    """Geocoded location with coordinates and metadata."""

    name: str
    latitude: float
    longitude: float
    elevation: float = 0.0
    country: str = ''
    country_code: str = ''
    admin1: str = ''  # state/province
    admin2: str = ''  # county/district
    timezone: str = 'auto'
    population: int = 0

    @property
    def display_name(self) -> str:
        parts = [self.name]
        if self.admin1:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ', '.join(parts)


def geocode(query: str, limit: int = 5, language: str = 'es') -> list[Location]:
    """
    Search for locations by name using Open-Meteo geocoding API.

    Parameters
    ----------
    query : str
        Search query (city name, address, etc.)
    limit : int
        Maximum number of results
    language : str
        Language for results (ISO 639-1)

    Returns
    -------
    list[Location]
    """
    try:
        resp = requests.get(
            _GEOCODING_URL,
            params={
                'name': query,
                'count': limit,
                'language': language,
                'format': 'json',
            },
            headers={'User-Agent': _UA},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error('Geocoding error: %s', e)
        return []

    results = data.get('results', [])
    locations = []

    for r in results:
        locations.append(Location(
            name=r.get('name', ''),
            latitude=r.get('latitude', 0),
            longitude=r.get('longitude', 0),
            elevation=r.get('elevation', 0),
            country=r.get('country', ''),
            country_code=r.get('country_code', ''),
            admin1=r.get('admin1', ''),
            admin2=r.get('admin2', ''),
            timezone=r.get('timezone', 'auto'),
            population=r.get('population', 0),
        ))

    return locations


def get_elevation(latitude: float, longitude: float) -> float:
    """
    Get elevation for a coordinate using Open-Meteo elevation API.

    Parameters
    ----------
    latitude, longitude : float

    Returns
    -------
    float — elevation in meters above sea level
    """
    try:
        resp = requests.get(
            _ELEVATION_URL,
            params={'latitude': latitude, 'longitude': longitude},
            headers={'User-Agent': _UA},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        elevations = data.get('elevation', [])
        return float(elevations[0]) if elevations else 0.0
    except Exception:
        return 0.0


@dataclass
class WeatherData:
    """Container for hourly weather forecast data."""

    location: Location
    model: str
    hourly: dict[str, list]
    daily: dict[str, list] | None = None
    hourly_units: dict[str, str] | None = None
    daily_units: dict[str, str] | None = None

    @property
    def times(self) -> list[str]:
        return self.hourly.get('time', [])

    def get(self, key: str, default=None):
        return self.hourly.get(key, default)


def fetch_forecast(
    location: Location,
    hourly_vars: list[str] | None = None,
    daily_vars: list[str] | None = None,
    forecast_days: int = 3,
    past_days: int = 1,
    timezone: str = 'auto',
) -> WeatherData | None:
    """
    Fetch weather forecast from Open-Meteo using best model.

    Parameters
    ----------
    location : Location
        Geocoded location
    hourly_vars : list[str]
        Hourly weather variables to fetch
    daily_vars : list[str]
        Daily weather variables to fetch
    forecast_days : int
        Number of forecast days (1-16)
    past_days : int
        Number of past days to include
    timezone : str
        Timezone for timestamps

    Returns
    -------
    WeatherData or None on error
    """
    if hourly_vars is None:
        hourly_vars = [
            'temperature_2m',
            'relative_humidity_2m',
            'dew_point_2m',
            'precipitation',
            'weather_code',
            'wind_speed_10m',
            'wind_direction_10m',
            'wind_gusts_10m',
            'cloud_cover',
            'pressure_msl',
            'surface_pressure',
            'shortwave_radiation',
            'cape',
            'is_day',
        ]

    if daily_vars is None:
        daily_vars = [
            'weather_code',
            'temperature_2m_max',
            'temperature_2m_min',
            'precipitation_sum',
            'sunrise',
            'sunset',
            'wind_speed_10m_max',
            'wind_gusts_10m_max',
        ]

    params = {
        'latitude': location.latitude,
        'longitude': location.longitude,
        'elevation': location.elevation,
        'hourly': ','.join(hourly_vars),
        'daily': ','.join(daily_vars),
        'forecast_days': forecast_days,
        'past_days': past_days,
        'timezone': timezone,
        'models': 'best_match',
    }

    try:
        resp = requests.get(
            _FORECAST_URL,
            params=params,
            headers={'User-Agent': _UA},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error('Forecast fetch error for %s: %s', location.name, e)
        return None

    return WeatherData(
        location=location,
        model=data.get('model', 'best_match'),
        hourly=data.get('hourly', {}),
        daily=data.get('daily'),
        hourly_units=data.get('hourly_units'),
        daily_units=data.get('daily_units'),
    )


def search_and_fetch(
    query: str,
    hourly_vars: list[str] | None = None,
    daily_vars: list[str] | None = None,
    forecast_days: int = 3,
    past_days: int = 1,
) -> WeatherData | None:
    """
    Convenience: geocode a query and fetch forecast for the first result.

    Parameters
    ----------
    query : str
        Location name to search
    hourly_vars, daily_vars : list[str]
        Variables to fetch
    forecast_days, past_days : int
        Time range

    Returns
    -------
    WeatherData or None
    """
    locations = geocode(query, limit=1)
    if not locations:
        logger.warning('No location found for: %s', query)
        return None

    return fetch_forecast(
        locations[0],
        hourly_vars=hourly_vars,
        daily_vars=daily_vars,
        forecast_days=forecast_days,
        past_days=past_days,
    )
