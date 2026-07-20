# AGENTS.md

## Project Overview

European wildfire tracking aggregator with weather data. Collects fire data from official platforms across Spain, Portugal, and France, deduplicates, and serves via REST API. Includes full wildfire weather meteogram using Open-Meteo best_match (5-panel chart matching open-meteograms).

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
| `src/fire_tracker/weather.py` | Geocoding + Open-Meteo best_match fetcher (surface + pressure levels) + transforms (Fosberg FM, VPD FM, ignition prob, C-Haines, BLH Richardson) |
| `src/fire_tracker/meteogram.py` | 5-panel wildfire weather meteogram (matplotlib, MPLBACKEND=Agg) |
| `src/fire_tracker/wx_stations.py` | WU PWS station discovery (tile API) + hourly history download |
| `src/fire_tracker/metar.py` | METAR station observations from aviationweather.gov (NOAA) — free, no auth |
| `src/fire_tracker/frp.py` | LSA SAF FRP-PIXEL data fetcher (MTG satellite fire detections) |
| `src/fire_tracker/xmonitor.py` | X.com fire mention scraper using twscrape (GraphQL API, cookies auth) |
| `src/fire_tracker/xgrok.py` | Grok chat client via X.com reverse-engineered API (for AI-assisted fire queries) |
| `src/fire_tracker/frp_locator.py` | FRP-based fire locator (geocode → FRP bbox search → centroid) |
| `src/fire_tracker/monitor.py` | Fire monitor orchestrator (X search → FRP cross-reference → DB) |
| `src/fire_tracker/api/app.py` | Flask REST API |
| `src/fire_tracker/gradio_app.py` | Gradio + FastAPI deployment (HF Spaces) |
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

## FRP (Fire Radiative Power)

Satellite fire detection data from LSA SAF (IPMA/LSA SAF).

| Source | Platform | Auth | Resolution | Update Freq |
|--------|----------|------|------------|-------------|
| LSA SAF FRP-PIXEL | MTG (FCI) | Basic Auth (LSA_SAF_USER/LSA_SAF_PASS) | 1 km² | 10 min |

### Data access

- **WMS overlay**: `https://adaguc.lsasvcs.ipma.pt//adaguc-server?DATASET=MTG-FRP&SERVICE=WMS&`
- **CSV downloads**: `https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/NATIVE/{YYYY}/{MM}/{DD}/`
- **Viewer**: `https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/`

### CSV fields

| Field | Description |
|-------|-------------|
| `LONGITUDE`, `LATITUDE` | Fire pixel coordinates |
| `FRP` | Fire Radiative Power (MW) |
| `FIRE_CONFIDENCE` | Detection confidence (0-1) |
| `FRP_UNCERTAINTY` | FRP uncertainty (MW) |
| `PIXEL_SIZE` | Effective pixel area (km²) |
| `ACQTIME` | Acquisition time (YYYYMMDDHHmmss) |

### Usage note

FRP detections can be used to **locate fires where official sources don't provide coordinates** (e.g., Madrid region). Cross-reference FRP points with declared fires by timestamp and proximity.

## X.com Fire Monitor

Monitors X/Twitter for wildfire mentions in ES/PT/FR using twscrape (GraphQL API, cookies auth). Cross-references with FRP satellite data for precise location.

| Component | Source | Auth | Status |
|-----------|--------|------|--------|
| `xmonitor.py` | X.com (GraphQL API) | Cookies (X_AUTH_TOKEN + X_CT0) | Working |
| `xgrok.py` | X.com Grok API | Cookies (X_AUTH_TOKEN + X_CT0) | Working |
| `frp_locator.py` | FRP + Nominatim | LSA SAF credentials | Working |
| `monitor.py` | Orchestrator | — | Working |

### Setup

1. Log into x.com in your browser
2. F12 → Application → Cookies → x.com
3. Copy `auth_token` and `ct0` values to `.env`:
   ```
   X_AUTH_TOKEN=your_auth_token_here
   X_CT0=your_ct0_here
   ```

### Usage

```bash
# Single run
python -m fire_tracker.monitor

# Custom parameters
python -m fire_tracker.monitor --hours-back 4 --limit 30

# Cron (every hour)
0 * * * * cd ~/Proyectos/fire-tracker && .venv/bin/python -m fire_tracker.monitor
```

### How it works

1. **X Search**: Searches X.com for `#IF` hashtag (the standard for fire reports in Spain)
2. **Official accounts**: Batched OR queries for known emergency/fire accounts in regions without scrapers (3 batches of 6)
3. **Location extraction**: Extracts municipality name directly from `#IF{Municipio}` hashtag (most reliable)
4. **Geographic filter**: Only keeps fires within Iberian Peninsula bbox (lat 34-44, lon -10 to 5) to reject false positives
5. **FRP cross-reference**: Searches FRP satellite detections within 15km of the municipality
6. **Fire location**: Uses FRP centroid if detections found, geocoded coords otherwise
7. **Deduplication**: Checks tweet ID and proximity to existing fires
8. **Database**: Adds new fires with source="xmonitor" + chronology URL

### Official accounts monitored (Spain only — regions without scrapers)

| Region | Accounts |
|--------|----------|
| Aragón | 112Arago, IIFFAragon |
| Madrid | 112cmadrid, bomberos_infoma, AT_Brif, BBFFMadrid |
| Comunidad Valenciana | emergenciascv |
| País Vasco + Navarra + La Rioja | 112euskadi, 112_na, BBFFLaRioja, MAmbienteRioja |
| Extremadura | PLANINFOEX, JuntaEx112 |
| Cantabria | 112CantabriaV2 |
| Asturias | 112asturias |
| Baleares | Emergencies_112 |
| Murcia | 112Murcia |

### Search strategy

- **Only `#IF` hashtag** — the standard for fire reports in Spain (`#IF{Municipio}`)
- **Official account batches** — `from:handle1 OR from:handle2 ...` (3 batches of 6)
- **Total: ~4 API calls per cycle** — manageable with 1 twscrape account running hourly
- **No PT/FR** — reliable official scrapers already cover Portugal and France

### Hashtag #IF convention

Spanish Twitter/X uses `#IF{Municipio}` to report active wildfires:
- `#IFJaen` — Incendio en Jaén
- `#IFCordoba` — Incendio en Córdoba
- `#IFVillanuevaDelRosario` — Multi-word municipality in CamelCase

The monitor searches for `#IF` as primary query and extracts the municipality name directly from the hashtag, which is more reliable than regex pattern matching on tweet text.

## Regions monitored

### With official scrapers
| Region | Scraper | Source |
|--------|---------|--------|
| Andalucía | InfocaAndaluciaScraper | INFOCA |
| Cataluña | IncendiscatCatScraper | incendiscat.cat |
| Castilla y León | IncendiosCyLScraper | INCyL |
| Castilla-La Mancha | FidiasClmScraper | FIDIAS CLM + FRP |
| Portugal | FogosPtScraper | fogos.pt |
| Francia | FeuxDeForetFrScraper | feuxdeforet.fr |

### Without scrapers (xmonitor via #IF hashtags + FRP)
| Region | Accounts |
|--------|----------|
| Aragón | 112Arago, IIFFAragon |
| Madrid | 112cmadrid, bomberos_infoma, AT_Brif, BBFFMadrid |
| Comunidad Valenciana | emergenciascv |
| Extremadura | PLANINFOEX, JuntaEx112 |
| País Vasco | 112euskadi |
| Navarra | 112_na |
| La Rioja | BBFFLaRioja, MAmbienteRioja |
| Cantabria | 112CantabriaV2 |
| Asturias | 112asturias |
| Baleares | Emergencies_112 |
| Murcia | 112Murcia |

### Excluded from xmonitor
| Region | Reason |
|--------|--------|
| Canarias | Outside Iberian Peninsula — not monitored |
| Galicia | Pending scraper implementation |

## Chronology URL

Each fire detected via xmonitor includes a `chronology_url` in `raw_data`:
```
https://x.com/search?q=%23IF{Municipio}&src=typed_query&f=live
```

API endpoint: `GET /api/fires/{source}:{external_id}/chronology` — redirects to the X.com search.

## Grok Integration (xgrok.py)

Uses X.com's reverse-engineered Grok API to query AI about fires per region. Intended for region-by-region queries with existing fire list for deduplication.

| Endpoint | Purpose |
|----------|---------|
| `CreateGrokConversation` (GraphQL) | Create new conversation |
| `add_response.json` (REST) | Send message, get response |

**Status**: Module created, API connection verified (conversation creation works). Plan is to iterate autonomous communities, passing existing fire list for deduplication and location correction.

## Weather & Meteogram

Uses Open-Meteo API (https://open-meteo.com) with "Best Match" model selection.

### Data pipeline

1. **Surface fetch** — hourly variables: temperature_2m, relative_humidity_2m, dew_point_2m, apparent_temperature, wind_speed_10m, wind_direction_10m, wind_gusts_10m, vapour_pressure_deficit, is_day, precipitation, weather_code, cloud_cover, pressure_msl, surface_pressure, shortwave_radiation, cape
2. **Pressure level fetch** — hourly variables: boundary_layer_height + temperature/RH/wind_speed/wind_direction/geopotential_height at 1000, 925, 850, 700, 600, 500, 250 hPa
3. **Transforms** — Fosberg FM1h, Resco VPD FM10h, ignition probability, C-Haines index, BLH Richardson fallback, wind u/v components, inversions

### Meteogram panels (5-panel wildfire weather chart)

| Panel | Content |
|-------|---------|
| 0 (top) | **Wind profile**: wind barbs at 700/600/500/250 hPa + BLH (red dashed) + C-Haines colored bar + inversions |
| 1 | **Wind**: surface wind speed (green) + gusts (orange fill) + direction arrows |
| 2 | **Temp/RH**: temperature (red) + dew point (grey dashed) + RH (blue, right axis) |
| 3 (bottom) | **Fuel**: Fosberg FM1h (orange dashed) + Resco VPD FM10h (red) + ignition probability semaphore |

### Key formulas

- **Fosberg FM**: Table A lookup indexed by is_day, temperature bin, RH
- **Resco VPD FM**: `FM = 3.5 + 28 * exp(-1.5 * VPD_kPa)` — calibrated on BONFIRE global dataset
- **Ignition probability**: 9×16 lookup table indexed by temperature (5°C bins) × fuel moisture

### Weather stations (WU PWS)

Uses Weather Underground Personal Weather Stations API:
- **Discovery**: tile API (`products/614`) — searches within radius, returns stations with current obs
- **History**: `pws/history/hourly` — monthly pagination, metric units
- Requires `WU_API_KEY` in environment or passed as query parameter
- Station discovery uses bounding box + haversine distance filter (50km default, max 100km)
- Returns sorted by distance from center point
- **C-Haines**: `A + B + C` (range 0–9), thermal lapse + low-level dryness + upper-level dryness from 850/700 hPa
- **BLH**: API value with Bulk Richardson number fallback (Ri_crit=0.25, 4 levels 1000–700 hPa)

## Key Design Patterns

- **FireIncident dataclass**: Normalized incident from any source
- **FireScraper ABC**: Each source implements `fetch() -> list[FireIncident]`
- **Location dataclass**: Geocoded location with coordinates and metadata
- **WeatherData**: Container for surface + vertical DataFrames
- **Accumulator pattern**: Orchestrator collects from all scrapers, deduplicates, persists
- **Deduplication**: Spatial (<500m) + temporal (<3h) matching across sources
- **State history**: Status changes tracked in `fire_history` table

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/fires/tracked` | GET | GeoJSON of active fires |
| `/api/fires/refresh` | POST | Re-run all scrapers |
| `/api/fires/stats` | GET | Database statistics |
| `/api/fires/{source}:{id}/chronology` | GET | Redirect to X.com #IF search |
| `/api/geocode?q=...` | GET | Search locations |
| `/api/meteogram.png?lat=...&lon=...` | GET | Full 5-panel meteogram (PNG) |
| `/api/stations?lat=...&lon=...` | GET | WU PWS stations within radius (JSON) |
| `/api/metar` | GET | METAR stations in ES/PT/FR with current obs (JSON) |
| `/api/frp` | GET | LSA SAF FRP-PIXEL GeoJSON (latest detections) |

## External Dependencies

- `requests` — HTTP client
- `pyproj` — UTM→WGS84 conversion (INCyL only)
- `flask` — REST API
- `gradio` — Gradio UI (HF Spaces deployment)
- `uvicorn` — ASGI server (Gradio app)
- `matplotlib` — Meteogram generation
- `numpy` — Numerical operations
- `pandas` — Data processing
- `twscrape` — X.com GraphQL API scraper (cookies auth)

## METAR Data

METAR (Meteorological Aerodrome Report) provides professional-grade aviation weather observations from airports.

| Source | Auth | Coverage | Update Freq |
|--------|------|----------|-------------|
| aviationweather.gov (NOAA) | None | ~100 stations in ES/PT/FR | Hourly |

**API endpoints**:
- Station info: `GET /api/data/stationinfo?bbox=s,w,n,e&format=json`
- Current METAR: `GET /api/data/metar?ids=LEMD,LEBL,LPPT&format=json`
- Fields: temp (°C), dewp (°C), wspd (knots), wdir (°), altim (hPa), visib, fltCat

**ICAO prefixes**: LE (Spain), LP (Portugal), LF (France), LS (Switzerland)

**Coverage**: ~40 Spain, ~9 Portugal, ~48 France, ~2 Switzerland

**Usage**: METAR stations appear alongside WU PWS stations when searching for weather data. Each station links to metar-taf.com for detailed TAF forecasts.

## HF Spaces Deployment

Automated deployment to Hugging Face Spaces (Docker SDK, CPU Basic free tier, 16GB persistent storage).

### Architecture

```
HF Space (Docker)
├── Dockerfile              ← python:3.11-slim + cron + gunicorn
├── docker-entrypoint.sh    ← init orchestrator + cron 30min + gunicorn
├── gunicorn.conf.py        ← 2 workers, port 7860, timeout 120s
└── /data/                  ← HF persistent storage
    └── fires.db            ← SQLite (auto-created)
```

### Environment variables (HF Secrets)

| Variable | Required | Usage |
|----------|----------|-------|
| `DB_PATH` | No | Defaults to `/data/fires.db` in HF, `data/fires.db` locally |
| `WU_API_KEY` | No | Weather Underground station data |
| `LSA_SAF_USER` / `LSA_SAF_PASS` | No | FRP satellite detections |
| `X_AUTH_TOKEN` / `X_CT0` | No | X.com fire monitoring |

### Milestones

| # | Milestone | Files | Status |
|---|-----------|-------|--------|
| 1 | DB_PATH env var | `orchestrator.py`, `api/app.py` | ✅ |
| 2 | gunicorn dependency | `pyproject.toml` | ✅ |
| 3 | gunicorn config | `gunicorn.conf.py` | ✅ |
| 4 | Dockerfile | `Dockerfile` | ✅ |
| 5 | Entry point | `docker-entrypoint.sh` | ✅ |
| 6 | Local test (gunicorn) | `requirements.txt` | ✅ |
| 7 | HF Space deploy | GitHub push + HF config | ⏳ |

### Notes

- Data path: single DB, two contexts (local `data/`, HF `/data/`). Never coexist.
- `.gitignore` excludes `data/*.db` — DB never enters git
- HF free tier: 512MB RAM, 16GB storage, sleep on idle (~30s cold start)
- Cron runs inside Docker container (not HF native)

## HF Spaces Deployment (Gradio)

Alternative deployment using Gradio SDK (free on HF, no Docker required).

### Architecture

```
HF Space (Gradio)
├── gradio_app.py          ← FastAPI + Gradio Blocks (Leaflet map, sidebar, popups)
├── /data/                  ← HF persistent storage
│   └── fires.db            ← SQLite (auto-created)
└── pyproject.toml          ← gradio>=4.0 in dependencies
```

### Milestones

| # | Milestone | Files | Status |
|---|-----------|-------|--------|
| 1 | DB_PATH env var | `orchestrator.py`, `api/app.py` | ✅ |
| 2 | gunicorn + gradio deps | `pyproject.toml` | ✅ |
| 3 | Gradio app | `gradio_app.py` | ✅ |
| 4 | HF Space deploy | HF config | ⏳ |

### API endpoints (mounted on FastAPI)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/fires/tracked` | GET | GeoJSON of active fires |
| `/api/fires/refresh` | POST | Re-run all scrapers |
| `/api/geocode?q=...` | GET | Search locations |
| `/api/meteogram?lat=...&lon=...` | GET | Full 5-panel meteogram (PNG) |
| `/api/stations?lat=...&lon=...` | GET | WU PWS stations within radius (JSON) |
| `/api/metar` | GET | METAR stations in ES/PT/FR with current obs (JSON) |
| `/api/frp` | GET | LSA SAF FRP-PIXEL GeoJSON (latest detections) |
| `/api/refresh` | POST | Re-run all scrapers + return stats |

### Notes

- Reuses all existing modules (scrapers, database, weather, meteogram)
- FastAPI serves custom API routes; Gradio mounts at `/` via `gr.mount_gradio_app`
- Gradio 6.x: `theme`, `css`, `js` passed to `mount_gradio_app()`, not `Blocks()` constructor
- JS calls `/api/...` directly (not `/gradio_api/api/...`)
- Branch: `feat/gradio-ui`

## Pending

- **FRP cross-reference**: Use FRP detections to locate declared fires where official sources lack coordinates (e.g., Madrid region). Match by timestamp and proximity (<1km, <1h).
- Madrid: no official real-time source (only satellite data)
- incendiscat.cat HMAC key rotation monitoring
- Mobile-friendly frontend (Leaflet map + meteogram modal)
- Station data overlay on meteogram (scatter markers for PWS observations)

## Future: Station Mapping Project

**Goal**: Build temperature, humidity, and wind maps at large scale by interpolating station data based on elevation.

**Data sources**:
- **METAR** — aviationweather.gov API (free, no auth, JSON, ~100 stations in ES/PT/FR)
- **WU PWS** — Weather Underground personal weather stations (requires API key, dense coverage)

**Approach**:
1. Periodic sync (cron) of METAR stations via `aviationweather.gov/api/data/stationinfo?bbox=s,w,n,e`
2. Fetch current METAR observations for all stations in bbox
3. Store in SQLite: `stations` (metadata) + `station_observations` (hourly)
4. Interpolation: IDW or kriging weighted by elevation difference
5. Render as tile overlay layers (temperature, humidity, wind) on the Leaflet map

**METAR API** (aviationweather.gov):
- Station info: `GET /api/data/stationinfo?bbox=34,-12,51,10&format=json`
- Current METAR: `GET /api/data/metar?ids=LEMD,LEBL,LPPT&format=json`
- Fields: temp (°C), dewp (°C), wspd (knots), wdir (°), altim (hPa), visib, fltCat
- Rate limit: 100 req/min, no auth needed
- ~57 stations Spain, ~12 Portugal, ~30+ France (south)
