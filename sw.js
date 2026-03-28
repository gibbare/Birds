// Aurora Push Notifications – Service Worker
const WORKER_URL = 'https://aurora-push.gibbare.workers.dev';

self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let title = '🌌 Aurora Alert';
    let body  = 'Öppna appen för att se uppdateringar.';
    let tag   = 'aurora';
    try {
      const res = await fetch(`${WORKER_URL}/latest`);
      if (res.ok) {
        const d = await res.json();
        title = d.title || title;
        body  = d.body  || body;
        tag   = d.tag   || tag;
      }
    } catch {}
    await self.registration.showNotification(title, {
      body, tag, renotify: true, data: { url: d.url || '/' }
    });
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url === url && 'focus' in c) return c.focus();
      }
      return clients.openWindow(url);
    })
  );
});
