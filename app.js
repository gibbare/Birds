const SMHI_URL = (lon, lat) =>
  `https://opendata-download-metfcst.smhi.se/api/category/pmp3g/version/2/geotype/point/lon/${lon}/lat/${lat}/data.json`;

let _currentTimeSeries = null;
let _currentLat = null;
let _currentLon = null;

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

// ─── Moon & Sun helpers ───────────────────────────────────────────────────────

function moonPhaseEmoji(date) {
  const knownNewMoon = new Date(Date.UTC(2000, 0, 6, 18, 14, 0));
  const cycle = 29.53058867;
  const daysSince = (date - knownNewMoon) / 86400000;
  const f = (((daysSince % cycle) + cycle) % cycle) / cycle;
  if (f < 0.0625 || f >= 0.9375) return '🌑';
  if (f < 0.1875) return '🌒';
  if (f < 0.3125) return '🌓';
  if (f < 0.4375) return '🌔';
  if (f < 0.5625) return '🌕';
  if (f < 0.6875) return '🌖';
  if (f < 0.8125) return '🌗';
  return '🌘';
}

function sunriseSunset(date, lat, lon) {
  const D2R = Math.PI / 180;
  const jd = date.getTime() / 86400000 + 2440587.5;
  const n = Math.floor(jd - 2451545.0 + 0.5);
  const jStar = n - lon / 360;
  const M = ((357.5291 + 0.98560028 * jStar) % 360 + 360) % 360;
  const C = 1.9148 * Math.sin(M * D2R) + 0.02 * Math.sin(2 * M * D2R);
  const lam = ((M + C + 180 + 102.9372) % 360 + 360) % 360;
  const sinDec = Math.sin(lam * D2R) * Math.sin(23.45 * D2R);
  const cosDec = Math.cos(Math.asin(sinDec));
  const cosHa = (Math.sin(-0.833 * D2R) - Math.sin(lat * D2R) * sinDec) / (Math.cos(lat * D2R) * cosDec);
  const jTransit = 2451545.0 + jStar + 0.0053 * Math.sin(M * D2R) - 0.0069 * Math.sin(2 * lam * D2R);
  if (Math.abs(cosHa) > 1) {
    const d0 = new Date(date); d0.setHours(0,0,0,0);
    const d1 = new Date(date); d1.setHours(23,59,59,999);
    return cosHa > 1 ? { sunrise: null, sunset: null } : { sunrise: d0, sunset: d1 };
  }
  const ha = Math.acos(cosHa) * 180 / Math.PI;
  const toDate = j => new Date((j - 2440587.5) * 86400000);
  return { sunrise: toDate(jTransit - ha / 360), sunset: toDate(jTransit + ha / 360) };
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
  el.innerHTML = summaries.map((s, i) => `
    <div class="day-card" onclick="openDayDetail(${i})">
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

// ─── Day detail ───────────────────────────────────────────────────────────────

function openDayDetail(dayIndex) {
  if (!_currentTimeSeries) return;
  const days = {};
  _currentTimeSeries.forEach(ts => {
    const key = new Date(ts.validTime).toDateString();
    if (!days[key]) days[key] = [];
    days[key].push(ts);
  });
  const dayKey = Object.keys(days)[dayIndex];
  if (!dayKey) return;
  const dayData = days[dayKey].sort((a, b) => new Date(a.validTime) - new Date(b.validTime));
  const dayDate = new Date(dayData[0].validTime);
  document.getElementById('day-detail-title').textContent =
    dayDate.toLocaleDateString('sv-SE', { weekday: 'long', day: 'numeric', month: 'long' });
  drawDayDetailChart(dayData, _currentLat, _currentLon);
  document.getElementById('weather').classList.add('hidden');
  document.getElementById('day-detail').classList.remove('hidden');
}

function closeDayDetail() {
  document.getElementById('day-detail').classList.add('hidden');
  document.getElementById('weather').classList.remove('hidden');
}

function drawDayDetailChart(dayData, lat, lon) {
  const canvas = document.getElementById('detail-chart');
  const n = dayData.length;
  if (n < 1) return;

  const HOUR_SPACING = 46;
  const PAD_L = 36, PAD_R = 20;

  // Section y-coords
  const TIME_Y      = 14;
  const SUNMOON_Y   = 33;
  const WXEMOJI_Y   = 52;
  const TEMP_TOP    = 62;
  const TEMP_H      = 110;
  const TEMP_BOT    = TEMP_TOP + TEMP_H;
  const WIND_TOP    = TEMP_BOT + 14;
  const WIND_BAR_H  = 38;
  const WIND_SPD_Y  = WIND_TOP + WIND_BAR_H + 12;
  const WIND_ARR_Y  = WIND_SPD_Y + 16;
  const WIND_BOT    = WIND_ARR_Y + 12;
  const CLOUD_TOP   = WIND_BOT + 14;
  const CLOUD_BAR_H = 36;
  const CLOUD_PCT_Y = CLOUD_TOP + CLOUD_BAR_H + 11;
  const TOTAL_H     = CLOUD_PCT_Y + 10;

  const chartW = HOUR_SPACING * Math.max(n - 1, 1);
  const totalW = PAD_L + chartW + PAD_R;

  canvas.width  = totalW;
  canvas.height = TOTAL_H;
  canvas.style.width  = totalW + 'px';
  canvas.style.height = TOTAL_H + 'px';

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, totalW, TOTAL_H);

  const times   = dayData.map(ts => new Date(ts.validTime));
  const temps   = dayData.map(ts => getParam(ts, 't')        ?? 0);
  const winds   = dayData.map(ts => getParam(ts, 'ws')       ?? 0);
  const wdirs   = dayData.map(ts => getParam(ts, 'wd')       ?? 0);
  const clouds  = dayData.map(ts => getParam(ts, 'tcc_mean') ?? 0);
  const precips = dayData.map(ts => getParam(ts, 'pmean')    ?? 0);
  const symbols = dayData.map(ts => getParam(ts, 'Wsymb2')   ?? 1);

  const xOf = i => PAD_L + i * HOUR_SPACING;

  const { sunrise, sunset } = (lat != null && lon != null)
    ? sunriseSunset(times[0], lat, lon)
    : { sunrise: null, sunset: null };

  // Section bg
  [[TEMP_TOP, TEMP_H, '80,120,200'], [WIND_TOP, WIND_BOT - WIND_TOP, '80,180,160'], [CLOUD_TOP, CLOUD_PCT_Y - CLOUD_TOP + 8, '140,140,200']].forEach(([y, h, rgb]) => {
    ctx.fillStyle = `rgba(${rgb},0.06)`;
    ctx.beginPath(); ctx.roundRect(PAD_L, y, chartW, h, 4); ctx.fill();
  });

  // Section axis labels
  ctx.font = '8px system-ui'; ctx.textAlign = 'right'; ctx.fillStyle = 'rgba(100,130,180,0.6)';
  ctx.fillText('°C', PAD_L - 4, TEMP_TOP + 8);
  ctx.fillText('m/s', PAD_L - 4, WIND_TOP + 10);
  ctx.fillText('moln', PAD_L - 4, CLOUD_TOP + 10);

  // Per-column: time, sun/moon, weather emoji
  times.forEach((t, i) => {
    const x = xOf(i);
    const isDay = sunrise && sunset ? (t >= sunrise && t <= sunset) : true;
    ctx.fillStyle = 'rgba(140,165,210,0.8)';
    ctx.font = '10px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(t.getHours().toString().padStart(2,'0') + ':00', x, TIME_Y);
    ctx.font = '13px serif';
    ctx.fillText(isDay ? '☀️' : moonPhaseEmoji(t), x, SUNMOON_Y);
    ctx.fillText(WEATHER_EMOJI[symbols[i]] || '🌡️', x, WXEMOJI_Y);
  });

  // Temperature
  const tMin = Math.floor(Math.min(...temps)) - 1;
  const tMax = Math.ceil(Math.max(...temps)) + 2;
  const tRange = tMax - tMin || 1;
  const yTemp = t => TEMP_TOP + TEMP_H - ((t - tMin) / tRange) * TEMP_H;
  const tempPts = temps.map((t, i) => ({ x: xOf(i), y: yTemp(t) }));

  for (let t = Math.ceil(tMin); t <= tMax; t += 2) {
    const y = yTemp(t);
    if (y < TEMP_TOP - 2 || y > TEMP_BOT + 2) continue;
    ctx.strokeStyle = t === 0 ? 'rgba(126,184,247,0.3)' : 'rgba(255,255,255,0.06)';
    ctx.lineWidth = 1; ctx.setLineDash(t === 0 ? [5,3] : []);
    ctx.beginPath(); ctx.moveTo(PAD_L, y); ctx.lineTo(PAD_L + chartW, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = t === 0 ? 'rgba(126,184,247,0.8)' : 'rgba(140,165,210,0.55)';
    ctx.font = (t === 0 ? 'bold ' : '') + '9px system-ui'; ctx.textAlign = 'right';
    ctx.fillText(t + '°', PAD_L - 4, y + 3);
  }

  // Precip bars
  const maxP = Math.max(...precips, 0.5);
  precips.forEach((p, i) => {
    if (p <= 0) return;
    const bh = Math.max((p / maxP) * TEMP_H * 0.25, 3);
    const bw = HOUR_SPACING * 0.5;
    ctx.fillStyle = 'rgba(80,160,255,0.45)';
    ctx.beginPath(); ctx.roundRect(xOf(i) - bw/2, TEMP_BOT - bh, bw, bh, 2); ctx.fill();
  });

  // Gradient fill under temp curve
  if (n > 1) {
    const fg = ctx.createLinearGradient(0, TEMP_TOP, 0, TEMP_BOT);
    fg.addColorStop(0, 'rgba(126,184,247,0.4)');
    fg.addColorStop(0.6, 'rgba(126,184,247,0.1)');
    fg.addColorStop(1, 'rgba(126,184,247,0)');
    ctx.beginPath(); smoothPath(ctx, tempPts);
    ctx.lineTo(tempPts[tempPts.length-1].x, TEMP_BOT);
    ctx.lineTo(tempPts[0].x, TEMP_BOT);
    ctx.closePath(); ctx.fillStyle = fg; ctx.fill();

    const lg = ctx.createLinearGradient(PAD_L, 0, PAD_L + chartW, 0);
    temps.forEach((t, i) => lg.addColorStop(i / (temps.length - 1), tempColor(t)));
    ctx.beginPath(); smoothPath(ctx, tempPts);
    ctx.strokeStyle = lg; ctx.lineWidth = 2.5; ctx.lineJoin = 'round'; ctx.stroke();
  }

  tempPts.forEach((pt, i) => {
    ctx.shadowColor = tempColor(temps[i]); ctx.shadowBlur = 5;
    ctx.beginPath(); ctx.arc(pt.x, pt.y, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = tempColor(temps[i]); ctx.strokeStyle = 'rgba(255,255,255,0.85)';
    ctx.lineWidth = 1.5; ctx.fill(); ctx.stroke(); ctx.shadowBlur = 0;
    ctx.fillStyle = '#e8f0ff'; ctx.font = 'bold 10px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(Math.round(temps[i]) + '°', pt.x, pt.y - 7);
  });

  // Wind bars + speed + direction arrow
  const maxW = Math.max(...winds, 1);
  winds.forEach((w, i) => {
    const x = xOf(i);
    const bh = Math.max((w / maxW) * WIND_BAR_H, 2);
    const bw = HOUR_SPACING * 0.5;
    const strong = w > 15;
    ctx.fillStyle = strong ? 'rgba(220,80,80,0.5)' : 'rgba(100,170,255,0.4)';
    ctx.beginPath(); ctx.roundRect(x - bw/2, WIND_TOP + WIND_BAR_H - bh, bw, bh, 2); ctx.fill();
    ctx.fillStyle = strong ? 'rgba(255,140,140,0.9)' : 'rgba(160,176,208,0.85)';
    ctx.font = 'bold 9px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(w.toFixed(1), x, WIND_SPD_Y);
    // Direction arrow (points in wind direction)
    const toRad = ((wdirs[i] + 180) % 360) * Math.PI / 180;
    const AL = 7, HL = 4, HA = Math.PI / 5;
    const hx = x + Math.sin(toRad) * AL, hy = WIND_ARR_Y - Math.cos(toRad) * AL;
    const tx = x - Math.sin(toRad) * AL, ty = WIND_ARR_Y + Math.cos(toRad) * AL;
    ctx.strokeStyle = 'rgba(140,190,255,0.75)'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(hx, hy);
    ctx.lineTo(hx - HL*Math.sin(toRad-HA), hy + HL*Math.cos(toRad-HA));
    ctx.moveTo(hx, hy);
    ctx.lineTo(hx - HL*Math.sin(toRad+HA), hy + HL*Math.cos(toRad+HA));
    ctx.stroke();
  });

  // Cloud cover bars
  clouds.forEach((c, i) => {
    const x = xOf(i), frac = c / 8;
    const bw = HOUR_SPACING * 0.6;
    ctx.fillStyle = 'rgba(255,255,255,0.05)';
    ctx.beginPath(); ctx.roundRect(x - bw/2, CLOUD_TOP + 14, bw, CLOUD_BAR_H, 3); ctx.fill();
    const fillH = CLOUD_BAR_H * frac;
    if (fillH > 0) {
      ctx.fillStyle = `rgba(150,180,230,${0.15 + frac * 0.55})`;
      ctx.beginPath(); ctx.roundRect(x - bw/2, CLOUD_TOP + 14 + CLOUD_BAR_H - fillH, bw, fillH, 3); ctx.fill();
    }
    ctx.fillStyle = 'rgba(160,176,208,0.85)';
    ctx.font = '9px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(Math.round(frac * 100) + '%', x, CLOUD_PCT_Y);
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

function saveLocation(lat, lon, name) {
  try { localStorage.setItem('wx_location', JSON.stringify({ lat, lon, name })); } catch {}
}

async function loadWeatherForCoords(lat, lon, placeName) {
  _currentLat = lat;
  _currentLon = lon;
  saveLocation(lat, lon, placeName);
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
  _currentTimeSeries = data.timeSeries;
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

  // Restore last used location if available
  try {
    const saved = localStorage.getItem('wx_location');
    if (saved) {
      const { lat, lon, name } = JSON.parse(saved);
      loadWeatherForCoords(lat, lon, name);
      return;
    }
  } catch {}

  document.getElementById('location-name').textContent = 'Hämtar din position...';

  if (!navigator.geolocation) {
    showError('Din webbläsare stöder inte geolokalisering.');
    return;
  }

  navigator.geolocation.getCurrentPosition(
    async ({ coords }) => {
      const { latitude: lat, longitude: lon } = coords;
      _currentLat = lat;
      _currentLon = lon;
      try {
        const [placeName, weatherData] = await Promise.all([
          reverseGeocode(lat, lon),
          fetchWeather(lat, lon),
        ]);
        document.getElementById('location-name').textContent = placeName;
        saveLocation(lat, lon, placeName);
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
