// Trackside Pulse - Application Orchestrator & Polling Engine
import { state } from './state.js';
import { haptics } from './haptics.js';

const STALE_THRESHOLD_SECONDS = 90;
const POLLING_INTERVAL_MS = 6000;

// DOM Elements
const stalenessBanner = document.getElementById('staleness-banner');
const stalenessText = document.getElementById('staleness-text');
const liveDot = document.getElementById('live-dot');
const syncTimeEl = document.getElementById('sync-time');
const sessionBadgeEl = document.getElementById('session-badge');
const sessionLapsEl = document.getElementById('session-laps');
const flagBadgeEl = document.getElementById('flag-badge');
const circuitBadgeEl = document.getElementById('circuit-badge');
const gridDeltaHeader = document.getElementById('grid-delta-header');

// Hero Card Elements
const heroCardEl = document.getElementById('hero-card');
const heroDriverCode = document.getElementById('hero-driver-code');
const heroDriverName = document.getElementById('hero-driver-name');
const heroDriverPos = document.getElementById('hero-driver-pos');
const heroFiaBadgeEl = document.getElementById('hero-fia-badge');
const heroDeltaVal = document.getElementById('hero-delta-val');
const heroTyreVal = document.getElementById('hero-tyre-val');
const heroTrendVal = document.getElementById('hero-trend-val');
const metric1Label = document.getElementById('metric-1-label');
const metric2Label = document.getElementById('metric-2-label');
const metric3Label = document.getElementById('metric-3-label');
const heroPitPred = document.getElementById('hero-pit-pred');
const heroAccentBar = document.getElementById('hero-accent-bar');
const driverModal = document.getElementById('driver-modal');
const modalList = document.getElementById('modal-driver-list');
const openModalBtn = document.getElementById('open-driver-modal');
const closeModalBtn = document.getElementById('close-driver-modal');

// Timing Grid
const timingGridBody = document.getElementById('timing-grid-body');
const viewTop11Btn = document.getElementById('view-top11-btn');
const viewBottom11Btn = document.getElementById('view-bottom11-btn');
const viewAllBtn = document.getElementById('view-all-btn');
let hasUserManuallyToggledSegment = false;
// Ticker
const tickerListEl = document.getElementById('ticker-list');

let previousFlag = null;
let previousInPit = new Set();
let lastTrackedLap = null;
let previousLapPositions = {};
let currentLapPositions = {};

function normalizeStr(str) {
  return (str || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '');
}

function matchCircuit(rawCircuit, sdCircuits) {
  if (!rawCircuit || !sdCircuits) return null;
  const target = normalizeStr(rawCircuit);
  if (!target) return null;

  for (const [key, meta] of Object.entries(sdCircuits)) {
    const normKey = normalizeStr(key);
    if (target === normKey || target.includes(normKey) || normKey.includes(target)) {
      return meta;
    }
  }

  for (const meta of Object.values(sdCircuits)) {
    const candidates = [
      meta.short_name,
      meta.name,
      meta.gp_name,
      meta.country
    ];
    for (const c of candidates) {
      if (!c) continue;
      const normC = normalizeStr(c);
      if (normC && (target.includes(normC) || normC.includes(target))) {
        return meta;
      }
    }
  }

  return null;
}

// 1. Initial Load: Load season_data.json
async function loadSeasonData() {
  try {
    const res = await fetch('/static/data/season_data.json');
    if (!res.ok) throw new Error('Failed to load season data');
    const data = await res.json();
    state.setSeasonData(data);
    buildDriverModal(data);
  } catch (err) {
    console.error('Season data fetch error:', err);
  }
}

// 2. Build Driver Selector Modal
function buildDriverModal(seasonData) {
  modalList.innerHTML = '';
  if (!seasonData?.drivers) return;

  const sortedCodes = Object.keys(seasonData.drivers).sort();
  for (const code of sortedCodes) {
    const info = seasonData.drivers[code];
    const btn = document.createElement('button');
    btn.className = 'driver-pick-btn';
    if (code === state.favoriteDriver) btn.classList.add('selected');
    
    btn.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <span style="font-weight:900; font-size:16px;">${code}</span>
        <span style="font-size:11px; color:#a3a3a3;">#${info.num}</span>
      </div>
      <div style="font-size:12px; color:#fff;">${info.name}</div>
      <div style="font-size:10px; color:${info.color}; font-weight:700;">${info.team}</div>
    `;

    btn.addEventListener('click', () => {
      state.setFavoriteDriver(code);
      if (state.gridSegmentMode !== 'all' && state.raceState?.drivers) {
        const dRow = state.raceState.drivers.find(d => d[0] === code);
        if (dRow) {
          const p = dRow[1] || 1;
          state.setGridSegmentMode(p <= 11 ? 'top11' : 'bottom11');
        }
      }
      driverModal.classList.remove('open');
    });

    modalList.appendChild(btn);
  }
}

// 3. Polling Engine with ETag / 304 handling
async function fetchRaceState() {
  const headers = {};
  if (state.lastEtag) {
    headers['If-None-Match'] = state.lastEtag;
  }

  try {
    const res = await fetch('/api/live', { headers });

    // X-TS viaja también en los 304: el ETag excluye "ts", así que sin esto
    // un estado sin cambios parecería "desfasado" aunque el servidor esté vivo.
    const headerTs = parseInt(res.headers.get('X-TS'), 10);
    if (headerTs) state.lastTs = headerTs;
    state.setOffline(false);

    if (res.status === 304) {
      // 304 Not Modified: 0 bytes transferred!
      checkStaleness();
      return;
    }

    if (res.ok) {
      const etag = res.headers.get('ETag');
      const data = await res.json();
      if (!headerTs && data.ts) state.lastTs = data.ts;
      
      // Check for flag alert triggers
      if (data.flag && data.flag !== previousFlag) {
        if (data.flag === 'YELLOW' || data.flag === 'SC' || data.flag === 'VSC') {
          haptics.notify('flag_yellow');
        } else if (data.flag === 'RED') {
          haptics.notify('flag_red');
        }
        previousFlag = data.flag;
      }

      // Sincronizar metadatos de pilotos si el servidor los envía dinámicamente
      if (data.grid_meta && Object.keys(data.grid_meta).length > 0) {
        const currentSd = state.seasonData || { drivers: {} };
        let metaChanged = false;
        for (const [code, info] of Object.entries(data.grid_meta)) {
          if (!currentSd.drivers[code] || currentSd.drivers[code].name !== info.name) {
            currentSd.drivers[code] = info;
            metaChanged = true;
          }
        }
        if (metaChanged) {
          state.setSeasonData(currentSd);
          buildDriverModal(currentSd);
        }
      }

      state.updateRaceState(data, etag);
      checkStaleness();
    } else {
      console.warn('API response status:', res.status);
    }
  } catch (err) {
    console.warn('Network error or offline during fetch:', err);
    state.setOffline(!navigator.onLine);
    checkStaleness();
  }
}

// 4. Staleness Detector (> 90 seconds)
function checkStaleness() {
  const lastTs = state.lastTs || state.raceState?.ts;
  if (!lastTs) {
    return;
  }
  const nowSec = Math.floor(Date.now() / 1000);
  const diff = nowSec - lastTs;

  if (diff > STALE_THRESHOLD_SECONDS) {
    state.markStale(true);
    stalenessBanner.classList.add('visible');
    stalenessText.textContent = `⚠️ DATOS DESFASADOS - VUELTA CONGELADA (Última señal hace ${diff}s)`;
    liveDot.className = 'brand-dot stale';
    if (syncTimeEl) syncTimeEl.textContent = `Desfasado (+${diff}s)`;
  } else {
    state.markStale(false);
    stalenessBanner.classList.remove('visible');
    liveDot.className = state.isOffline ? 'brand-dot offline' : 'brand-dot';
    if (syncTimeEl) {
      if (diff <= 3) syncTimeEl.textContent = 'En vivo';
      else syncTimeEl.textContent = `Hace ${diff}s`;
    }
  }
}

// 5. Pit Exit Calculation (Gap Accumulation)
function calculatePitExit(favDriverCode, drivers, pitLossSec) {
  if (!drivers || drivers.length === 0) return null;
  const favIndex = drivers.findIndex(d => d[0] === favDriverCode);
  if (favIndex === -1) return null;

  const favDriver = drivers[favIndex];
  const favPos = favDriver[1];

  // If already leading by a huge gap or last
  // Parse interval deltas to calculate cumulative gaps
  let cumulativeGapBehind = 0;
  let rejoiningBehindDriver = null;
  let projectedPos = favPos;

  // Drivers behind the favorite
  for (let i = favIndex + 1; i < drivers.length; i++) {
    const d = drivers[i];
    // Delta string: "+1.840" or "1 LAP"
    const deltaStr = d[2] || '+0.000';
    let deltaSec = 0;
    if (/LAP/i.test(deltaStr)) {
      deltaSec = 999; // doblado: fuera de cualquier ventana de pit
    } else if (deltaStr.includes('+')) {
      deltaSec = parseFloat(deltaStr.replace('+', '')) || 0;
    }
    cumulativeGapBehind += deltaSec;

    if (cumulativeGapBehind < pitLossSec) {
      projectedPos = d[1];
      rejoiningBehindDriver = d[0];
    } else {
      // We found the gap where pit loss is absorbed
      break;
    }
  }

  if (projectedPos === favPos) {
    return { pos: favPos, behind: null, retained: true };
  }
  return { pos: projectedPos, behind: rejoiningBehindDriver, retained: false };
}

// 6. UI Render Engine
function renderUI() {
  const rs = state.raceState;
  const sd = state.seasonData;

  if (!rs) return;

  // Top Session Status & Detection
  const sess = (rs.sess || 'R').toUpperCase();
  const isPractice = sess.startsWith('P') || sess.includes('FP');
  const isQualy = sess.startsWith('Q');
  const isRace = sess === 'R' || sess === 'CARRERA';

  // Badge de sesión exacto
  if (sessionBadgeEl) {
    sessionBadgeEl.textContent = sess;
    sessionBadgeEl.style.color = isRace ? '#ffd000' : isQualy ? '#ff334b' : '#00e5ff';
  }

  // Circuito del calendario oficial
  if (circuitBadgeEl) {
    const rawCircuit = rs.circuit || sd?.circuit_default || '';
    const matched = matchCircuit(rawCircuit, sd?.circuits);
    const flag = matched?.flag || '🏁';
    const shortName = matched?.short_name || rawCircuit || 'F1 GP';
    circuitBadgeEl.textContent = `${flag} ${shortName.toUpperCase()}`;
    circuitBadgeEl.title = matched?.name ? `${matched.name} (${matched.gp_name || ''})` : shortName;
  }

  // Vuelta / tiempo transcurrido / Cronómetro de sesión
  sessionLapsEl.classList.remove('urgent');
  if (isRace) {
    sessionLapsEl.textContent = rs.total && rs.total > 0 ? `V ${rs.lap}/${rs.total}` : `V ${rs.lap}`;
    gridDeltaHeader.textContent = 'Delta';
    metric1Label.textContent = 'Delta Ant.';
    metric3Label.textContent = 'En Vuelta';
  } else if (isQualy) {
    if (rs.remaining) {
      sessionLapsEl.textContent = `⏱ ${rs.remaining}`;
      // Alerta urgente si quedan menos de 2 minutos (ej. 01:xx o 00:xx)
      if (rs.remaining.startsWith('01:') || rs.remaining.startsWith('00:')) {
        sessionLapsEl.classList.add('urgent');
      }
    } else {
      sessionLapsEl.textContent = rs.lap ? `V ${rs.lap}` : 'QUALY';
    }
    gridDeltaHeader.textContent = 'Gap / Best';
    metric1Label.textContent = 'Gap a P1';
    metric3Label.textContent = 'Estado';
  } else {
    // Prácticas (FP1, FP2, FP3)
    if (rs.remaining) {
      sessionLapsEl.textContent = `⏱ ${rs.remaining}`;
      if (rs.remaining.startsWith('01:') || rs.remaining.startsWith('00:')) {
        sessionLapsEl.classList.add('urgent');
      }
    } else {
      sessionLapsEl.textContent = rs.lap ? `V ${rs.lap}` : 'PRACTICE';
    }
    gridDeltaHeader.textContent = 'Mejor V.';
    metric1Label.textContent = 'Gap a P1';
    metric3Label.textContent = 'Vueltas';
  }
  
  if (rs.flag) {
    flagBadgeEl.textContent = rs.flag;
    flagBadgeEl.className = 'flag-badge ' + (
      (rs.flag === 'GREEN' || rs.flag === 'FIN') ? 'flag-green' :
      rs.flag === 'YELLOW' ? 'flag-yellow' :
      rs.flag === 'RED' ? 'flag-red' :
      rs.flag === 'SC' ? 'flag-sc' : 'flag-vsc'
    );
  }

  // Render Hero Favorite
  const favCode = state.favoriteDriver;
  const favDriverMeta = sd?.drivers?.[favCode] || { name: favCode, color: '#3671c6' };
  heroAccentBar.style.backgroundColor = favDriverMeta.color || '#fff';
  heroDriverCode.textContent = favCode;
  heroDriverName.textContent = favDriverMeta.name;

  const currentDrivers = rs.drivers || [];
  const currentLap = rs.lap || 1;

  // Seguimiento de cambio de posición en la vuelta actual
  if (isRace && currentDrivers.length > 0) {
    if (lastTrackedLap === null) {
      previousLapPositions = {};
      for (const d of currentDrivers) {
        if (d[0] && d[1] != null) previousLapPositions[d[0]] = d[1];
      }
      lastTrackedLap = currentLap;
    } else if (currentLap > lastTrackedLap) {
      if (currentLapPositions && Object.keys(currentLapPositions).length > 0) {
        previousLapPositions = { ...currentLapPositions };
      }
      lastTrackedLap = currentLap;
    } else if (currentLap < lastTrackedLap) {
      previousLapPositions = {};
      for (const d of currentDrivers) {
        if (d[0] && d[1] != null) previousLapPositions[d[0]] = d[1];
      }
      lastTrackedLap = currentLap;
    }

    currentLapPositions = {};
    for (const d of currentDrivers) {
      if (d[0] && d[1] != null) {
        currentLapPositions[d[0]] = d[1];
        if (previousLapPositions[d[0]] === undefined) {
          previousLapPositions[d[0]] = d[1];
        }
      }
    }
  }

  const favRow = currentDrivers.find(d => d[0] === favCode);

  if (favRow) {
    const pos = favRow[1];
    
    // Variables según modo
    let delta = '';
    let tyre = 'M';
    let age = 0;
    let inPit = false;
    let trend = 0;
    let bestLap = '--';
    let gapP1 = '--';

    if (isRace) {
      // Carrera: [CODE, POS, GAP_ANT, TYRE, AGE, IN_PIT, TREND]
      delta = favRow[2] || '+0.000';
      tyre = favRow[3] || 'M';
      age = favRow[4] || 0;
      inPit = favRow[5] === 1;
      trend = favRow[6] || 0;
    } else {
      // FP / Qualy: [CODE, POS, BEST_LAP, GAP_P1, TYRE, LAPS_TOTAL, IN_PIT]
      bestLap = favRow[2] || '--';
      gapP1 = favRow[3] || '--';
      tyre = favRow[4] || 'M';
      age = favRow[5] || 0;
      inPit = favRow[6] === 1;
      delta = bestLap;
    }

    heroDriverPos.textContent = `P${pos}`;

    // Estado FIA del piloto favorito (investigación o sanción)
    const favFia = (favRow ? favRow[7] : '') || rs.penalties?.[favCode] || '';
    if (heroFiaBadgeEl) {
      if (favFia === 'INV') {
        heroFiaBadgeEl.textContent = '⚠️ INV. FIA';
        heroFiaBadgeEl.className = 'hero-fia-badge hero-fia-inv';
        heroFiaBadgeEl.style.display = 'inline-flex';
      } else if (favFia) {
        heroFiaBadgeEl.textContent = `🚨 ${favFia}`;
        heroFiaBadgeEl.className = 'hero-fia-badge hero-fia-pen';
        heroFiaBadgeEl.style.display = 'inline-flex';
      } else {
        heroFiaBadgeEl.style.display = 'none';
      }
    }

    if (isRace) {
      heroDeltaVal.textContent = delta;
      heroTyreVal.textContent = `${tyre} (${age}v)`;
      
      const basePos = previousLapPositions[favCode] != null ? previousLapPositions[favCode] : pos;
      const diff = basePos - pos;

      if (diff > 0) {
        heroTrendVal.textContent = `▲ Ganó ${diff} pos`;
        heroTrendVal.className = 'metric-val trend-up';
      } else if (diff < 0) {
        const lost = Math.abs(diff);
        heroTrendVal.textContent = `▼ Perdió ${lost} pos`;
        heroTrendVal.className = 'metric-val trend-down';
      } else {
        heroTrendVal.textContent = '― Mantuvo';
        heroTrendVal.className = 'metric-val trend-flat';
      }
    } else if (isQualy) {
      heroDeltaVal.textContent = gapP1;
      heroTyreVal.textContent = `${tyre} (${age}v)`;
      const inDanger = pos > 15;
      heroTrendVal.textContent = inDanger ? '🔴 ZONA CORTE' : '🟢 PASA';
      heroTrendVal.className = 'metric-val ' + (inDanger ? 'trend-down' : 'trend-up');
    } else {
      // FP
      heroDeltaVal.textContent = gapP1;
      heroTyreVal.textContent = `${tyre} (Mejor)`;
      heroTrendVal.textContent = `${age} giros`;
      heroTrendVal.className = 'metric-val trend-flat';
    }

    // Notify haptic if favorite just entered pit
    if (inPit && !previousInPit.has(favCode)) {
      haptics.notify('pit');
    }

    // Pit Window / Info Box según modo
    if (isQualy) {
      const cutoffGap = pos > 15 ? 'Fuera por el momento' : 'Dentro del corte';
      heroPitPred.innerHTML = `<span class="pit-pred-icon">⏱️</span> Mejor tiempo: <strong>${bestLap}</strong> (${cutoffGap})`;
    } else if (isPractice) {
      heroPitPred.innerHTML = `<span class="pit-pred-icon">🏎️</span> Mejor registro: <strong>${bestLap}</strong> • ${age} vueltas en sesión`;
    } else if (state.isStale) {
      heroPitPred.innerHTML = '<span class="pit-pred-icon">⏸️</span> Predicción pausada (datos desfasados)';
    } else if (inPit) {
      heroPitPred.innerHTML = '<span class="pit-pred-icon">🛠️</span> <strong>EN PIT LANE AHORA</strong>';
    } else {
      const circuitMeta = sd?.circuits?.[state.currentCircuit] || { pit_loss_sec: rs.pit_delta || 21.0 };
      const pitLoss = rs.pit_delta || circuitMeta.pit_loss_sec || 21.0;
      const pred = calculatePitExit(favCode, currentDrivers, pitLoss);
      if (pred) {
        if (pred.retained) {
          heroPitPred.innerHTML = `<span class="pit-pred-icon">⏱️</span> Si para ahora: <strong>Retiene P${pred.pos}</strong> (Ventana abierta)`;
        } else {
          heroPitPred.innerHTML = `<span class="pit-pred-icon">⏱️</span> Si para ahora: Sale <strong>P${pred.pos}</strong> detrás de <strong>${pred.behind}</strong>`;
        }
      }
    }
  } else {
    heroDriverPos.textContent = 'P--';
    heroDeltaVal.textContent = '--';
    heroTyreVal.textContent = '--';
    heroTrendVal.textContent = '--';
    heroPitPred.innerHTML = 'Piloto no clasificado o fuera de sesión';
  }

  // Update inPit set
  previousInPit = new Set(currentDrivers.filter(d => (isRace ? d[5] === 1 : d[6] === 1)).map(d => d[0]));

  // Auto-align segment to favorite driver's position if user hasn't toggled manually
  if (!hasUserManuallyToggledSegment && favRow && state.gridSegmentMode !== 'all') {
    const p = favRow[1] || 1;
    state.gridSegmentMode = p <= 11 ? 'top11' : 'bottom11';
  }
  updateSegmentButtons();

  // Filtrar según segmentación de grilla: Top 11 (P1-11), Bottom 11 (P12-22) o Todos (22)
  let displayedDrivers = currentDrivers;
  if (state.gridSegmentMode === 'top11') {
    displayedDrivers = currentDrivers.filter(d => (d[1] || 99) <= 11);
  } else if (state.gridSegmentMode === 'bottom11') {
    displayedDrivers = currentDrivers.filter(d => (d[1] || 99) > 11);
  }

  // Render Timing Grid según modo de sesión
  let gridHtml = '';
  for (const d of displayedDrivers) {
    const code = d[0];
    const pos = d[1];
    let dDelta = '';
    let dTyre = 'M';
    let dAge = 0;
    let dInPit = false;
    let dTrend = 0;

    if (isRace) {
      dDelta = d[2];
      dTyre = d[3];
      dAge = d[4];
      dInPit = d[5] === 1;
      dTrend = d[6];
    } else {
      // FP / Qualy: d[2]=best_lap, d[3]=gap_p1, d[4]=tyre, d[5]=laps, d[6]=in_pit
      dDelta = isPractice ? d[2] : (pos === 1 ? d[2] : d[3]);
      dTyre = d[4];
      dAge = d[5];
      dInPit = d[6] === 1;
    }

    const dFia = (d[7] || rs.penalties?.[code] || '').trim();
    let statusHtml = '';
    if (dInPit) {
      statusHtml = '<span class="in-pit-tag">PIT</span>';
    } else if (dFia) {
      if (dFia === 'INV') {
        statusHtml = '<span class="fia-badge fia-inv" title="Bajo investigación FIA">INV</span>';
      } else {
        statusHtml = `<span class="fia-badge fia-pen" title="Sanción pendiente">${dFia}</span>`;
      }
    }

    const dMeta = sd?.drivers?.[code] || { color: '#ffffff' };
    const isFav = code === favCode;
    const isCutoff = isQualy && (pos === 15 || pos === 10);

    const basePos = previousLapPositions[code] != null ? previousLapPositions[code] : pos;
    const posDiff = basePos - pos;
    let posArrowHtml = '<span class="pos-arrow"></span>';
    if (isRace && posDiff > 0) {
      posArrowHtml = '<span class="pos-arrow pos-arrow-up" title="Adelantó en la vuelta actual">▲</span>';
    } else if (isRace && posDiff < 0) {
      posArrowHtml = '<span class="pos-arrow pos-arrow-down" title="Fue adelantado en la vuelta actual">▼</span>';
    }

    gridHtml += `
      <div class="grid-row ${isFav ? 'favorite' : ''} ${isCutoff ? 'elimination-cutoff' : ''}" data-driver="${code}">
        <div class="pos-cell">
          ${posArrowHtml}
          <span class="pos-num" style="color:${pos <= 3 ? '#ffd000' : isQualy && pos > 15 ? '#ff334b' : '#fff'};">${pos}</span>
        </div>
        <div class="driver-col">
          <div class="team-pill" style="background:${dMeta.color};"></div>
          <span style="letter-spacing:0.5px;">${code}</span>
        </div>
        <div style="text-align:center; display:flex; align-items:center; justify-content:center;">
          ${statusHtml}
        </div>
        <div style="display:flex; align-items:center; justify-content:center;">
          <span class="tyre-badge tyre-${dTyre}">${dTyre}</span>
          <span style="font-size:10px; color:#a3a3a3; font-family:monospace;">${dAge}v</span>
        </div>
        <div class="delta-col">${dDelta}</div>
      </div>
    `;
  }
  timingGridBody.innerHTML = gridHtml;

  // Add click to select favorite directly from grid
  timingGridBody.querySelectorAll('.grid-row').forEach(row => {
    row.addEventListener('click', () => {
      const code = row.getAttribute('data-driver');
      if (code) state.setFavoriteDriver(code);
    });
  });

  // Render Ticker
  if (rs.ticker && rs.ticker.length > 0) {
    tickerListEl.innerHTML = rs.ticker
      .slice(0, 5)
      .map(t => `<li class="ticker-item">${t}</li>`)
      .join('');
  }
}

// 7. Event Handlers & Modal
openModalBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  driverModal.classList.add('open');
});

// Ergonomía táctil: Tocar la Hero Card abre directamente el selector de pilotos
if (heroCardEl) {
  heroCardEl.addEventListener('click', () => driverModal.classList.add('open'));
}

function updateSegmentButtons() {
  if (viewTop11Btn) viewTop11Btn.classList.toggle('active', state.gridSegmentMode === 'top11');
  if (viewBottom11Btn) viewBottom11Btn.classList.toggle('active', state.gridSegmentMode === 'bottom11');
  if (viewAllBtn) viewAllBtn.classList.toggle('active', state.gridSegmentMode === 'all');
}

if (viewTop11Btn) {
  viewTop11Btn.addEventListener('click', () => {
    hasUserManuallyToggledSegment = true;
    state.setGridSegmentMode('top11');
    updateSegmentButtons();
  });
}

if (viewBottom11Btn) {
  viewBottom11Btn.addEventListener('click', () => {
    hasUserManuallyToggledSegment = true;
    state.setGridSegmentMode('bottom11');
    updateSegmentButtons();
  });
}

if (viewAllBtn) {
  viewAllBtn.addEventListener('click', () => {
    hasUserManuallyToggledSegment = true;
    state.setGridSegmentMode('all');
    updateSegmentButtons();
  });
}

closeModalBtn.addEventListener('click', () => driverModal.classList.remove('open'));
driverModal.addEventListener('click', (e) => {
  if (e.target === driverModal) driverModal.classList.remove('open');
});

window.addEventListener('online', () => state.setOffline(false));
window.addEventListener('offline', () => state.setOffline(true));

// 8. Bootstrap
state.subscribe(renderUI);

// Initial bootstrap sequence
async function init() {
  await loadSeasonData();
  await fetchRaceState();
  setInterval(fetchRaceState, POLLING_INTERVAL_MS);
  setInterval(checkStaleness, 3000);
}

init();

// Register Service Worker (PWA Cache-First)
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(err => {
      console.warn('SW registration failed:', err);
    });
  });
}
