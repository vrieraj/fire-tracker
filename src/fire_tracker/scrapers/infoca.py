"""
INFOCA Andalucia — ArcGIS FeatureServer public API.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fire_tracker.scrapers.base import FireIncident, FireScraper

logger = logging.getLogger(__name__)

_INFOCA_URL = (
    'https://utility.arcgis.com/usrsvcs/servers/'
    'd6d1c0079ddd4c7f8876d58e13fcf1ac/rest/services/'
    'INFOCA/AN_INCIDENTES_PRO/FeatureServer/2/query'
)


class InfocaAndaluciaScraper(FireScraper):
    source = 'infoca'

    def fetch(self) -> list[FireIncident]:
        params = {
            'where': "ESTADO IN ('ACTIVO', 'DECLARADO', 'ESTABILIZADO', 'CONTROLADO')",
            'outFields': '*',
            'returnGeometry': 'true',
            'outSR': '4326',
            'f': 'geojson',
        }
        try:
            resp = self._get(_INFOCA_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error('INFOCA fetch error: %s', e)
            return []

        features = data.get('features', [])
        incidents = []
        for feat in features:
            props = feat.get('properties', {})
            geom = feat.get('geometry', {})

            coords = geom.get('coordinates', [None, None])
            try:
                lon, lat = float(coords[0]), float(coords[1])
            except (TypeError, IndexError, ValueError):
                lon, lat = None, None

            fecha_str = props.get('FECHA')
            detection = None
            if fecha_str:
                try:
                    ts_ms = int(fecha_str)
                    detection = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            raw_status = props.get('ESTADO', '')
            status = self._status_normalize(raw_status, self.source)

            resources = {
                'grupos_especialistas': props.get('GRUPOS_ESPECIALISTAS'),
                'bricas': props.get('BRICAS'),
                'vehiculos': props.get('VEHICULOS'),
                'tecnicos': props.get('TECNICOS'),
                'medios_aereos': props.get('MEDIOS_AEREOS'),
                'grupos_apoyo': props.get('GRUPOS_APOYO'),
            }

            raw_type = props.get('TIPO_INCIDENTE', '')
            fire_type = 'forestal' if 'INCENDIOS FORESTALES' in raw_type.upper() else None

            source_url = (
                'https://laagencia.maps.arcgis.com/apps/dashboards/'
                '87a5fe2d397e4140add84f50d8bdafd3'
            )

            incidents.append(FireIncident(
                source=self.source,
                external_id=str(props.get('ESRI_OID', props.get('OID_ENTERO', ''))),
                source_url=source_url,
                latitude=lat,
                longitude=lon,
                municipality=props.get('TERMINO_MUNICIPAL'),
                province=props.get('PROVINCIA'),
                region='Andalucia',
                country='ES',
                status=status,
                fire_type=fire_type,
                detection_date=detection,
                resources=resources if any(v is not None for v in resources.values()) else None,
                raw_data=props,
            ))

        logger.info('INFOCA: %d active incidents', len(incidents))
        return incidents
