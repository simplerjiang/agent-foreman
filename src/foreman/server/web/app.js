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

// Decision card -> step detail drill-down (§6.3). P4 wires GET /api/actions/{id}/detail.
const detailSection = document.getElementById('detail');

function openDetail(actionId) {
  console.log('load detail (P4):', actionId); // fetch raw output + diff, fill #tab-raw / #tab-diff
  detailSection?.removeAttribute('hidden');
  detailSection?.scrollIntoView({ behavior: 'smooth' });
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('.view-detail');
  if (btn) openDetail(btn.dataset.actionId);
});

document.getElementById('detail-back')?.addEventListener('click', () => {
  detailSection?.setAttribute('hidden', '');
});

// Detail tab switching: 原始返回 / 代码改动.
document.querySelectorAll('#detail .tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#detail .tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    const which = tab.dataset.tab; // 'raw' | 'diff'
    document.getElementById('tab-raw').hidden = which !== 'raw';
    document.getElementById('tab-diff').hidden = which !== 'diff';
  });
});

init();
