/**
 * Aurora Push Worker — Web Crypto only, no npm dependencies.
 * Implements RFC 8291 (Web Push message encryption) and RFC 8292 (VAPID).
 *
 * Environment variables (set in Cloudflare dashboard → Settings → Variables):
 *   VAPID_PUBLIC_KEY   — base64url-encoded P-256 public key  (65 bytes uncompressed)
 *   VAPID_PRIVATE_KEY  — base64url-encoded P-256 private key (32 bytes raw)
 *   VAPID_EMAIL        — contact email, e.g. you@example.com
 *
 * KV namespace binding (Settings → Variables → KV):
 *   SUBS  — stores push subscriptions and metadata
 */

const KP_COOLDOWN_MS = 2 * 60 * 60 * 1000; // 2h between Kp alerts

export default {

  // ── HTTP: /subscribe (POST) and /unsubscribe (DELETE) ──────────────────
  async fetch(request, env) {
    const path = new URL(request.url).pathname;

    if (request.method === 'OPTIONS') return cors(null, 204);

    if (request.method === 'POST' && path === '/subscribe') {
      const { subscription, lat, lon } = await request.json();
      const key = await subKey(subscription.endpoint);
      await env.SUBS.put(key, JSON.stringify({ subscription, lat, lon }));
      return cors('{"ok":true}', 200);
    }

    if (request.method === 'DELETE' && path === '/unsubscribe') {
      const { subscription } = await request.json();
      await env.SUBS.delete(await subKey(subscription.endpoint));
      return cors('{"ok":true}', 200);
    }

    return new Response('Not found', { status: 404 });
  },

  // ── Scheduled: check NOAA every 10 min, send push if needed ───────────
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
    let   sentKpAlert  = false;

    for (const { name } of keys) {
      if (name.startsWith('__')) continue;
      const raw = await env.SUBS.get(name);
      if (!raw) continue;
      const { subscription, lat, lon } = JSON.parse(raw);

      // Cloud cover for this subscriber's location
      let cloud = 100;
      try {
        const r = await fetch(`https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=cloud_cover&timezone=auto`);
        cloud = (await r.json()).current?.cloud_cover ?? 100;
      } catch {}

      if (currentKp > 4 && cloud < 20 && kpCooledDown) {
        const ok = await sendPush(subscription, {
          title: '🌌 Aurora möjlig!',
          body:  `Kp ${currentKp.toFixed(1)} – molnighet ${cloud}%. Håll utkik efter norrsken!`,
          tag:   'kp-alert',
        }, env);
        if (ok) sentKpAlert = true;
      }

      for (const alert of freshAlerts) {
        const alertKey = `__alert_${alert.product_id}`;
        if (await env.SUBS.get(alertKey)) continue;
        await sendPush(subscription, {
          title: '⚠️ Rymdvädervarning',
          body:  (alert.message ?? 'Ny rymdvädervarning från NOAA').substring(0, 140),
          tag:   `alert-${alert.product_id}`,
        }, env);
        await env.SUBS.put(alertKey, '1', { expirationTtl: 86400 });
      }
    }

    if (sentKpAlert) await env.SUBS.put('__kp_last', String(Date.now()));
  },
};

// ── Send one push notification ─────────────────────────────────────────────

async function sendPush(subscription, payload, env) {
  try {
    const body = new TextEncoder().encode(JSON.stringify(payload));
    const encrypted = await encryptPayload(subscription, body);
    const endpoint  = subscription.endpoint;
    const audience  = new URL(endpoint).origin;
    const jwt       = await vapidJWT(audience, env.VAPID_EMAIL, env.VAPID_PUBLIC_KEY, env.VAPID_PRIVATE_KEY);

    const res = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Authorization':    `vapid t=${jwt},k=${env.VAPID_PUBLIC_KEY}`,
        'Content-Encoding': 'aes128gcm',
        'Content-Type':     'application/octet-stream',
        'TTL':              '86400',
      },
      body: encrypted,
    });

    if (res.status === 410 || res.status === 404) {
      // Subscription gone — clean up
      await env.SUBS.delete(await subKey(endpoint));
    }
    return res.ok || res.status === 201;
  } catch (e) {
    console.error('sendPush error:', e);
    return false;
  }
}

// ── RFC 8291: Web Push payload encryption (aes128gcm) ─────────────────────

async function encryptPayload(subscription, plaintext) {
  const authSecret  = b64u(subscription.keys.auth);
  const receiverPub = b64u(subscription.keys.p256dh);

  // Ephemeral sender key pair + salt
  const salt       = crypto.getRandomValues(new Uint8Array(16));
  const senderKeys = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
  const senderPub  = new Uint8Array(await crypto.subtle.exportKey('raw', senderKeys.publicKey));

  // ECDH shared secret
  const recvKey = await crypto.subtle.importKey('raw', receiverPub, { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const secret  = new Uint8Array(await crypto.subtle.deriveBits({ name: 'ECDH', public: recvKey }, senderKeys.privateKey, 256));

  // IKM via HKDF (auth secret as salt, ecdh secret as ikm)
  const infoWebpush = join(str('WebPush: info\x00'), receiverPub, senderPub);
  const ikm = await hkdf(authSecret, secret, infoWebpush, 32);

  // Content Encryption Key (16 bytes) and Nonce (12 bytes)
  const cek   = await hkdf(salt, ikm, join(str('Content-Encoding: aes128gcm\x00'), new Uint8Array([1])), 16);
  const nonce = await hkdf(salt, ikm, join(str('Content-Encoding: nonce\x00'),     new Uint8Array([1])), 12);

  // Encrypt: plaintext || 0x02 (last-record delimiter)
  const aesKey    = await crypto.subtle.importKey('raw', cek, 'AES-GCM', false, ['encrypt']);
  const encrypted = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce }, aesKey, join(plaintext, new Uint8Array([2]))));

  // RFC 8291 binary header: salt(16) | rs(4 BE) | keyid_len(1) | sender_pub(65) | ciphertext
  const rs = new Uint8Array(4);
  new DataView(rs.buffer).setUint32(0, 4096, false);
  return join(salt, rs, new Uint8Array([senderPub.length]), senderPub, encrypted);
}

// ── RFC 8292: VAPID JWT (ES256) ────────────────────────────────────────────

async function vapidJWT(audience, email, pubB64u, privB64u) {
  const hdr = { typ: 'JWT', alg: 'ES256' };
  const pay = { aud: audience, exp: Math.floor(Date.now() / 1000) + 43200, sub: `mailto:${email}` };
  const enc = o => btoa(JSON.stringify(o)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,'');
  const input = `${enc(hdr)}.${enc(pay)}`;

  const pub  = b64u(pubB64u);
  const priv = b64u(privB64u);
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

async function hkdf(salt, ikm, info, len) {
  const key  = await crypto.subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt, info }, key, len * 8);
  return new Uint8Array(bits);
}

function join(...arrays) {
  const out = new Uint8Array(arrays.reduce((n, a) => n + a.length, 0));
  let i = 0; for (const a of arrays) { out.set(a, i); i += a.length; }
  return out;
}

function str(s)    { return new TextEncoder().encode(s); }
function b64u(s)   { return Uint8Array.from(atob((s + '==='.slice((s.length + 3) % 4)).replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0)); }
function toB64u(a) { return btoa(String.fromCharCode(...a)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=/g,''); }

async function subKey(endpoint) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
  return 'sub_' + toB64u(new Uint8Array(buf)).substring(0, 22);
}

function cors(body, status) {
  return new Response(body, { status, headers: {
    'Content-Type':                'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods':'POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers':'Content-Type',
  }});
}
