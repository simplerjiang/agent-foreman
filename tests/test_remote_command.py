from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.server.auth_manager import AuthManager
from foreman.server.store import ServerStore
from foreman.shared.config import load_config


class FakeRelay:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or {"ok": True, "session_id": "s1"}

    async def route_with_ack(self, account_id, env, *, process_id):
        self.calls.append(
            {
                "account_id": account_id,
                "process_id": process_id,
                "kind": env.kind,
                "id": env.id,
                "payload": env.payload,
            }
        )
        return dict(self.result)


def _client(tmp_path, relay):
    st = ServerStore(str(tmp_path / "team.db"))
    st.init()
    auth = AuthManager(st)
    auth.create_account("alice", "password1")
    token = auth.login("alice", "password1")["token"]
    app = create_app(load_config(tmp_path / "none.yaml"), auth=auth, relay=relay)
    return TestClient(app), token, relay


def test_remote_dispatch_requires_auth(tmp_path):
    client, _token, _relay = _client(tmp_path, FakeRelay())
    r = client.post("/api/dispatch", json={"process_id": "p1", "goal": "g"})
    assert r.status_code == 401


def test_remote_dispatch_routes_command_by_token_account_and_process(tmp_path):
    client, token, relay = _client(tmp_path, FakeRelay({"ok": True, "session_id": "s1"}))
    r = client.post(
        "/api/dispatch",
        headers={"Authorization": f"Bearer {token}"},
        json={"process_id": "p1", "goal": "ship", "workspace": "E:/AutoWorkAgent"},
    )
    assert r.status_code == 200
    assert r.json()["session_id"] == "s1"
    assert relay.calls[0]["process_id"] == "p1"
    assert relay.calls[0]["payload"]["action"] == "dispatch"
    assert relay.calls[0]["payload"]["goal"] == "ship"


def test_remote_dispatch_requires_process_id(tmp_path):
    client, token, relay = _client(tmp_path, FakeRelay())
    r = client.post(
        "/api/dispatch",
        headers={"Authorization": f"Bearer {token}"},
        json={"process_id": "", "goal": "ship"},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "process_required"
    assert relay.calls == []


def test_remote_dispatch_maps_machine_offline(tmp_path):
    client, token, _relay = _client(tmp_path, FakeRelay({"ok": False, "error": "machine_offline"}))
    r = client.post(
        "/api/dispatch",
        headers={"Authorization": f"Bearer {token}"},
        json={"process_id": "p1", "goal": "ship"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "machine_offline"


def test_remote_approve_routes_card_choice(tmp_path):
    client, token, relay = _client(tmp_path, FakeRelay({"ok": True}))
    r = client.post(
        "/api/approve",
        headers={"Authorization": f"Bearer {token}"},
        json={"process_id": "p1", "card_id": "c1", "option": "approve"},
    )
    assert r.status_code == 200
    assert relay.calls[0]["payload"]["action"] == "card_choice"
    assert relay.calls[0]["payload"]["card_id"] == "c1"


def test_remote_snapshot_routes_snapshot_request(tmp_path):
    client, token, relay = _client(
        tmp_path, FakeRelay({"ok": True, "sessions": [], "cards": [], "kind": "snapshot"})
    )
    r = client.post(
        "/api/snapshot",
        headers={"Authorization": f"Bearer {token}"},
        json={"process_id": "p1"},
    )
    assert r.status_code == 200
    assert r.json()["kind"] == "snapshot"
    assert relay.calls[0]["kind"] == "snapshot_req"


def test_remote_autonomy_routes_to_selected_process(tmp_path):
    client, token, relay = _client(tmp_path, FakeRelay({"ok": True, "level": 2}))
    r = client.post(
        "/api/remote/settings/autonomy",
        headers={"Authorization": f"Bearer {token}"},
        json={"process_id": "p1", "level": 2},
    )
    assert r.status_code == 200
    assert r.json()["level"] == 2
    assert relay.calls[0]["process_id"] == "p1"
    assert relay.calls[0]["payload"]["action"] == "set_autonomy"
    assert relay.calls[0]["payload"]["level"] == 2
