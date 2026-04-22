/**
 * Birds API – Cloudflare Worker
 * ==============================
 * Fungerar som API-gateway för fågelappen:
 *
 *   /api/observer_stats   → läser observers_se_YYYY.json direkt från R2
 *   /api/statistics       → läser stats_cache_XX_YYYY.json från R2 om cachad,
 *                           annars proxas till Railway (triggar bygge)
 *   allt annat            → proxas till Railway (dagslista, hackning, login …)
 *
 * Railway behöver aldrig kontaktas för läsningar av cachad data.
 */

const CORS_HEADERS = {
  'Access-Control-Allow-Origin':  '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization',
};

// ── Hjälpfunktioner ─────────────────────────────────────────────────────────

function jsonResp(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json', ...CORS_HEADERS },
  });
}

/**
 * Konverterar kompakt R2-format till API-svarsformat för observer_stats.
 * Hanterar både nytt format (sp/pl listor) och gammalt format (species/places dicts).
 */
function buildObserverResult(data) {
  if (!data?.reporters) return [];

  const result = [];
  for (const [name, d] of Object.entries(data.reporters)) {
    let art, topLokal, lokaler, species;

    if (Array.isArray(d.sp)) {
      // Nytt kompakt format
      const spList = d.sp || [];
      const plList = d.pl || [];
      art      = d.art || 0;
      topLokal = plList[0]?.name || '';
      lokaler  = plList.slice(0, 3).map(x => ({ name: x.name, obs: x.obs }));
      species  = spList.slice(0, 3).map(x => ({ sv: x.sv, obs: x.obs, ind: x.ind ?? x.obs }));
    } else {
      // Gammalt format (species/places dicts) – bakåtkompatibilitet
      const sp = d.species || {};
      const pl = d.places  || {};
      art = Object.keys(sp).length;
      const plSorted = Object.entries(pl).sort((a, b) => b[1] - a[1]);
      topLokal = plSorted[0]?.[0] || '';
      lokaler  = plSorted.slice(0, 3).map(([n, o]) => ({ name: n, obs: o }));
      species  = Object.values(sp)
        .filter(v => v.sv)
        .sort((a, b) => b.obs - a.obs)
        .slice(0, 3)
        .map(v => ({ sv: v.sv, obs: v.obs }));
    }

    result.push({
      name,
      obs:     d.obs     || 0,
      art,
      dagar:   d.dagar   || 0,
      lastObs: d.lastObs || '',
      topLokal,
      lokaler,
      species,
      monthly: d.monthly || Array(12).fill(0),
    });
  }

  result.sort((a, b) => b.art - a.art || b.obs - a.obs);
  return result;
}

/**
 * Proxar en request till Railway och lägger på CORS-headers i svaret.
 */
async function proxyToRailway(request, env) {
  const url    = new URL(request.url);
  const target = env.RAILWAY_URL + url.pathname + url.search;

  const railResp = await fetch(target, {
    method:  request.method,
    headers: request.headers,
    body:    ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
  });

  const body = await railResp.arrayBuffer();
  const headers = new Headers(railResp.headers);
  Object.entries(CORS_HEADERS).forEach(([k, v]) => headers.set(k, v));

  return new Response(body, { status: railResp.status, headers });
}

// ── Huvud-handler ────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    // ── /api/observer_stats – R2 om cachad, annars Railway ───────────────
    if (url.pathname === '/api/observer_stats') {
      const year = url.searchParams.get('year') || String(new Date().getFullYear());
      try {
        const obj = await env.BUCKET.get(`observers_se_${year}.json`);
        if (!obj) {
          // Inte i R2 ännu → Railway som fallback
          return proxyToRailway(request, env);
        }
        const data = await obj.json();
        return jsonResp(buildObserverResult(data));
      } catch (e) {
        // Något gick fel med R2 → Railway som fallback
        return proxyToRailway(request, env);
      }
    }

    // ── /api/observer_species – alla artnamn för en observatör från R2 ─────
    if (url.pathname === '/api/observer_species') {
      const year = url.searchParams.get('year') || String(new Date().getFullYear());
      const name = url.searchParams.get('name') || '';
      if (!name) return jsonResp({ error: 'name_required' }, 400);
      try {
        const obj = await env.BUCKET.get(`observers_se_sp_${year}.json`);
        if (!obj) return jsonResp({ species: [] });
        const data = await obj.json();
        const species = data.reporters?.[name] || [];
        return jsonResp({ species });
      } catch (e) {
        return jsonResp({ error: e.message }, 500);
      }
    }

    // ── /api/statistics – R2 om cachad, annars Railway ────────────────────
    if (url.pathname === '/api/statistics') {
      const year     = url.searchParams.get('year')   || String(new Date().getFullYear());
      const county   = url.searchParams.get('county') || '24';
      const innerKey = `${county}_${year}`;
      try {
        const obj = await env.BUCKET.get(`stats_cache_${innerKey}.json`);
        if (obj) {
          const data  = await obj.json();
          const stats = data[innerKey];
          if (stats) {
            return jsonResp({ status: 'ready', data: stats });
          }
        }
      } catch (_) { /* filen finns ej eller är skadad – fall through till Railway */ }

      // Inte i R2 → Railway triggar bygge och returnerar 202
      return proxyToRailway(request, env);
    }

    // ── Allt annat – proxy till Railway ───────────────────────────────────
    return proxyToRailway(request, env);
  },
};
