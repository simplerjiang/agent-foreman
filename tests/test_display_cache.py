"""Remote-control relay state boundaries.

The relay no longer stores display snapshots. It only forwards snapshot/event frames and persists
tiny TTL notifications.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.cache_sync import build_snapshot, card_summary, session_summary
from foreman.client.store.db import Store
from foreman.client.store.models import Approval, Definition, Report
from foreman.server.app import create_app
from foreman.server.auth_manager import AuthManager
from foreman.server.relay import Relay, RelayClient
from foreman.server.store import ServerStore
from foreman.shared.config import load_config
from foreman.shared.events import EventBus
from foreman.shared.protocol import KIND_EVENT, KIND_NOTIFY, KIND_SNAPSHOT, Envelope


def _store(tmp_path):
    st = ServerStore(str(tmp_path / "team.db"))
    st.init()
    return st


class _SendOnlyWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, d: dict) -> None:
        self.sent.append(d)


def test_server_store_no_longer_creates_display_cache_tables(tmp_path):
    st = _store(tmp_path)
    rows = st.table_stats()
    names = {r["name"] for r in rows}
    assert "notifications" in names
    assert "push_subscriptions" in names
    assert "cache_sessions" not in names
    assert "cache_cards" not in names


def test_cache_endpoints_are_removed_even_for_authenticated_team_user(tmp_path):
    st = _store(tmp_path)
    auth = AuthManager(st)
    auth.create_account("alice", "password1")
    token = auth.login("alice", "password1")["token"]
    client = TestClient(create_app(load_config(tmp_path / "none.yaml"), auth=auth, relay=Relay(st)))

    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/cache/sessions", headers=headers).status_code == 404
    assert client.get("/api/cache/cards", headers=headers).status_code == 404


def test_build_snapshot_frame_shape_is_display_safe():
    class Session:
        id = "s1"
        goal = "ship"
        status = "running"
        agent_type = "pm"
        created_at = "c"
        updated_at = "u"

    class Card:
        id = "c1"
        session_id = "s1"
        summary = "approve?"
        audit_note = "note"
        diff_stat = "3 files"
        options_json = '[{"label":"Approve","action":"approve"}]'
        chosen = ""
        decided_at = ""
        ts = "t"

    env = build_snapshot([Session()], [Card()], corr_id="corr")
    assert env.kind == KIND_SNAPSHOT and env.id == "corr"
    assert env.payload["sessions"] == [session_summary(Session())]
    assert env.payload["cards"] == [card_summary(Card())]
    blob = str(env.payload)
    assert "access_key" not in blob and "key_hash" not in blob


def test_build_snapshot_includes_selected_local_process_state(tmp_path):
    store = Store(str(tmp_path / "local.db"))
    store.init()
    store.set_setting("autonomy.level", "3")
    store.set_setting("workspaces.json", '[{"path":"E:/AutoWorkAgent","name":"Foreman"}]')
    store.add_approval(
        Approval(
            id="a1",
            session_id="s1",
            action="deploy",
            risk_level="requires-approval",
            nonce="nonce-1",
            requested_at="t",
        )
    )
    store.add_report(Report(id="r1", title="Daily", body_md="done", ts="t"))
    store.add_definition(
        Definition(
            id="d1",
            kind="workflow",
            name="ship",
            is_active=True,
            body="steps: []",
            metadata_json='{"description":"ship work"}',
        )
    )
    cfg = load_config(tmp_path / "none.yaml")
    cfg.secrets.cloud_access_key = "fk_live_secret"
    cfg.secrets.llm_api_key = "sk-local"

    env = build_snapshot([], [], store=store, cfg=cfg)

    assert env.payload["autonomy"]["level"] == 3
    assert env.payload["workspaces"] == [{"path": "E:/AutoWorkAgent", "name": "Foreman"}]
    assert env.payload["approvals"][0]["id"] == "a1"
    assert env.payload["reports"][0]["title"] == "Daily"
    assert env.payload["definitions"][0]["body"] == "steps: []"
    assert env.payload["llm"]["api_key_set"] is True
    blob = str(env.payload)
    assert "fk_live_secret" not in blob and "sk-local" not in blob


async def test_relay_event_republishes_to_bus_without_persistence(tmp_path):
    st = _store(tmp_path)
    bus = EventBus()
    q = bus.subscribe_queue()
    relay = Relay(st, bus)
    client = RelayClient(account_id="a1", process_id="p1", key_id="k1", name="box", ws=_SendOnlyWS())

    await relay._on_frame(
        client,
        Envelope(
            kind=KIND_EVENT,
            id="e1",
            payload={"session_id": "s1", "type": "agent_output", "text": "hi"},
        ).to_dict(),
    )

    ev = q.get_nowait()
    assert ev.type == "relay_frame"
    assert ev.payload["account_id"] == "a1"
    assert ev.payload["frame"]["kind"] == KIND_EVENT
    assert "cache_sessions" not in {r["name"] for r in st.table_stats()}


async def test_relay_notify_enqueues_tiny_ttl_row_scoped_to_authenticated_account(tmp_path):
    st = _store(tmp_path)
    relay = Relay(st)
    client = RelayClient(account_id="a1", process_id="p1", key_id="k1", name="box", ws=_SendOnlyWS())
    hostile = Envelope(
        kind=KIND_NOTIFY,
        id="n1",
        account_id="evil",
        payload={
            "kind": "decision_needed",
            "ref": "card-1",
            "title": "Approve deploy?",
            "dedup_key": "card-1",
        },
    )

    await relay._on_frame(client, hostile.to_dict())

    rows = st.list_notifications("a1")
    assert len(rows) == 1
    assert rows[0].account_id == "a1"
    assert rows[0].title == "Approve deploy?"
    assert st.list_notifications("evil") == []


def test_legacy_cache_sync_frame_is_tolerated_but_not_persisted(tmp_path):
    st = _store(tmp_path)
    relay = Relay(st)
    client = RelayClient(account_id="a1", process_id="p1", key_id="k1", name="box", ws=_SendOnlyWS())

    import asyncio

    asyncio.run(relay._on_frame(client, Envelope(kind="cache_sync", payload={"sessions": []}).to_dict()))
    assert "cache_sessions" not in {r["name"] for r in st.table_stats()}
