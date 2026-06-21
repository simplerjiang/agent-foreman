"""The relay (总机): server side of the local-process <-> server wss link (DESIGN §8.5).

A local process is behind a router/firewall, so the connection is always **outbound**: the
process dials `wss://<domain>/relay`, sends its access key in the first frame, and the relay
(this module) authenticates it, marks it online in `process_registry`, and keeps the long
connection so a PWA request can be **routed by account** to the right machine.

The relay holds NO 秘方 / diffs / raw output / LLM keys — it only forwards control signals
(DESIGN §8.3/§8.4). It speaks `foreman.shared.protocol.Envelope` frames so the client and
server evolve against one shared contract.

Testability: `serve()` is a thin shell over duck-typed `accept/receive_json/send_json/close`
(FastAPI's WebSocket satisfies it; tests inject a fake). The real logic — `authenticate`,
`register`/`unregister`, `route` — is plain and unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from starlette.websockets import WebSocketDisconnect

from foreman.shared.events import EventBus, make_event, utc_now_iso
from foreman.shared.protocol import (
    KIND_HEARTBEAT,
    KIND_HELLO_ACK,
    Envelope,
)

from .auth import hash_access_key
from .store.models import ProcessRegistry


@dataclass
class AuthResult:
    """Outcome of the access-key handshake (DESIGN §8.5 ①)."""

    ok: bool
    account_id: str = ""
    key_id: str = ""
    reason: str = ""


@dataclass
class RelayClient:
    """One live outbound connection from a local process, after a successful handshake."""

    account_id: str
    process_id: str
    key_id: str
    name: str
    ws: object  # duck-typed: has async send_json(dict)
    extra: dict = field(default_factory=dict)

    async def send(self, env: Envelope) -> None:
        await self.ws.send_json(env.to_dict())


class Relay:
    """Routes PWA traffic to the right local process by account (DESIGN §8.5 ②).

    `store` is a ServerStore (accounts / access_keys / process_registry). `bus` (optional)
    receives `health` events when a process connects/drops, so the rest of the server can
    react. `now` is injectable for deterministic tests.
    """

    def __init__(self, store, bus: EventBus | None = None, *, now=utc_now_iso) -> None:
        self.store = store
        self.bus = bus
        self._now = now
        # account_id -> live connections (a person may run several machines — §8.2).
        self.conns: dict[str, list[RelayClient]] = {}

    # ── handshake (DESIGN §8.5 ①) ────────────────────────────────────────────────────────────
    def authenticate(self, payload: dict) -> AuthResult:
        """Verify the hello frame's access key by hash; resolve the owning account.

        Rejects: missing/unknown key, revoked key, expired key, disabled/missing account.
        Never trusts an account_id sent by the client — it's derived from the key (§8.4).
        """
        key_plain = (payload or {}).get("access_key") or ""
        if not key_plain:
            return AuthResult(ok=False, reason="missing access key")
        row = self.store.get_access_key_by_hash(hash_access_key(key_plain))
        if row is None:
            return AuthResult(ok=False, reason="unknown access key")
        if row.status != "active":
            return AuthResult(ok=False, reason="revoked access key")
        if row.expires_at and row.expires_at <= self._now():  # ISO8601 UTC -> lexical compare
            return AuthResult(ok=False, reason="expired access key")
        account = self.store.get_account(row.account_id)
        if account is None or account.status != "active":
            return AuthResult(ok=False, reason="account disabled")
        return AuthResult(ok=True, account_id=row.account_id, key_id=row.id)

    # ── registry (DESIGN §8.5 ① / §7.2) ──────────────────────────────────────────────────────
    def register(self, client: RelayClient) -> None:
        """Track the connection in-memory and flip the process online in the registry."""
        self.conns.setdefault(client.account_id, []).append(client)
        self.store.register_process(
            ProcessRegistry(
                id=client.process_id,
                account_id=client.account_id,
                access_key_id=client.key_id,
                name=client.name,
                online=True,
                last_heartbeat=self._now(),
            )
        )
        self.store.touch_access_key(client.key_id, self._now())

    def unregister(self, client: RelayClient) -> None:
        """Drop the connection and mark the process offline (PWA then falls back to the
        server display cache until it reconnects — §8.3/§8.5 ③)."""
        live = self.conns.get(client.account_id)
        if live and client in live:
            live.remove(client)
        if live is not None and not live:
            self.conns.pop(client.account_id, None)
        self.store.set_process_online(client.process_id, False, self._now())

    # ── routing (DESIGN §8.5 ②) ──────────────────────────────────────────────────────────────
    def clients_for(self, account_id: str, process_id: str | None = None) -> list[RelayClient]:
        """Live connections for an account, optionally narrowed to one machine."""
        live = list(self.conns.get(account_id, []))
        if process_id is not None:
            live = [c for c in live if c.process_id == process_id]
        return live

    async def route(
        self, account_id: str, env: Envelope, *, process_id: str | None = None
    ) -> int:
        """Forward a frame to an account's local process(es). Returns how many got it
        (0 = nobody online → caller should serve the display cache, §8.3)."""
        targets = self.clients_for(account_id, process_id)
        for client in targets:
            await client.send(env)
        return len(targets)

    # ── connection lifecycle (the thin wss shell) ────────────────────────────────────────────
    async def serve(self, ws) -> None:
        """Accept one outbound connection: handshake → register → pump → cleanup."""
        await ws.accept()
        try:
            hello = await ws.receive_json()
        except Exception:  # disconnect/parse error before handshake — nothing registered yet
            return
        env = Envelope.from_dict(hello)
        auth = self.authenticate(env.payload)
        if not auth.ok:
            await self._safe_send(ws, Envelope(kind=KIND_HELLO_ACK, payload={"ok": False, "reason": auth.reason}))
            await self._safe_close(ws)
            return

        # process_id is SERVER-derived from the key — NEVER taken from the client frame. The key
        # is one-machine-per-key (§8.2) and bound to an account, so this id can't collide with /
        # overwrite another account's registry row (would otherwise be a cross-tenant hijack). The
        # client may only suggest a display `name`.
        process_id = auth.key_id
        name = str(env.payload.get("name") or "")
        client = RelayClient(
            account_id=auth.account_id, process_id=process_id, key_id=auth.key_id, name=name, ws=ws
        )
        self.register(client)
        try:
            await self._publish_health(client, online=True)
            await self._safe_send(
                ws,
                Envelope(
                    kind=KIND_HELLO_ACK,
                    account_id=auth.account_id,
                    payload={"ok": True, "process_id": process_id},
                ),
            )
            while True:
                msg = await ws.receive_json()
                await self._on_frame(client, msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            # Always pairs with register() above, so a crash never leaves a process online forever.
            self.unregister(client)
            await self._publish_health(client, online=False)

    async def _on_frame(self, client: RelayClient, msg: object) -> None:
        """Handle one inbound frame from a local process. Today: heartbeat keep-alive.

        Event/command/card forwarding to the PWA is layered on in P4 (decision loop) — the
        relay already routes frames either way (`route`), only the higher-level wiring is later.
        """
        env = Envelope.from_dict(msg)
        if env.kind == KIND_HEARTBEAT:
            # Any heartbeat (ping or pong) proves the process is alive → refresh last_heartbeat.
            self.store.set_process_online(client.process_id, True, self._now())
            # Reply pong ONLY to a ping (a bare heartbeat). A heartbeat carrying pong=True is the
            # peer's reply to OUR ping — replying again would bounce forever (§8.5 ③ ping/pong).
            if not env.payload.get("pong"):
                await client.send(Envelope(kind=KIND_HEARTBEAT, payload={"pong": True}))

    async def _publish_health(self, client: RelayClient, *, online: bool) -> None:
        if self.bus is None:
            return
        await self.bus.publish(
            make_event(
                "health",
                source="relay",
                session_id="",
                payload={
                    "account_id": client.account_id,
                    "process_id": client.process_id,
                    "online": online,
                },
            )
        )

    @staticmethod
    async def _safe_send(ws, env: Envelope) -> None:
        try:
            await ws.send_json(env.to_dict())
        except Exception:
            pass

    @staticmethod
    async def _safe_close(ws) -> None:
        try:
            await ws.close(code=1008)  # policy violation: failed handshake
        except Exception:
            pass
