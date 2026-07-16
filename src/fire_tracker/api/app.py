"""
Flask application for Fire Tracker.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from flask import Flask, jsonify, request, Response

_root = Path(__file__).resolve().parents[2]
if str(_root / 'src') not in sys.path:
    sys.path.insert(0, str(_root / 'src'))

from fire_tracker.database import FireDatabase
from fire_tracker.orchestrator import FireOrchestrator
from fire_tracker.weather import geocode, fetch_forecast
from fire_tracker.meteogram import generate_meteogram, meteogram_to_png

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

_DB_PATH = _root / 'data' / 'fires.db'
_db = FireDatabase(_DB_PATH)

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


@app.route('/api/fires/tracked')
def fires_tracked():
    country = request.args.get('country')
    fires = _db.get_active_fires(country=country)
    features = []
    for f in fires:
        if f.get('latitude') is None or f.get('longitude') is None:
            continue
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


# ── Weather & Meteogram ───────────────────────────────────────────────────


@app.route('/api/geocode')
def api_geocode():
    """
    Search for locations by name.

    Query params:
        q: search query (required)
        limit: max results (default 5)
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'Missing query parameter "q"'}), 400

    limit = request.args.get('limit', 5, type=int)
    locations = geocode(query, limit=limit)

    return jsonify({
        'results': [
            {
                'name': loc.name,
                'latitude': loc.latitude,
                'longitude': loc.longitude,
                'elevation': loc.elevation,
                'country': loc.country,
                'country_code': loc.country_code,
                'admin1': loc.admin1,
                'admin2': loc.admin2,
                'timezone': loc.timezone,
                'population': loc.population,
                'display_name': loc.display_name,
            }
            for loc in locations
        ]
    })


@app.route('/api/weather')
def api_weather():
    """
    Get weather forecast for a location.

    Query params:
        lat, lon: coordinates (required)
        forecast_days: days of forecast (default 3, max 16)
        past_days: past days to include (default 1)
    """
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    if lat is None or lon is None:
        return jsonify({'error': 'Missing "lat" and/or "lon" parameters'}), 400

    from fire_tracker.weather import Location
    location = Location(
        name=request.args.get('name', ''),
        latitude=lat,
        longitude=lon,
    )

    forecast_days = request.args.get('forecast_days', 3, type=int)
    past_days = request.args.get('past_days', 1, type=int)

    weather = fetch_forecast(
        location,
        forecast_days=forecast_days,
        past_days=past_days,
    )
    if weather is None:
        return jsonify({'error': 'Failed to fetch weather data'}), 502

    return jsonify({
        'location': {
            'name': weather.location.name,
            'latitude': weather.location.latitude,
            'longitude': weather.location.longitude,
            'elevation': weather.location.elevation,
        },
        'model': weather.model,
        'hourly': weather.hourly,
        'daily': weather.daily,
        'hourly_units': weather.hourly_units,
        'daily_units': weather.daily_units,
    })


@app.route('/api/meteogram.png')
def api_meteogram_png():
    """
    Generate meteogram image for a location.

    Query params:
        lat, lon: coordinates (required)
        name: location name (optional, for title)
        forecast_days: days of forecast (default 3)
        past_days: past days (default 1)
        width: figure width in inches (default 12)
        height: figure height in inches (default 10)
    """
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    if lat is None or lon is None:
        return jsonify({'error': 'Missing "lat" and/or "lon" parameters'}), 400

    from fire_tracker.weather import Location
    location = Location(
        name=request.args.get('name', ''),
        latitude=lat,
        longitude=lon,
    )

    forecast_days = request.args.get('forecast_days', 3, type=int)
    past_days = request.args.get('past_days', 1, type=int)
    width = request.args.get('width', 12, type=float)
    height = request.args.get('height', 10, type=float)

    weather = fetch_forecast(
        location,
        forecast_days=forecast_days,
        past_days=past_days,
    )
    if weather is None:
        return jsonify({'error': 'Failed to fetch weather data'}), 502

    try:
        png_data = meteogram_to_png(weather, figsize=(width, height))
    except Exception as e:
        logger.error('Meteogram generation error: %s', e)
        return jsonify({'error': f'Meteogram generation failed: {e}'}), 500

    return Response(png_data, mimetype='image/png')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
