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

    // Ad Monitor: hämta config (autentiserat med secret query param)
    if (request.method === 'GET' && path === '/config') {
      const secret = new URL(request.url).searchParams.get('secret');
      if (!env.NOTIFY_SECRET || secret !== env.NOTIFY_SECRET)
        return new Response('Unauthorized', { status: 401 });
      const raw = await env.SUBS.get('__adhunter_config');
      const cfg = raw ? JSON.parse(raw) : {
        terms: [], interval: 20,
        sites: { blocket:true, mpb:true, kamerastore:true, scandinavianphoto:true, cyberphoto:true, goecker:true }
      };
      return cors(JSON.stringify(cfg), 200);
    }

    // Ad Monitor: spara config (autentiserat med secret i body)
    if (request.method === 'POST' && path === '/config') {
      const body = await request.json();
      if (!env.NOTIFY_SECRET || body.secret !== env.NOTIFY_SECRET)
        return new Response('Unauthorized', { status: 401 });
      const { secret: _s, ...cfg } = body;
      await env.SUBS.put('__adhunter_config', JSON.stringify(cfg));
      return cors('{"ok":true}', 200);
    }

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

    // Ad Monitor: prenumerera på annonsnotiser (separerat från väderappen)
    if (request.method === 'POST' && path === '/subscribe-ads') {
      const { subscription } = await request.json();
      const key = 'ad_' + (await subKey(subscription.endpoint));
      await env.SUBS.put(key, JSON.stringify({ subscription }));
      return cors('{"ok":true}', 200);
    }

    // Ad Monitor: avprenumerera
    if (request.method === 'DELETE' && path === '/unsubscribe-ads') {
      const { subscription } = await request.json();
      await env.SUBS.delete('ad_' + (await subKey(subscription.endpoint)));
      return cors('{"ok":true}', 200);
    }

    // Ad Monitor: service workern hämtar detta vid leverans
    if (request.method === 'GET' && path === '/latest-ad') {
      const latest = await env.SUBS.get('__latest_ad_alert');
      const data = latest
        ? JSON.parse(latest)
        : { title: '📦 Ad Monitor', body: 'Öppna appen för detaljer.', tag: 'ad', url: '/' };
      return cors(JSON.stringify(data), 200);
    }

    // Ad Monitor: skicka push-notis för ny annons – endast till ad_sub_*-prenumeranter
    // POST /notify  { secret, title, body, tag, url }
    if (request.method === 'POST' && path === '/notify') {
      const { secret, title, body, tag, url } = await request.json();
      if (!env.NOTIFY_SECRET || secret !== env.NOTIFY_SECRET) {
        return new Response('Unauthorized', { status: 401 });
      }
      const msg = { title, body, tag: tag || 'ad', url: url || '/' };
      await env.SUBS.put('__latest_ad_alert', JSON.stringify(msg), { expirationTtl: 3600 });
      const { keys } = await env.SUBS.list();
      let sent = 0;
      for (const { name } of keys) {
        if (!name.startsWith('ad_sub_')) continue;
        const raw = await env.SUBS.get(name);
        if (!raw) continue;
        const { subscription } = JSON.parse(raw);
        await sendPush(subscription, env);
        sent++;
      }
      return cors(JSON.stringify({ ok: true, sent }), 200);
    }

    // Ad Monitor: testa push till alla ad-prenumeranter (kräver secret)
    if (request.method === 'POST' && path === '/test-notify') {
      const body = await request.json();
      if (!env.NOTIFY_SECRET || body.secret !== env.NOTIFY_SECRET)
        return new Response('Unauthorized', { status: 401 });
      const msg = { title: '📦 Test – Ad Monitor', body: 'Push-notiser fungerar!', tag: 'ad-test', url: '/' };
      await env.SUBS.put('__latest_ad_alert', JSON.stringify(msg), { expirationTtl: 300 });
      const { keys } = await env.SUBS.list();
      let sent = 0, accepted = 0;
      const results = [];
      for (const { name } of keys) {
        if (!name.startsWith('ad_sub_')) continue;
        const raw = await env.SUBS.get(name);
        if (!raw) continue;
        const { subscription } = JSON.parse(raw);
        const status = await sendPush(subscription, env);
        sent++;
        if (status >= 200 && status < 300) accepted++;
        results.push({ endpoint: subscription.endpoint.substring(0, 40) + '...', status });
      }
      return cors(JSON.stringify({ ok: true, sent, accepted, results }), 200);
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

    // Prenumerationssida för Ad Monitor
    if (request.method === 'GET' && path === '/subscribe') {
      return new Response(subscribeHTML(env.VAPID_PUBLIC_KEY || 'BOkPa5xxrv4_txqeqZ6Dx5KDgfAlxdWG5LGyV1V76oFFzAqtzhww-VSsOiz1CMDxCJA8zAC1Z6yvhGhyGMo4qvs'), {
        headers: { 'Content-Type': 'text/html;charset=UTF-8' },
      });
    }

    // Service worker för Ad Monitor (måste serveras från samma origin)
    if (request.method === 'GET' && path === '/ad-sw.js') {
      return new Response(adServiceWorker(), {
        headers: {
          'Content-Type': 'application/javascript',
          'Service-Worker-Allowed': '/',
        },
      });
    }

    // ── Found ads storage ─────────────────────────────────────────────────────

    // GET /ads?secret=... – return all saved ads
    if (request.method === 'GET' && path === '/ads') {
      const secret = new URL(request.url).searchParams.get('secret');
      if (!env.NOTIFY_SECRET || secret !== env.NOTIFY_SECRET)
        return new Response('Unauthorized', { status: 401 });
      const raw = await env.SUBS.get('__found_ads');
      return cors(raw || '[]', 200);
    }

    // POST /ads – save a new found ad { secret, ad: {id,title,price,url,site,query} }
    if (request.method === 'POST' && path === '/ads') {
      const body = await request.json();
      if (!env.NOTIFY_SECRET || body.secret !== env.NOTIFY_SECRET)
        return new Response('Unauthorized', { status: 401 });
      const raw = await env.SUBS.get('__found_ads');
      const ads = raw ? JSON.parse(raw) : [];
      if (!ads.find(a => a.id === body.ad.id)) {
        ads.unshift({ ...body.ad, starred: false, foundAt: new Date().toISOString() });
        if (ads.length > 500) ads.length = 500;
        await env.SUBS.put('__found_ads', JSON.stringify(ads));
      }
      return cors('{"ok":true}', 200);
    }

    // PATCH /ads/star – toggle star { secret, id, starred }
    if (request.method === 'PATCH' && path === '/ads/star') {
      const body = await request.json();
      if (!env.NOTIFY_SECRET || body.secret !== env.NOTIFY_SECRET)
        return new Response('Unauthorized', { status: 401 });
      const raw = await env.SUBS.get('__found_ads');
      const ads = raw ? JSON.parse(raw) : [];
      const ad = ads.find(a => a.id === body.id);
      if (ad) ad.starred = body.starred;
      await env.SUBS.put('__found_ads', JSON.stringify(ads));
      return cors('{"ok":true}', 200);
    }

    // DELETE /ads – remove all unstarred ads { secret }
    if (request.method === 'DELETE' && path === '/ads') {
      const body = await request.json();
      if (!env.NOTIFY_SECRET || body.secret !== env.NOTIFY_SECRET)
        return new Response('Unauthorized', { status: 401 });
      const raw = await env.SUBS.get('__found_ads');
      const ads = raw ? JSON.parse(raw) : [];
      const kept = ads.filter(a => a.starred);
      await env.SUBS.put('__found_ads', JSON.stringify(kept));
      return cors(JSON.stringify({ removed: ads.length - kept.length, kept: kept.length }), 200);
    }

    return new Response('Not found', { status: 404 });
  },

  // ── Schemalagd körning var 10:e minut ─────────────────────────────────
  async scheduled(_event, env) {
    const [kpRes, fcRes, alertRes] = await Promise.all([
      fetch('https://services.swpc.noaa.gov/json/boulder_k_index_1m.json'),
      fetch('https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json'),
      fetch('https://services.swpc.noaa.gov/products/alerts.json'),
    ]);
    const kpData    = await kpRes.json();
    const fcData    = await fcRes.json();
    const alertData = await alertRes.json();

    const currentKp   = parseFloat(kpData.at(-1)?.[1] ?? 0);
    const freshAlerts = alertData.filter(a =>
      Date.now() - new Date(a.issue_datetime).getTime() < 11 * 60 * 1000
    );

    // Kp-prognos: max-värde de närmaste 6 timmarna
    const now = Date.now();
    const forecastKp = fcData
      .filter(row => {
        const t = new Date(row[0]).getTime();
        return t > now && t <= now + 6 * 3600 * 1000 && row[2] === 'predicted';
      })
      .reduce((max, row) => Math.max(max, parseFloat(row[1]) || 0), 0);

    const { keys } = await env.SUBS.list();
    if (!keys.length) return;

    const lastKpAlert    = parseInt(await env.SUBS.get('__kp_last') ?? '0');
    const lastFcAlert    = parseInt(await env.SUBS.get('__kp_fc_last') ?? '0');
    const kpCooledDown   = Date.now() - lastKpAlert > KP_COOLDOWN_MS;
    const fcCooledDown   = Date.now() - lastFcAlert > KP_COOLDOWN_MS;
    let sentKpAlert = false;
    let sentFcAlert = false;

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

      // Aktuellt Kp > 4 + molnighet < 20%
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

      // Prognos: Kp > 4 inom 6 timmar
      if (forecastKp > 4 && fcCooledDown) {
        const msg = {
          title: '🌌 Norrsken kan väntas!',
          body:  `Kp-prognos ${forecastKp.toFixed(1)} inom 6 timmar. Förbered dig!`,
          tag:   'kp-forecast-alert',
        };
        await env.SUBS.put('__latest_alert', JSON.stringify(msg), { expirationTtl: 3600 });
        const ok = await sendPush(subscription, env);
        if (ok) sentFcAlert = true;
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

    if (sentKpAlert) await env.SUBS.put('__kp_last',    String(Date.now()));
    if (sentFcAlert) await env.SUBS.put('__kp_fc_last', String(Date.now()));
  },
};

// ── Skicka push UTAN krypterad body (service workern hämtar /latest) ──────

async function sendPush(subscription, env) {
  try {
    const endpoint = subscription.endpoint;
    const audience = new URL(endpoint).origin;
    const jwt = await vapidJWT(audience, env.VAPID_EMAIL, env.VAPID_PUBLIC_KEY, env.VAPID_PRIVATE_KEY);

    const headers = {
      'Authorization': `vapid t=${jwt},k=${env.VAPID_PUBLIC_KEY}`,
      'TTL': '86400',
      'Content-Type': 'application/octet-stream',
    };

    const res = await fetch(endpoint, { method: 'POST', headers, body: new Uint8Array(0) });
    if (res.status === 410 || res.status === 404) {
      await env.SUBS.delete(await subKey(endpoint));
    }
    return res.status;
  } catch (e) {
    console.error('sendPush error:', e.message);
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

function adServiceWorker() {
  return `
const WORKER_URL = 'https://aurora-push.gibbare.workers.dev';
self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let title = '📦 Ny annons', body = 'Öppna för att se annonsen.', tag = 'ad', url = '/';
    try {
      const res = await fetch(WORKER_URL + '/latest-ad');
      if (res.ok) { const d = await res.json(); title = d.title||title; body = d.body||body; tag = d.tag||tag; url = d.url||url; }
    } catch {}
    await self.registration.showNotification(title, { body, tag, renotify: true, data: { url } });
  })());
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(clients.openWindow(url));
});
`.trim();
}

function subscribeHTML(vapidKey) {
  return `<!DOCTYPE html>
<html lang="sv">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ad Monitor – Prenumerera</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 420px; margin: 60px auto; padding: 0 20px; background: #0f0f0f; color: #f0f0f0; }
    h1   { font-size: 1.3rem; margin-bottom: 0.3rem; }
    p    { color: #aaa; font-size: 0.9rem; margin-top: 0; }
    button { width: 100%; padding: 14px; margin-top: 20px; border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer; }
    button:disabled { opacity: 0.4; cursor: default; }
    #btn-sub   { background: #4ade80; color: #000; }
    #btn-unsub { background: #f87171; color: #000; margin-top: 10px; }
    #status    { margin-top: 16px; font-size: 0.85rem; color: #aaa; text-align: center; }
  </style>
</head>
<body>
  <h1>📦 Ad Monitor</h1>
  <p>Prenumerera för att få push-notiser när en ny annons dyker upp.</p>
  <button id="btn-sub">Aktivera notiser</button>
  <button id="btn-unsub" style="display:none">Avaktivera notiser</button>
  <div id="status"></div>
  <script>
    const WORKER_URL = 'https://aurora-push.gibbare.workers.dev';
    const VAPID_KEY  = '${vapidKey}';
    const btnSub = document.getElementById('btn-sub');
    const btnUnsub = document.getElementById('btn-unsub');
    const status = document.getElementById('status');
    function setStatus(m) { status.textContent = m; }
    function b64(s) {
      const p = '='.repeat((4 - s.length % 4) % 4);
      return Uint8Array.from(atob((s+p).replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0));
    }
    async function getReg() { return navigator.serviceWorker.register('/ad-sw.js', { scope: '/' }); }
    async function checkState() {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) { setStatus('Push stöds inte i den här webbläsaren.'); btnSub.disabled = true; return; }
      const sub = await (await getReg()).pushManager.getSubscription();
      if (sub) { btnSub.style.display='none'; btnUnsub.style.display='block'; setStatus('✅ Notiser aktiverade på den här enheten.'); }
      else      { btnSub.style.display='block'; btnUnsub.style.display='none'; setStatus('Notiser är inte aktiverade.'); }
    }
    btnSub.addEventListener('click', async () => {
      btnSub.disabled = true; setStatus('Aktiverar...');
      try {
        if (await Notification.requestPermission() !== 'granted') { setStatus('Notiser nekades.'); btnSub.disabled=false; return; }
        const sub = await (await getReg()).pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(VAPID_KEY) });
        await fetch(WORKER_URL + '/subscribe-ads', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ subscription: sub }) });
        await checkState();
      } catch(e) { setStatus('Fel: ' + e.message); btnSub.disabled=false; }
    });
    btnUnsub.addEventListener('click', async () => {
      btnUnsub.disabled = true; setStatus('Avaktiverar...');
      try {
        const sub = await (await getReg()).pushManager.getSubscription();
        if (sub) {
          await fetch(WORKER_URL + '/unsubscribe-ads', { method:'DELETE', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ subscription: sub }) });
          await sub.unsubscribe();
        }
        await checkState();
      } catch(e) { setStatus('Fel: ' + e.message); btnUnsub.disabled=false; }
    });
    checkState();
  </script>
</body>
</html>`;
}

function cors(body, status) {
  return new Response(body, { status, headers: {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, DELETE, OPTIONS, GET',
    'Access-Control-Allow-Headers': 'Content-Type',
  }});
}
