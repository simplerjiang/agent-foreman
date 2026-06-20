"""Outbound relay connector: the local process dials the server (DESIGN §8.5).

The PC is behind a firewall, so it always connects **out** to `wss://<domain>/relay`, sends
its access key in the first frame, then keeps the long connection alive (heartbeat / pong)
and **auto-reconnects with exponential backoff** when the line drops (§8.5 ③). On reconnect
it re-registers with the same access key.

Transport-agnostic by design: `RelayConnector` takes a `connect` factory returning a
connection with async `send(str)` / `recv() -> str` / `close()`. The default factory lazily
imports `websockets` (an optional client dep); tests inject a fake. This keeps the module
importable without a websocket lib installed and the reconnect logic unit-testable.

Live wiring (a long-running `foreman` command that owns this loop, plus a periodic
client-initiated heartbeat timer) is deferred to the P4 decision loop / live rollout — this
task delivers the connector + handshake + pong + backoff, all mock-tested.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from foreman.shared.protocol import (
    KIND_HEARTBEAT,
    KIND_HELLO,
    KIND_HELLO_ACK,
    Envelope,
)


class RelayAuthError(Exception):
    """The relay rejected our access key (revoked / unknown / expired). Don't retry blindly."""


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff for reconnects (§8.5 ③): base * 2**attempt, capped. attempt is 0-based."""
    if attempt < 0:
        attempt = 0
    return min(cap, base * (2.0**attempt))


class RelayConnector:
    def __init__(
        self,
        url: str,
        access_key: str,
        *,
        process_id: str,
        name: str = "",
        connect: Callable[[str], Awaitable[object]] | None = None,
        on_frame: Callable[[Envelope], Awaitable[None]] | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.access_key = access_key
        self.process_id = process_id
        self.name = name
        self._connect = connect or _default_connect
        self._on_frame = on_frame
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._clock = clock
        self._handshook = False  # set per-session once the relay accepts our key

    def hello(self) -> Envelope:
        """The first frame: access key (inside TLS, never bare) + machine identity (§8.5 ①)."""
        return Envelope(
            kind=KIND_HELLO,
            payload={
                "access_key": self.access_key,
                "process_id": self.process_id,
                "name": self.name,
            },
        )

    async def run_once(self, conn) -> None:
        """One connected session: handshake, then read frames until the line drops.

        Replies pong to relay heartbeats (§8.5 ③). Raises RelayAuthError if the relay denies
        the handshake — the caller's reconnect loop treats that as fatal (no point retrying a
        revoked key). Any other read error propagates so the loop reconnects with backoff.
        """
        await conn.send(self.hello().to_json())
        ack = Envelope.from_json(await conn.recv())
        if ack.kind != KIND_HELLO_ACK:
            # Protocol drift / transient garbage first frame — reconnect (NOT a fatal auth error).
            raise ConnectionError("unexpected first frame; expected hello_ack")
        if ack.payload.get("ok") is not True:
            # Explicit denial (revoked/unknown/expired) — fatal; retrying a bad key is pointless.
            raise RelayAuthError(str(ack.payload.get("reason") or "handshake denied"))
        self._handshook = True
        while True:
            env = Envelope.from_json(await conn.recv())
            if env.kind == KIND_HEARTBEAT:
                await conn.send(Envelope(kind=KIND_HEARTBEAT, payload={"pong": True}).to_json())
                continue
            if self._on_frame is not None:
                await self._on_frame(env)

    async def run(self, *, max_attempts: int | None = None) -> None:
        """Keep a connection up forever, reconnecting with exponential backoff (§8.5 ③).

        A successful session resets the backoff. RelayAuthError stops the loop (fatal —
        the key needs fixing). `max_attempts` bounds reconnect tries (None = unbounded;
        tests pass a small number so the loop terminates).
        """
        attempt = 0
        tries = 0
        while True:
            conn = None
            self._handshook = False
            started = self._clock()
            try:
                conn = await self._connect(self.url)
                await self.run_once(conn)
            except RelayAuthError:
                raise  # key needs fixing — retrying a revoked/unknown key is pointless
            except Exception:
                pass  # transport/connect/read error -> back off and retry below
            finally:
                if conn is not None:
                    await _safe_close(conn)
            tries += 1
            if max_attempts is not None and tries >= max_attempts:
                return
            # Reset backoff only after a session that BOTH authenticated AND lasted at least one
            # backoff interval — so a relay that accepts-then-instantly-drops still backs off
            # instead of being hammered once a second forever.
            if self._handshook and (self._clock() - started) >= self._backoff_base:
                attempt = 0
            await self._sleep(backoff_delay(attempt, base=self._backoff_base, cap=self._backoff_cap))
            attempt += 1


async def _safe_close(conn) -> None:
    try:
        close = getattr(conn, "close", None)
        if close is not None:
            await close()
    except Exception:
        pass


async def _default_connect(url: str):
    """Default transport: lazily import `websockets` (optional client dep) and adapt it to the
    send(str)/recv()->str/close() shape RelayConnector expects."""
    import websockets  # noqa: PLC0415  (lazy: keeps this module importable without the dep)

    raw = await websockets.connect(url)
    return _WebsocketsConn(raw)


class _WebsocketsConn:
    """Adapter over a `websockets` connection -> the send/recv/close shape we use."""

    def __init__(self, raw) -> None:
        self._raw = raw

    async def send(self, data: str) -> None:
        await self._raw.send(data)

    async def recv(self) -> str:
        return await self._raw.recv()

    async def close(self) -> None:
        await self._raw.close()
