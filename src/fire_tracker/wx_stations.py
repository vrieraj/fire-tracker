"""
Weather station discovery and historical data retrieval.

Sources
-------
WU PWS (Weather Underground Personal Weather Stations)
    - Discovery via tile API (products/614)
    - Hourly history via api.weather.com/v2/pws/history/hourly
    - Requires a WU API key (WU_API_KEY env var or parameter)

Output DataFrame columns match Open-Meteo variables so that
the meteogram pipeline can use station data directly.
"""

from __future__ import annotations

import calendar
import math
import os
import time
from typing import Any

import pandas as pd
import requests

# ── WU API constants ──────────────────────────────────────────────────────────

_WU_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    "Accept":  "*/*",
    "Origin":  "https://www.wunderground.com",
    "Referer": "https://www.wunderground.com/wundermap",
}

_TILE_EP    = "https://api.weather.com/v2/vector-api/products/614/features"
_HISTORY_EP = "https://api.weather.com/v2/pws/history/hourly"

_LOD       = 9
_TILE_SIZE = 512
_EFF_ZOOM  = 8   # lod=9 + tile-size=512 → effective Slippy-map zoom 8

_HTTP_TIMEOUT = 15


# ── Geo helpers ───────────────────────────────────────────────────────────────

def _bbox_from_radius(lat: float, lon: float, radius_km: float):
    """Axis-aligned bounding box for a radius around (lat, lon)."""
    R    = 6371.0
    dlat = math.degrees(radius_km / R)
    dlon = math.degrees(radius_km / (R * math.cos(math.radians(lat))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def _latlon_to_tile(lat: float, lon: float, zoom: int):
    n  = 2 ** zoom
    x  = int((lon + 180) / 360 * n)
    lr = math.radians(lat)
    y  = int((1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _bbox_to_tiles(min_lat, min_lon, max_lat, max_lon):
    x_min, y_max = _latlon_to_tile(min_lat, min_lon, _EFF_ZOOM)
    x_max, y_min = _latlon_to_tile(max_lat, max_lon, _EFF_ZOOM)
    return [
        (x, y)
        for x in range(x_min, x_max + 1)
        for y in range(y_min, y_max + 1)
    ]


def _haversine_km(lat1, lon1, lat2, lon2):
    """Haversine distance in km between two points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── WU unit conversions ───────────────────────────────────────────────────────

def _f2c(f):      return round((f - 32) * 5 / 9, 1) if f is not None else None
def _mph2kmh(v):  return round(v * 1.60934, 1)       if v is not None else None
def _inhg2hpa(v): return round(v * 33.8639, 1)       if v is not None else None
def _in2mm(v):    return round(v * 25.4, 2)           if v is not None else None


# ── WU: current-observation time params (tile API) ────────────────────────────

def _wu_time_params():
    now_ms = int(time.time() * 1000)
    block  = 900_000          # 15-minute windows
    now_r  = (now_ms // block) * block
    return [
        f"time={now_r - (i + 1) * block}-{now_r - i * block}:{op}"
        for i, op in enumerate([0, 15, 30, 45, 60])
    ]


# ── WU: station discovery ─────────────────────────────────────────────────────

def fetch_wu_stations_near(
    lat: float, lon: float, radius_km: float, api_key: str
) -> list[dict]:
    """
    Discover WU PWS stations within `radius_km` of (lat, lon).

    Returns a list of station dicts with current observations.
    Each dict has keys:
        stationId, platform, lat, lon, name, adm1, country, elev_m,
        temp_c, humidity_pct, windspeed_kmh, windgust_kmh, pressure_hpa,
        rain_daily_mm, distance_km
    """
    min_lat, min_lon, max_lat, max_lon = _bbox_from_radius(lat, lon, radius_km)
    tiles = _bbox_to_tiles(min_lat, min_lon, max_lat, max_lon)

    stations: dict[str, dict] = {}
    for x, y in tiles:
        url = (
            f"{_TILE_EP}?x={x}&y={y}&lod={_LOD}&apiKey={api_key}"
            f"&tile-size={_TILE_SIZE}&{'&'.join(_wu_time_params())}&stepped=true"
        )
        try:
            r = requests.get(url, headers=_WU_HEADERS, timeout=_HTTP_TIMEOUT)
            if r.status_code != 200:
                continue
            seen: set[str] = set()
            for block_data in r.json().values():
                for feat in block_data.get("features", []):
                    fid = feat.get("id")
                    if not fid or fid in seen:
                        continue
                    seen.add(fid)
                    s = _parse_wu_feature(feat, lat, lon, min_lat, min_lon,
                                          max_lat, max_lon, radius_km)
                    if s:
                        stations[fid] = s
        except Exception:
            pass
        time.sleep(0.2)

    # Sort by distance
    return sorted(stations.values(), key=lambda s: s.get("distance_km", 999))


def _parse_wu_feature(
    feat: dict, center_lat: float, center_lon: float,
    min_lat: float, min_lon: float, max_lat: float, max_lon: float,
    radius_km: float,
) -> dict | None:
    p      = feat.get("properties", {})
    coords = feat.get("geometry", {}).get("coordinates", [None, None])
    lon, stlat = coords[0], coords[1]
    if stlat is None or lon is None:
        return None
    if not (min_lat <= stlat <= max_lat and min_lon <= lon <= max_lon):
        return None
    dist = _haversine_km(center_lat, center_lon, stlat, lon)
    if dist > radius_km:
        return None
    return {
        "stationId":     feat.get("id"),
        "platform":      "WU_PWS",
        "lat":           stlat,
        "lon":           lon,
        "name":          (p.get("neighborhood") or "").strip(),
        "adm1":          (p.get("adm1") or "").strip(),
        "country":       (p.get("country") or "").strip(),
        "elev_m":        p.get("elev"),
        "temp_c":        _f2c(p.get("tempf")),
        "humidity_pct":  p.get("humidity"),
        "windspeed_kmh": _mph2kmh(p.get("windspeedmph")),
        "winddir_avg":   p.get("winddir"),
        "windgust_kmh":  _mph2kmh(p.get("windgustmph")),
        "pressure_hpa":  _inhg2hpa(p.get("baromin")),
        "rain_daily_mm": _in2mm(p.get("dailyrainin")),
        "distance_km":   round(dist, 1),
    }


# ── WU: hourly history ────────────────────────────────────────────────────────

def fetch_wu_hourly(
    station_id: str, d_start: str, d_end: str, api_key: str
) -> pd.DataFrame | None:
    """
    Download WU PWS hourly history for [d_start, d_end].

    Returns a DataFrame with Open-Meteo-compatible columns, or None.
    """
    start = pd.Timestamp(d_start)
    end   = pd.Timestamp(d_end) + pd.Timedelta(days=1)

    rows: list[dict] = []
    y, m = int(d_start[:4]), int(d_start[5:7])
    y_end, m_end = int(d_end[:4]), int(d_end[5:7])

    while (y, m) <= (y_end, m_end):
        last = calendar.monthrange(y, m)[1]
        url = (
            f"{_HISTORY_EP}?stationId={station_id}&format=json&units=m"
            f"&startDate={y}{m:02d}01&endDate={y}{m:02d}{last:02d}"
            f"&numericPrecision=decimal&apiKey={api_key}"
        )
        try:
            r = requests.get(url, headers=_WU_HEADERS, timeout=20)
            if r.status_code == 200:
                for obs in r.json().get("observations", []):
                    row = _parse_wu_obs(obs)
                    if row:
                        rows.append(row)
        except Exception:
            pass
        time.sleep(0.5)
        m += 1
        if m > 12:
            m, y = 1, y + 1

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])
    df = df[(df["time"] >= start) & (df["time"] < end)]
    df = df.sort_values("time").reset_index(drop=True)

    df["is_day"] = df["time"].dt.hour.between(6, 20).astype(int)
    df["vapour_pressure_deficit"] = float("nan")
    df["source"] = station_id
    return df if not df.empty else None


def _parse_wu_obs(obs: dict) -> dict | None:
    t_str = obs.get("obsTimeLocal") or obs.get("obsTimeUtc")
    if not t_str:
        return None
    m = obs.get("metric", {})
    return {
        "time":                  t_str,
        "temperature_2m":        m.get("tempAvg"),
        "dew_point_2m":          m.get("dewptAvg"),
        "relative_humidity_2m":  obs.get("humidityAvg"),
        "wind_speed_10m":        m.get("windspeedAvg"),
        "wind_gusts_10m":        m.get("windgustHigh"),
        "wind_direction_10m":    obs.get("winddirAvg"),
    }


def get_wu_api_key() -> str | None:
    """Get WU API key from environment variable."""
    return os.environ.get("WU_API_KEY")
