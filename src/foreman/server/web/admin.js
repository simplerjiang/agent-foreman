// Foreman admin console (T7.2, DESIGN §8.2): an existing admin logs in, then builds users +
// hands out one-time invite codes. NO self-signup — only an admin reaches this console, and the
// only non-admin path to a usable password is redeeming an invite (see redeem.html).
//
// All account-supplied text (usernames, display names) is rendered via textContent — never
// innerHTML — so a hostile username can't inject markup (XSS).

const I18N = {
  zh: {
    adminConsole: '管理员控制台', logout: '登出', adminLogin: '管理员登录',
    username: '用户名', password: '密码', signIn: '登录', displayName: '显示名',
    role: '角色', roleMember: '成员', roleAdmin: '管理员',
    initialPassword: '初始密码（可选）', buildUser: '建用户', createUser: '创建用户',
    buildHint: '留空密码 → 生成一次性邀请码（无自助注册）。',
    inviteCode: '邀请码', shownOnce: '只显示一次',
    inviteShare: '把这串发给用户，让 TA 在 /redeem.html 设置密码。',
    accounts: '账号', noAccounts: '还没有账号。', reinvite: '重发邀请',
    disable: '停用', enable: '启用', active: '活跃', invited: '待激活', disabled: '已停用',
    badLogin: '用户名或密码错误，或非管理员。', created: '已创建', needAdmin: '需要管理员权限。',
  },
  en: {
    adminConsole: 'Admin console', logout: 'Log out', adminLogin: 'Admin login',
    username: 'Username', password: 'Password', signIn: 'Sign in', displayName: 'Display name',
    role: 'Role', roleMember: 'Member', roleAdmin: 'Admin',
    initialPassword: 'Initial password (optional)', buildUser: 'Build user',
    createUser: 'Create user',
    buildHint: 'Leave the password blank → a one-time invite code (no self-signup).',
    inviteCode: 'Invite code', shownOnce: 'shown once',
    inviteShare: 'Send this to the user; they set a password at /redeem.html.',
    accounts: 'Accounts', noAccounts: 'No accounts yet.', reinvite: 'Re-invite',
    disable: 'Disable', enable: 'Enable', active: 'active', invited: 'invited', disabled: 'disabled',
    badLogin: 'Wrong credentials, or not an admin.', created: 'Created', needAdmin: 'Admin only.',
  },
};

let lang = localStorage.getItem('foreman.lang') || 'zh';
let token = localStorage.getItem('foreman.adminToken') || '';

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
  const me = await r.json();
  if (me.role !== 'admin') return false;  // console is admin-only
  token = me.token;
  localStorage.setItem('foreman.adminToken', token);
  return true;
}

async function enterConsole() {
  // Confirm the stored token is still a valid admin session.
  const r = await fetch('/api/auth/me', { headers: authHeaders() });
  if (!r.ok) return false;
  const me = await r.json();
  if (me.role !== 'admin') return false;
  $('who').textContent = `${me.display_name || me.username} · admin`;
  $('who').hidden = false;
  $('logout').hidden = false;
  $('login-pane').hidden = true;
  $('console-pane').hidden = false;
  await loadAccounts();
  return true;
}

function logout() {
  fetch('/api/auth/logout', { method: 'POST', headers: authHeaders() }).catch(() => {});
  token = '';
  localStorage.removeItem('foreman.adminToken');
  location.reload();
}

async function loadAccounts() {
  const r = await fetch('/api/admin/accounts', { headers: authHeaders() });
  const list = $('account-list');
  list.textContent = '';
  if (!r.ok) {
    list.className = 'empty';
    list.textContent = t('needAdmin');
    return;
  }
  const accounts = await r.json();
  if (!accounts.length) {
    list.className = 'empty';
    list.textContent = t('noAccounts');
    return;
  }
  list.className = '';
  for (const a of accounts) {
    list.appendChild(renderAccount(a));
  }
}

function renderAccount(a) {
  const row = document.createElement('article');
  row.className = 'card';
  const head = document.createElement('p');
  const name = document.createElement('strong');
  name.textContent = a.username;                       // textContent → XSS-safe
  head.appendChild(name);
  const display = document.createElement('span');
  display.className = 'card-diffstat';
  display.textContent = a.display_name ? ` · ${a.display_name}` : '';
  head.appendChild(display);
  const badge = document.createElement('span');
  badge.className = 'pill ' + (a.status === 'active' ? 'ok' : a.status === 'disabled' ? 'bad' : '');
  badge.textContent = ` ${a.role} · ${t(a.status) || a.status}`;
  head.appendChild(badge);
  row.appendChild(head);

  const actions = document.createElement('div');
  actions.className = 'card-actions';

  const reinvite = document.createElement('button');
  reinvite.textContent = t('reinvite');
  reinvite.addEventListener('click', () => doReinvite(a.id));
  actions.appendChild(reinvite);

  const toggle = document.createElement('button');
  const willEnable = a.status === 'disabled';
  toggle.textContent = willEnable ? t('enable') : t('disable');
  toggle.addEventListener('click', () => setStatus(a.id, willEnable));
  actions.appendChild(toggle);

  row.appendChild(actions);
  return row;
}

function showInvite(code) {
  $('invite-code').textContent = code;
  $('invite-box').hidden = false;
}

async function createUser(ev) {
  ev.preventDefault();
  const body = {
    username: $('new-user').value.trim(),
    display_name: $('new-display').value.trim(),
    role: $('new-role').value,
    password: $('new-pass').value,
  };
  const r = await fetch('/api/admin/accounts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  });
  const status = $('create-status');
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail || 'error';
    showStatus(status, `${r.status}: ${detail}`, true);
    return;
  }
  const res = await r.json();
  showStatus(status, `${t('created')}: ${body.username}`, false);
  $('create-form').reset();
  if (res.invite_code) showInvite(res.invite_code);
  else $('invite-box').hidden = true;
  await loadAccounts();
}

async function doReinvite(id) {
  const r = await fetch(`/api/admin/accounts/${id}/invite`, {
    method: 'POST',
    headers: authHeaders(),
  });
  if (r.ok) showInvite((await r.json()).invite_code);
}

async function setStatus(id, enabled) {
  const r = await fetch(`/api/admin/accounts/${id}/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ enabled }),
  });
  if (!r.ok) {
    // e.g. an admin trying to disable their own account -> 400 "cannot disable self".
    const detail = (await r.json().catch(() => ({}))).detail || 'error';
    showStatus($('create-status'), `${r.status}: ${detail}`, true);
  }
  await loadAccounts();
}

async function init() {
  applyLang();
  $('lang-toggle').addEventListener('click', () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    localStorage.setItem('foreman.lang', lang);
    applyLang();
    if (!$('console-pane').hidden) loadAccounts();
  });
  $('logout').addEventListener('click', logout);
  $('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const ok = await login($('login-user').value.trim(), $('login-pass').value);
    if (ok) await enterConsole();
    else showStatus($('login-status'), t('badLogin'), true);
  });
  $('create-form').addEventListener('submit', createUser);

  if (token) await enterConsole();
}

init();
