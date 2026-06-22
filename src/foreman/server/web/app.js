// Foreman PWA — P0 skeleton.
// Registers the service worker and probes /health. P3/P4 will add /ws live events,
// /api/* calls, Web Push subscription, and the dispatch/approve flows.

const statusEl = document.getElementById('status');

// ── personal-mode access token (issue #1 P0) ────────────────────────────────────────────────────
// When the local app is exposed (e.g. via a Cloudflare/Tailscale/frp tunnel), FOREMAN_AUTH_TOKEN
// gates every operational call. The user pastes the token once; it's stored and attached to all
// same-origin /api and /hooks requests as a bearer, and to the WS as a ?token= query param (a
// browser can't set an Authorization header on a WebSocket). On 127.0.0.1 with no token set the
// server is open, so this is a no-op for the on-machine native window.
const TOKEN_KEY = 'foreman.token';
function authToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
let _promptedForToken = false;
function promptForToken() {
  if (_promptedForToken) return;
  _promptedForToken = true;
  const t = window.prompt('Access token required (FOREMAN_AUTH_TOKEN):', '');
  if (t && t.trim()) { localStorage.setItem(TOKEN_KEY, t.trim()); location.reload(); }
}
const _origFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const url = typeof input === 'string' ? input : (input && input.url) || '';
  const sameOrigin = url.startsWith('/') || url.startsWith(location.origin);
  const t = authToken();
  if (sameOrigin && t) {
    init = { ...init, headers: { ...(init.headers || {}), Authorization: `Bearer ${t}` } };
  }
  const r = await _origFetch(input, init);
  if (r.status === 401 && sameOrigin) promptForToken();  // exposed app, missing/stale token
  return r;
};

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
  loadReports();
  loadDefinitions();
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

// Dispatch a task from the phone → a new Root Session (§5.1, T4.6): POST /api/tasks.
document.getElementById('dispatch-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = document.getElementById('task-input');
  const goal = input.value.trim();
  if (!goal) return;
  try {
    const r = await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goal }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      input.value = '';
      showDispatchStatus(`${I18N[currentLang].dispatched} · ${data.agent || ''}`);
      loadSessions();  // a new session appears in the multi-session list
    } else {
      showDispatchStatus(`${I18N[currentLang].dispatchFailed} (${data.detail || r.status})`);
    }
  } catch (err) {
    showDispatchStatus(I18N[currentLang].dispatchFailed);
  }
});

function showDispatchStatus(text) {
  const el = document.getElementById('dispatch-status');
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
}

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

// ── Toast: transient feedback for actions with no inline UI (e.g. enable-push) ──────────────
// The host is created lazily so index.html needs no extra markup. role=alert for errors so
// screen readers announce failures; status for the rest.
function toast(message, type = 'info') {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.textContent = message;
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));  // animate in after layout
  setTimeout(() => {
    el.classList.remove('show');
    el.addEventListener('transitionend', () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400);  // fallback if transitionend never fires
  }, 3200);
}

async function enablePush() {
  const btn = document.getElementById('enable-push');
  const dict = I18N[currentLang];
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    console.warn('Web Push not supported in this browser');
    toast(dict.pushUnsupported, 'error');
    return;
  }
  try {
    const r = await fetch('/api/push/vapid-public-key');
    const { key, enabled } = await r.json();
    if (!enabled || !key) {
      console.warn('Web Push not configured on the server (no VAPID key)');
      toast(dict.pushNotConfigured, 'error');
      return;
    }
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      console.warn('Notification permission not granted:', perm);
      toast(dict.pushDenied, 'error');
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
    const resp = await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON ? sub.toJSON() : sub),
    });
    if (!resp.ok) {
      console.warn('push subscribe rejected by server:', resp.status);
      toast(dict.pushFailed, 'error');
      return;
    }
    if (btn) { btn.textContent = '🔔 ✓'; btn.disabled = true; }
    toast(dict.pushEnabled, 'success');
    // Fire a real system notification right now: it both confirms success and proves the
    // OS-level pipeline works, which a banner alone can't. Missing icon degrades gracefully.
    try {
      await reg.showNotification('Foreman', {
        body: dict.pushEnabledBody,
        icon: '/icon-192.png',
        badge: '/icon-192.png',
        tag: 'foreman-push-test',
      });
    } catch (e) {
      console.warn('test notification failed (subscription still active)', e);
    }
  } catch (e) {
    console.warn('enable push failed', e);
    toast(dict.pushFailed, 'error');
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

// Multi-session dashboard (T4.6): /api/overview enriches each session with activity counts so
// several concurrent root sessions show at a glance. Falls back to /api/sessions if unavailable.
async function loadSessions() {
  try {
    let sessions = null;
    try {
      const ov = await fetch('/api/overview');
      if (ov.ok) sessions = await ov.json();
    } catch (e) { /* fall through to /api/sessions below */ }
    if (sessions === null) {
      const r = await fetch('/api/sessions');
      if (!r.ok) { sessionListEl.textContent = 'No local store (server mode).'; return; }
      sessions = await r.json();
    }
    if (!sessions.length) {
      sessionListEl.classList.add('empty');
      sessionListEl.textContent = I18N[currentLang].noSessions;
      return;
    }
    sessionListEl.classList.remove('empty');
    sessionListEl.replaceChildren();
    for (const s of sessions) {
      const b = document.createElement('button');
      b.className = 'session-item';
      // textContent only — goal echoes untrusted user/agent input (never innerHTML).
      const head = document.createElement('span');
      head.className = 'session-head';
      head.textContent = `${s.goal || s.id} · ${s.status || ''} [${s.agent_type || ''}]`;
      b.appendChild(head);
      if (s.events !== undefined) {
        const meta = document.createElement('span');
        meta.className = 'session-meta';
        const bits = [`📋 ${s.events}`];
        if (s.open_cards) bits.push(`🗂️ ${s.open_cards}`);
        if (s.pending_approvals) bits.push(`⛔ ${s.pending_approvals}`);
        if (s.last_event_type) bits.push(s.last_event_type);
        meta.textContent = bits.join(' · ');
        b.appendChild(meta);
      }
      b.addEventListener('click', () => openTimeline(s.id));
      sessionListEl.appendChild(b);
    }
  } catch (e) {
    sessionListEl.textContent = 'Failed to load sessions.';
  }
}

// ── Briefings (§5.5, T4.6): the phone's status-report feed + a one-tap "generate now" ─────────
const reportListEl = document.getElementById('report-list');

async function loadReports() {
  if (!reportListEl) return;
  try {
    const r = await fetch('/api/reports');
    if (!r.ok) { renderReports([]); return; }
    renderReports(await r.json());
  } catch (e) {
    renderReports([]);
  }
}

// textContent only — briefing text comes from the LLM over untrusted agent output.
function renderReports(reports) {
  if (!reportListEl) return;
  if (!reports.length) {
    reportListEl.classList.add('empty');
    reportListEl.textContent = I18N[currentLang].noReports;
    return;
  }
  reportListEl.classList.remove('empty');
  reportListEl.replaceChildren();
  for (const rep of reports) {
    const card = document.createElement('article');
    card.className = 'card report';
    const title = document.createElement('p');
    title.className = 'card-summary';
    title.textContent = `📰 ${rep.title || rep.kind || ''}`;
    const body = document.createElement('pre');
    body.className = 'report-body';
    body.textContent = rep.body_md || '';
    const meta = document.createElement('p');
    meta.className = 'session-meta';
    meta.textContent = `${rep.kind || ''} · ${rep.ts || ''}`;
    card.append(title, body, meta);
    reportListEl.appendChild(card);
  }
}

async function generateReport() {
  const btn = document.getElementById('generate-brief');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/reports/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'active-briefing' }),
    });
    if (!r.ok) console.warn('generate briefing failed', r.status);
  } catch (e) {
    console.warn('generate briefing error', e);
  }
  if (btn) btn.disabled = false;
  loadReports();
}

document.getElementById('generate-brief')?.addEventListener('click', generateReport);

function openTimeline(sessionId) {
  if (ws) { try { ws.close(); } catch (e) { /* ignore */ } }
  eventListEl.replaceChildren();
  eventListEl.classList.remove('empty');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const t = authToken();
  const tokenQuery = t ? `&token=${encodeURIComponent(t)}` : '';  // WS auth rides the query (P0)
  ws = new WebSocket(
    `${proto}://${location.host}/ws?session_id=${encodeURIComponent(sessionId)}${tokenQuery}`
  );
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

// ── Definition editor (§11.2, T6.1): add/edit/delete 工作流/技能/规范/QA from the phone/web ─────
const defnListEl = document.getElementById('defn-list');
const defnForm = document.getElementById('defn-form');
const KIND_LABEL = {
  workflow: 'kindWorkflow', skill: 'kindSkill', code_standard: 'kindStandard', qa_rubric: 'kindQa',
};

async function loadDefinitions() {
  if (!defnListEl) return;
  const kind = document.getElementById('defn-filter')?.value || '';
  try {
    const url = kind ? `/api/definitions?kind=${encodeURIComponent(kind)}` : '/api/definitions';
    const r = await fetch(url);
    if (!r.ok) { renderDefinitions([]); return; }
    renderDefinitions(await r.json());
  } catch (e) {
    renderDefinitions([]);
  }
}

// textContent only — names/bodies are user-authored 秘方; treat as untrusted, never innerHTML.
function renderDefinitions(defs) {
  const dict = I18N[currentLang];
  if (!defnListEl) return;
  if (!defs.length) {
    defnListEl.classList.add('empty');
    defnListEl.textContent = dict.noDefinitions;
    return;
  }
  defnListEl.classList.remove('empty');
  defnListEl.replaceChildren();
  for (const d of defs) {
    const row = document.createElement('article');
    row.className = 'card defn-item';
    const head = document.createElement('p');
    head.className = 'card-summary';
    const kindTxt = dict[KIND_LABEL[d.kind]] || d.kind;
    const live = d.is_active ? ' ✓' : '';
    head.textContent = `${kindTxt} · ${d.name} v${d.version}${live}`;
    const actions = document.createElement('div');
    actions.className = 'card-actions';
    actions.appendChild(defnBtn(dict.edit, () => openDefnForm(d)));
    if (!d.is_active) actions.appendChild(defnBtn(dict.activate, () => activateDefn(d.id)));
    actions.appendChild(defnBtn(dict.delete, () => deleteDefn(d.id)));
    row.append(head, actions);
    defnListEl.appendChild(row);
  }
}

function defnBtn(label, onClick) {
  const b = document.createElement('button');
  b.className = 'option';
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

// Open the editor: blank for a new block, or pre-filled to edit an existing one. Editing locks
// identity (kind/name) — a body change is an in-place edit; a new version is a fresh "+ New".
async function openDefnForm(def) {
  if (!defnForm) return;
  defnForm.hidden = false;
  const idEl = document.getElementById('defn-id');
  const kindEl = document.getElementById('defn-kind');
  const nameEl = document.getElementById('defn-name');
  const scopeEl = document.getElementById('defn-scope');
  const bodyEl = document.getElementById('defn-body');
  const activateEl = document.getElementById('defn-activate');
  hideDefnStatus();
  if (def) {
    idEl.value = def.id;
    kindEl.value = def.kind; kindEl.disabled = true;
    nameEl.value = def.name; nameEl.disabled = true;
    scopeEl.value = def.scope_json || '{}';
    bodyEl.value = def.body || '';
    // "Activate on save" means "make THIS version the live one" — there is no deactivate (exactly
    // one version is ever live, switched by activating another). So default it unchecked on edit:
    // checking it makes this version live; leaving it just saves the body without changing which
    // version is live. (Pre-checking an already-active row would be a no-op the user can't undo.)
    activateEl.checked = false;
  } else {
    idEl.value = '';
    kindEl.disabled = false; nameEl.disabled = false;
    kindEl.value = document.getElementById('defn-filter')?.value || 'workflow';
    nameEl.value = ''; scopeEl.value = '{}'; bodyEl.value = ''; activateEl.checked = true;
  }
  defnForm.scrollIntoView({ behavior: 'smooth' });
}

function showDefnStatus(text) {
  const el = document.getElementById('defn-status');
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
}
function hideDefnStatus() {
  const el = document.getElementById('defn-status');
  if (el) el.hidden = true;
}

defnForm?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const dict = I18N[currentLang];
  const id = document.getElementById('defn-id').value;
  const scope = document.getElementById('defn-scope').value.trim() || '{}';
  const body = document.getElementById('defn-body').value;
  const activate = document.getElementById('defn-activate').checked;
  try {
    let r;
    if (id) {
      // Edit in place (identity is locked); activate separately if requested.
      r = await fetch(`/api/definitions/${encodeURIComponent(id)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ body, scope_json: scope }),
      });
      if (r.ok && activate) {
        await fetch(`/api/definitions/${encodeURIComponent(id)}/activate`, { method: 'POST' });
      }
    } else {
      r = await fetch('/api/definitions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: document.getElementById('defn-kind').value,
          name: document.getElementById('defn-name').value.trim(),
          body, scope_json: scope, activate,
        }),
      });
    }
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      defnForm.hidden = true;
      loadDefinitions();
    } else {
      showDefnStatus(`${dict.saveFailed} (${data.detail || r.status})`);
    }
  } catch (err) {
    showDefnStatus(dict.saveFailed);
  }
});

document.getElementById('defn-new')?.addEventListener('click', () => openDefnForm(null));
document.getElementById('defn-cancel')?.addEventListener('click', () => { defnForm.hidden = true; });
document.getElementById('defn-filter')?.addEventListener('change', loadDefinitions);

// ── Backup: export / import all 秘方 (T6.2, §765) ─────────────────────────────────────────────
// Export downloads the bundle as a JSON file; import reads one back and POSTs it (merge — existing
// rows are skipped, so re-import is idempotent and never clobbers live recipes).
document.getElementById('defn-export')?.addEventListener('click', async () => {
  try {
    const r = await fetch('/api/definitions/export');
    if (!r.ok) { showDefnStatus(I18N[currentLang].exportFailed); return; }
    const bundle = await r.json();
    const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'foreman-definitions.json';
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (err) {
    showDefnStatus(I18N[currentLang].exportFailed);
  }
});

const defnImportFile = document.getElementById('defn-import-file');
document.getElementById('defn-import')?.addEventListener('click', () => defnImportFile?.click());
defnImportFile?.addEventListener('change', async () => {
  const dict = I18N[currentLang];
  const file = defnImportFile.files && defnImportFile.files[0];
  defnImportFile.value = '';  // allow re-importing the same file
  if (!file) return;
  try {
    const bundle = JSON.parse(await file.text());
    const r = await fetch('/api/definitions/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bundle }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) {
      showDefnStatus(`${dict.imported}: ${data.imported} (+${data.links_imported} links)`);
      loadDefinitions();
    } else {
      showDefnStatus(`${dict.importFailed} (${data.detail || r.status})`);
    }
  } catch (err) {
    showDefnStatus(dict.importFailed);
  }
});

async function activateDefn(id) {
  try {
    const r = await fetch(`/api/definitions/${encodeURIComponent(id)}/activate`, { method: 'POST' });
    if (!r.ok) console.warn('activate failed', r.status);
  } catch (e) { console.warn('activate error', e); }
  loadDefinitions();
}

async function deleteDefn(id) {
  if (!confirm(I18N[currentLang].confirmDelete)) return;
  try {
    const r = await fetch(`/api/definitions/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!r.ok) console.warn('delete failed', r.status);
  } catch (e) { console.warn('delete error', e); }
  loadDefinitions();
}

// ── i18n: UI language (zh/en) + sync to backend so LLM output language follows (§15) ─────────
const I18N = {
  zh: { sessions: '会话', decisions: '决策', approvals: '审批', timeline: '时间线', dispatch: '下发任务',
        send: '发送', enablePush: '开启通知', stepDetail: '步骤详情', rawReturn: '原始返回',
        codeDiff: '代码改动', noSessions: '暂无活动会话。', noDecisions: '暂无待决策。',
        noApprovals: '没有待你处理的。', approve: '批准', reject: '驳回', viewDetail: '查看详情',
        autonomy: '自治', langToggle: 'EN', briefings: '简报', generateBriefing: '生成简报',
        noReports: '暂无简报。', dispatched: '已下发', dispatchFailed: '下发失败',
        definitionsTitle: '秘方', filterKind: '类型', kindAll: '全部', kindWorkflow: '工作流',
        kindSkill: '技能', kindStandard: '代码规范', kindQa: 'QA 标准', newDefinition: '+ 新建',
        noDefinitions: '暂无秘方。', defnKind: '类型', defnName: '名称', defnScope: '适用范围 (JSON)',
        defnBody: '内容', defnActivate: '保存即启用', save: '保存', cancel: '取消', edit: '编辑',
        activate: '启用', delete: '删除', confirmDelete: '确定删除这条秘方？',
        saveFailed: '保存失败', exportDefinitions: '⬇ 导出', importDefinitions: '⬆ 导入',
        exportFailed: '导出失败', importFailed: '导入失败', imported: '已导入',
        pushEnabled: '通知已开启', pushEnabledBody: '你将在这里收到决策与审批提醒。',
        pushUnsupported: '此浏览器不支持通知', pushNotConfigured: '服务器未配置推送',
        pushDenied: '通知权限被拒绝（请在浏览器设置中允许）', pushFailed: '开启通知失败' },
  en: { sessions: 'Sessions', decisions: 'Decisions', approvals: 'Approvals', timeline: 'Timeline',
        dispatch: 'Dispatch', send: 'Send', enablePush: 'Enable notifications', stepDetail: 'Step detail',
        rawReturn: 'Raw return', codeDiff: 'Code diff', noSessions: 'No active sessions yet.',
        noDecisions: 'No decisions waiting.', noApprovals: 'Nothing waiting on you.',
        approve: 'Approve', reject: 'Reject', viewDetail: 'View detail',
        autonomy: 'Autonomy', langToggle: '中', briefings: 'Briefings', generateBriefing: 'Generate briefing',
        noReports: 'No briefings yet.', dispatched: 'Dispatched', dispatchFailed: 'Dispatch failed',
        definitionsTitle: 'Recipes', filterKind: 'Kind', kindAll: 'All', kindWorkflow: 'Workflow',
        kindSkill: 'Skill', kindStandard: 'Code standard', kindQa: 'QA rubric', newDefinition: '+ New',
        noDefinitions: 'No recipes yet.', defnKind: 'Kind', defnName: 'Name', defnScope: 'Scope (JSON)',
        defnBody: 'Body', defnActivate: 'Activate on save', save: 'Save', cancel: 'Cancel', edit: 'Edit',
        activate: 'Activate', delete: 'Delete', confirmDelete: 'Delete this recipe?',
        saveFailed: 'Save failed', exportDefinitions: '⬇ Export', importDefinitions: '⬆ Import',
        exportFailed: 'Export failed', importFailed: 'Import failed', imported: 'Imported',
        pushEnabled: 'Notifications enabled', pushEnabledBody: "You'll get decision & approval alerts here.",
        pushUnsupported: 'Notifications not supported in this browser', pushNotConfigured: 'Push not configured on the server',
        pushDenied: 'Notification permission denied (allow it in browser settings)', pushFailed: 'Could not enable notifications' },
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
  loadReports();  // re-render briefings (empty text follows the language)
  loadDefinitions();  // re-render the 秘方 list + labels in the new language
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
