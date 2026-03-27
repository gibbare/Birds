// Aurora Push Notifications – Service Worker
const APP_ICON = '/icons/icon-192.png';

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data?.json() ?? {}; } catch {}
  event.waitUntil(
    self.registration.showNotification(data.title ?? '🌌 Aurora Alert', {
      body:     data.body ?? '',
      tag:      data.tag  ?? 'aurora',
      renotify: true,
      data:     { url: '/' }
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if ('focus' in c) return c.focus();
      }
      return clients.openWindow('/');
    })
  );
});
