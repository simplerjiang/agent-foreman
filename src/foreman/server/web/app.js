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
  loadCards();
  await loadApprovals();
  maybeActOnDeepLink();  // a cold one-tap (notification → openWindow with ?approval=&action=)
}

// A notification opened the app cold with ?approval=<id>&action=approve|reject — act on it once.
function maybeActOnDeepLink() {
  const params = new URLSearchParams(location.search);
  const id = params.get('approval');
  const action = params.get('action');
  if (id && (action === 'approve' || action === 'reject')) {
    const a = pendingApprovals.find((x) => x.id === id);
    if (a) decideApproval(a.id, action, a.nonce);
  }
  // Drop the one-shot params so a refresh doesn't re-fire (and stale ids don't linger in the URL).
  if (id || action) history.replaceState(null, '', location.pathname);
}

// The service worker forwards a notification tap (approve/reject one-tap, or just a focus) here.
navigator.serviceWorker?.addEventListener('message', (e) => {
  const m = e.data || {};
  if (m.type !== 'notificationclick') return;
  loadApprovals().then(() => {
    // If the SW relayed a one-tap action, pre-trigger it for the deep-linked approval.
    const id = approvalIdFromUrl(m.url);
    if (id && (m.action === 'approve' || m.action === 'reject')) {
      const a = pendingApprovals.find((x) => x.id === id);
      if (a) decideApproval(a.id, m.action, a.nonce);
    }
  });
});

function approvalIdFromUrl(url) {
  try { return new URL(url, location.origin).searchParams.get('approval'); }
  catch (e) { return null; }
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

// ── Decision cards (§6.3): folded summary + audit note + one-tap options + 🔍 详情 ───────────
const cardListEl = document.getElementById('card-list');
const cardTemplate = document.getElementById('card-template');

async function loadCards() {
  if (!cardListEl) return;
  try {
    const r = await fetch('/api/cards');
    if (!r.ok) { renderCards([]); return; }
    renderCards(await r.json());
  } catch (e) {
    renderCards([]);
  }
}

// Build cards from the template with textContent only — summaries/audit notes echo untrusted
// agent output, so never innerHTML them (same rule as the timeline + approvals).
function renderCards(cards) {
  const dict = I18N[currentLang];
  if (!cards.length) {
    cardListEl.classList.add('empty');
    cardListEl.textContent = dict.noDecisions;
    return;
  }
  cardListEl.classList.remove('empty');
  cardListEl.replaceChildren();
  for (const c of cards) {
    const node = cardTemplate.content.cloneNode(true);
    node.querySelector('.summary').textContent = c.summary || '';
    node.querySelector('.audit').textContent = c.audit_note || '';
    node.querySelector('.diffstat').textContent = c.diff_stat || dict.viewDetail;
    const detailBtn = node.querySelector('.view-detail');
    detailBtn.dataset.actionId = c.action_id || '';
    if (!c.action_id) detailBtn.hidden = true;
    const actions = node.querySelector('.card-actions');
    for (const opt of c.options || []) {
      const b = document.createElement('button');
      b.className = 'option';
      b.textContent = opt.label || opt.action || '';
      b.dataset.cardId = c.id || '';
      b.dataset.option = opt.action || '';
      b.addEventListener('click', () => chooseCard(c.id, opt.action));
      actions.appendChild(b);
    }
    cardListEl.appendChild(node);
  }
}

// One-tap card decision (§6.3): record the chosen option. Executing the chosen path (run / nudge /
// undo the agent) is the two-way control layer (P4) — this closes the "you tap" half.
async function chooseCard(cardId, option) {
  if (!cardId || !option) return;
  try {
    const r = await fetch(`/api/cards/${encodeURIComponent(cardId)}/choose`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ option }),
    });
    if (!r.ok) console.warn('choose failed', r.status);
  } catch (e) {
    console.warn('choose error', e);
  }
  loadCards();  // refresh so the decided card reflects the choice
}

// Decision card -> step detail drill-down (§6.3): GET /api/actions/{id}/detail.
const detailSection = document.getElementById('detail');

async function openDetail(actionId) {
  detailSection?.removeAttribute('hidden');
  detailSection?.scrollIntoView({ behavior: 'smooth' });
  const rawEl = document.getElementById('tab-raw');
  const diffEl = document.getElementById('tab-diff');
  rawEl.textContent = '…';
  diffEl.replaceChildren();
  try {
    const r = await fetch(`/api/actions/${encodeURIComponent(actionId)}/detail`);
    if (!r.ok) { rawEl.textContent = `（详情不可用 ${r.status}）`; return; }
    const detail = await r.json();
    renderRaw(rawEl, detail.raw || []);
    renderDiff(diffEl, detail.diff || { files: [] });
  } catch (e) {
    rawEl.textContent = '（加载详情失败）';
  }
}

// Tab ① 原始返回 — each raw event on its own line; textContent only (untrusted agent output).
function renderRaw(el, events) {
  if (!events.length) { el.textContent = '（这一步没有原始返回）'; return; }
  el.textContent = events
    .map((e) => `[${e.ts || ''}] ${e.type} (${e.source || ''})\n${JSON.stringify(e.payload)}`)
    .join('\n\n');
}

// Tab ② 代码改动 — per-file, per-line diff with line tags/highlight; textContent only.
function renderDiff(el, diff) {
  const files = diff.files || [];
  if (!files.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = diff.note || '（这一步没有代码改动）';
    el.appendChild(p);
    return;
  }
  for (const f of files) {
    const head = document.createElement('div');
    head.className = 'diff-file';
    head.textContent = `${f.path}  +${f.additions} / −${f.deletions}`;
    el.appendChild(head);
    for (const ln of f.lines || []) {
      const row = document.createElement('div');
      row.className = `diff-line diff-${ln.kind}`;
      const sign = ln.kind === 'add' ? '+' : ln.kind === 'del' ? '−' : ' ';
      row.textContent = sign + ln.text;
      el.appendChild(row);
    }
  }
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

// ── Approvals: the phone's approve/reject queue (T3.4, §6.6) ─────────────────────────────────
const approvalListEl = document.getElementById('approval-list');
let pendingApprovals = [];

async function loadApprovals() {
  if (!approvalListEl) return;
  try {
    const r = await fetch('/api/approvals');
    if (!r.ok) { pendingApprovals = []; renderApprovals(); return; }
    pendingApprovals = await r.json();
    renderApprovals();
  } catch (e) {
    pendingApprovals = [];
    renderApprovals();
  }
}

// Build rows with textContent only — the held action is untrusted agent input, never innerHTML it.
function renderApprovals() {
  const dict = I18N[currentLang];
  if (!pendingApprovals.length) {
    approvalListEl.classList.add('empty');
    approvalListEl.textContent = dict.noApprovals;
    return;
  }
  approvalListEl.classList.remove('empty');
  approvalListEl.replaceChildren();
  for (const a of pendingApprovals) {
    const card = document.createElement('article');
    card.className = 'card approval';
    const risk = document.createElement('p');
    risk.className = 'card-summary';
    risk.textContent = `⛔ ${a.risk_level || 'requires-approval'}`;
    const action = document.createElement('p');
    action.className = 'card-audit';
    action.textContent = a.action || a.diff_summary || '';
    const btns = document.createElement('div');
    btns.className = 'card-actions';
    btns.appendChild(makeDecisionBtn(a, 'approve', `✅ ${dict.approve}`));
    btns.appendChild(makeDecisionBtn(a, 'reject', `⛔ ${dict.reject}`));
    card.append(risk, action, btns);
    approvalListEl.appendChild(card);
  }
}

function makeDecisionBtn(approval, decision, label) {
  const b = document.createElement('button');
  b.className = 'option';
  b.textContent = label;
  b.addEventListener('click', () => decideApproval(approval.id, decision, approval.nonce));
  return b;
}

async function decideApproval(id, decision, nonce) {
  try {
    const r = await fetch(`/api/approvals/${encodeURIComponent(id)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, nonce: nonce || '' }),
    });
    if (!r.ok) console.warn('decide failed', r.status);
  } catch (e) {
    console.warn('decide error', e);
  }
  loadApprovals();  // refresh the queue (decided ones drop off)
}

// ── i18n: UI language (zh/en) + sync to backend so LLM output language follows (§15) ─────────
const I18N = {
  zh: { sessions: '会话', decisions: '决策', approvals: '审批', timeline: '时间线', dispatch: '下发任务',
        send: '发送', enablePush: '开启通知', stepDetail: '步骤详情', rawReturn: '原始返回',
        codeDiff: '代码改动', noSessions: '暂无活动会话。', noDecisions: '暂无待决策。',
        noApprovals: '没有待你处理的。', approve: '批准', reject: '驳回', viewDetail: '查看详情',
        autonomy: '自治', langToggle: 'EN' },
  en: { sessions: 'Sessions', decisions: 'Decisions', approvals: 'Approvals', timeline: 'Timeline',
        dispatch: 'Dispatch', send: 'Send', enablePush: 'Enable notifications', stepDetail: 'Step detail',
        rawReturn: 'Raw return', codeDiff: 'Code diff', noSessions: 'No active sessions yet.',
        noDecisions: 'No decisions waiting.', noApprovals: 'Nothing waiting on you.',
        approve: 'Approve', reject: 'Reject', viewDetail: 'View detail',
        autonomy: 'Autonomy', langToggle: '中' },
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
  loadCards();  // re-render decision cards in the new language
  renderApprovals();  // re-label approve/reject + empty text in the new language
  try {
    await fetch('/api/settings/language', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ language: currentLang }),
    });
  } catch (e) { /* offline / server mode — UI still switched locally */ }
}

document.getElementById('lang-toggle')?.addEventListener('click',
  () => setLang(currentLang === 'zh' ? 'en' : 'zh'));

// ── autonomy dial (0/1/2/3): how proactive Foreman is — capabilities stay full, only whether
//    placing a move asks you first changes (DESIGN §6.4). Synced to the backend (config_kv). ───
const autonomySelect = document.getElementById('autonomy-select');

async function initAutonomy() {
  if (!autonomySelect) return;
  try {
    const r = await fetch('/api/settings/autonomy');
    if (r.ok) {
      const { level, label } = await r.json();
      autonomySelect.value = String(level);
      if (label) autonomySelect.title = label;  // hover shows the level's meaning
    }
  } catch (e) { /* offline / server mode — leave default */ }
}

autonomySelect?.addEventListener('change', async () => {
  const level = parseInt(autonomySelect.value, 10);
  try {
    const r = await fetch('/api/settings/autonomy', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level }),
    });
    if (r.ok) {
      const data = await r.json();
      autonomySelect.value = String(data.level);  // reflect the server's clamp
      if (data.label) autonomySelect.title = data.label;
    }
  } catch (e) { /* offline — selection still reflects intent locally */ }
});

init();
initAutonomy();
