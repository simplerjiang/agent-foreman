"""Tests for the relay link (TASKS T3.2, DESIGN §8.5).

Covers the shared Envelope contract, server-side access-key hashing, the server Relay
(handshake / registry / per-account routing / heartbeat) and the client RelayConnector
(hello frame / pong reply / exponential-backoff reconnect). No network, no real wss — the
wss transport is duck-typed on both ends and faked here (the live dialer is deferred).
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from foreman.client.relay import (
    RelayAuthError,
    RelayConnector,
    backoff_delay,
)
from foreman.server.auth import (
    generate_access_key,
    hash_access_key,
    verify_access_key,
)
from foreman.server.relay import Relay, RelayClient
from foreman.server.store import ServerStore
from foreman.server.store.models import AccessKey, Account
from foreman.shared.events import EventBus
from foreman.shared.protocol import (
    KIND_ACK,
    KIND_COMMAND,
    KIND_HEARTBEAT,
    KIND_HELLO,
    KIND_HELLO_ACK,
    KIND_SUBSCRIBE,
    KIND_UNSUBSCRIBE,
    PROTOCOL_VERSION,
    Envelope,
    attach_mac,
    command_envelope,
    verify_mac,
)


# ── shared: Envelope contract ──────────────────────────────────────────────────────────────────
def test_envelope_roundtrip_json():
    env = Envelope(
        kind="event",
        id="c1",
        account_id="a1",
        payload={"x": 1},
        nonce="n",
        seq=7,
        ts=1.5,
        mac="m",
    )
    back = Envelope.from_json(env.to_json())
    assert back == env
    assert back.version == PROTOCOL_VERSION == 2


def test_envelope_from_dict_is_tolerant():
    # non-dict -> empty envelope, never raises
    assert Envelope.from_dict(None).kind == ""
    assert Envelope.from_dict("nope").kind == ""
    # bad payload type coerced to {}
    e = Envelope.from_dict({"kind": "x", "payload": "oops", "version": "bad"})
    assert e.payload == {} and e.version == PROTOCOL_VERSION
    # malformed json -> empty envelope
    assert Envelope.from_json("{not json").kind == ""


def test_command_envelope_mac_roundtrip():
    env = command_envelope("dispatch", {"goal": "g"}, seq=1, corr_id="cmd")
    attach_mac(env, "key-hash")
    assert env.kind == KIND_COMMAND and env.id == "cmd"
    assert verify_mac(env, "key-hash") is True
    assert verify_mac(env, "wrong") is False


# ── server: access-key hashing (DESIGN §8.4) ─────────────────────────────────────────────────────
def test_access_key_hash_and_verify():
    plain = generate_access_key()
    h = hash_access_key(plain)
    assert h and h != plain and len(h) == 64  # sha256 hex, never the plaintext
    assert verify_access_key(plain, h) is True
    assert verify_access_key("wrong", h) is False
    assert verify_access_key("", h) is False
    assert verify_access_key(plain, "") is False
    assert hash_access_key(plain) == h  # deterministic


# ── server: Relay handshake/auth (DESIGN §8.5 ①) ─────────────────────────────────────────────────
def _seed(
    tmp_path, *, key_status="active", acct_status="active", expires_at="", plain="sim-card",
    name="srv.db",
):
    st = ServerStore(str(tmp_path / name))
    st.init()
    st.add_account(Account(id="a1", username="alice", status=acct_status))
    st.add_access_key(
        AccessKey(
            id="k1", account_id="a1", key_hash=hash_access_key(plain),
            status=key_status, expires_at=expires_at,
        )
    )
    return st, plain


def test_authenticate_ok(tmp_path):
    st, plain = _seed(tmp_path)
    auth = Relay(st).authenticate({"access_key": plain})
    assert auth.ok and auth.account_id == "a1" and auth.key_id == "k1"


def test_authenticate_rejects_bad_inputs(tmp_path):
    st, plain = _seed(tmp_path)
    relay = Relay(st)
    assert relay.authenticate({}).reason == "missing access key"
    assert relay.authenticate({"access_key": "nope"}).reason == "unknown access key"


def test_authenticate_rejects_revoked_and_disabled(tmp_path):
    st_rev, p1 = _seed(tmp_path, key_status="revoked", name="rev.db")
    assert Relay(st_rev).authenticate({"access_key": p1}).reason == "revoked access key"

    st_dis, p2 = _seed(tmp_path, acct_status="disabled", name="dis.db")
    assert Relay(st_dis).authenticate({"access_key": p2}).reason == "account disabled"


def test_authenticate_rejects_expired(tmp_path):
    st, plain = _seed(tmp_path, expires_at="2000-01-01T00:00:00Z")
    relay = Relay(st, now=lambda: "2026-06-20T00:00:00Z")
    assert relay.authenticate({"access_key": plain}).reason == "expired access key"
    # still valid before expiry
    relay_ok = Relay(st, now=lambda: "1999-01-01T00:00:00Z")
    assert relay_ok.authenticate({"access_key": plain}).ok


# ── server: registry + routing (DESIGN §8.5 ② / §7.2) ────────────────────────────────────────────
class _SendOnlyWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, d: dict) -> None:
        self.sent.append(d)


def test_register_marks_online_and_unregister_offline(tmp_path):
    st, _ = _seed(tmp_path)
    relay = Relay(st, now=lambda: "2026-06-20T00:00:00Z")
    client = RelayClient(account_id="a1", process_id="p1", key_id="k1", name="box", ws=_SendOnlyWS())

    relay.register(client)
    assert {p.id for p in st.get_online_processes("a1")} == {"p1"}
    assert relay.clients_for("a1") == [client]
    assert st.get_access_keys("a1")[0].last_seen_at == "2026-06-20T00:00:00Z"

    relay.unregister(client)
    assert st.get_online_processes("a1") == []
    assert relay.clients_for("a1") == []  # connection dropped from the in-memory registry


async def test_route_only_to_matching_account_and_process(tmp_path):
    st, _ = _seed(tmp_path)
    st.add_account(Account(id="a2", username="bob"))
    relay = Relay(st)
    c1 = RelayClient(account_id="a1", process_id="p1", key_id="k1", name="", ws=_SendOnlyWS())
    c2 = RelayClient(account_id="a1", process_id="p2", key_id="k1", name="", ws=_SendOnlyWS())
    c3 = RelayClient(account_id="a2", process_id="p3", key_id="k2", name="", ws=_SendOnlyWS())
    for c in (c1, c2, c3):
        relay.conns.setdefault(c.account_id, []).append(c)

    env = Envelope(kind="command", payload={"do": "x"})
    assert await relay.route("a1", env) == 2  # both of a1's machines
    assert c1.ws.sent and c2.ws.sent and not c3.ws.sent
    assert await relay.route("a1", env, process_id="p2") == 1  # narrowed to one machine
    assert await relay.route("a1", env, process_id="ghost") == 0  # nobody -> caller uses cache
    assert await relay.route("a2", env) == 1


async def test_route_with_ack_resolves_same_id(tmp_path):
    st, _ = _seed(tmp_path)
    relay = Relay(st, ack_timeout=1.0)
    ws = _SendOnlyWS()
    client = RelayClient(
        account_id="a1",
        process_id="p1",
        key_id="k1",
        key_hash=hash_access_key("sim-card"),
        name="",
        ws=ws,
    )
    relay.conns.setdefault("a1", []).append(client)
    env = command_envelope("dispatch", {"goal": "g"}, corr_id="cmd1")

    task = asyncio.create_task(relay.route_with_ack("a1", env, process_id="p1"))
    await asyncio.sleep(0)
    sent = Envelope.from_dict(ws.sent[-1])
    assert sent.id == "cmd1" and sent.kind == KIND_COMMAND
    await relay._on_frame(
        client, Envelope(kind=KIND_ACK, id="cmd1", payload={"ok": True, "session_id": "s"}).to_dict()
    )
    assert await task == {"ok": True, "session_id": "s", "kind": KIND_ACK, "id": "cmd1", "process_id": "p1"}


async def test_presence_subscribe_unsubscribe_edges(tmp_path):
    st, _ = _seed(tmp_path)
    relay = Relay(st)
    ws = _SendOnlyWS()
    client = RelayClient(account_id="a1", process_id="p1", key_id="k1", name="", ws=ws)
    relay.conns.setdefault("a1", []).append(client)

    assert await relay.subscribe("a1") == 1
    assert Envelope.from_dict(ws.sent[-1]).kind == KIND_SUBSCRIBE
    assert await relay.subscribe("a1") == 2
    assert [Envelope.from_dict(row).kind for row in ws.sent].count(KIND_SUBSCRIBE) == 1
    assert await relay.unsubscribe("a1") == 1
    assert [Envelope.from_dict(row).kind for row in ws.sent].count(KIND_UNSUBSCRIBE) == 0
    assert await relay.unsubscribe("a1") == 0
    assert Envelope.from_dict(ws.sent[-1]).kind == KIND_UNSUBSCRIBE


# ── server: full connection lifecycle via a fake WS ──────────────────────────────────────────────
class _FakeServerWS:
    """Duck-types FastAPI's WebSocket: yields queued inbound frames then a disconnect."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.accepted = False
        self.closed: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        if self._frames:
            return self._frames.pop(0)
        raise WebSocketDisconnect(code=1000)

    async def send_json(self, d: dict) -> None:
        self.sent.append(d)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def test_serve_happy_path_handshake_heartbeat_disconnect(tmp_path):
    st, plain = _seed(tmp_path)
    bus = EventBus()
    q = bus.subscribe_queue()
    relay = Relay(st, bus, now=lambda: "2026-06-20T00:00:00Z")
    ws = _FakeServerWS(
        [
            Envelope(kind=KIND_HELLO, payload={"access_key": plain, "process_id": "p1", "name": "box"}).to_dict(),
            Envelope(kind=KIND_HEARTBEAT).to_dict(),
        ]
    )

    await relay.serve(ws)

    assert ws.accepted
    # hello_ack(ok) then a heartbeat pong. process_id is SERVER-derived from the key (k1),
    # NOT the "p1" the client suggested — see the cross-tenant-hijack guard.
    assert ws.sent[0]["kind"] == KIND_HELLO_ACK and ws.sent[0]["payload"]["ok"] is True
    assert ws.sent[0]["payload"]["process_id"] == "k1"
    assert ws.sent[1]["kind"] == KIND_HEARTBEAT and ws.sent[1]["payload"]["pong"] is True
    # process went online then offline across the session
    assert st.get_online_processes("a1") == []
    assert relay.clients_for("a1") == []
    # health events published on connect and on drop
    health = [q.get_nowait() for _ in range(q.qsize())]
    assert [e.payload["online"] for e in health] == [True, False]
    assert all(e.type == "health" and e.payload["account_id"] == "a1" for e in health)


async def test_serve_denied_handshake_closes_and_registers_nothing(tmp_path):
    st, _ = _seed(tmp_path)
    relay = Relay(st)
    ws = _FakeServerWS([Envelope(kind=KIND_HELLO, payload={"access_key": "bogus"}).to_dict()])

    await relay.serve(ws)

    assert ws.sent[0]["kind"] == KIND_HELLO_ACK and ws.sent[0]["payload"]["ok"] is False
    assert ws.closed == 1008
    assert st.get_online_processes() == []  # nothing registered for a failed handshake


async def test_serve_handles_disconnect_before_hello(tmp_path):
    st, _ = _seed(tmp_path)
    relay = Relay(st)
    ws = _FakeServerWS([])  # disconnects before sending hello
    await relay.serve(ws)  # must not raise
    assert ws.sent == [] and st.get_online_processes() == []


# ── client: connector (DESIGN §8.5 ① / ③) ────────────────────────────────────────────────────────
def test_backoff_delay_grows_and_caps():
    assert backoff_delay(0, base=1.0, cap=60.0) == 1.0
    assert backoff_delay(1, base=1.0, cap=60.0) == 2.0
    assert backoff_delay(3, base=1.0, cap=60.0) == 8.0
    assert backoff_delay(100, base=1.0, cap=60.0) == 60.0  # capped
    assert backoff_delay(-5, base=1.0, cap=60.0) == 1.0  # clamps negative attempts


def test_hello_frame_carries_key_and_identity():
    conn = RelayConnector("wss://x/relay", "sim-card", process_id="p1", name="box")
    hello = conn.hello()
    assert hello.kind == KIND_HELLO
    assert hello.payload == {"access_key": "sim-card", "process_id": "p1", "name": "box"}


class _FakeClientConn:
    """Duck-types the client transport: send(str)/recv()->str/close()."""

    def __init__(self, incoming: list[str]) -> None:
        self._incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        if self._incoming:
            return self._incoming.pop(0)
        raise ConnectionError("closed")

    async def close(self) -> None:
        self.closed = True


async def test_run_once_handshakes_and_pongs():
    got: list[Envelope] = []
    conn = RelayConnector(
        "wss://x/relay", "sim", process_id="p1",
        on_frame=lambda e: got.append(e) or _noop(),
    )
    incoming = [
        Envelope(kind=KIND_HELLO_ACK, payload={"ok": True, "process_id": "p1"}).to_json(),
        Envelope(kind=KIND_HEARTBEAT).to_json(),
        Envelope(kind="command", payload={"do": "x"}).to_json(),
    ]
    fc = _FakeClientConn(incoming)
    with pytest.raises(ConnectionError):  # recv exhausts -> session ends
        await conn.run_once(fc)

    sent = [Envelope.from_json(s) for s in fc.sent]
    assert sent[0].kind == KIND_HELLO and sent[0].payload["access_key"] == "sim"
    assert sent[1].kind == KIND_HEARTBEAT and sent[1].payload["pong"] is True  # replied pong
    assert conn._handshook is True
    assert [e.kind for e in got] == ["command"]  # non-heartbeat frames go to on_frame


async def _noop() -> None:
    return None


async def test_run_once_raises_on_denied_handshake():
    conn = RelayConnector("wss://x/relay", "sim", process_id="p1")
    fc = _FakeClientConn(
        [Envelope(kind=KIND_HELLO_ACK, payload={"ok": False, "reason": "revoked access key"}).to_json()]
    )
    with pytest.raises(RelayAuthError, match="revoked"):
        await conn.run_once(fc)


async def test_run_reconnects_with_backoff_then_stops():
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    attempts: list[int] = []

    async def connect(url: str):
        attempts.append(1)
        return _FakeClientConn([])  # recv raises immediately -> run_once fails fast

    conn = RelayConnector(
        "wss://x/relay", "sim", process_id="p1", connect=connect, sleep=fake_sleep,
    )
    await conn.run(max_attempts=3)
    assert len(attempts) == 3  # tried, retried, retried then stopped
    assert sleeps == [1.0, 2.0]  # exponential backoff between attempts


async def test_run_resets_backoff_after_a_real_session():
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    ticks = iter(float(t) for t in range(0, 1000, 2))  # each clock() advances 2s -> session dur 2s

    def clock() -> float:
        return next(ticks)

    # 1st connect fails to even produce a conn; 2nd gets a real (handshook) session that drops.
    seq = [
        None,
        _FakeClientConn([Envelope(kind=KIND_HELLO_ACK, payload={"ok": True}).to_json()]),
    ]

    async def connect(url: str):
        item = seq.pop(0)
        if item is None:
            raise ConnectionError("dial failed")
        return item

    conn = RelayConnector(
        "wss://x/relay", "sim", process_id="p1", connect=connect, sleep=fake_sleep, clock=clock,
    )
    await conn.run(max_attempts=3)
    # loop1 fail -> backoff(0)=1; loop2 handshook + lasted 2s (>= base) -> reset -> backoff(0)=1
    assert sleeps == [1.0, 1.0]


async def test_run_does_not_reset_backoff_on_instant_flap():
    """A relay that authenticates then INSTANTLY drops must still back off, not hammer at 1s."""
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    async def connect(url: str):
        # handshakes ok, then recv() raises immediately -> zero-duration session
        return _FakeClientConn([Envelope(kind=KIND_HELLO_ACK, payload={"ok": True}).to_json()])

    conn = RelayConnector(
        "wss://x/relay", "sim", process_id="p1", connect=connect, sleep=fake_sleep,
        clock=lambda: 0.0,  # session duration always 0 < backoff_base -> no reset
    )
    await conn.run(max_attempts=3)
    assert sleeps == [1.0, 2.0]  # backoff still escalates despite repeated handshakes


async def test_run_stops_on_auth_error():
    attempts: list[int] = []

    async def connect(url: str):
        attempts.append(1)
        return _FakeClientConn(
            [Envelope(kind=KIND_HELLO_ACK, payload={"ok": False, "reason": "unknown access key"}).to_json()]
        )

    conn = RelayConnector("wss://x/relay", "sim", process_id="p1", connect=connect)
    with pytest.raises(RelayAuthError):
        await conn.run(max_attempts=5)
    assert len(attempts) == 1  # fatal: did not retry a bad key


# ── app wiring: the /relay endpoint (sync TestClient — drives a real starlette WS) ────────────────
def _app(tmp_path, relay=None):
    from foreman.server.app import create_app
    from foreman.shared.config import load_config

    return create_app(load_config(tmp_path / "none.yaml"), relay=relay)


def test_relay_endpoint_closes_without_relay(tmp_path):
    from fastapi.testclient import TestClient

    client = TestClient(_app(tmp_path))  # personal mode: no relay injected
    with pytest.raises(WebSocketDisconnect):  # accepted then closed (1008)
        with client.websocket_connect("/relay") as ws:
            ws.receive_text()


def test_relay_endpoint_delegates_to_injected_relay(tmp_path):
    from fastapi.testclient import TestClient

    calls: list[int] = []

    class FakeRelay:
        async def serve(self, ws) -> None:
            calls.append(1)
            await ws.accept()
            await ws.close()

    client = TestClient(_app(tmp_path, relay=FakeRelay()))
    try:
        with client.websocket_connect("/relay"):
            pass
    except WebSocketDisconnect:
        pass
    assert calls == [1]


# ── heartbeat: ping/pong keep-alive without a bounce (DESIGN §8.5 ③, T7.1) ────────────────────────
async def test_read_loop_pongs_a_ping_but_stays_quiet_for_a_pong():
    """A relay *ping* gets a pong; a relay *pong* is terminal — replying would bounce forever."""
    conn = _FakeClientConn(
        [
            Envelope(kind=KIND_HEARTBEAT, payload={"ping": True}).to_json(),  # relay ping -> we pong
            Envelope(kind=KIND_HEARTBEAT, payload={"pong": True}).to_json(),  # relay pong -> quiet
        ]
    )
    rc = RelayConnector("wss://x/relay", "sim", process_id="p1")
    with pytest.raises(ConnectionError):  # recv exhausts -> session ends
        await rc._read_loop(conn)
    replies = [Envelope.from_json(s) for s in conn.sent]
    assert len(replies) == 1  # exactly one pong — the bare pong was NOT echoed
    assert replies[0].kind == KIND_HEARTBEAT and replies[0].payload.get("pong") is True


async def test_run_once_sends_periodic_heartbeats():
    """run_once runs a client-initiated heartbeat timer alongside the read loop (§8.5 ③)."""
    done = asyncio.Event()
    fired = {"n": 0}

    async def hb_sleep(_interval: float) -> None:
        fired["n"] += 1
        if fired["n"] >= 3:  # after two pings, unblock the read loop and stop pinging
            done.set()
            await asyncio.Event().wait()  # block this task forever (run_once will cancel it)

    class _BlockingConn:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self._first = True

        async def send(self, data: str) -> None:
            self.sent.append(data)

        async def recv(self) -> str:
            if self._first:  # the handshake ack
                self._first = False
                return Envelope(kind=KIND_HELLO_ACK, payload={"ok": True}).to_json()
            await done.wait()  # keep the session alive until the heartbeats have fired
            raise ConnectionError("closed")

        async def close(self) -> None:
            pass

    conn = _BlockingConn()
    rc = RelayConnector(
        "wss://x/relay", "sim", process_id="p1",
        heartbeat_interval=0.01, heartbeat_sleep=hb_sleep,
    )
    with pytest.raises(ConnectionError):
        await rc.run_once(conn)

    sent = [Envelope.from_json(s) for s in conn.sent]
    pings = [e for e in sent if e.kind == KIND_HEARTBEAT and e.payload.get("ping")]
    assert len(pings) == 2  # the heartbeat timer fired periodically while connected


async def test_server_on_frame_pongs_a_ping_and_drops_a_pong(tmp_path):
    """Server mirror of the no-bounce rule: refresh liveness on any heartbeat, reply only to a ping."""
    st, _ = _seed(tmp_path)
    relay = Relay(st, now=lambda: "2026-06-20T00:00:00Z")
    ws = _SendOnlyWS()
    client = RelayClient(account_id="a1", process_id="p1", key_id="k1", name="", ws=ws)
    relay.register(client)

    # a pong from the local process: liveness refreshed, but NO reply (would bounce, §8.5 ③)
    await relay._on_frame(client, Envelope(kind=KIND_HEARTBEAT, payload={"pong": True}).to_dict())
    assert ws.sent == []
    assert st.get_online_processes("a1")  # still online (heartbeat refreshed)

    # a ping from the local process: replied with a pong
    await relay._on_frame(client, Envelope(kind=KIND_HEARTBEAT, payload={"ping": True}).to_dict())
    assert len(ws.sent) == 1 and ws.sent[0]["payload"]["pong"] is True


# ── serve wiring: foreman serve assembles personal vs team mode (DESIGN §8.5, T7.1) ───────────────
def test_build_serve_app_personal_mode_has_no_relay_or_auth(tmp_path):
    from fastapi.testclient import TestClient

    from foreman.server.app import build_serve_app
    from foreman.shared.config import load_config

    app = build_serve_app(load_config(tmp_path / "none.yaml"))  # default mode = personal
    assert app.state.relay is None and app.state.auth is None
    assert TestClient(app).get("/health").status_code == 200


def test_build_serve_app_team_mode_wires_relay_and_auth(tmp_path):
    from fastapi.testclient import TestClient

    from foreman.server.app import build_serve_app
    from foreman.shared.config import load_config

    cfg = load_config(tmp_path / "none.yaml")
    cfg.server.mode = "team"
    cfg.server.db_path = str(tmp_path / "srv.db")
    app = build_serve_app(cfg)

    assert app.state.relay is not None  # relay 总机 wired (DESIGN §8.5)
    assert app.state.auth is not None  # user login + key mgmt wired (§8.2)
    assert app.state.cache is None  # no relay-side display cache; browser/local process own state
    assert app.state.store is None  # no client-style local store on the relay box
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    # In team mode every operational endpoint requires a valid account token at the guard, so an
    # unauthenticated request is blocked (401) before reaching the handler (issue #1 P0 / codex).
    assert c.get("/api/sessions").status_code == 401
    assert c.get("/api/notifications").status_code == 401  # account-scoped → 401 without token
    # auth IS configured, so bad creds are a 401 (not the 503 "auth not configured" of personal mode)
    assert c.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 401
    # WITH a valid token, the personal session endpoint still 503s — the relay box has no local store
    app.state.auth.create_account("boss", "password1", role="admin")
    tok = c.post("/api/auth/login", json={"username": "boss", "password": "password1"}).json()[
        "token"
    ]
    assert c.get("/api/sessions", headers={"Authorization": f"Bearer {tok}"}).status_code == 503
    assert c.get("/api/cache/sessions", headers={"Authorization": f"Bearer {tok}"}).status_code == 404


def test_team_relay_multiple_processes_dial_in_routed_by_account(tmp_path):
    """End-to-end through the REAL /relay endpoint: several local processes dial in and the
    registry separates them BY ACCOUNT (DESIGN §8.5 ① outbound接入 + ② 按账号路由)."""
    from fastapi.testclient import TestClient

    from foreman.server.app import create_app
    from foreman.shared.config import load_config

    st = ServerStore(str(tmp_path / "team.db"))
    st.init()
    st.add_account(Account(id="a1", username="alice"))
    st.add_account(Account(id="a2", username="bob"))
    st.add_access_key(AccessKey(id="k1", account_id="a1", key_hash=hash_access_key("alice-1")))
    st.add_access_key(AccessKey(id="k2", account_id="a1", key_hash=hash_access_key("alice-2")))
    st.add_access_key(AccessKey(id="k3", account_id="a2", key_hash=hash_access_key("bob-1")))
    relay = Relay(st)
    client = TestClient(create_app(load_config(tmp_path / "none.yaml"), relay=relay))

    def _hello(plain: str, name: str) -> dict:
        return Envelope(kind=KIND_HELLO, payload={"access_key": plain, "name": name}).to_dict()

    # Two of alice's machines + one of bob's, all dialing the same relay concurrently.
    with client.websocket_connect("/relay") as w1:
        w1.send_json(_hello("alice-1", "box1"))
        assert w1.receive_json()["payload"]["ok"] is True
        with client.websocket_connect("/relay") as w2:
            w2.send_json(_hello("alice-2", "box2"))
            assert w2.receive_json()["payload"]["ok"] is True
            with client.websocket_connect("/relay") as w3:
                w3.send_json(_hello("bob-1", "bbox"))
                assert w3.receive_json()["payload"]["ok"] is True

                # process_id is server-derived from the key → k1/k2 are alice's, k3 is bob's.
                assert {p.id for p in st.get_online_processes("a1")} == {"k1", "k2"}
                assert {p.id for p in st.get_online_processes("a2")} == {"k3"}
                assert {c.process_id for c in relay.clients_for("a1")} == {"k1", "k2"}
                assert {c.process_id for c in relay.clients_for("a2")} == {"k3"}
                # a1's traffic never reaches a2's machine and vice-versa
                assert relay.clients_for("a1", process_id="k3") == []
