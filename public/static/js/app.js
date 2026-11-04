// Trackside Pulse - Application Orchestrator & Polling Engine
import { state } from './state.js';
import { haptics } from './haptics.js';

const STALE_THRESHOLD_SECONDS = 90;
const POLLING_INTERVAL_MS = 6000;

// DOM Elements
const stalenessBanner = document.getElementById('staleness-banner');
const stalenessText = document.getElementById('staleness-text');
const liveDot = document.getElementById('live-dot');
const sessionLapsEl = document.getElementById('session-laps');
const flagBadgeEl = document.getElementById('flag-badge');

// Hero Card Elements
const heroDriverCode = document.getElementById('hero-driver-code');
const heroDriverName = document.getElementById('hero-driver-name');
const heroDriverPos = document.getElementById('hero-driver-pos');
const heroDeltaVal = document.getElementById('hero-delta-val');
const heroTyreVal = document.getElementById('hero-tyre-val');
const heroTrendVal = document.getElementById('hero-trend-val');
const heroPitPred = document.getElementById('hero-pit-pred');
const heroAccentBar = document.getElementById('hero-accent-bar');
const driverModal = document.getElementById('driver-modal');
const modalList = document.getElementById('modal-driver-list');
const openModalBtn = document.getElementById('open-driver-modal');
const closeModalBtn = document.getElementById('close-driver-modal');

// Timing Grid
const timingGridBody = document.getElementById('timing-grid-body');
// Ticker
const tickerListEl = document.getElementById('ticker-list');

let previousFlag = null;
let previousInPit = new Set();

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
  } else {
    state.markStale(false);
    stalenessBanner.classList.remove('visible');
    liveDot.className = state.isOffline ? 'brand-dot offline' : 'brand-dot';
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

  // Top Session Status
  const isRace = rs.total && rs.total > 0;
  sessionLapsEl.textContent = isRace ? `V ${rs.lap}/${rs.total}` : `V ${rs.lap}`;
  
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
  const favRow = currentDrivers.find(d => d[0] === favCode);

  if (favRow) {
    // favRow: [CODE, POS, DELTA, TYRE, AGE, IN_PIT, TREND]
    const pos = favRow[1];
    const delta = favRow[2];
    const tyre = favRow[3];
    const age = favRow[4];
    const inPit = favRow[5] === 1;
    const trend = favRow[6];

    heroDriverPos.textContent = `P${pos}`;
    heroDeltaVal.textContent = delta;
    heroTyreVal.textContent = `${tyre} (${age}v)`;
    heroTrendVal.textContent = trend === 1 ? '▲ Ganando' : trend === -1 ? '▼ Perdiendo' : '― Estable';
    heroTrendVal.className = 'metric-val ' + (trend === 1 ? 'trend-up' : trend === -1 ? 'trend-down' : 'trend-flat');

    // Notify haptic if favorite just entered pit
    if (inPit && !previousInPit.has(favCode)) {
      haptics.notify('pit');
    }

    // Pit Window Prediction
    const circuitMeta = sd?.circuits?.[state.currentCircuit] || { pit_loss_sec: rs.pit_delta || 21.0 };
    const pitLoss = rs.pit_delta || circuitMeta.pit_loss_sec || 21.0;

    if (state.isStale) {
      heroPitPred.innerHTML = '<span class="pit-pred-icon">⏸️</span> Predicción pausada (datos desfasados)';
    } else if (inPit) {
      heroPitPred.innerHTML = '<span class="pit-pred-icon">🛠️</span> <strong>EN PIT LANE AHORA</strong>';
    } else {
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
  previousInPit = new Set(currentDrivers.filter(d => d[5] === 1).map(d => d[0]));

  // Render Timing Grid
  let gridHtml = '';
  for (const d of currentDrivers) {
    const code = d[0];
    const pos = d[1];
    const delta = d[2];
    const tyre = d[3];
    const age = d[4];
    const inPit = d[5] === 1;
    const trend = d[6];

    const dMeta = sd?.drivers?.[code] || { color: '#ffffff' };
    const isFav = code === favCode;
    const trendClass = trend === 1 ? 'trend-up' : trend === -1 ? 'trend-down' : 'trend-flat';
    const trendArrow = trend === 1 ? '▲' : trend === -1 ? '▼' : ' ';

    gridHtml += `
      <div class="grid-row ${isFav ? 'favorite' : ''}" data-driver="${code}">
        <div style="font-family:monospace; color:${pos <= 3 ? '#ffd000' : '#fff'};">${pos}</div>
        <div class="driver-col">
          <div class="team-pill" style="background:${dMeta.color};"></div>
          <span>${code}</span>
        </div>
        <div>
          ${inPit ? '<span class="in-pit-tag">IN PIT</span>' : ''}
        </div>
        <div>
          <span class="tyre-badge tyre-${tyre}">${tyre}</span>
          <span style="font-size:11px; color:#a3a3a3;">${age}v</span>
        </div>
        <div class="delta-col">
          <span class="${trendClass}">${trendArrow}</span> ${delta}
        </div>
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
openModalBtn.addEventListener('click', () => driverModal.classList.add('open'));
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
