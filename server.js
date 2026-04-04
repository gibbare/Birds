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
const REDIS_URL = process.env.REDIS_URL || process.env.REDIS_PRIVATE_URL;
if (!REDIS_URL) console.warn('[Redis] No REDIS_URL env var – strikes will not persist across restarts');

const redisOptions = {
  maxRetriesPerRequest: 1,
  retryStrategy: times => Math.min(times * 1000, 10_000),
  lazyConnect: true,
};
// Railway Redis may require TLS (rediss://)
if (REDIS_URL && REDIS_URL.startsWith('rediss://')) {
  redisOptions.tls = { rejectUnauthorized: false };
}

const redis = REDIS_URL
  ? new Redis(REDIS_URL, redisOptions)
  : new Redis('redis://localhost:6379', { ...redisOptions, lazyConnect: true });

redis.on('connect', () => console.log('[Redis] Connected to', REDIS_URL ? REDIS_URL.replace(/:\/\/.*@/, '://***@') : 'localhost'));
redis.on('error',   e  => console.error('[Redis] Error:', e.message));

// Fallback in-memory buffer used when Redis is unavailable
let memBuffer = [];
let redisReady = false;
redis.connect().then(() => { redisReady = true; }).catch(() => {
  console.warn('[Redis] Could not connect – using in-memory fallback');
});

// Remove strikes older than 30 minutes
async function pruneOld() {
  const cutoff = Date.now() - STRIKE_TTL * 1000;
  if (redisReady) {
    await redis.zremrangebyscore(REDIS_KEY, '-inf', cutoff).catch(() => {});
  } else {
    memBuffer = memBuffer.filter(s => s.time > cutoff);
  }
}

// Add one strike (fire-and-forget)
let savedCount = 0;
let saveErrors = 0;
async function saveStrike(strike) {
  if (redisReady) {
    try {
      await redis.zadd(REDIS_KEY, strike.time, JSON.stringify(strike));
      await redis.expire(REDIS_KEY, STRIKE_TTL * 2);
      savedCount++;
    } catch (err) {
      saveErrors++;
      if (saveErrors <= 5) console.error('[Redis] zadd failed:', err.message);
      // fall back to memory
      memBuffer.push(strike);
    }
  } else {
    memBuffer.push(strike);
    if (memBuffer.length > 30000) memBuffer = memBuffer.slice(-25000);
  }
}

// Get strikes from the last 30 minutes (or since a timestamp)
async function getStrikes(since = 0) {
  const cutoff = since > 0 ? since : Date.now() - STRIKE_TTL * 1000;
  if (redisReady) {
    const raw = await redis.zrangebyscore(REDIS_KEY, cutoff, '+inf').catch(() => []);
    return raw.map(r => { try { return JSON.parse(r); } catch { return null; } }).filter(Boolean);
  }
  return memBuffer.filter(s => s.time > cutoff);
}

// Prune every 5 minutes
setInterval(pruneOld, 5 * 60 * 1000);

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

let wsIdx          = 0;
let blitzWs        = null;
let reconnectTimer = null;
let msgReceived    = 0;
let msgDecodeOk    = 0;
let msgDecodeErr   = 0;
let lastDecodeErr  = null;

function connectBlitzortung() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  const url = WS_SERVERS[wsIdx % WS_SERVERS.length];
  console.log(`[Blitzortung] Connecting to ${url}`);

  try { blitzWs = new WebSocket(url, { skipUTF8Validation: true }); } catch (err) {
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
    msgReceived++;
    clearTimeout(noDataTimer);
    try {
      // ws with skipUTF8Validation delivers data as Buffer – decode as UTF-8
      // to preserve multi-byte LZW code points (same as browser behaviour)
      let text;
      if (Buffer.isBuffer(raw))       text = raw.toString('utf8');
      else if (typeof raw === 'string') text = raw;
      else                              text = Buffer.from(raw).toString('utf8');

      const strike = JSON.parse(blitzDecode(text));
      msgDecodeOk++;
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
        });
      }
    } catch (e) {
      msgDecodeErr++;
      lastDecodeErr = e.message;
      if (msgDecodeErr <= 3) {
        console.error('[Blitzortung] Decode error:', e.message,
          '| isBuffer:', Buffer.isBuffer(raw),
          '| length:', raw?.length ?? raw?.byteLength);
      }
    }
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

// ── News Cache ────────────────────────────────────────────────────────────────
const NEWS_FEEDS = [
  { source: 'SVT',         url: 'https://www.svt.se/nyheter/inrikes/rss.xml',                       defCat: 'Inrikes' },
  { source: 'SVT',         url: 'https://www.svt.se/nyheter/utrikes/rss.xml',                       defCat: 'Utrikes' },
  { source: 'SVT',         url: 'https://www.svt.se/sport/rss.xml',                                 defCat: 'Sport'   },
  { source: 'Aftonbladet', url: 'https://rss.aftonbladet.se/rss2/small/pages/sections/senastenytt', defCat: 'Nyheter' },
  { source: 'Expressen',   url: 'https://feeds.expressen.se/nyheter',                               defCat: 'Nyheter' },
  { source: 'Expressen',   url: 'https://feeds.expressen.se/sport',                                 defCat: 'Sport'   },
  { source: 'DN',          url: 'https://www.dn.se/rss',                                            defCat: 'Nyheter' },
  { source: 'SvD',         url: 'https://www.svd.se/feed/articles.rss',                             defCat: 'Nyheter' },
];
const SR_PROGRAMS = [
  { id: 4540, cat: 'Nyheter'   },
  { id: 83,   cat: 'Nyheter'   },
  { id: 406,  cat: 'Samhälle'  },
  { id: 164,  cat: 'Nyheter'   },
];

let newsCache = { items: [], refreshed: null };

function parseSRDate(raw) {
  if (!raw) return null;
  const m = raw.match(/\/Date\((\d+)\)\//);
  return m ? new Date(parseInt(m[1])).toISOString() : null;
}

function parseRSSXml(xml, source, defCat) {
  const items = [];
  const itemRe = /<item>([\s\S]*?)<\/item>/g;
  let m;
  while ((m = itemRe.exec(xml)) !== null) {
    const block = m[1];
    const get = tag => {
      const r = new RegExp(`<${tag}[^>]*>(?:<!\\[CDATA\\[)?([\\s\\S]*?)(?:\\]\\]>)?<\\/${tag}>`, 'i');
      const mm = r.exec(block);
      return mm ? mm[1].replace(/<[^>]+>/g, '').trim() : '';
    };
    const title = get('title');
    const link  = get('link') || get('guid');
    const pubStr = get('pubDate');
    const cat   = get('category') || defCat;
    if (!title) continue;
    items.push({ id: `rss-${source}-${link||title}`, title, url: link,
      pubDate: pubStr ? new Date(pubStr).toISOString() : null, source, category: cat });
  }
  return items;
}

async function fetchSRForDate(dateStr) {
  const items = [];
  await Promise.allSettled(SR_PROGRAMS.map(async prog => {
    try {
      const url = `https://api.sr.se/api/v2/episodes/index?programid=${prog.id}&format=json&fromdate=${dateStr}&todate=${dateStr}&size=50`;
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      (data.episodes || []).forEach(ep => items.push({
        id: `sr-${ep.id}`, title: ep.title || '', url: ep.url || '',
        pubDate: parseSRDate(ep.publishdateutc), source: 'SR', category: prog.cat,
      }));
    } catch (e) { console.warn('[News] SR fetch failed:', e.message); }
  }));
  return items;
}

async function refreshNews() {
  console.log('[News] Refreshing cache…');
  const today     = new Date().toISOString().slice(0, 10);
  const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);

  const results = await Promise.allSettled([
    ...NEWS_FEEDS.map(async feed => {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 10000);
      const res = await fetch(feed.url, { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: ctrl.signal }).finally(() => clearTimeout(timer));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return parseRSSXml(await res.text(), feed.source, feed.defCat);
    }),
    fetchSRForDate(today),
    fetchSRForDate(yesterday),
  ]);

  const all = [];
  results.forEach(r => { if (r.status === 'fulfilled') all.push(...r.value); });

  const seen = new Set();
  newsCache = {
    items: all.filter(item => { const k = item.title.toLowerCase().slice(0,60); if (seen.has(k)) return false; seen.add(k); return true; }),
    refreshed: new Date().toISOString(),
  };
  console.log(`[News] Cache updated: ${newsCache.items.length} items`);
}

refreshNews();
setInterval(refreshNews, 30 * 60 * 1000);

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
app.get('/api/news', async (req, res) => {
  try {
    const date = req.query.date;
    const today = new Date().toISOString().slice(0, 10);
    if (date && date !== today) {
      // Historical date: fetch SR on demand, combine with cached RSS
      const srItems  = await fetchSRForDate(date);
      const rssItems = newsCache.items.filter(i => i.source !== 'SR');
      return res.json({ items: [...srItems, ...rssItems], refreshed: newsCache.refreshed });
    }
    res.json(newsCache);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

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
    const cutoff = Date.now() - STRIKE_TTL * 1000;
    let count = 0, oldestTime = null;
    if (redisReady) {
      count = await redis.zcount(REDIS_KEY, cutoff, '+inf').catch(() => 0);
      const oldest = await redis.zrangebyscore(REDIS_KEY, '-inf', '+inf', 'LIMIT', 0, 1).catch(() => []);
      oldestTime = oldest[0] ? JSON.parse(oldest[0]).time : null;
    } else {
      count = memBuffer.filter(s => s.time > cutoff).length;
      oldestTime = memBuffer[0]?.time ?? null;
    }
    // Quick Redis write/read test
    let redisWriteTest = 'not tested';
    if (redisReady) {
      try {
        await redis.set('blitz:test', '1', 'EX', 10);
        const val = await redis.get('blitz:test');
        redisWriteTest = val === '1' ? 'ok' : 'read mismatch';
      } catch (e) {
        redisWriteTest = 'FAILED: ' + e.message;
      }
    }

    res.json({
      blitzortung:   blitzWs?.readyState === WebSocket.OPEN ? 'connected' : 'disconnected',
      server:        WS_SERVERS[wsIdx % WS_SERVERS.length],
      redis:         redisReady ? 'connected' : 'unavailable (in-memory fallback)',
      redisWriteTest,
      msgReceived,
      msgDecodeOk,
      msgDecodeErr,
      lastDecodeErr,
      savedCount,
      saveErrors,
      buffered:      count,
      memBuffered:   memBuffer.length,
      oldestStrike:  oldestTime,
      cacheMinutes:  oldestTime ? Math.floor((Date.now() - oldestTime) / 60000) : 0,
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
