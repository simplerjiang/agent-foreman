"""Non-browser checks for the timeline page (TASKS T1.11).

Confirms the static page ships and wires the API + WS (browser acceptance is done separately
with a real browser per the autodev loop).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.shared.config import load_config


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
    # agent output must be rendered safely (no innerHTML of event payloads)
    assert "textContent" in js.text
