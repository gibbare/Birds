const SMHI_URL = (lon, lat) =>
  `https://opendata-download-metfcst.smhi.se/api/category/pmp3g/version/2/geotype/point/lon/${lon}/lat/${lat}/data.json`;

const WEATHER_SYMBOLS = {
  1: 'Klart', 2: 'Nästan klart', 3: 'Halvklart', 4: 'Halvmulet',
  5: 'Mulet', 6: 'Mulet', 7: 'Dimma', 8: 'Lätt regnskur',
  9: 'Regnskur', 10: 'Kraftig regnskur', 11: 'Åskväder',
  12: 'Lätt snöblandad regnskur', 13: 'Snöblandad regnskur',
  14: 'Kraftig snöblandad regnskur', 15: 'Lätt snöskur',
  16: 'Snöskur', 17: 'Kraftig snöskur', 18: 'Lätt regn',
  19: 'Regn', 20: 'Kraftigt regn', 21: 'Åska',
  22: 'Lätt snöblandat regn', 23: 'Snöblandat regn',
  24: 'Kraftigt snöblandat regn', 25: 'Lätt snöfall',
  26: 'Snöfall', 27: 'Kraftigt snöfall',
};

const WEATHER_EMOJI = {
  1: '☀️', 2: '🌤️', 3: '⛅', 4: '🌥️', 5: '☁️', 6: '☁️',
  7: '🌫️', 8: '🌦️', 9: '🌧️', 10: '🌧️', 11: '⛈️',
  12: '🌨️', 13: '🌨️', 14: '🌨️', 15: '❄️', 16: '❄️', 17: '❄️',
  18: '🌧️', 19: '🌧️', 20: '🌧️', 21: '⛈️',
  22: '🌨️', 23: '🌨️', 24: '🌨️', 25: '❄️', 26: '❄️', 27: '❄️',
};

function getParam(ts, name) {
  const p = ts.parameters.find(p => p.name === name);
  return p ? p.values[0] : null;
}

function feelsLike(temp, windMs) {
  const windKmh = windMs * 3.6;
  if (temp > 10 || windKmh < 5) return temp;
  return Math.round(13.12 + 0.6215 * temp - 11.37 * Math.pow(windKmh, 0.16) + 0.3965 * temp * Math.pow(windKmh, 0.16));
}

async function reverseGeocode(lat, lon) {
  try {
    const res = await fetch(
      `https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lon}&format=json`,
      { headers: { 'Accept-Language': 'sv' } }
    );
    const data = await res.json();
    const addr = data.address;
    return addr.city || addr.town || addr.village || addr.hamlet ||
           addr.suburb || addr.quarter || addr.locality ||
           addr.municipality || addr.county || 'Okänd plats';
  } catch {
    return `${lat.toFixed(2)}°N, ${lon.toFixed(2)}°E`;
  }
}

async function fetchWeather(lat, lon) {
  const res = await fetch(SMHI_URL(lon.toFixed(6), lat.toFixed(6)));
  if (!res.ok) throw new Error(`SMHI svarade med ${res.status}`);
  return res.json();
}

// ─── Chart ────────────────────────────────────────────────────────────────────

function tempColor(t) {
  if (t <= 0)  return '#7eb8f7';
  if (t <= 10) return '#5dd6c0';
  if (t <= 20) return '#f7c948';
  return '#f77b5e';
}

function smoothPath(ctx, pts) {
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(i - 1, 0)];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[Math.min(i + 2, pts.length - 1)];
    const cp1x = p1.x + (p2.x - p0.x) / 6;
    const cp1y = p1.y + (p2.y - p0.y) / 6;
    const cp2x = p2.x - (p3.x - p1.x) / 6;
    const cp2y = p2.y - (p3.y - p1.y) / 6;
    ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
  }
}

// Groups timeSeries into days and returns [{date, label, emoji, tMin, tMax}]
function buildDaySummaries(timeSeries) {
  const now    = new Date(timeSeries[0].validTime);
  const days = {};

  timeSeries.forEach(ts => {
    const d = new Date(ts.validTime);
    const key = d.toDateString();
    if (!days[key]) days[key] = { date: d, temps: [], symbols: [] };
    const t = getParam(ts, 't');
    const s = getParam(ts, 'Wsymb2');
    if (t != null) days[key].temps.push(t);
    if (s != null) days[key].symbols.push(s);
  });

  return Object.values(days).slice(0, 3).map(day => {
    const tMin = Math.round(Math.min(...day.temps));
    const tMax = Math.round(Math.max(...day.temps));
    // Most common symbol
    const freq = {};
    day.symbols.forEach(s => freq[s] = (freq[s] || 0) + 1);
    const dominant = Object.entries(freq).sort((a, b) => b[1] - a[1])[0]?.[0] ?? 1;
    const label = day.date.toLocaleDateString('sv-SE', { weekday: 'short' }).toUpperCase();
    return { label, emoji: WEATHER_EMOJI[dominant] || '🌡️', tMin, tMax };
  });
}

function drawDaySummary(summaries) {
  const el = document.getElementById('day-summary');
  el.innerHTML = summaries.map(s => `
    <div class="day-card">
      <div class="dc-name">${s.label}</div>
      <div class="dc-emoji">${s.emoji}</div>
      <div class="dc-temps">${s.tMax}° <span>/ ${s.tMin}°</span></div>
    </div>
  `).join('');
}

function drawForecastChart(timeSeries) {
  const canvas = document.getElementById('forecast-chart');

  // ── 2 punkter per dag: UTC 06+18 (morgon/kväll) eller UTC 00+12 som fallback ─
  const byDay = {};
  timeSeries.forEach(ts => {
    const key = new Date(ts.validTime).toDateString();
    if (!byDay[key]) byDay[key] = [];
    byDay[key].push(ts);
  });
  const pts3h = [];
  Object.values(byDay).forEach(dayPts => {
    const utcHours = new Set(dayPts.map(ts => new Date(ts.validTime).getUTCHours()));
    const targets  = (utcHours.has(6) || utcHours.has(18)) ? [6, 18] : [0, 12];
    targets.forEach(t => {
      const match = dayPts.find(ts => new Date(ts.validTime).getUTCHours() === t);
      if (match) pts3h.push(match);
    });
  });
  pts3h.sort((a, b) => new Date(a.validTime) - new Date(b.validTime));
  if (pts3h.length < 2) return;

  // ── canvas dimensions ───────────────────────────────────────────────────────
  const PT_SPACING = 64;
  const PAD = { top: 72, right: 70, bottom: 42, left: 38 };
  const chartW = PT_SPACING * (pts3h.length - 1);
  const totalW = chartW + PAD.left + PAD.right;
  const totalH = 220;
  const chartH = totalH - PAD.top - PAD.bottom;

  canvas.width  = totalW;
  canvas.height = totalH;
  canvas.style.width  = totalW + 'px';
  canvas.style.height = totalH + 'px';

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, totalW, totalH);

  // ── data ────────────────────────────────────────────────────────────────────
  const temps   = pts3h.map(ts => getParam(ts, 't')      ?? 0);
  const precips = pts3h.map(ts => getParam(ts, 'pmean')  ?? 0);
  const symbols = pts3h.map(ts => getParam(ts, 'Wsymb2') ?? 1);
  const times   = pts3h.map(ts => new Date(ts.validTime));

  const tMin   = Math.floor(Math.min(...temps)) - 2;
  const tMax   = Math.ceil(Math.max(...temps))  + 3;
  const tRange = tMax - tMin || 1;

  const xOf = i => PAD.left + i * PT_SPACING;
  const yOf = t => PAD.top + chartH - ((t - tMin) / tRange) * chartH;
  const points = temps.map((t, i) => ({ x: xOf(i), y: yOf(t) }));

  // ── pre-compute day groups ───────────────────────────────────────────────────
  const dayGroups = [];
  let gStart = 0;
  for (let i = 1; i <= times.length; i++) {
    if (i === times.length || times[i].toDateString() !== times[gStart].toDateString()) {
      dayGroups.push({ startI: gStart, endI: i - 1, date: times[gStart] });
      gStart = i;
    }
  }

  // ── day background bands ─────────────────────────────────────────────────────
  const DAY_COLORS = ['rgba(100,140,200,0.07)', 'rgba(80,120,180,0.03)'];
  dayGroups.forEach((g, di) => {
    const x1 = Math.max(xOf(g.startI) - PT_SPACING / 2, PAD.left);
    const x2 = xOf(g.endI) + PT_SPACING / 2;
    ctx.fillStyle = DAY_COLORS[di % 2];
    ctx.fillRect(x1, PAD.top, x2 - x1, chartH);
  });

  // ── horizontal grid + temp axis ──────────────────────────────────────────────
  for (let t = Math.ceil(tMin); t <= tMax; t += 2) {
    const y = yOf(t);
    ctx.strokeStyle = t === 0 ? 'rgba(126,184,247,0.3)' : 'rgba(255,255,255,0.06)';
    ctx.lineWidth   = 1;
    ctx.setLineDash(t === 0 ? [6, 3] : []);
    ctx.beginPath();
    ctx.moveTo(PAD.left, y);
    ctx.lineTo(PAD.left + chartW, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = t === 0 ? 'rgba(126,184,247,0.9)' : 'rgba(160,176,208,0.7)';
    ctx.font      = `${t === 0 ? 'bold ' : ''}11px system-ui`;
    ctx.textAlign = 'right';
    ctx.fillText(t + '°', PAD.left - 6, y + 4);
  }

  // ── precipitation bars ───────────────────────────────────────────────────────
  const maxPrecip = Math.max(...precips, 0.5);
  const barMaxH   = chartH * 0.28;
  precips.forEach((p, i) => {
    if (p <= 0) return;
    const bh = Math.max((p / maxPrecip) * barMaxH, 4);
    const bw = PT_SPACING * 0.5;
    ctx.fillStyle = 'rgba(80,160,255,0.45)';
    ctx.beginPath();
    ctx.roundRect(xOf(i) - bw / 2, PAD.top + chartH - bh, bw, bh, 3);
    ctx.fill();
  });

  // ── day separators + centered labels with date ───────────────────────────────
  dayGroups.forEach((g, di) => {
    const cx = (xOf(g.startI) + xOf(g.endI)) / 2;
    const dayName  = g.date.toLocaleDateString('sv-SE', { weekday: 'short' }).toUpperCase();
    const dateStr  = g.date.toLocaleDateString('sv-SE', { day: 'numeric', month: 'short' });

    // separator before each day except the first
    if (di > 0) {
      const sepX = xOf(g.startI) - PT_SPACING / 2;
      ctx.strokeStyle = 'rgba(255,255,255,0.2)';
      ctx.lineWidth   = 1.5;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(sepX, PAD.top);
      ctx.lineTo(sepX, PAD.top + chartH);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // day name
    ctx.fillStyle = 'rgba(180,210,255,0.85)';
    ctx.font      = 'bold 11px system-ui';
    ctx.textAlign = 'center';
    ctx.fillText(dayName, cx, PAD.top - 22);

    // date below day name
    ctx.fillStyle = 'rgba(140,170,220,0.65)';
    ctx.font      = '10px system-ui';
    ctx.fillText(dateStr, cx, PAD.top - 10);
  });

  // ── gradient fill ─────────────────────────────────────────────────────────────
  const fillGrad = ctx.createLinearGradient(0, PAD.top, 0, PAD.top + chartH);
  fillGrad.addColorStop(0,   'rgba(126,184,247,0.4)');
  fillGrad.addColorStop(0.6, 'rgba(126,184,247,0.1)');
  fillGrad.addColorStop(1,   'rgba(126,184,247,0)');
  ctx.beginPath();
  smoothPath(ctx, points);
  ctx.lineTo(points[points.length - 1].x, PAD.top + chartH);
  ctx.lineTo(points[0].x, PAD.top + chartH);
  ctx.closePath();
  ctx.fillStyle = fillGrad;
  ctx.fill();

  // ── temperature curve ────────────────────────────────────────────────────────
  const lineGrad = ctx.createLinearGradient(PAD.left, 0, PAD.left + chartW, 0);
  temps.forEach((t, i) => lineGrad.addColorStop(i / (temps.length - 1), tempColor(t)));
  ctx.beginPath();
  smoothPath(ctx, points);
  ctx.strokeStyle = lineGrad;
  ctx.lineWidth   = 3;
  ctx.lineJoin    = 'round';
  ctx.stroke();

  // ── dots + labels ────────────────────────────────────────────────────────────
  points.forEach((pt, i) => {
    const h = times[i].getHours();

    // dot with glow
    ctx.shadowColor = tempColor(temps[i]);
    ctx.shadowBlur  = 6;
    ctx.beginPath();
    ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2);
    ctx.fillStyle   = tempColor(temps[i]);
    ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.lineWidth   = 1.5;
    ctx.fill();
    ctx.stroke();
    ctx.shadowBlur = 0;

    // temp label
    ctx.fillStyle = '#e8f0ff';
    ctx.font      = 'bold 10px system-ui';
    ctx.textAlign = 'center';
    ctx.fillText(Math.round(temps[i]) + '°', pt.x, pt.y - 9);

    // emoji
    ctx.font = '15px serif';
    ctx.fillText(WEATHER_EMOJI[symbols[i]] || '🌡️', pt.x, pt.y - 23);

    // time label
    ctx.fillStyle = 'rgba(160,176,208,0.9)';
    ctx.font      = '10px system-ui';
    ctx.fillText(h.toString().padStart(2,'0') + ':00', pt.x, PAD.top + chartH + 14);
  });
}

// ─── Search ───────────────────────────────────────────────────────────────────

async function searchPlaces(query) {
  const url = `https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(query)}&format=json&limit=6&addressdetails=1&accept-language=sv`;
  const res = await fetch(url, { headers: { 'Accept-Language': 'sv' } });
  return res.json();
}

function openSearch() {
  document.getElementById('search-modal').classList.remove('hidden');
  const input = document.getElementById('search-input');
  input.value = '';
  document.getElementById('search-results').innerHTML = '';
  setTimeout(() => input.focus(), 50);
}

function closeSearch() {
  document.getElementById('search-modal').classList.add('hidden');
}

function renderSearchResults(results) {
  const list = document.getElementById('search-results');
  if (!results.length) {
    list.innerHTML = '<li class="search-empty">Inga orter hittades</li>';
    return;
  }
  function placeName(r) {
    const addr = r.address || {};
    return addr.city || addr.town || addr.village || addr.hamlet ||
           addr.suburb || addr.quarter || addr.locality ||
           r.display_name.split(',')[0];
  }

  list.innerHTML = results.map((r, i) => {
    const addr = r.address || {};
    const name = placeName(r);
    const detail = [addr.municipality, addr.county, addr.country].filter(Boolean).join(', ');
    return `<li data-index="${i}">
      <div class="result-name">${name}</div>
      <div class="result-detail">${detail}</div>
    </li>`;
  }).join('');

  list.querySelectorAll('li[data-index]').forEach(el => {
    el.addEventListener('click', () => {
      const r = results[+el.dataset.index];
      const name = placeName(r);
      closeSearch();
      loadWeatherForCoords(parseFloat(r.lat), parseFloat(r.lon), name);
    });
  });
}

async function loadWeatherForCoords(lat, lon, placeName) {
  document.getElementById('loading').classList.remove('hidden');
  document.getElementById('weather').classList.add('hidden');
  document.getElementById('error').classList.add('hidden');
  document.getElementById('location-name').textContent = placeName;
  try {
    const weatherData = await fetchWeather(lat, lon);
    renderWeather(weatherData);
    showWeather();
  } catch (err) {
    showError('Kunde inte hämta väderdata: ' + err.message);
  }
}

function initSearch() {
  let debounceTimer;
  const input = document.getElementById('search-input');

  document.getElementById('app-title').addEventListener('click', openSearch);
  document.getElementById('search-close').addEventListener('click', closeSearch);
  document.getElementById('search-backdrop').addEventListener('click', closeSearch);

  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    const q = input.value.trim();
    if (q.length < 2) {
      document.getElementById('search-results').innerHTML = '';
      return;
    }
    debounceTimer = setTimeout(async () => {
      const results = await searchPlaces(q);
      renderSearchResults(results);
    }, 300);
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeSearch();
  });
}

// ─── Render ───────────────────────────────────────────────────────────────────

function renderWeather(data) {
  const now     = data.timeSeries[0];
  const temp    = getParam(now, 't');
  const wind    = getParam(now, 'ws');
  const humidity = getParam(now, 'r');
  const precip  = getParam(now, 'pmean') ?? 0;
  const symbol  = getParam(now, 'Wsymb2');

  document.getElementById('temperature').textContent  = Math.round(temp);
  document.getElementById('temperature').style.color  = temp > 0 ? '#7dde8a' : '#7eb8f7';

  document.getElementById('wind').textContent         = wind != null ? wind.toFixed(1) : '--';
  document.getElementById('wind').closest('.card').style.background =
    wind != null && wind > 15 ? 'rgba(220, 80, 80, 0.25)' : '';
  document.getElementById('humidity').textContent     = humidity != null ? Math.round(humidity) : '--';
  document.getElementById('precip').textContent       = precip != null ? precip.toFixed(1) : '--';
  document.getElementById('feels-like').textContent   = wind != null ? feelsLike(temp, wind) : Math.round(temp);
  document.getElementById('weather-desc').textContent = WEATHER_SYMBOLS[symbol] || '--';

  drawDaySummary(buildDaySummaries(data.timeSeries));
  drawForecastChart(data.timeSeries);
}

function showError(msg) {
  document.getElementById('loading').classList.add('hidden');
  document.getElementById('weather').classList.add('hidden');
  document.getElementById('error-msg').textContent = msg;
  document.getElementById('error').classList.remove('hidden');
}

function showWeather() {
  document.getElementById('loading').classList.add('hidden');
  document.getElementById('error').classList.add('hidden');
  document.getElementById('weather').classList.remove('hidden');
}

async function init() {
  document.getElementById('loading').classList.remove('hidden');
  document.getElementById('weather').classList.add('hidden');
  document.getElementById('error').classList.add('hidden');
  document.getElementById('location-name').textContent = 'Hämtar din position...';

  if (!navigator.geolocation) {
    showError('Din webbläsare stöder inte geolokalisering.');
    return;
  }

  navigator.geolocation.getCurrentPosition(
    async ({ coords }) => {
      const { latitude: lat, longitude: lon } = coords;
      try {
        const [placeName, weatherData] = await Promise.all([
          reverseGeocode(lat, lon),
          fetchWeather(lat, lon),
        ]);
        document.getElementById('location-name').textContent = placeName;
        renderWeather(weatherData);
        showWeather();
      } catch (err) {
        showError('Kunde inte hämta väderdata: ' + err.message);
      }
    },
    (err) => {
      const messages = {
        1: 'Du nekade åtkomst till din position.',
        2: 'Din position kunde inte bestämmas.',
        3: 'Tidsgräns för positionshämtning överskreds.',
      };
      showError(messages[err.code] || 'Okänt positionsfel.');
    },
    { timeout: 10000, maximumAge: 60000 }
  );
}

initSearch();
init();
