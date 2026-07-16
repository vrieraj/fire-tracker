# Fire Tracker

European wildfire tracking aggregator that collects data from official platforms across Spain, Portugal, and France.

## Features

- Multi-source fire tracking (INFOCA, feuxdeforet.fr, incendiscat.cat, fogos.pt, INCyL, FIDIAS CLM)
- Automatic deduplication across sources
- SQLite persistence with state history
- REST API for frontend integration
- Mobile-friendly viewer (planned)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

### Run the orchestrator

```bash
python -m fire_tracker.orchestrator
```

### Start the API server

```bash
python -m fire_tracker.api.app
# → http://localhost:5000
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/fires/tracked` | GET | GeoJSON of active fires |
| `/api/fires/refresh` | POST | Re-run all scrapers |
| `/api/fires/stats` | GET | Database statistics |

## Data Sources

| Source | Region | Method |
|--------|--------|--------|
| INFOCA | Andalucia, Spain | ArcGIS FeatureServer |
| feuxdeforet.fr | France | GeoJSON + scraping |
| incendiscat.cat | Catalunya, Spain | HMAC-SHA256 API |
| fogos.pt | Portugal | REST API |
| INCyL | Castilla y Leon, Spain | Official API |
| FIDIAS CLM | Castilla-La Mancha, Spain | HTML scraping + satellite |

## License

GPL-3.0-or-later
