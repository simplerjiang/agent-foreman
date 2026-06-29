// Foreman invite redemption (T7.2, DESIGN §8.2): a new user pastes the admin's one-time invite
// code and sets their own password. This is the ONLY non-admin path to a usable password — what
// "no self-signup" means. On success the server logs them straight in (returns a bearer token).

const I18N = {
  zh: {
    redeemTitle: '激活账号', redeemHeading: '设置你的密码',
    redeemHint: '用管理员发给你的邀请码设置密码（无自助注册）。',
    inviteCode: '邀请码', newPassword: '新密码（≥ 8 位）', activate: '激活',
    ok: '已激活，正在跳转…', badCode: '邀请码无效、已用过或已过期。', badPass: '密码至少 8 位。',
  },
  en: {
    redeemTitle: 'Activate account', redeemHeading: 'Set your password',
    redeemHint: "Use the admin's invite code to set a password (no self-signup).",
    inviteCode: 'Invite code', newPassword: 'New password (≥ 8 chars)', activate: 'Activate',
    ok: 'Activated — redirecting…', badCode: 'Invalid, used, or expired invite code.',
    badPass: 'Password must be at least 8 characters.',
  },
};

const LANG_KEY = 'foreman.lang';
function normalizeUiLang(value) {
  return String(value || '').trim().toLowerCase().startsWith('zh') ? 'zh' : 'en';
}
function detectedUiLang() {
  const stored = localStorage.getItem(LANG_KEY);
  if (stored) return normalizeUiLang(stored);
  const langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ''];
  return normalizeUiLang(langs[0]);
}

let lang = detectedUiLang();
const t = (k) => (I18N[lang] && I18N[lang][k]) || (I18N.zh[k] || k);
const $ = (id) => document.getElementById(id);

function applyLang() {
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
  document.title = lang === 'zh' ? 'Foreman · 激活账号' : 'Foreman · Activate account';
  for (const el of document.querySelectorAll('[data-i18n]')) {
    el.textContent = t(el.getAttribute('data-i18n'));
  }
  $('lang-toggle').textContent = lang === 'zh' ? 'EN' : '中';
}

function showStatus(msg, bad) {
  const el = $('redeem-status');
  el.textContent = msg;
  el.hidden = false;
  el.style.color = bad ? 'var(--bad)' : 'var(--accent)';
}

async function redeem(ev) {
  ev.preventDefault();
  const code = $('redeem-code').value.trim();
  const password = $('redeem-pass').value;
  if (password.length < 8) {
    showStatus(t('badPass'), true);
    return;
  }
  const r = await fetch('/api/auth/redeem', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, password }),
  });
  if (!r.ok) {
    const detail = (await r.json().catch(() => ({}))).detail || '';
    showStatus(detail === 'bad_password' ? t('badPass') : t('badCode'), true);
    return;
  }
  const res = await r.json();
  // Logged straight in: hand the token to the PWA so the user lands in the app.
  localStorage.setItem('foreman.token', res.token);
  showStatus(t('ok'), false);
  setTimeout(() => { location.href = '/'; }, 1000);
}

applyLang();
$('lang-toggle').addEventListener('click', () => {
  lang = lang === 'zh' ? 'en' : 'zh';
  localStorage.setItem(LANG_KEY, lang);
  applyLang();
});
$('redeem-form').addEventListener('submit', redeem);
