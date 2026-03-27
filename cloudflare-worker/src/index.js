import webpush from 'web-push';

// Cooldown: don't re-send Kp alert within this many ms (2 hours)
const KP_COOLDOWN_MS = 2 * 60 * 60 * 1000;

export default {
  // ── HTTP: subscribe / unsubscribe ────────────────────────────────────
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === 'OPTIONS') return cors(null, 204);

    if (request.method === 'POST' && url.pathname === '/subscribe') {
      const { subscription, lat, lon } = await request.json();
      const key = await subKey(subscription.endpoint);
      await env.SUBS.put(key, JSON.stringify({ subscription, lat, lon }));
      return cors(JSON.stringify({ ok: true }), 200);
    }

    if (request.method === 'DELETE' && url.pathname === '/unsubscribe') {
      const { subscription } = await request.json();
      const key = await subKey(subscription.endpoint);
      await env.SUBS.delete(key);
      return cors(JSON.stringify({ ok: true }), 200);
    }

    return new Response('Not found', { status: 404 });
  },

  // ── Scheduled: check NOAA + send push if conditions met ─────────────
  async scheduled(event, env) {
    webpush.setVapidDetails(
      `mailto:${env.VAPID_EMAIL}`,
      env.VAPID_PUBLIC_KEY,
      env.VAPID_PRIVATE_KEY
    );

    // Fetch Kp + alerts in parallel
    const [kpRes, alertRes] = await Promise.all([
      fetch('https://services.swpc.noaa.gov/json/boulder_k_index_1m.json'),
      fetch('https://services.swpc.noaa.gov/products/alerts.json')
    ]);
    const kpData = await kpRes.json();
    const alertData = await alertRes.json();

    const currentKp = parseFloat(kpData[kpData.length - 1]?.[1] ?? 0);

    // Only new alerts (issued within last 11 minutes)
    const freshAlerts = alertData.filter(a => {
      const age = Date.now() - new Date(a.issue_datetime).getTime();
      return age < 11 * 60 * 1000;
    });

    // Load all subscriptions
    const { keys } = await env.SUBS.list();
    if (!keys.length) return;

    // Kp cooldown: only send once per cooldown period
    const lastKpAlert = parseInt(await env.SUBS.get('__kp_last_alert') ?? '0');
    const kpCooledDown = (Date.now() - lastKpAlert) > KP_COOLDOWN_MS;

    for (const { name } of keys) {
      if (name.startsWith('__')) continue; // skip metadata keys
      const raw = await env.SUBS.get(name);
      if (!raw) continue;
      const { subscription, lat, lon } = JSON.parse(raw);

      // Check cloud cover for this subscriber's location
      let cloudCover = 100;
      try {
        const r = await fetch(
          `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=cloud_cover&timezone=auto`
        );
        const d = await r.json();
        cloudCover = d.current?.cloud_cover ?? 100;
      } catch {}

      // ── Kp > 4 + klar himmel ───────────────────────────────────────
      if (currentKp > 4 && cloudCover < 20 && kpCooledDown) {
        await send(subscription, {
          title: '🌌 Aurora möjlig!',
          body: `Kp-index ${currentKp.toFixed(1)} – molnighet ${cloudCover}%. Håll utkik efter norrsken!`,
          tag: 'kp-alert'
        }, env);
      }

      // ── Rymdvädervarningar ─────────────────────────────────────────
      for (const alert of freshAlerts) {
        const alertKey = `__alert_${alert.product_id}`;
        const alreadySent = await env.SUBS.get(alertKey);
        if (alreadySent) continue;

        await send(subscription, {
          title: '⚠️ Rymdvädervarning',
          body: (alert.message ?? 'Ny rymdvädervarning från NOAA').substring(0, 140),
          tag: `alert-${alert.product_id}`
        }, env);

        // Mark alert as sent (expires after 24h)
        await env.SUBS.put(alertKey, '1', { expirationTtl: 86400 });
      }
    }

    // Update Kp cooldown timestamp if we just sent a Kp alert
    if (currentKp > 4 && kpCooledDown) {
      const anyCloudClear = true; // simplified — already checked per subscriber above
      await env.SUBS.put('__kp_last_alert', String(Date.now()));
    }
  }
};

// ── Helpers ──────────────────────────────────────────────────────────────

async function send(subscription, payload, env) {
  try {
    await webpush.sendNotification(subscription, JSON.stringify(payload));
  } catch (e) {
    // Remove expired/invalid subscriptions
    if (e.statusCode === 410 || e.statusCode === 404) {
      const key = await subKey(subscription.endpoint);
      await env.SUBS.delete(key);
    }
  }
}

async function subKey(endpoint) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
  return 'sub_' + btoa(String.fromCharCode(...new Uint8Array(buf))).substring(0, 24);
}

function cors(body, status) {
  return new Response(body, {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'POST, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type'
    }
  });
}
