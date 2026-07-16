# AGENTS.md

## Project Overview

European wildfire tracking aggregator. Collects fire data from official platforms across Spain, Portugal, and France, deduplicates, and serves via REST API.

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
| `src/fire_tracker/orchestratorator.py` | Runs all scrapers, deduplicates (<500m, <3h), persists to DB |
| `src/fire_tracker/api/app.py` | Flask REST API |
| `scripts/fidias_crossref.py` | Standalone FIDIAS CLM cross-reference script |

## Data Sources

| Source | Region | Auth | Status |
|--------|--------|------|--------|
| INFOCA | Andalucia | None (public ArcGIS) | Working |
| feuxdeforet.fr | France | None (public GeoJSON) | Working |
| incendiscat.cat | Catalunya | HMAC-SHA256 (embedded key) | Working (key may rotate) |
| fogos.pt | Portugal | None (public API) | Working |
| INCyL | Castilla y Leon | None (official API) | Working |
| FIDIAS CLM | Castilla-La Mancha | None (HTML + satellite) | Working |

## Key Design Patterns

- **FireIncident dataclass**: Normalized incident from any source
- **FireScraper ABC**: Each source implements `fetch() -> list[FireIncident]`
- **Accumulator pattern**: Orchestrator collects from all scrapers, deduplicates, persists
- **Deduplication**: Spatial (<500m) + temporal (<3h) matching across sources
- **State history**: Status changes tracked in `fire_history` table

## External Dependencies

- `requests` — HTTP client
- `pyproj` — UTM→WGS84 conversion (INCyL only)
- `flask` — REST API

## Pending

- Madrid: no official real-time source (only satellite data)
- incendiscat.cat HMAC key rotation monitoring
- Cron/scheduler for automatic refresh
- Mobile-friendly frontend
