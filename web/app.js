// Foreman PWA — P0 skeleton.
// Registers the service worker and probes /health. P3/P4 will add /ws live events,
// /api/* calls, Web Push subscription, and the dispatch/approve flows.

const statusEl = document.getElementById('status');

async function init() {
  if ('serviceWorker' in navigator) {
    try {
      await navigator.serviceWorker.register('/sw.js');
    } catch (e) {
      console.warn('SW registration failed', e);
    }
  }
  probeHealth();
}

async function probeHealth() {
  try {
    const r = await fetch('/health');
    const j = await r.json();
    statusEl.textContent = `online · v${j.version} · ${(j.agents || []).join(', ') || 'no agents'}`;
    statusEl.classList.add('ok');
  } catch (e) {
    statusEl.textContent = 'offline';
    statusEl.classList.add('bad');
  }
}

// Placeholder: dispatch a task (P4 wires POST /api/tasks).
document.getElementById('dispatch-form')?.addEventListener('submit', (e) => {
  e.preventDefault();
  const input = document.getElementById('task-input');
  if (!input.value.trim()) return;
  console.log('dispatch (P4):', input.value);
  input.value = '';
});

// Placeholder: enable Web Push (P3 wires PushManager.subscribe + POST /api/push/subscribe).
document.getElementById('enable-push')?.addEventListener('click', () => {
  console.log('enable push (P3)');
});

init();
