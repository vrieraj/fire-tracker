"""
Copernicus EFFIS burnt area perimeters fetcher.

Downloads MODIS monthly burnt area perimeters from EFFIS WFS,
parses Shapefile, filters for Iberian Peninsula + France (ES/PT/FR),
and converts to GeoJSON for the map layer.

Source: https://forest-fire.emergency.copernicus.eu
Data: modis.ba.poly.month (MODIS Burnt Areas polygons, last 30 days)
"""

from __future__ import annotations

import io
import logging
import urllib.request
import zipfile

import shapefile

logger = logging.getLogger(__name__)

# WFS endpoint — monthly MODIS burnt area polygons
_URL = (
    'https://maps.effis.emergency.copernicus.eu/effis'
    '?service=WFS&request=getfeature'
    '&typename=ms:modis.ba.poly.month'
    '&version=1.1.0&outputformat=SHAPEZIP'
)

# Target countries
_TARGET_COUNTRIES = {'ES', 'PT', 'FR'}


def download_shapefile_zip() -> bytes | None:
    """Download the EFFIS monthly burnt area Shapefile ZIP."""
    try:
        req = urllib.request.Request(_URL, headers={
            'User-Agent': 'fire-tracker/1.0 (github.com/fire-tracker)',
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        logger.info('EFFIS: downloaded %.1f MB', len(data) / 1e6)
        return data
    except Exception as e:
        logger.error('EFFIS: download failed: %s', e)
        return None


def parse_shapefile(zip_bytes: bytes) -> list[dict]:
    """
    Parse Shapefile from ZIP bytes, filter for ES/PT/FR.

    Returns list of perimeter dicts with geometry as GeoJSON-compatible coords.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Find the .shp file
        shp_name = None
        for name in zf.namelist():
            if name.endswith('.shp'):
                shp_name = name
                break
        if not shp_name:
            logger.error('EFFIS: no .shp file found in ZIP')
            return []

        # Extract all files to a temp dict
        files = {}
        for name in zf.namelist():
            # Base name without extension
            base = name.rsplit('.', 1)[0]
            if base == shp_name.rsplit('.', 1)[0]:
                files[name] = zf.read(name)

    # Use pyshp to read from memory
    # pyshp expects file-like objects
    shp_key = shp_name
    dbf_key = shp_name.replace('.shp', '.dbf')
    shx_key = shp_name.replace('.shp', '.shx')

    shp_buf = io.BytesIO(files.get(shp_key, b''))
    dbf_buf = io.BytesIO(files.get(dbf_key, b''))
    shx_buf = io.BytesIO(files.get(shx_key, b''))

    reader = shapefile.Reader(shp=shp_buf, dbf=dbf_buf, shx=shx_buf, encoding='latin-1')

    # Field names from DBF
    fields = [f[0] for f in reader.fields[1:]]  # skip DeletionFlag

    perimeters = []
    for record in reader.iterShapeRecords():
        # Filter by country
        country = record.record[fields.index('COUNTRY')] if 'COUNTRY' in fields else ''
        if country not in _TARGET_COUNTRIES:
            continue

        # Extract metadata
        rec = record.record
        rec_dict = dict(zip(fields, rec))

        # Convert shape to GeoJSON geometry
        geom = record.shape.__geo_interface__
        if geom is None:
            continue

        # Simplify: extract only what we need
        perimeter = {
            'id': str(rec_dict.get('id', '')),
            'fire_date': str(rec_dict.get('FIREDATE', '')),
            'last_update': str(rec_dict.get('LASTUPDATE', '')),
            'country': country,
            'province': str(rec_dict.get('PROVINCE', '')),
            'commune': str(rec_dict.get('COMMUNE', '')),
            'area_ha': _safe_float(rec_dict.get('AREA_HA')),
            'geometry': geom,
        }
        perimeters.append(perimeter)

    logger.info('EFFIS: parsed %d perimeters for ES/PT/FR', len(perimeters))
    return perimeters


def _safe_float(val) -> float | None:
    """Safely convert to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def perimeters_to_geojson(perimeters: list[dict]) -> dict:
    """
    Convert list of perimeters to GeoJSON FeatureCollection.
    """
    features = []
    for p in perimeters:
        feature = {
            'type': 'Feature',
            'geometry': p['geometry'],
            'properties': {
                'id': p['id'],
                'fire_date': p['fire_date'],
                'last_update': p['last_update'],
                'country': p['country'],
                'province': p['province'],
                'commune': p['commune'],
                'area_ha': p['area_ha'],
            },
        }
        features.append(feature)

    return {
        'type': 'FeatureCollection',
        'features': features,
        'metadata': {
            'source': 'Copernicus EFFIS (MODIS Burnt Areas)',
            'source_url': 'https://forest-fire.emergency.copernicus.eu',
            'perimeter_count': len(features),
            'countries': list(_TARGET_COUNTRIES),
        },
    }


def fetch_perimeters() -> dict:
    """
    Download, parse, and return GeoJSON FeatureCollection of burnt area perimeters.
    """
    zip_bytes = download_shapefile_zip()
    if zip_bytes is None:
        return _empty_result()

    perimeters = parse_shapefile(zip_bytes)
    if not perimeters:
        logger.info('EFFIS: no perimeters found for ES/PT/FR')
        return _empty_result()

    return perimeters_to_geojson(perimeters)


def _empty_result() -> dict:
    return {
        'type': 'FeatureCollection',
        'features': [],
        'metadata': {
            'source': 'Copernicus EFFIS (MODIS Burnt Areas)',
            'source_url': 'https://forest-fire.emergency.copernicus.eu',
            'perimeter_count': 0,
            'countries': list(_TARGET_COUNTRIES),
        },
    }
