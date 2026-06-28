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

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from starlette.websockets import WebSocketDisconnect

from foreman.shared.events import EventBus, make_event, utc_now_iso
from foreman.shared.protocol import (
    KIND_ACK,
    KIND_COMMAND,
    KIND_EVENT,
    KIND_HEARTBEAT,
    KIND_HELLO_ACK,
    KIND_NOTIFY,
    KIND_SNAPSHOT,
    KIND_SNAPSHOT_REQ,
    KIND_SUBSCRIBE,
    KIND_UNSUBSCRIBE,
    Envelope,
    attach_mac,
)
from foreman.shared.ratelimit import SlidingWindowLimiter

from .auth import hash_access_key
from .store.models import Notification, ProcessRegistry

COMMAND_ACK_TIMEOUT_SECONDS = 10.0
NOTIFICATION_LIMIT_PER_ACCOUNT = 200
NOTIFY_DECISION_TTL_DAYS = 7
NOTIFY_RESULT_TTL_HOURS = 24


@dataclass
class AuthResult:
    """Outcome of the access-key handshake (DESIGN §8.5 ①)."""

    ok: bool
    account_id: str = ""
    key_id: str = ""
    key_hash: str = ""
    reason: str = ""


@dataclass
class RelayClient:
    """One live outbound connection from a local process, after a successful handshake."""

    account_id: str
    process_id: str
    key_id: str
    name: str
    ws: Any  # duck-typed: has async send_json(dict)
    key_hash: str = ""
    extra: dict = field(default_factory=dict)

    async def send(self, env: Envelope) -> None:
        await self.ws.send_json(env.to_dict())


class Relay:
    """Routes PWA traffic to the right local process by account (DESIGN §8.5 ②).

    `store` is a ServerStore (accounts / access_keys / process_registry). `bus` (optional)
    receives `health` events when a process connects/drops, so the rest of the server can
    react. `now` is injectable for deterministic tests.
    """

    def __init__(
        self,
        store,
        bus: EventBus | None = None,
        *,
        now=utc_now_iso,
        pusher=None,
        ack_timeout: float = COMMAND_ACK_TIMEOUT_SECONDS,
    ) -> None:
        self.store = store
        self.bus = bus
        self._now = now
        self.pusher = pusher
        self.ack_timeout = ack_timeout
        # account_id -> live connections (a person may run several machines — §8.2).
        self.conns: dict[str, list[RelayClient]] = {}
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._offline_queue: dict[tuple[str, str], list[tuple[Envelope, asyncio.Future[dict], float]]] = {}
        self._subscribers: dict[str, int] = {}
        self._seq: dict[str, int] = {}
        self._command_limiter = SlidingWindowLimiter(120, 60.0)
        self._notify_limiter = SlidingWindowLimiter(120, 60.0)

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
        return AuthResult(ok=True, account_id=row.account_id, key_id=row.id, key_hash=row.key_hash)

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
        """Drop the connection and mark the process offline."""
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
        (0 = nobody online)."""
        targets = self.clients_for(account_id, process_id)
        for client in targets:
            await self._send_to_client(client, env)
        return len(targets)

    async def route_with_ack(
        self,
        account_id: str,
        env: Envelope,
        *,
        process_id: str,
        timeout: float | None = None,
    ) -> dict:
        """Route a single-process request and wait for a same-id ACK/SNAPSHOT.

        If the machine is briefly reconnecting, queue the frame until the timeout expires. This is
        the short command delivery buffer, not the long-lived notification queue.
        """
        if not process_id:
            return {"ok": False, "error": "process_required"}
        if not env.id:
            return {"ok": False, "error": "missing_id"}
        if env.kind == KIND_COMMAND and not self._command_limiter.allow(f"{account_id}:{process_id}"):
            return {"ok": False, "error": "rate_limited"}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[env.id] = fut
        deadline = time.monotonic() + (timeout if timeout is not None else self.ack_timeout)
        sent = await self._try_route_pending(account_id, env, process_id, fut, deadline)
        if sent == 0:
            self._offline_queue.setdefault((account_id, process_id), []).append((env, fut, deadline))
        try:
            return await asyncio.wait_for(
                fut, timeout=max(0.01, deadline - time.monotonic())
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": "machine_offline"}
        finally:
            self._pending.pop(env.id, None)
            self._drop_queued(env.id)

    async def subscribe(self, account_id: str) -> int:
        """Mark one browser subscriber present for an account and notify local processes."""
        before = self._subscribers.get(account_id, 0)
        self._subscribers[account_id] = before + 1
        if before == 0:
            await self.route(account_id, Envelope(kind=KIND_SUBSCRIBE))
        return self._subscribers[account_id]

    async def unsubscribe(self, account_id: str) -> int:
        """Drop one browser subscriber and quiet local processes on the 1->0 edge."""
        before = self._subscribers.get(account_id, 0)
        after = max(0, before - 1)
        if after:
            self._subscribers[account_id] = after
        else:
            self._subscribers.pop(account_id, None)
            if before:
                await self.route(account_id, Envelope(kind=KIND_UNSUBSCRIBE))
        return after

    async def _try_route_pending(
        self,
        account_id: str,
        env: Envelope,
        process_id: str,
        fut: asyncio.Future[dict],
        deadline: float,
    ) -> int:
        if fut.done() or time.monotonic() > deadline:
            return 0
        targets = self.clients_for(account_id, process_id)
        for client in targets:
            await self._send_to_client(client, env)
        return len(targets)

    def _drop_queued(self, env_id: str) -> None:
        for key in list(self._offline_queue):
            rows = [row for row in self._offline_queue[key] if row[0].id != env_id]
            if rows:
                self._offline_queue[key] = rows
            else:
                self._offline_queue.pop(key, None)

    async def _flush_offline_queue(self, client: RelayClient) -> None:
        key = (client.account_id, client.process_id)
        rows = self._offline_queue.pop(key, [])
        keep: list[tuple[Envelope, asyncio.Future[dict], float]] = []
        for env, fut, deadline in rows:
            if fut.done() or time.monotonic() > deadline:
                if not fut.done():
                    fut.set_result({"ok": False, "error": "machine_offline"})
                continue
            await self._send_to_client(client, env)
            keep.append((env, fut, deadline))
        if keep:
            self._offline_queue[key] = keep

    async def _send_to_client(self, client: RelayClient, env: Envelope) -> None:
        out = Envelope.from_dict(env.to_dict())
        out.account_id = client.account_id
        if out.kind in {KIND_COMMAND, KIND_SNAPSHOT_REQ}:
            cid = f"{client.account_id}:{client.process_id}"
            self._seq[cid] = self._seq.get(cid, 0) + 1
            out.seq = self._seq[cid]
            if not out.nonce:
                from foreman.shared.protocol import new_nonce

                out.nonce = new_nonce()
            if not out.ts:
                out.ts = time.time()
            if out.kind == KIND_COMMAND:
                attach_mac(out, client.key_hash)
        await client.send(out)

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
            account_id=auth.account_id,
            process_id=process_id,
            key_id=auth.key_id,
            key_hash=auth.key_hash,
            name=name,
            ws=ws,
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
            if self._subscribers.get(client.account_id, 0) > 0:
                await self._send_to_client(client, Envelope(kind=KIND_SUBSCRIBE))
            await self._flush_offline_queue(client)
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
        """Handle one inbound frame from a local process."""
        env = Envelope.from_dict(msg)
        if env.kind == KIND_HEARTBEAT:
            # Any heartbeat (ping or pong) proves the process is alive → refresh last_heartbeat.
            self.store.set_process_online(client.process_id, True, self._now())
            # Reply pong ONLY to a ping (a bare heartbeat). A heartbeat carrying pong=True is the
            # peer's reply to OUR ping — replying again would bounce forever (§8.5 ③ ping/pong).
            if not env.payload.get("pong"):
                await client.send(Envelope(kind=KIND_HEARTBEAT, payload={"pong": True}))
            return
        if env.kind == KIND_ACK:
            self._resolve_pending(env, client)
            await self._publish_relay_frame(client, env)
            return
        if env.kind == KIND_SNAPSHOT:
            self._resolve_pending(env, client)
            await self._publish_relay_frame(client, env)
            return
        if env.kind == KIND_EVENT:
            await self._publish_relay_frame(client, env)
            return
        if env.kind == KIND_NOTIFY:
            await self._handle_notify(client, env)
            return
        # Retired v1 display-cache frames and unknown future kinds are ignored fail-closed.

    def _resolve_pending(self, env: Envelope, client: RelayClient) -> None:
        if not env.id:
            return
        fut = self._pending.get(env.id)
        if fut is not None and not fut.done():
            payload = dict(env.payload)
            payload.setdefault("ok", env.kind != KIND_ACK or env.payload.get("ok", True))
            payload["kind"] = env.kind
            payload["id"] = env.id
            payload["process_id"] = client.process_id
            fut.set_result(payload)

    async def _publish_relay_frame(self, client: RelayClient, env: Envelope) -> None:
        if self.bus is None:
            return
        frame = env.to_dict()
        frame["account_id"] = client.account_id
        frame["process_id"] = client.process_id
        await self.bus.publish(
            make_event(
                "relay_frame",
                source="relay",
                session_id=str(env.payload.get("session_id") or ""),
                payload={
                    "account_id": client.account_id,
                    "process_id": client.process_id,
                    "frame": frame,
                },
            )
        )

    async def _handle_notify(self, client: RelayClient, env: Envelope) -> None:
        bucket = f"{client.account_id}:{client.process_id}"
        if not self._notify_limiter.allow(bucket):
            return
        payload = env.payload if isinstance(env.payload, dict) else {}
        kind = str(payload.get("kind") or "")
        ref = str(payload.get("ref") or "")
        if kind not in {"decision_needed", "result_ready"} or not ref:
            return
        title = str(payload.get("title") or kind)[:200]
        dedup_key = str(payload.get("dedup_key") or f"{client.account_id}:{client.process_id}:{ref}")
        ttl = (
            timedelta(days=NOTIFY_DECISION_TTL_DAYS)
            if kind == "decision_needed"
            else timedelta(hours=NOTIFY_RESULT_TTL_HOURS)
        )
        now_dt = datetime.now(timezone.utc)
        row = Notification(
            id=env.id or f"{client.process_id}:{ref}:{int(now_dt.timestamp())}",
            account_id=client.account_id,
            process_id=client.process_id,
            kind=kind,
            ref=ref,
            title=title,
            dedup_key=dedup_key,
            created_at=now_dt.isoformat(),
            expires_at=(now_dt + ttl).isoformat(),
        )
        if hasattr(self.store, "upsert_notification"):
            self.store.upsert_notification(row, per_account_limit=NOTIFICATION_LIMIT_PER_ACCOUNT)
        await self._push_notification(client.account_id, row)

    async def _push_notification(self, account_id: str, row: Notification) -> None:
        if self.pusher is None or not hasattr(self.store, "get_push_subscriptions"):
            return
        subs = self.store.get_push_subscriptions(account_id)
        gone = await self.pusher.send_to_all(
            subs,
            "Foreman",
            row.title,
            {
                "kind": row.kind,
                "ref": row.ref,
                "process_id": row.process_id,
                "url": self._notification_url(row),
            },
        )
        for endpoint in gone:
            self.store.delete_push_subscription(endpoint, account_id=account_id)

    @staticmethod
    def _notification_url(row: Notification) -> str:
        params = {"view": "decisions", "process": row.process_id}
        if row.kind == "result_ready":
            params = {"view": "workspace", "process": row.process_id, "session": row.ref}
        return "/?" + urlencode(params)

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
