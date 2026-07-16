# AGENTS.md

## Project Overview

European wildfire tracking aggregator with weather data. Collects fire data from official platforms across Spain, Portugal, and France, deduplicates, and serves via REST API. Includes weather forecasting using Open-Meteo's best model and meteogram generation.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Architecture

| Module | Responsibility |
|--------|----------------|
| `src/fire_tracker/scrapers/` | One scraper per data source (INFOCA, feuxdeforet, incendiscat, fogos, INCyL, FIDIAS CLM) |
| `src/fire_tracker/database.py` | SQLite persistence with upsert and state history |
| `src/fire_tracker/orchestrator.py` | Runs all scrapers, deduplicates (<500m, <3h), persists to DB |
| `src/fire_tracker/weather.py` | Geocoding + Open-Meteo best model forecast fetcher |
| `src/fire_tracker/meteogram.py` | 4-panel meteogram generator (matplotlib) |
| `src/fire_tracker/api/app.py` | Flask REST API |
| `scripts/fidias_crossref.py` | Standalone FIDIAS CLM cross-reference script |

## Data Sources — Fire Tracking

| Source | Region | Auth | Status |
|--------|--------|------|--------|
| INFOCA | Andalucia | None (public ArcGIS) | Working |
| feuxdeforet.fr | France | None (public GeoJSON) | Working |
| incendiscat.cat | Catalunya | HMAC-SHA256 (embedded key) | Working (key may rotate) |
| fogos.pt | Portugal | None (public API) | Working |
| INCyL | Castilla y Leon | None (official API) | Working |
| FIDIAS CLM | Castilla-La Mancha | None (HTML + satellite) | Working |

## Weather & Meteogram

Uses Open-Meteo API (https://open-meteo.com) with "Best Match" model selection:

- **Geocoding**: `/v1/search` — search locations by name
- **Forecast**: `/v1/forecast?models=best_match` — auto-selects best model for location
- **Elevation**: `/v1/elevation` — 90m DEM elevation

### Default hourly variables

```
temperature_2m, relative_humidity_2m, dew_point_2m, precipitation,
weather_code, wind_speed_10m, wind_direction_10m, wind_gusts_10m,
cloud_cover, pressure_msl, surface_pressure, shortwave_radiation,
cape, is_day
```

### Meteogram panels

| Panel | Content |
|-------|---------|
| 0 (top) | Wind: speed + gusts + direction arrows |
| 1 | Temperature: temp + dew point + apparent |
| 2 | Precipitation: rain + clouds |
| 3 (bottom) | Radiation: shortwave + CAPE |

Night shading based on `is_day` flag.

## Key Design Patterns

- **FireIncident dataclass**: Normalized incident from any source
- **FireScraper ABC**: Each source implements `fetch() -> list[FireIncident]`
- **Location dataclass**: Geocoded location with coordinates and metadata
- **WeatherData**: Container for hourly/daily forecast data
- **Accumulator pattern**: Orchestrator collects from all scrapers, deduplicates, persists
- **Deduplication**: Spatial (<500m) + temporal (<3h) matching across sources
- **State history**: Status changes tracked in `fire_history` table

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/fires/tracked` | GET | GeoJSON of active fires |
| `/api/fires/refresh` | POST | Re-run all scrapers |
| `/api/fires/stats` | GET | Database statistics |
| `/api/geocode?q=...` | GET | Search locations |
| `/api/weather?lat=...&lon=...` | GET | Weather forecast (JSON) |
| `/api/meteogram.png?lat=...&lon=...` | GET | Meteogram image (PNG) |

## External Dependencies

- `requests` — HTTP client
- `pyproj` — UTM→WGS84 conversion (INCyL only)
- `flask` — REST API
- `matplotlib` — Meteogram generation
- `numpy` — Numerical operations

## Pending

- Madrid: no official real-time source (only satellite data)
- incendiscat.cat HMAC key rotation monitoring
- Cron/scheduler for automatic refresh
- Mobile-friendly frontend
- Weather station search (50km radius)
- Pressure level vertical profiles
