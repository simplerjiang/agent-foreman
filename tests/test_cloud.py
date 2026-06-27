"""Cloud relay connection (DESIGN §8.5) — the Settings → 云端连接 feature.

Covers the CloudManager lifecycle against a fake relay (connect → connected, auth-deny → error,
disconnect → offline) and the /api/settings/cloud endpoints (save url + key, connect/disconnect,
key never returned, unavailable when no manager is injected).
"""

from __future__ import annotations

import asyncio
import hashlib
import time

from fastapi.testclient import TestClient

from foreman.client.core.cloud import CloudManager, normalize_relay_url
from foreman.client.relay import RelayConnector
from foreman.server.app import create_app
from foreman.shared.config import load_config
from foreman.shared.protocol import (
    KIND_ACK,
    KIND_COMMAND,
    KIND_HELLO_ACK,
    KIND_SNAPSHOT,
    KIND_SNAPSHOT_REQ,
    Envelope,
    attach_mac,
)


class FakeStore:
    def __init__(self) -> None:
        self._kv: dict[str, str] = {}

    def get_setting(self, key: str):
        return self._kv.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self._kv[key] = value

    def get_sessions(self):
        return []

    def get_decision_cards(self, session_id=None):
        return []


class _FakeConn:
    """A relay connection that handshakes OK (or denies) then holds the line open."""

    def __init__(self, *, ack_ok: bool = True) -> None:
        self._ack_ok = ack_ok
        self._acked = False
        self._closed = asyncio.Event()
        self.sent: list[str] = []

    async def send(self, data: str) -> None:  # noqa: D401
        self.sent.append(data)

    async def recv(self) -> str:
        if not self._acked:
            self._acked = True
            return Envelope(kind=KIND_HELLO_ACK, payload={"ok": self._ack_ok, "process_id": "p"}).to_json()
        await self._closed.wait()
        raise ConnectionError("closed")

    async def close(self) -> None:
        self._closed.set()


class _SlowHandshakeConn:
    """A relay connection whose first hello_ack arrives after the connect wait window."""

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def send(self, data: str) -> None:  # noqa: D401
        pass

    async def recv(self) -> str:
        await self._closed.wait()
        raise ConnectionError("closed")

    async def close(self) -> None:
        self._closed.set()


def _factory(ack_ok: bool = True, conns: list | None = None):
    def make(*, url, access_key, process_id, name, on_status, on_error=None, sync_provider=None):
        async def connect(_u):
            conn = _FakeConn(ack_ok=ack_ok)
            if conns is not None:
                conns.append(conn)
            return conn
        return RelayConnector(
            url, access_key, process_id=process_id, name=name, on_status=on_status,
            on_error=on_error, sync_provider=sync_provider, sync_interval=0.05,
            connect=connect, heartbeat_interval=0, backoff_base=0.05,
        )
    return make


def _factory_slow_handshake():
    def make(*, url, access_key, process_id, name, on_status, on_error=None, sync_provider=None):
        async def connect(_u):
            return _SlowHandshakeConn()
        return RelayConnector(
            url, access_key, process_id=process_id, name=name, on_status=on_status,
            on_error=on_error, sync_provider=sync_provider, connect=connect,
            heartbeat_interval=0, backoff_base=0.05, backoff_cap=0.1,
        )
    return make


def _factory_unreachable():
    def make(*, url, access_key, process_id, name, on_status, on_error=None, sync_provider=None):
        async def connect(_u):
            raise ConnectionError("connection refused")
        return RelayConnector(
            url, access_key, process_id=process_id, name=name, on_status=on_status,
            on_error=on_error, sync_provider=sync_provider, connect=connect,
            heartbeat_interval=0, backoff_base=0.05, backoff_cap=0.1,
        )
    return make


def test_normalize_relay_url():
    assert normalize_relay_url("https://foreman.team.dev") == "wss://foreman.team.dev/relay"
    assert normalize_relay_url("http://host:8787") == "ws://host:8787/relay"
    assert normalize_relay_url("foreman.team.dev") == "wss://foreman.team.dev/relay"
    assert normalize_relay_url("wss://host/relay") == "wss://host/relay"
    assert normalize_relay_url("") == ""


def test_cloud_manager_not_configured():
    mgr = CloudManager(store=FakeStore(), cfg=load_config(), connector_factory=_factory())
    state = mgr.connect(wait=0.2)
    assert state["connected"] is False
    assert state["error"] == "not_configured"


def test_cloud_manager_connect_and_disconnect():
    store = FakeStore()
    store.set_setting("cloud.url", "wss://relay.example/relay")
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory(ack_ok=True))

    state = mgr.connect(wait=3.0)
    assert state["connected"] is True
    assert state["access_key_set"] is True
    assert state["url"] == "wss://relay.example/relay"
    # a stable process id is minted + persisted
    assert store.get_setting("cloud.process_id")

    off = mgr.disconnect()
    assert off["connected"] is False
    # an intentional disconnect is a clean offline state — no leftover error from tearing the loop
    assert off["error"] == ""


def test_cloud_manager_does_not_push_periodic_cache_sync():
    store = FakeStore()
    store.set_setting("cloud.url", "wss://relay.example/relay")
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    conns: list = []
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory(ack_ok=True, conns=conns))
    assert mgr.connect(wait=3.0)["connected"] is True
    time.sleep(0.2)
    mgr.disconnect()
    assert conns and all("cache_sync" not in s for s in conns[0].sent)


async def test_cloud_manager_snapshot_request_is_on_demand():
    cfg = load_config()
    mgr = CloudManager(store=FakeStore(), cfg=cfg)
    reply = await mgr._on_frame(Envelope(kind=KIND_SNAPSHOT_REQ, id="corr"))
    assert reply.kind == KIND_SNAPSHOT
    assert reply.id == "corr"
    assert reply.payload == {"sessions": [], "cards": []}


async def test_cloud_manager_command_disabled_by_default():
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    mgr = CloudManager(store=FakeStore(), cfg=cfg)
    env = Envelope(kind=KIND_COMMAND, id="cmd1", seq=1, nonce="n", ts=1.0)
    attach_mac(env, hashlib.sha256(b"fk_live_test").hexdigest())
    reply = await mgr._on_frame(env)
    assert reply.kind == KIND_ACK
    assert reply.payload == {"ok": False, "error": "disabled"}


async def test_cloud_manager_command_enabled_via_config_kv():
    """Toggling 允许远端执行 (config_kv override) lets a command past the breaker without a restart —
    the gate reads the effective flag per command, so the next frame is no longer 'disabled'."""
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    store = FakeStore()
    store.set_setting("cloud.remote_execution_enabled", "1")
    mgr = CloudManager(store=store, cfg=cfg)
    env = Envelope(kind=KIND_COMMAND, id="cmd-on", seq=1, nonce="n", ts=1.0)
    attach_mac(env, hashlib.sha256(b"fk_live_test").hexdigest())
    reply = await mgr._on_frame(env)
    assert reply.kind == KIND_ACK
    # Past the breaker: no app loop is bound in this unit, so it reaches the runner and reports
    # not_ready — the point is it is NOT 'disabled'.
    assert reply.payload != {"ok": False, "error": "disabled"}
    assert reply.payload == {"ok": False, "error": "not_ready"}


async def test_cloud_manager_replay_check_happens_before_idempotency_cache():
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    key_hash = hashlib.sha256(b"fk_live_test").hexdigest()
    mgr = CloudManager(store=FakeStore(), cfg=cfg)

    first = Envelope(kind=KIND_COMMAND, id="cmd1", seq=2, nonce="n2", ts=2.0)
    attach_mac(first, key_hash)
    assert (await mgr._on_frame(first)).payload == {"ok": False, "error": "disabled"}

    replay = Envelope(kind=KIND_COMMAND, id="cmd1", seq=1, nonce="n1", ts=1.0)
    attach_mac(replay, key_hash)
    assert (await mgr._on_frame(replay)).payload == {"ok": False, "error": "replay"}

    retry = Envelope(kind=KIND_COMMAND, id="cmd1", seq=3, nonce="n3", ts=3.0)
    attach_mac(retry, key_hash)
    assert (await mgr._on_frame(retry)).payload == {"ok": False, "error": "disabled"}


def test_cloud_manager_clearing_config_stops_connection():
    store = FakeStore()
    store.set_setting("cloud.url", "wss://relay.example/relay")
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory(ack_ok=True))
    assert mgr.connect(wait=3.0)["connected"] is True
    # user clears the config, then presses Connect again
    store.set_setting("cloud.url", "")
    cfg.secrets.cloud_access_key = ""
    state = mgr.connect(wait=0.5)
    assert state["connected"] is False
    assert state["error"] == "not_configured"
    # the old connector thread must be gone, not left dialing with stale creds
    assert mgr._thread is None or not mgr._thread.is_alive()


def test_cloud_manager_auth_denied_surfaces_error():
    store = FakeStore()
    store.set_setting("cloud.url", "wss://relay.example/relay")
    cfg = load_config()
    cfg.secrets.cloud_access_key = "bad"
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory(ack_ok=False))
    mgr.connect(wait=1.0)
    # the reconnect loop treats a denied key as fatal → not connected, error recorded
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and mgr.status()["error"] not in ("auth",):
        time.sleep(0.05)
    assert mgr.status()["connected"] is False
    assert mgr.status()["error"] == "auth"
    mgr.disconnect()


def test_cloud_manager_wait_window_does_not_report_false_timeout():
    store = FakeStore()
    store.set_setting("cloud.url", "wss://relay.example/relay")
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory_slow_handshake())
    state = mgr.connect(wait=0.05)
    assert state["connected"] is False
    assert state["error"] == ""
    mgr.disconnect()


def test_cloud_manager_unreachable_surfaces_error():
    store = FakeStore()
    store.set_setting("cloud.url", "wss://relay.example/relay")
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory_unreachable())
    state = mgr.connect(wait=1.0)
    # a relay that actively fails must surface a controlled code, not a raw exception string
    assert state["connected"] is False
    assert state["error"] == "unreachable"
    mgr.disconnect()


def test_cloud_endpoints_save_and_status(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.env_path = str(tmp_path / ".env")
    store = FakeStore()
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory(ack_ok=True))
    c = TestClient(create_app(cfg, store=store, cloud=mgr))

    # initially unconfigured but available (a manager is injected)
    s0 = c.get("/api/settings/cloud").json()
    assert s0["available"] is True and s0["connected"] is False and s0["access_key_set"] is False

    # save url + key
    saved = c.post("/api/settings/cloud", json={"url": "wss://relay.example/relay", "access_key": "fk_live_abc"}).json()
    assert saved["url"] == "wss://relay.example/relay"
    assert saved["access_key_set"] is True
    # the key is persisted to .env, never returned
    assert "access_key" not in saved
    assert "fk_live_abc" in (tmp_path / ".env").read_text(encoding="utf-8")

    # connect → connected
    conn = c.post("/api/settings/cloud/connect").json()
    assert conn["connected"] is True
    # saving new settings while connected reconciles the live link (drops it; no stale connection)
    after_save = c.post("/api/settings/cloud", json={"url": "wss://other.example/relay"}).json()
    assert after_save["connected"] is False
    # disconnect is idempotent → offline
    off = c.post("/api/settings/cloud/disconnect").json()
    assert off["connected"] is False


def test_remote_execution_enabled_helper_override_and_parse():
    """The shared resolver: config_kv override wins over the cfg baseline; truthiness is lenient."""
    from foreman.shared.config import remote_execution_enabled

    s = FakeStore()
    assert remote_execution_enabled(s, default=False) is False
    assert remote_execution_enabled(s, default=True) is True  # no override → baseline
    assert remote_execution_enabled(None, default=True) is True  # no store → baseline
    s.set_setting("cloud.remote_execution_enabled", "1")
    assert remote_execution_enabled(s, default=False) is True
    s.set_setting("cloud.remote_execution_enabled", "0")
    assert remote_execution_enabled(s, default=True) is False  # override wins over a True baseline


def test_cloud_remote_execution_toggle_persists_without_dropping_connection(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.env_path = str(tmp_path / ".env")
    store = FakeStore()
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory(ack_ok=True))
    c = TestClient(create_app(cfg, store=store, cloud=mgr))

    # default OFF, surfaced in status so the UI toggle reflects the gate
    assert c.get("/api/settings/cloud").json()["remote_execution_enabled"] is False

    # configure + connect a live link
    c.post("/api/settings/cloud", json={"url": "wss://relay.example/relay", "access_key": "fk_live_abc"})
    assert c.post("/api/settings/cloud/connect").json()["connected"] is True

    # toggle ON: persists to config_kv, reported effective, and a pure toggle must NOT drop the link
    on = c.post("/api/settings/cloud", json={"remote_execution_enabled": True}).json()
    assert on["remote_execution_enabled"] is True
    assert on["connected"] is True
    assert store.get_setting("cloud.remote_execution_enabled") == "1"

    # toggle OFF likewise persists and keeps the connection
    off = c.post("/api/settings/cloud", json={"remote_execution_enabled": False}).json()
    assert off["remote_execution_enabled"] is False
    assert off["connected"] is True
    assert store.get_setting("cloud.remote_execution_enabled") == "0"


def test_cloud_endpoints_unavailable_without_manager(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    store = FakeStore()
    c = TestClient(create_app(cfg, store=store))  # no cloud manager (e.g. team cache server)
    assert c.get("/api/settings/cloud").json()["available"] is False
    assert c.post("/api/settings/cloud/connect").status_code == 503
