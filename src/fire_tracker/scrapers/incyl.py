"""
servicios.jcyl.es — INCyL official API (Castilla y Leon).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pyproj import Transformer

from fire_tracker.scrapers.base import FireIncident, FireScraper

logger = logging.getLogger(__name__)

_API_URL = 'https://servicios.jcyl.es/incyl/json/emergencias'

_STATUS_MAP = {
    'Activo': 'active',
    'Controlado': 'controlled',
    'Estabilizado': 'stabilized',
    'Extinguido': 'extinguished',
    'Falsa Alarma': 'false_alarm',
}

_PROVINCE_MAP = {
    'AV': 'Avila',
    'BU': 'Burgos',
    'LE': 'Leon',
    'P': 'Palencia',
    'SA': 'Salamanca',
    'SG': 'Segovia',
    'SO': 'Soria',
    'VA': 'Valladolid',
    'ZA': 'Zamora',
}

_utm_transformer = Transformer.from_crs('EPSG:25830', 'EPSG:4326', always_xy=True)


class IncendiosCyLScraper(FireScraper):
    source = 'incendios_cyl'

    def fetch(self) -> list[FireIncident]:
        try:
            resp = self._get(_API_URL)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error('incendios_cyl fetch error: %s', e)
            return []

        emergencias = data.get('listaEmergencias', [])
        if not emergencias:
            logger.info('incendios_cyl: 0 emergencies')
            return []

        incidents = []
        seen: set[str] = set()

        for em in emergencias:
            try:
                incident = self._parse_incident(em)
                if incident is not None and incident.external_id not in seen:
                    seen.add(incident.external_id)
                    incidents.append(incident)
            except Exception as e:
                logger.debug('incendios_cyl parse error: %s', e)

        logger.info('incendios_cyl: %d fires', len(incidents))
        return incidents

    def _parse_incident(self, em: dict) -> FireIncident | None:
        estado = em.get('estado', {})
        estado_nombre = estado.get('NOMBRE', '')
        if estado_nombre in ('Extinguido', 'Falsa Alarma'):
            return None

        lat_utm = em.get('latitud')
        lon_utm = em.get('longitud')
        if lat_utm is None or lon_utm is None:
            return None

        try:
            x, y = _utm_transformer.transform(float(lon_utm), float(lat_utm))
            lat, lon = y, x
        except Exception:
            return None

        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None

        fecha_inicio = em.get('fecha_inicio', '')
        detection = None
        if fecha_inicio:
            try:
                detection = datetime.strptime(fecha_inicio, '%d/%m/%Y %H:%M:%S')
                detection = detection.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        fecha_ext = em.get('fecha_extinguido')
        extinction = None
        if fecha_ext:
            try:
                extinction = datetime.strptime(fecha_ext, '%d/%m/%Y %H:%M:%S')
                extinction = extinction.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        localidad = em.get('localidad', {})
        municipio = localidad.get('municipio', {})
        municipality = municipio.get('nombre') or localidad.get('nombre', '')
        if municipality:
            municipality = municipality.title()

        cpm = em.get('cpm', '')
        province = _PROVINCE_MAP.get(cpm, cpm)

        medios = em.get('medios', [])
        active_medios = [m for m in medios if m.get('ACTUANDO')]
        medio_count = len(active_medios)
        medio_types = {}
        for m in active_medios:
            tipo = m.get('TIPO', {}).get('NOMBRE', '?')
            medio_types[tipo] = medio_types.get(tipo, 0) + 1

        causa = em.get('causa', '')

        area_ha = em.get('superficie')
        if area_ha is not None:
            try:
                area_ha = float(area_ha)
            except (TypeError, ValueError):
                area_ha = None

        num1 = em.get('emergencia_num1', '')
        num2 = em.get('emergencia_num2', '')
        external_id = f'{num1}_{num2}' if num1 else f'cyl_{lat:.4f}_{lon:.4f}'

        return FireIncident(
            source=self.source,
            external_id=external_id,
            source_url='https://servicios.jcyl.es/incyl/incyl',
            latitude=lat,
            longitude=lon,
            municipality=municipality,
            province=province,
            region='Castilla y Leon',
            country='ES',
            status=_STATUS_MAP.get(estado_nombre, self._status_normalize(estado_nombre, self.source)),
            fire_type='forestal',
            detection_date=detection,
            extinction_date=extinction,
            area_ha=area_ha,
            resources={
                'medios_activos': medio_count,
                'medios_detalle': medio_types,
                'causa': causa,
            },
            raw_data=em,
        )
