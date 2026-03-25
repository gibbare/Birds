/**
 * Väder & Blixt – Railway backend
 *
 * • Serves static files (index.html, app.js, style.css, …)
 * • Maintains a permanent WebSocket connection to Blitzortung
 * • Stores the last 30 minutes of global lightning strikes in Redis
 *   (survives server restarts, deploys and Railway sleep/wake cycles)
 * • GET /api/strikes  → JSON array of recent strikes
 * • GET /api/status   → health check
 */

const express   = require('express');
const WebSocket = require('ws');
const Redis     = require('ioredis');
const path      = require('path');

const app        = express();
const PORT       = process.env.PORT || 3000;
const STRIKE_TTL = 30 * 60;          // 30 minutes in seconds
const REDIS_KEY  = 'blitz:strikes';  // sorted set – score = timestamp ms

// ── Redis ─────────────────────────────────────────────────────────────────────
const redis = new Redis(process.env.REDIS_URL || 'redis://localhost:6379', {
  maxRetriesPerRequest: 3,
  lazyConnect: false,
});

redis.on('connect', () => console.log('[Redis] Connected'));
redis.on('error',   e  => console.error('[Redis] Error:', e.message));

// Remove strikes older than 30 minutes
async function pruneRedis() {
  const cutoff = Date.now() - STRIKE_TTL * 1000;
  await redis.zremrangebyscore(REDIS_KEY, '-inf', cutoff);
}

// Add one strike to Redis (fire-and-forget)
async function saveStrike(strike) {
  const score = strike.time;
  const value = JSON.stringify(strike);
  await redis.zadd(REDIS_KEY, score, value);
  // Set TTL on the key so Redis cleans up automatically if server is idle
  await redis.expire(REDIS_KEY, STRIKE_TTL * 2);
}

// Get all strikes from the last 30 minutes
async function getStrikes(since = 0) {
  const cutoff = since > 0 ? since : Date.now() - STRIKE_TTL * 1000;
  const raw    = await redis.zrangebyscore(REDIS_KEY, cutoff, '+inf');
  return raw.map(r => {
    try { return JSON.parse(r); } catch { return null; }
  }).filter(Boolean);
}

// Prune Redis every 5 minutes
setInterval(pruneRedis, 5 * 60 * 1000);

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

let wsIdx         = 0;
let blitzWs       = null;
let reconnectTimer = null;

function connectBlitzortung() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  const url = WS_SERVERS[wsIdx % WS_SERVERS.length];
  console.log(`[Blitzortung] Connecting to ${url}`);

  try { blitzWs = new WebSocket(url); } catch (err) {
    console.error('[Blitzortung] Socket error:', err.message);
    scheduleReconnect(); return;
  }

  // If no data within 10 s, try next server
  const noDataTimer = setTimeout(() => {
    console.warn(`[Blitzortung] No data from ${url}, switching`);
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
      const strike = JSON.parse(blitzDecode(text));

      if (strike.lat != null && strike.lon != null) {
        saveStrike({
          lat:  strike.lat,
          lon:  strike.lon,
          time: Date.now(),
          data: {
            pol:   strike.pol,
            alt:   strike.alt,
            delay: strike.delay,
            sig:   strike.sig ? strike.sig.slice(0, 5) : undefined,
          },
        }).catch(() => {});
      }
    } catch { /* ignore malformed frames */ }
  });

  blitzWs.on('error', err => {
    clearTimeout(noDataTimer);
    console.error(`[Blitzortung] Error:`, err.message);
    wsIdx++;
    scheduleReconnect(2000);
  });

  blitzWs.on('close', () => {
    clearTimeout(noDataTimer);
    console.log(`[Blitzortung] Closed, reconnecting…`);
    scheduleReconnect(5000);
  });
}

function scheduleReconnect(delay = 5000) {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => { reconnectTimer = null; connectBlitzortung(); }, delay);
}

// ── HTTP API ──────────────────────────────────────────────────────────────────
app.use(express.static(path.join(__dirname)));

app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  next();
});

/**
 * GET /api/strikes
 * Returns strikes from the last 30 minutes (or since ?since=<unix_ms>).
 */
app.get('/api/strikes', async (req, res) => {
  try {
    const since   = req.query.since ? parseInt(req.query.since, 10) : 0;
    const strikes = await getStrikes(since);
    res.json(strikes);
  } catch (err) {
    console.error('[API] /api/strikes error:', err.message);
    res.status(500).json({ error: 'Redis unavailable' });
  }
});

/**
 * GET /api/status
 * Health check: connection state + buffer size.
 */
app.get('/api/status', async (req, res) => {
  try {
    const count  = await redis.zcount(REDIS_KEY, Date.now() - STRIKE_TTL * 1000, '+inf');
    const oldest = await redis.zrangebyscore(REDIS_KEY, '-inf', '+inf', 'LIMIT', 0, 1);
    res.json({
      blitzortung:  blitzWs?.readyState === WebSocket.OPEN ? 'connected' : 'disconnected',
      server:       WS_SERVERS[wsIdx % WS_SERVERS.length],
      redis:        redis.status,
      buffered:     count,
      oldestStrike: oldest[0] ? JSON.parse(oldest[0]).time : null,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`Server listening on port ${PORT}`);
  connectBlitzortung();
});
