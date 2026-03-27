// Aurora Push Notifications – Service Worker
const APP_ICON = '/icons/icon-192.png';

self.addEventListener('push', event => {
  const data = event.data?.json() ?? {};
  event.waitUntil(
    self.registration.showNotification(data.title ?? '🌌 Aurora Alert', {
      body:      data.body   ?? '',
      icon:      APP_ICON,
      badge:     APP_ICON,
      tag:       data.tag    ?? 'aurora',
      renotify:  true,
      vibrate:   [200, 100, 200],
      data:      { url: '/' }
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
