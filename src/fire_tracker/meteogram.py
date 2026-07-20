"""
Meteogram generator — 5-panel wildfire weather chart.

Panels (matching open-meteograms layout):
  0. WIND PROFILE: wind barbs at 700/600/500/250 hPa + BLH + C-Haines bar
  1. WIND: surface wind speed + gusts + direction arrows
  2. TEMP/RH: temperature + dew point + relative humidity
  3. FUEL: Fosberg 1h + Resco VPD 10h + ignition probability semaphore

Uses matplotlib for server-side rendering (MPLBACKEND=Agg).
"""

from __future__ import annotations

import io
import logging

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.dates as mdates
import matplotlib.lines as mlines
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Pressure levels for wind profile panel
LEVELS_DISPLAY = [700, 600, 500, 250]
# Approximate altitudes (m ASL) for y-axis mapping
ALTITUDES = {
    250: 10500, 500: 5600, 600: 4200, 700: 3000,
}


def _night_shades(ax, times_mpl, is_day):
    """Add night shading based on is_day flag."""
    if is_day is None or len(is_day) != len(times_mpl):
        return
    in_night = False
    start = None
    for i, (t, day) in enumerate(zip(times_mpl, is_day)):
        if day == 0 and not in_night:
            in_night = True
            start = t
        elif day == 1 and in_night:
            in_night = False
            ax.axvspan(start, t, alpha=0.1, color='#2C3E50', zorder=0)
    if in_night and start is not None:
        ax.axvspan(start, times_mpl[-1], alpha=0.1, color='#2C3E50', zorder=0)


def generate_meteogram(
    weather_data,
    figsize: tuple[float, float] = (12, 10),
    dpi: int = 100,
) -> plt.Figure:
    """
    Generate a 5-panel wildfire weather meteogram.

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
    sfc = weather_data.sfc
    vrt = weather_data.vrt

    if sfc is None or sfc.empty:
        raise ValueError('No surface data available')

    times = sfc['time']
    times_mpl = mdates.date2num(times)

    is_day = sfc['is_day'].values if 'is_day' in sfc.columns else None

    # Check if vertical profile data is available
    has_vrt = (vrt is not None
               and not vrt.empty
               and 'temperature_700hPa' in vrt.columns
               and vrt['temperature_700hPa'].notna().any())

    # ── FIGURE ────────────────────────────────────────────
    if has_vrt:
        height_ratios = [1.6, 1, 1, 1]
        total_rows = 4
    else:
        height_ratios = [0.5, 1, 1, 1]
        total_rows = 4

    fig, ax = plt.subplots(
        total_rows, 1, figsize=figsize, dpi=dpi,
        gridspec_kw={'height_ratios': height_ratios},
    )
    TOP, WIND, TEMP, FUEL = range(4)

    # RH twin axis on TEMP panel
    ax_rh = ax[TEMP].twinx()
    ax[TEMP].set_zorder(ax_rh.get_zorder() + 1)
    ax[TEMP].patch.set_visible(False)

    # ── AXIS FORMAT ───────────────────────────────────────
    init_date = times.iloc[0]
    end_date = times.iloc[-1]
    init_mpl = mdates.date2num(init_date)
    end_mpl = mdates.date2num(end_date)
    n_days = (end_date - init_date).days

    for i in range(total_rows):
        ax[i].set_xlim(init_mpl, end_mpl)
        if n_days <= 2:
            ax[i].xaxis.set_major_locator(mdates.DayLocator())
            ax[i].xaxis.set_minor_locator(mdates.HourLocator(byhour=range(3, 24, 3)))
        elif n_days <= 4:
            ax[i].xaxis.set_major_locator(mdates.DayLocator())
            ax[i].xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18]))
        else:
            ax[i].xaxis.set_major_locator(mdates.DayLocator())
        if n_days <= 4:
            ax[i].xaxis.grid(True, which='minor', alpha=0.4, linestyle=':')
        if i < total_rows - 1:
            ax[i].tick_params(labelbottom=False)
        else:
            ax[i].xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
            if n_days <= 4:
                ax[i].xaxis.set_minor_formatter(mdates.DateFormatter('%H:%M'))
                ax[i].tick_params(axis='x', which='minor', labelsize=7, length=3)

    # ── NIGHT SHADING ─────────────────────────────────────
    if is_day is not None:
        mask_night = (is_day == 0)
        for a in [ax[WIND], ax[TEMP], ax[FUEL]]:
            a.fill_between(times_mpl, 0, 100,
                           where=mask_night, alpha=0.25, color='lightblue', zorder=0)

    # ── PALETTE (matching open-meteograms style) ─────────
    c_wind = '#27AE60'
    c_gust = '#F39C12'
    c_temp = '#E74C3C'
    c_dew  = '#95A5A6'
    c_rh   = '#3498DB'
    c_fm_f = '#F39C12'
    c_fm_v = '#E74C3C'

    # ════════════════════════════════════════════════════════
    #  PANEL 0: WIND PROFILE + BLH + C-HAINES
    # ════════════════════════════════════════════════════════
    if has_vrt:
        _draw_wind_profile(ax[TOP], vrt, times_mpl, is_day)
    else:
        _draw_wind_direction_fallback(ax[TOP], sfc, times_mpl, is_day)

    # ════════════════════════════════════════════════════════
    #  PANEL 1: WIND SPEED + GUSTS + DIRECTION ARROWS
    # ════════════════════════════════════════════════════════
    ax[WIND].plot(times_mpl, sfc['wind_speed_10m'], color=c_wind,
                  linewidth=1.5, label='Wind Speed')
    if 'wind_gusts_10m' in sfc.columns:
        gusts = pd.to_numeric(sfc['wind_gusts_10m'], errors='coerce')
        if gusts.notna().any():
            ax[WIND].fill_between(times_mpl, sfc['wind_speed_10m'], gusts,
                                  alpha=0.3, color=c_gust, label='Gusts')
            ax[WIND].plot(times_mpl, gusts, color=c_gust, linewidth=0.8,
                          linestyle='--', alpha=0.6)

    ax[WIND].set_ylabel('Wind (km/h)')
    ax[WIND].grid(True, alpha=0.3)

    max_gusts = sfc['wind_gusts_10m'].max() if 'wind_gusts_10m' in sfc.columns else sfc['wind_speed_10m'].max()
    if pd.notna(max_gusts) and max_gusts <= 50:
        ax[WIND].set_ylim(0, 60)
    else:
        ax[WIND].set_ylim(0, None)

    ax[WIND].legend(loc='upper left', fontsize=7)

    # Wind direction arrows at top
    if 'wind_direction_10m' in sfc.columns:
        arrows = _wind_arrows(sfc['wind_direction_10m'])
        max_ws = sfc['wind_speed_10m'].max()
        step = max(1, len(times_mpl) // 20)
        for i in range(0, len(times_mpl), step):
            if i < len(arrows) and pd.notna(arrows.iloc[i]):
                ax[WIND].annotate(arrows.iloc[i],
                                  xy=(times_mpl[i], max_ws * 0.9),
                                  fontsize=10, ha='center', va='center',
                                  color='#7F8C8D')

    if is_day is not None:
        _night_shades(ax[WIND], times_mpl, is_day)

    # ════════════════════════════════════════════════════════
    #  PANEL 2: TEMPERATURE + DEW POINT + RELATIVE HUMIDITY
    # ════════════════════════════════════════════════════════
    ax[TEMP].plot(times_mpl, sfc['temperature_2m'], color=c_temp,
                  linewidth=1.5, label='Temperature')
    if 'dew_point_2m' in sfc.columns:
        ax[TEMP].plot(times_mpl, sfc['dew_point_2m'], color=c_dew,
                      linewidth=1.5, linestyle='--', label='Dew Point')
    ax[TEMP].set_ylabel('Temperature / Dewpoint (°C)')
    ax[TEMP].grid(True, alpha=0.3)

    # RH on twin axis
    if 'relative_humidity_2m' in sfc.columns:
        ax_rh.plot(times_mpl, sfc['relative_humidity_2m'], color=c_rh,
                   linewidth=1, label='RH')
        ax_rh.set_ylabel('Relative Humidity (%)')
        ax_rh.set_ylim(0, 100)
        ax_rh.set_yticks(range(0, 100, 10))
        h2, l2 = ax_rh.get_legend_handles_labels()
        ax_rh.legend(h2, l2, loc='upper right', fontsize=7)

    # Temperature range
    t_max = sfc['temperature_2m'].max()
    t_min = sfc[['temperature_2m']].min().min()
    if 'dew_point_2m' in sfc.columns:
        t_min = min(t_min, sfc['dew_point_2m'].min())
    if pd.notna(t_min) and pd.notna(t_max) and t_min >= -5 and t_max <= 40:
        ax[TEMP].set_ylim(-5, 40)
        ax[TEMP].set_yticks(range(-5, 45, 5))

    ax[TEMP].legend(loc='upper left', fontsize=7)

    if is_day is not None:
        _night_shades(ax[TEMP], times_mpl, is_day)

    # ════════════════════════════════════════════════════════
    #  PANEL 3: FUEL MOISTURE + IGNITION PROBABILITY
    # ════════════════════════════════════════════════════════
    if 'fuel_moisture' in sfc.columns:
        fm_fos = pd.to_numeric(sfc['fuel_moisture'], errors='coerce')
        if fm_fos.notna().any():
            ax[FUEL].plot(times_mpl, fm_fos, color=c_fm_f,
                          linewidth=1.5, linestyle='--', label='FM Fosberg (1h)')

    if 'fuel_moisture_vpd' in sfc.columns:
        fm_vpd = pd.to_numeric(sfc['fuel_moisture_vpd'], errors='coerce')
        if fm_vpd.notna().any():
            ax[FUEL].plot(times_mpl, fm_vpd, color=c_fm_v,
                          linewidth=1.5, label='FM VPD (Resco)')

    ax[FUEL].set_ylabel('Fuel Moisture (%)')
    ax[FUEL].set_ylim(0, 35)
    ax[FUEL].set_yticks(range(0, 35, 5))
    ax[FUEL].grid(True, alpha=0.3)
    ax[FUEL].legend(loc='upper left', fontsize=7)

    # Ignition probability semaphore
    _plot_ignition_semaphore(ax[FUEL], sfc, times_mpl)

    if is_day is not None:
        _night_shades(ax[FUEL], times_mpl, is_day)

    # ── MINOR GRID ────────────────────────────────────────
    if n_days <= 4:
        for a in ax:
            a.xaxis.grid(True, which='minor', alpha=0.4, linestyle=':')

    # ── TITLE ─────────────────────────────────────────────
    loc = weather_data.location
    lat_dir = 'N' if loc.latitude >= 0 else 'S'
    lon_dir = 'E' if loc.longitude >= 0 else 'W'
    title = (f'Meteogram — {loc.display_name}  |  '
             f'{abs(loc.latitude):.2f}° {lat_dir}  {abs(loc.longitude):.2f}° {lon_dir}')
    fig.suptitle(title, fontsize=12, fontweight='bold')
    fig.tight_layout()
    fig.subplots_adjust(hspace=0.12)
    return fig


# ═══════════════════════════════════════════════════════════════════════════
#  PANEL 0: WIND PROFILE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _draw_wind_profile(ax, vrt: pd.DataFrame, times_mpl, is_day):
    """
    Draw time-height cross section with wind barbs, BLH, inversions, and C-Haines.
    """
    # Night shading
    if is_day is not None:
        ax.fill_between(
            times_mpl, min(LEVELS_DISPLAY) - 50, max(LEVELS_DISPLAY) + 50,
            where=(is_day == 0), alpha=0.08, color='lightblue', zorder=0)

    # Inversion shading
    layer_colors = {
        'inversion_700_600': ('royalblue', 0.2),
        'inversion_600_500': ('darkorange', 0.2),
        'inversion_500_250': ('darkorange', 0.15),
    }
    for col_name, (color, alpha) in layer_colors.items():
        if col_name in vrt.columns:
            inv_mask = vrt[col_name].fillna(False).values.astype(bool)
            parts = col_name.split('_')
            lower, upper = int(parts[1]), int(parts[2])
            ax.fill_between(times_mpl, lower, upper,
                            where=inv_mask, color=color, alpha=alpha, zorder=1,
                            label=f'Inv. {lower}-{upper}' if col_name == 'inversion_700_600' else None)

    # Wind barbs (thinned for readability)
    n_hours = len(vrt)
    thin = max(1, n_hours // 40)
    vrt_thin = vrt.iloc[::thin]
    times_thin = mdates.date2num(vrt_thin['time'])

    for level in LEVELS_DISPLAY:
        u_col = f'u_{level}'
        v_col = f'v_{level}'
        if u_col not in vrt_thin.columns or v_col not in vrt_thin.columns:
            continue
        u = pd.to_numeric(vrt_thin[u_col], errors='coerce').values
        v = pd.to_numeric(vrt_thin[v_col], errors='coerce').values
        valid = np.isfinite(u) & np.isfinite(v)
        if valid.any():
            p = np.full(valid.sum(), level, dtype=float)
            ax.barbs(times_thin[valid], p, u[valid], v[valid],
                     length=5, linewidth=0.5,
                     barb_increments=dict(half=5, full=10, flag=50),
                     zorder=5)

    # Boundary layer height
    if 'boundary_layer_height' in vrt.columns:
        blh_m = pd.to_numeric(vrt['boundary_layer_height'], errors='coerce').values
        # Convert meters AGL to pressure (approximate)
        blh_p = np.where(np.isfinite(blh_m),
                         1013.25 * np.exp(-blh_m / 8500.0), np.nan)
        valid_t = ~np.isnan(blh_p)
        if valid_t.any():
            ax.plot(times_mpl[valid_t], blh_p[valid_t],
                    color='red', linewidth=2, linestyle='--',
                    label='BLH', zorder=6)

    # Axes config
    ax.set_ylim(max(LEVELS_DISPLAY) + 20, min(LEVELS_DISPLAY) - 20)
    ax.set_yticks(LEVELS_DISPLAY)
    ax.set_yticklabels([str(l) for l in LEVELS_DISPLAY])
    ax.set_ylabel('Pressure (hPa)')
    ax.grid(True, alpha=0.3)

    # Right axis with geopotential height labels
    ax2 = ax.twinx()
    ax2.set_ylim(ax.get_ylim())
    ax2.set_yticks(LEVELS_DISPLAY)
    height_labels = []
    for level in LEVELS_DISPLAY:
        zcol = f'geopotential_height_{level}hPa'
        if zcol in vrt.columns:
            z = pd.to_numeric(vrt[zcol], errors='coerce').iloc[0]
            if np.isfinite(z):
                height_labels.append(f'{int(round(z))} m')
            else:
                height_labels.append('—')
        else:
            height_labels.append('—')
    ax2.set_yticklabels(height_labels)
    ax2.set_ylabel('Geopotential height (m ASL)')

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(loc='upper right', fontsize=7, ncol=2).set_zorder(10)

    # C-Haines bar
    if 'c_haines' in vrt.columns:
        _plot_haines_bar(ax, vrt, times_mpl)
    ax2.set_ylim(ax.get_ylim())


def _draw_wind_direction_fallback(ax, sfc, times_mpl, is_day):
    """Fallback: show wind direction arrows when no vertical data."""
    if is_day is not None:
        ax.fill_between(times_mpl, 0, 100,
                        where=(is_day == 0), alpha=0.35, color='lightblue', zorder=0)

    if 'wind_direction_10m' in sfc.columns:
        arrows = _wind_arrows(sfc['wind_direction_10m'])
        step = max(1, len(times_mpl) // 20)
        for i in range(0, len(times_mpl), step):
            if i < len(arrows) and pd.notna(arrows.iloc[i]):
                ax.text(times_mpl[i], 0.5, arrows.iloc[i],
                        fontsize=18, ha='center', va='center', color='k', zorder=5,
                        transform=ax.get_xaxis_transform())

    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_title('Wind Direction (surface)', fontsize=9, loc='left')
    ax.grid(True, axis='x', alpha=0.3)


def _plot_haines_bar(ax, vrt, times_mpl):
    """Coloured bar of C-Haines at the bottom of the vertical profile panel."""
    haines = vrt['c_haines'].values

    if len(haines) == 0 or np.all(np.isnan(haines)):
        return

    ylim = ax.get_ylim()
    bar_height = (ylim[1] - ylim[0]) * 0.06
    bar_bottom = ylim[0]

    cmap = mcolors.LinearSegmentedColormap.from_list(
        'haines', ['#2d9e2d', '#f0c929', '#e88a1a', '#d92525'], N=256)
    norm = mcolors.Normalize(vmin=0, vmax=9)

    for i in range(len(times_mpl) - 1):
        h = haines[i]
        if np.isnan(h):
            continue
        ax.axvspan(times_mpl[i], times_mpl[i + 1],
                   ymin=0, ymax=bar_height / (ylim[1] - ylim[0]),
                   color=cmap(norm(h)), alpha=0.8, zorder=4)

    ax.set_ylim(bar_bottom - bar_height * 1.0, ylim[1])
    ax.text(times_mpl[0], 0.03,
            'C-Haines', fontsize=7, va='center', ha='left',
            color='white', fontweight='bold', zorder=5,
            transform=ax.get_xaxis_transform())


# ═══════════════════════════════════════════════════════════════════════════
#  PANEL 3: IGNITION PROBABILITY SEMAPHORE
# ═══════════════════════════════════════════════════════════════════════════

def _plot_ignition_semaphore(ax_fuel, sfc, times_mpl):
    """Draw ignition probability semaphore bar at the bottom of fuel panel."""
    prob = sfc['prob_ignition'].values

    if all(p is None for p in prob):
        return

    ylim = ax_fuel.get_ylim()
    bar_height = (ylim[1] - ylim[0]) * 0.06
    bar_bottom = ylim[0]

    cmap = mcolors.LinearSegmentedColormap.from_list(
        'ignition', ['#2d9e2d', '#f0c929', '#e88a1a', '#d92525'], N=256)
    norm = mcolors.Normalize(vmin=0, vmax=100)

    for i in range(len(times_mpl) - 1):
        p = prob[i]
        if p is None or (isinstance(p, float) and np.isnan(p)):
            continue
        ax_fuel.axvspan(times_mpl[i], times_mpl[i + 1],
                        ymin=0, ymax=bar_height / (ylim[1] - ylim[0]),
                        color=cmap(norm(p)), alpha=0.8, zorder=4)

    ax_fuel.set_ylim(bar_bottom - bar_height * 0.5, ylim[1])
    ax_fuel.text(times_mpl[0], bar_bottom + bar_height * 0.3,
                 'Ignition Prob.', fontsize=7, va='center', ha='left',
                 color='white', fontweight='bold', zorder=5)


# ═══════════════════════════════════════════════════════════════════════════
#  WIND DIRECTION ARROWS
# ═══════════════════════════════════════════════════════════════════════════

def _wind_arrows(directions) -> pd.Series:
    """Convert wind direction (degrees) to arrow symbols."""
    return pd.cut(
        pd.Series(directions) % 360,
        bins=[-1, 23, 67, 112, 157, 202, 247, 292, 337, 361],
        labels=[
            r'$\downarrow$', r'$\swarrow$', r'$\leftarrow$',
            r'$\nwarrow$', r'$\uparrow$', r'$\nearrow$',
            r'$\rightarrow$', r'$\searrow$', r'$\downarrow$'
        ],
        right=False, ordered=False,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  EXPORT HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def meteogram_to_png(weather_data, **kwargs) -> bytes:
    """Generate meteogram and return as PNG bytes."""
    fig = generate_meteogram(weather_data, **kwargs)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def meteogram_to_base64(weather_data, **kwargs) -> str:
    """Generate meteogram and return as base64-encoded PNG."""
    import base64
    png_bytes = meteogram_to_png(weather_data, **kwargs)
    return base64.b64encode(png_bytes).decode('ascii')
