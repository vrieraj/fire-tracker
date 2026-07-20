"""
Weather data module — geocoding, Open-Meteo best_match fetcher, and transforms.

Uses Open-Meteo's "Best Match" endpoint which automatically selects
the best weather model for any location worldwide.

Surface pipeline: fetch → transform (Fosberg FM, VPD FM, ignition prob)
Vertical pipeline: fetch pressure levels → C-Haines, BLH estimate
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import requests

logger = logging.getLogger(__name__)

_GEOCODING_URL = 'https://geocoding-api.open-meteo.com/v1/search'
_FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
_ELEVATION_URL = 'https://api.open-meteo.com/v1/elevation'
_HTTP_TIMEOUT = 30
_UA = 'FireTracker/0.2 (https://github.com/vrieraj/fire-tracker)'

# Pressure levels for wind profile panel (Open-Meteo supports these)
LEVELS_WIND = [700, 600, 500, 250]
# Pressure levels for C-Haines + BLH calculation
LEVELS_BLH = [1000, 925, 850, 700]
# All levels to fetch (union)
ALL_LEVELS = sorted(set(LEVELS_WIND) | set(LEVELS_BLH))


# ═══════════════════════════════════════════════════════════════════════════
#  GEOCODING & LOCATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Location:
    """Geocoded location with coordinates and metadata."""

    name: str
    latitude: float
    longitude: float
    elevation: float = 0.0
    country: str = ''
    country_code: str = ''
    admin1: str = ''  # state/province
    admin2: str = ''  # county/district
    timezone: str = 'auto'
    population: int = 0

    @property
    def display_name(self) -> str:
        parts = [self.name]
        if self.admin1:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ', '.join(parts)


def geocode(query: str, limit: int = 5, language: str = 'es') -> list[Location]:
    """Search for locations by name using Open-Meteo geocoding API."""
    try:
        resp = requests.get(
            _GEOCODING_URL,
            params={
                'name': query,
                'count': limit,
                'language': language,
                'format': 'json',
            },
            headers={'User-Agent': _UA},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error('Geocoding error: %s', e)
        return []

    return [
        Location(
            name=r.get('name', ''),
            latitude=r.get('latitude', 0),
            longitude=r.get('longitude', 0),
            elevation=r.get('elevation', 0),
            country=r.get('country', ''),
            country_code=r.get('country_code', ''),
            admin1=r.get('admin1', ''),
            admin2=r.get('admin2', ''),
            timezone=r.get('timezone', 'auto'),
            population=r.get('population', 0),
        )
        for r in data.get('results', [])
    ]


def get_elevation(latitude: float, longitude: float) -> float:
    """Get elevation for a coordinate using Open-Meteo elevation API."""
    try:
        resp = requests.get(
            _ELEVATION_URL,
            params={'latitude': latitude, 'longitude': longitude},
            headers={'User-Agent': _UA},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        elevations = data.get('elevation', [])
        return float(elevations[0]) if elevations else 0.0
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  HTTP HELPERS
# ═══════════════════════════════════════════════════════════════════════════

_RETRY_STATUS_CODES = {502, 503, 504, 429}
_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.5


def _fetch_with_retries(url: str, params: dict, label: str) -> dict | None:
    """GET with exponential backoff for transient failures."""
    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = requests.get(
                url, params=params,
                headers={'User-Agent': _UA},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = exc
            logger.warning('%s request failed (attempt %d/%d): %s',
                           label, attempt, _MAX_RETRIES, exc)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF * (2 ** (attempt - 1)))
            continue

        if response.status_code == 200:
            data = response.json()
            if data.get('error'):
                logger.error('%s API error: %s',
                             label, data.get('reason', data))
                return None
            return data

        if response.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES:
            logger.warning('%s HTTP %d (attempt %d/%d), retrying…',
                           label, response.status_code, attempt, _MAX_RETRIES)
            time.sleep(_RETRY_BACKOFF * (2 ** (attempt - 1)))
            continue

        logger.error('%s HTTP %d: %s',
                     label, response.status_code, response.text[:300])
        return None

    logger.error('%s exhausted %d retries: %s', label, _MAX_RETRIES, last_error)
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  WEATHER DATA CONTAINER
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class WeatherData:
    """Container for processed weather + vertical data."""

    location: Location
    model: str
    sfc: pd.DataFrame       # surface data with transforms
    vrt: pd.DataFrame       # pressure-level data (wide format)
    elev: float = 0.0

    @property
    def times(self) -> pd.DatetimeIndex:
        return self.sfc['time']


# ═══════════════════════════════════════════════════════════════════════════
#  DATA FETCHING
# ═══════════════════════════════════════════════════════════════════════════

def fetch_surface(
    location: Location,
    forecast_days: int = 1,
    past_days: int = 1,
) -> pd.DataFrame | None:
    """
    Fetch hourly surface data from Open-Meteo best_match.

    Returns DataFrame or None on error.
    """
    hourly_vars = [
        'temperature_2m', 'relative_humidity_2m', 'dew_point_2m',
        'apparent_temperature',
        'wind_speed_10m', 'wind_direction_10m', 'wind_gusts_10m',
        'vapour_pressure_deficit', 'is_day',
        'precipitation', 'weather_code',
        'cloud_cover', 'pressure_msl', 'surface_pressure',
        'shortwave_radiation', 'cape',
    ]
    daily_vars = [
        'weather_code', 'temperature_2m_max', 'temperature_2m_min',
        'precipitation_sum', 'sunrise', 'sunset',
        'wind_speed_10m_max', 'wind_gusts_10m_max',
    ]

    params = {
        'latitude': location.latitude,
        'longitude': location.longitude,
        'elevation': location.elevation,
        'hourly': ','.join(hourly_vars),
        'daily': ','.join(daily_vars),
        'forecast_days': forecast_days,
        'past_days': past_days,
        'timezone': location.timezone,
        'models': 'best_match',
    }

    data = _fetch_with_retries(_FORECAST_URL, params, 'surface[best_match]')
    if data is None:
        return None

    df = pd.DataFrame(data['hourly'])
    df['time'] = pd.to_datetime(df['time'])
    df['source'] = data.get('model', 'best_match')
    return df


def fetch_vertical(
    location: Location,
    forecast_days: int = 1,
    past_days: int = 1,
) -> pd.DataFrame | None:
    """
    Fetch hourly pressure-level data from Open-Meteo best_match.

    Returns wide DataFrame with columns like wind_speed_700hPa, etc.
    """
    vert_vars = ['boundary_layer_height']
    for level in ALL_LEVELS:
        vert_vars.append(f'temperature_{level}hPa')
        vert_vars.append(f'relative_humidity_{level}hPa')
        vert_vars.append(f'wind_speed_{level}hPa')
        vert_vars.append(f'wind_direction_{level}hPa')
        vert_vars.append(f'geopotential_height_{level}hPa')

    params = {
        'latitude': location.latitude,
        'longitude': location.longitude,
        'elevation': location.elevation,
        'hourly': ','.join(vert_vars),
        'forecast_days': forecast_days,
        'past_days': past_days,
        'timezone': location.timezone,
        'models': 'best_match',
    }

    data = _fetch_with_retries(_FORECAST_URL, params, 'vertical[best_match]')
    if data is None:
        return None

    df = pd.DataFrame(data['hourly'])
    df['time'] = pd.to_datetime(df['time'])

    # Coerce numeric columns
    for level in ALL_LEVELS:
        for var in ['temperature', 'relative_humidity', 'wind_speed',
                    'wind_direction', 'geopotential_height']:
            col = f'{var}_{level}hPa'
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

    df['boundary_layer_height'] = pd.to_numeric(
        df.get('boundary_layer_height'), errors='coerce')

    # Wind u,v components (knots) for barb display
    for level in ALL_LEVELS:
        ws = df.get(f'wind_speed_{level}hPa', pd.Series(dtype=float))
        wd = df.get(f'wind_direction_{level}hPa', pd.Series(dtype=float))
        ws_kt = ws * 0.539957  # km/h → knots
        df[f'u_{level}'] = -ws_kt * np.sin(np.radians(wd))
        df[f'v_{level}'] = -ws_kt * np.cos(np.radians(wd))

    # Inversions between consecutive BLH levels (descending pressure)
    blh_levels = sorted(LEVELS_BLH, reverse=True)
    for i in range(len(blh_levels) - 1):
        lower = blh_levels[i]
        upper = blh_levels[i + 1]
        t_lo = df.get(f'temperature_{lower}hPa', pd.Series(dtype=float))
        t_hi = df.get(f'temperature_{upper}hPa', pd.Series(dtype=float))
        df[f'inversion_{lower}_{upper}'] = t_hi > t_lo

    # BLH: API value with Richardson fallback
    blh_ri = _compute_blh_richardson(df)
    df['blh_richardson'] = blh_ri
    mask_missing = df['boundary_layer_height'].isna()
    df.loc[mask_missing, 'boundary_layer_height'] = df.loc[mask_missing, 'blh_richardson']

    return df


def fetch_forecast(
    location: Location,
    forecast_days: int = 1,
    past_days: int = 1,
) -> WeatherData | None:
    """
    Fetch surface + vertical data and return WeatherData with transforms.

    This is the main entry point for the meteogram pipeline.
    """
    sfc = fetch_surface(location, forecast_days, past_days)
    if sfc is None:
        logger.error('Failed to fetch surface data for %s', location.name)
        return None

    vrt = fetch_vertical(location, forecast_days, past_days)
    if vrt is None:
        logger.warning('Vertical data unavailable, meteogram will use wind arrows')
        # Create empty vrt with matching time index
        vrt = pd.DataFrame({'time': sfc['time']})

    # Apply surface transforms
    sfc = _transform_sfc(sfc)

    # Apply vertical transforms (C-Haines)
    if 'temperature_850hPa' in vrt.columns:
        vrt['c_haines'] = _compute_haines(vrt)

    model_name = sfc['source'].iloc[0] if 'source' in sfc.columns else 'best_match'

    return WeatherData(
        location=location,
        model=model_name,
        sfc=sfc,
        vrt=vrt,
        elev=location.elevation,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  TRANSFORMS — SURFACE
# ═══════════════════════════════════════════════════════════════════════════

def _transform_sfc(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize and compute derived surface variables."""
    df = df.copy()
    df['day_year'] = df['time'].dt.dayofyear
    df['wind_direction_arrow'] = _wind_arrows(df)
    df['fuel_moisture'] = _fuel_moisture_fosberg(df)
    df['fuel_moisture_vpd'] = _fuel_moisture_vpd(df)
    df['prob_ignition'] = _prob_ignition(df)
    return df


def _wind_arrows(df: pd.DataFrame) -> pd.Series:
    """Convert wind direction (degrees) to arrow symbols."""
    return pd.cut(
        df['wind_direction_10m'] % 360,
        bins=[-1, 23, 67, 112, 157, 202, 247, 292, 337, 361],
        labels=[
            r'$\downarrow$', r'$\swarrow$', r'$\leftarrow$',
            r'$\nwarrow$', r'$\uparrow$', r'$\nearrow$',
            r'$\rightarrow$', r'$\searrow$', r'$\downarrow$'
        ],
        right=False, ordered=False,
    )


def _fuel_moisture_fosberg(df: pd.DataFrame) -> list:
    """Fosberg Table A — classic NFDRS 1-h fuel moisture."""
    tabla = {
        'dia': {
            't10': [1,2,2,3,4,5,5,6,7,7,7,8,9,9,10,10,11,12,13,13,13],
            't21': [1,2,2,3,4,5,5,6,6,7,7,8,8,9,9,10,11,12,12,12,13],
            't32': [1,1,2,2,3,4,5,5,6,7,7,8,8,8,9,10,10,11,12,12,13],
            't43': [1,1,2,2,3,4,4,5,6,7,7,8,8,8,9,10,10,11,12,12,13],
            'tmax':[1,1,2,2,3,4,4,5,6,7,7,8,8,8,9,10,10,11,12,12,13],
        },
        'noche': {
            't10': [1,2,3,4,5,6,7,8,9,9,11,11,12,13,14,16,18,21,24,25,25],
            't21': [1,2,3,4,5,6,6,8,8,9,10,11,11,12,14,16,17,20,23,25,25],
            't32': [1,2,3,4,4,5,6,7,8,9,10,10,11,12,13,15,17,20,23,25,25],
            't43': [1,2,3,3,4,5,6,7,8,9,9,10,10,11,13,14,16,19,22,25,25],
            'tmax':[1,2,2,3,4,5,6,6,8,8,9,9,10,11,12,14,16,19,21,24,25],
        },
    }

    def get_temp_key(temp):
        if temp is None or np.isnan(temp):
            return None
        if temp < 10:   return 't10'
        elif temp < 21:  return 't21'
        elif temp < 32:  return 't32'
        elif temp < 43:  return 't43'
        else:            return 'tmax'

    values = []
    for _, row in df.iterrows():
        period = 'dia' if row['is_day'] == 1 else 'noche'
        tkey = get_temp_key(row['temperature_2m'])
        if tkey is None or pd.isna(row['relative_humidity_2m']):
            values.append(None)
            continue
        hum_idx = int(np.clip(round(row['relative_humidity_2m'] / 5), 0, 20))
        values.append(tabla[period][tkey][hum_idx])
    return values


def _fuel_moisture_vpd(df: pd.DataFrame) -> list:
    """Resco de Dios et al. (2015, 2024) — VPD-based FM10h model."""
    vpd = df.get('vapour_pressure_deficit')
    if vpd is None:
        return [None] * len(df)
    vpd = pd.to_numeric(vpd, errors='coerce')
    FM0, FM1, m = 3.5, 28.0, 1.5
    fm = FM0 + FM1 * np.exp(-m * vpd)
    return fm.where(vpd.notna(), other=None).tolist()


def _prob_ignition(df: pd.DataFrame) -> list:
    """Probability of ignition lookup (temperature × fuel moisture)."""
    tabla = [
        [90,70,60,60,50,40,40,30,30,20,20,20,10,10,10,10],
        [90,70,60,60,50,40,40,30,30,20,20,20,10,10,10,10],
        [90,80,70,60,50,40,40,30,30,20,20,20,10,10,10,10],
        [90,80,70,60,50,40,40,30,30,20,20,20,10,10,10,10],
        [100,80,70,60,60,50,40,40,30,30,20,20,20,10,10,10],
        [100,90,80,70,60,50,40,40,30,30,20,20,20,20,10,10],
        [100,90,80,70,60,50,50,40,30,30,30,20,20,20,10,10],
        [100,90,80,70,60,60,50,40,40,30,30,20,20,20,10,10],
        [100,100,90,80,70,60,50,40,40,30,30,30,20,20,20,10],
    ]
    values = []
    for _, row in df.iterrows():
        t = row['temperature_2m']
        fm = row['fuel_moisture']
        if pd.isna(t) or fm is None or pd.isna(fm):
            values.append(None)
            continue
        t_idx = int(np.clip(t / 5, 0, 8))
        h_idx = int(np.clip(fm - 2, 0, 15))
        values.append(tabla[t_idx][h_idx])
    return values


# ═══════════════════════════════════════════════════════════════════════════
#  TRANSFORMS — VERTICAL (C-Haines, BLH)
# ═══════════════════════════════════════════════════════════════════════════

def _dewpoint_from_t_rh(T, rh):
    """Magnus formula: dewpoint from temperature (°C) and RH (%)."""
    e_s = 6.112 * np.exp(17.67 * T / (T + 243.5))
    e = (rh / 100.0) * e_s
    return 243.5 * np.log(e / 6.112) / (17.67 - np.log(e / 6.112))


def _theta_v(T_celsius, rh_pct, p_hPa):
    """
    Virtual potential temperature θv (K).

    θ  = T·(1000/p)^0.286
    e_s = 6.112·exp(17.67·T / (T+243.5))     (Magnus, hPa)
    r  = 0.622·e / (p - e)                    (mixing ratio)
    θv = θ·(1 + 0.61·r)
    """
    T_K = T_celsius + 273.15
    theta = T_K * (1000.0 / p_hPa) ** 0.286
    e_s = 6.112 * np.exp(17.67 * T_celsius / (T_celsius + 243.5))
    e = rh_pct / 100.0 * e_s
    r = 0.622 * e / max(p_hPa - e, 0.1)
    return theta * (1 + 0.61 * r)


def _compute_haines(df: pd.DataFrame) -> np.ndarray:
    """
    Continuous Haines Index (CH) from 850–700 hPa layer.

    CH = A + B + C, range 0–9
    A = clip((T_850 − T_700 − 3) / 3, 0, 3)   — thermal lapse
    B = clip((T_850 − Td_850) / 4, 0, 3)       — low-level dryness
    C = clip((T_700 − Td_700) / 6, 0, 3)       — upper-level dryness
    """
    required = ['temperature_850hPa', 'temperature_700hPa',
                'relative_humidity_850hPa', 'relative_humidity_700hPa']
    if not all(col in df.columns for col in required):
        return np.full(len(df), np.nan)

    T850 = pd.to_numeric(df['temperature_850hPa'], errors='coerce').values
    T700 = pd.to_numeric(df['temperature_700hPa'], errors='coerce').values
    RH850 = pd.to_numeric(df['relative_humidity_850hPa'], errors='coerce').values
    RH700 = pd.to_numeric(df['relative_humidity_700hPa'], errors='coerce').values

    Td850 = np.full(len(df), np.nan)
    Td700 = np.full(len(df), np.nan)
    m850 = np.isfinite(T850) & np.isfinite(RH850)
    m700 = np.isfinite(T700) & np.isfinite(RH700)
    Td850[m850] = _dewpoint_from_t_rh(T850[m850], RH850[m850])
    Td700[m700] = _dewpoint_from_t_rh(T700[m700], RH700[m700])

    A = np.clip((T850 - T700 - 3) / 3, 0, 3)
    B = np.clip((T850 - Td850) / 4, 0, 3)
    C = np.clip((T700 - Td700) / 6, 0, 3)

    haines = A + B + C
    haines[~(np.isfinite(A) & np.isfinite(B) & np.isfinite(C))] = np.nan
    return haines


def _compute_blh_richardson(df: pd.DataFrame) -> np.ndarray:
    """
    Estimate BLH using Bulk Richardson Number across 4 pressure levels.

    Ri(z) = g·(θv(z) - θv_sfc)·(z - z_sfc) / [θv_sfc · |Δu|²]

    When Ri crosses 0.25, linear interpolation gives BLH (meters AGL).
    """
    g = 9.81
    Ri_crit = 0.25
    levels = sorted(LEVELS_BLH)

    blh_values = np.full(len(df), np.nan)

    for idx in range(len(df)):
        T_sfc = df.get(f'temperature_{levels[0]}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan
        rh_sfc = df.get(f'relative_humidity_{levels[0]}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan
        z_sfc = df.get(f'geopotential_height_{levels[0]}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan
        ws_sfc = df.get(f'wind_speed_{levels[0]}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan
        wd_sfc = df.get(f'wind_direction_{levels[0]}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan

        if any(np.isnan(x) if isinstance(x, (int, float, np.floating)) else True
               for x in [T_sfc, rh_sfc, z_sfc, ws_sfc, wd_sfc]):
            continue

        theta_v_sfc = _theta_v(T_sfc, rh_sfc, levels[0])
        u_sfc = -ws_sfc / 3.6 * np.sin(np.radians(wd_sfc))
        v_sfc = -ws_sfc / 3.6 * np.cos(np.radians(wd_sfc))

        Ri_prev = 0.0
        z_prev = z_sfc

        for lev in levels[1:]:
            T = df.get(f'temperature_{lev}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan
            rh = df.get(f'relative_humidity_{lev}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan
            z = df.get(f'geopotential_height_{lev}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan
            ws = df.get(f'wind_speed_{lev}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan
            wd = df.get(f'wind_direction_{lev}hPa', pd.Series(dtype=float)).iloc[idx] if idx < len(df) else np.nan

            if any(np.isnan(x) if isinstance(x, (int, float, np.floating)) else True
                   for x in [T, rh, z, ws, wd]):
                continue

            theta_v = _theta_v(T, rh, lev)
            dz = z - z_sfc
            if dz <= 0:
                continue

            u = -ws / 3.6 * np.sin(np.radians(wd))
            v = -ws / 3.6 * np.cos(np.radians(wd))
            du2 = max((u - u_sfc)**2 + (v - v_sfc)**2, 0.01)

            Ri = g * (theta_v - theta_v_sfc) * dz / (theta_v_sfc * du2)

            if Ri >= Ri_crit:
                if Ri != Ri_prev:
                    frac = (Ri_crit - Ri_prev) / (Ri - Ri_prev)
                    blh_z = z_prev + frac * (z - z_prev) - z_sfc
                else:
                    blh_z = dz
                blh_values[idx] = max(blh_z, 10)
                break

            Ri_prev = Ri
            z_prev = z

    return blh_values
