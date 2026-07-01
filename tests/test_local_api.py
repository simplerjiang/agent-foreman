"""Tests for the local REST API + WS (TASKS T1.10).

create_app is injected with a CLIENT store (personal-mode wiring) — proving it is store-agnostic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from foreman.client.core.dispatch_service import DispatchService
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg, load_config
from foreman.shared.events import EventBus, make_event
from foreman.server.app import _subprocess_no_window_kwargs, create_app


def _app_with_store(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g1"))
    store.add_event(make_event("agent_output", "claude-code", "s1", payload={"text": "hi"}))
    store.add_event(make_event("stop", "claude-code", "s1", payload={"result": "done"}))
    return create_app(load_config(), store, EventBus())


def test_subprocess_no_window_kwargs_hides_windows_console():
    kwargs = _subprocess_no_window_kwargs()
    if os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert "startupinfo" in kwargs
    else:
        assert kwargs == {}


def test_api_sessions(tmp_path):
    c = TestClient(_app_with_store(tmp_path))
    sessions = c.get("/api/sessions").json()
    assert any(s["id"] == "s1" and s["goal"] == "g1" for s in sessions)


def test_api_sessions_expose_main_workspace_and_workspace_exists(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    main = tmp_path / "main"
    main.mkdir()
    missing = tmp_path / "missing-worktree"
    store.add_session(
        Session(id="s1", goal="g1", workspace=str(missing), main_workspace=str(main))
    )
    c = TestClient(create_app(load_config(), store, EventBus()))

    row = c.get("/api/sessions").json()[0]
    assert row["workspace"] == str(missing)
    assert row["main_workspace"] == str(main)
    assert row["workspace_exists"] is False


def test_api_events(tmp_path):
    c = TestClient(_app_with_store(tmp_path))
    events = c.get("/api/sessions/s1/events").json()
    assert len(events) == 2
    assert sorted(e["type"] for e in events) == ["agent_output", "stop"]
    payloads = {e["type"]: e["payload"] for e in events}
    assert payloads["agent_output"] == {"text": "hi"}  # payload_json parsed back to an object


def test_api_renames_session_title(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.add_session(Session(id="s1", goal="old title"))
    c = TestClient(create_app(load_config(), store, EventBus()))

    renamed = c.patch("/api/sessions/s1", json={"title": "  new title  "})
    assert renamed.status_code == 200
    assert renamed.json()["goal"] == "new title"
    assert store.get_session("s1").goal == "new title"

    empty = c.patch("/api/sessions/s1", json={"title": "  "})
    assert empty.status_code == 400
    assert empty.json()["detail"] == "empty_goal"

    missing = c.patch("/api/sessions/nope", json={"title": "x"})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "session_not_found"


def test_api_cancel_and_delete_session(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g1", status="running"))
    c = TestClient(create_app(load_config(), store, EventBus(), dispatcher=DispatchService(load_config(), store)))

    cancelled = c.post("/api/sessions/s1/cancel")
    assert cancelled.status_code == 200
    assert store.get_session("s1").status == "cancelled"

    deleted = c.delete("/api/sessions/s1")
    assert deleted.status_code == 200
    assert store.get_session("s1") is None


def test_api_refuses_delete_live_session(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g1", status="running"))
    c = TestClient(create_app(load_config(), store, EventBus(), dispatcher=DispatchService(load_config(), store)))

    deleted = c.delete("/api/sessions/s1")
    assert deleted.status_code == 409
    assert deleted.json()["detail"] == "session_busy"
    assert store.get_session("s1") is not None


def test_api_cancel_delete_require_dispatcher(tmp_path):
    c = TestClient(create_app(load_config()))
    assert c.post("/api/sessions/s1/cancel").status_code == 503
    assert c.delete("/api/sessions/s1").status_code == 503


def test_api_503_without_store():
    c = TestClient(create_app(load_config()))  # store=None
    assert c.get("/api/sessions").status_code == 503
    assert c.get("/api/sessions/x/events").status_code == 503
    assert c.patch("/api/sessions/x", json={"title": "new"}).status_code == 503


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
    assert agents == [
        {"name": "claude-code", "model": "sonnet", "effort": "high", "full_access": True}
    ]


def test_agent_settings_persist_and_refresh_runner(tmp_path):
    class Runner:
        def __init__(self):
            self.syncs = 0

        def sync_config(self):
            self.syncs += 1

    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    cfg.agents = {
        "claude-code": AgentCfg(command="claude", enabled=True),
        "codex": AgentCfg(command="codex", enabled=True),
    }
    runner = Runner()
    dispatcher = type("Dispatcher", (), {"runner": runner})()
    c = TestClient(create_app(cfg, store, EventBus(), dispatcher=dispatcher))

    saved = c.post(
        "/api/settings/agents",
        json={
            "agents": [
                {
                    "name": "claude-code",
                    "enabled": True,
                    "command": "claude.cmd",
                    "model": "sonnet",
                    "effort": "high",
                    "full_access": False,
                },
                {"name": "codex", "enabled": False, "command": "codex", "model": "gpt-5"},
            ]
        },
    ).json()

    assert {row["name"] for row in saved} == {"claude-code", "codex", "copilot-cli"}
    assert cfg.agents["claude-code"].command == "claude.cmd"
    assert cfg.agents["claude-code"].model == "sonnet"
    assert cfg.agents["claude-code"].full_access is False
    assert cfg.agents["codex"].enabled is False
    assert cfg.agents["copilot-cli"].enabled is False
    assert "claude.cmd" in (store.get_setting("agents.json") or "")
    assert runner.syncs >= 2
    assert c.get("/api/agents").json() == [
        {"name": "claude-code", "model": "sonnet", "effort": "high", "full_access": False}
    ]


def test_agent_settings_rejects_disabling_all(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    c = TestClient(create_app(Config(), store, EventBus()))
    res = c.post(
        "/api/settings/agents",
        json={
            "agents": [
                {"name": "claude-code", "enabled": False, "command": "claude"},
                {"name": "codex", "enabled": False, "command": "codex"},
            ]
        },
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "no_enabled_agent"


def test_agent_settings_can_enable_copilot_cli(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    c = TestClient(create_app(Config(), store, EventBus()))

    saved = c.post(
        "/api/settings/agents",
        json={
            "agents": [
                {"name": "claude-code", "enabled": False, "command": "claude"},
                {"name": "codex", "enabled": False, "command": "codex"},
                {
                    "name": "copilot-cli",
                    "enabled": True,
                    "command": sys.executable,
                    "model": "gpt-test",
                    "effort": "high",
                    "full_access": True,
                },
            ]
        },
    ).json()

    row = next(item for item in saved if item["name"] == "copilot-cli")
    assert row["enabled"] is True
    assert row["command"] == sys.executable
    assert row["model"] == "gpt-test"
    assert row["effort"] == "high"
    assert c.get("/api/agents").json() == [
        {"name": "copilot-cli", "model": "gpt-test", "effort": "high", "full_access": True}
    ]


def test_api_models_returns_pm_defaults_without_key():
    cfg = Config()
    cfg.llm.model = "pm-model"
    cfg.secrets.llm_api_key = ""
    cfg.agents = {
        "codex": AgentCfg(command="codex", enabled=True, model="agent-model"),
    }
    c = TestClient(create_app(cfg))
    data = c.get("/api/models").json()
    assert data["models"] == [{"id": "pm-model", "source": "pm"}]
    assert data["default"] == "pm-model"
    assert "LLMConfigError" in data["error"]


def test_api_models_preserves_provider_context_metadata(monkeypatch):
    class FakeLLM:
        def __init__(self, *_args, **_kwargs):
            pass

        async def list_model_infos(self):
            return [{"id": "big-model", "context_length": 256000, "max_tokens": 8192}]

        async def aclose(self):
            pass

    monkeypatch.setattr("foreman.shared.llm.LLMClient", FakeLLM)
    cfg = Config()
    cfg.llm.model = "pm-model"
    cfg.secrets.llm_api_key = "k"
    data = TestClient(create_app(cfg)).get("/api/models").json()

    assert {
        "id": "big-model",
        "source": "provider",
        "context_length": 256000,
    } in data["models"]


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


def test_llm_settings_persist_reasoning_effort(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    c = TestClient(create_app(cfg, store, EventBus()))

    saved = c.post("/api/settings/llm", json={"reasoning_effort": "max"}).json()

    assert saved["reasoning_effort"] == "max"
    assert store.get_setting("llm.reasoning_effort") == "max"
    assert cfg.llm.reasoning_effort == "max"


def test_llm_settings_reject_bad_reasoning_effort(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    res = TestClient(create_app(Config(), store, EventBus())).post(
        "/api/settings/llm", json={"reasoning_effort": "turbo"}
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "bad_reasoning_effort"


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
    assert store.get_session(session_id).main_workspace == path
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


def test_followup_without_workspace_falls_back_to_session_main_workspace(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    dispatcher = DispatchService(cfg, store)
    c = TestClient(create_app(cfg, store, EventBus(), dispatcher=dispatcher))
    main = tmp_path / "main"
    main.mkdir()
    missing = tmp_path / "gone-worktree"
    c.post("/api/workspaces", json={"path": str(main), "name": "Main"})

    res = c.post("/api/tasks", json={"goal": "do x", "workspace": str(main), "source": "desktop"})
    session_id = res.json()["session_id"]
    store.update_session(session_id, workspace=str(missing))

    follow = c.post(
        "/api/tasks",
        json={"goal": "do y", "session_id": session_id, "source": "desktop"},
    )
    assert follow.status_code == 200
    assert follow.json()["continued"] is True
    assert follow.json()["workspace"] == str(main)


def test_workspace_file_read_and_open_are_scoped(tmp_path, monkeypatch):
    ws = tmp_path / "project"
    docs = ws / "docs"
    docs.mkdir(parents=True)
    target = docs / "note.md"
    target.write_text("# 标题\n正文\n", encoding="utf-8")
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    cfg = Config(workspaces=[WorkspaceCfg(path=str(ws), name="Project")])
    c = TestClient(create_app(cfg))

    read = c.get(
        "/api/workspace-file/read",
        params={"workspace": str(ws), "path": "docs/note.md:2"},
    )
    assert read.status_code == 200
    assert read.json()["relative_path"] == "docs/note.md"
    assert "标题" in read.json()["content"]

    opened = []
    monkeypatch.setattr("foreman.server.app._open_file_with_system", lambda path: opened.append(path))
    res = c.post(
        "/api/workspace-file/open",
        json={"workspace": str(ws), "path": "docs/note.md"},
    )
    assert res.status_code == 200
    assert opened == [target.resolve()]

    escaped = c.get(
        "/api/workspace-file/read",
        params={"workspace": str(ws), "path": "../secret.md"},
    )
    assert escaped.status_code == 400
    assert escaped.json()["detail"] == "file_outside_workspace"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_workspace_git_status_and_init(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    c = TestClient(create_app(cfg, store, EventBus()))
    path = tmp_path / "project"
    path.mkdir()

    c.post("/api/workspaces", json={"path": str(path), "name": "Project"})
    before = c.get("/api/workspaces/git-status", params={"path": str(path)}).json()
    assert before["git_available"] is True
    assert before["is_git_repo"] is False
    assert before["can_init"] is True

    initialized = c.post("/api/workspaces/init-git", json={"path": str(path)}).json()
    assert initialized["is_git_repo"] is True
    assert initialized["worktree"].replace("\\", "/").endswith("/project")

    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Foreman Test", "-c", "user.email=foreman@example.test", "commit", "-m", "init"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )
    base_branch = c.get("/api/workspaces/git-status", params={"path": str(path)}).json()["branch"]

    subprocess.run(
        ["git", "checkout", "-b", "feature/ws-status"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    )
    after = c.get("/api/workspaces/git-status", params={"path": str(path)}).json()
    assert after["branch"] == "feature/ws-status"
    assert "feature/ws-status" in after["branches"]

    switched = c.post(
        "/api/workspaces/checkout-branch",
        json={"path": str(path), "branch": base_branch},
    ).json()
    assert switched["branch"] == base_branch


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_workspace_git_status_allows_session_owned_worktree(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    c = TestClient(create_app(cfg, store, EventBus()))
    path = tmp_path / "pm-worktree"
    path.mkdir()
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, text=True, check=True)
    store.add_session(Session(id="s1", goal="g", workspace=str(path)))

    status = c.get("/api/workspaces/git-status", params={"path": str(path)})
    assert status.status_code == 200
    assert status.json()["is_git_repo"] is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_workspace_git_status_allows_session_main_workspace_fallback(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    c = TestClient(create_app(cfg, store, EventBus()))
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init"], cwd=str(main), capture_output=True, text=True, check=True)
    store.add_session(
        Session(id="s1", goal="g", workspace=str(tmp_path / "gone"), main_workspace=str(main))
    )

    status = c.get("/api/workspaces/git-status", params={"path": str(main)})
    assert status.status_code == 200
    assert status.json()["is_git_repo"] is True


# ── /api/settings/llm: switch the PM brain at runtime (§15) ──────────────────────────────────────


def test_llm_settings_default_and_override(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.model = "gpt-4o"
    cfg.llm.transport = "http"
    cfg.secrets.llm_api_key = "k"
    c = TestClient(create_app(cfg, store, EventBus()))

    got = c.get("/api/settings/llm").json()
    assert got["provider"] == "openai" and got["model"] == "gpt-4o" and got["api_key_set"] is True
    assert got["transport"] == "http"
    assert got["context_window_tokens"] == 272000
    assert "max_tokens" not in got

    saved = c.post(
        "/api/settings/llm",
        json={
            "model": "gpt-5",
            "provider": "anthropic",
            "transport": "ws",
            "context_window_tokens": 272000,
            "max_tokens": 128000,
        },
    ).json()
    assert saved["model"] == "gpt-5" and saved["provider"] == "anthropic"
    assert saved["transport"] == "ws"
    assert saved["context_window_tokens"] == 272000
    assert "max_tokens" not in saved
    assert cfg.llm.transport == "ws"
    assert cfg.llm.context_window_tokens == 272000
    # persisted as a config_kv override (survives a fresh GET)
    got = c.get("/api/settings/llm").json()
    assert got["model"] == "gpt-5"
    assert got["transport"] == "ws"
    assert got["context_window_tokens"] == 272000
    assert "max_tokens" not in got
    assert store.get_setting("llm.max_tokens") is None


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


def test_llm_settings_rejects_bad_transport(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    c = TestClient(create_app(Config(), store, EventBus()))
    assert c.post("/api/settings/llm", json={"transport": "sse"}).status_code == 400


def test_llm_settings_503_without_store():
    c = TestClient(create_app(Config()))  # store=None
    assert c.post("/api/settings/llm", json={"model": "x"}).status_code == 503
