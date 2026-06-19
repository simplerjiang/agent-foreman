"""Tests for i18n: language_directive + the /api/settings/language endpoint (TASKS T1.13)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.store import Store
from foreman.server.app import create_app
from foreman.shared.config import load_config
from foreman.shared.i18n import language_directive, normalize


def test_normalize_and_directive():
    assert normalize("zh") == "zh"
    assert normalize("zh-CN") == "zh"
    assert normalize("English") == "en"
    assert normalize("en-US") == "en"
    assert normalize(None) == "zh"        # default
    assert normalize("klingon") == "zh"   # unknown → default
    assert "中文" in language_directive("zh")
    assert "English" in language_directive("en")


def test_language_endpoint_roundtrip(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    c = TestClient(create_app(load_config(), store))
    assert c.get("/api/settings/language").json()["language"] == "zh"  # config default
    assert c.post("/api/settings/language", json={"language": "English"}).json()["language"] == "en"
    assert c.get("/api/settings/language").json()["language"] == "en"  # persisted to config_kv


def test_set_language_without_store_returns_503():
    c = TestClient(create_app(load_config()))  # store=None
    assert c.post("/api/settings/language", json={"language": "en"}).status_code == 503
