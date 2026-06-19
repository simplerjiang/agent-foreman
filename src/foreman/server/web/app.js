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
  loadSessions();
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

// ── Sessions + live timeline (T1.11) ─────────────────────────────────────────
let ws = null;
const sessionListEl = document.getElementById('session-list');
const eventListEl = document.getElementById('event-list');

async function loadSessions() {
  try {
    const r = await fetch('/api/sessions');
    if (!r.ok) { sessionListEl.textContent = 'No local store (server mode).'; return; }
    const sessions = await r.json();
    if (!sessions.length) { sessionListEl.textContent = 'No sessions yet.'; return; }
    sessionListEl.classList.remove('empty');
    sessionListEl.replaceChildren();
    for (const s of sessions) {
      const b = document.createElement('button');
      b.className = 'session-item';
      b.textContent = `${s.goal || s.id} · ${s.status || ''} [${s.agent_type || ''}]`;
      b.addEventListener('click', () => openTimeline(s.id));
      sessionListEl.appendChild(b);
    }
  } catch (e) {
    sessionListEl.textContent = 'Failed to load sessions.';
  }
}

function openTimeline(sessionId) {
  if (ws) { try { ws.close(); } catch (e) { /* ignore */ } }
  eventListEl.replaceChildren();
  eventListEl.classList.remove('empty');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?session_id=${encodeURIComponent(sessionId)}`);
  ws.addEventListener('message', (ev) => {
    try { renderEvent(JSON.parse(ev.data)); } catch (e) { console.warn('bad event', e); }
  });
}

// Build rows with textContent only — agent output is untrusted, never innerHTML it.
function renderEvent(e) {
  const li = document.createElement('li');
  li.className = 'event';
  const cells = [
    ['ev-type', e.type],
    ['ev-source', e.source || ''],
    ['ev-ts', e.ts || ''],
    ['ev-payload', e.payload ? JSON.stringify(e.payload) : ''],
  ];
  for (const [cls, val] of cells) {
    const span = document.createElement('span');
    span.className = cls;
    span.textContent = String(val).slice(0, 300);
    li.appendChild(span);
  }
  eventListEl.appendChild(li);
}

init();
