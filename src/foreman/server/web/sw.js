// Foreman service worker — enables install + Web Push receipt (T3.3).
// Caching of the app shell + one-tap approve/reject actions land with the Gate (T3.4).

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// Web Push: show the approval card / briefing. Payload is JSON sent by the server Pusher:
// { title, body, data: { url?, actions?, tag? } } — see server/push.py.
self.addEventListener('push', (event) => {
  let data = { title: 'Foreman', body: '', data: {} };
  try {
    if (event.data) data = event.data.json();
  } catch (_) {}
  const d = data.data || {};
  const options = {
    body: data.body || '',
    data: d,
    tag: d.tag || undefined,            // collapse repeats for the same card/session
    badge: '/icon-192.png',
    icon: '/icon-192.png',
  };
  // The server may offer one-tap options (e.g. approve/reject) — wired to the Gate in T3.4.
  if (Array.isArray(d.actions)) options.actions = d.actions;
  event.waitUntil(
    self.registration.showNotification(data.title || 'Foreman', options)
  );
});

// Tapping a notification (or one of its actions) focuses the app and deep-links to the card.
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  // Deep-link target must be same-origin — never follow an off-origin or javascript: URL
  // that could reach the payload once T3.4 wires real card URLs.
  let target = '/';
  try {
    const raw = (event.notification.data && event.notification.data.url) || '/';
    const u = new URL(raw, self.location.origin);
    if (u.origin === self.location.origin) target = u.pathname + u.search + u.hash;
  } catch (_) {}
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const c of clients) {
        if ('focus' in c) {
          // Forward the action (approve/reject) + target so the page can act on it (T3.4).
          c.postMessage({ type: 'notificationclick', action: event.action, url: target });
          return c.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
