from __future__ import annotations

import json

from fastapi.testclient import TestClient

from foreman.client.store import Store
from foreman.server.app import create_app
from foreman.shared.config import Config
from foreman.shared.events import EventBus


def test_pm_tool_settings_defaults_and_save(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    cfg.pm_tools.git_worktree = True
    cfg.pm_tools.worktree_roots = [str(tmp_path / "worktrees")]
    cfg.pm_tools.worktree_branch_prefix = "foreman/e2e-"
    cfg.pm_tools.default_base_ref = "main"
    c = TestClient(create_app(cfg, store, EventBus()))

    defaults = c.get("/api/settings/pm-tools").json()
    assert defaults["file_read"] is True
    assert defaults["file_write"] is False
    assert defaults["shell"] is False
    assert defaults["web_fetch"] is False
    assert defaults["web_search"] is False
    assert defaults["browser"] is False
    assert defaults["git_worktree"] is True
    assert defaults["worktree_roots"] == [str(tmp_path / "worktrees")]

    saved = c.post(
        "/api/settings/pm-tools",
        json={
            "file_write": True,
            "shell": True,
            "web_fetch": True,
            "web_search": True,
            "browser": True,
            "allowed_origins": ["http://example.test", "http://example.test"],
            "web_search_provider": "searxng",
            "searxng_url": "https://search.example.test",
            "browser_headless": True,
            "max_rounds": 99,
        },
    ).json()

    assert saved["file_read"] is True
    assert "allowed_" + "commands" not in saved
    assert saved["allowed_origins"] == ["http://example.test"]
    assert saved["web_search_provider"] == "searxng"
    assert saved["max_rounds"] == 99
    assert saved["git_worktree"] is True
    assert saved["worktree_roots"] == [str(tmp_path / "worktrees")]
    assert saved["worktree_branch_prefix"] == "foreman/e2e-"
    assert saved["default_base_ref"] == "main"
    assert cfg.pm_tools.shell is True


def test_pm_tool_settings_clamps_large_persisted_max_rounds(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.set_setting("pm_tools.json", json.dumps({"max_rounds": 9999999}))
    cfg = Config()
    c = TestClient(create_app(cfg, store, EventBus()))

    loaded = c.get("/api/settings/pm-tools").json()

    assert loaded["max_rounds"] == 999
    assert cfg.pm_tools.max_rounds == 999
