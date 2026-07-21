"""
Fire Tracker — Gradio UI with Leaflet map.

Full-screen wildfire map with sidebar, served via FastAPI + Gradio 6.
Designed for Hugging Face Spaces deployment.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import gradio as gr
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response

_root = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get('DB_PATH', str(_root / 'data' / 'fires.db')))

from fire_tracker.database import FireDatabase
from fire_tracker.orchestrator import FireOrchestrator
from fire_tracker.weather import geocode as wx_geocode, Location
from fire_tracker.meteogram import meteogram_to_png
from fire_tracker.wx_stations import fetch_wu_stations_near
from fire_tracker.frp import _get_age_color, _WINDOW_HOURS
from fire_tracker.metar import fetch_metar_stations

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)

_db = FireDatabase(DB_PATH)

SOURCE_MAIN_URLS = {
    'infoca': 'https://www.juntadeandalucia.es/institucion/junta-de-andalucia/area-de-agricultura-ganaderia-pesca-y-desarrollo-sostenible/consejeria-de-agricultura-ganaderia-pesca-y-desarrollo-sostenible/medio-forestal/incendios-forestales',
    'feuxdeforet.fr': 'https://feuxdeforet.fr/',
    'incendiscat.cat': 'https://incendiscat.cat/',
    'fogos.pt': 'https://fogos.pt/',
    'incendios_cyl': 'https://servicios.jcyl.es/incyl/incyl',
    'fidias_clm': 'https://fidias.castillalamancha.es/',
}

SOURCE_LABELS = {
    'infoca': 'INFOCA (Andalucia)',
    'feuxdeforet.fr': 'feuxdeforet.fr (Francia)',
    'incendiscat.cat': 'incendiscat.cat (Catalunya)',
    'fogos.pt': 'fogos.pt (Portugal)',
    'incendios_cyl': 'InCyL (Castilla y Leon)',
    'fidias_clm': 'FIDIAS (Castilla-La Mancha)',
}


# ── Shared GeoJSON builders ──────────────────────────────────────────────────

def _fires_to_geojson(fires: list[dict]) -> dict:
    features = []
    for f in fires:
        if f.get('latitude') is None or f.get('longitude') is None:
            continue
        raw = f.get('raw_data', {})
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                raw = {}
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [f['longitude'], f['latitude']]},
            'properties': {
                'id': f'{f["source"]}:{f["external_id"]}',
                'source': f['source'],
                'source_label': SOURCE_LABELS.get(f['source'], f['source']),
                'external_id': f['external_id'],
                'source_url': f.get('source_url') or SOURCE_MAIN_URLS.get(f['source']),
                'chronology_url': raw.get('chronology_url', ''),
                'municipality': f.get('municipality'),
                'province': f.get('province'),
                'region': f.get('region'),
                'country': f.get('country'),
                'status': f.get('status'),
                'fire_type': f.get('fire_type'),
                'detection_date': f.get('detection_date'),
                'area_ha': f.get('area_ha'),
                'resources': f.get('resources'),
                'last_updated': f.get('last_updated'),
            },
        })
    return {'type': 'FeatureCollection', 'features': features}


def _frp_to_geojson(detections: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    features = []
    for d in detections:
        try:
            acq = datetime.fromisoformat(d['acquisition_time'])
        except (ValueError, TypeError):
            acq = now
        age_hours = (now - acq).total_seconds() / 3600.0
        color, size = _get_age_color(age_hours, d['frp_mw'])
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [d['longitude'], d['latitude']]},
            'properties': {
                'frp_mw': round(d['frp_mw'], 1),
                'confidence': round(d['confidence'], 1) if d.get('confidence') else 0,
                'frp_uncertainty': round(d['frp_uncertainty'], 1) if d.get('frp_uncertainty') else 0,
                'pixel_size_km2': round(d['pixel_size_km2'], 2) if d.get('pixel_size_km2') else 0,
                'acquisition_time': d['acquisition_time'],
                'bt_tir': round(d['bt_tir'], 1) if d.get('bt_tir') else 0,
                'color': color, 'radius': size,
                'age_hours': round(age_hours, 1),
            },
        })
    return {'type': 'FeatureCollection', 'features': features}


# ── Gradio callbacks ─────────────────────────────────────────────────────────

def _get_fires_json() -> str:
    return json.dumps(_fires_to_geojson(_db.get_active_fires()), ensure_ascii=False)


def _get_frp_json() -> str:
    return json.dumps(_frp_to_geojson(_db.get_frp_detections(hours=_WINDOW_HOURS)), ensure_ascii=False)


def _do_refresh() -> str:
    orch = FireOrchestrator(DB_PATH)
    stats = orch.run()
    return (
        f"Raw: {stats['total_raw']} → Dedup: {stats['total_after_dedup']} → "
        f"Upserted: {stats['upserted']} (DB: {stats['total_in_db']}) en {stats['duration_s']:.1f}s"
    )


def _do_meteogram(lat: float, lon: float, name: str):
    from fire_tracker.weather import fetch_forecast
    loc = Location(name=name, latitude=lat, longitude=lon, elevation=0)
    weather = fetch_forecast(loc, forecast_days=2, past_days=0)
    if weather is None:
        return None
    return meteogram_to_png(weather, figsize=(14, 10))


# ── FastAPI routes (for JS fetch calls) ──────────────────────────────────────

def _mount_api_routes(app: FastAPI) -> None:

    @app.get('/api/fires/tracked')
    def api_fires_tracked():
        return JSONResponse(_fires_to_geojson(_db.get_active_fires()))

    @app.get('/api/frp')
    def api_frp():
        return JSONResponse(_frp_to_geojson(_db.get_frp_detections(hours=_WINDOW_HOURS)))

    @app.post('/api/refresh')
    def api_refresh():
        try:
            stats = _do_refresh()
            return JSONResponse({'ok': True, 'stats': stats})
        except Exception as e:
            logger.error('Refresh error: %s', e)
            return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)

    @app.get('/api/geocode')
    def api_geocode(q: str = Query(...), limit: int = Query(5)):
        locations = wx_geocode(q, limit=limit)
        results = [
            {'name': loc.name, 'latitude': loc.latitude, 'longitude': loc.longitude,
             'display_name': loc.display_name}
            for loc in locations
        ]
        return JSONResponse({'results': results})

    @app.get('/api/stations')
    def api_stations(lat: float = Query(...), lon: float = Query(...), radius_km: float = Query(30)):
        try:
            stations = fetch_wu_stations_near(lat, lon, radius_km=radius_km)
            return JSONResponse({'stations': stations})
        except Exception as e:
            logger.warning('WU stations error: %s', e)
            return JSONResponse({'stations': [], 'error': str(e)})

    @app.get('/api/metar')
    def api_metar():
        try:
            stations = fetch_metar_stations()
            return JSONResponse({'stations': stations})
        except Exception as e:
            logger.warning('METAR error: %s', e)
            return JSONResponse({'stations': [], 'error': str(e)})

    @app.get('/api/meteogram')
    def api_meteogram(lat: float = Query(...), lon: float = Query(...), name: str = Query('')):
        try:
            img_bytes = _do_meteogram(lat, lon, name)
            if img_bytes is None:
                return JSONResponse({'error': 'No se pudo generar el meteograma'}, status_code=404)
            return Response(content=img_bytes, media_type='image/png')
        except Exception as e:
            logger.warning('Meteogram error: %s', e)
            return JSONResponse({'error': str(e)}, status_code=500)

    @app.get('/map')
    def map_page():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=_build_map_html())

    @app.get('/')
    def root_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url='/map')


# ── Head HTML (Leaflet CDN) ──────────────────────────────────────────────────

def _build_map_html() -> str:
    """Build standalone HTML page for the fire map (no Gradio dependency)."""
    # Strip IIFE wrapper from JS_CODE to get the raw functions
    js = JS_CODE
    # Remove the (() => { and })(); wrapper
    js = js.replace('(() => {\n', '').rstrip()
    if js.endswith('})();'):
        js = js[:-5]
    # Make _initApp the entry point
    js = js.replace('function _initApp() {', 'function initApp() {')
    js = js.replace('window.initApp = _initApp;', '')

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fire Tracker</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<style>
{CSS}
</style>
</head>
<body>
{MAP_HTML}
<script>
{js}

// Auto-init when DOM ready
if (document.readyState === 'loading') {{
  document.addEventListener('DOMContentLoaded', function() {{
    if (document.getElementById('fire-map') && window.L) initApp();
  }});
}} else {{
  if (document.getElementById('fire-map') && window.L) initApp();
}}
</script>
</body>
</html>"""


HEAD_HTML = """
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      crossorigin="" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
"""


# ── CSS ──────────────────────────────────────────────────────────────────────

CSS = """
:root {
    --bg: #1a1b2e;
    --surface: #252640;
    --surface2: #2e3050;
    --text: #d4d4d8;
    --text-muted: #9ca3af;
    --accent: #6b8cce;
    --accent-hover: #8ba8de;
    --border: #3a3d5c;
    --radius: 8px;
}

/* ── Full-viewport Gradio override ── */
html, body, gradio-app, gradio-app > div {
    margin: 0 !important; padding: 0 !important;
    height: 100vh !important; width: 100vw !important;
    overflow: hidden !important;
    background: var(--bg) !important;
}

/* Make Gradio container fill viewport and be transparent */
.gradio-container {
    max-width: none !important;
    width: 100vw !important;
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    background: transparent !important;
}

/* Hide Gradio chrome but NOT the container */
footer, .gr-header, .gr-topbar, .prose, .container { display: none !important; }

/* Strip padding from Gradio wrappers around gr.HTML */
.gradio-html, .gr-html, .gr-block, .gr-group,
.gradio-html > div, .gr-html > div, .gr-block > div, .gr-group > div,
form {
    padding: 0 !important; margin: 0 !important; border: none !important;
    background: transparent !important; min-height: 0 !important; overflow: visible !important;
}

/* ── Main layout ── */
#fire-app { display: flex; height: 100vh; width: 100%; background: var(--bg); overflow: hidden; position: relative; }

/* ── Sidebar ── */
#fire-sidebar {
    width: 340px; min-width: 340px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    overflow: hidden; z-index: 1000;
    position: relative; height: 100%;
}
#fire-sidebar header {
    padding: 16px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
}
#fire-sidebar header h1 { font-size: 1.2rem; font-weight: 700; color: var(--accent); margin: 0; }
#sidebar-search { padding: 12px 16px; border-bottom: 1px solid var(--border); position: relative; }
#sidebar-search input {
    width: 100%; padding: 8px 12px;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: var(--radius); color: var(--text); font-size: 0.9rem; outline: none;
}
#sidebar-search input:focus { border-color: var(--accent); }
#fire-count { padding: 8px 16px; font-size: 0.8rem; color: var(--text-muted); border-bottom: 1px solid var(--border); }
#fire-list { flex: 1; overflow-y: auto; list-style: none; padding: 0; margin: 0; }
.fire-item {
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    cursor: pointer; transition: background 0.15s;
}
.fire-item:hover { background: var(--surface2); }
.fire-item .fire-name { font-weight: 600; font-size: 0.9rem; margin-bottom: 2px; }
.fire-item .fire-meta { font-size: 0.78rem; color: var(--text-muted); }
.fire-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.fire-status {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 0.7rem; font-weight: 600; color: #fff;
}
.fire-source {
    display: inline-block; padding: 1px 6px; background: var(--surface2);
    border-radius: 3px; font-size: 0.72rem; color: var(--text-muted);
}
.fire-area { font-size: 0.75rem; color: var(--accent); }
.fire-date { font-size: 0.72rem; color: var(--text-muted); }
#btn-refresh {
    margin: 12px 16px; padding: 10px;
    background: var(--accent); border: none; border-radius: var(--radius);
    color: #fff; font-weight: 600; font-size: 0.85rem; cursor: pointer;
    transition: background 0.15s;
}
#btn-refresh:hover { background: var(--accent-hover); }

/* ── Map area ── */
#fire-map-container { flex: 1; position: relative; min-height: 0; }
#fire-map { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }

/* ── Mobile sidebar toggle ── */
#btn-open-sidebar {
    display: none; position: absolute; top: 12px; left: 12px; z-index: 1500;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); color: var(--text);
    font-size: 1.3rem; width: 40px; height: 40px; cursor: pointer;
}
#btn-close-sidebar {
    background: none; border: none; color: var(--text);
    font-size: 1.5rem; cursor: pointer; display: none;
}

/* ── Meteogram modal ── */
#meteogram-modal {
    position: fixed; inset: 0; background: rgba(0,0,0,0.85);
    display: flex; align-items: center; justify-content: center;
    z-index: 2000;
}
#meteogram-modal.hidden { display: none; }
#meteogram-modal img { max-width: 95vw; max-height: 85vh; border-radius: var(--radius); }
#btn-close-modal {
    position: absolute; top: -12px; right: -12px;
    background: var(--accent); border: none; border-radius: 50%;
    color: #fff; font-size: 1.2rem; width: 32px; height: 32px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
}

/* ── Station markers ── */
.station-label {
    font-family: system-ui, sans-serif; font-size: 11px; text-align: center;
    background: var(--surface); color: var(--text); padding: 2px 4px;
    border-radius: 4px; border: 1px solid var(--border);
    white-space: nowrap; pointer-events: none;
}
.station-wind { color: var(--accent); font-weight: 600; }
.station-temp { color: var(--text-muted); }

/* ── Responsive ── */
@media (max-width: 768px) {
    #fire-sidebar {
        position: fixed; top: 0; left: -340px;
        height: 100vh; z-index: 1500; transition: left 0.25s ease;
    }
    #fire-sidebar.open { left: 0; }
    #btn-open-sidebar { display: flex; align-items: center; justify-content: center; }
    #btn-close-sidebar { display: block; }
}

/* ── Leaflet overrides ── */
.leaflet-popup-content-wrapper {
    background: var(--surface) !important; color: var(--text) !important;
    border-radius: var(--radius) !important; box-shadow: 0 4px 12px rgba(0,0,0,0.4) !important;
}
.leaflet-popup-tip { background: var(--surface) !important; }
.leaflet-popup-close-button { color: var(--text-muted) !important; }
.leaflet-control-layers { background: var(--surface) !important; color: var(--text) !important; }
.leaflet-control-layers label { color: var(--text) !important; }
"""


# ── JavaScript ───────────────────────────────────────────────────────────────

JS_CODE = """
(() => {
  let firesData = {features: []};
  let frpData = {features: []};
  let fireMarkers = {};
  let frpLayer = null;
  let stationLayer = null;
  let searchTimeout = null;
  let mapReady = false;

  function _initApp() {
    if (mapReady) return;
    mapReady = true;

    // ── Map init ──
    const EUMETSAT_WMS = 'https://view.eumetsat.int/geoserver/mtg_fd';
    const layers = {
      street: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap', maxZoom: 19 }),
      darkMatter: L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; CartoDB', maxZoom: 19 }),
      googleStreets: L.tileLayer('https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
        attribution: '&copy; Google', maxZoom: 20, subdomains: '0123' }),
      satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: '&copy; Esri', maxZoom: 18 }),
      googleSatellite: L.tileLayer('https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', {
        attribution: '&copy; Google', maxZoom: 20, subdomains: '0123' }),
      hybrid: L.layerGroup([
        L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
          attribution: '&copy; Esri', maxZoom: 18 }),
        L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', { maxZoom: 18 }),
      ]),
      googleHybrid: L.tileLayer('https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', {
        attribution: '&copy; Google', maxZoom: 20, subdomains: '0123' }),
      fireTemp: L.tileLayer.wms(EUMETSAT_WMS + '/rgb_firetemperature/ows', {
        layers: 'rgb_firetemperature', format: 'image/png', transparent: true,
        version: '1.3.0', crs: L.CRS.EPSG3857, attribution: '&copy; EUMETSAT' }),
      geoColour: L.tileLayer.wms(EUMETSAT_WMS + '/rgb_geocolour/ows', {
        layers: 'rgb_geocolour', format: 'image/png', transparent: true,
        version: '1.3.0', crs: L.CRS.EPSG3857, attribution: '&copy; EUMETSAT' }),
    };

    const map = L.map('fire-map', { zoomControl: true, layers: [layers.darkMatter] }).setView([40.0, -4.0], 6);

    // FRP layer
    frpLayer = L.geoJSON(null, {
      pointToLayer: (feature, latlng) => {
        const p = feature.properties;
        return L.circleMarker(latlng, {
          radius: p.radius || 6, fillColor: p.color || '#ffaa00',
          color: p.color || '#ffaa00', weight: 0, fillOpacity: 0.5,
        });
      },
      onEachFeature: (feature, layer) => {
        const p = feature.properties;
        const months = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
        let dateStr = p.acquisition_time;
        try {
          const dt = new Date(p.acquisition_time);
          dateStr = dt.getUTCDate() + ' ' + months[dt.getUTCMonth()] + ' ' + dt.getUTCFullYear() + ', ' +
            String(dt.getUTCHours()).padStart(2,'0') + ':' + String(dt.getUTCMinutes()).padStart(2,'0') + ' h UTC';
        } catch(e) {}
        layer.bindPopup(
          '<div style="font-family:sans-serif;min-width:150px">' +
          '<strong style="color:' + p.color + '">FRP Detection</strong>' +
          '<hr style="margin:4px 0;border-color:#3a3d5c">' +
          '<div style="font-size:0.85rem">' +
          '<b>' + dateStr + '</b><br>' +
          '<b>Confianza:</b> ' + p.confidence + '%<br>' +
          '<b>FRP:</b> ' + p.frp_mw + ' \\u00b1 ' + p.frp_uncertainty + ' MW' +
          '</div></div>'
        );
      },
    });

    // Station layer
    stationLayer = L.layerGroup();

    // Layer control
    const baseLayers = {
      'CartoDB Dark': layers.darkMatter,
      'OSM Callejero': layers.street,
      'Google Callejero': layers.googleStreets,
      'Esri Sat\\u00e9lite': layers.satellite,
      'Google Sat\\u00e9lite': layers.googleSatellite,
      'Esri H\\u00edbrido': layers.hybrid,
      'Google H\\u00edbrido': layers.googleHybrid,
      'Fire Temperature': layers.fireTemp,
      'GeoColour': layers.geoColour,
    };
    const overlayLayers = {
      'FRP (LSA SAF)': frpLayer,
      'Estaciones': stationLayer,
    };
    L.control.layers(baseLayers, overlayLayers, { position: 'topright' }).addTo(map);

    // ── Sun times ──
    async function getSunTimes(lat, lon) {
      try {
        const today = new Date().toISOString().split('T')[0];
        const res = await fetch('https://api.open-meteo.com/v1/forecast?latitude=' + lat + '&longitude=' + lon + '&daily=sunrise,sunset&timezone=auto&start_date=' + today + '&end_date=' + today);
        const data = await res.json();
        if (data.daily && data.daily.sunrise && data.daily.sunrise[0] && data.daily.sunset && data.daily.sunset[0]) {
          const fmt = function(iso) { var d = new Date(iso); return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0'); };
          return { sunrise: fmt(data.daily.sunrise[0]), sunset: fmt(data.daily.sunset[0]) };
        }
      } catch (e) {}
      return null;
    }

    // ── Render fires ──
    function renderFires(geojson) {
      Object.values(fireMarkers).forEach(function(m) { map.removeLayer(m); });
      fireMarkers = {};
      var list = document.getElementById('fire-list');
      var countEl = document.getElementById('fire-count');
      if (!list || !countEl) return;

      var features = (geojson.features || []).filter(function(f) { return f.geometry && f.geometry.coordinates; });
      countEl.textContent = features.length + ' incendio' + (features.length !== 1 ? 's' : '') + ' activo' + (features.length !== 1 ? 's' : '');

      var STATUS_COLORS = { active: '#e57373', declarado: '#e57373', controlled: '#ffb74d', stabilized: '#fff176', extinguished: '#bdbdbd', false_alarm: '#bdbdbd', unknown: '#ce93d8' };
      var STATUS_LABELS = { active: 'Activo', declarado: 'Declarado', controlled: 'Controlado', stabilized: 'Estabilizado', extinguished: 'Extinguido', false_alarm: 'Falsa alarma', unknown: 'Desconocido' };

      list.innerHTML = '';

      features.forEach(function(f) {
        var coords = f.geometry.coordinates;
        var lon = coords[0], lat = coords[1];
        var p = f.properties;
        var color = STATUS_COLORS[p.status] || STATUS_COLORS.unknown;
        var statusLbl = STATUS_LABELS[p.status] || p.status || 'Desconocido';

        var marker = L.circleMarker([lat, lon], {
          radius: 7, fillColor: color, color: '#fff', weight: 1.5, opacity: 0.9, fillOpacity: 0.8,
        }).addTo(map);

        var areaText = p.area_ha ? p.area_ha + ' ha' : 'N/D';
        var dateText = p.detection_date || '';
        var sourceUrl = p.source_url || '#';
        var coordText = Math.abs(lat).toFixed(4) + '\\u00b0 ' + (lat >= 0 ? 'N' : 'S') + ', ' + Math.abs(lon).toFixed(4) + '\\u00b0 ' + (lon >= 0 ? 'E' : 'W');

        var safeMunicipality = (p.municipality || '').replace(/'/g, "\\\\'");

        marker.bindPopup(
          '<div style="font-family:sans-serif;min-width:200px">' +
          '<strong>' + (p.municipality || p.external_id) + '</strong><br>' +
          '<small style="color:#9ca3af">' + (p.province ? p.province + ', ' : '') + (p.region || '') + '</small>' +
          '<div style="font-size:0.78rem;color:#9ca3af;margin:2px 0">' + coordText + '</div>' +
          '<hr style="margin:4px 0;border-color:#3a3d5c">' +
          '<div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">' +
          '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:' + color + '"></span>' +
          '<span>Estado: <b>' + statusLbl + '</b></span></div>' +
          '<span>\\u00c1rea: <b>' + areaText + '</b></span><br>' +
          (dateText ? '<span>Detecci\\u00f3n: ' + dateText + '</span><br>' : '') +
          '<span>Fuente: <a href="' + sourceUrl + '" target="_blank" style="color:#6b8cce">' + p.source_label + '</a></span>' +
          '<hr style="margin:4px 0;border-color:#3a3d5c">' +
          '<div style="display:flex;flex-direction:column;gap:4px">' +
          '<button onclick="window._openMeteogram(' + lat + ',' + lon + ',\\'' + safeMunicipality + '\\')" style="background:#6b8cce;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Meteograma</button>' +
          '<button onclick="window._searchStations(' + lat + ',' + lon + ')" style="background:#6b8cce;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Buscar estaciones</button>' +
          '</div>' +
          '<hr style="margin:4px 0;border-color:#3a3d5c">' +
          '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
          '<a href="https://www.windy.com/' + lat + '/' + lon + '" target="_blank" style="color:#6b8cce;font-size:0.78rem;text-decoration:none">Windy \\u2197</a>' +
          '<a href="https://www.meteoblue.com/en/weather/week/' + lat + ',' + lon + '" target="_blank" style="color:#6b8cce;font-size:0.78rem;text-decoration:none">Meteoblue \\u2197</a>' +
          '<a href="https://www.google.com/maps/@' + lat + ',' + lon + ',12z" target="_blank" style="color:#6b8cce;font-size:0.78rem;text-decoration:none">Google Maps \\u2197</a>' +
          '</div></div>'
        );

        fireMarkers[p.id] = marker;

        var li = document.createElement('div');
        li.className = 'fire-item';
        li.innerHTML =
          '<div class="fire-name">' + (p.municipality || p.external_id) + '</div>' +
          '<div class="fire-meta">' + (p.province ? p.province + ', ' : '') + (p.region || '') + '</div>' +
          '<div class="fire-tags">' +
          '<span class="fire-status" style="background:' + color + '">' + statusLbl + '</span>' +
          '<span class="fire-source">' + p.source_label + '</span>' +
          (p.area_ha ? '<span class="fire-area">' + p.area_ha + ' ha</span>' : '') +
          (dateText ? '<span class="fire-date">' + dateText.split('T')[0] + '</span>' : '') +
          '</div>';
        li.addEventListener('click', (function(lat, lon, marker) {
          return function() { map.setView([lat, lon], 12); marker.openPopup(); };
        })(lat, lon, marker));
        list.appendChild(li);
      });

      Object.values(fireMarkers).forEach(function(m) { m.bringToFront(); });
    }

    // ── Meteogram modal ──
    window._openMeteogram = function(lat, lon, name) {
      map.closePopup();
      var modal = document.getElementById('meteogram-modal');
      var img = document.getElementById('meteogram-img');
      if (modal && img) {
        img.src = '/api/meteogram?lat=' + lat + '&lon=' + lon + '&name=' + encodeURIComponent(name);
        modal.classList.remove('hidden');
      }
    };

    var closeModal = document.getElementById('btn-close-modal');
    var modal = document.getElementById('meteogram-modal');
    if (closeModal) closeModal.addEventListener('click', function() { modal.classList.add('hidden'); });
    if (modal) modal.addEventListener('click', function(e) { if (e.target === modal) modal.classList.add('hidden'); });

    // ── Map click → reverse geocode ──
    map.on('click', async function(e) {
      if (e.originalEvent.target.closest('.leaflet-marker-icon') || e.originalEvent.target.closest('.leaflet-interactive')) return;
      var lat = e.latlng.lat, lng = e.latlng.lng;
      var popup = L.popup().setLatLng([lat, lng])
        .setContent('<div style="font-family:sans-serif;text-align:center;padding:8px"><div style="font-size:0.8rem;color:#9ca3af">Buscando ubicaci\\u00f3n...</div></div>')
        .openOn(map);
      try {
        var res = await fetch('https://nominatim.openstreetmap.org/reverse?lat=' + lat + '&lon=' + lng + '&format=json&accept-language=es', { headers: { 'User-Agent': 'FireTracker/1.0' } });
        var data = await res.json();
        var addr = data.address || {};
        var name = data.name || addr.city || addr.town || addr.village || addr.hamlet || '';
        var locLine = [addr.city||addr.town||addr.village||addr.hamlet, addr.state||addr.county, addr.autonomous_community||addr.region, addr.country].filter(Boolean).join(', ');
        var sun = await getSunTimes(lat, lng);
        var safeName = (name || '').replace(/'/g, "\\\\'");
        popup.setContent(
          '<div style="font-family:sans-serif;min-width:200px">' +
          (name ? '<strong>' + name + '</strong><br>' : '') +
          (locLine ? '<small style="color:#9ca3af">' + locLine + '</small><br>' : '') +
          '<div style="font-size:0.78rem;color:#9ca3af;margin:2px 0">' + Math.abs(lat).toFixed(4) + '\\u00b0 ' + (lat >= 0 ? 'N' : 'S') + ', ' + Math.abs(lng).toFixed(4) + '\\u00b0 ' + (lng >= 0 ? 'E' : 'W') + '</div>' +
          (sun ? '<div style="font-size:0.78rem;color:#9ca3af;margin:2px 0">\\u2600 ' + sun.sunrise + ' \\u00b7 \\u263d ' + sun.sunset + '</div>' : '') +
          '<hr style="margin:4px 0;border-color:#3a3d5c">' +
          '<div style="display:flex;flex-direction:column;gap:4px">' +
          '<button onclick="window._openMeteogram(' + lat + ',' + lng + ',\\'' + safeName + '\\')" style="background:#6b8cce;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Meteograma</button>' +
          '<button onclick="window._searchStations(' + lat + ',' + lng + ')" style="background:#6b8cce;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Buscar estaciones</button>' +
          '</div>' +
          '<hr style="margin:4px 0;border-color:#3a3d5c">' +
          '<div style="display:flex;gap:6px;flex-wrap:wrap">' +
          '<a href="https://www.windy.com/' + lat + '/' + lng + '" target="_blank" style="color:#6b8cce;font-size:0.78rem;text-decoration:none">Windy \\u2197</a>' +
          '<a href="https://www.meteoblue.com/en/weather/week/' + lat + ',' + lng + '" target="_blank" style="color:#6b8cce;font-size:0.78rem;text-decoration:none">Meteoblue \\u2197</a>' +
          '<a href="https://www.google.com/maps/@' + lat + ',' + lng + ',12z" target="_blank" style="color:#6b8cce;font-size:0.78rem;text-decoration:none">Google Maps \\u2197</a>' +
          '</div></div>'
        );
      } catch (e) {
        popup.setContent('<div style="font-family:sans-serif;text-align:center;padding:8px;color:#9ca3af">Error en reverse geocode</div>');
      }
    });

    // ── Station search ──
    window._searchStations = async function(lat, lon) {
      map.closePopup();
      stationLayer.clearLayers();
      if (!map.hasLayer(stationLayer)) stationLayer.addTo(map);

      var popup = L.popup().setLatLng([lat, lon])
        .setContent('<div style="font-family:sans-serif;text-align:center;padding:8px;color:#9ca3af">Buscando estaciones...</div>')
        .openOn(map);

      try {
        var results = await Promise.allSettled([
          fetch('/api/stations?lat=' + lat + '&lon=' + lon + '&radius_km=30').then(function(r) { return r.json(); }),
          fetch('/api/metar').then(function(r) { return r.json(); }),
        ]);
        popup.close();
        var count = 0;

        function kmhToBeaufort(kmh) {
          if (kmh < 1) return 0; if (kmh <= 5) return 1; if (kmh <= 11) return 2;
          if (kmh <= 19) return 3; if (kmh <= 28) return 4; if (kmh <= 38) return 5;
          if (kmh <= 49) return 6; if (kmh <= 61) return 7; if (kmh <= 74) return 8;
          if (kmh <= 88) return 9; if (kmh <= 102) return 10; if (kmh <= 117) return 11;
          return 12;
        }
        function getWindArrow(deg) {
          var arrows = ['\\u2193','\\u2199','\\u2190','\\u2196','\\u2191','\\u2197','\\u2192','\\u2198'];
          return arrows[Math.round(deg / 45) % 8];
        }

        function addStation(s) {
          var slat = s.lat, slon = s.lon;
          var temp = s.temp_c, hum = s.humidity_pct, wspd = s.windspeed_kmh, wdir = s.winddir_avg;
          var arrow = wdir != null ? getWindArrow(wdir) : '';
          var beauf = wspd != null ? kmhToBeaufort(wspd) : null;
          var windText = wspd != null ? arrow + ' ' + beauf + ' (' + wspd + ')' : '';
          var tempRh = [temp != null ? temp + '\\u00b0' : '', hum != null ? hum + '%' : ''].filter(Boolean).join(' \\u00b7 ');
          var icon = L.divIcon({
            className: 'station-marker',
            html: '<div class="station-label"><div class="station-wind">' + windText + '</div><div class="station-temp">' + tempRh + '</div></div>',
            iconSize: [60, 36], iconAnchor: [30, 18],
          });
          var marker = L.marker([slat, slon], { icon: icon }).addTo(stationLayer);
          var isMetar = s.platform === 'METAR';
          var srcUrl = isMetar ? 'https://metar-taf.com/es/' + s.stationId : 'https://www.wunderground.com/dashboard/pws/' + s.stationId;
          var srcLbl = isMetar ? 'METAR-TAF \\u2197' : 'Weather Underground \\u2197';
          marker.bindPopup(
            '<div style="font-family:sans-serif;min-width:180px">' +
            '<strong>' + (s.name || s.stationId) + '</strong><br>' +
            '<small style="color:#9ca3af">' + (s.adm1 || '') + ' ' + (s.country_name || s.country || '') + ' \\u00b7 ' + s.stationId + '</small>' +
            '<hr style="margin:4px 0;border-color:#3a3d5c">' +
            '<div style="font-size:0.85rem">' +
            'Temp: <b>' + (temp != null ? temp + ' \\u00b0C' : 'N/D') + '</b><br>' +
            'HR: <b>' + (hum != null ? hum + ' %' : 'N/D') + '</b><br>' +
            'Viento: <b>' + (wspd != null ? beauf + ' (' + wspd + ') km/h' : 'N/D') + '</b><br>' +
            'Direcci\\u00f3n: <b>' + (wdir != null ? wdir + '\\u00b0' : 'N/D') + '</b><br>' +
            (s.windgust_kmh ? 'R\\u00e1fagas: ' + s.windgust_kmh + ' km/h<br>' : '') +
            (s.pressure_hpa ? 'Presi\\u00f3n: ' + s.pressure_hpa + ' hPa<br>' : '') +
            '</div>' +
            '<hr style="margin:4px 0;border-color:#3a3d5c">' +
            '<div style="font-size:0.78rem;color:#9ca3af">' +
            'Altitud: ' + (s.elev_m != null ? s.elev_m + ' m' : 'N/D') + (s.distance_km ? ' \\u00b7 ' + s.distance_km + ' km' : '') +
            '</div>' +
            '<hr style="margin:4px 0;border-color:#3a3d5c">' +
            '<a href="' + srcUrl + '" target="_blank" style="color:#6b8cce;font-size:0.8rem;text-decoration:none">' + srcLbl + '</a>' +
            '</div>'
          );
          count++;
        }

        if (results[0].status === 'fulfilled' && results[0].value && results[0].value.stations) results[0].value.stations.forEach(addStation);
        if (results[1].status === 'fulfilled' && results[1].value && results[1].value.stations) results[1].value.stations.forEach(addStation);
        if (count === 0) {
          L.popup().setLatLng([lat, lon])
            .setContent('<div style="font-family:sans-serif;text-align:center;padding:8px;color:#9ca3af">No se encontraron estaciones cercanas</div>')
            .openOn(map);
        }
      } catch (e) {
        console.error('Station search error:', e);
        popup.close();
      }
    };

    // ── Sidebar toggle (mobile) ──
    var btnOpen = document.getElementById('btn-open-sidebar');
    var btnClose = document.getElementById('btn-close-sidebar');
    var sidebar = document.getElementById('fire-sidebar');
    if (btnOpen) btnOpen.addEventListener('click', function() { sidebar.classList.add('open'); });
    if (btnClose) btnClose.addEventListener('click', function() { sidebar.classList.remove('open'); });

    // ── Geocode search ──
    var searchInput = document.getElementById('sidebar-search-input');
    var searchResults = document.getElementById('sidebar-search-results');
    if (searchInput) {
      searchInput.addEventListener('input', function() {
        clearTimeout(searchTimeout);
        var q = searchInput.value.trim();
        if (q.length < 2) { if (searchResults) searchResults.style.display = 'none'; return; }
        searchTimeout = setTimeout(async function() {
          try {
            var res = await fetch('/api/geocode?q=' + encodeURIComponent(q) + '&limit=5');
            var data = await res.json();
            if (!data.results || !data.results.length) { if (searchResults) searchResults.style.display = 'none'; return; }
            if (searchResults) {
              searchResults.innerHTML = '';
              data.results.forEach(function(loc) {
                var li = document.createElement('li');
                li.textContent = loc.display_name || loc.name;
                li.style.cssText = 'padding:8px 12px;cursor:pointer;font-size:0.85rem;border-bottom:1px solid #3a3d5c';
                li.addEventListener('click', function() {
                  map.setView([loc.latitude, loc.longitude], 11);
                  searchResults.style.display = 'none';
                  searchInput.value = loc.name;
                });
                searchResults.appendChild(li);
              });
              searchResults.style.display = 'block';
            }
          } catch (e) { console.error('Geocode error:', e); }
        }, 300);
      });
    }

    // ── Expose data update functions for Gradio callbacks ──
    window._updateFires = function(jsonStr) {
      try { firesData = JSON.parse(jsonStr); renderFires(firesData); } catch (e) { console.error('Parse error:', e); }
    };
    window._updateFRP = function(jsonStr) {
      try { frpData = JSON.parse(jsonStr); frpLayer.clearLayers(); if (frpData.features && frpData.features.length) frpLayer.addData(frpData); } catch (e) { console.error('FRP parse error:', e); }
    };

    window._doRefresh = async function() {
      var btn = document.getElementById('btn-refresh');
      if (btn) { btn.textContent = 'Actualizando...'; btn.disabled = true; }
      try {
        var res = await fetch('/api/refresh', { method: 'POST' });
        var data = await res.json();
        if (data.ok) {
          var results = await Promise.all([
            fetch('/api/fires/tracked').then(function(r) { return r.json(); }),
            fetch('/api/frp').then(function(r) { return r.json(); }),
          ]);
          window._updateFires(JSON.stringify(results[0]));
          window._updateFRP(JSON.stringify(results[1]));
        }
      } catch (e) { console.error('Refresh error:', e); }
      if (btn) { btn.textContent = 'Actualizar datos'; btn.disabled = false; }
    };
  }

  // Expose globally for inline bootstrap script
  window.initApp = _initApp;
})();
"""


# ── HTML templates ───────────────────────────────────────────────────────────

JS_ONLOAD = """
(function(element) {
  function ldCSS(url) {
    if (!document.querySelector('link[href="' + url + '"]')) {
      var l = document.createElement('link'); l.rel = 'stylesheet'; l.href = url; l.crossOrigin = '';
      document.head.appendChild(l);
    }
  }
  function ldJS(url) {
    return new Promise(function(ok, fail) {
      if (document.querySelector('script[src="' + url + '"]')) { ok(); return; }
      var s = document.createElement('script'); s.src = url; s.crossOrigin = '';
      s.onload = ok; s.onerror = fail; document.head.appendChild(s);
    });
  }
  ldCSS('https://unpkg.com/leaflet@1.9.4/dist/leaflet.css');
  ldJS('https://unpkg.com/leaflet@1.9.4/dist/leaflet.js').then(function() {
    var tries = 0;
    var iv = setInterval(function() {
      tries++;
      if (typeof window.initApp === 'function' && document.getElementById('fire-map')) {
        clearInterval(iv); window.initApp();
      }
      if (tries > 200) clearInterval(iv);
    }, 50);
  });
})(element);
"""

MAP_HTML = """
<div id="fire-app">
  <div id="fire-sidebar">
    <header>
      <h1>&#x1F525; Fire Tracker</h1>
      <button id="btn-close-sidebar" aria-label="Cerrar">&times;</button>
    </header>
    <div id="sidebar-search">
      <input type="text" id="sidebar-search-input" placeholder="Buscar ubicaci\u00f3n..." autocomplete="off" />
      <ul id="sidebar-search-results" style="display:none;position:absolute;left:16px;right:16px;background:var(--surface);border:1px solid var(--border);border-radius:8px;list-style:none;max-height:200px;overflow-y:auto;z-index:1000;padding:0;margin:0"></ul>
    </div>
    <div id="fire-count">Cargando...</div>
    <div id="fire-list"></div>
    <button id="btn-refresh" onclick="window._doRefresh()">Actualizar datos</button>
  </div>
  <div id="fire-map-container">
    <button id="btn-open-sidebar" aria-label="Menu">&#9776;</button>
    <div id="fire-map"></div>
  </div>
</div>
<div id="meteogram-modal" class="hidden">
  <button id="btn-close-modal" aria-label="Cerrar">&times;</button>
  <img id="meteogram-img" src="" alt="Meteograma" />
</div>
"""


# ── Gradio + FastAPI app ─────────────────────────────────────────────────────

def build_app() -> FastAPI:
    app = FastAPI(title='Fire Tracker')
    _mount_api_routes(app)

    theme = gr.themes.Base(
        primary_hue='slate',
        neutral_hue='slate',
        font=['system-ui', 'sans-serif'],
    ).set(
        body_background_fill='#1a1b2e',
        body_background_fill_dark='#1a1b2e',
        body_text_color='#d4d4d8',
        body_text_color_dark='#d4d4d8',
        block_background_fill='#252640',
        block_background_fill_dark='#252640',
        block_border_color='#3a3d5c',
        block_border_color_dark='#3a3d5c',
        block_label_text_color='#9ca3af',
        block_title_text_color='#d4d4d8',
        input_background_fill='#2e3050',
        input_background_fill_dark='#2e3050',
        input_border_color='#3a3d5c',
        button_primary_background_fill='#6b8cce',
        button_primary_background_fill_dark='#6b8cce',
        button_primary_text_color='#ffffff',
        slider_color='#6b8cce',
    )

    with gr.Blocks(title='Fire Tracker') as demo:
        fires_output = gr.Textbox(visible=False)
        frp_output = gr.Textbox(visible=False)

        gr.HTML(MAP_HTML, elem_id='fire-tracker-map', js_on_load=JS_ONLOAD)

        demo.load(fn=_get_fires_json, outputs=[fires_output]).then(
            fn=None, js="(json) => { if (window._updateFires) window._updateFires(json); }", inputs=[fires_output])
        demo.load(fn=_get_frp_json, outputs=[frp_output]).then(
            fn=None, js="(json) => { if (window._updateFRP) window._updateFRP(json); }", inputs=[frp_output])

    gr.mount_gradio_app(app, demo, path='/gradio', theme=theme, css=CSS, js=JS_CODE, head=HEAD_HTML)
    return app


# ── Entry point ──────────────────────────────────────────────────────────────

app = build_app()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=7860)
