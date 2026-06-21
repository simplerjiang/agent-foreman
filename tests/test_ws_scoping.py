"""Team-mode /ws must be authenticated and account-scoped — no cross-tenant health leak (issue #9).

In team mode the relay box's bus carries cross-tenant `health` events. `/ws` therefore requires a
valid bearer token (query param, since browsers can't set WS headers) and hard-filters the stream to
the caller's account. Personal mode (no auth manager) is unchanged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


# ── integration: the auth gate + account scope on a real team app (store=None) ───────────────────
def _team_app(tmp_path):
    sstore = ServerStore(str(tmp_path / "srv.db"))
    sstore.init()
    auth = AuthManager(sstore)
    # store=None mirrors build_serve_app's team relay box (秘方/events live on each user's machine).
    app = create_app(load_config(tmp_path / "none.yaml"), auth=auth)
    return app, auth


def test_team_ws_rejects_missing_or_bad_token(tmp_path):
    app, _ = _team_app(tmp_path)
    client = TestClient(app)
    for url in ("/ws", "/ws?token=nonsense"):
        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect(url) as ws:
                ws.receive_text()
        assert ei.value.code == 1008  # policy violation: unauthenticated


def test_team_ws_streams_only_the_callers_account(tmp_path):
    """End-to-end: a valid token opens the stream, but another tenant's health frame is filtered
    out — the cross-tenant leak (issue #9) is closed even on a live connection."""
    app, auth = _team_app(tmp_path)
    auth.create_account("alice", "pw")
    client = TestClient(app)
    with client:  # enter lifespan so the blocking portal is live (publish into the app's loop)
        tok = client.post(
            "/api/auth/login", json={"username": "alice", "password": "pw"}
        ).json()["token"]
        mine = auth.store.get_account_by_username("alice").id
        with client.websocket_connect(f"/ws?token={tok}") as ws:
            # publish another tenant's frame FIRST, then the caller's: a working filter drops the
            # first and delivers only the second, so the first frame received must be the caller's.
            client.portal.call(app.state.bus.publish, _health(account_id="other-tenant"))
            client.portal.call(app.state.bus.publish, _health(account_id=mine))
            msg = ws.receive_json()
    assert msg["payload"]["account_id"] == mine
