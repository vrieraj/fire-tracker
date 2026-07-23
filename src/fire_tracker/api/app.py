"""
Flask application for Fire Tracker.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv

_here = Path(__file__).resolve().parent
_root = Path(__file__).resolve().parents[3]
load_dotenv(_root / '.env')
if str(_root / 'src') not in sys.path:
    sys.path.insert(0, str(_root / 'src'))

from fire_tracker.database import FireDatabase
from fire_tracker.orchestrator import FireOrchestrator
from fire_tracker.weather import geocode, Location
from fire_tracker.wx_stations import fetch_wu_stations_near, get_wu_api_key
from fire_tracker.frp import fetch_frp
from fire_tracker.metar import fetch_metar_stations

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

_DB_PATH = Path(os.environ.get('DB_PATH', str(_root / 'data' / 'fires.db')))
_db = FireDatabase(_DB_PATH)

SOURCE_MAIN_URLS = {
    'infoca': 'https://www.juntadeandalucia.es/institucion/junta-de-andalucia/area-de-agricultura-ganaderia-pesca-y-desarrollo-sostenible/consejeria-de-agricultura-ganaderia-pesca-y-desarrollo-sostenible/medio-forestal/incendios-forestales',
    'feuxdeforet.fr': 'https://feuxdeforet.fr/',
    'incendiscat.cat': 'https://incendiscat.cat/',
    'fogos.pt': 'https://www.sgifr.gov.pt/fogos-incendios-rurais-ativos',
    'incendios_cyl': 'https://servicios.jcyl.es/incyl/incyl',
    'fidias_clm': 'https://fidias.castillalamancha.es/',
}

SOURCE_SECONDARY_URLS = {
    'incendiscat.cat': {
        'url': 'https://interior.gencat.cat/ca/arees_dactuacio/bombers/actuacions-de-bombers',
        'label': 'Interior.gencat',
    },
    'fogos.pt': {
        'url': 'https://www.sgifr.gov.pt/fogos-incendios-rurais-ativos',
        'label': 'SGIFR',
    },
}

SOURCE_LABELS = {
    'infoca': 'INFOCA (Andalucia)',
    'feuxdeforet.fr': 'feuxdeforet.fr (Francia)',
    'incendiscat.cat': 'incendiscat.cat (Catalunya)',
    'fogos.pt': 'fogos.pt (Portugal)',
    'incendios_cyl': 'InCyL (Castilla y Leon)',
    'fidias_clm': 'FIDIAS (Castilla-La Mancha)',
}


@app.route('/')
def index():
    return send_from_directory(str(_here / 'static'), 'index.html')


@app.route('/api/fires/tracked')
def fires_tracked():
    import json as _json
    country = request.args.get('country')
    fires = _db.get_active_fires(country=country)
    features = []
    for f in fires:
        if f.get('latitude') is None or f.get('longitude') is None:
            continue

        raw_data = f.get('raw_data', {})
        if isinstance(raw_data, str):
            try:
                raw_data = _json.loads(raw_data)
            except (_json.JSONDecodeError, TypeError):
                raw_data = {}

        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [f['longitude'], f['latitude']],
            },
            'properties': {
                'id': f'{f["source"]}:{f["external_id"]}',
                'source': f['source'],
                'source_label': SOURCE_LABELS.get(f['source'], f['source']),
                'external_id': f['external_id'],
                'source_url': f.get('source_url') or SOURCE_MAIN_URLS.get(f['source']),
                'source_secondary': SOURCE_SECONDARY_URLS.get(f['source']),
                'chronology_url': raw_data.get('chronology_url', ''),
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
    return jsonify({
        'type': 'FeatureCollection',
        'features': features,
    })


@app.route('/api/fires/refresh', methods=['POST'])
def fires_refresh():
    orch = FireOrchestrator(_DB_PATH)
    stats = orch.run()
    return jsonify(stats)


@app.route('/api/fires/stats')
def fires_stats():
    return jsonify({
        'total': _db.count(),
        'sources': SOURCE_LABELS,
    })


@app.route('/api/fires/<fire_id>/chronology')
def fire_chronology(fire_id):
    """
    Redirect to X.com search for the fire's hashtag chronology.

    The fire_id format is "source:external_id" (e.g., "xmonitor:x_123456").
    """
    from flask import redirect
    import json

    parts = fire_id.split(':', 1)
    if len(parts) != 2:
        return jsonify({'error': 'Invalid fire_id format. Expected "source:external_id"'}), 400

    source, external_id = parts

    # Find the fire in the database
    fire = _db.get_fire(source, external_id)
    if not fire:
        return jsonify({'error': 'Fire not found'}), 404

    raw_data = fire.get('raw_data', {})
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            raw_data = {}

    chronology_url = raw_data.get('chronology_url', '')
    if not chronology_url:
        # Generate from municipality if available
        municipality = fire.get('municipality', '')
        if municipality:
            import urllib.parse
            hashtag = f"#IF{municipality.replace(' ', '')}"
            chronology_url = f"https://x.com/search?q={urllib.parse.quote(hashtag)}&src=typed_query&f=live"
        else:
            return jsonify({'error': 'No chronology URL available for this fire'}), 404

    return redirect(chronology_url)


# ── Weather ────────────────────────────────────────────────────────────────


@app.route('/api/geocode')
def api_geocode():
    """
    Search for locations by name.

    Query params:
        q: search query (required)
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Missing query parameter "q"'}), 400

    loc = geocode(query)

    if loc is None:
        return jsonify({'results': []})

    return jsonify({
        'results': [
            {
                'name': loc.name,
                'latitude': loc.latitude,
                'longitude': loc.longitude,
                'elevation': loc.elevation,
                'country': loc.country,
                'region': loc.region,
            }
        ]
    })


@app.route('/api/stations')
def api_stations():
    """
    Search for WU PWS weather stations near a point.

    Query params:
        lat, lon: coordinates (required)
        radius_km: search radius in km (default 50, max 100)
        api_key: WU API key (optional, falls back to WU_API_KEY env var)
    """
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    if lat is None or lon is None:
        return jsonify({'error': 'Missing "lat" and/or "lon" parameters'}), 400

    radius_km = request.args.get('radius_km', 50, type=float)
    radius_km = min(radius_km, 100)

    api_key = request.args.get('api_key') or get_wu_api_key()
    if not api_key:
        return jsonify({'error': 'WU API key required (param or WU_API_KEY env)'}), 400

    try:
        stations = fetch_wu_stations_near(lat, lon, radius_km, api_key)
    except Exception as e:
        logger.error('Station search error: %s', e)
        return jsonify({'error': f'Station search failed: {e}'}), 500

    return jsonify({
        'count': len(stations),
        'radius_km': radius_km,
        'center': {'lat': lat, 'lon': lon},
        'stations': stations,
    })


# ── METAR (aviation weather stations) ────────────────────────────────────────


@app.route('/api/metar')
def api_metar():
    """
    Get all METAR stations in ES/PT/FR with current observations.

    Returns GeoJSON-compatible station list from aviationweather.gov.
    No authentication required. Data updates hourly.
    """
    try:
        stations = fetch_metar_stations()
    except Exception as e:
        logger.error('METAR fetch error: %s', e)
        return jsonify({'error': f'METAR fetch failed: {e}'}), 500

    return jsonify({
        'count': len(stations),
        'source': 'aviationweather.gov (NOAA)',
        'stations': stations,
    })


# ── FRP (Fire Radiative Power) ──────────────────────────────────────────────


@app.route('/api/frp')
def api_frp():
    """
    Get FRP fire detections for Iberia + France (last 7 days).

    Purges detections older than 7 days before serving.
    Serves from DB (fast), fetches from LSA SAF if DB is empty.
    Returns GeoJSON FeatureCollection.
    """
    from fire_tracker.frp import _get_age_color, _BBOX, _WINDOW_HOURS
    from datetime import datetime, timezone, timedelta

    # Purge old detections first
    _db.purge_frp_detections(hours=_WINDOW_HOURS)

    now = datetime.now(timezone.utc)

    # Try DB first
    db_detections = _db.get_frp_detections(hours=_WINDOW_HOURS)

    if not db_detections:
        # DB empty — fetch from LSA SAF
        import threading
        result = [None]

        def _fetch():
            try:
                result[0] = fetch_frp()
            except Exception as e:
                result[0] = {'error': str(e)}

        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=30)

        if t.is_alive():
            return jsonify({'type': 'FeatureCollection', 'features': [], 'metadata': {'error': 'timeout'}}), 200

        data = result[0]
        if isinstance(data, dict) and data.get('error'):
            return jsonify({'type': 'FeatureCollection', 'features': [], 'metadata': {'error': data['error']}}), 200

        # Persist to DB
        db_rows = []
        for f in data.get('features', []):
            p = f['properties']
            db_rows.append({
                'longitude': f['geometry']['coordinates'][0],
                'latitude': f['geometry']['coordinates'][1],
                'frp_mw': p['frp_mw'],
                'confidence': p['confidence'],
                'frp_uncertainty': p['frp_uncertainty'],
                'pixel_size_km2': p['pixel_size_km2'],
                'acquisition_time': p['acquisition_time'],
                'bt_mir': p['bt_mir'],
                'bt_tir': p['bt_tir'],
            })
        if db_rows:
            _db.insert_frp_detections(db_rows)
            db_detections = _db.get_frp_detections(hours=_WINDOW_HOURS)

    # Build GeoJSON from DB rows
    features = []
    for d in db_detections:
        try:
            acq = datetime.fromisoformat(d['acquisition_time'])
        except (ValueError, TypeError):
            acq = now
        age_hours = (now - acq).total_seconds() / 3600.0
        color, size = _get_age_color(age_hours, d['frp_mw'])

        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [d['longitude'], d['latitude']],
            },
            'properties': {
                'frp_mw': round(d['frp_mw'], 1),
                'confidence': round(d['confidence'], 1) if d.get('confidence') else 0,
                'frp_uncertainty': round(d['frp_uncertainty'], 1) if d.get('frp_uncertainty') else 0,
                'pixel_size_km2': round(d['pixel_size_km2'], 2) if d.get('pixel_size_km2') else 0,
                'acquisition_time': d['acquisition_time'],
                'bt_tir': round(d['bt_tir'], 1) if d.get('bt_tir') else 0,
                'color': color,
                'radius': size,
                'age_hours': round(age_hours, 1),
            },
        })

    return jsonify({
        'type': 'FeatureCollection',
        'features': features,
        'metadata': {
            'source': 'LSA SAF FRP-PIXEL (MTG)',
            'source_url': 'https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/',
            'detection_count': len(features),
            'window_hours': _WINDOW_HOURS,
            'bbox': _BBOX,
        },
    })


# ── EFFIS Burnt Area Perimeters ────────────────────────────────────────────


@app.route('/api/perimeters')
def api_perimeters():
    """
    Get EFFIS burnt area perimeters for ES/PT/FR as GeoJSON.
    Serves from DB (populated by GitHub Actions daily).
    """
    from fire_tracker.effis_perimeters import perimeters_to_geojson

    perimeters = _db.get_perimeters()
    return jsonify(perimeters_to_geojson(perimeters))


# ── Cron endpoints (for cron-job.org) ─────────────────────────────────────


@app.route('/ping')
def ping():
    return jsonify({"status": "ok"})


@app.route('/api/cron/run', methods=['GET', 'POST'])
def cron_run():
    stats = {}
    try:
        orch = FireOrchestrator(_DB_PATH)
        stats["scrapers"] = orch.run()
    except Exception as e:
        stats["scrapers_error"] = str(e)
    try:
        from fire_tracker.monitor import run_monitor
        stats["monitor"] = run_monitor()
    except Exception as e:
        stats["monitor_error"] = str(e)
    return jsonify(stats)


@app.route('/api/cron/scrapers', methods=['GET', 'POST'])
def cron_scrapers():
    import threading
    result = [{'stats': None, 'done': False}]
    def _run():
        try:
            orch = FireOrchestrator(_DB_PATH)
            result[0]['stats'] = orch.run()
        except Exception as e:
            result[0]['stats'] = {'error': str(e)}
        result[0]['done'] = True
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=25)
    if result[0]['done']:
        return jsonify(result[0]['stats'])
    return jsonify({'status': 'running', 'message': 'scrapers started in background'})


@app.route('/api/cron/monitor', methods=['GET', 'POST'])
def cron_monitor():
    from fire_tracker.monitor import run_monitor
    stats = run_monitor()
    return jsonify(stats)


@app.route('/api/cron/stations', methods=['GET', 'POST'])
def cron_stations():
    return jsonify({"status": "ok", "message": "station cache cleanup (no-op)"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
