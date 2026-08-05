"""
DGT eTraffic road incidents (fire campaign).

Fetches live road incidents related to wildfires from the Spanish DGT
eTraffic portal. The "Incendios" campaign filters incidents whose
subcausa is "Incendio" (road closures, lane cuts, warnings, etc.).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_API_URL = 'https://etraffic.dgt.es/etrafficWEB/api/cache/getFilteredData'
_SOURCE_URL = 'https://etraffic.dgt.es/etrafficWEB/?campanya=Incendios'
_XOR_KEY = 0x66
_TIMEOUT = 30
_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64)'
_REFERER = 'https://etraffic.dgt.es/etrafficWEB/'


def _decrypt(payload: str) -> str:
    raw = base64.b64decode(payload)
    return bytes(b ^ _XOR_KEY for b in raw).decode('utf-8')


def _extract_geometry(geom_str) -> dict | None:
    if not geom_str:
        return None
    try:
        geom = json.loads(geom_str)
    except (ValueError, TypeError):
        return None
    if not isinstance(geom, dict) or geom.get('type') not in ('LineString', 'MultiLineString'):
        return None
    return {'type': geom['type'], 'coordinates': geom['coordinates']}


def fetch_dgt_traffic(filtros_subcausas=('Incendio',)) -> dict:
    """Fetch fire-related road incidents as a GeoJSON FeatureCollection."""
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': _USER_AGENT,
        'Referer': _REFERER,
    }
    try:
        resp = requests.post(
            _API_URL,
            json={'filtrosSubcausas': list(filtros_subcausas)},
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = json.loads(_decrypt(resp.text))
    except Exception as e:
        logger.error('eTraffic fetch error: %s', e)
        return _empty_geojson(str(e))

    records = data.get('situationsRecords', [])
    features = []
    for r in records:
        geom = _extract_geometry(r.get('geometria'))
        if geom is None:
            continue

        pk_ini = r.get('pkIni')
        pk_fin = r.get('pkFin')
        if pk_ini is not None and pk_fin is not None:
            pk = f'{pk_ini:g}–{pk_fin:g} km'
        elif pk_ini is not None:
            pk = f'pk {pk_ini:g}'
        else:
            pk = ''

        features.append({
            'type': 'Feature',
            'geometry': geom,
            'properties': {
                'id': r.get('id'),
                'subtipo': r.get('subtipoVialidad') or 'Otra incidencia',
                'causa': r.get('causa') or '',
                'subcausa': r.get('subcausa') or '',
                'carretera': r.get('carretera') or '',
                'sentido': r.get('sentido') or '',
                'pk': pk,
                'municipio': r.get('municipioIni') or '',
                'provincia': r.get('provinciaIni') or '',
                'municipio_fin': r.get('municipioFin') or '',
                'provincia_fin': r.get('provinciaFin') or '',
                'fecha_inicio': r.get('fechaInicio') or '',
            },
        })

    return {
        'type': 'FeatureCollection',
        'features': features,
        'metadata': {
            'source': 'DGT eTraffic — Incendios',
            'source_url': _SOURCE_URL,
            'incident_count': len(features),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        },
    }


def _empty_geojson(reason: str) -> dict:
    return {
        'type': 'FeatureCollection',
        'features': [],
        'metadata': {
            'source': 'DGT eTraffic — Incendios',
            'source_url': _SOURCE_URL,
            'incident_count': 0,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'error': reason,
        },
    }
