"""Tests for the local REST API + WS (TASKS T1.10).

create_app is injected with a CLIENT store (personal-mode wiring) — proving it is store-agnostic.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.shared.config import load_config
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
