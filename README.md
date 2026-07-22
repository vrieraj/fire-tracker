# Fire Tracker

European wildfire tracking aggregator. Collects fire data from official platforms across Spain, Portugal, and France, deduplicates, and serves via REST API.

Live: [fire-tracker-z83q.onrender.com](https://fire-tracker-z83q.onrender.com)

## Data Sources

| Source | Region | Official Platform |
|--------|--------|-------------------|
| INFOCA | Andalucia | [juntadeandalucia.es/infoca](https://www.juntadeandalucia.es/organismos/agriculturaganaderiapescaagroalimentacion/areas/agricultura/infoca) |
| INCyL | Castilla y Leon | [incendios.castillayleon.es](https://incendios.castillayleon.es) |
| FIDIAS CLM | Castilla-La Mancha | [fidias.castillalamancha.es](https://fidias.castillalamancha.es) |
| incendiscat.cat | Catalunya | [incendiscat.cat](https://www.incendiscat.cat) |
| fogos.pt | Portugal | [fogos.pt](https://fogos.pt) |
| feuxdeforet.fr | France | [feuxdeforet.fr](https://www.feuxdeforet.fr) |
| LSA SAF FRP | ES/PT/FR (satellite) | [lasaf.ipma.pt](https://lasaf.ipma.pt) |
| X.com Monitor | Spain (regions w/o scraper) | [@112Arago](https://x.com/112Arago), [@112cmadrid](https://x.com/112cmadrid), [@emergenciascv](https://x.com/emergenciascv), [@112euskadi](https://x.com/112euskadi), [@112asturias](https://x.com/112asturias) |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Usage

```bash
# Run all scrapers
python -m fire_tracker.orchestrator

# Run X.com fire monitor
python -m fire_tracker.monitor

# Start API server
python -m fire_tracker.api.app
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Frontend (Leaflet map) |
| `/ping` | GET | Health check |
| `/api/fires/tracked` | GET | GeoJSON of active fires |
| `/api/fires/stats` | GET | Database statistics |
| `/api/frp` | GET | FRP satellite detections (GeoJSON) |
| `/api/cron/scrapers` | POST | Trigger official scrapers |
| `/api/cron/monitor` | POST | Trigger X.com monitor |
| `/api/cron/frp` | POST | Refresh FRP data |
| `/api/geocode?q=...` | GET | Nominatim geocoding |

## Architecture

| Module | Responsibility |
|--------|----------------|
| `scrapers/` | One scraper per data source |
| `database.py` | Dual SQLite/PostgreSQL |
| `orchestrator.py` | Runs scrapers, deduplicates, persists |
| `monitor.py` | X.com fire mention monitor |
| `frp.py` | LSA SAF FRP satellite data |
| `weather.py` | Nominatim geocoding + elevation |
| `api/app.py` | Flask REST API |

## License

GPL-3.0-or-later
