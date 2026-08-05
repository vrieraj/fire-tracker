"""Tests for DGT eTraffic road incidents module + endpoint."""

import base64
import json
from unittest.mock import Mock, patch

import pytest

from fire_tracker.api.app import app
from fire_tracker.dgt_traffic import _decrypt, fetch_dgt_traffic


def _obfuscate(obj):
    raw = json.dumps(obj).encode('utf-8')
    return base64.b64encode(bytes(b ^ 102 for b in raw)).decode('ascii')


def _record(rid='1', subtipo='Carretera cortada', geom=True):
    rec = {
        'id': rid,
        'subtipoVialidad': subtipo,
        'causa': 'Otras incidencias',
        'subcausa': 'Incendio',
        'carretera': 'N-400',
        'sentido': 'both',
        'pkIni': 37.1,
        'pkFin': 37.3,
        'provinciaIni': 'Toledo',
        'municipioIni': 'Ontígola',
        'provinciaFin': 'Toledo',
        'municipioFin': 'Ontígola',
        'fechaInicio': '2026-08-05T12:00:00',
        'geometria': '{"type":"LineString","coordinates":[[-3.5,40.0],[-3.4,40.1]]}',
    }
    if not geom:
        rec['geometria'] = None
    return rec


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def test_decrypt_roundtrip():
    """Un payload base64+XOR(0x66) se descifra a JSON legible."""
    payload = _obfuscate({'a': 1, 'b': 'hi'})
    assert _decrypt(payload) == '{"a": 1, "b": "hi"}'


def test_fetch_geojson():
    """fetch_dgt_traffic devuelve FeatureCollection con las propiedades mapeadas."""
    payload = _obfuscate({'situationsRecords': [_record('1'), _record('2', 'Carril cortado')]})
    with patch('fire_tracker.dgt_traffic.requests.post') as mock_post:
        mock_post.return_value = Mock(status_code=200, text=payload)
        mock_post.return_value.raise_for_status = Mock()
        data = fetch_dgt_traffic()

    assert data['type'] == 'FeatureCollection'
    assert len(data['features']) == 2
    p = data['features'][0]['properties']
    assert p['subtipo'] == 'Carretera cortada'
    assert p['carretera'] == 'N-400'
    assert p['pk'] == '37.1–37.3 km'
    assert p['municipio'] == 'Ontígola'
    assert data['features'][0]['geometry']['type'] == 'LineString'
    assert data['metadata']['incident_count'] == 2


def test_skip_null_geometry():
    """Registros sin geometría se omiten."""
    payload = _obfuscate({'situationsRecords': [_record('1'), _record('2', geom=False)]})
    with patch('fire_tracker.dgt_traffic.requests.post') as mock_post:
        mock_post.return_value = Mock(status_code=200, text=payload)
        mock_post.return_value.raise_for_status = Mock()
        data = fetch_dgt_traffic()
    assert len(data['features']) == 1
    assert data['features'][0]['properties']['id'] == '1'


def test_fetch_error_returns_empty():
    """Un fallo de red devuelve FeatureCollection vacío con metadata.error."""
    with patch('fire_tracker.dgt_traffic.requests.post', side_effect=Exception('boom')):
        data = fetch_dgt_traffic()
    assert data['features'] == []
    assert 'error' in data['metadata']


def test_endpoint_ok(client):
    """GET /api/traffic-incidents retorna 200 con GeoJSON."""
    geo = {
        'type': 'FeatureCollection',
        'features': [{
            'type': 'Feature',
            'geometry': {'type': 'LineString', 'coordinates': [[-3.5, 40.0], [-3.4, 40.1]]},
            'properties': {'id': '1', 'subtipo': 'Carretera cortada', 'carretera': 'N-400'},
        }],
        'metadata': {'incident_count': 1},
    }
    with patch('fire_tracker.api.app.fetch_dgt_traffic', return_value=geo):
        resp = client.get('/api/traffic-incidents')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['metadata']['incident_count'] == 1
    assert data['features'][0]['geometry']['type'] == 'LineString'
