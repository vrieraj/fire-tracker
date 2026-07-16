"""
Meteogram generator — simplified 4-panel weather chart.

Panels:
1. Wind: speed + gusts + direction arrows
2. Temperature: temp + dew point + apparent temp
3. Precipitation: rain + snow + cloud cover
4. Radiation: shortwave + CAPE

Mobile-friendly: narrow figure with clear labels.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta
from typing import Any

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import numpy as np

logger = logging.getLogger(__name__)

# Color palette
COLOR_TEMP = '#E74C3C'
COLOR_DEW = '#27AE60'
COLOR_APPARENT = '#E74C3C80'
COLOR_WIND = '#3498DB'
COLOR_GUST = '#3498DB50'
COLOR_PRECIP_RAIN = '#2980B9'
COLOR_PRECIP_SNOW = '#9B59B6'
COLOR_CLOUD = '#95A5A6'
COLOR_RADIATION = '#F39C12'
COLOR_CAPE = '#E74C3C'


def _wind_arrows(directions: list[float]) -> list[str]:
    """Convert wind direction (degrees) to arrow symbols."""
    arrows = []
    for d in directions:
        if d is None or np.isnan(d):
            arrows.append('')
            continue
        d = d % 360
        if d < 23 or d >= 337:
            arrows.append('\u2193')  # ↓ N
        elif d < 67:
            arrows.append('\u2199')  # ↙ NE
        elif d < 112:
            arrows.append('\u2190')  # ← E
        elif d < 157:
            arrows.append('\u2196')  # ↖ SE
        elif d < 202:
            arrows.append('\u2191')  # ↑ S
        elif d < 247:
            arrows.append('\u2197')  # ↗ SW
        elif d < 292:
            arrows.append('\u2192')  # → W
        else:
            arrows.append('\u2198')  # ↘ NW
    return arrows


def _night_shades(ax, times, is_day):
    """Add night shading based on is_day flag."""
    if not is_day or len(is_day) != len(times):
        return
    in_night = False
    start = None
    for i, (t, day) in enumerate(zip(times, is_day)):
        if day == 0 and not in_night:
            in_night = True
            start = t
        elif day == 1 and in_night:
            in_night = False
            ax.axvspan(start, t, alpha=0.1, color='#2C3E50', zorder=0)
    if in_night and start is not None:
        ax.axvspan(start, times[-1], alpha=0.1, color='#2C3E50', zorder=0)


def generate_meteogram(
    weather_data: Any,
    figsize: tuple[float, float] = (12, 10),
    dpi: int = 100,
) -> plt.Figure:
    """
    Generate a 4-panel meteogram from WeatherData.

    Parameters
    ----------
    weather_data : WeatherData
        From fire_tracker.weather.fetch_forecast()
    figsize : tuple
        Figure size (width, height) in inches
    dpi : int
        Resolution

    Returns
    -------
    matplotlib.figure.Figure
    """
    hourly = weather_data.hourly
    units = weather_data.hourly_units or {}

    # Parse times
    times = [datetime.fromisoformat(t) for t in hourly.get('time', [])]
    if not times:
        raise ValueError('No hourly data available')

    # Extract variables
    temp = hourly.get('temperature_2m', [])
    dew = hourly.get('dew_point_2m', [])
    apparent = hourly.get('apparent_temperature', [])
    wind_speed = hourly.get('wind_speed_10m', [])
    wind_dir = hourly.get('wind_direction_10m', [])
    wind_gusts = hourly.get('wind_gusts_10m', [])
    precip = hourly.get('precipitation', [])
    cloud = hourly.get('cloud_cover', [])
    radiation = hourly.get('shortwave_radiation', [])
    cape = hourly.get('cape', [])
    is_day = hourly.get('is_day', [])

    # Create figure
    fig, axes = plt.subplots(4, 1, figsize=figsize, dpi=dpi,
                              sharex=True, gridspec_kw={'height_ratios': [1.2, 1, 1, 0.8]})

    # Panel 0: Wind
    ax0 = axes[0]
    ax0.plot(times, wind_speed, color=COLOR_WIND, linewidth=1.5, label='Wind speed')
    if wind_gusts:
        ax0.fill_between(times, wind_speed, wind_gusts,
                         alpha=0.3, color=COLOR_GUST, label='Gusts')
    ax0.set_ylabel(f'Wind ({units.get("wind_speed_10m", "km/h")})')
    ax0.legend(loc='upper right', fontsize=8)
    ax0.grid(True, alpha=0.3)

    # Wind direction arrows (top of panel)
    if wind_dir:
        arrows = _wind_arrows(wind_dir)
        max_wind = max((w for w in wind_speed if w is not None), default=0)
        for i in range(0, len(times), max(1, len(times) // 20)):
            if i < len(arrows) and arrows[i]:
                ax0.annotate(arrows[i], xy=(times[i], max_wind * 0.9),
                           fontsize=10, ha='center', va='center', color='#7F8C8D')

    _night_shades(ax0, times, is_day)

    # Panel 1: Temperature
    ax1 = axes[1]
    ax1.plot(times, temp, color=COLOR_TEMP, linewidth=1.5, label='Temperature')
    if dew:
        ax1.plot(times, dew, color=COLOR_DEW, linewidth=1.5, label='Dew point')
    if apparent:
        ax1.plot(times, apparent, color=COLOR_APPARENT, linewidth=1,
                 linestyle='--', label='Apparent')
    ax1.set_ylabel(f'Temperature ({units.get("temperature_2m", "°C")})')
    ax1.legend(loc='upper right', fontsize=8)
    ax1.grid(True, alpha=0.3)

    # Add freezing line
    ax1.axhline(y=0, color='#3498DB', linewidth=0.5, linestyle=':', alpha=0.5)

    _night_shades(ax1, times, is_day)

    # Panel 2: Precipitation + Clouds
    ax2 = axes[2]
    if precip:
        ax2.bar(times, precip, width=0.03, color=COLOR_PRECIP_RAIN,
                alpha=0.7, label='Precipitation')
    ax2_twin = ax2.twinx()
    if cloud:
        ax2_twin.fill_between(times, cloud, alpha=0.3, color=COLOR_CLOUD, label='Clouds')
        ax2_twin.set_ylabel(f'Clouds ({units.get("cloud_cover", "%")})', color=COLOR_CLOUD)
        ax2_twin.set_ylim(0, 100)
    ax2.set_ylabel(f'Precip ({units.get("precipitation", "mm")})')
    ax2.legend(loc='upper right', fontsize=8)
    ax2.grid(True, alpha=0.3)

    _night_shades(ax2, times, is_day)

    # Panel 3: Radiation + CAPE
    ax3 = axes[3]
    if radiation:
        ax3.fill_between(times, radiation, alpha=0.5, color=COLOR_RADIATION,
                         label='Solar radiation')
        ax3.set_ylabel(f'Radiation ({units.get("shortwave_radiation", "W/m²")})')
    if cape:
        ax3_twin = ax3.twinx()
        ax3_twin.plot(times, cape, color=COLOR_CAPE, linewidth=1, label='CAPE')
        ax3_twin.set_ylabel(f'CAPE ({units.get("cape", "J/kg")})', color=COLOR_CAPE)
    ax3.legend(loc='upper right', fontsize=8)
    ax3.grid(True, alpha=0.3)

    _night_shades(ax3, times, is_day)

    # Format x-axis
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%d %H:%M'))
    axes[-1].xaxis.set_major_locator(mdates.HourLocator(interval=6))
    plt.setp(axes[-1].xaxis.get_majorticklabels(), rotation=45, ha='right')

    # Title
    loc = weather_data.location
    title = f'{loc.display_name}'
    if weather_data.model:
        title += f' — {weather_data.model}'
    fig.suptitle(title, fontsize=12, fontweight='bold', y=0.98)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    return fig


def meteogram_to_png(weather_data: Any, **kwargs) -> bytes:
    """
    Generate meteogram and return as PNG bytes.

    Parameters
    ----------
    weather_data : WeatherData
    **kwargs : passed to generate_meteogram()

    Returns
    -------
    bytes — PNG image data
    """
    fig = generate_meteogram(weather_data, **kwargs)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def meteogram_to_base64(weather_data: Any, **kwargs) -> str:
    """
    Generate meteogram and return as base64-encoded PNG.

    Parameters
    ----------
    weather_data : WeatherData
    **kwargs : passed to generate_meteogram()

    Returns
    -------
    str — base64-encoded PNG image
    """
    import base64
    png_bytes = meteogram_to_png(weather_data, **kwargs)
    return base64.b64encode(png_bytes).decode('ascii')
