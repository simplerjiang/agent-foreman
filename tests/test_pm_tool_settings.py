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
    c = TestClient(create_app(cfg, store, EventBus()))

    defaults = c.get("/api/settings/pm-tools").json()
    assert defaults["file_read"] is True
    assert defaults["file_write"] is False
    assert defaults["shell"] is False
    assert defaults["web_fetch"] is False
    assert defaults["web_search"] is False
    assert defaults["browser"] is False

    saved = c.post(
        "/api/settings/pm-tools",
        json={
            "file_write": True,
            "shell": True,
            "web_fetch": True,
            "web_search": True,
            "browser": True,
            "allowed_commands": ["python --version", "", "python --version"],
            "allowed_origins": ["http://example.test", "http://example.test"],
            "web_search_provider": "searxng",
            "searxng_url": "https://search.example.test",
            "browser_headless": True,
            "max_rounds": 99,
        },
    ).json()

    assert saved["file_read"] is True
    assert saved["allowed_commands"] == ["python --version"]
    assert saved["allowed_origins"] == ["http://example.test"]
    assert saved["web_search_provider"] == "searxng"
    assert saved["max_rounds"] == 99
    assert cfg.pm_tools.shell is True


def test_pm_tool_settings_preserves_large_persisted_max_rounds(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.set_setting("pm_tools.json", json.dumps({"max_rounds": 9999999}))
    cfg = Config()
    c = TestClient(create_app(cfg, store, EventBus()))

    loaded = c.get("/api/settings/pm-tools").json()

    assert loaded["max_rounds"] == 9999999
    assert cfg.pm_tools.max_rounds == 9999999
