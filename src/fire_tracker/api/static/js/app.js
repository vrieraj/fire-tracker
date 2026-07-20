(() => {
  'use strict';

  let fires = [];
  let markers = {};
  let searchTimeout = null;
  let stationLayer = L.layerGroup();
  let lastClickLatLng = null;

  const $ = (s) => document.querySelector(s);
  const mapEl = $('#map');
  const sidebar = $('#sidebar');
  const fireList = $('#fire-list');
  const fireCount = $('#fire-count');
  const searchInput = $('#search-input');
  const searchResults = $('#search-results');
  const btnRefresh = $('#btn-refresh');
  const btnOpen = $('#btn-open-sidebar');
  const btnClose = $('#btn-close-sidebar');
  const modal = $('#meteogram-modal');
  const modalImg = $('#meteogram-img');
  const btnCloseModal = $('#btn-close-modal');

  // ── Map layers ─────────────────────────────────────
  const EUMETSAT_WMS = 'https://view.eumetsat.int/geoserver/mtg_fd';

  const layers = {
    // Base layers
    street: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap',
      maxZoom: 19,
    }),
    googleStreets: L.tileLayer('https://mt{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
      attribution: '&copy; Google',
      maxZoom: 20,
      subdomains: '0123',
    }),
    satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
      attribution: '&copy; Esri',
      maxZoom: 18,
    }),
    googleSatellite: L.tileLayer('https://mt{s}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}', {
      attribution: '&copy; Google',
      maxZoom: 20,
      subdomains: '0123',
    }),
    hybrid: L.layerGroup([
      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: '&copy; Esri',
        maxZoom: 18,
      }),
      L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
        maxZoom: 18,
      }),
    ]),
    googleHybrid: L.tileLayer('https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', {
      attribution: '&copy; Google',
      maxZoom: 20,
      subdomains: '0123',
    }),
    // EUMETSAT
    fireTemp: L.tileLayer.wms(EUMETSAT_WMS + '/rgb_firetemperature/ows', {
      layers: 'rgb_firetemperature',
      format: 'image/png',
      transparent: true,
      version: '1.3.0',
      crs: L.CRS.EPSG3857,
      attribution: '&copy; EUMETSAT',
    }),
    geoColour: L.tileLayer.wms(EUMETSAT_WMS + '/rgb_geocolour/ows', {
      layers: 'rgb_geocolour',
      format: 'image/png',
      transparent: true,
      version: '1.3.0',
      crs: L.CRS.EPSG3857,
      attribution: '&copy; EUMETSAT',
    }),
  };

  const map = L.map(mapEl, { zoomControl: true, layers: [layers.hybrid] }).setView([40.0, -4.0], 6);

  // FRP layer (loaded dynamically)
  let frpLayer = L.geoJSON(null, {
    pointToLayer: (feature, latlng) => {
      const p = feature.properties;
      return L.circleMarker(latlng, {
        radius: p.radius || 6,
        fillColor: p.color || '#ffaa00',
        color: p.color || '#ffaa00',
        weight: 0,
        fillOpacity: 0.5,
      });
    },
    onEachFeature: (feature, layer) => {
      const p = feature.properties;
      // Friendly datetime: "17 jul 2026, 14:30 h UTC"
      const months = ['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'];
      let dateStr = p.acquisition_time;
      try {
        const dt = new Date(p.acquisition_time);
        const day = dt.getUTCDate();
        const mon = months[dt.getUTCMonth()];
        const year = dt.getUTCFullYear();
        const hh = String(dt.getUTCHours()).padStart(2, '0');
        const mm = String(dt.getUTCMinutes()).padStart(2, '0');
        dateStr = `${day} ${mon} ${year}, ${hh}:${mm} h UTC`;
      } catch(e) {}

      layer.bindPopup(`
        <div style="font-family:sans-serif;min-width:150px">
          <strong style="color:${p.color}">🔥 FRP Detection</strong>
          <hr style="margin:4px 0;border-color:#444">
          <div style="font-size:0.85rem">
            <b>${dateStr}</b><br>
            <b>Confianza:</b> ${p.confidence}%<br>
            <b>FRP:</b> ${p.frp_mw} ± ${p.frp_uncertainty} MW
          </div>
        </div>
      `);
    },
  });

  // Build layer control
  const baseLayers = {
    'OSM Callejero': layers.street,
    'Google Callejero': layers.googleStreets,
    'Esri Satélite': layers.satellite,
    'Google Satélite': layers.googleSatellite,
    'Esri Híbrido': layers.hybrid,
    'Google Híbrido': layers.googleHybrid,
    '🔥 Fire Temperature': layers.fireTemp,
    '🛰️ GeoColour': layers.geoColour,
  };

  const overlayLayers = {
    '🔥 FRP (LSA SAF)': frpLayer,
    '📡 Estaciones': stationLayer,
  };

  const layerControl = L.control.layers(baseLayers, overlayLayers, { position: 'topright' }).addTo(map);

  // Add separator and EUMETView links after layer control is in DOM
  requestAnimationFrame(() => {
    const controlContainer = layerControl.getContainer();
    const allLabels = controlContainer.querySelectorAll('.leaflet-control-layers label');

    // Find labels by their span text content
    function findLabelByText(searchText) {
      for (const label of allLabels) {
        const span = label.querySelector('span');
        if (span && span.textContent.includes(searchText)) return label;
      }
      return null;
    }

    // Insert separator before EUMETSAT base layers
    const fireTempLabel = findLabelByText('Fire Temperature');
    if (fireTempLabel) {
      const sep = document.createElement('div');
      sep.style.borderTop = '1px solid #888';
      sep.style.marginTop = '4px';
      fireTempLabel.parentNode.insertBefore(sep, fireTempLabel);
    }

    // Add EUMETView links to EUMETSAT layers
    const eumetsatLinks = {
      'Fire Temperature': 'https://view.eumetsat.int/productviewer?v=mtg_fd:rgb_firetemperature',
      'GeoColour': 'https://view.eumetsat.int/productviewer?v=mtg_fd:rgb_geocolour',
    };
    for (const [text, url] of Object.entries(eumetsatLinks)) {
      const label = findLabelByText(text);
      if (label) {
        const link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.textContent = 'EUMETView';
        link.className = 'eumetsat-view-link';
        link.onclick = (e) => e.stopPropagation();
        label.appendChild(link);
      }
    }

    // Add LSA SAF link to FRP overlay
    const frpLabel = findLabelByText('FRP');
    if (frpLabel) {
      const link = document.createElement('a');
      link.href = 'https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/';
      link.target = '_blank';
      link.textContent = 'LSA SAF';
      link.className = 'eumetsat-view-link';
      link.onclick = (e) => e.stopPropagation();
      frpLabel.appendChild(link);
    }
  });

  // ── Fire status colors ─────────────────────────────
  const STATUS_COLORS = {
    active: '#e74c3c',
    declarado: '#e74c3c',
    controlled: '#f39c12',
    stabilized: '#f1c40f',
    extinguished: '#95a5a6',
    false_alarm: '#7f8c8d',
    unknown: '#9b59b6',
  };

  const STATUS_LABELS = {
    active: 'Activo',
    declarado: 'Declarado',
    controlled: 'Controlado',
    stabilized: 'Estabilizado',
    extinguished: 'Extinguido',
    false_alarm: 'Falsa alarma',
    unknown: 'Desconocido',
  };

  function statusColor(status) {
    return STATUS_COLORS[status] || STATUS_COLORS.unknown;
  }

  function statusLabel(status) {
    return STATUS_LABELS[status] || status || 'Desconocido';
  }

  // ── Sunrise/sunset via Open-Meteo API ──────────────
  async function getSunTimes(lat, lon) {
    try {
      const today = new Date().toISOString().split('T')[0];
      const res = await fetch(
        `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&daily=sunrise,sunset&timezone=auto&start_date=${today}&end_date=${today}`
      );
      const data = await res.json();
      if (data.daily?.sunrise?.[0] && data.daily?.sunset?.[0]) {
        const sr = data.daily.sunrise[0];
        const ss = data.daily.sunset[0];
        const fmt = (iso) => {
          const d = new Date(iso);
          return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        };
        return { sunrise: fmt(sr), sunset: fmt(ss) };
      }
    } catch (e) {
      console.error('Sun times error:', e);
    }
    return null;
  }

  // ── Load fires ─────────────────────────────────────
  async function loadFires() {
    try {
      const res = await fetch('/api/fires/tracked');
      const geojson = await res.json();
      fires = geojson.features || [];
      renderFires();
    } catch (e) {
      console.error('Error loading fires:', e);
    }
  }

  // ── Render ─────────────────────────────────────────
  async function renderFires() {
    const query = searchInput.value.toLowerCase().trim();

    const filtered = fires.filter(f => {
      const p = f.properties;
      if (query) {
        const text = [p.municipality, p.province, p.region, p.source_label, p.external_id]
          .filter(Boolean).join(' ').toLowerCase();
        if (!text.includes(query)) return false;
      }
      return true;
    });

    // Sort: most recent first
    filtered.sort((a, b) => {
      const da = a.properties.detection_date || '';
      const db = b.properties.detection_date || '';
      return db.localeCompare(da);
    });

    fireCount.textContent = `${filtered.length} incendio${filtered.length !== 1 ? 's' : ''} activo${filtered.length !== 1 ? 's' : ''}`;

    Object.values(markers).forEach(m => map.removeLayer(m));
    markers = {};

    // Preload sun times for all fires
    const sunCache = {};
    for (const f of filtered) {
      const [lon, lat] = f.geometry.coordinates;
      const key = `${lat.toFixed(3)},${lon.toFixed(3)}`;
      if (!sunCache[key]) {
        sunCache[key] = getSunTimes(lat, lon);
      }
    }
    // Resolve all sun time promises
    for (const key of Object.keys(sunCache)) {
      sunCache[key] = await sunCache[key];
    }

    for (const f of filtered) {
      const [lon, lat] = f.geometry.coordinates;
      const p = f.properties;
      const color = statusColor(p.status);

      const marker = L.circleMarker([lat, lon], {
        radius: 7,
        fillColor: color,
        color: '#fff',
        weight: 1.5,
        opacity: 0.9,
        fillOpacity: 0.8,
      }).addTo(map);

      const areaText = p.area_ha ? `${p.area_ha} ha` : 'N/D';
      const dateText = p.detection_date || '';
      const statusLbl = statusLabel(p.status);
      const sourceUrl = p.source_url || '#';
      const sunKey = `${lat.toFixed(3)},${lon.toFixed(3)}`;
      const sun = sunCache[sunKey];

      marker.bindPopup(`
        <div style="font-family:sans-serif;min-width:200px">
          <strong>${p.municipality || p.external_id}</strong><br>
          <small>${p.province ? p.province + ', ' : ''}${p.region || ''}</small>
          <div style="font-size:0.78rem;color:#666;margin:2px 0">${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(4)}° ${lon >= 0 ? 'E' : 'W'}</div>
          <hr style="margin:4px 0;border-color:#444">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
            <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${color}"></span>
            <span>Estado: <b>${statusLbl}</b></span>
          </div>
          <span>Area: <b>${areaText}</b></span><br>
          ${dateText ? `<span>Deteccion: ${dateText}</span><br>` : ''}
          <span>Fuente: <a href="${sourceUrl}" target="_blank" style="color:#ef6c35">${p.source_label}</a></span>
          ${sun ? `<br><span style="font-size:0.78rem;color:#888">☀ ${sun.sunrise} · ☽ ${sun.sunset}</span>` : ''}
          <hr style="margin:4px 0;border-color:#444">
          <div style="display:flex;flex-direction:column;gap:4px">
            <button onclick="window.openMeteogram(${lat},${lon},'${(p.municipality||'').replace(/'/g, "\\'")}')" style="background:#ef6c35;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Meteograma</button>
            <button onclick="window.searchStations(${lat},${lon})" style="background:#3498db;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Buscar estaciones</button>
          </div>
          <hr style="margin:4px 0;border-color:#444">
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <a href="https://www.windy.com/${lat}/${lon}" target="_blank" style="color:#3498db;font-size:0.78rem;text-decoration:none">Windy ↗</a>
            <a href="https://www.meteoblue.com/en/weather/week/${lat},${lon}" target="_blank" style="color:#3498db;font-size:0.78rem;text-decoration:none">Meteoblue ↗</a>
            <a href="https://www.google.com/maps/@${lat},${lon},12z" target="_blank" style="color:#3498db;font-size:0.78rem;text-decoration:none">Google Maps ↗</a>
          </div>
        </div>
      `);

      marker.fireId = p.id;
      markers[p.id] = marker;
    }

    // Ensure fire markers are on top of all layers
    Object.values(markers).forEach(m => m.bringToFront());

    // Fire list (sorted already)
    fireList.innerHTML = '';
    filtered.forEach(f => {
      const p = f.properties;
      const li = document.createElement('li');
      li.className = 'fire-item';
      const color = statusColor(p.status);
      const statusLbl = statusLabel(p.status);
      const areaText = p.area_ha ? `<span class="fire-area">${p.area_ha} ha</span>` : '';
      const dateText = p.detection_date ? `<span class="fire-date">${p.detection_date.split('T')[0]}</span>` : '';
      li.innerHTML = `
        <div class="fire-name">${p.municipality || p.external_id}</div>
        <div class="fire-meta">${p.province ? p.province + ', ' : ''}${p.region || ''}</div>
        <div class="fire-tags">
          <span class="fire-status" style="background:${color}">${statusLbl}</span>
          <span class="fire-source">${p.source_label}</span>${areaText}${dateText}
        </div>
      `;
      li.addEventListener('click', () => {
        const [lon, lat] = f.geometry.coordinates;
        map.setView([lat, lon], 12);
        markers[p.id]?.openPopup();
        document.querySelectorAll('.fire-item').forEach(el => el.classList.remove('active'));
        li.classList.add('active');
      });
      fireList.appendChild(li);
    });
  }

  // ── Meteogram modal ────────────────────────────────
  window.openMeteogram = (lat, lon, name) => {
    map.closePopup();
    const url = `/api/meteogram.png?lat=${lat}&lon=${lon}&name=${encodeURIComponent(name)}&forecast_days=2&past_days=0&width=14&height=10`;
    modalImg.src = url;
    modal.classList.remove('hidden');
  };

  btnCloseModal.addEventListener('click', () => modal.classList.add('hidden'));
  modal.addEventListener('click', (e) => {
    if (e.target === modal) modal.classList.add('hidden');
  });

  // ── Map click → Nominatim reverse + info popup ─────
  map.on('click', (e) => {
    if (e.originalEvent.target.closest('.leaflet-marker-icon') ||
        e.originalEvent.target.closest('.leaflet-interactive')) {
      return;
    }
    lastClickLatLng = e.latlng;
    const { lat, lng } = e.latlng;

    // Show loading popup immediately
    const loadingPopup = L.popup()
      .setLatLng([lat, lng])
      .setContent(`
        <div style="font-family:sans-serif;min-width:160px;text-align:center;padding:8px">
          <div style="font-size:0.8rem;color:#666">
            ${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? 'N' : 'S'},
            ${Math.abs(lng).toFixed(4)}° ${lng >= 0 ? 'E' : 'W'}
          </div>
          <div style="margin-top:4px;font-size:0.8rem;color:#888">Buscando ubicacion...</div>
        </div>
      `)
      .openOn(map);

    // Reverse geocode with Nominatim
    reverseGeocode(lat, lng).then(info => {
      showPointPopup(lat, lng, info);
    });
  });

  async function reverseGeocode(lat, lon) {
    try {
      const res = await fetch(
        `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lon}&format=json&accept-language=es`,
        { headers: { 'User-Agent': 'FireTracker/1.0' } }
      );
      if (!res.ok) return null;
      const data = await res.json();
      const addr = data.address || {};
      return {
        name: data.name || addr.city || addr.town || addr.village || addr.hamlet || addr.municipality || '',
        municipality: addr.city || addr.town || addr.village || addr.hamlet || addr.municipality || '',
        province: addr.state || addr.county || '',
        region: addr.autonomous_community || addr.region || '',
        country: addr.country || '',
        countryCode: addr.country_code || '',
        fullAddress: data.display_name || '',
      };
    } catch {
      return null;
    }
  }

  async function showPointPopup(lat, lon, info) {
    const coordText = `${Math.abs(lat).toFixed(4)}° ${lat >= 0 ? 'N' : 'S'}, ${Math.abs(lon).toFixed(4)}° ${lon >= 0 ? 'E' : 'W'}`;
    const locName = info?.name || info?.municipality || '';
    const locLine = [info?.municipality, info?.province, info?.region, info?.country].filter(Boolean).join(', ');
    const sun = await getSunTimes(lat, lon);

    const popup = L.popup()
      .setLatLng([lat, lon])
      .setContent(`
        <div style="font-family:sans-serif;min-width:200px">
          ${locName ? `<strong>${locName}</strong><br>` : ''}
          ${locLine ? `<small style="color:#888">${locLine}</small><br>` : ''}
          <div style="font-size:0.78rem;color:#666;margin:2px 0">${coordText}</div>
          ${sun ? `<div style="font-size:0.78rem;color:#888;margin:2px 0">☀ ${sun.sunrise} · ☽ ${sun.sunset}</div>` : ''}
          <hr style="margin:4px 0;border-color:#444">
          <div style="display:flex;flex-direction:column;gap:4px">
            <button onclick="window.openMeteogram(${lat},${lon},'${(locName || '').replace(/'/g, "\\'")}')" style="background:#ef6c35;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Meteograma</button>
            <button onclick="window.searchStations(${lat},${lon})" style="background:#3498db;border:none;color:#fff;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:0.8rem;width:100%">Buscar estaciones</button>
          </div>
          <hr style="margin:4px 0;border-color:#444">
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <a href="https://www.windy.com/${lat}/${lon}" target="_blank" style="color:#3498db;font-size:0.78rem;text-decoration:none">Windy ↗</a>
            <a href="https://www.meteoblue.com/en/weather/week/${lat},${lon}" target="_blank" style="color:#3498db;font-size:0.78rem;text-decoration:none">Meteoblue ↗</a>
            <a href="https://www.google.com/maps/@${lat},${lon},12z" target="_blank" style="color:#3498db;font-size:0.78rem;text-decoration:none">Google Maps ↗</a>
          </div>
        </div>
      `)
      .openOn(map);
  }

  // ── Station search & display ───────────────────────
  window.searchStations = async (lat, lon) => {
    map.closePopup();
    stationLayer.clearLayers();
    if (!map.hasLayer(stationLayer)) {
      stationLayer.addTo(map);
    }

    const loadingPopup = L.popup()
      .setLatLng([lat, lon])
      .setContent('<div style="font-family:sans-serif;text-align:center;padding:8px">Buscando estaciones...</div>')
      .openOn(map);

    try {
      // Load both WU PWS and METAR stations
      const [wuRes, metarRes] = await Promise.allSettled([
        fetch(`/api/stations?lat=${lat}&lon=${lon}&radius_km=30`).then(r => r.json()),
        fetch('/api/metar').then(r => r.json()),
      ]);

      loadingPopup.close();

      let totalCount = 0;

      // Add WU PWS stations
      if (wuRes.status === 'fulfilled' && wuRes.value.stations) {
        wuRes.value.stations.forEach(s => addStationMarker(s));
        totalCount += wuRes.value.stations.length;
      }

      // Add METAR stations
      if (metarRes.status === 'fulfilled' && metarRes.value.stations) {
        metarRes.value.stations.forEach(s => addStationMarker(s));
        totalCount += metarRes.value.stations.length;
      }

      if (totalCount === 0) {
        L.popup()
          .setLatLng([lat, lon])
          .setContent('<div style="font-family:sans-serif;text-align:center;padding:8px">No se encontraron estaciones cercanas</div>')
          .openOn(map);
      }
    } catch (e) {
      console.error('Station search error:', e);
      loadingPopup.close();
    }
  };

  // ── METAR stations (aviationweather.gov) ───────────
  async function loadMetarStations() {
    try {
      const res = await fetch('/api/metar');
      const data = await res.json();
      if (data.stations) {
        data.stations.forEach(s => addStationMarker(s));
        console.log(`METAR: ${data.stations.length} stations loaded`);
      }
    } catch (e) {
      console.error('METAR load error:', e);
    }
  }

  function kmhToBeaufort(kmh) {
    if (kmh < 1) return 0;
    if (kmh <= 5) return 1;
    if (kmh <= 11) return 2;
    if (kmh <= 19) return 3;
    if (kmh <= 28) return 4;
    if (kmh <= 38) return 5;
    if (kmh <= 49) return 6;
    if (kmh <= 61) return 7;
    if (kmh <= 74) return 8;
    if (kmh <= 88) return 9;
    if (kmh <= 102) return 10;
    if (kmh <= 117) return 11;
    return 12;
  }

  function addStationMarker(station) {
    const { lat, lon } = station;
    const temp = station.temp_c;
    const humidity = station.humidity_pct;
    const windSpeed = station.windspeed_kmh;
    const windDir = station.winddir_avg;

    const windArrow = windDir != null ? getWindArrow(windDir) : '';
    const beaufort = windSpeed != null ? kmhToBeaufort(windSpeed) : null;
    const windText = windSpeed != null ? `${windArrow} ${beaufort} (${windSpeed})` : '';
    const tempText = temp != null ? `${temp}°` : '';
    const rhText = humidity != null ? `${humidity}%` : '';
    const tempRhText = [tempText, rhText].filter(Boolean).join(' · ');

    const icon = L.divIcon({
      className: 'station-marker',
      html: `
        <div class="station-label">
          <div class="station-wind">${windText}</div>
          <div class="station-temp">${tempRhText}</div>
        </div>
      `,
      iconSize: [60, 36],
      iconAnchor: [30, 18],
    });

    const marker = L.marker([lat, lon], { icon }).addTo(stationLayer);

    const isMetar = station.platform === 'METAR';
    const sourceUrl = isMetar
      ? `https://metar-taf.com/es/${station.stationId}`
      : `https://www.wunderground.com/dashboard/pws/${station.stationId}`;
    const sourceLabel = isMetar ? 'METAR-TAF ↗' : 'Weather Underground ↗';
    const elevText = station.elev_m != null ? `${station.elev_m} m` : 'N/D';
    const gustText = station.windgust_kmh != null ? `Rafagas: ${station.windgust_kmh} km/h` : '';
    const rainText = station.rain_daily_mm != null ? `Lluvia: ${station.rain_daily_mm} mm` : '';
    const fltCat = station.fltCat ? `<span style="display:inline-block;padding:1px 5px;border-radius:3px;font-size:0.7rem;font-weight:600;color:#fff;background:${
      station.fltCat === 'VFR' ? '#27ae60' : station.fltCat === 'MVFR' ? '#3498db' : station.fltCat === 'IFR' ? '#e74c3c' : '#8e44ad'
    }">${station.fltCat}</span> ` : '';
    const metarTime = station.metar_time ? `<div style="font-size:0.72rem;color:#888;margin-top:2px">Obs: ${new Date(station.metar_time).toLocaleTimeString('es-ES', {hour:'2-digit', minute:'2-digit'})} UTC</div>` : '';

    marker.bindPopup(`
      <div style="font-family:sans-serif;min-width:180px">
        <strong>${station.name || station.stationId}</strong><br>
        <small style="color:#888">${station.adm1 || ''} ${station.country_name || station.country || ''} · ${station.stationId}</small>
        ${metarTime}
        <hr style="margin:4px 0;border-color:#444">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px;font-size:0.85rem">
          <span>Temp:</span><b>${temp != null ? temp + ' °C' : 'N/D'}</b>
          <span>HR:</span><b>${humidity != null ? humidity + ' %' : 'N/D'}</b>
          <span>Viento:</span><b>${windSpeed != null ? beaufort + ' (' + windSpeed + ') km/h' : 'N/D'}</b>
          <span>Direccion:</span><b>${windDir != null ? windDir + '°' : 'N/D'}</b>
          ${gustText ? `<span></span><span>${gustText}</span>` : ''}
          ${station.pressure_hpa ? `<span>Presion:</span><b>${station.pressure_hpa} hPa</b>` : ''}
          ${station.visibility ? `<span>Visibilidad:</span><b>${station.visibility} km</b>` : ''}
        </div>
        <hr style="margin:4px 0;border-color:#444">
        <div style="font-size:0.78rem;color:#888">
          ${fltCat}Altitud: ${elevText}${station.distance_km ? ` · ${station.distance_km} km` : ''}
        </div>
        <hr style="margin:4px 0;border-color:#444">
        <a href="${sourceUrl}" target="_blank" style="color:#3498db;font-size:0.8rem;text-decoration:none">${sourceLabel}</a>
      </div>
    `);
  }

  function getWindArrow(degrees) {
    const arrows = ['↓', '↙', '←', '↖', '↑', '↗', '→', '↘'];
    const idx = Math.round(degrees / 45) % 8;
    return `<span class="wind-arrow" style="display:inline-block;transform:rotate(0deg)">${arrows[idx]}</span>`;
  }

  // ── Geocode search ─────────────────────────────────
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimeout);
    const q = searchInput.value.trim();
    if (q.length < 2) {
      searchResults.classList.remove('active');
      return;
    }
    searchTimeout = setTimeout(async () => {
      try {
        const res = await fetch(`/api/geocode?q=${encodeURIComponent(q)}&limit=5`);
        const data = await res.json();
        if (!data.results?.length) {
          searchResults.classList.remove('active');
          return;
        }
        searchResults.innerHTML = '';
        data.results.forEach(loc => {
          const li = document.createElement('li');
          li.textContent = loc.display_name || loc.name;
          li.addEventListener('click', () => {
            map.setView([loc.latitude, loc.longitude], 11);
            searchResults.classList.remove('active');
            searchInput.value = loc.name;

            const locInfo = {
              name: loc.name,
              municipality: loc.name,
              province: loc.admin1,
              region: '',
              country: loc.country,
            };
            showPointPopup(loc.latitude, loc.longitude, locInfo);
          });
          searchResults.appendChild(li);
        });
        searchResults.classList.add('active');
      } catch (e) {
        console.error('Geocode error:', e);
      }
    }, 300);
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('#search-box')) {
      searchResults.classList.remove('active');
    }
  });

  // ── Sidebar toggle (mobile) ────────────────────────
  btnOpen.addEventListener('click', () => sidebar.classList.add('open'));
  btnClose.addEventListener('click', () => sidebar.classList.remove('open'));

  // ── Refresh ────────────────────────────────────────
  btnRefresh.addEventListener('click', async () => {
    btnRefresh.disabled = true;
    btnRefresh.textContent = 'Actualizando...';
    try {
      await fetch('/api/fires/refresh', { method: 'POST' });
      await loadFires();
    } catch (e) {
      console.error('Refresh error:', e);
    }
    btnRefresh.disabled = false;
    btnRefresh.textContent = 'Actualizar datos';
  });

  // ── FRP ────────────────────────────────────────────
  async function loadFRP() {
    try {
      const res = await fetch('/api/frp');
      const geojson = await res.json();
      frpLayer.clearLayers();
      if (geojson.features?.length) {
        frpLayer.addData(geojson);
        console.log(`FRP: ${geojson.features.length} fire detections loaded`);
      }
    } catch (e) {
      console.error('FRP load error:', e);
    }
  }

  // ── Init ───────────────────────────────────────────
  loadFires();
  loadFRP();
})();
