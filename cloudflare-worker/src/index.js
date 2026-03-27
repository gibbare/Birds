/**
 * Aurora Push Worker — skickar push-notiser utan krypterad payload.
 * Service workern hämtar meddelandet från /latest vid leverans.
 *
 * Cloudflare Variables (Settings → Variables):
 *   VAPID_PUBLIC_KEY   — base64url P-256 public key
 *   VAPID_PRIVATE_KEY  — base64url P-256 private key (32 bytes raw)
 *   VAPID_EMAIL        — mailto: e-postadress
 *
 * KV binding: SUBS
 */

const KP_COOLDOWN_MS = 2 * 60 * 60 * 1000; // 2h mellan Kp-varningar

export default {

  // ── HTTP endpoints ────────────────────────────────────────────────────
  async fetch(request, env) {
    const path = new URL(request.url).pathname;

    if (request.method === 'OPTIONS') return cors(null, 204);

    // Spara prenumeration
    if (request.method === 'POST' && path === '/subscribe') {
      const { subscription, lat, lon } = await request.json();
      const key = await subKey(subscription.endpoint);
      await env.SUBS.put(key, JSON.stringify({ subscription, lat, lon }));
      return cors('{"ok":true}', 200);
    }

    // Ta bort prenumeration
    if (request.method === 'DELETE' && path === '/unsubscribe') {
      const { subscription } = await request.json();
      await env.SUBS.delete(await subKey(subscription.endpoint));
      return cors('{"ok":true}', 200);
    }

    // Service workern hämtar detta vid push-leverans
    if (request.method === 'GET' && path === '/latest') {
      const latest = await env.SUBS.get('__latest_alert');
      const data = latest
        ? JSON.parse(latest)
        : { title: '🌌 Aurora Alert', body: 'Öppna appen för uppdateringar.', tag: 'aurora' };
      return cors(JSON.stringify(data), 200);
    }

    // Testa push till alla prenumeranter
    if (request.method === 'GET' && path === '/test') {
      const msg = { title: '🌌 Testnotis', body: 'Push-notiser fungerar!', tag: 'test' };
      await env.SUBS.put('__latest_alert', JSON.stringify(msg), { expirationTtl: 300 });
      const { keys } = await env.SUBS.list();
      const results = [];
      for (const { name } of keys) {
        if (!name.startsWith('sub_')) continue;
        const raw = await env.SUBS.get(name);
        if (!raw) continue;
        const { subscription } = JSON.parse(raw);
        const status = await sendPush(subscription, env);
        results.push({ name, status });
      }
      return cors(JSON.stringify({ results }, null, 2), 200);
    }

    return new Response('Not found', { status: 404 });
  },

  // ── Schemalagd körning var 10:e minut ─────────────────────────────────
  async scheduled(_event, env) {
    const [kpRes, alertRes] = await Promise.all([
      fetch('https://services.swpc.noaa.gov/json/boulder_k_index_1m.json'),
      fetch('https://services.swpc.noaa.gov/products/alerts.json'),
    ]);
    const kpData    = await kpRes.json();
    const alertData = await alertRes.json();

    const currentKp   = parseFloat(kpData.at(-1)?.[1] ?? 0);
    const freshAlerts = alertData.filter(a =>
      Date.now() - new Date(a.issue_datetime).getTime() < 11 * 60 * 1000
    );

    const { keys } = await env.SUBS.list();
    if (!keys.length) return;

    const lastKpAlert  = parseInt(await env.SUBS.get('__kp_last') ?? '0');
    const kpCooledDown = Date.now() - lastKpAlert > KP_COOLDOWN_MS;
    let sentKpAlert = false;

    for (const { name } of keys) {
      if (!name.startsWith('sub_')) continue;
      const raw = await env.SUBS.get(name);
      if (!raw) continue;
      const { subscription, lat, lon } = JSON.parse(raw);

      // Kontrollera molntäcke för denna position
      let cloud = 100;
      try {
        const r = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=cloud_cover&timezone=auto`);
        cloud = (await r.json()).current?.cloud_cover ?? 100;
      } catch {}

      // Kp > 4 + molnighet < 20%
      if (currentKp > 4 && cloud < 20 && kpCooledDown) {
        const msg = {
          title: '🌌 Aurora möjlig!',
          body:  `Kp ${currentKp.toFixed(1)} – molnighet ${cloud}%. Håll utkik efter norrsken!`,
          tag:   'kp-alert',
        };
        await env.SUBS.put('__latest_alert', JSON.stringify(msg), { expirationTtl: 3600 });
        const ok = await sendPush(subscription, env);
        if (ok) sentKpAlert = true;
      }

      // Rymdvädervarningar från NOAA
      for (const alert of freshAlerts) {
        const alertKey = `__alert_${alert.product_id}`;
        if (await env.SUBS.get(alertKey)) continue;
        const msg = {
          title: '⚠️ Rymdvädervarning',
          body:  (alert.message ?? 'Ny rymdvädervarning från NOAA').substring(0, 140),
          tag:   `alert-${alert.product_id}`,
        };
        await env.SUBS.put('__latest_alert', JSON.stringify(msg), { expirationTtl: 3600 });
        await sendPush(subscription, env);
        await env.SUBS.put(alertKey, '1', { expirationTtl: 86400 });
      }
    }

    if (sentKpAlert) await env.SUBS.put('__kp_last', String(Date.now()));
  },
};

// ── Skicka push UTAN krypterad body (service workern hämtar /latest) ──────

async function sendPush(subscription, env) {
  try {
    const endpoint = subscription.endpoint;
    const audience = new URL(endpoint).origin;
    const jwt = await vapidJWT(audience, env.VAPID_EMAIL, env.VAPID_PUBLIC_KEY, env.VAPID_PRIVATE_KEY);
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Authorization': `vapid t=${jwt},k=${env.VAPID_PUBLIC_KEY}`,
        'TTL': '86400',
        'Content-Length': '0',
      },
    });
    if (res.status === 410 || res.status === 404) {
      await env.SUBS.delete(await subKey(endpoint));
    }
    return res.status;
  } catch (e) {
    console.error('sendPush error:', e);
    return 0;
  }
}

// ── RFC 8292: VAPID JWT (ES256) ────────────────────────────────────────────

async function vapidJWT(audience, email, pubB64u, privB64u) {
  const hdr = { typ: 'JWT', alg: 'ES256' };
  const pay = { aud: audience, exp: Math.floor(Date.now() / 1000) + 43200, sub: `mailto:${email}` };
  const enc = o => btoa(JSON.stringify(o)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
  const input = `${enc(hdr)}.${enc(pay)}`;

  const pub  = b64u(pubB64u);
  const jwk  = {
    kty: 'EC', crv: 'P-256',
    x: toB64u(pub.slice(1, 33)),
    y: toB64u(pub.slice(33, 65)),
    d: privB64u,
  };
  const key = await crypto.subtle.importKey('jwk', jwk, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
  const sig  = new Uint8Array(await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, key, new TextEncoder().encode(input)));
  return `${input}.${toB64u(sig)}`;
}

// ── Helpers ────────────────────────────────────────────────────────────────

function b64u(s)   { return Uint8Array.from(atob((s + '==='.slice((s.length + 3) % 4)).replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)); }
function toB64u(a) { return btoa(String.fromCharCode(...a)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,''); }

async function subKey(endpoint) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
  return 'sub_' + toB64u(new Uint8Array(buf)).substring(0, 22);
}

function cors(body, status) {
  return new Response(body, { status, headers: {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, DELETE, OPTIONS, GET',
    'Access-Control-Allow-Headers': 'Content-Type',
  }});
}
