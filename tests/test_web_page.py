"""Non-browser checks for the Foreman web console (warm-paper handoff, 2026-06-23).

Confirms the static page ships and wires the API + WS + new features (browser/E2E acceptance is
done separately with codex per the goal). The control dashboard is embedded inside the login-gated
team console at app.html. The invariant that survives every redesign: untrusted agent output is
rendered through React, never assigned to innerHTML.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.shared.config import WorkspaceCfg, load_config

ROOT = Path(__file__).resolve().parents[1]


def _dashboard_bundle(c: TestClient) -> str:
    return "\n".join(
        [
            c.get("/app-core.js").text,
            c.get("/app-context.js").text,
            c.get("/app-timeline.js").text,
            c.get("/app.js").text,
        ]
    )


def test_index_served():
    c = TestClient(create_app(load_config()))
    r = c.get("/")
    assert r.status_code == 200
    assert "Foreman" in r.text
    assert '<div id="root" data-admin-root="1">' in r.text
    assert "/admin-app.js" in r.text and "/app-core.js" in r.text and "/app-context.js" in r.text and "/app-timeline.js" in r.text and "/app.js" in r.text
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
    assert "/app.css" in html and "/app-core.js" in html and "/app-context.js" in html and "/app-timeline.js" in html and "/app.js" in html and "/admin-app.js" in html
    # self-hosted variable fonts, preloaded
    assert "plus-jakarta-sans-latin.woff2" in html
    assert "jetbrains-mono-latin.woff2" in html
    # the woff2 files actually ship and are valid WOFF2 (magic 'wOF2')
    for font in ("plus-jakarta-sans-latin.woff2", "jetbrains-mono-latin.woff2"):
        r = c.get(f"/vendor/fonts/{font}")
        assert r.status_code == 200, font
        assert r.content[:4] == b"wOF2", font


def test_app_split_scripts_are_served_without_build_step():
    c = TestClient(create_app(load_config()))
    html = c.get("/app.html").text
    assert '<script src="/app-core.js?v=' in html
    assert '<script src="/app-context.js?v=' in html
    assert '<script src="/app-timeline.js?v=' in html
    assert '<script src="/app.js?v=' in html
    assert html.index("/app-core.js") < html.index("/app-context.js") < html.index("/app-timeline.js") < html.index("/app.js")
    for path in ("/app-core.js", "/app-context.js", "/app-timeline.js", "/app.js"):
        res = c.get(path)
        assert res.status_code == 200, path
        assert "type=\"module\"" not in html
    lower = html.lower()
    for word in ("vite", "webpack", "babel", "tsx", "jsx"):
        assert word not in lower


def test_app_core_exports_shared_helpers():
    c = TestClient(create_app(load_config()))
    js = c.get("/app-core.js").text
    assert "window.ForemanApp" in js
    for name in (
        "api",
        "friendlyError",
        "tokenK",
        "shortPath",
        "formatTime",
        "getToken",
        "setToken",
        "redirectToLogin",
        "SERVER_API_PREFIXES",
        "PROCESS_KEY",
    ):
        assert name in js


def test_app_js_consumes_foreman_app_helpers():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "window.ForemanApp" in js
    for name in (
        "api",
        "friendlyError",
        "tokenK",
        "shortPath",
        "formatTime",
        "getToken",
        "setToken",
        "redirectToLogin",
        "PROCESS_KEY",
    ):
        assert name in js


def test_no_duplicate_api_helper_in_app_js_if_removed():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "async function api(" not in js
    assert "function friendlyError(" not in js
    assert "function tokenK(" not in js
    assert "function shortPath(" not in js
    assert "function formatTime(" not in js
    assert "function getToken(" not in js
    assert "window.fetch = async" not in js


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
    bundle = _dashboard_bundle(c)
    assert "/api/overview" in js and "/api/sessions" in js
    assert "/ws?session_id=" in js
    assert "ReactDOM.createRoot" in js and "htm.bind" in bundle
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


def test_ui_language_defaults_to_browser_language_when_unset():
    c = TestClient(create_app(load_config()))
    app_js = c.get("/app.js").text
    admin_js = c.get("/admin-app.js").text
    keys_js = c.get("/keys.js").text
    redeem_js = c.get("/redeem.js").text
    html = c.get("/app.html").text

    for js in (app_js, admin_js, keys_js, redeem_js):
        assert "function detectedUiLang()" in js
        assert "navigator.languages" in js
        assert 'localStorage.getItem(LANG_KEY)' in js
        assert "normalizeUiLang(langs[0])" in js

    assert "setLangState(detectedUiLang())" in app_js
    assert 'api("/api/settings/language").then((data) => setLangState' not in app_js
    assert "Loading Foreman Console" in html
    assert "langs.some" not in app_js + admin_js + keys_js + redeem_js + html


def test_friendly_error_maps_backend_codes_and_network_errors():
    c = TestClient(create_app(load_config()))
    js = c.get("/app-core.js").text
    start = js.index("function friendlyError")
    end = js.index("window.ForemanApp", start)
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


def test_nav_views_present():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    for view in ("workspace", "decisions", "briefings", "rules", "settings", "version"):
        assert view in js, view
    assert "function Workspace" in js and "function Settings" in js


def test_version_information_page_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    health = c.get("/health").json()

    assert health["version"]
    assert "navVersion" in js and "function VersionInfo" in js
    assert "Current runtime version" in js and "当前运行版本" in js
    assert "Check for updates" in js and "检查更新" in js
    assert "Version-page update check button" in js
    assert "版本页增加检查更新按钮" in js
    assert "function UpdateModal" in js and "updateDownloadProgress" in js
    assert 'api("/api/update/status")' in js and 'api("/api/update/cancel"' in js
    assert "VERSION_HISTORY" in js and "Historical update notes" in js
    assert "This release" not in js and "本次更新内容" not in js
    assert "v1.3.6" in js and "v1.3.5" in js and "v1.3.0" in js and "v1.2.9" in js and "v1.2.8" in js and "v1.2.7" in js
    assert "onCheckUpdate: () => checkAppUpdate(true)" in js
    assert 'api("/api/update/check")' in js
    assert '["briefings", "rules", "settings", "version"].includes(viewName)' in js
    assert ".version-number" in css and ".version-path" in css and ".version-history" in css
    assert ".version-actions" in css and ".version-check-status" in css and ".version-meta-grid" in css
    assert ".update-progress" in css and ".update-modal-notes" in css


def test_readme_and_agents_require_version_notes():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    history = (ROOT / "docs" / "VERSION_HISTORY.md").read_text(encoding="utf-8")

    assert "### Version Information" in readme and "### 版本信息" in readme
    assert "v1.3.6" in readme and "v1.3.5" in readme and "v1.3.0" in readme and "v1.2.9" in readme and "v1.2.8" in readme
    assert "Update history:" in readme and "更新历史：" in readme
    assert "This release adds" not in readme and "本次更新" not in readme
    assert "docs/VERSION_HISTORY.md" in readme
    assert "v1.2.6" in readme and "v1.2.5" in readme and "v1.2.4" in readme
    assert "v1.2.1" in readme and "v1.2.0" in readme
    assert "最终领取版本号时同步更新 README" in agents
    assert "README.md" in agents and "Version / 版本" in agents and "docs/VERSION_HISTORY.md" in agents
    assert "## v1.3.6" in history and "## v1.3.5" in history and "## v1.3.0" in history and "## v1.2.9" in history and "## v1.2.8" in history
    assert "## v1.2.6" in history and "## v1.2.5" in history and "## v1.2.4" in history
    assert "## v1.2.3" in history and "## v1.2.2" in history and "## v1.2.1" in history and "## v1.2.0" in history
    assert "历史更新记录" in agents and "不能只显示最新版本" in agents


def test_autonomy_dial_wired_in_page():
    """The PWA exposes the autonomy dial (0/1/2/3) and syncs it to the backend (§6.4)."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    bundle = _dashboard_bundle(c)
    css = c.get("/app.css").text
    assert "/api/settings/autonomy" in js and "/api/remote/api" in bundle and "saveAutonomy" in js
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
    timeline_js = c.get("/app-timeline.js").text
    css = c.get("/app.css").text
    assert "function digest" in js  # events -> thread/todos/subagents/terminal
    assert "function ThreadNode" in timeline_js
    assert "function ThreadNode" not in js
    assert "function TodoPanel" in js and "function SubPanel" in js and "function TermPanel" in js
    assert "rightTab" in js and "tabTodos" in js and "tabSubagents" in js and "tabTerminal" in js
    assert ".thread" in css and ".ws-right" in css and ".composer-box" in css


def test_workspace_thread_scrolls_to_bottom_on_new_messages():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "const threadRef = useRef(null)" in js
    assert "lastThreadNodeId" in js
    assert "el.scrollTop = el.scrollHeight" in js
    assert 'className="thread" ref=${threadRef} onScroll=${onThreadScroll}' in js
    assert 'data-testid="conversation-scroll-container"' in js


def test_workspace_user_and_pm_bubbles_have_copy_buttons():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    timeline_js = c.get("/app-timeline.js").text
    css = c.get("/app.css").text
    assert "function BubbleCopy" in timeline_js
    assert "<${BubbleCopy} text=${n.goal}" in timeline_js
    assert "<${BubbleCopy} text=${n.text}" in timeline_js
    assert "onCopy=${onCopy}" in js and "onCopy=${mainProps.onCopy}" in js
    assert ".bubble-copy" in css and ".bubble-copy.invert" in css


def test_composer_dispatch_with_effort_and_context_meter():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "function Composer" in js
    assert "/api/tasks" in js and "body.session_id = sessionRow.id" in js
    assert 'teamMode ? "/api/dispatch" : "/api/tasks"' not in js
    assert "source: clientSource()" in js
    # Thinking level is a raw-value dropdown in the composer, not Fast/Std/Deep buttons.
    assert "effort" in js and "effort-pick" in js and "<option value=\"low\">low</option>" in js
    assert 'setEffort("low")' not in js and 'setEffort("high")' not in js
    # context meter + compact action
    assert "ctx-meter" in js and "/compact" in js and "runCompact" in js
    assert "contextLimitFor" in js and "context_length" in js
    assert "nextContextStats(sessionRow, events || [])" in js
    assert "context_tokens" in js and "context_compacted" in js
    assert "contextLength - outputReserve" not in js
    assert ".ctx-meter" in css and ".effort-pick" in css
    assert ".context-pack" in css and "function ContextPackPanel" in _dashboard_bundle(c)
    assert 'kind: "context-pack"' in js and "contextPackView(p)" in js
    assert 'e.key === "@"' in js and "addAttach(); return;" in js
    assert 'attachments.map((a) => `@${a.name}`).join(" ")' in js
    assert "continue_mode" in js and 'runDispatch("queue")' in js
    assert 'runDispatch("interrupt")' not in js
    assert "onCancelSession" in js and "busy-chip" in css
    assert "clipboardImageFiles" in js and "addPastedImages" in js and "onPaste" in js


def test_context_panel_renders_usage_meter():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    css = c.get("/app.css").text
    assert 'data-testid="context-tab"' in js
    assert 'data-testid="context-panel"' in js
    assert 'data-testid="context-usage-card"' in js
    assert 'data-testid="context-usage-percent"' in js
    assert 'data-testid="context-usage-used"' in js
    assert 'data-testid="context-usage-window"' in js
    assert 'data-testid="context-soft-remaining"' in js
    assert 'data-testid="context-hard-remaining"' in js
    assert ".context-meter-track" in css


def test_context_tab_selector_exists():
    c = TestClient(create_app(load_config()))
    app_js = c.get("/app.js").text
    start = app_js.index('data-testid="context-tab"')
    button = app_js[app_js.rfind("<button", 0, start) : app_js.index("</button>", start)]
    assert 'data-testid="context-tab"' in button
    assert 'setRightTab("ctx")' in button
    assert "${d.context}" in button


def test_context_panel_renders_lane_usage():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'data-testid="context-lane-usage"' in js
    for lane in range(1, 8):
        assert f"context-lane-${{lane}}" in js or f"context-lane-{lane}" in js
    assert "Lane 7 noise is high" in js


def test_context_panel_renders_runtime_state():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'data-testid="context-runtime-state"' in js
    assert 'data-testid="context-runtime-workspace"' in js
    assert 'data-testid="context-runtime-cwd"' in js
    assert 'data-testid="context-runtime-worktree"' in js
    assert 'data-testid="context-runtime-branch"' in js
    assert 'data-testid="context-runtime-base-ref"' in js
    assert 'data-testid="context-runtime-head-sha"' in js


def test_context_panel_renders_active_agents():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    css = c.get("/app.css").text
    assert 'data-testid="context-agents-card"' in js
    assert 'data-testid="context-agent-row"' in js
    assert 'data-testid="context-agent-status"' in js
    assert 'data-testid="context-agent-cwd"' in js
    assert 'data-testid="context-agent-worktree"' in js
    assert 'data-testid="context-agent-branch"' in js
    assert 'data-testid="context-agent-native-session"' in js
    assert ".agent-status.failed" in css


def test_context_panel_agent_last_meaningful_output_object_is_stringified():
    c = TestClient(create_app(load_config()))
    context_js = c.get("/app-context.js").text
    assert "function contextText" in context_js
    assert "sanitizeContextTextValue" in context_js
    assert "HIDDEN_CONTEXT_KEYS" in context_js
    assert '"std" + "out"' in context_js and '"std" + "err"' in context_js
    assert '"provider" + "_" + "payload"' in context_js
    assert '"encrypted" + "_" + "content"' in context_js
    assert "contextText(a.last_meaningful_output)" in context_js
    assert '${a.last_meaningful_output || ""}' not in context_js


def test_context_panel_sanitizer_redacts_reasoning_and_secret_keys():
    c = TestClient(create_app(load_config()))
    context_js = c.get("/app-context.js").text
    assert "function isHiddenContextKey" in context_js
    assert "HIDDEN_CONTEXT_KEY_PARTS" in context_js
    assert "k.includes(part)" in context_js
    assert '"reason" + "ing"' in context_js
    assert '"sec" + "ret"' in context_js
    assert '"tok" + "en"' in context_js
    assert '"api" + "_" + "key"' in context_js
    assert '"author" + "ization"' in context_js
    assert '"raw" + "_" + "output"' in context_js
    assert '"aggregated" + "_" + "output"' in context_js
    assert '${a.last_meaningful_output || ""}' not in context_js


def test_context_panel_renders_latest_checkpoint():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'data-testid="latest-checkpoint-card"' in js
    assert 'data-testid="latest-checkpoint-id"' in js
    assert 'data-testid="latest-checkpoint-trigger"' in js
    assert 'data-testid="latest-checkpoint-method"' in js
    assert 'data-testid="latest-checkpoint-before-tokens"' in js
    assert 'data-testid="latest-checkpoint-after-tokens"' in js
    assert 'data-testid="latest-checkpoint-items-count"' in js


def test_context_panel_renders_checkpoint_list():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'data-testid="checkpoint-list"' in js
    assert 'data-testid="checkpoint-row"' in js
    assert 'data-testid="checkpoint-row-created"' in js
    assert 'data-testid="checkpoint-row-trigger"' in js
    assert 'data-testid="checkpoint-row-reason"' in js
    assert 'data-testid="checkpoint-row-method"' in js
    assert 'data-testid="checkpoint-row-before"' in js
    assert 'data-testid="checkpoint-row-after"' in js
    assert 'data-testid="checkpoint-row-status"' in js


def test_context_panel_renders_checkpoint_detail():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'data-testid="checkpoint-detail"' in js
    assert 'data-testid="checkpoint-summary"' in js
    assert 'data-testid="checkpoint-runtime"' in js
    assert 'data-testid="checkpoint-token-usage"' in js
    assert 'data-testid="checkpoint-source-cursor"' in js
    assert 'data-testid="checkpoint-warnings"' in js
    assert 'data-testid="active-context-preview"' in js


def test_context_panel_hides_provider_payload_by_default():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert "provider_payload" not in js
    assert "raw replacement_history full JSON" not in js


def test_context_panel_hides_encrypted_content():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert "encrypted_content" not in js


def test_context_panel_hides_hidden_reasoning():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert "hidden_reasoning" not in js
    assert "pm_reasoning raw" not in js


def test_compact_now_button_calls_manual_compact_endpoint():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'data-testid="context-compact-now"' in js
    assert "/context/compact" in js
    assert 'trigger: "manual", reason: "user_requested"' in js


def test_compact_now_button_disabled_while_running():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'disabled=${state === "compacting" || !sessionId}' in js
    assert "Compacting..." in js


def test_compact_now_success_refreshes_context():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'setCompactMsg("Context compacted.")' in js
    assert "await loadContext();" in js


def test_compact_now_failure_shows_error():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert "Context compact failed." in js
    assert "Latest checkpoint was not changed." in js
    assert 'data-testid="context-compact-error"' in js


def test_compact_progress_started_completed_visible():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'data-testid="context-compact-loading"' in js
    assert 'data-testid="timeline-context-compaction"' in js
    assert 'data-testid="timeline-context-compaction-started"' in js
    assert 'data-testid="timeline-context-compaction-completed"' in js


def test_compact_progress_failed_visible():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert 'data-testid="timeline-context-compaction-failed"' in js


def test_new_message_scrolls_conversation_to_bottom():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert 'data-testid="conversation-scroll-container"' in js
    assert "stickToBottomRef" in js
    assert "el.scrollTop = el.scrollHeight" in js
    assert 'data-testid="message-composer"' in js
    assert 'data-testid="send-message"' in js


def test_context_panel_refresh_does_not_jump_conversation_to_top():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert "function ContextPanel" in js
    assert "setState((prev) => prev === \"ready\" || prev === \"degraded\" ? prev : \"loading\")" in js
    assert "threadNodes.length" in js and "loadContext" in js


def test_compact_progress_item_does_not_break_bottom_scroll():
    c = TestClient(create_app(load_config()))
    js = _dashboard_bundle(c)
    assert "stickToBottomRef.current" in js
    assert "timeline-context-compaction" in js


def test_context_meter_and_context_pack_helpers_match_provider_context():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function estTokens")
    end = js.index("function clipboardImageFiles", start)
    helpers = js[start:end]
    script = helpers + r'''
const must = (cond, label, value) => { if (!cond) { console.error(label, value); process.exit(1); } };
const noisyEvents = [{ type: "agent_output", payload: { summary: "x".repeat(4000) } }];
let stats = nextContextStats({ context_tokens: 809, context_compacted: true }, noisyEvents);
must(stats.tokens === 809 && stats.compacted === true, "compacted sessions use backend base without compact marker", stats);
stats = nextContextStats({ context_tokens: 0, context_compacted: false }, noisyEvents);
must(stats.tokens === 1000 && stats.compacted === false, "uncompacted sessions use raw event context", stats);
const payload = {
  summary: JSON.stringify({ session_state: { summary: "done" }, dynamic_tail: [{ text: "tail" }] }),
  after_tokens: 12,
  summary_chars: 90,
  format: "context_pack_v1",
};
const view = contextPackView(payload);
must(view.preview === "done", "context pack preview", view);
must(view.json.includes('\n  "session_state"') && view.json.includes('"dynamic_tail"'), "pretty json", view.json);
const broken = contextPackView({ summary: '{"dynamic_tail":', after_tokens: 5, summary_chars: 16 });
must(broken.json.includes('"parse_error"') && !broken.json.includes('{"dynamic_tail":'), "broken json is not raw rendered", broken.json);
stats = nextContextStats(null, [{ type: "context_compact", payload }]);
must(stats.tokens === 12 && stats.compacted === true, "compact event fallback", stats);
stats = nextContextStats({ context_tokens: 809, context_compacted: true }, [
  { type: "context_compact", payload },
  { type: "agent_output", payload: { summary: "x".repeat(4000) } },
]);
must(stats.tokens === 1809 && stats.compacted === true, "compacted sessions add post-compact tail", stats);
stats = nextContextStats({ context_tokens: 809, context_compacted: true }, [
  { type: "context_compact", payload },
  { type: "pm_reasoning", payload: { delta: "x".repeat(4000) } },
  { type: "pm_output", payload: { delta: "x".repeat(4000) } },
]);
must(stats.tokens === 809 && stats.compacted === true, "pm stream noise does not grow context meter", stats);
stats = nextContextStats(null, [
  { type: "context_compact", payload },
  { type: "agent_output", payload: { summary: "x".repeat(4000) } },
]);
must(stats.tokens === 1012 && stats.compacted === true, "compact event fallback adds post-compact tail", stats);
'''
    subprocess.run(["node"], input=script, text=True, encoding="utf-8", check=True)


def test_pm_brain_timeout_setting_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "request_timeout_s" in js
    assert "context_window_tokens" in js
    assert "max_tokens" not in js
    assert "Planning timeout (s)" in js and "规划超时（秒）" in js
    assert "Context limit tokens" in js and "上下文上限 token" in js
    assert "Max output tokens" not in js and "maxOutputTokens" not in js
    assert "30–3600 seconds" in js and "30–3600 秒" in js
    assert "DEFAULT_CONTEXT_TOKENS = 272000" in js
    start = js.index("async function saveLlm")
    end = js.index("async function clearLlmKey", start)
    assert "request_timeout_s: Number(llm.request_timeout_s) || 300" in js[start:end]
    assert "context_window_tokens: Number(llm.context_window_tokens) || 272000" in js[start:end]
    assert "max_tokens" not in js[start:end]


def test_remote_control_ui_wires_process_target_and_approve_endpoint():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    bundle = _dashboard_bundle(c)
    css = c.get("/app.css").text
    assert "/api/processes" in js and "loadProcesses" in js
    assert "selectedProcessId" in js and "process_id" in js
    assert "/api/snapshot" in js and "snapshot_req" not in js  # browser calls REST, server builds frame
    assert "/api/remote/api" in bundle and "SERVER_API_PREFIXES" in bundle
    assert "card_choice" not in js
    assert "machine_offline" in bundle and "relay_unavailable" in bundle
    assert "machine-select" in js and ".m-machine" in css


def test_remote_control_prefers_online_process_over_stale_offline_selection():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "const currentRow = rows.find((p) => p.id === selectedProcessId) || null;" in js
    assert "const current = currentRow && (currentRow.online || !online.length) ? currentRow.id : \"\";" in js
    assert "const current = selectedProcessId && ids.includes(selectedProcessId)" not in js


def test_member_console_has_control_entry_into_dashboard():
    """A team member's 我的机器 card must offer a 「控制」 entry into the control dashboard, and the
    dashboard must accept the console session token without leaving the single /app.html React root."""
    c = TestClient(create_app(load_config()))
    admin_js = c.get("/admin-app.js").text
    app_js = c.get("/app.js").text
    core_js = c.get("/app-core.js").text
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
    token_start = core_js.index("const getToken =")
    token_end = core_js.index("const setToken", token_start)
    assert "localStorage.getItem(TOKEN_KEY) || localStorage.getItem(CONSOLE_TOKEN_KEY)" in core_js[token_start:token_end]
    assert "window.ForemanControlApp = { Root: Shell }" in app_js and "dataset.adminRoot" in app_js
    assert "/app.html?next=" in core_js and "/api/auth/me" in app_js
    assert '!path.startsWith("/api/auth/")' in core_js
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
    # per-dispatch model override is wired as a select from /api/models — not a dead path
    assert "modelChoices.map" in js and "model-pick" in js and "setModel(e.target.value)" in js
    assert 'list="composer-models"' not in js
    # agent stays auto-picked by the PM; the composer never forces or advertises an agent choice
    assert "执行 agent 由 PM 自动选择" not in js
    assert "agent auto-picked by PM" not in js
    assert "body.agent = agent" not in js


def test_pm_review_rendered_in_thread():
    """pm_review stays an internal diagnostic, while pm_reply is the user-visible PM bubble."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    timeline_js = c.get("/app-timeline.js").text
    assert 't === "pm_review"' in js and "follow_up" in js
    assert "todo_status" in js and "mergeTodoRows" in js
    assert 't === "pm_reply"' in js
    assert 'nodes.push({ kind: "pm-review"' in js
    assert 'className=${`pm-review${n.done ? " done" : ""}`}' in timeline_js


def test_pm_stream_replaces_starting_status():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "const hidePmStatus" in js
    assert 'if (p.phase) hidePmStatus(p.phase);' in js
    assert "formatPartialPmJsonObject" in js
    assert "function formatPmReasoningText" in js
    assert 'kind: t === "pm_reasoning" ? "pm-thinking" : "pm"' in js


def test_pm_stream_preserves_delta_boundary_spaces():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function extractTextParts")
    end = js.index("function terminalText", start)
    helpers = js[start:end]
    script = helpers + r'''
const chunks = [
  { delta: "I" },
  { delta: " need" },
  { delta: " to" },
  { delta: " submit" },
];
let buffer = "";
let rendered = "";
for (const chunk of chunks) {
  const rawTxt = extractStreamText(chunk);
  rendered = cleanPmStreamText(`${buffer}${rawTxt}`);
  buffer = `${buffer}${rawTxt}`;
}
if (buffer !== "I need to submit" || rendered !== "I need to submit") {
  console.error({ buffer, rendered });
  process.exit(1);
}
'''
    subprocess.run(["node", "-e", script], check=True)


def test_pm_thinking_collapsed_title_uses_reasoning_heading():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function clip")
    end = js.index("function looksEnglishPmStatus", start)
    helpers = js[start:end]
    script = helpers + r'''
const boldTitle = pmThinkingTitle("Before body. **Clarifying user request**\n\nI need to inspect the request.", "思考摘要");
if (boldTitle !== "Clarifying user request") {
  console.error({ boldTitle });
  process.exit(1);
}
const parts = pmThinkingParts("Before body. **Clarifying user request**\n\nI need to inspect the request.", "思考摘要");
if (parts.title !== "Clarifying user request" || parts.body.includes("Clarifying user request") || !parts.body.includes("I need to inspect")) {
  console.error({ parts });
  process.exit(4);
}
const fallbackTitle = pmThinkingTitle("Plain generated heading\n\nMore reasoning.", "思考摘要");
if (fallbackTitle !== "Plain generated heading") {
  console.error({ fallbackTitle });
  process.exit(2);
}
const emptyTitle = pmThinkingTitle("", "思考摘要");
if (emptyTitle !== "思考摘要") {
  console.error({ emptyTitle });
  process.exit(3);
}
'''
    subprocess.run(["node"], input=script, text=True, encoding="utf-8", check=True)


def test_pm_tool_activity_helpers_make_public_timeline_labels():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function shellQuote")
    end = js.index("function looksEnglishPmStatus", start)
    helpers = js[start:end]
    script = helpers + r'''
const must = (cond, label, value) => { if (!cond) { console.error(label, value); process.exit(1); } };
const readPre = { tool: "read_file", call_id: "r1", input: { path: "README.md" } };
must(pmToolKind("read_file") === "read", "read kind");
must(pmToolPreTitle(readPre, "zh") === "我先读取 README.md", "read pre", pmToolPreTitle(readPre, "zh"));
const searchPre = { tool: "search_repo", call_id: "s0", input: { query: "PM activity" } };
must(pmToolPreTitle(searchPre, "zh") === "我先检索 PM activity", "search pre", pmToolPreTitle(searchPre, "zh"));
const noted = { tool: "search_repo", call_id: "s1", input: { query: "PM", public_note: "我先查 PM 事件" } };
must(pmToolPreTitle(noted, "zh") === "我先查 PM 事件", "public note wins");
must(!pmToolActivityDetail(noted, { input: noted.input }).includes("public_note"), "detail hides public_note");
const readPost = { tool: "read_file", call_id: "r1", result: { ok: true, data: { text: "a\nb\n" } } };
must(pmToolPostTitle(readPost, null, "zh") === "读取完成，返回 2 行", "read post", pmToolPostTitle(readPost, null, "zh"));
const cmdPost = { tool: "run_command", call_id: "c1", result: { ok: true, data: { returncode: 0, stdout: "ok", log_path: "run.log" } } };
must(pmToolPostTitle(cmdPost, null, "zh") === "命令完成，exit 0", "cmd post");
must(pmToolActivityDetail(cmdPost, null).includes("run.log"), "log detail");
const searchPost = { tool: "search_repo", call_id: "s1", result: { ok: true, data: { matches: [{}, {}, {}] } } };
must(pmToolPostTitle(searchPost, null, "zh") === "检索命中 3 处", "search post");
const failed = { tool: "read_file", call_id: "f1", result: { ok: false, error: "not_file" } };
must(pmToolPostTitle(failed, null, "en") === "read_file failed: not_file", "failed post");
'''
    subprocess.run(["node"], input=script, text=True, encoding="utf-8", check=True)


def test_pm_tool_events_digest_to_public_activity_timeline():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function shellQuote")
    end = js.index("function Empty", start)
    helpers = js[start:end]
    script = helpers + r'''
const must = (cond, label, value) => { if (!cond) { console.error(label, value); process.exit(1); } };
const events = [
  { id: "pre", type: "tool_pre", source: "pm-agent", session_id: "s1", task_id: "t1", ts: "2026-01-01T00:00:00Z",
    payload: { source: "pm-agent", tool: "run_command", call_id: "c1", input: { command: "echo ok" } } },
  { id: "stream", type: "tool_stream", source: "pm-agent", session_id: "s1", task_id: "t1", ts: "2026-01-01T00:00:01Z",
    payload: { source: "pm-agent", tool: "run_command", call_id: "c1", stream: "stdout", delta: "ok\n", log_path: "run.log" } },
  { id: "post", type: "tool_post", source: "pm-agent", session_id: "s1", task_id: "t1", ts: "2026-01-01T00:00:02Z",
    payload: { source: "pm-agent", tool: "run_command", call_id: "c1", ok: true,
      result: { ok: true, data: { returncode: 0, stdout: "ok\n", log_path: "run.log" }, artifact_paths: ["run.log"] } } },
];
const dig = digest(events, {}, "zh");
must(dig.nodes.length === 1, "one public node", dig.nodes);
must(dig.nodes[0].kind === "pm-activity" && dig.nodes[0].status === "done", "activity node", dig.nodes[0]);
must(dig.nodes[0].title === "命令完成，exit 0", "post summary", dig.nodes[0].title);
must(dig.nodes[0].detail.includes("echo ok") && dig.nodes[0].detail.includes("run.log"), "expanded detail", dig.nodes[0].detail);
must(dig.terminal.length === 2, "stream/post stay terminal", dig.terminal);
must(dig.calls.size === 0 && dig.subagents.length === 0, "pm tools are not subagent calls", dig.subagents);
'''
    subprocess.run(["node"], input=script, text=True, encoding="utf-8", check=True)


def test_tool_stream_and_icon_stop_controls_are_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    timeline_js = c.get("/app-timeline.js").text
    css = c.get("/app.css").text
    assert 't === "tool_stream"' in js
    assert 'p.stream === "stderr" ? "err" : "out"' in js
    assert 'p.tool === "run_command" && p.input && p.input.command' in js
    assert 'className="btn danger icon stop-btn"' in js
    assert 'aria-label=${d.cancelSession}' in js
    assert "function TermPanel({ d, terminal, agentType, sessionRow, onCancelSession })" in js
    assert 'String(e.key || "").toLowerCase() === "c"' in js
    assert "onCancelSession(sessionRow.id)" in js
    assert 'className="term-input"' in js
    assert ".term-input-row" in css and ".term-input" in css
    assert ".stop-icon" in css and "background: currentColor" in css
    assert "function ThinkingPanel" in timeline_js
    assert 'className=${`pm-thinking${open ? " open" : ""}`}' in timeline_js
    assert 'className="pm-thinking-head"' in timeline_js and "aria-expanded=${open}" in timeline_js
    assert "pmThinkingParts(text, d.thinkingTrace)" in timeline_js and 'className="pm-thinking-title"' in timeline_js
    assert 'const txt = t === "pm_reasoning" ? formatPmReasoningText(cleaned) : displayPmStreamText(cleaned, lang, d);' in js
    assert "<${MD} text=${parts.body} maxChars=${4000} />" in timeline_js
    assert ".pm-thinking-head:hover .pm-thinking-icon" in css and ".pm-thinking.open .pm-thinking-icon" in css
    assert ".pm-thinking .markdown-body" in css
    assert ".pm-thinking .markdown-body p" in css and "white-space: normal" in css
    assert "function PmActivity" in timeline_js and 'kind: "pm-activity"' in js
    assert "isPmToolEvent(e, p)" in js and "upsertPmActivityPost(e, p)" in js
    assert ".pm-activity" in css and ".pm-activity-body" in css


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
    subprocess.run(["node"], input=script, text=True, encoding="utf-8", check=True)


def test_subagent_digest_keeps_running_replies_in_chronological_timeline():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    start = js.index("function extractTextParts")
    end = js.index("function Empty", start)
    helpers = js[start:end]
    script = helpers + r'''
const must = (cond, label, value) => { if (!cond) { console.error(label, value); process.exit(1); } };
const d = { ev_stop: "Done" };
const events = [
  { id: "start", type: "agent_start", source: "codex", session_id: "s1", task_id: "call1", ts: "2026-01-01T00:00:00Z",
    payload: { command: ["codex", "exec", "--json", "fix UI"] } },
  { id: "say1", type: "agent_output", source: "codex", session_id: "s1", task_id: "call1", ts: "2026-01-01T00:00:01Z",
    payload: { text: "I will inspect the UI first." } },
  { id: "cmd1", type: "agent_output", source: "codex", session_id: "s1", task_id: "call1", ts: "2026-01-01T00:00:02Z",
    payload: { type: "item.completed", item: { id: "c1", type: "command_execution", command: "Get-Content app.js", status: "completed", exit_code: 0 } } },
  { id: "say2", type: "agent_output", source: "codex", session_id: "s1", task_id: "call1", ts: "2026-01-01T00:00:03Z",
    payload: { type: "item.completed", item: { id: "m1", type: "agent_message", text: "我会按顺序继续检查界面。" } } },
  { id: "cmd2", type: "agent_output", source: "codex", session_id: "s1", task_id: "call1", ts: "2026-01-01T00:00:04Z",
    payload: { type: "item.completed", item: { id: "c2", type: "command_execution", command: "Select-String finalReply app.js", status: "completed", exit_code: 0 } } },
];
const running = digest(events, d, "en");
const call = Array.from(running.calls.values())[0];
must(call.reply === "", "running output is not final reply", call.reply);
must(call.lastReply === "我会按顺序继续检查界面。", "last running reply tracked", call.lastReply);
must(call.timeline.map((x) => x.kind).join(">") === "cmd>reply>step>reply>step", "timeline order", call.timeline);
must(call.timeline.filter((x) => x.kind === "reply").every((x) => !x.final), "no synthetic final reply", call.timeline);

const done = digest([...events, { id: "stop", type: "stop", source: "codex", session_id: "s1", task_id: "call1", ts: "2026-01-01T00:00:05Z", payload: { result: "最终总结" } }], d, "en");
const doneCall = Array.from(done.calls.values())[0];
must(doneCall.reply === "最终总结", "stop result becomes final", doneCall.reply);
const last = doneCall.timeline[doneCall.timeline.length - 1];
must(last.kind === "reply" && last.final && last.text === "最终总结", "final reply is last timeline item", doneCall.timeline);
const sub = done.subagents[0];
must(sub.name === "codex", "subagent title stays agent identity", sub);
must(sub.act === "Select-String finalReply app.js", "activity uses latest step", sub);
must(sub.detail === "最终总结", "subagent detail preserves final text", sub);
'''
    subprocess.run(["node"], input=script, text=True, encoding="utf-8", check=True)


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
    bundle = _dashboard_bundle(c)
    assert "/api/sessions/${encodeURIComponent(id)}/cancel" in js
    assert 'api(`/api/sessions/${encodeURIComponent(id)}`' in js
    assert "session_busy" in bundle and "!live" in js and "waiting_approval" in js
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
    assert "allowed_" + "commands" not in js and "allowed_origins" in js
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


def test_composer_shows_workspace_git_status_instead_of_success_noise():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "已下发" not in js and "已发送到当前" not in js
    assert "Dispatched" not in js and "Sent to current session" not in js
    assert "WorkspaceGitStatus" in js
    assert "/api/workspaces/git-status" in js and "/api/workspaces/init-git" in js
    assert "/api/workspaces/checkout-branch" in js
    assert "function effectiveSessionWorkspace(row, fallback)" in js
    assert "const effectiveWorkspace = effectiveSessionWorkspace(sessionRow, workspace)" in js
    assert "row.workspace_exists !== false" in js
    assert "workspaceNoWorktree" in js and "hasSession=${!!sessionRow}" in js
    assert "${d.workspaceWorktree}: ${shortPath(workspace, d)}" in js
    assert "value=${effectiveWorkspace}" in js


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


def test_markdown_file_references_open_or_preview_from_workspace():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text
    assert "foreman:file-ref" in js
    assert "/api/workspace-file/read" in js and "/api/workspace-file/open" in js
    assert 'matchMedia("(max-width: 760px)")' in js
    assert ".inline-file-ref" in css and ".file-viewer-pre" in css

    start = js.index("function isMobileViewport")
    end = js.index("function renderInline", start)
    helpers = js[start:end]
    script = helpers + r'''
const must = (cond, label) => { if (!cond) { console.error(label); process.exit(1); } };
must(isLocalFileRef("docs/WHOLE_COMPUTER_CONTROL.zh-CN.md"), "relative md path");
must(isLocalFileRef("src/foreman/server/web/app.js:12"), "relative source path with line");
must(isLocalFileRef("C:\\Users\\me\\project\\README.md"), "absolute windows path");
must(!isLocalFileRef("print('hello')"), "ordinary code stays code");
must(!isLocalFileRef("https://example.com/a.md"), "url is not a local file ref");
'''
    subprocess.run(["node"], input=script, text=True, encoding="utf-8", check=True)


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
    js = c.get("/app-core.js").text
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
