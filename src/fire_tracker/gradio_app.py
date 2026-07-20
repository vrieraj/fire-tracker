"""
Fire Tracker — Gradio UI replica.

Replica the original Flask+Leaflet frontend using Gradio Blocks,
with softer styling (gr.themes.Soft) and responsive sidebar.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from pathlib import Path

import gradio as gr
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, RedirectResponse, Response

_root = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get('DB_PATH', str(_root / 'data' / 'fires.db')))

from fire_tracker.database import FireDatabase
from fire_tracker.orchestrator import FireOrchestrator
from fire_tracker.weather import geocode as wx_geocode, Location
from fire_tracker.meteogram import meteogram_to_png
from fire_tracker.wx_stations import fetch_wu_stations_near, get_wu_api_key
from fire_tracker.frp import fetch_frp, _get_age_color, _BBOX, _WINDOW_HOURS
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

STATUS_COLORS = {
    'active': '#e57373', 'declarado': '#e57373',
    'controlled': '#ffb74d', 'stabilized': '#fff176',
    'extinguished': '#bdbdbd', 'false_alarm': '#bdbdbd', 'unknown': '#ce93d8',
}

STATUS_LABELS = {
    'active': 'Activo', 'declarado': 'Declarado',
    'controlled': 'Controlado', 'stabilized': 'Estabilizado',
    'extinguished': 'Extinguido', 'false_alarm': 'Falsa alarma', 'unknown': 'Desconocido',
}


def _get_fires_json() -> str:
    """Return active fires as GeoJSON string for JS consumption."""
    fires = _db.get_active_fires()
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
    return json.dumps({'type': 'FeatureCollection', 'features': features}, ensure_ascii=False)


def _get_frp_json() -> str:
    """Return FRP detections as GeoJSON string."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    detections = _db.get_frp_detections(hours=_WINDOW_HOURS)
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
    return json.dumps({'type': 'FeatureCollection', 'features': features}, ensure_ascii=False)


def _do_refresh() -> str:
    """Run orchestrator and return stats string."""
    orch = FireOrchestrator(DB_PATH)
    stats = orch.run()
    return (
        f"Raw: {stats['total_raw']} → Dedup: {stats['total_after_dedup']} → "
        f"Upserted: {stats['upserted']} (DB: {stats['total_in_db']}) en {stats['duration_s']:.1f}s"
    )


def _do_geocode(query: str) -> list[dict]:
    locations = wx_geocode(query, limit=5)
    return [{'name': loc.name, 'lat': loc.latitude, 'lon': loc.longitude,
             'display': loc.display_name} for loc in locations]


def _do_meteogram(lat: float, lon: float, name: str):
    from fire_tracker.weather import fetch_forecast
    loc = Location(name=name, latitude=lat, longitude=lon, elevation=0)
    weather = fetch_forecast(loc, forecast_days=2, past_days=0)
    if weather is None:
        return None
    return meteogram_to_png(weather, figsize=(14, 10))


# ── FastAPI routes (mounted on Gradio app) ──────────────────────────────────

def _mount_api_routes(app: FastAPI) -> None:

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

    @app.get('/api/fires/tracked')
    def api_fires_tracked():
        fires = _db.get_active_fires()
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
        return JSONResponse({'type': 'FeatureCollection', 'features': features})

    @app.get('/api/frp')
    def api_frp():
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        detections = _db.get_frp_detections(hours=_WINDOW_HOURS)
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
        return JSONResponse({'type': 'FeatureCollection', 'features': features})

    @app.post('/api/refresh')
    def api_refresh():
        try:
            stats = _do_refresh()
            return JSONResponse({'ok': True, 'stats': stats})
        except Exception as e:
            logger.error('Refresh error: %s', e)
            return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)


# ── CSS ──────────────────────────────────────────────────────────────────────

CSS = """
:root {
    --bg: #f8f9fa;
    --surface: #ffffff;
    --surface2: #e9ecef;
    --text: #212529;
    --text-muted: #6c757d;
    --accent: #4a90d9;
    --accent-hover: #3a7bc8;
    --border: #dee2e6;
    --radius: 8px;
}

/* Hide Gradio default elements */
.gradio-container { max-width: 100% !important; padding: 0 !important; }
footer, .gr-header, .gr-topbar { display: none !important; }

/* Main layout */
#fire-app { display: flex; height: calc(100vh - 4px); background: var(--bg); }

/* Sidebar */
#fire-sidebar {
    width: 340px; min-width: 340px;
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column;
    overflow: hidden;
    z-index: 1000;
}
#fire-sidebar header {
    padding: 16px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
}
#fire-sidebar header h1 { font-size: 1.2rem; font-weight: 700; color: var(--accent); margin: 0; }
#sidebar-search { padding: 12px 16px; border-bottom: 1px solid var(--border); }
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
}
#btn-refresh:hover { background: var(--accent-hover); }

/* Map area */
#fire-map-container { flex: 1; position: relative; }
#fire-map { width: 100%; height: 100%; }

/* Mobile sidebar toggle */
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

/* Meteogram modal */
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

/* Responsive */
@media (max-width: 768px) {
    #fire-sidebar {
        position: fixed; top: 0; left: -340px;
        height: 100vh; z-index: 1500; transition: left 0.25s ease;
    }
    #fire-sidebar.open { left: 0; }
    #btn-open-sidebar { display: flex; align-items: center; justify-content: center; }
    #btn-close-sidebar { display: block; }
}

/* Leaflet overrides */
.leaflet-popup-content-wrapper {
    background: var(--surface) !important;
    color: var(--text) !important;
    border-radius: var(--radius) !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15) !important;
}
.leaflet-popup-tip { background: var(--surface) !important; }
"""

# ── JavaScript ───────────────────────────────────────────────────────────────

JS_CODE = """
(() => {
  // State
  let firesData = {features: []};
  let frpData = {features: []};
  let fireMarkers = {};
  let frpLayer = null;
  let stationLayer = null;
  let searchTimeout = null;

  // Wait for Leaflet
  function waitForLeaflet(cb) {
    if (window.L) return cb();
    setTimeout(() => waitForLeaflet(cb), 100);
  }

  waitForLeaflet(() => {
    // ── Map init ──
    const EUMETSAT_WMS = 'https://view.eumetsat.int/geoserver/mtg_fd';
    const layers = {
      street: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap', maxZoom: 19 }),
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

    const map = L.map('fire-map', { zoomControl: true, layers: [layers.hybrid] }).setView([40.0, -4.0], 6);

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
          dateStr = `${dt.getUTCDate()} ${months[dt.getUTCMonth()]} ${dt.getUTCFullYear()}, ${String(dt.getUTCHours()).padStart(2,'0')}:${String(dt.getUTCMinutes()).padStart(2,'0')} h UTC`;
        } catch(e) {}
        layer.bindPopup(`
          <div style="font-family:sans-serif;min-width:150px">
            <strong style="color:${p.color}">FRP Detection</strong>
            <hr style="margin:4px 0;border-color:#ddd">
            <div style="font-size:0.85rem">
              <b>${dateStr}</b><br>
              <b>Confianza:</b> ${p.confidence}%<br>
              <b>FRP:</b> ${p.frp_mw} ± ${p.frp_uncertainty} MW
            </div>
          </div>
        `);
      },
    });

    // Station layer
    stationLayer = L.layerGroup();

    // Layer control
    const baseLayers = {
      'OSM Callejero': layers.street,
      'Google Callejero': layers.googleStreets,
      'Esri Satélite': layers.satellite,
      'Google Satélite': layers.googleSatellite,
      'Esri Híbrido': layers.hybrid,
      'Google Híbrido': layers.googleHybrid,
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
        const res = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&daily=sunrise,sunset&timezone=auto&start_date=${today}&end_date=${today}`);
        const data = await res.json();
        if (data.daily?.sunrise?.[0] && data.daily?.sunset?.[0]) {
          const fmt = (iso) => { const d = new Date(iso); return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; };
          return { sunrise: fmt(data.daily.sunrise[0]), sunset: fmt(data.daily.sunset[0]) };
        }
      } catch (e) {}
      return null;
    }

    // ── Render fires on map ──
    function renderFires(geojson) {
      Object.values(fireMarkers).forEach(m => map.removeLayer(m));
      fireMarkers = {};
      const list = document.getElementById('fire-list');
      const countEl = document.getElementById('fire-count');
      if (!list || !countEl) return;

      const features = (geojson.features || []).filter(f => f.geometry?.coordinates);
      countEl.textContent = features.length + ' incendio' + (features.length !== 1 ? 's' : '') + ' activo' + (features.length !== 1 ? 's' : '');

      const STATUS_COLORS = { active: '#e57373', declarado: '#e57373', controlled: '#ffb74d', stabilized: '#fff176', extinguished: '#bdbdbd', false_alarm: '#bdbdbd', unknown: '#ce93d8' };
      const STATUS_LABELS = { active: 'Activo', declarado: 'Declarado', controlled: 'Controlado', stabilized: 'Estabilizado', extinguished: 'Extinguido', false_alarm: 'Falsa alarma', unknown: 'Desconocido' };

      list.innerHTML = '';

      features.forEach(f => {
        const [lon, lat] = f.geometry.coordinates;
        const p = f.properties;
        const color = STATUS_COLORS[p.status] || STATUS_COLORS.unknown;
        const statusLbl = STATUS_LABELS[p.status] || p.status || 'Desconocido';

        // Map marker
        const marker = L.circleMarker([lat, lon], {
          radius: 7, fillColor: color, color: '#fff', weight: 1.5, opacity: 0.9, fillOpacity: 0.8,
        }).addTo(map);

        const areaText = p.area_ha ? p.area_ha + ' ha' : 'N/D';
        const dateText = p.detection_date || '';
        const sourceUrl = p.source_url || '#';
        const coordText = `${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(4)}° ${lon >= 0 ? 'E' : 'W'}`;

        marker.bindPopup(`
          <div style="font-family:sans-serif;min-width:200px">
            <strong>${p.municipality || p.external_id}</strong><br>
            <small>${p.province ? p.province + ', ' : ''}${p.region || ''}</small>
            <div style="font-size:0.78rem;color:#666;margin:2px 0">${coordText}</div>
            <hr style="margin:4px 0;border-color:#ddd">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
              <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${color}"></span>
              <span>Estado: <b>${statusLbl}</b></span>
            </div>
            <span>Area: <b>${areaText}</b></span><br>
            ${dateText ? '<span>Deteccion: ' + dateText + '</span><br>' : ''}
            <span>Fuente: <a href="' + sourceUrl + '" target="_blank" style="color:#4a90d9">' + p.source_label + '</a></span>
            <hr style="margin:4px 0;border-color:#ddd">
            <div style="display:flex;flex-direction:column;gap:4px">
              <button onclick="window._openMeteogram(${lat},${lon},'${(p.municipality||'').replace(/'/g, "\\'")}')" style="background:#4a90d9;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Meteograma</button>
              <button onclick="window._searchStations(${lat},${lon})" style="background:#4a90d9;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Buscar estaciones</button>
            </div>
            <hr style="margin:4px 0;border-color:#ddd">
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              <a href="https://www.windy.com/${lat}/${lon}" target="_blank" style="color:#4a90d9;font-size:0.78rem;text-decoration:none">Windy ↗</a>
              <a href="https://www.meteoblue.com/en/weather/week/${lat},${lon}" target="_blank" style="color:#4a90d9;font-size:0.78rem;text-decoration:none">Meteoblue ↗</a>
              <a href="https://www.google.com/maps/@${lat},${lon},12z" target="_blank" style="color:#4a90d9;font-size:0.78rem;text-decoration:none">Google Maps ↗</a>
            </div>
          </div>
        `);

        fireMarkers[p.id] = marker;

        // Sidebar list item
        const li = document.createElement('div');
        li.className = 'fire-item';
        li.innerHTML = `
          <div class="fire-name">${p.municipality || p.external_id}</div>
          <div class="fire-meta">${p.province ? p.province + ', ' : ''}${p.region || ''}</div>
          <div class="fire-tags">
            <span class="fire-status" style="background:${color}">${statusLbl}</span>
            <span class="fire-source">${p.source_label}</span>
            ${p.area_ha ? '<span class="fire-area">' + p.area_ha + ' ha</span>' : ''}
            ${dateText ? '<span class="fire-date">' + dateText.split('T')[0] + '</span>' : ''}
          </div>
        `;
        li.addEventListener('click', () => {
          map.setView([lat, lon], 12);
          marker.openPopup();
        });
        list.appendChild(li);
      });

      Object.values(fireMarkers).forEach(m => m.bringToFront());
    }

    // ── Meteogram modal ──
    window._openMeteogram = (lat, lon, name) => {
      map.closePopup();
      const modal = document.getElementById('meteogram-modal');
      const img = document.getElementById('meteogram-img');
      if (modal && img) {
        img.src = `/api/meteogram?lat=${lat}&lon=${lon}&name=${encodeURIComponent(name)}`;
        modal.classList.remove('hidden');
      }
    };

    const closeModal = document.getElementById('btn-close-modal');
    const modal = document.getElementById('meteogram-modal');
    if (closeModal) closeModal.addEventListener('click', () => modal.classList.add('hidden'));
    if (modal) modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.add('hidden'); });

    // ── Map click → reverse geocode ──
    map.on('click', async (e) => {
      if (e.originalEvent.target.closest('.leaflet-marker-icon') || e.originalEvent.target.closest('.leaflet-interactive')) return;
      const { lat, lng } = e.latlng;
      const popup = L.popup().setLatLng([lat, lng])
        .setContent('<div style="font-family:sans-serif;text-align:center;padding:8px"><div style="font-size:0.8rem;color:#666">Buscando ubicacion...</div></div>')
        .openOn(map);
      try {
        const res = await fetch(`https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json&accept-language=es`, { headers: { 'User-Agent': 'FireTracker/1.0' } });
        const data = await res.json();
        const addr = data.address || {};
        const name = data.name || addr.city || addr.town || addr.village || addr.hamlet || '';
        const locLine = [addr.city||addr.town||addr.village||addr.hamlet, addr.state||addr.county, addr.autonomous_community||addr.region, addr.country].filter(Boolean).join(', ');
        const sun = await getSunTimes(lat, lng);
        popup.setContent(`
          <div style="font-family:sans-serif;min-width:200px">
            ${name ? '<strong>' + name + '</strong><br>' : ''}
            ${locLine ? '<small style="color:#888">' + locLine + '</small><br>' : ''}
            <div style="font-size:0.78rem;color:#666;margin:2px 0">${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lng).toFixed(4)}° ${lng >= 0 ? 'E' : 'W'}</div>
            ${sun ? '<div style="font-size:0.78rem;color:#888;margin:2px 0">☀ ' + sun.sunrise + ' · ☽ ' + sun.sunset + '</div>' : ''}
            <hr style="margin:4px 0;border-color:#ddd">
            <div style="display:flex;flex-direction:column;gap:4px">
              <button onclick="window._openMeteogram(${lat},${lng},'${(name||'').replace(/'/g, "\\'")}')" style="background:#4a90d9;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Meteograma</button>
              <button onclick="window._searchStations(${lat},${lng})" style="background:#4a90d9;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Buscar estaciones</button>
            </div>
            <hr style="margin:4px 0;border-color:#ddd">
            <div style="display:flex;gap:6px;flex-wrap:wrap">
              <a href="https://www.windy.com/${lat}/${lng}" target="_blank" style="color:#4a90d9;font-size:0.78rem;text-decoration:none">Windy ↗</a>
              <a href="https://www.meteoblue.com/en/weather/week/${lat},${lng}" target="_blank" style="color:#4a90d9;font-size:0.78rem;text-decoration:none">Meteoblue ↗</a>
              <a href="https://www.google.com/maps/@${lat},${lng},12z" target="_blank" style="color:#4a90d9;font-size:0.78rem;text-decoration:none">Google Maps ↗</a>
            </div>
          </div>
        `);
      } catch (e) {
        popup.setContent('<div style="font-family:sans-serif;text-align:center;padding:8px">Error en reverse geocode</div>');
      }
    });

    // ── Station search ──
    window._searchStations = async (lat, lon) => {
      map.closePopup();
      stationLayer.clearLayers();
      if (!map.hasLayer(stationLayer)) stationLayer.addTo(map);

      const popup = L.popup().setLatLng([lat, lon])
        .setContent('<div style="font-family:sans-serif;text-align:center;padding:8px">Buscando estaciones...</div>')
        .openOn(map);

      try {
        const [wuRes, metarRes] = await Promise.allSettled([
          fetch('/api/stations?lat=' + lat + '&lon=' + lon + '&radius_km=30').then(r => r.json()),
          fetch('/api/metar').then(r => r.json()),
        ]);
        popup.close();
        let count = 0;

        function kmhToBeaufort(kmh) {
          if (kmh < 1) return 0; if (kmh <= 5) return 1; if (kmh <= 11) return 2;
          if (kmh <= 19) return 3; if (kmh <= 28) return 4; if (kmh <= 38) return 5;
          if (kmh <= 49) return 6; if (kmh <= 61) return 7; if (kmh <= 74) return 8;
          if (kmh <= 88) return 9; if (kmh <= 102) return 10; if (kmh <= 117) return 11;
          return 12;
        }
        function getWindArrow(deg) {
          const arrows = ['↓','↙','←','↖','↑','↗','→','↘'];
          return arrows[Math.round(deg / 45) % 8];
        }

        function addStation(s) {
          const { lat: slat, lon: slon } = s;
          const temp = s.temp_c, hum = s.humidity_pct, wspd = s.windspeed_kmh, wdir = s.winddir_avg;
          const arrow = wdir != null ? getWindArrow(wdir) : '';
          const beauf = wspd != null ? kmhToBeaufort(wspd) : null;
          const windText = wspd != null ? arrow + ' ' + beauf + ' (' + wspd + ')' : '';
          const tempRh = [temp != null ? temp + '°' : '', hum != null ? hum + '%' : ''].filter(Boolean).join(' · ');
          const icon = L.divIcon({
            className: 'station-marker',
            html: '<div class="station-label"><div class="station-wind">' + windText + '</div><div class="station-temp">' + tempRh + '</div></div>',
            iconSize: [60, 36], iconAnchor: [30, 18],
          });
          const marker = L.marker([slat, slon], { icon }).addTo(stationLayer);
          const isMetar = s.platform === 'METAR';
          const srcUrl = isMetar ? 'https://metar-taf.com/es/' + s.stationId : 'https://www.wunderground.com/dashboard/pws/' + s.stationId;
          const srcLbl = isMetar ? 'METAR-TAF ↗' : 'Weather Underground ↗';
          marker.bindPopup(`
            <div style="font-family:sans-serif;min-width:180px">
              <strong>${s.name || s.stationId}</strong><br>
              <small style="color:#888">${s.adm1 || ''} ${s.country_name || s.country || ''} · ${s.stationId}</small>
              <hr style="margin:4px 0;border-color:#ddd">
              <div style="font-size:0.85rem">
                Temp: <b>${temp != null ? temp + ' °C' : 'N/D'}</b><br>
                HR: <b>${hum != null ? hum + ' %' : 'N/D'}</b><br>
                Viento: <b>${wspd != null ? beauf + ' (' + wspd + ') km/h' : 'N/D'}</b><br>
                Direccion: <b>${wdir != null ? wdir + '°' : 'N/D'}</b><br>
                ${s.windgust_kmh ? 'Rafagas: ' + s.windgust_kmh + ' km/h<br>' : ''}
                ${s.pressure_hpa ? 'Presion: ' + s.pressure_hpa + ' hPa<br>' : ''}
              </div>
              <hr style="margin:4px 0;border-color:#ddd">
              <div style="font-size:0.78rem;color:#888">
                Altitud: ${s.elev_m != null ? s.elev_m + ' m' : 'N/D'}${s.distance_km ? ' · ' + s.distance_km + ' km' : ''}
              </div>
              <hr style="margin:4px 0;border-color:#ddd">
              <a href="${srcUrl}" target="_blank" style="color:#4a90d9;font-size:0.8rem;text-decoration:none">${srcLbl}</a>
            </div>
          `);
          count++;
        }

        if (wuRes.status === 'fulfilled' && wuRes.value?.stations) wuRes.value.stations.forEach(addStation);
        if (metarRes.status === 'fulfilled' && metarRes.value?.stations) metarRes.value.stations.forEach(addStation);
        if (count === 0) {
          L.popup().setLatLng([lat, lon])
            .setContent('<div style="font-family:sans-serif;text-align:center;padding:8px">No se encontraron estaciones cercanas</div>')
            .openOn(map);
        }
      } catch (e) {
        console.error('Station search error:', e);
        popup.close();
      }
    };

    // ── Sidebar toggle (mobile) ──
    const btnOpen = document.getElementById('btn-open-sidebar');
    const btnClose = document.getElementById('btn-close-sidebar');
    const sidebar = document.getElementById('fire-sidebar');
    if (btnOpen) btnOpen.addEventListener('click', () => sidebar.classList.add('open'));
    if (btnClose) btnClose.addEventListener('click', () => sidebar.classList.remove('open'));

    // ── Geocode search ──
    const searchInput = document.getElementById('sidebar-search-input');
    const searchResults = document.getElementById('sidebar-search-results');
    if (searchInput) {
      searchInput.addEventListener('input', () => {
        clearTimeout(searchTimeout);
        const q = searchInput.value.trim();
        if (q.length < 2) { if (searchResults) searchResults.style.display = 'none'; return; }
        searchTimeout = setTimeout(async () => {
          try {
            const res = await fetch('/api/geocode?q=' + encodeURIComponent(q) + '&limit=5');
            const data = await res.json();
            if (!data.results?.length) { if (searchResults) searchResults.style.display = 'none'; return; }
            if (searchResults) {
              searchResults.innerHTML = '';
              data.results.forEach(loc => {
                const li = document.createElement('li');
                li.textContent = loc.display_name || loc.name;
                li.style.cssText = 'padding:8px 12px;cursor:pointer;font-size:0.85rem;border-bottom:1px solid #dee2e6';
                li.addEventListener('click', () => {
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

    // ── Expose data update functions ──
    window._updateFires = (jsonStr) => {
      try { firesData = JSON.parse(jsonStr); renderFires(firesData); } catch (e) { console.error('Parse error:', e); }
    };
    window._updateFRP = (jsonStr) => {
      try { frpData = JSON.parse(jsonStr); frpLayer.clearLayers(); if (frpData.features?.length) frpLayer.addData(frpData); } catch (e) { console.error('FRP parse error:', e); }
    };

    window._doRefresh = async () => {
      const btn = document.getElementById('btn-refresh');
      if (btn) { btn.textContent = 'Actualizando...'; btn.disabled = true; }
      try {
        const res = await fetch('/api/refresh', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          const [firesRes, frpRes] = await Promise.all([
            fetch('/api/fires/tracked').then(r => r.json()),
            fetch('/api/frp').then(r => r.json()),
          ]);
          window._updateFires(JSON.stringify(firesRes));
          window._updateFRP(JSON.stringify(frpRes));
        }
      } catch (e) { console.error('Refresh error:', e); }
      if (btn) { btn.textContent = 'Actualizar datos'; btn.disabled = false; }
    };

    // ── Initial load via Gradio ──
    // Triggered by Gradio's load event calling Python functions
    window._triggerLoad = () => {
      // Dispatch custom event that Gradio can listen to
      document.dispatchEvent(new CustomEvent('fire-tracker-load'));
    };
  });
})();
"""


# ── HTML templates ───────────────────────────────────────────────────────────

MAP_HTML = f"""
<div id="fire-app">
  <div id="fire-sidebar">
    <header>
      <h1>🔥 Fire Tracker</h1>
      <button id="btn-close-sidebar" aria-label="Cerrar">&times;</button>
    </header>
    <div id="sidebar-search">
      <input type="text" id="sidebar-search-input" placeholder="Buscar ubicacion..." autocomplete="off" />
      <ul id="sidebar-search-results" style="display:none;position:absolute;left:16px;right:16px;background:#fff;border:1px solid #dee2e6;border-radius:8px;list-style:none;max-height:200px;overflow-y:auto;z-index:1000;padding:0;margin:0"></ul>
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


# ── Gradio app ───────────────────────────────────────────────────────────────

def build_app() -> FastAPI:
    app = FastAPI(title='Fire Tracker')
    _mount_api_routes(app)

    theme = gr.themes.Soft(
        primary_hue='blue',
        neutral_hue='gray',
        font=['system-ui', 'sans-serif'],
    )

    with gr.Blocks(title='Fire Tracker') as demo:
        fires_json = gr.State('')
        frp_json = gr.State('')
        gr.HTML(MAP_HTML)
        fires_output = gr.Textbox(visible=False)
        frp_output = gr.Textbox(visible=False)

        demo.load(fn=_get_fires_json, outputs=[fires_output]).then(
            fn=None, js="""(json) => { window._updateFires(json); }""", inputs=[fires_output])
        demo.load(fn=_get_frp_json, outputs=[frp_output]).then(
            fn=None, js="""(json) => { window._updateFRP(json); }""", inputs=[frp_output])

    gr.mount_gradio_app(app, demo, path='/',
                        theme=theme, css=CSS, js=JS_CODE)
    return app


if __name__ == '__main__':
    import uvicorn
    app = build_app()
    uvicorn.run(app, host='0.0.0.0', port=7860)
