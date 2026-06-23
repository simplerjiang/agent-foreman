"""Cloud relay connection (DESIGN §8.5) — the Settings → 云端连接 feature.

Covers the CloudManager lifecycle against a fake relay (connect → connected, auth-deny → error,
disconnect → offline) and the /api/settings/cloud endpoints (save url + key, connect/disconnect,
key never returned, unavailable when no manager is injected).
"""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from foreman.client.core.cloud import CloudManager, normalize_relay_url
from foreman.client.relay import RelayConnector
from foreman.server.app import create_app
from foreman.shared.config import load_config
from foreman.shared.protocol import KIND_HELLO_ACK, Envelope


class FakeStore:
    def __init__(self) -> None:
        self._kv: dict[str, str] = {}

    def get_setting(self, key: str):
        return self._kv.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self._kv[key] = value


class _FakeConn:
    """A relay connection that handshakes OK (or denies) then holds the line open."""

    def __init__(self, *, ack_ok: bool = True) -> None:
        self._ack_ok = ack_ok
        self._acked = False
        self._closed = asyncio.Event()

    async def send(self, data: str) -> None:  # noqa: D401
        return None

    async def recv(self) -> str:
        if not self._acked:
            self._acked = True
            return Envelope(kind=KIND_HELLO_ACK, payload={"ok": self._ack_ok, "process_id": "p"}).to_json()
        await self._closed.wait()
        raise ConnectionError("closed")

    async def close(self) -> None:
        self._closed.set()


def _factory(ack_ok: bool = True):
    def make(*, url, access_key, process_id, name, on_status, on_error=None):
        async def connect(_u):
            return _FakeConn(ack_ok=ack_ok)
        return RelayConnector(
            url, access_key, process_id=process_id, name=name, on_status=on_status,
            on_error=on_error, connect=connect, heartbeat_interval=0, backoff_base=0.05,
        )
    return make


def _factory_unreachable():
    def make(*, url, access_key, process_id, name, on_status, on_error=None):
        async def connect(_u):
            raise ConnectionError("connection refused")
        return RelayConnector(
            url, access_key, process_id=process_id, name=name, on_status=on_status,
            on_error=on_error, connect=connect, heartbeat_interval=0,
            backoff_base=0.05, backoff_cap=0.1,
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


def test_cloud_manager_unreachable_surfaces_error():
    store = FakeStore()
    store.set_setting("cloud.url", "wss://relay.example/relay")
    cfg = load_config()
    cfg.secrets.cloud_access_key = "fk_live_test"
    mgr = CloudManager(store=store, cfg=cfg, connector_factory=_factory_unreachable())
    state = mgr.connect(wait=1.0)
    # a relay that never answers must not show an indefinite "connecting": an error is surfaced
    assert state["connected"] is False
    assert state["error"]  # non-empty (the dial error, or the connect-wait "timeout")
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
    # disconnect → offline
    off = c.post("/api/settings/cloud/disconnect").json()
    assert off["connected"] is False


def test_cloud_endpoints_unavailable_without_manager(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    store = FakeStore()
    c = TestClient(create_app(cfg, store=store))  # no cloud manager (e.g. team cache server)
    assert c.get("/api/settings/cloud").json()["available"] is False
    assert c.post("/api/settings/cloud/connect").status_code == 503
