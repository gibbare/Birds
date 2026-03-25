/**
 * Väder & Blixt – Railway backend
 *
 * • Serves static files (index.html, app.js, style.css, …)
 * • Maintains a permanent WebSocket connection to Blitzortung
 * • Buffers the last 30 minutes of global lightning strikes in memory
 * • Exposes GET /api/strikes  →  JSON array of recent strikes
 */

const express  = require('express');
const WebSocket = require('ws');
const path     = require('path');

const app          = express();
const PORT         = process.env.PORT || 3000;
const STRIKE_TTL   = 30 * 60 * 1000;   // 30 minutes in ms
const MAX_BUFFER   = 30000;             // hard cap to keep RAM sane

// ── Strike buffer ─────────────────────────────────────────────────────────────
let strikeBuffer = [];

function pruneBuffer() {
  const cutoff = Date.now() - STRIKE_TTL;
  strikeBuffer = strikeBuffer.filter(s => s.time > cutoff);
  // Also cap absolute size
  if (strikeBuffer.length > MAX_BUFFER) {
    strikeBuffer = strikeBuffer.slice(-MAX_BUFFER);
  }
}

// Prune every 60 seconds
setInterval(pruneBuffer, 60_000);

// ── LZW decompression (identical to client-side blitzDecode) ─────────────────
function blitzDecode(data) {
  const e = {};
  const d = [...data];
  let c = d[0], f = c;
  const g = [c];
  const h = 256;
  let o = h;
  for (let i = 1; i < d.length; i++) {
    const code = d[i].charCodeAt(0);
    const a = code < h ? d[i] : (e.hasOwnProperty(code) ? e[code] : (f + c));
    g.push(a);
    c = a[0];
    e[o++] = f + c;
    f = a;
  }
  return g.join('');
}

// ── Blitzortung WebSocket connection ──────────────────────────────────────────
const WS_SERVERS = [
  'wss://ws1.blitzortung.org/',
  'wss://ws3.blitzortung.org/',
  'wss://ws7.blitzortung.org/',
  'wss://ws8.blitzortung.org/',
];

let wsIdx = 0;
let blitzWs = null;
let reconnectTimer = null;

function connectBlitzortung() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  const url = WS_SERVERS[wsIdx % WS_SERVERS.length];
  console.log(`[Blitzortung] Connecting to ${url}`);

  try { blitzWs = new WebSocket(url); } catch (err) {
    console.error('[Blitzortung] Could not create socket:', err.message);
    scheduleReconnect();
    return;
  }

  // Timeout: if no data within 10 s, try next server
  const noDataTimer = setTimeout(() => {
    console.warn(`[Blitzortung] No data from ${url}, trying next`);
    blitzWs.terminate();
    wsIdx++;
    scheduleReconnect(1000);
  }, 10_000);

  blitzWs.on('open', () => {
    blitzWs.send(JSON.stringify({ a: 111 }));
  });

  blitzWs.on('message', raw => {
    clearTimeout(noDataTimer);
    try {
      const text   = typeof raw === 'string' ? raw : raw.toString('binary');
      const decoded = blitzDecode(text);
      const strike  = JSON.parse(decoded);

      if (strike.lat != null && strike.lon != null) {
        strikeBuffer.push({
          lat:  strike.lat,
          lon:  strike.lon,
          time: Date.now(),
          data: {
            pol:   strike.pol,
            alt:   strike.alt,
            delay: strike.delay,
            sig:   strike.sig ? strike.sig.slice(0, 5) : undefined,
          },
        });
      }
    } catch { /* ignore malformed frames */ }
  });

  blitzWs.on('error', err => {
    clearTimeout(noDataTimer);
    console.error(`[Blitzortung] Error on ${url}:`, err.message);
    wsIdx++;
    scheduleReconnect(2000);
  });

  blitzWs.on('close', () => {
    clearTimeout(noDataTimer);
    console.log(`[Blitzortung] Connection closed, reconnecting…`);
    scheduleReconnect(5000);
  });
}

function scheduleReconnect(delay = 5000) {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connectBlitzortung(); }, delay);
}

// ── HTTP API ──────────────────────────────────────────────────────────────────
// Serve static files from the same directory
app.use(express.static(path.join(__dirname)));

// CORS header so the browser can call the API from any origin during dev
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  next();
});

/**
 * GET /api/strikes
 * Returns all strikes buffered in the last 30 minutes.
 * Optional query param: ?since=<unix_ms>  to fetch only newer strikes.
 */
app.get('/api/strikes', (req, res) => {
  pruneBuffer();
  const since  = req.query.since ? parseInt(req.query.since, 10) : 0;
  const result = since > 0
    ? strikeBuffer.filter(s => s.time > since)
    : strikeBuffer;
  res.json(result);
});

/**
 * GET /api/status
 * Simple health-check endpoint.
 */
app.get('/api/status', (req, res) => {
  res.json({
    connected:    blitzWs?.readyState === WebSocket.OPEN,
    server:       WS_SERVERS[wsIdx % WS_SERVERS.length],
    buffered:     strikeBuffer.length,
    oldestStrike: strikeBuffer[0]?.time ?? null,
  });
});

// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
  connectBlitzortung();
});
