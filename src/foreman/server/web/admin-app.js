/* Foreman 控制台 — login-gated Ant Design SPA (team mode).
 *
 * No build step: React + Ant Design + dayjs + htm are vendored UMD bundles loaded by app.html;
 * this file is plain first-party JS (htm gives JSX-like templates without a transpiler). The whole
 * app is gated behind login — nothing but the login screen renders until /api/auth/me succeeds.
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
    if (d) return d + "天 " + h + "小时";
    if (h) return h + "小时 " + m + "分";
    return m + "分钟";
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
        onAuthed();
      } catch (e) {
        toast.err(e.status === 429 ? "尝试过于频繁，请稍后再试" : "用户名或密码错误");
      } finally { setBusy(false); }
    };
    const doRedeem = async (v) => {
      setBusy(true);
      try {
        const r = await api("/api/auth/redeem", { method: "POST", body: { code: v.code.trim(), password: v.password } });
        setToken(r.token);
        toast.ok("已设置密码并登录");
        onAuthed();
      } catch (e) {
        toast.err(e.status === 400 ? "邀请码无效/已用/已过期，或密码太短(≥8位)" : "兑换失败");
      } finally { setBusy(false); }
    };

    const items = [
      {
        key: "login",
        label: "登录",
        children: html`
          <${A.Form} layout="vertical" onFinish=${doLogin} requiredMark=${false} style=${{ marginTop: 8 }}>
            <${A.Form.Item} name="username" label="用户名" rules=${[{ required: true, message: "请输入用户名" }]}>
              <${A.Input} size="large" prefix=${icon("UserOutlined")} placeholder="用户名" autoComplete="username" />
            </${A.Form.Item}>
            <${A.Form.Item} name="password" label="密码" rules=${[{ required: true, message: "请输入密码" }]}>
              <${A.Input.Password} size="large" prefix=${icon("LockOutlined")} placeholder="密码" autoComplete="current-password" />
            </${A.Form.Item}>
            <${A.Button} type="primary" htmlType="submit" size="large" block loading=${busy}>登录</${A.Button}>
          </${A.Form}>`,
      },
      {
        key: "redeem",
        label: "兑换邀请码",
        children: html`
          <${A.Form} layout="vertical" onFinish=${doRedeem} requiredMark=${false} style=${{ marginTop: 8 }}>
            <${A.Form.Item} name="code" label="邀请码" rules=${[{ required: true, message: "请输入邀请码" }]}>
              <${A.Input} size="large" placeholder="管理员发给你的一次性邀请码" />
            </${A.Form.Item}>
            <${A.Form.Item} name="password" label="设置密码" rules=${[{ required: true, min: 8, message: "至少 8 位" }]}>
              <${A.Input.Password} size="large" placeholder="为自己设置一个密码 (≥8位)" autoComplete="new-password" />
            </${A.Form.Item}>
            <${A.Button} type="primary" htmlType="submit" size="large" block loading=${busy}>设置密码并登录</${A.Button}>
          </${A.Form}>`,
      },
    ];

    return html`
      <div style=${{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
        <${A.Card} style=${{ width: 380, maxWidth: "100%" }} variant="outlined">
          <div style=${{ textAlign: "center", marginBottom: 8 }}>
            <div style=${{ fontSize: 30 }}>🦺</div>
            <${A.Typography.Title} level=${3} style=${{ margin: "6px 0 0" }}>Foreman 控制台</${A.Typography.Title}>
            <${A.Typography.Text} type="secondary">请先登录</${A.Typography.Text}>
          </div>
          <${A.Tabs} activeKey=${tab} onChange=${setTab} items=${items} centered />
        </${A.Card}>
      </div>`;
  }

  // ── admin: overview ──────────────────────────────────────────────────────────────────────
  function OverviewSection() {
    const { data, loading, error, reload } = useAsync(() => api("/api/admin/overview"));
    if (error) return html`<${A.Alert} type="error" message=${"加载失败: " + error.message} />`;
    const d = data || {};
    const acc = d.accounts || {};
    const proc = d.processes || {};
    const stat = (title, value, suffix, color) =>
      html`<${A.Col} xs=${12} sm=${8} lg=${6}>
        <${A.Card} variant="outlined"><${A.Statistic} title=${title} value=${value} suffix=${suffix} valueStyle=${color ? { color } : undefined} /></${A.Card}>
      </${A.Col}>`;
    return html`
      <${F}>
        <${SectionHeader} title="概览" onReload=${reload} loading=${loading} />
        <${A.Spin} spinning=${loading}>
          <${A.Row} gutter=${[16, 16]}>
            ${stat("账户总数", acc.total ?? 0)}
            ${stat("活跃账户", acc.active ?? 0, null, "#52c41a")}
            ${stat("待激活", acc.invited ?? 0, null, "#faad14")}
            ${stat("已禁用", acc.disabled ?? 0, null, "#ff4d4f")}
            ${stat("在线进程", proc.online ?? 0, "/ " + (proc.total ?? 0))}
            ${stat("在线会话", d.active_sessions ?? 0)}
            ${stat("数据库", fmtBytes(d.db && d.db.size_bytes))}
            ${stat("运行时长", fmtDuration(d.uptime_seconds))}
          </${A.Row}>
          <${A.Descriptions} style=${{ marginTop: 16 }} bordered size="small" column=${1}
            items=${[
              { key: "v", label: "版本", children: "v" + (d.version || "?") },
              { key: "m", label: "模式", children: d.mode || "team" },
              { key: "s", label: "数据库 schema", children: String((d.db && d.db.schema_version) ?? "—") },
              { key: "p", label: "数据库路径", children: (d.db && d.db.path) || "—" },
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
          html`<${A.Button} icon=${icon("ReloadOutlined")} onClick=${onReload} loading=${loading}>刷新</${A.Button}>`}
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
          title: "一次性邀请码（仅显示一次）",
          width: 460,
          content: html`
            <div>
              <${A.Typography.Paragraph} copyable=${{ text: code }} style=${{ fontFamily: "monospace", fontSize: 13, background: "rgba(127,127,127,.12)", padding: 8, borderRadius: 6, wordBreak: "break-all" }}>${code}</${A.Typography.Paragraph}>
              <${A.Typography.Text} type="secondary">把它发给用户，让 TA 在登录页「兑换邀请码」里设置密码。</${A.Typography.Text}>
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
        else toast.ok("用户已创建");
      } catch (e) {
        toast.err(e.status === 409 ? "用户名已存在" : "创建失败: " + e.message);
      }
    };
    const setStatus = async (id, enabled) => {
      try { await api("/api/admin/accounts/" + id + "/status", { method: "POST", body: { enabled } }); toast.ok(enabled ? "已启用" : "已禁用"); reload(); }
      catch (e) { toast.err("操作失败: " + e.message); }
    };
    const reinvite = async (id) => {
      try { const r = await api("/api/admin/accounts/" + id + "/invite", { method: "POST" }); if (r && r.invite_code) showInvite(r.invite_code); }
      catch (e) { toast.err("重新邀请失败: " + e.message); }
    };

    const roleTag = (r) => html`<${A.Tag} color=${r === "admin" ? "geekblue" : "default"}>${r === "admin" ? "管理员" : "成员"}</${A.Tag}>`;
    const statusTag = (s) =>
      html`<${A.Tag} color=${s === "active" ? "green" : s === "invited" ? "gold" : "red"}>${s === "active" ? "活跃" : s === "invited" ? "待激活" : "已禁用"}</${A.Tag}>`;

    const columns = [
      { title: "用户名", dataIndex: "username", key: "username", render: (t) => html`<strong>${t}</strong>` },
      { title: "显示名", dataIndex: "display_name", key: "display_name", responsive: ["md"] },
      { title: "角色", dataIndex: "role", key: "role", render: roleTag },
      { title: "状态", dataIndex: "status", key: "status", render: statusTag },
      { title: "创建时间", dataIndex: "created_at", key: "created_at", responsive: ["lg"], render: fmtTime },
      {
        title: "操作", key: "ops", render: (_, row) =>
          html`<${A.Space} size="small">
            ${row.status === "active"
              ? html`<${A.Popconfirm} title="禁用该账户？" onConfirm=${() => setStatus(row.id, false)} okText="禁用" cancelText="取消"><${A.Button} size="small" danger>禁用</${A.Button}></${A.Popconfirm}>`
              : html`<${A.Button} size="small" onClick=${() => setStatus(row.id, true)}>启用</${A.Button}>`}
            <${A.Button} size="small" onClick=${() => reinvite(row.id)}>重新邀请</${A.Button}>
          </${A.Space}>`,
      },
    ];

    return html`
      <${F}>
        <${SectionHeader} title="账户管理" onReload=${reload} loading=${loading}
          extra=${html`<${A.Button} type="primary" icon=${icon("PlusOutlined")} onClick=${() => setCreating(true)}>新建用户</${A.Button}>`} />
        ${error
          ? html`<${A.Alert} type="error" message=${"加载失败: " + error.message} />`
          : html`<${A.Table} rowKey="id" loading=${loading} columns=${columns} dataSource=${data || []} size="middle" pagination=${{ pageSize: 20, hideOnSinglePage: true }} scroll=${{ x: "max-content" }} />`}
        <${A.Modal} title="新建用户" open=${creating} onCancel=${() => setCreating(false)} onOk=${() => form.submit()} okText="创建" cancelText="取消" destroyOnClose>
          <${A.Form} form=${form} layout="vertical" onFinish=${onCreate} requiredMark=${false} preserve=${false}>
            <${A.Form.Item} name="username" label="用户名" rules=${[{ required: true, message: "请输入用户名" }]}>
              <${A.Input} placeholder="alice" />
            </${A.Form.Item}>
            <${A.Form.Item} name="display_name" label="显示名"><${A.Input} placeholder="Alice (可选)" /></${A.Form.Item}>
            <${A.Form.Item} name="role" label="角色" initialValue="member">
              <${A.Select} options=${[{ value: "member", label: "成员" }, { value: "admin", label: "管理员" }]} />
            </${A.Form.Item}>
            <${A.Form.Item} name="password" label="初始密码" extra="留空 → 生成一次性邀请码（无自助注册）">
              <${A.Input} placeholder="(留空 = 发邀请码)" />
            </${A.Form.Item}>
          </${A.Form}>
        </${A.Modal}>
      </${F}>`;
  }

  // ── admin: active sessions (登录账户 / 在线会话) ───────────────────────────────────────────
  function SessionsSection() {
    const { data, loading, error, reload } = useAsync(() => api("/api/admin/sessions"));
    const columns = [
      { title: "用户名", dataIndex: "username", key: "username", render: (t) => html`<strong>${t}</strong>` },
      { title: "显示名", dataIndex: "display_name", key: "display_name", responsive: ["md"] },
      { title: "角色", dataIndex: "role", key: "role", render: (r) => html`<${A.Tag} color=${r === "admin" ? "geekblue" : "default"}>${r === "admin" ? "管理员" : "成员"}</${A.Tag}>` },
      { title: "登录时间", dataIndex: "created_at", key: "created_at", render: fmtTime },
      { title: "到期时间", dataIndex: "expires_at", key: "expires_at", responsive: ["md"], render: fmtTime },
    ];
    return html`
      <${F}>
        <${SectionHeader} title="在线会话" onReload=${reload} loading=${loading} />
        ${error
          ? html`<${A.Alert} type="error" message=${"加载失败: " + error.message} />`
          : html`<${A.Table} rowKey=${(r) => r.account_id + r.created_at} loading=${loading} columns=${columns} dataSource=${data || []} size="middle" locale=${{ emptyText: "当前没有活跃登录会话" }} pagination=${{ pageSize: 20, hideOnSinglePage: true }} scroll=${{ x: "max-content" }} />`}
      </${F}>`;
  }

  // ── admin: processes ─────────────────────────────────────────────────────────────────────
  function ProcessesSection() {
    const { data, loading, error, reload } = useAsync(() => api("/api/admin/processes"));
    const columns = [
      { title: "账户", dataIndex: "username", key: "username", render: (t) => html`<strong>${t}</strong>` },
      { title: "机器", dataIndex: "name", key: "name", render: (t) => t || "—" },
      { title: "状态", dataIndex: "online", key: "online", render: (o) => html`<${A.Badge} status=${o ? "success" : "default"} text=${o ? "在线" : "离线"} />` },
      { title: "最后心跳", dataIndex: "last_heartbeat", key: "last_heartbeat", render: fmtTime },
      { title: "注册时间", dataIndex: "created_at", key: "created_at", responsive: ["lg"], render: fmtTime },
    ];
    return html`
      <${F}>
        <${SectionHeader} title="进程 / 机器" onReload=${reload} loading=${loading} />
        ${error
          ? html`<${A.Alert} type="error" message=${"加载失败: " + error.message} />`
          : html`<${A.Table} rowKey="id" loading=${loading} columns=${columns} dataSource=${data || []} size="middle" locale=${{ emptyText: "还没有机器接入" }} pagination=${{ pageSize: 20, hideOnSinglePage: true }} scroll=${{ x: "max-content" }} />`}
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
      } catch (e) { toast.err("读取失败: " + e.message); setPage((p) => ({ ...p, loading: false })); }
    }, []);
    const openTable = (name) => { setActive(name); loadTable(name, 0); };

    const maint = async (action) => {
      try {
        const r = await api("/api/admin/db/maintenance", { method: "POST", body: { action } });
        if (action === "integrity_check") {
          (r.result === "ok" ? toast.ok : toast.err)("完整性检查: " + r.result);
        } else { toast.ok("VACUUM 完成"); reload(); }
      } catch (e) { toast.err("操作失败: " + e.message); }
    };

    if (error) return html`<${A.Alert} type="error" message=${"加载失败: " + error.message} />`;
    const d = data || {};

    const tableCols = [
      { title: "表", dataIndex: "name", key: "name", render: (t) => html`<a onClick=${() => openTable(t)}>${t}</a>` },
      { title: "行数", dataIndex: "rows", key: "rows", align: "right", render: (n) => (n < 0 ? "—" : n.toLocaleString()) },
    ];
    const rowCols = (page.columns || []).map((c) => ({
      title: c, dataIndex: c, key: c, ellipsis: true,
      render: (v) => (v === null || v === undefined ? html`<${A.Typography.Text} type="secondary">null</${A.Typography.Text}>` : String(v)),
    }));

    return html`
      <${F}>
        <${SectionHeader} title="数据库管理" onReload=${reload} loading=${loading}
          extra=${html`<${A.Space}>
            <${A.Popconfirm} title="执行 VACUUM（整理碎片/回收空间）？" onConfirm=${() => maint("vacuum")} okText="执行" cancelText="取消"><${A.Button}>VACUUM</${A.Button}></${A.Popconfirm}>
            <${A.Button} onClick=${() => maint("integrity_check")}>完整性检查</${A.Button}>
          </${A.Space}>`} />
        <${A.Spin} spinning=${loading}>
          <${A.Descriptions} bordered size="small" column=${{ xs: 1, sm: 3 }} style=${{ marginBottom: 16 }}
            items=${[
              { key: "size", label: "大小", children: fmtBytes(d.size_bytes) },
              { key: "ver", label: "Schema", children: String(d.schema_version ?? "—") },
              { key: "path", label: "路径", children: d.path || "—" },
            ]} />
          <${A.Row} gutter=${[16, 16]}>
            <${A.Col} xs=${24} lg=${8}>
              <${A.Card} size="small" title="数据表" variant="outlined">
                <${A.Table} rowKey="name" columns=${tableCols} dataSource=${d.tables || []} size="small" pagination=${false}
                  rowClassName=${(r) => (r.name === active ? "ant-table-row-selected" : "")} />
              </${A.Card}>
            </${A.Col}>
            <${A.Col} xs=${24} lg=${16}>
              <${A.Card} size="small" variant="outlined"
                title=${active ? "表内容：" + active + "（敏感列已脱敏）" : "选择左侧的表查看内容"}>
                ${active
                  ? html`<${A.Table} rowKey=${(_, i) => i} loading=${page.loading} columns=${rowCols} dataSource=${page.rows}
                      size="small" scroll=${{ x: "max-content" }}
                      pagination=${{
                        current: Math.floor(page.offset / page.limit) + 1, pageSize: page.limit, total: page.total,
                        showSizeChanger: false, onChange: (p) => loadTable(active, (p - 1) * page.limit),
                      }} />`
                  : html`<${A.Empty} description="未选择数据表" />`}
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
      { title: "时间", dataIndex: "ts", key: "ts", width: 180, render: fmtTime },
      { title: "级别", dataIndex: "level", key: "level", width: 100, render: (l) => html`<${A.Tag} color=${levelColor(l)}>${l}</${A.Tag}>` },
      { title: "来源", dataIndex: "logger", key: "logger", width: 160, responsive: ["lg"], ellipsis: true },
      { title: "消息", dataIndex: "msg", key: "msg", render: (m) => html`<span style=${{ fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>${m}</span>` },
    ];
    const records = (data && data.records) || [];

    return html`
      <${F}>
        <${SectionHeader} title="日志管理" onReload=${reload} loading=${loading}
          extra=${html`<${A.Space}>
            <${A.Select} value=${level} style=${{ width: 130 }} onChange=${setLevel}
              options=${[{ value: "", label: "全部级别" }, { value: "INFO", label: "INFO" }, { value: "WARNING", label: "WARNING" }, { value: "ERROR", label: "ERROR" }]} />
            <span>自动刷新 <${A.Switch} size="small" checked=${auto} onChange=${setAuto} /></span>
          </${A.Space}>`} />
        ${error
          ? html`<${A.Alert} type="error" message=${"加载失败: " + error.message} />`
          : html`<${A.Table} rowKey=${(_, i) => i} loading=${loading} columns=${columns} dataSource=${records} size="small"
              locale=${{ emptyText: "暂无日志（进程重启后清空）" }} pagination=${{ pageSize: 50, hideOnSinglePage: true }} scroll=${{ x: "max-content" }} />`}
      </${F}>`;
  }

  // ── admin console shell ──────────────────────────────────────────────────────────────────
  const SECTIONS = {
    overview: { label: "概览", icon: "DashboardOutlined", comp: OverviewSection },
    accounts: { label: "账户", icon: "TeamOutlined", comp: AccountsSection },
    sessions: { label: "在线会话", icon: "ClockCircleOutlined", comp: SessionsSection },
    processes: { label: "进程", icon: "ApiOutlined", comp: ProcessesSection },
    database: { label: "数据库", icon: "DatabaseOutlined", comp: DatabaseSection },
    logs: { label: "日志", icon: "FileTextOutlined", comp: LogsSection },
  };

  function AdminConsole({ me, onLogout, dark, onToggleTheme }) {
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
              <${A.Tooltip} title=${dark ? "切换浅色" : "切换深色"}>
                <${A.Button} type="text" icon=${icon(dark ? "BulbOutlined" : "BulbFilled")} onClick=${onToggleTheme} />
              </${A.Tooltip}>
              <${A.Dropdown} menu=${{ items: [{ key: "logout", icon: icon("LogoutOutlined"), label: "退出登录", danger: true, onClick: onLogout }] }}>
                <${A.Space} style=${{ cursor: "pointer" }}>
                  <${A.Avatar} size="small" style=${{ background: "#1677ff" }}>${(me.display_name || me.username || "?").slice(0, 1).toUpperCase()}</${A.Avatar}>
                  <span>${me.display_name || me.username} <${A.Tag} color="geekblue" style=${{ marginInlineStart: 4 }}>管理员</${A.Tag}></span>
                </${A.Space}>
              </${A.Dropdown}>
            </${A.Space}>
          </${A.Layout.Header}>
          <${A.Layout.Content} style=${{ margin: 16 }}>
            <div style=${{ background: dark ? "#141414" : "#fff", padding: 20, borderRadius: 8, minHeight: "100%" }}>
              <${Section} />
            </div>
          </${A.Layout.Content}>
        </${A.Layout}>
      </${A.Layout}>`;
  }

  // ── member view (non-admin) ──────────────────────────────────────────────────────────────
  function MemberView({ me, onLogout, dark, onToggleTheme }) {
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
          title: "新的访问密钥（仅显示一次）",
          content: html`<${A.Typography.Paragraph} copyable=${{ text: r.key }} style=${{ fontFamily: "monospace", wordBreak: "break-all" }}>${r.key}</${A.Typography.Paragraph}>`,
        });
      } catch (e) { toast.err("创建失败: " + e.message); }
    };
    const revoke = async (id) => { try { await api("/api/keys/" + id, { method: "DELETE" }); toast.ok("已吊销"); keys.reload(); } catch (e) { toast.err(e.message); } };

    // 「控制」入口：把成员送进真正的控制台 PWA（index.html → app.js），那里有目标机器选择 +
    // 远端派发 / 审批 / 快照。同源 localStorage，所以先把要操控的机器写进 app.js 的 PROCESS_KEY
    // （"foreman.process"），让控制台开屏即选中这台；同时同步当前登录 token，避免旧 dashboard
    // token 残留时覆盖团队登录态。
    const control = (row) => {
      if (row && row.id) localStorage.setItem("foreman.process", row.id);
      const token = getToken();
      if (token) localStorage.setItem(DASHBOARD_TOKEN_KEY, token);
      location.href = "/index.html";
    };

    return html`
      <${A.Layout} style=${{ minHeight: "100vh" }}>
        <${A.Layout.Header} style=${{ padding: "0 16px", display: "flex", alignItems: "center", background: dark ? "#1f1f1f" : "#fff" }}>
          <${A.Typography.Text} strong style=${{ flex: 1, fontSize: 16 }}>🦺 Foreman</${A.Typography.Text}>
          <${A.Space}>
            <${A.Button} type="text" icon=${icon(dark ? "BulbOutlined" : "BulbFilled")} onClick=${onToggleTheme} />
            <span>${me.display_name || me.username}</span>
            <${A.Button} icon=${icon("LogoutOutlined")} onClick=${onLogout}>退出</${A.Button}>
          </${A.Space}>
        </${A.Layout.Header}>
        <${A.Layout.Content} style=${{ padding: 16, maxWidth: 900, margin: "0 auto", width: "100%" }}>
          <${A.Card} title="我的机器" style=${{ marginBottom: 16 }} extra=${html`<${A.Button} size="small" onClick=${procs.reload}>刷新</${A.Button}>`}>
            <${A.Table} rowKey="id" loading=${procs.loading} pagination=${false} size="small"
              locale=${{ emptyText: "还没有机器接入" }}
              columns=${[
                { title: "机器", dataIndex: "name", render: (t) => t || "—" },
                { title: "状态", dataIndex: "online", render: (o) => html`<${A.Badge} status=${o ? "success" : "default"} text=${o ? "在线" : "离线"} />` },
                { title: "最后心跳", dataIndex: "last_heartbeat", render: fmtTime },
                { title: "操作", key: "ops", width: 96, render: (_, r) => html`<${A.Tooltip} title=${r.online ? "进入控制台：远程查看会话 / 派发任务 / 审批" : "机器离线，无法控制"}><${A.Button} size="small" type="link" disabled=${!r.online} onClick=${() => control(r)}>控制</${A.Button}></${A.Tooltip}>` },
              ]} dataSource=${procs.data || []} />
            <${A.Typography.Text} type="secondary" style=${{ fontSize: 12 }}>点「控制」进入控制台：可远程查看该机器的会话与卡片；要远程派发任务 / 审批，需先在该机器本地 Foreman 开启「远端执行」。</${A.Typography.Text}>
          </${A.Card}>
          <${A.Card} size="small" style=${{ marginBottom: 16 }}>
            <${A.Typography.Text} type="secondary" style=${{ fontSize: 12 }}>Relay 接入地址 · 复制到本机 Foreman「云端连接设置 → 云端地址」</${A.Typography.Text}>
            <${A.Typography.Paragraph} copyable=${{ text: relayUrl }} style=${{ fontFamily: "monospace", margin: "4px 0 0", wordBreak: "break-all" }}>${relayUrl}</${A.Typography.Paragraph}>
          </${A.Card}>
          <${A.Card} title="访问密钥" extra=${html`<${A.Button} size="small" type="primary" onClick=${() => setMinting(true)}>新建密钥</${A.Button}>`}>
            <${A.Table} rowKey="id" loading=${keys.loading} pagination=${false} size="small"
              locale=${{ emptyText: "还没有访问密钥" }}
              columns=${[
                { title: "标签", dataIndex: "label", render: (t) => t || "(无标签)" },
                { title: "状态", dataIndex: "status", render: (s) => html`<${A.Tag} color=${s === "active" ? "green" : "red"}>${s}</${A.Tag}>` },
                { title: "创建时间", dataIndex: "created_at", render: fmtTime },
                { title: "操作", key: "ops", render: (_, r) => (r.active ? html`<${A.Popconfirm} title="吊销该密钥？" onConfirm=${() => revoke(r.id)} okText="吊销" cancelText="取消"><${A.Button} size="small" danger>吊销</${A.Button}></${A.Popconfirm}>` : null) },
              ]} dataSource=${keys.data || []} />
          </${A.Card}>
          <${A.Modal} title="新建访问密钥" open=${minting} onCancel=${() => setMinting(false)} onOk=${() => form.submit()} okText="生成" cancelText="取消" destroyOnClose>
            <${A.Form} form=${form} layout="vertical" onFinish=${onMint} requiredMark=${false} preserve=${false} initialValues=${{ expires_in_days: 0 }}>
              <${A.Form.Item} name="label" label="标签" rules=${[{ required: true, message: "请给这台机器起个标签，便于日后辨认 / 吊销" }]}>
                <${A.Input} placeholder="例如：我的台式机" maxLength=${60} autoFocus />
              </${A.Form.Item}>
              <${A.Form.Item} name="expires_in_days" label="有效期" tooltip="到期后该密钥自动失效；选「永久」则永不过期。">
                <${A.Select} options=${[
                  { value: 0, label: "永久" },
                  { value: 7, label: "7 天" },
                  { value: 30, label: "30 天" },
                  { value: 90, label: "90 天" },
                  { value: 365, label: "365 天" },
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
    const [state, setState] = useState({ phase: "loading", me: null }); // loading | login | ready

    const checkAuth = useCallback(() => {
      if (!getToken()) { setState({ phase: "login", me: null }); return; }
      api("/api/auth/me")
        .then((me) => setState({ phase: "ready", me }))
        .catch((e) => {
          if (e.status === 503) setState({ phase: "personal", me: null });
          else { setToken(""); setState({ phase: "login", me: null }); }
        });
    }, []);
    useEffect(() => { checkAuth(); }, [checkAuth]);
    useEffect(() => {
      const onUnauth = () => { setToken(""); setState({ phase: "login", me: null }); };
      window.addEventListener("foreman:unauthorized", onUnauth);
      return () => window.removeEventListener("foreman:unauthorized", onUnauth);
    }, []);

    const toggleTheme = () => setDark((d) => { localStorage.setItem("foreman_theme", !d ? "dark" : "light"); return !d; });
    const logout = async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* ignore */ } setToken(""); setState({ phase: "login", me: null }); };

    let body;
    if (state.phase === "loading") body = html`<div style=${{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}><${A.Spin} size="large" /></div>`;
    else if (state.phase === "personal") body = html`<div style=${{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}><${A.Result} status="info" title="此服务器处于个人模式" subTitle="个人模式没有账户系统。请使用本机仪表盘 (/index.html)。" /></div>`;
    else if (state.phase === "login") body = html`<${LoginView} onAuthed=${checkAuth} />`;
    else if (state.me && state.me.role === "admin") body = html`<${AdminConsole} me=${state.me} onLogout=${logout} dark=${dark} onToggleTheme=${toggleTheme} />`;
    else body = html`<${MemberView} me=${state.me} onLogout=${logout} dark=${dark} onToggleTheme=${toggleTheme} />`;

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
