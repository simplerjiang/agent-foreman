"""Team-mode /ws must be authenticated and account-scoped — no cross-tenant health leak (issue #9).

In team mode the relay box's bus carries cross-tenant `health` events. `/ws` therefore requires a
valid bearer token (query param, since browsers can't set WS headers) and hard-filters the stream to
the caller's account. Personal mode (no auth manager) is unchanged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.server.app import _event_visible_to, create_app
from foreman.server.auth_manager import AuthManager
from foreman.server.store import ServerStore
from foreman.shared.config import load_config
from foreman.shared.events import make_event


def _health(account_id="", session_id=""):
    return make_event(
        "health", source="relay", session_id=session_id,
        payload={"account_id": account_id, "online": True},
    )


# ── unit: the scoping that backs the /ws filter ──────────────────────────────────────────────────
def test_event_visible_personal_mode_only_narrows_by_session():
    ev = _health(account_id="acc-other", session_id="s1")
    assert _event_visible_to(ev, session_id=None, account_id=None) is True
    assert _event_visible_to(ev, session_id="s1", account_id=None) is True
    assert _event_visible_to(ev, session_id="s2", account_id=None) is False


def test_event_visible_team_mode_scopes_to_account():
    assert _event_visible_to(_health("me"), session_id=None, account_id="me") is True
    assert _event_visible_to(_health("them"), session_id=None, account_id="me") is False
    # an untagged event is NOT forwarded in team mode (fail-closed)
    untagged = make_event("health", source="relay", session_id="", payload={"online": True})
    assert _event_visible_to(untagged, session_id=None, account_id="me") is False


# ── integration: the auth gate on /ws ────────────────────────────────────────────────────────────
def _team_app(tmp_path, store=None):
    sstore = ServerStore(str(tmp_path / "srv.db"))
    sstore.init()
    auth = AuthManager(sstore)
    app = create_app(load_config(tmp_path / "none.yaml"), store=store, auth=auth)
    return app, auth


def test_team_ws_rejects_missing_or_bad_token(tmp_path):
    app, _ = _team_app(tmp_path)
    client = TestClient(app)
    for url in ("/ws", "/ws?token=nonsense"):
        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect(url) as ws:
                ws.receive_text()
        assert ei.value.code == 1008  # policy violation: unauthenticated


def test_team_ws_accepts_valid_token_and_streams_backlog(tmp_path):
    cstore = Store(str(tmp_path / "c.db"))
    cstore.init()
    cstore.add_session(Session(id="s1", goal="g"))
    cstore.add_event(make_event("agent_output", "claude-code", "s1", payload={"text": "hi"}))
    app, auth = _team_app(tmp_path, store=cstore)
    client = TestClient(app)
    auth.create_account("alice", "pw")
    tok = client.post("/api/auth/login", json={"username": "alice", "password": "pw"}).json()["token"]
    # a valid token opens the stream (proves it was NOT rejected) and backlog flows
    with client.websocket_connect(f"/ws?session_id=s1&token={tok}") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "agent_output" and msg["session_id"] == "s1"
