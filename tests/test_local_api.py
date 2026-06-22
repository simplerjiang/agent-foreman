"""Tests for the local REST API + WS (TASKS T1.10).

create_app is injected with a CLIENT store (personal-mode wiring) — proving it is store-agnostic.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.core.dispatch_service import DispatchService
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.shared.config import AgentCfg, Config, load_config
from foreman.shared.events import EventBus, make_event
from foreman.server.app import create_app


def _app_with_store(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g1"))
    store.add_event(make_event("agent_output", "claude-code", "s1", payload={"text": "hi"}))
    store.add_event(make_event("stop", "claude-code", "s1", payload={"result": "done"}))
    return create_app(load_config(), store, EventBus())


def test_api_sessions(tmp_path):
    c = TestClient(_app_with_store(tmp_path))
    sessions = c.get("/api/sessions").json()
    assert any(s["id"] == "s1" and s["goal"] == "g1" for s in sessions)


def test_api_events(tmp_path):
    c = TestClient(_app_with_store(tmp_path))
    events = c.get("/api/sessions/s1/events").json()
    assert len(events) == 2
    assert sorted(e["type"] for e in events) == ["agent_output", "stop"]
    payloads = {e["type"]: e["payload"] for e in events}
    assert payloads["agent_output"] == {"text": "hi"}  # payload_json parsed back to an object


def test_api_503_without_store():
    c = TestClient(create_app(load_config()))  # store=None
    assert c.get("/api/sessions").status_code == 503
    assert c.get("/api/sessions/x/events").status_code == 503


def test_ws_streams_backlog(tmp_path):
    app = _app_with_store(tmp_path)
    with TestClient(app) as c, c.websocket_connect("/ws?session_id=s1") as ws:
        m1 = ws.receive_json()
        m2 = ws.receive_json()
    assert sorted([m1["type"], m2["type"]]) == ["agent_output", "stop"]
    assert m1["session_id"] == "s1"


# ── /api/agents: the dispatch form's agent/model pickers (§5.1) ──────────────────────────────────


def test_api_agents_lists_enabled_with_model():
    cfg = Config()
    cfg.agents = {
        "claude-code": AgentCfg(command="claude", enabled=True, model="sonnet", effort="high"),
        "codex": AgentCfg(command="codex", enabled=False, model="gpt-5"),  # disabled → omitted
    }
    c = TestClient(create_app(cfg))
    agents = c.get("/api/agents").json()
    assert agents == [{"name": "claude-code", "model": "sonnet", "effort": "high"}]


def test_api_models_returns_configured_defaults_without_key():
    cfg = Config()
    cfg.llm.model = "pm-model"
    cfg.secrets.llm_api_key = ""
    cfg.agents = {
        "codex": AgentCfg(command="codex", enabled=True, model="agent-model"),
    }
    c = TestClient(create_app(cfg))
    data = c.get("/api/models?agent=codex").json()
    assert data["models"] == [
        {"id": "agent-model", "source": "agent"},
        {"id": "pm-model", "source": "pm"},
    ]
    assert data["default"] == "agent-model"
    assert "LLMConfigError" in data["error"]


def test_api_models_preview_uses_unsaved_settings_without_persisting(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    cfg.llm.model = "saved-model"
    cfg.secrets.llm_api_key = ""
    c = TestClient(create_app(cfg, store, EventBus()))

    data = c.post(
        "/api/models/preview",
        json={
            "provider": "anthropic",
            "model": "draft-model",
            "base_url": "https://example.invalid/v1",
        },
    ).json()

    assert data["models"] == [{"id": "draft-model", "source": "pm"}]
    assert data["default"] == "draft-model"
    assert "LLMConfigError" in data["error"]
    assert c.get("/api/settings/llm").json()["model"] == "saved-model"


# ── /api/workspaces: local UI can edit the workspace allowlist ──────────────────────────────────


def test_workspace_settings_can_dispatch_without_restart(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    dispatcher = DispatchService(cfg, store)
    c = TestClient(create_app(cfg, store, EventBus(), dispatcher=dispatcher))
    path = str(tmp_path / "project")

    rows = c.post("/api/workspaces", json={"path": path, "name": "Project"}).json()
    assert rows == [{"path": path, "name": "Project"}]
    assert cfg.workspaces[0].path == path

    res = c.post("/api/tasks", json={"goal": "do x", "workspace": path, "source": "desktop"})
    assert res.status_code == 200
    assert res.json()["workspace"] == path
    session_id = res.json()["session_id"]
    events = c.get(f"/api/sessions/{session_id}/events").json()
    assert events[-1]["source"] == "desktop"

    follow = c.post(
        "/api/tasks",
        json={"goal": "do y", "workspace": path, "session_id": session_id, "source": "desktop"},
    )
    assert follow.status_code == 200
    assert follow.json()["session_id"] == session_id
    assert follow.json()["continued"] is True

    compact = c.post(f"/api/sessions/{session_id}/compact")
    assert compact.status_code == 200
    assert compact.json()["summary"]

    assert c.delete("/api/workspaces", params={"path": path}).json() == []
    assert c.get("/api/workspaces").json() == []


# ── /api/settings/llm: switch the PM brain at runtime (§15) ──────────────────────────────────────


def test_llm_settings_default_and_override(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.model = "gpt-4o"
    cfg.secrets.llm_api_key = "k"
    c = TestClient(create_app(cfg, store, EventBus()))

    got = c.get("/api/settings/llm").json()
    assert got["provider"] == "openai" and got["model"] == "gpt-4o" and got["api_key_set"] is True

    saved = c.post("/api/settings/llm", json={"model": "gpt-5", "provider": "anthropic"}).json()
    assert saved["model"] == "gpt-5" and saved["provider"] == "anthropic"
    # persisted as a config_kv override (survives a fresh GET)
    assert c.get("/api/settings/llm").json()["model"] == "gpt-5"


def test_llm_settings_blank_key_is_not_configured(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    cfg.secrets.llm_api_key = "  "
    c = TestClient(create_app(cfg, store, EventBus()))

    assert c.get("/api/settings/llm").json()["api_key_set"] is False


def test_llm_settings_saves_and_clears_api_key_in_env(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    cfg.env_path = str(tmp_path / ".env")
    c = TestClient(create_app(cfg, store, EventBus()))

    saved = c.post("/api/settings/llm", json={"api_key": "sk-test"}).json()
    assert saved["api_key_set"] is True
    assert cfg.secrets.llm_api_key == "sk-test"
    assert "FOREMAN_LLM_API_KEY=sk-test" in (tmp_path / ".env").read_text(encoding="utf-8")

    cleared = c.post("/api/settings/llm", json={"api_key": ""}).json()
    assert cleared["api_key_set"] is False
    assert cfg.secrets.llm_api_key == ""
    assert "FOREMAN_LLM_API_KEY" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_llm_settings_rejects_bad_provider(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    c = TestClient(create_app(Config(), store, EventBus()))
    assert c.post("/api/settings/llm", json={"provider": "groq"}).status_code == 400


def test_llm_settings_503_without_store():
    c = TestClient(create_app(Config()))  # store=None
    assert c.post("/api/settings/llm", json={"model": "x"}).status_code == 503
