# AGENTS.md

## Project Overview

European wildfire tracking aggregator with weather data. Collects fire data from official platforms across Spain, Portugal, and France, deduplicates, and serves via REST API. Deploys on Render free tier with cron-job.org as external scheduler.

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
| `src/fire_tracker/database.py` | Dual persistence: SQLite (local dev) / PostgreSQL via psycopg2 (Supabase) |
| `src/fire_tracker/orchestrator.py` | Runs all scrapers, deduplicates (<500m, <3h), persists to DB |
| `src/fire_tracker/weather.py` | Nominatim geocoding + Open-Meteo elevation lookup |
| `src/fire_tracker/wx_stations.py` | WU PWS station discovery (tile API) + hourly history download |
| `src/fire_tracker/metar.py` | METAR station observations from aviationweather.gov (NOAA) |
| `src/fire_tracker/frp.py` | LSA SAF FRP-PIXEL data fetcher (MTG satellite fire detections) |
| `src/fire_tracker/xmonitor.py` | X.com fire mention scraper using twscrape (GraphQL API, cookies auth) |
| `src/fire_tracker/xgrok.py` | Grok chat client via X.com reverse-engineered API (AI-assisted fire queries) |
| `src/fire_tracker/frp_locator.py` | FRP-based fire locator (geocode → FRP bbox search → centroid) |
| `src/fire_tracker/monitor.py` | Fire monitor orchestrator (X #IF search + Grok queries → FRP cross-ref → DB) |
| `src/fire_tracker/api/app.py` | Flask REST API + cron trigger endpoints |
| `scripts/fidias_crossref.py` | Standalone FIDIAS CLM cross-reference script (local only) |

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

### CSV fields

| Field | Description |
|-------|-------------|
| `LONGITUDE`, `LATITUDE` | Fire pixel coordinates |
| `FRP` | Fire Radiative Power (MW) |
| `FIRE_CONFIDENCE` | Detection confidence (0-1) |
| `FRP_UNCERTAINTY` | FRP uncertainty (MW) |
| `PIXEL_SIZE` | Effective pixel area (km²) |
| `ACQTIME` | Acquisition time (YYYYMMDDHHmmss) |

## X.com Fire Monitor

| Component | Source | Auth | Status |
|-----------|--------|------|--------|
| `xmonitor.py` | X.com (GraphQL API) | Cookies (X_AUTH_TOKEN + X_CT0) | Working |
| `xgrok.py` | X.com Grok API | Cookies (X_AUTH_TOKEN + X_CT0) | Working |
| `frp_locator.py` | FRP + Nominatim | LSA SAF credentials | Working |
| `monitor.py` | Orchestrator | — | Working |

### How it works

1. **X Search**: Searches X.com for `#IF` hashtag (standard for fire reports in Spain)
2. **Official accounts**: Batched OR queries for emergency/fire accounts in regions without scrapers
3. **Grok queries**: AI-assisted fire detection per region, cross-referenced with existing fire list
4. **Location extraction**: Municipality name from `#IF{Municipio}` hashtag
5. **Geographic filter**: Iberian Peninsula bbox (lat 34-44, lon -10 to 5)
6. **FRP cross-reference**: Satellite detections within 15km of municipality
7. **Deduplication**: Tweet ID + proximity to existing fires
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

### Without scrapers (xmonitor via #IF + FRP)
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

## Weather & Geocoding

### Nominatim (geocoding)
- Free, no API key required
- Endpoint: `https://nominatim.openstreetmap.org`
- Rate limit: 1 request/second
- Usage: forward geocode (place name → coordinates) + reverse geocode (coordinates → place name)

### Open-Meteo (elevation)
- Free, no API key required
- Endpoint: `https://api.open-meteo.com/v1/elevation`
- Usage: get elevation for coordinates (used by frp_locator.py)

### Weather stations
- **WU PWS**: Discovery via tile API, hourly history download (requires WU_API_KEY)
- **METAR**: aviationweather.gov, ~100 stations in ES/PT/FR, no auth needed

## Key Design Patterns

- **FireIncident dataclass**: Normalized incident from any source
- **FireScraper ABC**: Each source implements `fetch() -> list[FireIncident]`
- **Location dataclass**: Geocoded location with coordinates and metadata
- **Accumulator pattern**: Orchestrator collects from all scrapers, deduplicates, persists
- **Deduplication**: Spatial (<500m) + temporal (<3h) matching across sources
- **State history**: Status changes tracked in `fire_history` table
- **Dual DB**: SQLite for local dev, PostgreSQL (Supabase) for production

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Frontend (Leaflet map) |
| `/ping` | GET | Health check (cron-job.org keepalive) |
| `/api/cron/run` | POST | Trigger all scrapers + monitor + FRP |
| `/api/cron/scrapers` | POST | Trigger only official scrapers |
| `/api/cron/monitor` | POST | Trigger only X monitor + Grok |
| `/api/cron/stations` | POST | Trigger station cache cleanup |
| `/api/fires/tracked` | GET | GeoJSON of active fires |
| `/api/fires/refresh` | POST | Re-run all scrapers |
| `/api/fires/stats` | GET | Database statistics |
| `/api/fires/{source}:{id}/chronology` | GET | Redirect to X.com #IF search |
| `/api/geocode?q=...` | GET | Nominatim geocoding |
| `/api/stations?lat=...&lon=...` | GET | WU PWS stations within radius |
| `/api/metar` | GET | METAR stations in ES/PT/FR |
| `/api/frp` | GET | LSA SAF FRP-PIXEL GeoJSON |

## External Dependencies

- `requests` — HTTP client
- `pyproj` — UTM→WGS84 conversion (INCyL only)
- `flask` — REST API
- `pandas` — Data processing
- `twscrape` — X.com GraphQL API scraper (cookies auth)
- `psycopg2-binary` — PostgreSQL adapter (Supabase)

**Removed** (no longer needed):
- `matplotlib` — Was for meteogram generation
- `numpy` — Was for meteogram calculations

## METAR Data

| Source | Auth | Coverage | Update Freq |
|--------|------|----------|-------------|
| aviationweather.gov (NOAA) | None | ~100 stations in ES/PT/FR | Hourly |

**ICAO prefixes**: LE (Spain), LP (Portugal), LF (France), LS (Switzerland)

---

## Deploy en Render — Plan de Implementación

### Arquitectura de producción

```
Render Web Service (free tier, 750 h/mes)
├── Dockerfile              ← python:3.11-slim + gunicorn
├── docker-entrypoint.sh    ← gunicorn (sin cron interno)
├── gunicorn.conf.py        ← 1 worker, port $PORT, timeout 120s
└── PostgreSQL externo      ← Supabase (500 MB free, permanente)

cron-job.org (gratis, ilimitado)
├── Keepalive               ← GET /ping cada 14 min
├── Scrapers               ← POST /api/cron/scrapers cada 60 min
├── Monitor                ← POST /api/cron/monitor cada 60 min
└── Stations cleanup       ← POST /api/cron/stations cada 60 min
```

### Stack de deploy

| Componente | Servicio | Coste |
|------------|----------|-------|
| Web service | Render free tier | $0 (750 h/mes) |
| Base de datos | Supabase free tier | $0 (500 MB, 7 días inactividad → keepalive lo previene) |
| Cron scheduler | cron-job.org | $0 (ilimitado, HTTP requests) |
| **Total** | | **$0/mes** |

### Variables de entorno (Render)

| Variable | Required | Usage |
|----------|----------|-------|
| `DATABASE_URL` | Sí | Supabase PostgreSQL connection string |
| `WU_API_KEY` | No | Weather Underground station data |
| `LSA_SAF_USER` / `LSA_SAF_PASS` | No | FRP satellite detections |
| `X_AUTH_TOKEN` / `X_CT0` | No | X.com fire monitoring |
| `PORT` | Auto | Render asigna automáticamente |

### Hitos de Implementación

#### Hito 1: Eliminar meteogram.py y todo lo relacionado

**Objetivo**: Eliminar el módulo de meteogramas completo (backend + frontend).

**Archivos a modificar**:
- `src/fire_tracker/meteogram.py` — ELIMINAR archivo completo
- `src/fire_tracker/api/app.py` — Eliminar endpoint `/api/meteogram.png`
- `src/fire_tracker/api/static/index.html` — Eliminar modal `#meteogram-modal`
- `src/fire_tracker/api/static/js/app.js` — Eliminar función `openMeteogram` y botones "Meteograma" en popups
- `src/fire_tracker/api/static/css/style.css` — Eliminar estilos `.modal`

**Tests**:
1. `test_metegram_module_deleted`: Verificar que `src/fire_tracker/meteogram.py` no existe
2. `test_no_meteogram_endpoint`: Verificar que `/api/meteogram.png` retorna 404
3. `test_no_meteogram_modal`: Verificar que `index.html` no contiene `#meteogram-modal`
4. `test_no_meteogram_js`: Verificar que `app.js` no contiene `openMeteogram`

**Criterio de éxito**: Todos los tests pasan. Si 2 o más fallan → PARAR y reevaluar.

---

#### Hito 2: Simplificar weather.py y migrar a Nominatim

**Objetivo**: Reducir weather.py de ~587 a ~120 líneas, eliminar dependencias de matplotlib/numpy del módulo.

**Archivos a modificar**:
- `src/fire_tracker/weather.py` — Reescribir: solo `Location`, `geocode()` con Nominatim, `get_elevation()` con Open-Meteo
- `src/fire_tracker/frp_locator.py` — Actualizar imports si es necesario
- `src/fire_tracker/api/app.py` — Actualizar endpoint `/api/geocode` para usar Nominatim

**Tests**:
1. `test_weather_module_exists`: Verificar que `weather.py` existe y es importable
2. `test_geocode_nominatim`: Verificar que `geocode("Madrid")` retorna Location con coords válidas
3. `test_get_elevation`: Verificar que `get_elevation(40.0, -3.5)` retorna número positivo
4. `test_no_matplotlib_import`: Verificar que `weather.py` no importa matplotlib
5. `test_geocode_endpoint`: Verificar que `/api/geocode?q=Madrid` retorna JSON con lat/lon

**Criterio de éxito**: Todos los tests pasan. Si 2 o más fallan → PARAR y reevaluar.

---

#### Hito 3: Adaptar database.py para PostgreSQL (Supabase)

**Objetivo**: Soporte dual SQLite (local) / PostgreSQL (Supabase).

**Archivos a modificar**:
- `src/fire_tracker/database.py` — Agregar soporte PostgreSQL con psycopg2, detectar `DATABASE_URL`
- `pyproject.toml` — Agregar `psycopg2-binary` a dependencias
- `requirements.txt` — Agregar `psycopg2-binary`

**Tests**:
1. `test_database_sqlite_local`: Verificar que sin `DATABASE_URL` usa SQLite
2. `test_database_postgresql`: Verificar que con `DATABASE_URL` usa PostgreSQL
3. `test_upsert_fire_sqlite`: Verificar upsert funciona en SQLite
4. `test_upsert_fire_postgresql`: Verificar upsert funciona en PostgreSQL (mock o Supabase test)
5. `test_state_history`: Verificar que `fire_history` se crea correctamente

**Criterio de éxito**: Todos los tests pasan. Si 2 o más fallan → PARAR y reevaluar.

---

#### Hito 4: Crear endpoints de cron en app.py

**Objetivo**: Crear endpoints para que cron-job.org dispare las tareas.

**Archivos a modificar**:
- `src/fire_tracker/api/app.py` — Agregar `/ping`, `/api/cron/run`, `/api/cron/scrapers`, `/api/cron/monitor`, `/api/cron/stations`

**Tests**:
1. `test_ping_endpoint`: Verificar que `GET /ping` retorna 200 con `{"status": "ok"}`
2. `test_cron_run_post`: Verificar que `POST /api/cron/run` retorna 200 con stats
3. `test_cron_scrapers_post`: Verificar que `POST /api/cron/scrapers` retorna 200
4. `test_cron_monitor_post`: Verificar que `POST /api/cron/monitor` retorna 200
5. `test_cron_stations_post`: Verificar que `POST /api/cron/stations` retorna 200
6. `test_cron_run_get_rejected`: Verificar que `GET /api/cron/run` retorna 405

**Criterio de éxito**: Todos los tests pasan. Si 2 o más fallan → PARAR y reevaluar.

---

#### Hito 5: Actualizar dependencias y Dockerfile

**Objetivo**: Optimizar dependencias y Docker para Render free tier.

**Archivos a modificar**:
- `pyproject.toml` — Quitar matplotlib/numpy, agregar psycopg2-binary
- `requirements.txt` — Sincronizar con pyproject.toml
- `Dockerfile` — Optimizar: excluir scripts/, xmonitor_accounts.db, .env, data/*.db
- `docker-entrypoint.sh` — Sin cron interno (cron-job.org lo maneja)
- `gunicorn.conf.py` — Ajustar a 1 worker, port desde $PORT

**Tests**:
1. `test_pyproject_no_matplotlib`: Verificar que pyproject.toml no lista matplotlib
2. `test_pyproject_no_numpy`: Verificar que pyproject.toml no lista numpy
3. `test_pyproject_has_psycopg2`: Verificar que pyproject.toml lista psycopg2-binary
4. `test_dockerfile_no_scripts`: Verificar que Dockerfile excluye scripts/
5. `test_dockerfile_no_db`: Verificar que Dockerfile excluye data/*.db
6. `test_gunicorn_config`: Verificar que gunicorn.conf.py usa port desde env

**Criterio de éxito**: Todos los tests pasan. Si 2 o más fallan → PARAR y reevaluar.

---

#### Hito 6: Crear render.yaml y documentación

**Objetivo**: Archivo de configuración para Render + documentación de setup.

**Archivos a crear/modificar**:
- `render.yaml` — Web Service Docker free tier
- `AGENTS.md` — Actualizar sección de deploy con instrucciones completas

**Tests**:
1. `test_render_yaml_exists`: Verificar que render.yaml existe
2. `test_render_yaml_valid`: Verificar que render.yaml tiene campos requeridos (services, envVars)
3. `test_render_yaml_free_tier`: Verificar que render.yaml especifica plan: free
4. `test_agents_md_updated`: Verificar que AGENTS.md contiene sección "Render Deploy"

**Criterio de éxito**: Todos los tests pasan. Si 2 o más fallan → PARAR y reevaluar.

---

#### Hito 7: Deploy y verificación en Render

**Objetivo**: Deploy funcional en Render + cron-job.org configurado.

**Pasos manuales** (no automatizados):
1. Crear cuenta Supabase, crear proyecto, ejecutar SQL de tablas
2. Push a GitHub, crear Render service desde repo
3. Configurar variables de entorno en Render
4. Crear cron-job.org account, configurar 4 jobs
5. Verificar que `/ping` responde
6. Verificar que `/api/cron/run` ejecuta scrapers

**Tests de verificación**:
1. `test_render_ping`: Verificar que `https://fire-tracker.onrender.com/ping` retorna 200
2. `test_render_tracked`: Verificar que `https://fire-tracker.onrender.com/api/fires/tracked` retorna GeoJSON
3. `test_supabase_connection`: Verificar conexión a Supabase desde Render
4. `test_cron_job_configured`: Verificar que cron-job.org tiene 4 jobs activos

**Criterio de éxito**: Todos los tests pasan. Si 2 o más fallan → PARAR y reevaluar.

---

### Regla de Parada

**Si 2 o más tests fallan en el mismo hito, se detiene la implementación para reevaluar los fallos.**

Motivos posibles de fallo múltiple:
- Dependencias circulares entre módulos
- Error de arquitectura (mal diseño de interfaces)
- Incompatibilidad de versiones de dependencias
- Fallo en cadena (un cambio afecta múltiples módulos)

Acción al fallar:
1. Documentar todos los fallos en `FAILURES.md`
2. Analizar causa raíz
3. Revisar diseño si es problema de arquitectura
4. Ajustar plan si es necesario
5. Reanudar desde el hito fallido

---

## Deploy local (desarrollo)

```bash
# SQLite (default)
python -m fire_tracker.api.app

# Con Supabase
DATABASE_URL=postgresql://user:pass@host:5432/dbname python -m fire_tracker.api.app
```

## Deploy en Render

### Prerrequisitos
1. Cuenta en [Supabase](https://supabase.com) (free tier)
2. Cuenta en [Render](https://render.com) (free tier)
3. Cuenta en [cron-job.org](https://cron-job.org) (gratis)
4. Repositorio en GitHub

### Paso 1: Supabase
1. Crear proyecto en Supabase
2. Ir a SQL Editor y ejecutar el schema de fires
3. Copiar la connection string (Settings → Database → Connection string → URI)

### Paso 2: Render
1. Crear Web Service → Docker
2. Conectar repositorio de GitHub
3. Configurar variables de entorno:
   - `DATABASE_URL`: connection string de Supabase
   - `WU_API_KEY` (opcional): API key de Weather Underground
   - `LSA_SAF_USER` / `LSA_SAF_PASS` (opcional): credenciales LSA SAF
   - `X_AUTH_TOKEN` / `X_CT0` (opcional): cookies de X.com
4. Deploy

### Paso 3: cron-job.org
1. Crear cuenta en cron-job.org
2. Crear 4 jobs:

| Job | URL | Method | Interval |
|-----|-----|--------|----------|
| Keepalive | `https://fire-tracker.onrender.com/ping` | GET | Every 14 minutes |
| Scrapers | `https://fire-tracker.onrender.com/api/cron/scrapers` | POST | Every 60 minutes |
| Monitor | `https://fire-tracker.onrender.com/api/cron/monitor` | POST | Every 60 minutes |
| Stations | `https://fire-tracker.onrender.com/api/cron/stations` | POST | Every 60 minutes |

### Variables de entorno

| Variable | Required | Usage |
|----------|----------|-------|
| `DATABASE_URL` | Sí | Supabase PostgreSQL connection string |
| `WU_API_KEY` | No | Weather Underground station data |
| `LSA_SAF_USER` / `LSA_SAF_PASS` | No | FRP satellite detections |
| `X_AUTH_TOKEN` / `X_CT0` | No | X.com fire monitoring |

### Costes
- Render free tier: $0 (750 h/mes)
- Supabase free tier: $0 (500 MB)
- cron-job.org: $0 (ilimitado)
- **Total: $0/mes**

### SQL para crear tablas en Supabase

```sql
CREATE TABLE fires (
    id SERIAL PRIMARY KEY,
    source VARCHAR(50) NOT NULL,
    external_id VARCHAR(100) NOT NULL,
    title VARCHAR(500),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    description TEXT,
    status VARCHAR(50),
    fire_type VARCHAR(50),
    hectares DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    raw_data JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(source, external_id)
);

CREATE TABLE fire_history (
    id SERIAL PRIMARY KEY,
    fire_id INTEGER REFERENCES fires(id),
    status VARCHAR(50),
    changed_at TIMESTAMP DEFAULT NOW(),
    source VARCHAR(50)
);

CREATE TABLE stations (
    id SERIAL PRIMARY KEY,
    station_id VARCHAR(50) UNIQUE,
    name VARCHAR(200),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    elevation DOUBLE PRECISION,
    source VARCHAR(50),
    last_reading JSONB,
    last_updated TIMESTAMP
);

CREATE INDEX idx_fires_source ON fires(source);
CREATE INDEX idx_fires_coords ON fires(latitude, longitude);
CREATE INDEX idx_fires_created ON fires(created_at);
CREATE INDEX idx_fire_history_fire_id ON fire_history(fire_id);
```
