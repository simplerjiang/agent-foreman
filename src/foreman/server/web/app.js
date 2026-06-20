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
  await initLang();
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

// ── Web Push (VAPID) — T3.3 ──────────────────────────────────────────────────
// VAPID application-server keys are base64url; PushManager wants a Uint8Array.
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function enablePush() {
  const btn = document.getElementById('enable-push');
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    console.warn('Web Push not supported in this browser');
    return;
  }
  try {
    const r = await fetch('/api/push/vapid-public-key');
    const { key, enabled } = await r.json();
    if (!enabled || !key) {
      console.warn('Web Push not configured on the server (no VAPID key)');
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      console.warn('Notification permission not granted:', perm);
      return;
    }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
    }
    await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON ? sub.toJSON() : sub),
    });
    if (btn) { btn.textContent = '🔔 ✓'; btn.disabled = true; }
  } catch (e) {
    console.warn('enable push failed', e);
  }
}

document.getElementById('enable-push')?.addEventListener('click', enablePush);

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
    if (!sessions.length) { sessionListEl.textContent = I18N[currentLang].noSessions; return; }
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

// ── i18n: UI language (zh/en) + sync to backend so LLM output language follows (§15) ─────────
const I18N = {
  zh: { sessions: '会话', decisions: '决策', approvals: '审批', timeline: '时间线', dispatch: '下发任务',
        send: '发送', enablePush: '开启通知', stepDetail: '步骤详情', rawReturn: '原始返回',
        codeDiff: '代码改动', noSessions: '暂无活动会话。', noDecisions: '暂无待决策。',
        noApprovals: '没有待你处理的。', langToggle: 'EN' },
  en: { sessions: 'Sessions', decisions: 'Decisions', approvals: 'Approvals', timeline: 'Timeline',
        dispatch: 'Dispatch', send: 'Send', enablePush: 'Enable notifications', stepDetail: 'Step detail',
        rawReturn: 'Raw return', codeDiff: 'Code diff', noSessions: 'No active sessions yet.',
        noDecisions: 'No decisions waiting.', noApprovals: 'Nothing waiting on you.', langToggle: '中' },
};
let currentLang = 'zh';

function applyI18n(lang) {
  currentLang = lang === 'en' ? 'en' : 'zh';
  const dict = I18N[currentLang];
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    const k = el.getAttribute('data-i18n');
    if (dict[k]) el.textContent = dict[k];
  });
  const ti = document.getElementById('task-input');
  if (ti) ti.placeholder = currentLang === 'zh'
    ? 'e.g. 重构 auth 模块，push 前问我' : 'e.g. refactor auth, ask me before push';
  const tg = document.getElementById('lang-toggle');
  if (tg) tg.textContent = dict.langToggle;  // shows the OTHER language to switch to
  document.documentElement.lang = currentLang === 'zh' ? 'zh-CN' : 'en';
}

async function initLang() {
  let lang = localStorage.getItem('foreman.lang');
  if (!lang) {
    try { const r = await fetch('/api/settings/language'); if (r.ok) lang = (await r.json()).language; }
    catch (e) { /* fall back to default below */ }
  }
  applyI18n(lang || 'zh');
}

async function setLang(lang) {
  applyI18n(lang);
  localStorage.setItem('foreman.lang', currentLang);
  loadSessions();  // re-render the (dynamic) session list so its empty text follows the language
  try {
    await fetch('/api/settings/language', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ language: currentLang }),
    });
  } catch (e) { /* offline / server mode — UI still switched locally */ }
}

document.getElementById('lang-toggle')?.addEventListener('click',
  () => setLang(currentLang === 'zh' ? 'en' : 'zh'));

init();
