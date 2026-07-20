"""
LSA SAF FRP-PIXEL data fetcher.

Downloads Fire Radiative Power data from IPMA/LSA SAF and filters
for the Iberian Peninsula + France region. Keeps last 24h of data
with time-based color gradient.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_root = Path(__file__).resolve().parents[2]
load_dotenv(_root / '.env')

logger = logging.getLogger(__name__)

_BASE_URL = 'https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/NATIVE'

# Bounding box: Iberian Peninsula + France
_BBOX = {
    'lon_min': -12.0,
    'lon_max': 10.0,
    'lat_min': 34.0,
    'lat_max': 51.0,
}

# Minimum confidence to include a detection (0-1)
_MIN_CONFIDENCE = 0.5

# Keep 24h of data
_WINDOW_HOURS = 24

# Colors: recent = yellow→red, older = red→pink
_RECENT_COLORS = ['#ffdd00', '#ffaa00', '#ff6600', '#ff0000']  # high FRP → low FRP (recent)
_OLDER_COLORS = ['#ff0000', '#dd4488', '#cc66aa', '#dd88bb']   # high FRP → low FRP (older)


@dataclass
class FRPDetection:
    """A single fire pixel detection from FRP-PIXEL product."""
    longitude: float
    latitude: float
    frp_mw: float
    confidence: float
    frp_uncertainty: float
    pixel_size_km2: float
    acquisition_time: datetime
    bt_mir: float
    bt_tir: float


def _get_credentials() -> tuple[str, str] | None:
    user = os.environ.get('LSA_SAF_USER')
    passwd = os.environ.get('LSA_SAF_PASS')
    if user and passwd:
        return user, passwd
    return None


def _list_csv_urls(date: datetime) -> list[str]:
    """List available CSV URLs for a given date."""
    date_str = date.strftime('%Y%m%d')
    url = f'{_BASE_URL}/{date.year}/{date.month:02d}/{date.day:02d}/'
    try:
        creds = _get_credentials()
        if creds:
            import base64
            auth = base64.b64encode(f'{creds[0]}:{creds[1]}'.encode()).decode()
            req = urllib.request.Request(url, headers={'Authorization': f'Basic {auth}'})
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        logger.debug('FRP directory listing failed for %s: %s', date_str, e)
        return []

    # Extract CSV filenames
    import re
    pattern = rf'href="[^"]*/(LSA-509_MTG_MTFRPPIXEL-ListProduct_MTG-FD_{date_str}\d{{4}}\.csv\.gz)"'
    matches = re.findall(pattern, html)
    return [url + m for m in matches]


def _download_csv(csv_url: str) -> str | None:
    """Download and decompress a gzipped CSV."""
    creds = _get_credentials()
    try:
        if creds:
            import base64
            auth = base64.b64encode(f'{creds[0]}:{creds[1]}'.encode()).decode()
            req = urllib.request.Request(csv_url, headers={'Authorization': f'Basic {auth}'})
        else:
            req = urllib.request.Request(csv_url)

        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()

        if csv_url.endswith('.gz'):
            raw = gzip.decompress(raw)
        return raw.decode('utf-8', errors='replace')
    except Exception as e:
        logger.error('FRP download failed for %s: %s', csv_url, e)
        return None


def _parse_csv(csv_text: str) -> list[FRPDetection]:
    """Parse FRP CSV text into detections, filtered by bbox and confidence."""
    detections = []
    reader = csv.DictReader(io.StringIO(csv_text))

    for row in reader:
        try:
            lon = float(row['LONGITUDE'])
            lat = float(row['LATITUDE'])
        except (KeyError, ValueError):
            continue

        if not (_BBOX['lon_min'] <= lon <= _BBOX['lon_max']):
            continue
        if not (_BBOX['lat_min'] <= lat <= _BBOX['lat_max']):
            continue

        try:
            frp = float(row['FRP'])
            confidence = float(row['FIRE_CONFIDENCE'])
        except (KeyError, ValueError):
            continue

        if confidence < _MIN_CONFIDENCE:
            continue

        try:
            uncertainty = float(row.get('FRP_UNCERTAINTY', 0))
            pixel_size = float(row.get('PIXEL_SIZE', 0))
            bt_mir = float(row.get('BT_MIR', 0))
            bt_tir = float(row.get('BT_TIR', 0))
            acq_str = row.get('ACQTIME', '')
            acq_time = datetime.strptime(acq_str, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            uncertainty = 0.0
            pixel_size = 0.0
            bt_mir = 0.0
            bt_tir = 0.0
            acq_time = datetime.now(timezone.utc)

        detections.append(FRPDetection(
            longitude=lon,
            latitude=lat,
            frp_mw=frp,
            confidence=confidence,
            frp_uncertainty=uncertainty,
            pixel_size_km2=pixel_size,
            acquisition_time=acq_time,
            bt_mir=bt_mir,
            bt_tir=bt_tir,
        ))

    return detections


def _get_age_color(age_hours: float, frp_mw: float) -> tuple[str, int]:
    """
    Get color based on detection age and FRP intensity.

    Recent (<6h): yellow (high FRP) → red (low FRP)
    Older (6-24h): red (high FRP) → pink (low FRP)
    """
    # Normalize FRP to 0-1 (cap at 200 MW for scaling)
    frp_norm = min(frp_mw / 200.0, 1.0)

    if age_hours < 6:
        # Recent: yellow → red
        colors = _RECENT_COLORS
        idx = int(frp_norm * (len(colors) - 1))
        size = 5 + int(frp_norm * 5)  # 5-10
    else:
        # Older: red → pink
        colors = _OLDER_COLORS
        idx = int(frp_norm * (len(colors) - 1))
        size = 4 + int(frp_norm * 4)  # 4-8

    return colors[min(idx, len(colors) - 1)], size


def fetch_frp() -> dict:
    """
    Fetch FRP data for Iberia + France (last 24 hours).

    Returns a GeoJSON FeatureCollection with time-based coloring.
    Uses concurrent requests with early bail-out on failure.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    now = datetime.now(timezone.utc)

    # Collect CSVs from last 24h — concurrent directory listings
    dates_to_check = [now - timedelta(hours=h) for h in range(_WINDOW_HOURS + 1)]
    csv_urls = []
    _dir_errors = 0

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_list_csv_urls, d): d for d in dates_to_check}
        for future in as_completed(futures, timeout=12):
            try:
                urls = future.result(timeout=1)
                if urls:
                    csv_urls.extend(urls)
                else:
                    _dir_errors += 1
            except Exception:
                _dir_errors += 1

    # If all directory listings failed, server is probably down
    if _dir_errors >= len(dates_to_check):
        logger.warning('FRP: all directory listings failed — source may be down')
        return _empty_result()

    if not csv_urls:
        logger.info('FRP: no CSV files found for last %dh', _WINDOW_HOURS)
        return _empty_result()

    # Sort URLs oldest first
    csv_urls.sort()

    # Evenly sample ~30 files across the full 24h window
    n = len(csv_urls)
    if n > 30:
        step = n / 30
        csv_urls = [csv_urls[int(i * step)] for i in range(30)]
    # Re-sort most recent first for processing
    csv_urls.sort(reverse=True)

    # Download and parse CSVs concurrently, deduplicate by (lon, lat, acq_time)
    seen = set()
    all_detections = []

    def _fetch_one(url):
        csv_text = _download_csv(url)
        if csv_text is None:
            return []
        return _parse_csv(csv_text)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_one, url): url for url in csv_urls}
        for future in as_completed(futures, timeout=60):
            try:
                detections = future.result(timeout=1)
                for d in detections:
                    key = (round(d.longitude, 4), round(d.latitude, 4), d.acquisition_time)
                    if key not in seen:
                        seen.add(key)
                        all_detections.append(d)
            except Exception as e:
                logger.debug('FRP CSV parse error: %s', e)

    if not all_detections:
        logger.info('FRP: no detections in Iberia+France for last %dh', _WINDOW_HOURS)
        return _empty_result()

    # Sort by time (oldest first) for consistent rendering
    all_detections.sort(key=lambda d: d.acquisition_time)

    # Build features with age-based coloring
    features = []
    for d in all_detections:
        age_hours = (now - d.acquisition_time).total_seconds() / 3600.0
        color, size = _get_age_color(age_hours, d.frp_mw)

        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [d.longitude, d.latitude],
            },
            'properties': {
                'frp_mw': round(d.frp_mw, 1),
                'confidence': round(d.confidence * 100, 1),
                'frp_uncertainty': round(d.frp_uncertainty, 1),
                'pixel_size_km2': round(d.pixel_size_km2, 2),
                'acquisition_time': d.acquisition_time.isoformat(),
                'bt_mir': round(d.bt_mir, 1),
                'bt_tir': round(d.bt_tir, 1),
                'color': color,
                'radius': size,
                'age_hours': round(age_hours, 1),
            },
        })

    logger.info('FRP: %d detections in Iberia+France (last %dh)',
               len(features), _WINDOW_HOURS)

    return {
        'type': 'FeatureCollection',
        'features': features,
        'metadata': {
            'source': 'LSA SAF FRP-PIXEL (MTG)',
            'source_url': 'https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/',
            'detection_count': len(features),
            'window_hours': _WINDOW_HOURS,
            'bbox': _BBOX,
        },
    }


def _empty_result() -> dict:
    return {
        'type': 'FeatureCollection',
        'features': [],
        'metadata': {
            'source': 'LSA SAF FRP-PIXEL (MTG)',
            'source_url': 'https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/',
            'detection_count': 0,
            'window_hours': _WINDOW_HOURS,
            'bbox': _BBOX,
        },
    }
