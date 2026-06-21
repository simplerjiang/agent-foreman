// Foreman access-key management (T7.3, DESIGN §8.2/§8.4): a logged-in user mints / lists /
// revokes their OWN access keys. A key is the "SIM card" a local process inserts to dial the
// relay — one machine per key, many per account, hash-stored, individually revocable, optionally
// time-limited. The plaintext is shown exactly ONCE at creation (only its hash is stored).
//
// All user-supplied text (labels) is rendered via textContent — never innerHTML — so a hostile
// label can't inject markup (XSS).

const I18N = {
  zh: {
    accessKeys: '接入密钥', logout: '登出', username: '用户名', password: '密码', signIn: '登录',
    mintKey: '生成接入密钥', label: '标签（机器名）', expiresIn: '有效期（天，0 = 永久）',
    mintHint: '一台机器一张，一个账号可发多张；丢了 / 换机可单独吊销。',
    newKey: '新接入密钥', shownOnce: '只显示一次',
    keyShare: '把这串填进你本地进程的配置（access key）。关掉本页就再也看不到了。',
    yourKeys: '你的密钥', noKeys: '还没有密钥。', revoke: '吊销',
    active: '活跃', revoked: '已吊销', expired: '已过期',
    lastSeen: '上次在线', expires: '有效期至', never: '永久', revokeConfirm: '吊销这张密钥？该机器将立即断开。',
    badLogin: '用户名或密码错误。', minted: '已生成密钥', error: '出错',
  },
  en: {
    accessKeys: 'Access keys', logout: 'Log out', username: 'Username', password: 'Password',
    signIn: 'Sign in', mintKey: 'Generate access key', label: 'Label (machine name)',
    expiresIn: 'Expires in (days, 0 = never)',
    mintHint: 'One key per machine, many per account; revoke one if lost / replaced.',
    newKey: 'New access key', shownOnce: 'shown once',
    keyShare: 'Paste this into your local process config (access key). It is gone once you leave.',
    yourKeys: 'Your keys', noKeys: 'No keys yet.', revoke: 'Revoke',
    active: 'active', revoked: 'revoked', expired: 'expired',
    lastSeen: 'last seen', expires: 'expires', never: 'never', revokeConfirm:
      'Revoke this key? That machine disconnects immediately.',
    badLogin: 'Wrong username or password.', minted: 'Key generated', error: 'error',
  },
};

let lang = localStorage.getItem('foreman.lang') || 'zh';
let token = localStorage.getItem('foreman.token') || '';

const t = (k) => (I18N[lang] && I18N[lang][k]) || (I18N.zh[k] || k);
const $ = (id) => document.getElementById(id);

function applyLang() {
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  for (const el of document.querySelectorAll('[data-i18n]')) {
    el.textContent = t(el.getAttribute('data-i18n'));
  }
  $('lang-toggle').textContent = lang === 'zh' ? 'EN' : '中';
}

function authHeaders() {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function showStatus(el, msg, bad) {
  el.textContent = msg;
  el.hidden = false;
  el.style.color = bad ? 'var(--bad)' : 'var(--muted)';
}

async function login(user, pass) {
  const r = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass }),
  });
  if (!r.ok) return false;
  token = (await r.json()).token;
  localStorage.setItem('foreman.token', token);
  return true;
}

async function enterKeys() {
  // Confirm the stored token is still a valid session.
  const r = await fetch('/api/auth/me', { headers: authHeaders() });
  if (!r.ok) return false;
  const me = await r.json();
  $('who').textContent = `${me.display_name || me.username} · ${me.role}`;
  $('who').hidden = false;
  $('logout').hidden = false;
  $('login-pane').hidden = true;
  $('keys-pane').hidden = false;
  await loadKeys();
  return true;
}

function logout() {
  fetch('/api/auth/logout', { method: 'POST', headers: authHeaders() }).catch(() => {});
  token = '';
  localStorage.removeItem('foreman.token');
  location.reload();
}

function keyState(k) {
  // A key is effectively unusable if revoked OR its expiry has passed (the relay enforces both).
  if (k.status !== 'active') return 'revoked';
  if (k.expires_at && k.expires_at <= new Date().toISOString()) return 'expired';
  return 'active';
}

async function loadKeys() {
  const r = await fetch('/api/keys', { headers: authHeaders() });
  const list = $('key-list');
  list.textContent = '';
  if (!r.ok) {
    list.className = 'empty';
    list.textContent = t('error');
    return;
  }
  const keys = await r.json();
  if (!keys.length) {
    list.className = 'empty';
    list.textContent = t('noKeys');
    return;
  }
  list.className = '';
  for (const k of keys) list.appendChild(renderKey(k));
}

function renderKey(k) {
  const state = keyState(k);
  const row = document.createElement('article');
  row.className = 'card';

  const head = document.createElement('p');
  const name = document.createElement('strong');
  name.textContent = k.label || '(no label)';            // textContent → XSS-safe
  head.appendChild(name);
  const badge = document.createElement('span');
  badge.className = 'pill ' + (state === 'active' ? 'ok' : 'bad');
  badge.textContent = ` ${t(state)}`;
  head.appendChild(badge);
  row.appendChild(head);

  const meta = document.createElement('p');
  meta.className = 'card-diffstat';
  const expires = k.expires_at ? `${t('expires')}: ${k.expires_at.slice(0, 10)}` : t('never');
  const seen = k.last_seen_at ? ` · ${t('lastSeen')}: ${k.last_seen_at.slice(0, 19).replace('T', ' ')}` : '';
  meta.textContent = `${expires}${seen}`;
  row.appendChild(meta);

  if (k.status === 'active') {
    const actions = document.createElement('div');
    actions.className = 'card-actions';
    const revoke = document.createElement('button');
    revoke.textContent = t('revoke');
    revoke.addEventListener('click', () => doRevoke(k.id));
    actions.appendChild(revoke);
    row.appendChild(actions);
  }
  return row;
}

function showKey(plain) {
  $('key-plain').textContent = plain;
  $('key-box').hidden = false;
}

async function mintKey(ev) {
  ev.preventDefault();
  const days = parseInt($('key-expiry').value, 10);
  const body = {
    label: $('key-label').value.trim(),
    expires_in_days: Number.isFinite(days) && days > 0 ? days : 0,
  };
  const r = await fetch('/api/keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  });
  const status = $('mint-status');
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail || t('error');
    showStatus(status, `${r.status}: ${detail}`, true);
    return;
  }
  const res = await r.json();
  showKey(res.key);
  showStatus(status, t('minted'), false);
  $('mint-form').reset();
  await loadKeys();
}

async function doRevoke(id) {
  if (!confirm(t('revokeConfirm'))) return;
  const r = await fetch(`/api/keys/${id}`, { method: 'DELETE', headers: authHeaders() });
  if (r.ok) await loadKeys();
}

async function init() {
  applyLang();
  $('lang-toggle').addEventListener('click', () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    localStorage.setItem('foreman.lang', lang);
    applyLang();
    if (!$('keys-pane').hidden) loadKeys();
  });
  $('logout').addEventListener('click', logout);
  $('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const ok = await login($('login-user').value.trim(), $('login-pass').value);
    if (ok) await enterKeys();
    else showStatus($('login-status'), t('badLogin'), true);
  });
  $('mint-form').addEventListener('submit', mintKey);

  if (token) await enterKeys();
}

init();
