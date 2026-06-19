// Foreman service worker — enables install + Web Push receipt.
// P0 skeleton. P3 wires real caching + push/notification handling.

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

// Web Push: show the approval card / briefing.
self.addEventListener('push', (event) => {
  let data = { title: 'Foreman', body: '', data: {} };
  try {
    if (event.data) data = event.data.json();
  } catch (_) {}
  event.waitUntil(
    self.registration.showNotification(data.title || 'Foreman', {
      body: data.body || '',
      data: data.data || {},
      // P3: actions for one-tap approve/reject:
      // actions: [{ action: 'approve', title: 'Approve' }, { action: 'reject', title: 'Reject' }],
    })
  );
});

// Tapping a notification focuses the app (and, P3, deep-links to the approval).
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      if (clients.length) return clients[0].focus();
      return self.clients.openWindow('/');
    })
  );
});
