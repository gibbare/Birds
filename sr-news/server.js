const express = require('express');
const path    = require('path');

const app  = express();
const PORT = process.env.PORT || 3000;

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
  { source: 'Norran',      url: 'https://www.norran.se/rss',                                        defCat: 'Nyheter' },
];
const SR_PROGRAMS = [
  { id: 4540, cat: 'Nyheter'  },
  { id: 83,   cat: 'Nyheter'  },
  { id: 406,  cat: 'Samhälle' },
  { id: 164,  cat: 'Nyheter'  },
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
    const title  = get('title');
    const link   = get('link') || get('guid');
    const pubStr = get('pubDate');
    const cat    = get('category') || defCat;
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
      const ctrl  = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 10000);
      const res   = await fetch(feed.url, {
        headers: { 'User-Agent': 'Mozilla/5.0' },
        signal: ctrl.signal,
      }).finally(() => clearTimeout(timer));
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
    items: all.filter(item => {
      const k = item.title.toLowerCase().slice(0, 60);
      if (seen.has(k)) return false;
      seen.add(k); return true;
    }),
    refreshed: new Date().toISOString(),
  };
  console.log(`[News] Cache updated: ${newsCache.items.length} items`);
}

refreshNews();
setInterval(refreshNews, 30 * 60 * 1000);

// ── HTTP ──────────────────────────────────────────────────────────────────────
app.use(express.static(path.join(__dirname)));

app.get('/api/news', async (req, res) => {
  try {
    const date  = req.query.date;
    const today = new Date().toISOString().slice(0, 10);
    if (date && date !== today) {
      const srItems  = await fetchSRForDate(date);
      const rssItems = newsCache.items.filter(i => i.source !== 'SR');
      return res.json({ items: [...srItems, ...rssItems], refreshed: newsCache.refreshed });
    }
    res.json(newsCache);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.listen(PORT, () => console.log(`News server listening on port ${PORT}`));
