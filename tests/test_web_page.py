"""Non-browser checks for the timeline page (TASKS T1.11).

Confirms the static page ships and wires the API + WS (browser acceptance is done separately
with a real browser per the autodev loop).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.shared.config import WorkspaceCfg, load_config


def test_index_served():
    c = TestClient(create_app(load_config()))
    r = c.get("/")
    assert r.status_code == 200
    assert "Foreman" in r.text


def test_app_js_wires_api_and_ws():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js")
    assert js.status_code == 200
    assert "/api/sessions" in js.text
    assert "/ws?session_id=" in js.text
    assert "ReactDOM.createRoot" in js.text and "A.Layout" in js.text
    # React renders agent output as text; never assign event payloads to raw HTML.
    assert ".innerHTML" not in js.text


def test_i18n_wired_in_page():
    c = TestClient(create_app(load_config()))
    html = c.get("/").text
    assert "/vendor/react.production.min.js" in html
    assert "/vendor/antd.min.js" in html
    assert "/vendor/htm.umd.js" in html
    js = c.get("/app.js").text
    assert "I18N" in js and "/api/settings/language" in js


def test_autonomy_dial_wired_in_page():
    """The PWA exposes the autonomy dial (0/1/2/3) and syncs it to the backend (T4.4 §6.4)."""
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "/api/settings/autonomy" in js and "loadAutonomy" in js
    assert "A.Slider" in js and "saveAutonomy" in js
    assert "自动执行权限" in js and "autonomyHelp" in js


def test_debug_mode_gates_raw_data_and_event_meta_chips():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "foreman.debug" in js and "debugMode" in js and "setDebugMode" in js
    assert "A.Switch" in js and "调试模式" in js
    assert "debugMode && event.payload" in js and "debugMode=${debugMode}" in js
    assert "eventMetaChips" in js and "showAgent" in js and "ApiOutlined" in js
    assert "payload.deliberation" in js and "payload.todo" in js
    assert "mergeStreamEvent" in js and "pm_reasoning" in js and "agent_reasoning" in js
    assert "groupTimelineEvents" in js and "event-stream-panel" in js
    assert "defaultActiveKey=${[\"body\"]}" in js


def test_followup_compact_source_and_mobile_nav_wired():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "clientSource" in js and "source: clientSource()" in js
    assert "body.session_id = activeSession.id" in js
    assert "/compact" in js and "compactContext" in js
    assert "A.Drawer" in js and "MenuOutlined" in js and "mobile-menu-button" in js
    assert "d.continueSession" in js and "d.newSession" in js


def test_dispatch_form_uses_pm_model_not_agent_choice():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    assert "dispatchPmModel" in js and "pmModelDefault" in js
    assert "pmTransport" in js and "transport: llm.transport" in js
    assert 'api("/api/models")' in js
    assert "body.model = model.trim()" in js
    assert "body.agent = agent" not in js
    assert "body.effort = effort" not in js


def test_dispatch_composer_floats_at_bottom_and_embeds_options():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text

    assert "function TaskComposer" in js
    assert "className=\"task-composer-shell\"" in js
    assert "className=\"task-composer-box\"" in js
    assert "className=\"task-composer-toolbar\"" in js
    assert "className=\"task-composer-workspace\"" in js
    assert "className=\"task-composer-model\"" in js
    assert "disabled=${Boolean(selectedSessionRow)}" in js
    assert ".task-composer-shell" in css
    assert "position: fixed;" in css and "bottom: max(" in css
    assert "left: 292px;" in css
    assert ".workspace-view" in css and "padding-bottom:" in css


def test_workspace_menu_endpoint_and_frontend_wired(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.workspaces = [WorkspaceCfg(path="D:/proj", name="Project")]
    c = TestClient(create_app(cfg))

    assert c.get("/api/workspaces").json() == [{"path": "D:/proj", "name": "Project"}]
    js = c.get("/app.js").text
    assert "/api/workspaces" in js and "loadWorkspaces" in js
    assert "saveWorkspace" in js and "deleteWorkspace" in js
    assert "A.Select" in js and "setWorkspace" in js


def test_llm_key_input_frontend_wired(tmp_path):
    c = TestClient(create_app(load_config(tmp_path / "none.yaml")))
    js = c.get("/app.js").text
    assert "A.Input.Password" in js
    assert "api_key" in js and "clearLlmKey" in js


def test_agent_settings_frontend_wired(tmp_path):
    c = TestClient(create_app(load_config(tmp_path / "none.yaml")))
    js = c.get("/app.js").text
    assert "/api/settings/agents" in js
    assert "agentSettings" in js and "saveAgentSettings" in js
    assert "agentResolvedPath" in js and "agentNotFound" in js
    assert "agentFullAccess" in js and "full_access" in js
    assert "SaveOutlined" in js and "A.Switch" in js


def test_decision_card_and_detail_wired(tmp_path):
    """The PWA fetches cards + drills into step detail, and renders the diff safely (T4.3 §6.3)."""
    c = TestClient(create_app(load_config()))
    html = c.get("/").text
    assert '<div id="root">' in html and "/app.js" in html
    js = c.get("/app.js").text
    assert "/api/cards" in js and "/api/actions/" in js and "/detail" in js
    assert "chooseCard" in js and "/choose" in js  # one-tap card decision wired
    # diff + raw output are untrusted -> rendered through React, never assigned to innerHTML.
    assert "renderDiff" in js and ".innerHTML" not in js


def test_llm_markdown_rendering_wired_safely(tmp_path):
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js").text
    css = c.get("/app.css").text

    assert "function MarkdownBody" in js and "renderMarkdownBlocks" in js
    assert "text=${summary} className=\"event-body\"" in js
    assert "text=${report.body_md || \"\"} className=\"report-body\"" in js
    assert "text=${card.summary || \"\"} className=\"markdown-title\"" in js
    assert "text=${card.audit_note} className=\"markdown-compact\"" in js
    assert "approval.action || approval.diff_summary" in js
    assert "<pre className=\"event-body\"" not in js
    assert "<pre className=\"report-body\"" not in js
    assert ".innerHTML" not in js
    assert "dangerouslySetInnerHTML" not in js
    assert ".markdown-body" in css and ".markdown-table-wrap" in css


def test_admin_console_spa_ships_and_wires(tmp_path):
    """The new Ant Design console SPA ships and is served at /app.html and /admin.html (the
    back-compat alias). It's login-gated client-side and wires the admin dashboard endpoints."""
    c = TestClient(create_app(load_config()))
    for path in ("/app.html", "/admin.html"):
        page = c.get(path)
        assert page.status_code == 200, path
        # loads the vendored Ant Design stack + the first-party app code (no build step)
        assert "admin-app.js" in page.text and "/vendor/antd.min.js" in page.text

    app_js = c.get("/admin-app.js").text
    # login + the admin dashboard endpoints are wired
    assert "/api/auth/login" in app_js and "/api/auth/me" in app_js
    assert "/api/admin/overview" in app_js and "/api/admin/accounts" in app_js
    assert "/api/admin/sessions" in app_js and "/api/admin/db" in app_js
    assert "/api/admin/logs" in app_js
    # rendering goes through React (htm), never raw innerHTML of server/account data (XSS).
    assert "htm.bind" in app_js and ".innerHTML" not in app_js


def test_redeem_page_still_ships(tmp_path):
    """The legacy invite-redemption page still ships (the new SPA also has a redeem tab)."""
    c = TestClient(create_app(load_config()))
    assert c.get("/redeem.html").status_code == 200
    redeem_js = c.get("/redeem.js").text
    assert "/api/auth/redeem" in redeem_js and ".innerHTML" not in redeem_js


def test_access_keys_page_ships_and_wires(tmp_path):
    """The user-facing access-key page ships and wires mint/list/revoke + expiry (T7.3 §8.2/§8.4)."""
    c = TestClient(create_app(load_config()))
    keys_html = c.get("/keys.html")
    assert keys_html.status_code == 200 and "data-i18n" in keys_html.text
    keys_js = c.get("/keys.js").text
    assert "/api/auth/login" in keys_js and "/api/keys" in keys_js
    assert "expires_in_days" in keys_js  # the expiry knob (§8.4) is wired
    assert "DELETE" in keys_js  # revoke
    # the key label is user-supplied → rendered via textContent only, never innerHTML (XSS).
    assert "textContent" in keys_js and ".innerHTML" not in keys_js
