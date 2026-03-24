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
    return addr.city || addr.town || addr.village || addr.municipality || addr.county || 'Okänd plats';
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
  // Maps temperature to a colour: cold=blue, mild=teal, warm=orange, hot=red
  if (t <= 0)  return '#7eb8f7';
  if (t <= 10) return '#5dd6c0';
  if (t <= 20) return '#f7c948';
  return '#f77b5e';
}

function smoothPath(ctx, pts) {
  // Catmull-Rom → cubic bezier
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

function drawForecastChart(timeSeries) {
  const canvas = document.getElementById('forecast-chart');
  const wrap   = canvas.parentElement;

  // ── filter: every 4 hours, up to 72 h ──────────────────────────────────────
  const now    = new Date(timeSeries[0].validTime);
  const cutoff = new Date(now.getTime() + 72 * 3600 * 1000);

  const points4h = timeSeries.filter(ts => {
    const d = new Date(ts.validTime);
    return d <= cutoff && d.getHours() % 4 === 0;
  });
  if (points4h.length < 2) return;

  // ── canvas size ─────────────────────────────────────────────────────────────
  const PT_SPACING = 52;           // px between data points
  const PAD = { top: 72, right: 20, bottom: 48, left: 38 };
  const chartW = PT_SPACING * (points4h.length - 1);
  const totalW = chartW + PAD.left + PAD.right;
  const totalH = 280;
  const chartH = totalH - PAD.top - PAD.bottom;

  canvas.width  = totalW;
  canvas.height = totalH;
  canvas.style.width  = totalW + 'px';
  canvas.style.height = totalH + 'px';

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, totalW, totalH);

  // ── data extraction ─────────────────────────────────────────────────────────
  const temps  = points4h.map(ts => getParam(ts, 't') ?? 0);
  const precips = points4h.map(ts => getParam(ts, 'pmean') ?? 0);
  const symbols = points4h.map(ts => getParam(ts, 'Wsymb2') ?? 1);
  const times   = points4h.map(ts => new Date(ts.validTime));

  const tMin = Math.floor(Math.min(...temps)) - 2;
  const tMax = Math.ceil(Math.max(...temps))  + 3;
  const tRange = tMax - tMin || 1;

  const xOf = i => PAD.left + i * PT_SPACING;
  const yOf = t => PAD.top + chartH - ((t - tMin) / tRange) * chartH;

  const pts = temps.map((t, i) => ({ x: xOf(i), y: yOf(t) }));

  // ── horizontal grid lines ───────────────────────────────────────────────────
  ctx.strokeStyle = 'rgba(255,255,255,0.07)';
  ctx.lineWidth = 1;
  for (let t = Math.ceil(tMin); t <= tMax; t += 2) {
    const y = yOf(t);
    ctx.beginPath();
    ctx.moveTo(PAD.left, y);
    ctx.lineTo(PAD.left + chartW, y);
    ctx.stroke();
    // axis label
    ctx.fillStyle = 'rgba(160,176,208,0.7)';
    ctx.font = '10px system-ui';
    ctx.textAlign = 'right';
    ctx.fillText(t + '°', PAD.left - 4, y + 3.5);
  }

  // ── precipitation bars ──────────────────────────────────────────────────────
  const maxPrecip = Math.max(...precips, 0.5);
  const barMaxH   = chartH * 0.3;
  precips.forEach((p, i) => {
    if (p <= 0) return;
    const bh = (p / maxPrecip) * barMaxH;
    const bw = PT_SPACING * 0.55;
    ctx.fillStyle = 'rgba(100,180,255,0.35)';
    ctx.beginPath();
    ctx.roundRect(xOf(i) - bw / 2, PAD.top + chartH - bh, bw, bh, 3);
    ctx.fill();
  });

  // ── day separator lines ─────────────────────────────────────────────────────
  let lastDay = times[0].getDate();
  times.forEach((d, i) => {
    if (i === 0) return;
    if (d.getDate() !== lastDay) {
      lastDay = d.getDate();
      const x = xOf(i) - PT_SPACING / 2;
      ctx.strokeStyle = 'rgba(255,255,255,0.18)';
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, PAD.top);
      ctx.lineTo(x, PAD.top + chartH);
      ctx.stroke();
      ctx.setLineDash([]);

      // Day name label at top of separator
      const dayName = d.toLocaleDateString('sv-SE', { weekday: 'short' });
      ctx.fillStyle = 'rgba(200,220,255,0.6)';
      ctx.font = 'bold 10px system-ui';
      ctx.textAlign = 'center';
      ctx.fillText(dayName.toUpperCase(), x, PAD.top - 6);
    }
  });

  // ── gradient fill under curve ────────────────────────────────────────────────
  const fillGrad = ctx.createLinearGradient(0, PAD.top, 0, PAD.top + chartH);
  fillGrad.addColorStop(0,   'rgba(126,184,247,0.35)');
  fillGrad.addColorStop(1,   'rgba(126,184,247,0)');
  ctx.beginPath();
  smoothPath(ctx, pts);
  ctx.lineTo(pts[pts.length - 1].x, PAD.top + chartH);
  ctx.lineTo(pts[0].x, PAD.top + chartH);
  ctx.closePath();
  ctx.fillStyle = fillGrad;
  ctx.fill();

  // ── temperature curve ────────────────────────────────────────────────────────
  const lineGrad = ctx.createLinearGradient(PAD.left, 0, PAD.left + chartW, 0);
  temps.forEach((t, i) => {
    lineGrad.addColorStop(i / (temps.length - 1), tempColor(t));
  });
  ctx.beginPath();
  smoothPath(ctx, pts);
  ctx.strokeStyle = lineGrad;
  ctx.lineWidth   = 2.5;
  ctx.lineJoin    = 'round';
  ctx.stroke();

  // ── dots + temperature labels + emoji ───────────────────────────────────────
  pts.forEach((pt, i) => {
    // dot
    ctx.beginPath();
    ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2);
    ctx.fillStyle   = tempColor(temps[i]);
    ctx.strokeStyle = 'rgba(255,255,255,0.8)';
    ctx.lineWidth   = 1.5;
    ctx.fill();
    ctx.stroke();

    // temperature label above dot
    ctx.fillStyle   = '#e0eaff';
    ctx.font        = 'bold 11px system-ui';
    ctx.textAlign   = 'center';
    ctx.fillText(Math.round(temps[i]) + '°', pt.x, pt.y - 10);

    // emoji above label
    ctx.font        = '16px serif';
    ctx.fillText(WEATHER_EMOJI[symbols[i]] || '🌡️', pt.x, pt.y - 24);

    // time label below chart
    const h = times[i].getHours().toString().padStart(2, '0');
    ctx.fillStyle = 'rgba(160,176,208,0.85)';
    ctx.font      = '10px system-ui';
    ctx.fillText(h + ':00', pt.x, PAD.top + chartH + 14);
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
  document.getElementById('wind').textContent         = wind != null ? wind.toFixed(1) : '--';
  document.getElementById('humidity').textContent     = humidity != null ? Math.round(humidity) : '--';
  document.getElementById('precip').textContent       = precip != null ? precip.toFixed(1) : '--';
  document.getElementById('feels-like').textContent   = wind != null ? feelsLike(temp, wind) : Math.round(temp);
  document.getElementById('weather-desc').textContent = WEATHER_SYMBOLS[symbol] || '--';

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

init();
