"""Non-browser checks for the Foreman web console (warm-paper handoff, 2026-06-23).

Confirms the static page ships and wires the API + WS + new features (browser/E2E acceptance is
done separately with codex per the goal). The control dashboard is embedded inside the login-gated
team console at app.html. The invariant that survives every redesign: untrusted agent output is
rendered through React, never assigned to innerHTML.
"""

from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.shared.config import WorkspaceCfg, load_config


def test_index_served():
    c = TestClient(create_app(load_config()))
    r = c.get("/")
    assert r.status_code == 200
    assert "Foreman" in r.text
    assert '<div id="root" data-admin-root="1">' in r.text
    assert "/admin-app.js" in r.text and "/app.js" in r.text
    assert "/index-redirect.js" not in r.text


def test_index_ships_slim_vendor_and_self_hosted_fonts():
    """The single console app loads React/htm/Ant Design plus the embedded control dashboard CSS.
    Fonts are self-hosted (CSP default-src 'self' blocks Google Fonts, also unreliable in China)."""
    c = TestClient(create_app(load_config()))
    html = c.get("/").text
    assert "/vendor/react.production.min.js" in html
    assert "/vendor/react-dom.production.min.js" in html
    assert "/vendor/htm.umd.js" in html
    assert "/vendor/antd.min.js" in html
    assert "/app.css" in html and "/app.js" in html and "/admin-app.js" in html
    # self-hosted variable fonts, preloaded
    assert "plus-jakarta-sans-latin.woff2" in html
    assert "jetbrains-mono-latin.woff2" in html
    # the woff2 files actually ship and are valid WOFF2 (magic 'wOF2')
    for font in ("plus-jakarta-sans-latin.woff2", "jetbrains-mono-latin.woff2"):
        r = c.get(f"/vendor/fonts/{font}")
        assert r.status_code == 200, font
        assert r.content[:4] == b"wOF2", font


def test_legacy_index_redirects_to_console_control_view():
    c = TestClient(create_app(load_config()))
    html = c.get("/index.html").text
    assert "/index-redirect.js" in html
    assert "/app.js" not in html and "ReactDOM.createRoot" not in html
    js = c.get("/index-redirect.js").text
    assert 'params.set("control", "1")' in js
    assert 'location.replace("/app.html"' in js


def test_app_js_wires_api_and_ws_and_is_xss_safe():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "/api/overview" in js and "/api/sessions" in js
    assert "/ws?session_id=" in js
    assert "ReactDOM.createRoot" in js and "htm.bind" in js
    # React renders agent/PM output; never assign event payloads to raw HTML (XSS).
    assert ".innerHTML" not in js
    assert "dangerouslySetInnerHTML" not in js
    # htm's `<>...</>` shorthand does NOT map to React.Fragment (it makes an invalid empty-tag
    # element that crashes ReactDOM) — never use it; use an array or React.Fragment (codex review).
    assert "html`<>" not in js


def test_i18n_and_language_sync_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "I18N" in js and "/api/settings/language" in js
    # bilingual zh/en is the default — both dictionaries present
    assert "navWorkspace" in js and "navSettings" in js


def test_friendly_error_maps_backend_codes_and_network_errors():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function friendlyError")
    end = js.index("function jsonObjectError", start)
    helper = js[start:end]
    script = helper + r'''
const d = {
  emptyGoal: "empty", dispatchNoWorkspace: "workspace", workspaceMissing: "missing",
  noEnabledAgent: "agent", noDispatcher: "dispatcher", briefNoLlm: "llm",
  badScopeJson: "scope", cloudNotConfigured: "cloud", cloudUnavailable: "unavailable",
  sessionBusy: "busy", noContext: "no context", noStore: "no store",
  sessionNotFound: "session missing", requestDeclined: "declined", networkError: "network",
  machineOffline: "offline", relayUnavailable: "relay", remoteDisabled: "remote disabled",
  remoteProcessRequired: "process", remoteRateLimited: "limited",
  cloudAuthFailed: "cloud auth", cloudTimeout: "cloud timeout", cloudUnreachable: "cloud unreachable",
};
for (const [code, expected] of Object.entries({
  no_context: "no context",
  no_store: "no store",
  session_not_found: "session missing",
  decline: "declined",
  machine_offline: "offline",
  relay_unavailable: "relay",
  disabled: "remote disabled",
  process_required: "process",
  rate_limited: "limited",
  auth: "cloud auth",
  timeout: "cloud timeout",
  unreachable: "cloud unreachable",
})) {
  const actual = friendlyError(new Error(code), d);
  if (actual !== expected || actual === code) {
    console.error({ code, actual, expected });
    process.exit(1);
  }
}
if (friendlyError(new TypeError("Failed to fetch"), d) !== "network") {
  process.exit(2);
}
'''
    subprocess.run(["node", "-e", script], check=True)


def test_five_nav_views_present():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    for view in ("workspace", "decisions", "briefings", "rules", "settings"):
        assert view in js, view
    assert "function Workspace" in js and "function Settings" in js


def test_autonomy_dial_wired_in_page():
    """The PWA exposes the autonomy dial (0/1/2/3) and syncs it to the backend (§6.4)."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "/api/settings/autonomy" in js and "/api/remote/api" in js and "saveAutonomy" in js
    assert "/api/remote/settings/autonomy" not in js
    assert "slider-wrap" in js and "autoExec" in js
    assert "自动执行权限" in js
    assert 'const autonomyName = d[`auto${autonomy}`]' in js
    assert 'title=${`${d.autonomy}: ${autonomyName}`}' in js
    assert 'className="name">${autonomyName}' in js
    assert ".slider-wrap" in css and ".slider-knob" in css
    assert ".autonomy-pill .name" in css


def test_workspace_chat_thread_and_right_panel_wired():
    """The redesigned workspace is a chat thread + composer + right panel (to-dos/subagents/
    terminal), all derived from the live event stream."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "function digest" in js  # events -> thread/todos/subagents/terminal
    assert "function ThreadNode" in js
    assert "function TodoPanel" in js and "function SubPanel" in js and "function TermPanel" in js
    assert "rightTab" in js and "tabTodos" in js and "tabSubagents" in js and "tabTerminal" in js
    assert ".thread" in css and ".ws-right" in css and ".composer-box" in css


def test_composer_dispatch_with_effort_and_context_meter():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "function Composer" in js
    assert "/api/tasks" in js and "body.session_id = sessionRow.id" in js
    assert 'teamMode ? "/api/dispatch" : "/api/tasks"' not in js
    assert "source: clientSource()" in js
    # Fast/Std/Deep maps to effort low|medium|high in the dispatch body
    assert "effort" in js and 'setEffort("low")' in js and 'setEffort("high")' in js
    # context meter + compact action
    assert "ctx-meter" in js and "/compact" in js and "runCompact" in js
    assert "contextLimitFor" in js and "context_length" in js and "max_tokens" in js
    assert "contextLength - outputReserve" in js
    assert ".ctx-meter" in css and ".seg" in css
    assert 'e.key === "@"' in js and "addAttach(); return;" in js
    assert 'attachments.map((a) => `@${a.name}`).join(" ")' in js
    assert "continue_mode" in js and 'runDispatch("interrupt")' in js and 'runDispatch("queue")' in js
    assert "guideHelp" in js and "queueHelp" in js and "busy-chip" in css


def test_pm_brain_timeout_setting_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "request_timeout_s" in js
    assert "Planning timeout (s)" in js and "规划超时（秒）" in js
    assert "30–3600 seconds" in js and "30–3600 秒" in js
    start = js.index("async function saveLlm")
    end = js.index("async function clearLlmKey", start)
    assert "request_timeout_s: Number(llm.request_timeout_s) || 300" in js[start:end]


def test_remote_control_ui_wires_process_target_and_approve_endpoint():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "/api/processes" in js and "loadProcesses" in js
    assert "selectedProcessId" in js and "process_id" in js
    assert "/api/snapshot" in js and "snapshot_req" not in js  # browser calls REST, server builds frame
    assert "/api/remote/api" in js and "SERVER_API_PREFIXES" in js
    assert "card_choice" not in js
    assert "machine_offline" in js and "relay_unavailable" in js
    assert "machine-select" in js and ".m-machine" in css


def test_member_console_has_control_entry_into_dashboard():
    """A team member's 我的机器 card must offer a 「控制」 entry into the control dashboard, and the
    dashboard must accept the console session token without leaving the single /app.html React root."""
    c = TestClient(create_app(load_config()))
    admin_js = c.get("/admin-app.js").text
    app_js = c.get("/app.js").text
    # MemberView/Admin processes: per-machine 控制 button → seed the dashboard target and switch view.
    assert "控制" in admin_js
    assert "function ControlView" in admin_js and "ForemanControlApp" in admin_js
    assert "wantsControlView" in admin_js and "controlHref" in admin_js
    assert "localStorage.setItem(PROCESS_KEY, id)" in admin_js
    assert 'const DASHBOARD_TOKEN_KEY = "foreman.token"' in admin_js
    assert "localStorage.setItem(DASHBOARD_TOKEN_KEY, t)" in admin_js
    assert "localStorage.setItem(DASHBOARD_TOKEN_KEY, token)" in admin_js
    assert 'history.pushState(null, "", controlHref(id))' in admin_js
    assert 'location.href = "/index.html"' not in admin_js
    # The control handoff refreshes the dashboard's canonical token before navigation; the dashboard
    # still accepts the old console key as a fallback for already-open tabs.
    token_start = app_js.index("const getToken =")
    token_end = app_js.index("const setToken", token_start)
    assert "localStorage.getItem(TOKEN_KEY) || localStorage.getItem(CONSOLE_TOKEN_KEY)" in app_js[token_start:token_end]
    assert "window.ForemanControlApp = { Root: Shell }" in app_js and "dataset.adminRoot" in app_js
    assert "/app.html?next=" in app_js and "/api/auth/me" in app_js
    assert '!path.startsWith("/api/auth/")' in app_js
    assert "Access token required" not in app_js and "window.prompt" not in app_js
    assert "nextUrl()" in admin_js and "finishAuth(onAuthed)" in admin_js


def test_local_desktop_console_does_not_require_team_login():
    """The packaged desktop app serves the same app.html shell, but local servers have no account
    manager. A 503 from /api/auth/me must enter the local dashboard, not the team login form."""
    c = TestClient(create_app(load_config()))
    admin_js = c.get("/admin-app.js").text
    app_js = c.get("/app.js").text

    check_start = admin_js.index("const checkAuth =")
    check_end = admin_js.index("useEffect(() => { checkAuth();", check_start)
    check = admin_js[check_start:check_end]
    assert 'api("/api/auth/me")' in check
    assert 'if (!getToken()) { setState({ phase: "login"' not in check
    assert 'setState({ phase: "local", me: null })' in check
    assert 'state.phase === "local"' in admin_js and "<${ControlView} />" in admin_js

    boot_start = app_js.rindex("useEffect(() => {", 0, app_js.index("if (bootStartedRef.current)"))
    boot_end = app_js.index("}, [", boot_start)
    boot = app_js[boot_start:boot_end]
    assert 'if (!getToken()) { redirectToLogin(); return; }' not in boot
    assert "e && e.status === 503" in boot
    assert "setTeamMode(false)" in boot
    assert "loadWorkspaces()" in boot and "loadSessions()" in boot
    assert "loadCards()" in boot and "loadApprovals()" in boot
    assert "loadLlm()" in boot and "loadAutonomy()" in boot


def test_local_dashboard_has_remote_execution_toggle():
    """The machine owner can grant/revoke remote execution from the local 云端连接 card; the toggle
    POSTs the breaker flag and reads it back from /api/settings/cloud."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "saveRemoteExec" in js
    assert "remote_execution_enabled" in js
    assert "d.remoteExec" in js and "remoteExec:" in js


def test_mobile_push_click_paths_are_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    enable = js[js.index("async function enablePush") : js.index("function addAttach")]
    assert "Notification.requestPermission()" in enable
    assert enable.index("Notification.requestPermission()") < enable.index('api("/api/push/vapid-public-key")')
    assert 'navigator.serviceWorker.addEventListener("message", onMessage)' in js
    assert 'msg.type === "notificationclick"' in js
    assert "handleNotificationTarget" in js
    assert 'params.get("view")' in js and 'params.get("process")' in js
    assert "const rows = await loadApprovals()" in js


def test_dispatch_model_picker_and_no_explicit_agent():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert 'api("/api/models")' in js
    assert "body.model = model.trim()" in js
    # per-dispatch model override is wired (datalist from /api/models) — not a dead path
    assert 'list="composer-models"' in js and "setModel(e.target.value)" in js
    # agent is auto-picked by the PM — the composer never forces an agent choice
    assert "agentAuto" in js
    assert "body.agent = agent" not in js


def test_pm_review_rendered_in_thread():
    """pm_review stays an internal diagnostic, while pm_reply is the user-visible PM bubble."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert 't === "pm_review"' in js and "follow_up" in js
    assert "todo_status" in js and "mergeTodoRows" in js
    assert 't === "pm_reply"' in js
    assert 'nodes.push({ kind: "pm-review"' in js
    assert 'className=${`pm-review${n.done ? " done" : ""}`}' in js


def test_pm_stream_replaces_starting_status():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "const hidePmStatus" in js
    assert 'if (p.phase) hidePmStatus(p.phase);' in js
    assert "formatPartialPmJsonObject" in js


def test_pm_partial_json_stream_text_is_readable():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function formatPmJsonObject")
    end = js.index("function terminalText", start)
    helpers = js[start:end]
    script = helpers + r'''
const partial = cleanPmStreamText(`{"type":"final_plan","summary":"PM is checking","todo":["inspect`);
if (!partial.includes("PM is checking") || !partial.includes("1. inspect")) {
  console.error(partial);
  process.exit(1);
}
const finalText = cleanPmStreamText(JSON.stringify({
  summary: "ready",
  deliberation: ["evidence note"],
  todo: ["verify"],
}));
if (!finalText.includes("ready") || !finalText.includes("- evidence note") || !finalText.includes("1. verify")) {
  console.error(finalText);
  process.exit(2);
}
const noVisibleFields = cleanPmStreamText(`{"type":"final_plan","agent":"codex"`);
if (noVisibleFields !== "") {
  console.error(noVisibleFields);
  process.exit(3);
}
'''
    subprocess.run(["node", "-e", script], check=True)


def test_process_steps_parsed_from_codex_and_claude_streams():
    """执行过程 renders a structured timeline, so the digest must turn each CLI's real stream events
    into typed process steps: codex `exec --json` item.* lines (command_execution / file_change /
    web_search) and Claude `stream-json` tool_use / tool_result blocks. Reply text must stay clean
    (the final answer only — never tool markers or reasoning)."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function shellQuote")
    end = js.index("function formatPmJsonObject", start)
    helpers = js[start:end]
    script = helpers + r'''
const must = (cond, label) => { if (!cond) { console.error("FAIL: " + label); process.exit(1); } };

// Codex command_execution (item.completed) -> a cmd step with exit code + key.
const cx = stepsFromAgentPayload({ type: "item.completed", item: { id: "i1", type: "command_execution", command: "npm install -g @github/copilot", aggregated_output: "added 1 package", exit_code: 0, status: "completed" } });
must(cx.length === 1 && cx[0].kind === "cmd", "codex cmd kind");
must(cx[0].exit === 0 && cx[0].status === "done" && cx[0].key === "cx-i1", "codex cmd fields");
must(cx[0].title === "npm install -g @github/copilot", "codex cmd title");

// Codex file_change -> edit step per changed path, carrying the change kind.
const fc = stepsFromAgentPayload({ type: "item.completed", item: { id: "i2", type: "file_change", status: "completed", changes: [{ path: "config.json", kind: "update" }, { path: "new.txt", kind: "add" }] } });
must(fc.length === 2 && fc[0].kind === "edit" && fc[0].fileKind === "update", "codex file_change update");
must(fc[1].fileKind === "add" && fc[1].title === "new.txt", "codex file_change add");

// Codex web_search -> web step with the query.
const ws = stepsFromAgentPayload({ type: "item.completed", item: { id: "i3", type: "web_search", query: "copilot cli install" } });
must(ws.length === 1 && ws[0].kind === "web" && ws[0].title === "copilot cli install", "codex web_search");

// Claude Bash tool_use -> cmd step keyed by tool id; WebSearch -> web step.
const cb = stepsFromAgentPayload({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Bash", input: { command: "ls -la" } }] } });
must(cb.length === 1 && cb[0].kind === "cmd" && cb[0].title === "ls -la" && cb[0].key === "cc-t1", "claude bash");
const cw = stepsFromAgentPayload({ type: "assistant", message: { content: [{ type: "tool_use", id: "t2", name: "WebSearch", input: { query: "anthropic" } }] } });
must(cw.length === 1 && cw[0].kind === "web" && cw[0].title === "anthropic", "claude websearch");
const ce = stepsFromAgentPayload({ type: "assistant", message: { content: [{ type: "tool_use", id: "t3", name: "Write", input: { file_path: "a.py" } }] } });
must(ce[0].kind === "edit" && ce[0].fileKind === "add", "claude write is edit/add");
// Claude PowerShell (the real Windows command tool) maps to a cmd step with the command as title.
const cps = stepsFromAgentPayload({ type: "assistant", message: { content: [{ type: "tool_use", id: "t9", name: "PowerShell", input: { command: "Get-Location", description: "cwd" } }] } });
must(cps.length === 1 && cps[0].kind === "cmd" && cps[0].title === "Get-Location", "claude PowerShell is cmd");

// Codex policy-declined command (status:"declined", exit_code:-1) must read as failed, not done.
const cd = stepsFromAgentPayload({ type: "item.completed", item: { id: "i9", type: "command_execution", command: "rm -rf /", exit_code: -1, status: "declined" } });
must(cd.length === 1 && cd[0].status === "failed" && cd[0].exit === -1, "codex declined -> failed");
// file_change steps carry a per-path key so a re-emitted item.id merges instead of duplicating.
must(fc[0].key === "cx-i2-config.json" && fc[1].key === "cx-i2-new.txt", "codex file_change per-path key");

// Claude tool_result -> an update marker matched to its tool_use by id.
const cr = stepsFromAgentPayload({ type: "user", message: { content: [{ type: "tool_result", tool_use_id: "t1", is_error: false, content: "ok" }] } });
must(cr.length === 1 && cr[0].update === true && cr[0].key === "cc-t1" && cr[0].status === "done", "claude tool_result update");

// replyText is the clean final answer only.
must(replyText({ type: "item.completed", item: { type: "agent_message", text: "all set." } }) === "all set.", "codex agent_message reply");
must(replyText({ type: "item.completed", item: { type: "command_execution", command: "ls" } }) === "", "command is not reply text");
// A claude message that ALSO calls a tool is pre-tool narration ("I'll run it.") — excluded from reply.
must(replyText({ type: "assistant", message: { content: [{ type: "text", text: "I'll run it." }, { type: "tool_use", id: "x", name: "Bash", input: { command: "ls" } }] } }) === "", "claude pre-tool narration excluded from reply");
// A no-tool claude message is the genuine final answer.
must(replyText({ type: "assistant", message: { content: [{ type: "text", text: "done" }] } }) === "done", "claude final text is reply");
'''
    subprocess.run(["node", "-e", script], check=True)


def test_first_substantive_line_skips_common_opening_meta_text():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function isOpeningMetaLine")
    end = js.index("// ---- markdown", start)
    helpers = js[start:end]
    script = helpers + r'''
const cases = [
  ["Let me inspect that first.\nActual fix summary", "Actual fix summary"],
  ["Sure, I can help.\nUse the retry button after failure", "Use the retry button after failure"],
  ["好的，我来检查。\n真正的摘要内容", "真正的摘要内容"],
  ["我们需要先看日志。\n保留第二行", "保留第二行"],
];
for (const [input, expected] of cases) {
  const actual = firstSubstantiveLine(input);
  if (actual !== expected) {
    console.error({ input, expected, actual });
    process.exit(1);
  }
}
'''
    subprocess.run(["node", "-e", script], check=True)


def test_zh_pm_stream_has_local_english_status_fallback():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function cleanPmStreamText")
    end = js.index("function terminalText", start)
    helpers = js[start:end]
    script = helpers + r'''
const d = { pmThinking: "PM 正在思考..." };
if (displayPmStreamText("Thinking through the plan now", "zh", d) !== d.pmThinking) {
  process.exit(1);
}
if (displayPmStreamText("PM 正在规划", "zh", d) !== "PM 正在规划") {
  process.exit(2);
}
if (displayPmStreamText("Thinking through the plan now", "en", d) !== "Thinking through the plan now") {
  process.exit(3);
}
const codeish = "read src/foreman/server/web/app.js";
if (displayPmStreamText(codeish, "zh", d) !== codeish) {
  process.exit(4);
}
'''
    subprocess.run(["node", "-e", script], check=True)


def test_session_controls_and_custom_delete_confirm_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "/api/sessions/${encodeURIComponent(id)}/cancel" in js
    assert 'api(`/api/sessions/${encodeURIComponent(id)}`' in js
    assert "session_busy" in js and "!live" in js and "waiting_approval" in js
    assert "async function retrySession(row)" in js
    assert "onRetrySession(sessionRow)" in js and "${d.retry}" in js
    assert "const body = { goal: row.goal, workspace: target, source: clientSource(), effort }" in js
    assert "window.confirm" not in js
    assert "confirmSessionDelete" in js and "confirmDefnDelete" in js


def test_session_title_rename_and_column_scrollbars_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text

    assert "function SessionTitleModal" in js
    assert "onDoubleClick" in js and "openRenameSession" in js
    assert 'api(`/api/sessions/${encodeURIComponent(row.id)}`' in js
    assert "/api/remote/sessions" not in js
    assert "method: \"PATCH\"" in js
    assert "editSessionTitle" in js and "sessionTitleUpdated" in js

    assert ".app.desktop" in css and "overflow: hidden" in css
    assert ".sb-sessions" in css and "overflow-y: auto" in css
    assert ".thread" in css and "scrollbar-gutter: stable" in css
    assert ".rp-body" in css and "overflow-y: auto" in css
    assert ".editable-title" in css


def test_mobile_failed_session_retry_controls_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "m-session-controls" in js
    assert "mainProps.onRetrySession(sessionRow)" in js
    assert "mainProps.onCancelSession(sessionRow.id)" in js
    assert "mainProps.onDeleteSession(sessionRow.id)" in js
    assert "const failed = status.includes(\"fail\") || status.includes(\"error\")" in js
    assert ".m-session-controls" in css


def test_sidebar_session_status_uses_i18n_label():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "function sessionStatusLabel" in js
    assert "sessionStatusLabel(s.status, d)" in js
    assert "s.status || \"-\", formatTime" not in js


def test_pm_settings_exposes_transport_picker():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "llm.transport" in js and 'value="ws"' in js and "WS stream" in js
    assert "llm.reasoning_effort" in js and "reasoningEffort" in js and 'value="max"' in js


def test_pm_tool_settings_frontend_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "/api/settings/pm-tools" in js
    assert "pmTools" in js and "savePmTools" in js and "loadPmTools" in js
    assert "allowed_commands" in js and "allowed_origins" in js
    assert "web_search_provider" in js and "browser_headless" in js
    assert "PM_TOOLS_MIN_ROUNDS = 1" in js and "PM_TOOLS_MAX_ROUNDS = 999" in js
    assert "clampPmToolRounds" in js and "max=${PM_TOOLS_MAX_ROUNDS}" in js
    assert "PM evidence rounds" in js and "PM 取证工具轮次" in js


def test_mobile_shell_drawer_and_bottom_tabs_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "function MobileShell" in js and "setDrawerOpen" in js
    assert "mTabChat" in js and "mTabTerm" in js
    assert ".m-drawer" in css and ".m-bottom" in css and ".appbar" in css


def test_readonly_output_panel_is_not_terminal_chrome():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert 'tabTerminal: "原始输出"' in js
    assert 'readOnlyLog: "只读日志"' in js
    assert 'tabTerminal: "终端"' not in js
    assert ".term-dotr" not in css


def test_workspace_settings_frontend_wired(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.workspaces = [WorkspaceCfg(path="D:/proj", name="Project")]
    c = TestClient(create_app(cfg))
    assert c.get("/api/workspaces").json() == [{"path": "D:/proj", "name": "Project"}]
    js = c.get("/app.js").text
    assert "/api/workspaces" in js and "loadWorkspaces" in js
    assert "saveWorkspace" in js and "deleteWorkspace" in js


def test_llm_key_input_frontend_wired(tmp_path):
    c = TestClient(create_app(load_config(tmp_path / "none.yaml")))
    js = c.get("/app.js").text
    assert 'type="password"' in js
    assert "api_key" in js and "clearLlmKey" in js and "saveLlm" in js


def test_agent_settings_frontend_wired(tmp_path):
    c = TestClient(create_app(load_config(tmp_path / "none.yaml")))
    js = c.get("/app.js").text
    assert "/api/settings/agents" in js
    assert "agentSettings" in js and "saveAgentSettings" in js
    assert "agentNotFound" in js and "full_access" in js
    assert "Copilot CLI" in js and "BYOK" in js and "--add-dir <workspace>" in js
    assert "更改这些环境变量后，请重启 Foreman 生效" in js
    assert "function Switch" in js  # custom toggle (no antd)


def test_decision_card_and_detail_wired(tmp_path):
    """The PWA fetches cards + drills into step detail, and renders the diff safely (§6.3)."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "/api/cards" in js and "/api/actions/" in js and "/detail" in js
    assert "onCard" in js and "/choose" in js  # one-tap card decision wired
    assert "/api/approvals" in js and "decideApproval" in js
    # diff is untrusted -> rendered through React, never assigned to innerHTML.
    assert "diff-file" in js and "diff-line" in js and ".innerHTML" not in js


def test_markdown_rendering_wired_safely(tmp_path):
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "function MD" in js and "renderBlocks" in js and "renderInline" in js
    assert ".innerHTML" not in js
    assert "dangerouslySetInnerHTML" not in js
    assert ".markdown-body" in css and ".markdown-table-wrap" in css


def test_cloud_connection_frontend_wired(tmp_path):
    """New feature (handoff design): the Settings → cloud-connection card links the machine to the
    relay 总机 (DESIGN §8.5)."""
    c = TestClient(create_app(load_config(tmp_path / "none.yaml")))
    js = c.get("/app.js").text
    assert "/api/settings/cloud" in js
    assert "connectCloud" in js and "disconnectCloud" in js and "saveCloud" in js
    assert "clearCloudKey" in js  # the saved access key can be cleared from the UI (codex review)
    assert "cloudConn" in js and "云端连接" in js
    assert "access_key" in js
    # the field asks for the wss relay endpoint the connector actually needs (codex review)
    assert "wss://foreman.yourteam.dev/relay" in js


def test_boot_does_not_block_on_model_discovery():
    """Launch must not hang on a slow provider /models: model/agent discovery runs outside the
    boot barrier (codex review finding)."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    # the boot barrier ends with the essential loaders — no loadModels()/loadLlm() inside it
    assert 'api("/api/auth/me").then(async () =>' in js
    assert "const processId = await loadProcesses();" in js
    assert "if (processId) await loadRemoteSnapshot(processId);" in js
    boot = js.split('api("/api/auth/me").then(async () =>', 1)[1].split("}).catch", 1)[0]
    assert "loadModels()" not in boot
    assert "loadLlm()" not in boot


def test_team_snapshot_drives_dashboard_state_and_decision_count():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("const applySnapshot =")
    end = js.index("const loadRemoteSnapshot", start)
    helper = js[start:end]
    assert "setApprovals(snap && snap.approvals || [])" in helper
    assert "setReports(snap && snap.reports || [])" in helper
    assert "setDefinitions(snap && snap.definitions || [])" in helper
    assert "setWorkspaces(snap.workspaces)" in helper
    assert "setAutonomyState(snap.autonomy.level)" in helper
    assert "setDebugSettings({ llm_trace: !!snap.debug.llm_trace })" in helper
    assert "setCloud({ url: c.url || \"\", access_key: \"\"" in helper
    assert "Array.isArray(snap.events)" in helper
    counts = js[js.index("const counts =") : js.index("const composerProps", js.index("const counts ="))]
    assert "decisions: openCards.length + approvals.length" in counts
    assert "notifications.length" not in counts


def test_team_api_wrapper_proxies_local_api_but_not_console_api():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "function shouldRouteLocal" in js
    assert 'requestJson("/api/remote/api"' in js
    assert 'path,' in js and 'body: opts.body' in js
    assert '"/api/auth"' in js and '"/api/processes"' in js and '"/api/notifications"' in js
    assert '"/api/push"' in js


def test_team_session_open_requests_remote_timeline_snapshot():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "const selectedSessionRef = useRef(\"\")" in js
    assert "if (sessionId) body.session_id = sessionId;" in js
    assert "loadRemoteSnapshot(processId, sessionId)" in js
    assert "openTimeline(sessionId, processId)" in js
    assert "selectedSessionRef.current === sid" in js


def test_mobile_drawer_has_session_picker():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    # MobileShell is handed the session list + selector so a phone can open an existing session
    assert "sessions=${sessions} selected=${selectedSession} onSelect=${openTimeline}" in js
    # ...and a new-session action so later mobile tasks aren't all forced into a follow-up (codex)
    assert "onNew=${newSession}" in js


def test_launch_splash_present(tmp_path):
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "function Launch" in js and "booted" in js
    assert ".launch" in css and ".boot" in css


# ── team-mode console ─────────────────────────────────

def test_admin_console_app_ships_and_wires(tmp_path):
    """The login-gated console ships at /app.html and /admin.html (alias)."""
    c = TestClient(create_app(load_config()))
    for path in ("/app.html", "/admin.html"):
        page = c.get(path)
        assert page.status_code == 200, path
        assert "admin-app.js" in page.text and "/vendor/antd.min.js" in page.text
        assert "/app.css" in page.text and "/app.js" in page.text
        assert 'data-admin-root="1"' in page.text

    app_js = c.get("/admin-app.js").text
    assert "/api/auth/login" in app_js and "/api/auth/me" in app_js
    assert "/api/admin/overview" in app_js and "/api/admin/accounts" in app_js
    assert "/api/admin/sessions" in app_js and "/api/admin/db" in app_js
    assert "/api/admin/logs" in app_js
    assert "htm.bind" in app_js and ".innerHTML" not in app_js


def test_redeem_page_still_ships(tmp_path):
    c = TestClient(create_app(load_config()))
    assert c.get("/redeem.html").status_code == 200
    redeem_js = c.get("/redeem.js").text
    assert "/api/auth/redeem" in redeem_js and ".innerHTML" not in redeem_js


def test_access_keys_page_ships_and_wires(tmp_path):
    c = TestClient(create_app(load_config()))
    keys_html = c.get("/keys.html")
    assert keys_html.status_code == 200 and "data-i18n" in keys_html.text
    keys_js = c.get("/keys.js").text
    assert "/api/auth/login" in keys_js and "/api/keys" in keys_js
    assert "expires_in_days" in keys_js
    assert "DELETE" in keys_js
    assert "textContent" in keys_js and ".innerHTML" not in keys_js
