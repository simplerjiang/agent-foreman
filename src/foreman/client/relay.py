"""Outbound relay connector: the local process dials the server (DESIGN §8.5).

The PC is behind a firewall, so it always connects **out** to `wss://<domain>/relay`, sends
its access key in the first frame, then keeps the long connection alive (heartbeat / pong)
and **auto-reconnects with exponential backoff** when the line drops (§8.5 ③). On reconnect
it re-registers with the same access key.

Transport-agnostic by design: `RelayConnector` takes a `connect` factory returning a
connection with async `send(str)` / `recv() -> str` / `close()`. The default factory lazily
imports `websockets` (an optional client dep); tests inject a fake. This keeps the module
importable without a websocket lib installed and the reconnect logic unit-testable.

While connected, the connector also runs a periodic client-initiated heartbeat timer (§8.5
③ "两端定时 ping/pong"): every `heartbeat_interval` seconds it sends a ping; the relay refreshes
`last_heartbeat` and replies pong. It replies pong only to a *ping* (never to a pong) so the
two ends don't bounce a heartbeat forever.

The remaining live wiring (the running `foreman` command that owns this loop with a real
access key + wss dial-out to a deployed relay) is the credential-gated team rollout — built
and mock-tested here; the live PC dial-out is hooked up in the live rollout (see TASKS T7.1).
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
        on_status: Callable[[bool], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_interval: float = 30.0,
        heartbeat_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        sync_provider: Callable[[], Envelope | None] | None = None,
        sync_interval: float = 20.0,
        sync_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.url = url
        self.access_key = access_key
        self.process_id = process_id
        self.name = name
        self._connect = connect or _default_connect
        self._on_frame = on_frame
        # Optional liveness callback (True after a successful handshake, False when the session
        # ends) so a supervising CloudManager can surface "connected" in the UI without reaching
        # into the connector's internals. Must never raise.
        self._on_status = on_status
        # Optional error observer: the dial/read errors run() otherwise swallows are reported here
        # so the UI can show "connection failed: ..." instead of an indefinite "connecting".
        self._on_error = on_error
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._clock = clock
        # Periodic client-initiated keep-alive (§8.5 ③). <= 0 disables it. A SEPARATE sleep from
        # the backoff `sleep` so injecting one in a reconnect test doesn't perturb the other.
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_sleep = heartbeat_sleep
        # Optional periodic display-cache push (DESIGN §8.5 ③ / T7.5): while connected, send a
        # KIND_CACHE_SYNC frame (session/card summaries — never diffs/秘方) so the relay can serve
        # the phone view. `sync_provider` returns the frame to send (or None to skip a tick).
        self._sync_provider = sync_provider
        self._sync_interval = sync_interval
        self._sync_sleep = sync_sleep
        self._handshook = False  # set per-session once the relay accepts our key

    def _notify_status(self, connected: bool) -> None:
        """Fire the optional liveness callback, swallowing any error (status reporting must never
        break the connection loop)."""
        if self._on_status is None:
            return
        try:
            self._on_status(connected)
        except Exception:  # noqa: BLE001 — a buggy observer must not kill the relay loop
            pass

    def _notify_error(self, exc: Exception) -> None:
        """Report a dial/read error to the optional observer, swallowing observer errors."""
        if self._on_error is None:
            return
        try:
            self._on_error(exc)
        except Exception:  # noqa: BLE001 — a buggy observer must not kill the relay loop
            pass

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
        """One connected session: handshake, then read frames + send heartbeats until it drops.

        Replies pong to relay pings and sends its own periodic pings (§8.5 ③). Raises
        RelayAuthError if the relay denies the handshake — the caller's reconnect loop treats
        that as fatal (no point retrying a revoked key). Any other read error propagates so the
        loop reconnects with backoff.
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
        self._notify_status(True)

        tasks = [asyncio.create_task(self._read_loop(conn))]
        if self._heartbeat_interval and self._heartbeat_interval > 0:
            tasks.append(asyncio.create_task(self._heartbeat_loop(conn)))
        if self._sync_provider is not None:
            tasks.append(asyncio.create_task(self._sync_loop(conn)))
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Re-raise the first finished task's error (the read loop normally ends with the drop),
        # so run()'s reconnect/backoff still triggers exactly as before.
        for t in done:
            exc = t.exception()
            if exc is not None:
                raise exc

    async def _read_loop(self, conn) -> None:
        """Read frames until the line drops, replying pong to pings (never to pongs — §8.5 ③)."""
        while True:
            env = Envelope.from_json(await conn.recv())
            if env.kind == KIND_HEARTBEAT:
                if not env.payload.get("pong"):  # a ping → pong it; a pong → already alive, drop it
                    await conn.send(
                        Envelope(kind=KIND_HEARTBEAT, payload={"pong": True}).to_json()
                    )
                continue
            if self._on_frame is not None:
                await self._on_frame(env)

    async def _heartbeat_loop(self, conn) -> None:
        """Send a ping every `heartbeat_interval` seconds (§8.5 ③). run_once cancels it on a normal
        disconnect; if a ping itself can't be sent the line is dead, so the error ends the session
        and run()'s backoff reconnects (same path as a read error)."""
        ping = Envelope(kind=KIND_HEARTBEAT, payload={"ping": True}).to_json()
        while True:
            await self._heartbeat_sleep(self._heartbeat_interval)
            await conn.send(ping)

    async def _sync_loop(self, conn) -> None:
        """Push a display-cache snapshot on connect, then every `sync_interval` seconds (§8.5 ③).
        The provider returns the frame (or None to skip); a send failure ends the session and
        run()'s backoff reconnects, same as a heartbeat/read error."""
        while True:
            frame = self._sync_provider() if self._sync_provider is not None else None
            if frame is not None:
                await conn.send(frame.to_json())
            await self._sync_sleep(self._sync_interval)

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
            except Exception as exc:  # noqa: BLE001
                self._notify_error(exc)  # surface the dial/read error; still back off and retry
            finally:
                if self._handshook:
                    self._notify_status(False)
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
