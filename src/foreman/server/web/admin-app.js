/* Foreman 控制台 — Ant Design console.
 *
 * No build step: React + Ant Design + dayjs + htm are vendored UMD bundles loaded by app.html;
 * this file is plain first-party JS (htm gives JSX-like templates without a transpiler). Team
 * relay deployments are gated behind login; local desktop servers have no account manager and
 * fall straight through to the embedded control dashboard.
 * Admins land on the dashboard (概览/账户/会话/进程/数据库/日志); members get a minimal machine/key view.
 */
(function () {
  "use strict";

  const { useState, useEffect, useCallback, useRef, useMemo } = React;
  const html = htm.bind(React.createElement);
  const A = antd;
  const F = React.Fragment;
  const ICONS = window.icons || {};
  const icon = (name) => (ICONS[name] ? html`<${ICONS[name]} />` : null);

  // ── token + api ──────────────────────────────────────────────────────────────────────────
  const TOKEN_KEY = "foreman_token";
  const DASHBOARD_TOKEN_KEY = "foreman.token";
  const PROCESS_KEY = "foreman.process";
  const LANG_KEY = "foreman.lang";
  function normalizeUiLang(value) {
    return String(value || "").trim().toLowerCase().startsWith("zh") ? "zh" : "en";
  }
  function detectedUiLang() {
    const stored = localStorage.getItem(LANG_KEY);
    if (stored) return normalizeUiLang(stored);
    const langs = (navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ""]);
    return normalizeUiLang(langs[0]);
  }
  const uiLang = detectedUiLang();
  const zh = (cn, en) => (uiLang === "zh" ? cn : en);
  const keyStatus = (s) => ({ active: zh("活跃", "active"), revoked: zh("已吊销", "revoked"), expired: zh("已过期", "expired") }[s] || s);
  document.documentElement.lang = uiLang === "zh" ? "zh-CN" : "en";
  document.title = zh("Foreman · 控制台", "Foreman · Console");
  const getToken = () => localStorage.getItem(TOKEN_KEY) || localStorage.getItem(DASHBOARD_TOKEN_KEY) || "";
  const setToken = (t) => {
    if (t) {
      localStorage.setItem(TOKEN_KEY, t);
      localStorage.setItem(DASHBOARD_TOKEN_KEY, t);
    } else {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(DASHBOARD_TOKEN_KEY);
    }
  };
  function nextUrl() {
    const raw = new URLSearchParams(location.search).get("next") || "";
    return raw.startsWith("/") && !raw.startsWith("//") ? raw : "";
  }
  function finishAuth(onAuthed) {
    const next = nextUrl();
    if (next) {
      location.href = next;
      return;
    }
    onAuthed();
  }
  function wantsControlView() {
    const p = new URLSearchParams(location.search);
    return p.get("control") === "1" || ["view", "session", "process", "approval", "action"].some((k) => p.has(k));
  }
  function controlHref(processId) {
    const p = new URLSearchParams();
    p.set("control", "1");
    if (processId) p.set("process", processId);
    return "/app.html?" + p.toString();
  }

  async function api(path, opts) {
    opts = opts || {};
    const headers = {};
    const token = getToken();
    if (token) headers["Authorization"] = "Bearer " + token;
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.indexOf("application/json") >= 0) {
      try { data = await res.json(); } catch (e) { /* empty body */ }
    }
    if (!res.ok) {
      // A 401 anywhere means our token is gone/expired → kick the whole app back to login.
      if (res.status === 401 && token) window.dispatchEvent(new CustomEvent("foreman:unauthorized"));
      const err = new Error((data && data.detail) || res.statusText || "HTTP " + res.status);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  // ── formatting helpers ───────────────────────────────────────────────────────────────────
  const fmtTime = (iso) => (iso ? dayjs(iso).format("YYYY-MM-DD HH:mm:ss") : "—");
  const fmtFromNow = (iso) => (iso ? dayjs(iso).format("MM-DD HH:mm") : "—");
  function fmtBytes(n) {
    if (!n && n !== 0) return "—";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + " " + u[i];
  }
  function fmtDuration(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d) return uiLang === "zh" ? d + "天 " + h + "小时" : d + "d " + h + "h";
    if (h) return uiLang === "zh" ? h + "小时 " + m + "分" : h + "h " + m + "m";
    return uiLang === "zh" ? m + "分钟" : m + "m";
  }

  // toast bridge: A.App.useApp() gives theme-aware message/modal; stored here for non-component use.
  const ui = { message: null, modal: null };
  function MessageBridge() {
    const app = A.App.useApp();
    useEffect(() => { ui.message = app.message; ui.modal = app.modal; }, [app]);
    return null;
  }
  const toast = {
    ok: (m) => ui.message && ui.message.success(m),
    err: (m) => ui.message && ui.message.error(m),
    info: (m) => ui.message && ui.message.info(m),
  };

  // small async-load hook with reload()
  function useAsync(fn, deps) {
    const [s, setS] = useState({ loading: true, data: null, error: null });
    const ref = useRef(fn);
    ref.current = fn;
    const reload = useCallback(() => {
      setS((p) => ({ ...p, loading: true }));
      ref.current()
        .then((data) => setS({ loading: false, data, error: null }))
        .catch((error) => setS({ loading: false, data: null, error }));
    }, deps || []); // eslint-disable-line
    useEffect(() => { reload(); }, [reload]);
    return { ...s, reload };
  }

  // ── login + redeem ───────────────────────────────────────────────────────────────────────
  function LoginView({ onAuthed }) {
    const [tab, setTab] = useState("login");
    const [busy, setBusy] = useState(false);

    const doLogin = async (v) => {
      setBusy(true);
      try {
        const r = await api("/api/auth/login", { method: "POST", body: { username: v.username, password: v.password } });
        setToken(r.token);
        finishAuth(onAuthed);
      } catch (e) {
        toast.err(e.status === 429 ? zh("尝试过于频繁，请稍后再试", "Too many attempts. Try again later.") : zh("用户名或密码错误", "Wrong username or password"));
      } finally { setBusy(false); }
    };
    const doRedeem = async (v) => {
      setBusy(true);
      try {
        const r = await api("/api/auth/redeem", { method: "POST", body: { code: v.code.trim(), password: v.password } });
        setToken(r.token);
        toast.ok(zh("已设置密码并登录", "Password set. Signed in."));
        finishAuth(onAuthed);
      } catch (e) {
        toast.err(e.status === 400 ? zh("邀请码无效/已用/已过期，或密码太短(≥8位)", "Invalid, used, or expired invite code, or the password is too short (8+ chars)") : zh("兑换失败", "Invite redemption failed"));
      } finally { setBusy(false); }
    };

    const items = [
      {
        key: "login",
        label: zh("登录", "Sign in"),
        children: html`
          <${A.Form} layout="vertical" onFinish=${doLogin} requiredMark=${false} style=${{ marginTop: 8 }}>
            <${A.Form.Item} name="username" label=${zh("用户名", "Username")} rules=${[{ required: true, message: zh("请输入用户名", "Enter a username") }]}>
              <${A.Input} size="large" prefix=${icon("UserOutlined")} placeholder=${zh("用户名", "Username")} autoComplete="username" />
            </${A.Form.Item}>
            <${A.Form.Item} name="password" label=${zh("密码", "Password")} rules=${[{ required: true, message: zh("请输入密码", "Enter a password") }]}>
              <${A.Input.Password} size="large" prefix=${icon("LockOutlined")} placeholder=${zh("密码", "Password")} autoComplete="current-password" />
            </${A.Form.Item}>
            <${A.Button} type="primary" htmlType="submit" size="large" block loading=${busy}>${zh("登录", "Sign in")}</${A.Button}>
          </${A.Form}>`,
      },
      {
        key: "redeem",
        label: zh("兑换邀请码", "Redeem invite"),
        children: html`
          <${A.Form} layout="vertical" onFinish=${doRedeem} requiredMark=${false} style=${{ marginTop: 8 }}>
            <${A.Form.Item} name="code" label=${zh("邀请码", "Invite code")} rules=${[{ required: true, message: zh("请输入邀请码", "Enter the invite code") }]}>
              <${A.Input} size="large" placeholder=${zh("管理员发给你的一次性邀请码", "One-time invite code from an admin")} />
            </${A.Form.Item}>
            <${A.Form.Item} name="password" label=${zh("设置密码", "Set password")} rules=${[{ required: true, min: 8, message: zh("至少 8 位", "At least 8 characters") }]}>
              <${A.Input.Password} size="large" placeholder=${zh("为自己设置一个密码 (≥8位)", "Set your password (8+ chars)")} autoComplete="new-password" />
            </${A.Form.Item}>
            <${A.Button} type="primary" htmlType="submit" size="large" block loading=${busy}>${zh("设置密码并登录", "Set password and sign in")}</${A.Button}>
          </${A.Form}>`,
      },
    ];

    return html`
      <div style=${{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
        <${A.Card} style=${{ width: 380, maxWidth: "100%" }} variant="outlined">
          <div style=${{ textAlign: "center", marginBottom: 8 }}>
            <div style=${{ fontSize: 30 }}>🦺</div>
            <${A.Typography.Title} level=${3} style=${{ margin: "6px 0 0" }}>${zh("Foreman 控制台", "Foreman Console")}</${A.Typography.Title}>
            <${A.Typography.Text} type="secondary">${zh("请先登录", "Sign in to continue")}</${A.Typography.Text}>
          </div>
          <${A.Tabs} activeKey=${tab} onChange=${setTab} items=${items} centered />
        </${A.Card}>
      </div>`;
  }

  // ── admin: overview ──────────────────────────────────────────────────────────────────────
  function OverviewSection() {
    const { data, loading, error, reload } = useAsync(() => api("/api/admin/overview"));
    if (error) return html`<${A.Alert} type="error" message=${zh("加载失败: ", "Load failed: ") + error.message} />`;
    const d = data || {};
    const acc = d.accounts || {};
    const proc = d.processes || {};
    const stat = (title, value, suffix, color) =>
      html`<${A.Col} xs=${12} sm=${8} lg=${6}>
        <${A.Card} variant="outlined"><${A.Statistic} title=${title} value=${value} suffix=${suffix} valueStyle=${color ? { color } : undefined} /></${A.Card}>
      </${A.Col}>`;
    return html`
      <${F}>
        <${SectionHeader} title=${zh("概览", "Overview")} onReload=${reload} loading=${loading} />
        <${A.Spin} spinning=${loading}>
          <${A.Row} gutter=${[16, 16]}>
            ${stat(zh("账户总数", "Total accounts"), acc.total ?? 0)}
            ${stat(zh("活跃账户", "Active accounts"), acc.active ?? 0, null, "#52c41a")}
            ${stat(zh("待激活", "Pending activation"), acc.invited ?? 0, null, "#faad14")}
            ${stat(zh("已禁用", "Disabled"), acc.disabled ?? 0, null, "#ff4d4f")}
            ${stat(zh("在线进程", "Online processes"), proc.online ?? 0, "/ " + (proc.total ?? 0))}
            ${stat(zh("在线会话", "Online sessions"), d.active_sessions ?? 0)}
            ${stat(zh("数据库", "Database"), fmtBytes(d.db && d.db.size_bytes))}
            ${stat(zh("运行时长", "Uptime"), fmtDuration(d.uptime_seconds))}
          </${A.Row}>
          <${A.Descriptions} style=${{ marginTop: 16 }} bordered size="small" column=${1}
            items=${[
              { key: "v", label: zh("版本", "Version"), children: "v" + (d.version || "?") },
              { key: "m", label: zh("模式", "Mode"), children: d.mode || "team" },
              { key: "s", label: zh("数据库 schema", "Database schema"), children: String((d.db && d.db.schema_version) ?? "—") },
              { key: "p", label: zh("数据库路径", "Database path"), children: (d.db && d.db.path) || "—" },
            ]} />
        </${A.Spin}>
      </${F}>`;
  }

  function SectionHeader({ title, onReload, loading, extra }) {
    return html`
      <div style=${{ display: "flex", alignItems: "center", marginBottom: 16, gap: 8 }}>
        <${A.Typography.Title} level=${4} style=${{ margin: 0, flex: 1 }}>${title}</${A.Typography.Title}>
        ${extra}
        ${onReload &&
          html`<${A.Button} icon=${icon("ReloadOutlined")} onClick=${onReload} loading=${loading}>${zh("刷新", "Refresh")}</${A.Button}>`}
      </div>`;
  }

  // ── admin: accounts ──────────────────────────────────────────────────────────────────────
  function AccountsSection() {
    const { data, loading, error, reload } = useAsync(() => api("/api/admin/accounts"));
    const [creating, setCreating] = useState(false);
    const [form] = A.Form.useForm();

    const showInvite = (code) => {
      ui.modal &&
        ui.modal.info({
          title: zh("一次性邀请码（仅显示一次）", "One-time invite code (shown once)"),
          width: 460,
          content: html`
            <div>
              <${A.Typography.Paragraph} copyable=${{ text: code }} style=${{ fontFamily: "monospace", fontSize: 13, background: "rgba(127,127,127,.12)", padding: 8, borderRadius: 6, wordBreak: "break-all" }}>${code}</${A.Typography.Paragraph}>
              <${A.Typography.Text} type="secondary">${zh("把它发给用户，让 TA 在登录页「兑换邀请码」里设置密码。", "Send this to the user. They can set a password from the Redeem invite tab.")}</${A.Typography.Text}>
            </div>`,
        });
    };

    const onCreate = async (v) => {
      try {
        const body = { username: v.username, display_name: v.display_name || "", role: v.role || "member" };
        if (v.password) body.password = v.password;
        const r = await api("/api/admin/accounts", { method: "POST", body });
        setCreating(false);
        form.resetFields();
        reload();
        if (r && r.invite_code) showInvite(r.invite_code);
        else toast.ok(zh("用户已创建", "User created"));
      } catch (e) {
        toast.err(e.status === 409 ? zh("用户名已存在", "Username already exists") : zh("创建失败: ", "Create failed: ") + e.message);
      }
    };
    const setStatus = async (id, enabled) => {
      try { await api("/api/admin/accounts/" + id + "/status", { method: "POST", body: { enabled } }); toast.ok(enabled ? zh("已启用", "Enabled") : zh("已禁用", "Disabled")); reload(); }
      catch (e) { toast.err(zh("操作失败: ", "Operation failed: ") + e.message); }
    };
    const reinvite = async (id) => {
      try { const r = await api("/api/admin/accounts/" + id + "/invite", { method: "POST" }); if (r && r.invite_code) showInvite(r.invite_code); }
      catch (e) { toast.err(zh("重新邀请失败: ", "Re-invite failed: ") + e.message); }
    };

    const roleTag = (r) => html`<${A.Tag} color=${r === "admin" ? "geekblue" : "default"}>${r === "admin" ? zh("管理员", "Admin") : zh("成员", "Member")}</${A.Tag}>`;
    const statusTag = (s) =>
      html`<${A.Tag} color=${s === "active" ? "green" : s === "invited" ? "gold" : "red"}>${s === "active" ? zh("活跃", "Active") : s === "invited" ? zh("待激活", "Invited") : zh("已禁用", "Disabled")}</${A.Tag}>`;

    const columns = [
      { title: zh("用户名", "Username"), dataIndex: "username", key: "username", render: (t) => html`<strong>${t}</strong>` },
      { title: zh("显示名", "Display name"), dataIndex: "display_name", key: "display_name", responsive: ["md"] },
      { title: zh("角色", "Role"), dataIndex: "role", key: "role", render: roleTag },
      { title: zh("状态", "Status"), dataIndex: "status", key: "status", render: statusTag },
      { title: zh("创建时间", "Created"), dataIndex: "created_at", key: "created_at", responsive: ["lg"], render: fmtTime },
      {
        title: zh("操作", "Actions"), key: "ops", render: (_, row) =>
          html`<${A.Space} size="small">
            ${row.status === "active"
              ? html`<${A.Popconfirm} title=${zh("禁用该账户？", "Disable this account?")} onConfirm=${() => setStatus(row.id, false)} okText=${zh("禁用", "Disable")} cancelText=${zh("取消", "Cancel")}><${A.Button} size="small" danger>${zh("禁用", "Disable")}</${A.Button}></${A.Popconfirm}>`
              : html`<${A.Button} size="small" onClick=${() => setStatus(row.id, true)}>${zh("启用", "Enable")}</${A.Button}>`}
            <${A.Button} size="small" onClick=${() => reinvite(row.id)}>${zh("重新邀请", "Re-invite")}</${A.Button}>
          </${A.Space}>`,
      },
    ];

    return html`
      <${F}>
        <${SectionHeader} title=${zh("账户管理", "Account management")} onReload=${reload} loading=${loading}
          extra=${html`<${A.Button} type="primary" icon=${icon("PlusOutlined")} onClick=${() => setCreating(true)}>${zh("新建用户", "New user")}</${A.Button}>`} />
        ${error
          ? html`<${A.Alert} type="error" message=${zh("加载失败: ", "Load failed: ") + error.message} />`
          : html`<${A.Table} rowKey="id" loading=${loading} columns=${columns} dataSource=${data || []} size="middle" pagination=${{ pageSize: 20, hideOnSinglePage: true }} scroll=${{ x: "max-content" }} />`}
        <${A.Modal} title=${zh("新建用户", "New user")} open=${creating} onCancel=${() => setCreating(false)} onOk=${() => form.submit()} okText=${zh("创建", "Create")} cancelText=${zh("取消", "Cancel")} destroyOnClose>
          <${A.Form} form=${form} layout="vertical" onFinish=${onCreate} requiredMark=${false} preserve=${false}>
            <${A.Form.Item} name="username" label=${zh("用户名", "Username")} rules=${[{ required: true, message: zh("请输入用户名", "Enter a username") }]}>
              <${A.Input} placeholder="alice" />
            </${A.Form.Item}>
            <${A.Form.Item} name="display_name" label=${zh("显示名", "Display name")}><${A.Input} placeholder=${zh("Alice (可选)", "Alice (optional)")} /></${A.Form.Item}>
            <${A.Form.Item} name="role" label=${zh("角色", "Role")} initialValue="member">
              <${A.Select} options=${[{ value: "member", label: zh("成员", "Member") }, { value: "admin", label: zh("管理员", "Admin") }]} />
            </${A.Form.Item}>
            <${A.Form.Item} name="password" label=${zh("初始密码", "Initial password")} extra=${zh("留空 → 生成一次性邀请码（无自助注册）", "Leave blank to generate a one-time invite code (no self-signup)")}>
              <${A.Input} placeholder=${zh("(留空 = 发邀请码)", "(blank = invite code)")} />
            </${A.Form.Item}>
          </${A.Form}>
        </${A.Modal}>
      </${F}>`;
  }

  // ── admin: active sessions (登录账户 / 在线会话) ───────────────────────────────────────────
  function SessionsSection() {
    const { data, loading, error, reload } = useAsync(() => api("/api/admin/sessions"));
    const columns = [
      { title: zh("用户名", "Username"), dataIndex: "username", key: "username", render: (t) => html`<strong>${t}</strong>` },
      { title: zh("显示名", "Display name"), dataIndex: "display_name", key: "display_name", responsive: ["md"] },
      { title: zh("角色", "Role"), dataIndex: "role", key: "role", render: (r) => html`<${A.Tag} color=${r === "admin" ? "geekblue" : "default"}>${r === "admin" ? zh("管理员", "Admin") : zh("成员", "Member")}</${A.Tag}>` },
      { title: zh("登录时间", "Signed in"), dataIndex: "created_at", key: "created_at", render: fmtTime },
      { title: zh("到期时间", "Expires"), dataIndex: "expires_at", key: "expires_at", responsive: ["md"], render: fmtTime },
    ];
    return html`
      <${F}>
        <${SectionHeader} title=${zh("在线会话", "Online sessions")} onReload=${reload} loading=${loading} />
        ${error
          ? html`<${A.Alert} type="error" message=${zh("加载失败: ", "Load failed: ") + error.message} />`
          : html`<${A.Table} rowKey=${(r) => r.account_id + r.created_at} loading=${loading} columns=${columns} dataSource=${data || []} size="middle" locale=${{ emptyText: zh("当前没有活跃登录会话", "No active login sessions") }} pagination=${{ pageSize: 20, hideOnSinglePage: true }} scroll=${{ x: "max-content" }} />`}
      </${F}>`;
  }

  // ── admin: processes ─────────────────────────────────────────────────────────────────────
  function ProcessesSection({ onControl }) {
    const { data, loading, error, reload } = useAsync(() => api("/api/admin/processes"));
    const columns = [
      { title: zh("账户", "Account"), dataIndex: "username", key: "username", render: (t) => html`<strong>${t}</strong>` },
      { title: zh("机器", "Machine"), dataIndex: "name", key: "name", render: (t) => t || "—" },
      { title: zh("状态", "Status"), dataIndex: "online", key: "online", render: (o) => html`<${A.Badge} status=${o ? "success" : "default"} text=${o ? zh("在线", "Online") : zh("离线", "Offline")} />` },
      { title: zh("最后心跳", "Last heartbeat"), dataIndex: "last_heartbeat", key: "last_heartbeat", render: fmtTime },
      { title: zh("注册时间", "Registered"), dataIndex: "created_at", key: "created_at", responsive: ["lg"], render: fmtTime },
      { title: zh("操作", "Actions"), key: "ops", width: 96, render: (_, r) => html`<${A.Tooltip} title=${r.online ? zh("进入控制台：远程查看会话 / 派发任务 / 审批", "Open console: view sessions, dispatch tasks, and approve remotely") : zh("机器离线，无法控制", "Machine is offline")}><${A.Button} size="small" type="link" disabled=${!r.online} onClick=${() => onControl && onControl(r.id)}>${zh("控制", "Control")}</${A.Button}></${A.Tooltip}>` },
    ];
    return html`
      <${F}>
        <${SectionHeader} title=${zh("进程 / 机器", "Processes / machines")} onReload=${reload} loading=${loading} />
        ${error
          ? html`<${A.Alert} type="error" message=${zh("加载失败: ", "Load failed: ") + error.message} />`
          : html`<${A.Table} rowKey="id" loading=${loading} columns=${columns} dataSource=${data || []} size="middle" locale=${{ emptyText: zh("还没有机器接入", "No machines connected yet") }} pagination=${{ pageSize: 20, hideOnSinglePage: true }} scroll=${{ x: "max-content" }} />`}
      </${F}>`;
  }

  // ── admin: database ──────────────────────────────────────────────────────────────────────
  function DatabaseSection() {
    const { data, loading, error, reload } = useAsync(() => api("/api/admin/db"));
    const [active, setActive] = useState(null); // table name
    const [page, setPage] = useState({ rows: [], columns: [], total: 0, loading: false, offset: 0, limit: 50 });

    const loadTable = useCallback(async (name, offset) => {
      setPage((p) => ({ ...p, loading: true }));
      try {
        const r = await api("/api/admin/db/" + encodeURIComponent(name) + "?limit=50&offset=" + (offset || 0));
        setPage({ rows: r.rows || [], columns: r.columns || [], total: r.total || 0, loading: false, offset: r.offset || 0, limit: r.limit || 50 });
      } catch (e) { toast.err(zh("读取失败: ", "Read failed: ") + e.message); setPage((p) => ({ ...p, loading: false })); }
    }, []);
    const openTable = (name) => { setActive(name); loadTable(name, 0); };

    const maint = async (action) => {
      try {
        const r = await api("/api/admin/db/maintenance", { method: "POST", body: { action } });
        if (action === "integrity_check") {
          (r.result === "ok" ? toast.ok : toast.err)(zh("完整性检查: ", "Integrity check: ") + r.result);
        } else { toast.ok(zh("VACUUM 完成", "VACUUM completed")); reload(); }
      } catch (e) { toast.err(zh("操作失败: ", "Operation failed: ") + e.message); }
    };

    if (error) return html`<${A.Alert} type="error" message=${zh("加载失败: ", "Load failed: ") + error.message} />`;
    const d = data || {};

    const tableCols = [
      { title: zh("表", "Table"), dataIndex: "name", key: "name", render: (t) => html`<a onClick=${() => openTable(t)}>${t}</a>` },
      { title: zh("行数", "Rows"), dataIndex: "rows", key: "rows", align: "right", render: (n) => (n < 0 ? "—" : n.toLocaleString()) },
    ];
    const rowCols = (page.columns || []).map((c) => ({
      title: c, dataIndex: c, key: c, ellipsis: true,
      render: (v) => (v === null || v === undefined ? html`<${A.Typography.Text} type="secondary">null</${A.Typography.Text}>` : String(v)),
    }));

    return html`
      <${F}>
        <${SectionHeader} title=${zh("数据库管理", "Database management")} onReload=${reload} loading=${loading}
          extra=${html`<${A.Space}>
            <${A.Popconfirm} title=${zh("执行 VACUUM（整理碎片/回收空间）？", "Run VACUUM to reclaim database space?")} onConfirm=${() => maint("vacuum")} okText=${zh("执行", "Run")} cancelText=${zh("取消", "Cancel")}><${A.Button}>VACUUM</${A.Button}></${A.Popconfirm}>
            <${A.Button} onClick=${() => maint("integrity_check")}>${zh("完整性检查", "Integrity check")}</${A.Button}>
          </${A.Space}>`} />
        <${A.Spin} spinning=${loading}>
          <${A.Descriptions} bordered size="small" column=${{ xs: 1, sm: 3 }} style=${{ marginBottom: 16 }}
            items=${[
              { key: "size", label: zh("大小", "Size"), children: fmtBytes(d.size_bytes) },
              { key: "ver", label: "Schema", children: String(d.schema_version ?? "—") },
              { key: "path", label: zh("路径", "Path"), children: d.path || "—" },
            ]} />
          <${A.Row} gutter=${[16, 16]}>
            <${A.Col} xs=${24} lg=${8}>
              <${A.Card} size="small" title=${zh("数据表", "Tables")} variant="outlined">
                <${A.Table} rowKey="name" columns=${tableCols} dataSource=${d.tables || []} size="small" pagination=${false}
                  rowClassName=${(r) => (r.name === active ? "ant-table-row-selected" : "")} />
              </${A.Card}>
            </${A.Col}>
            <${A.Col} xs=${24} lg=${16}>
              <${A.Card} size="small" variant="outlined"
                title=${active ? zh("表内容：", "Table: ") + active + zh("（敏感列已脱敏）", " (sensitive columns redacted)") : zh("选择左侧的表查看内容", "Select a table on the left")}>
                ${active
                  ? html`<${A.Table} rowKey=${(_, i) => i} loading=${page.loading} columns=${rowCols} dataSource=${page.rows}
                      size="small" scroll=${{ x: "max-content" }}
                      pagination=${{
                        current: Math.floor(page.offset / page.limit) + 1, pageSize: page.limit, total: page.total,
                        showSizeChanger: false, onChange: (p) => loadTable(active, (p - 1) * page.limit),
                      }} />`
                  : html`<${A.Empty} description=${zh("未选择数据表", "No table selected")} />`}
              </${A.Card}>
            </${A.Col}>
          </${A.Row}>
        </${A.Spin}>
      </${F}>`;
  }

  // ── admin: logs ──────────────────────────────────────────────────────────────────────────
  function LogsSection() {
    const [level, setLevel] = useState("");
    const [auto, setAuto] = useState(false);
    const fetcher = useCallback(() => api("/api/admin/logs?limit=300" + (level ? "&level=" + level : "")), [level]);
    const { data, loading, error, reload } = useAsync(fetcher, [level]);

    useEffect(() => {
      if (!auto) return;
      const t = setInterval(reload, 5000);
      return () => clearInterval(t);
    }, [auto, reload]);

    const levelColor = (l) => ({ ERROR: "red", CRITICAL: "volcano", WARNING: "gold", INFO: "blue", DEBUG: "default" }[l] || "default");
    const columns = [
      { title: zh("时间", "Time"), dataIndex: "ts", key: "ts", width: 180, render: fmtTime },
      { title: zh("级别", "Level"), dataIndex: "level", key: "level", width: 100, render: (l) => html`<${A.Tag} color=${levelColor(l)}>${l}</${A.Tag}>` },
      { title: zh("来源", "Source"), dataIndex: "logger", key: "logger", width: 160, responsive: ["lg"], ellipsis: true },
      { title: zh("消息", "Message"), dataIndex: "msg", key: "msg", render: (m) => html`<span style=${{ fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>${m}</span>` },
    ];
    const records = (data && data.records) || [];

    return html`
      <${F}>
        <${SectionHeader} title=${zh("日志管理", "Logs")} onReload=${reload} loading=${loading}
          extra=${html`<${A.Space}>
            <${A.Select} value=${level} style=${{ width: 130 }} onChange=${setLevel}
              options=${[{ value: "", label: zh("全部级别", "All levels") }, { value: "INFO", label: "INFO" }, { value: "WARNING", label: "WARNING" }, { value: "ERROR", label: "ERROR" }]} />
            <span>${zh("自动刷新", "Auto refresh")} <${A.Switch} size="small" checked=${auto} onChange=${setAuto} /></span>
          </${A.Space}>`} />
        ${error
          ? html`<${A.Alert} type="error" message=${zh("加载失败: ", "Load failed: ") + error.message} />`
          : html`<${A.Table} rowKey=${(_, i) => i} loading=${loading} columns=${columns} dataSource=${records} size="small"
              locale=${{ emptyText: zh("暂无日志（进程重启后清空）", "No logs yet (cleared after process restart)") }} pagination=${{ pageSize: 50, hideOnSinglePage: true }} scroll=${{ x: "max-content" }} />`}
      </${F}>`;
  }

  // ── admin console shell ──────────────────────────────────────────────────────────────────
  const SECTIONS = {
    overview: { label: zh("概览", "Overview"), icon: "DashboardOutlined", comp: OverviewSection },
    accounts: { label: zh("账户", "Accounts"), icon: "TeamOutlined", comp: AccountsSection },
    sessions: { label: zh("在线会话", "Online sessions"), icon: "ClockCircleOutlined", comp: SessionsSection },
    processes: { label: zh("进程", "Processes"), icon: "ApiOutlined", comp: ProcessesSection },
    database: { label: zh("数据库", "Database"), icon: "DatabaseOutlined", comp: DatabaseSection },
    logs: { label: zh("日志", "Logs"), icon: "FileTextOutlined", comp: LogsSection },
  };

  function ControlView({ onBack }) {
    const ControlRoot = window.ForemanControlApp && window.ForemanControlApp.Root;
    if (!ControlRoot) {
      return html`<div style=${{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}>
        <${A.Result} status="error" title=${zh("控制台组件未加载", "Console component did not load")} subTitle=${zh("请刷新页面后重试。", "Refresh the page and try again.")} extra=${html`<${A.Button} type="primary" onClick=${onBack}>${zh("返回总控制台", "Back to main console")}</${A.Button}>`} />
      </div>`;
    }
    return html`<${ControlRoot} embedded=${true} onBack=${onBack} />`;
  }

  function AdminConsole({ me, onLogout, dark, onToggleTheme, onControl }) {
    const [key, setKey] = useState("overview");
    const [collapsed, setCollapsed] = useState(false);
    const Section = SECTIONS[key].comp;
    const menuItems = Object.keys(SECTIONS).map((k) => ({ key: k, icon: icon(SECTIONS[k].icon), label: SECTIONS[k].label }));

    return html`
      <${A.Layout} style=${{ minHeight: "100vh" }}>
        <${A.Layout.Sider} breakpoint="lg" collapsedWidth=${0} collapsible collapsed=${collapsed} onCollapse=${setCollapsed} theme=${dark ? "dark" : "light"}>
          <div style=${{ height: 56, display: "flex", alignItems: "center", justifyContent: "center", fontSize: collapsed ? 22 : 16, fontWeight: 600, color: dark ? "#fff" : "#141414" }}>
            ${collapsed ? "🦺" : "🦺 Foreman"}
          </div>
          <${A.Menu} mode="inline" theme=${dark ? "dark" : "light"} selectedKeys=${[key]} onClick=${(e) => setKey(e.key)} items=${menuItems} />
        </${A.Layout.Sider}>
        <${A.Layout}>
          <${A.Layout.Header} style=${{ padding: "0 16px", display: "flex", alignItems: "center", background: dark ? "#1f1f1f" : "#fff", borderBottom: dark ? "1px solid #303030" : "1px solid #f0f0f0" }}>
            <${A.Typography.Text} strong style=${{ flex: 1, fontSize: 16 }}>${SECTIONS[key].label}</${A.Typography.Text}>
            <${A.Space} size="middle">
              <${A.Tooltip} title=${dark ? zh("切换浅色", "Switch to light") : zh("切换深色", "Switch to dark")}>
                <${A.Button} type="text" icon=${icon(dark ? "BulbOutlined" : "BulbFilled")} onClick=${onToggleTheme} />
              </${A.Tooltip}>
              <${A.Dropdown} menu=${{ items: [{ key: "logout", icon: icon("LogoutOutlined"), label: zh("退出登录", "Log out"), danger: true, onClick: onLogout }] }}>
                <${A.Space} style=${{ cursor: "pointer" }}>
                  <${A.Avatar} size="small" style=${{ background: "#1677ff" }}>${(me.display_name || me.username || "?").slice(0, 1).toUpperCase()}</${A.Avatar}>
                  <span>${me.display_name || me.username} <${A.Tag} color="geekblue" style=${{ marginInlineStart: 4 }}>${zh("管理员", "Admin")}</${A.Tag}></span>
                </${A.Space}>
              </${A.Dropdown}>
            </${A.Space}>
          </${A.Layout.Header}>
          <${A.Layout.Content} style=${{ margin: 16 }}>
            <div style=${{ background: dark ? "#141414" : "#fff", padding: 20, borderRadius: 8, minHeight: "100%" }}>
              <${Section} onControl=${onControl} />
            </div>
          </${A.Layout.Content}>
        </${A.Layout}>
      </${A.Layout}>`;
  }

  // ── member view (non-admin) ──────────────────────────────────────────────────────────────
  function MemberView({ me, onLogout, dark, onToggleTheme, onControl }) {
    const procs = useAsync(() => api("/api/processes"));
    const keys = useAsync(() => api("/api/keys"));
    const [minting, setMinting] = useState(false);
    const [form] = A.Form.useForm();

    // Relay 接入地址：本机进程拨号的总机 WS 端点。http→ws / https→wss，路径固定 /relay
    // (server: @app.websocket("/relay"); client/core/cloud.py 也接受裸 host 自动补 /relay)。
    const relayUrl = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/relay";

    const onMint = async (v) => {
      try {
        const body = { label: (v.label || "").trim() };
        if (v.expires_in_days) body.expires_in_days = Number(v.expires_in_days);
        const r = await api("/api/keys", { method: "POST", body });
        setMinting(false);
        form.resetFields();
        keys.reload();
        ui.modal && ui.modal.info({
          title: zh("新的访问密钥（仅显示一次）", "New access key (shown once)"),
          content: html`<${A.Typography.Paragraph} copyable=${{ text: r.key }} style=${{ fontFamily: "monospace", wordBreak: "break-all" }}>${r.key}</${A.Typography.Paragraph}>`,
        });
      } catch (e) { toast.err(zh("创建失败: ", "Create failed: ") + e.message); }
    };
    const revoke = async (id) => { try { await api("/api/keys/" + id, { method: "DELETE" }); toast.ok(zh("已吊销", "Revoked")); keys.reload(); } catch (e) { toast.err(e.message); } };

    // 「控制」入口：仍然先写目标机器和 dashboard token，但渲染留在 /app.html 的同一个
    // React 根应用里，由 Root 切换到控制台视图。
    const control = (row) => {
      if (onControl) onControl(row && row.id);
    };

    return html`
      <${A.Layout} style=${{ minHeight: "100vh" }}>
        <${A.Layout.Header} style=${{ padding: "0 16px", display: "flex", alignItems: "center", background: dark ? "#1f1f1f" : "#fff" }}>
          <${A.Typography.Text} strong style=${{ flex: 1, fontSize: 16 }}>🦺 Foreman</${A.Typography.Text}>
          <${A.Space}>
            <${A.Button} type="text" icon=${icon(dark ? "BulbOutlined" : "BulbFilled")} onClick=${onToggleTheme} />
            <span>${me.display_name || me.username}</span>
            <${A.Button} icon=${icon("LogoutOutlined")} onClick=${onLogout}>${zh("退出", "Log out")}</${A.Button}>
          </${A.Space}>
        </${A.Layout.Header}>
        <${A.Layout.Content} style=${{ padding: 16, maxWidth: 900, margin: "0 auto", width: "100%" }}>
          <${A.Card} title=${zh("我的机器", "My machines")} style=${{ marginBottom: 16 }} extra=${html`<${A.Button} size="small" onClick=${procs.reload}>${zh("刷新", "Refresh")}</${A.Button}>`}>
            <${A.Table} rowKey="id" loading=${procs.loading} pagination=${false} size="small"
              locale=${{ emptyText: zh("还没有机器接入", "No machines connected yet") }}
              columns=${[
                { title: zh("机器", "Machine"), dataIndex: "name", render: (t) => t || "—" },
                { title: zh("状态", "Status"), dataIndex: "online", render: (o) => html`<${A.Badge} status=${o ? "success" : "default"} text=${o ? zh("在线", "Online") : zh("离线", "Offline")} />` },
                { title: zh("最后心跳", "Last heartbeat"), dataIndex: "last_heartbeat", render: fmtTime },
                { title: zh("操作", "Actions"), key: "ops", width: 96, render: (_, r) => html`<${A.Tooltip} title=${r.online ? zh("进入控制台：远程查看会话 / 派发任务 / 审批", "Open console: view sessions, dispatch tasks, and approve remotely") : zh("机器离线，无法控制", "Machine is offline")}><${A.Button} size="small" type="link" disabled=${!r.online} onClick=${() => control(r)}>${zh("控制", "Control")}</${A.Button}></${A.Tooltip}>` },
              ]} dataSource=${procs.data || []} />
            <${A.Typography.Text} type="secondary" style=${{ fontSize: 12 }}>${zh("点「控制」进入控制台：可远程查看该机器的会话与卡片；要远程派发任务 / 审批，需先在该机器本地 Foreman 开启「远端执行」。", "Use Control to open the console for that machine. You can view sessions and cards remotely; remote task dispatch and approvals require Remote execution to be enabled on the local Foreman app.")}</${A.Typography.Text}>
          </${A.Card}>
          <${A.Card} size="small" style=${{ marginBottom: 16 }}>
            <${A.Typography.Text} type="secondary" style=${{ fontSize: 12 }}>${zh("Relay 接入地址 · 复制到本机 Foreman「云端连接设置 → 云端地址」", "Relay address · copy into local Foreman Cloud connection settings → Cloud URL")}</${A.Typography.Text}>
            <${A.Typography.Paragraph} copyable=${{ text: relayUrl }} style=${{ fontFamily: "monospace", margin: "4px 0 0", wordBreak: "break-all" }}>${relayUrl}</${A.Typography.Paragraph}>
          </${A.Card}>
          <${A.Card} title=${zh("访问密钥", "Access keys")} extra=${html`<${A.Button} size="small" type="primary" onClick=${() => setMinting(true)}>${zh("新建密钥", "New key")}</${A.Button}>`}>
            <${A.Table} rowKey="id" loading=${keys.loading} pagination=${false} size="small"
              locale=${{ emptyText: zh("还没有访问密钥", "No access keys yet") }}
              columns=${[
                { title: zh("标签", "Label"), dataIndex: "label", render: (t) => t || zh("(无标签)", "(no label)") },
                { title: zh("状态", "Status"), dataIndex: "status", render: (s) => html`<${A.Tag} color=${s === "active" ? "green" : "red"}>${keyStatus(s)}</${A.Tag}>` },
                { title: zh("创建时间", "Created"), dataIndex: "created_at", render: fmtTime },
                { title: zh("操作", "Actions"), key: "ops", render: (_, r) => (r.active ? html`<${A.Popconfirm} title=${zh("吊销该密钥？", "Revoke this key?")} onConfirm=${() => revoke(r.id)} okText=${zh("吊销", "Revoke")} cancelText=${zh("取消", "Cancel")}><${A.Button} size="small" danger>${zh("吊销", "Revoke")}</${A.Button}></${A.Popconfirm}>` : null) },
              ]} dataSource=${keys.data || []} />
          </${A.Card}>
          <${A.Modal} title=${zh("新建访问密钥", "New access key")} open=${minting} onCancel=${() => setMinting(false)} onOk=${() => form.submit()} okText=${zh("生成", "Generate")} cancelText=${zh("取消", "Cancel")} destroyOnClose>
            <${A.Form} form=${form} layout="vertical" onFinish=${onMint} requiredMark=${false} preserve=${false} initialValues=${{ expires_in_days: 0 }}>
              <${A.Form.Item} name="label" label=${zh("标签", "Label")} rules=${[{ required: true, message: zh("请给这台机器起个标签，便于日后辨认 / 吊销", "Give this machine a label so you can identify or revoke it later") }]}>
                <${A.Input} placeholder=${zh("例如：我的台式机", "Example: desktop PC")} maxLength=${60} autoFocus />
              </${A.Form.Item}>
              <${A.Form.Item} name="expires_in_days" label=${zh("有效期", "Expires in")} tooltip=${zh("到期后该密钥自动失效；选「永久」则永不过期。", "The key expires automatically; choose never to keep it permanent.")}>
                <${A.Select} options=${[
                  { value: 0, label: zh("永久", "Never") },
                  { value: 7, label: zh("7 天", "7 days") },
                  { value: 30, label: zh("30 天", "30 days") },
                  { value: 90, label: zh("90 天", "90 days") },
                  { value: 365, label: zh("365 天", "365 days") },
                ]} />
              </${A.Form.Item}>
            </${A.Form}>
          </${A.Modal}>
        </${A.Layout.Content}>
      </${A.Layout}>`;
  }

  // ── root: auth gate + theme ──────────────────────────────────────────────────────────────
  function Root() {
    const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const [dark, setDark] = useState(localStorage.getItem("foreman_theme") ? localStorage.getItem("foreman_theme") === "dark" : prefersDark);
    const [state, setState] = useState({ phase: "loading", me: null }); // loading | login | ready | local
    const [controlMode, setControlMode] = useState(wantsControlView);

    const checkAuth = useCallback(() => {
      api("/api/auth/me")
        .then((me) => {
          if (me && (me.display_name || me.username)) localStorage.setItem("foreman.user", me.display_name || me.username);
          setState({ phase: "ready", me });
        })
        .catch((e) => {
          if (e.status === 503) {
            setToken("");
            setState({ phase: "local", me: null });
          }
          else { setToken(""); setState({ phase: "login", me: null }); }
        });
    }, []);
    useEffect(() => { checkAuth(); }, [checkAuth]);
    useEffect(() => {
      const onUnauth = () => { setToken(""); setState({ phase: "login", me: null }); };
      window.addEventListener("foreman:unauthorized", onUnauth);
      return () => window.removeEventListener("foreman:unauthorized", onUnauth);
    }, []);
    useEffect(() => {
      const onPop = () => setControlMode(wantsControlView());
      window.addEventListener("popstate", onPop);
      return () => window.removeEventListener("popstate", onPop);
    }, []);

    const toggleTheme = () => setDark((d) => { localStorage.setItem("foreman_theme", !d ? "dark" : "light"); return !d; });
    const logout = async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* ignore */ } setToken(""); setState({ phase: "login", me: null }); };
    const openControl = useCallback((processId) => {
      const id = processId || "";
      if (id) localStorage.setItem(PROCESS_KEY, id);
      else localStorage.removeItem(PROCESS_KEY);
      const token = getToken();
      if (token) localStorage.setItem(DASHBOARD_TOKEN_KEY, token);
      history.pushState(null, "", controlHref(id));
      setControlMode(true);
    }, []);
    const closeControl = useCallback(() => {
      history.pushState(null, "", "/app.html");
      setControlMode(false);
    }, []);

    let body;
    if (state.phase === "loading") body = html`<div style=${{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}><${A.Spin} size="large" /></div>`;
    else if (state.phase === "local") body = html`<${ControlView} />`;
    else if (state.phase === "login") body = html`<${LoginView} onAuthed=${checkAuth} />`;
    else if (controlMode) body = html`<${ControlView} onBack=${closeControl} />`;
    else if (state.me && state.me.role === "admin") body = html`<${AdminConsole} me=${state.me} onLogout=${logout} dark=${dark} onToggleTheme=${toggleTheme} onControl=${openControl} />`;
    else body = html`<${MemberView} me=${state.me} onLogout=${logout} dark=${dark} onToggleTheme=${toggleTheme} onControl=${openControl} />`;

    return html`
      <${A.ConfigProvider} theme=${{ algorithm: dark ? A.theme.darkAlgorithm : A.theme.defaultAlgorithm, token: { colorPrimary: "#1677ff" } }}>
        <${A.App} style=${{ minHeight: "100vh" }}>
          <${MessageBridge} />
          ${body}
        </${A.App}>
      </${A.ConfigProvider}>`;
  }

  const rootEl = document.getElementById("root");
  const boot = document.getElementById("boot");
  if (boot) boot.remove(); // drop the pre-React loading shell; React owns #root from here
  ReactDOM.createRoot(rootEl).render(html`<${Root} />`);
})();
