"""
Flask application for Fire Tracker.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from flask import Flask, jsonify, request

_root = Path(__file__).resolve().parents[2]
if str(_root / 'src') not in sys.path:
    sys.path.insert(0, str(_root / 'src'))

from fire_tracker.database import FireDatabase
from fire_tracker.orchestrator import FireOrchestrator

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
