"""Cloud connection manager — drives the outbound relay dial-out for the local app.

The Settings → 云端连接 card lets the user link this machine to the team relay 总机 so the
phone can watch progress and approve from afar (DESIGN §8.5). The relay never stores the user's
code or LLM keys; it only routes. This manager owns the lifecycle of a `RelayConnector` running
in a background thread with its own event loop:

  configure(url, key) → persist → connect() starts the reconnect loop, disconnect() stops it.

`status()` reports {url, access_key_set, connected, error} for the UI. The actual access key is
read from `cfg.secrets.cloud_access_key` (stored in local .env, never returned) and the relay URL
from the local store (`cloud.url`). Connecting is opt-in (a button), never automatic, so the app
never dials out on its own.

The transport is injectable (`connector_factory`) so this is unit-testable with a fake relay —
the live wss dial-out uses the default `websockets` client (an optional dep, lazily imported).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import queue
import threading
import uuid
from typing import Any, Callable

from foreman.client.relay import RelayAuthError, RelayConnector
from foreman.shared.config import remote_execution_enabled
from foreman.shared.events import AgentEvent
from foreman.shared.protocol import (
    KIND_ACK,
    KIND_COMMAND,
    KIND_EVENT,
    KIND_NOTIFY,
    KIND_SNAPSHOT_REQ,
    KIND_SUBSCRIBE,
    KIND_UNSUBSCRIBE,
    Envelope,
    new_id,
    verify_mac,
)


def normalize_relay_url(url: str) -> str:
    """Coerce a user-entered cloud address into the wss relay endpoint the connector expects.

    The Settings field is friendly (users paste ``foreman.team.dev`` or an ``https://`` origin),
    but ``websockets.connect`` needs ``wss://<host>/relay`` (DESIGN §8.5). Map http→ws / https→wss,
    assume wss:// when no scheme is given, and append ``/relay`` when no path is present. An empty
    string stays empty."""
    from urllib.parse import urlparse

    u = (url or "").strip()
    if not u:
        return u
    if u.startswith("http://"):
        u = "ws://" + u[len("http://"):]
    elif u.startswith("https://"):
        u = "wss://" + u[len("https://"):]
    elif not (u.startswith("ws://") or u.startswith("wss://")):
        u = "wss://" + u
    parsed = urlparse(u)
    if not parsed.path or parsed.path == "/":
        u = u.rstrip("/") + "/relay"
    return u


class CloudManager:
    def __init__(
        self,
        *,
        store: Any,
        cfg: Any,
        name: str = "",
        bus: Any = None,
        dispatcher: Any = None,
        cards: Any = None,
        gate: Any = None,
        connector_factory: Callable[..., RelayConnector] | None = None,
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._name = name or "foreman"
        self._bus = bus
        self._dispatcher = dispatcher
        self._cards = cards
        self._gate = gate
        self._connector_factory = connector_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app_loop: asyncio.AbstractEventLoop | None = None
        self._event_task: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._connected = False
        self._error = ""
        self._want = False
        self._connected_event = threading.Event()
        self._subscribed = False
        self._last_seq = 0
        self._ack_cache: dict[str, dict] = {}
        self._outgoing: queue.Queue[Envelope] = queue.Queue(maxsize=1000)

    # ── config ────────────────────────────────────────────────────────────────
    def _url(self) -> str:
        if self._store is not None and hasattr(self._store, "get_setting"):
            return (self._store.get_setting("cloud.url") or "").strip()
        return ""

    def _key(self) -> str:
        return (getattr(self._cfg.secrets, "cloud_access_key", "") or "").strip()

    def _process_id(self) -> str:
        """A stable per-machine id (the relay derives its own from the key, but the hello frame
        carries one for logging). Persisted so reconnects keep the same identity."""
        if self._store is not None and hasattr(self._store, "get_setting"):
            current = (self._store.get_setting("cloud.process_id") or "").strip()
            if current:
                return current
            new_id = uuid.uuid4().hex[:16]
            if hasattr(self._store, "set_setting"):
                self._store.set_setting("cloud.process_id", new_id)
            return new_id
        return "local"

    def configured(self) -> bool:
        return bool(self._url() and self._key())

    def status(self) -> dict:
        return {
            "url": self._url(),
            "access_key_set": bool(self._key()),
            "connected": self._connected,
            "error": self._error,
            "available": True,
        }

    # ── lifecycle ──────────────────────────────────────────────────────────────
    def _on_status(self, connected: bool) -> None:
        self._connected = connected
        if connected:
            self._error = ""
            self._connected_event.set()

    def _on_error(self, exc: Exception) -> None:
        self._error = self._error_code(exc)

    def _error_code(self, exc: Exception) -> str:
        detail = str(exc or "").lower()
        if isinstance(exc, TimeoutError) or "timeout" in detail or "timed out" in detail:
            return "timeout"
        return "unreachable"

    def bind_app_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the uvicorn loop after startup. Constructor time is too early for this."""
        self._app_loop = loop
        if self._bus is not None and self._event_task is None:
            self._event_task = loop.create_task(self._event_pump())

    def _build_snapshot(self, corr_id: str = "") -> Envelope:
        """Build an on-demand display-safe snapshot. Errors become an empty snapshot."""
        store = self._store
        if store is None or not hasattr(store, "get_sessions") or not hasattr(store, "get_decision_cards"):
            return Envelope(kind=KIND_ACK, id=corr_id, payload={"ok": False, "error": "no_store"})
        try:
            from foreman.client.cache_sync import build_snapshot

            return build_snapshot(store.get_sessions(), store.get_decision_cards(None), corr_id=corr_id)
        except Exception:  # noqa: BLE001 — a snapshot failure must not kill the relay link
            return Envelope(kind=KIND_ACK, id=corr_id, payload={"ok": False, "error": "snapshot_failed"})

    def _build_connector(self, url: str, key: str) -> RelayConnector:
        process_id = self._process_id()
        kwargs = {
            "url": url,
            "access_key": key,
            "process_id": process_id,
            "name": self._name,
            "on_status": self._on_status,
            "on_error": self._on_error,
            "on_frame": self._on_frame,
            "outgoing": self._outgoing,
            "sync_provider": None,
        }
        if self._connector_factory is not None:
            params = inspect.signature(self._connector_factory).parameters
            accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            return self._connector_factory(
                **(kwargs if accepts_kwargs else {k: v for k, v in kwargs.items() if k in params})
            )
        return RelayConnector(
            url,
            key,
            process_id=process_id,
            name=self._name,
            on_status=self._on_status,
            on_error=self._on_error,
            on_frame=self._on_frame,
            outgoing=self._outgoing,
        )

    async def _on_frame(self, env: Envelope) -> Envelope | list[Envelope] | None:
        if env.kind == KIND_SUBSCRIBE:
            self._subscribed = True
            return None
        if env.kind == KIND_UNSUBSCRIBE:
            self._subscribed = False
            return None
        if env.kind == KIND_SNAPSHOT_REQ:
            return self._build_snapshot(env.id)
        if env.kind != KIND_COMMAND:
            return None
        if not verify_mac(env, self._key_hash()):
            return self._plain_ack(env, ok=False, error="bad_mac")
        if env.seq <= self._last_seq:
            return self._plain_ack(env, ok=False, error="replay")
        self._last_seq = env.seq
        cached = self._ack_cache.get(env.id)
        if cached is not None:
            return Envelope(kind=KIND_ACK, id=env.id, payload=cached)
        # Effective breaker: config_kv override (toggled live from 本机 Settings → 云端连接) wins over
        # the cfg baseline. Read per command so the owner can grant/revoke remote control instantly.
        cfg_default = bool(getattr(getattr(self._cfg, "server", None), "remote_execution_enabled", False))
        if not remote_execution_enabled(self._store, cfg_default):
            return self._ack(env, ok=False, error="disabled")
        try:
            result = await self._run_remote_command(env.payload)
            return self._ack(env, **result)
        except Exception as exc:  # noqa: BLE001
            return self._ack(env, ok=False, error=str(exc)[:160] or "remote_command_failed")

    def _ack(self, env: Envelope, **payload) -> Envelope:
        data = {"ok": bool(payload.pop("ok", True)), **payload}
        self._ack_cache[env.id] = data
        if len(self._ack_cache) > 512:
            for key in list(self._ack_cache)[:128]:
                self._ack_cache.pop(key, None)
        return Envelope(kind=KIND_ACK, id=env.id, payload=data)

    def _plain_ack(self, env: Envelope, **payload) -> Envelope:
        data = {"ok": bool(payload.pop("ok", True)), **payload}
        return Envelope(kind=KIND_ACK, id=env.id, payload=data)

    def _key_hash(self) -> str:
        return hashlib.sha256(self._key().encode("utf-8")).hexdigest()

    async def _run_remote_command(self, payload: dict) -> dict:
        action = str(payload.get("action") or "")
        if self._app_loop is None:
            return {"ok": False, "error": "not_ready"}
        if action == "dispatch":
            if self._dispatcher is None or not hasattr(self._dispatcher, "create"):
                return {"ok": False, "error": "no_dispatcher"}
            coro = self._dispatcher.create(
                str(payload.get("goal") or ""),
                workspace=str(payload.get("workspace") or "") or None,
                agent=str(payload.get("agent") or "") or None,
                model=str(payload.get("model") or "") or None,
                effort=str(payload.get("effort") or "") or None,
                session_id=str(payload.get("session_id") or "") or None,
                source="phone",
            )
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(coro, self._app_loop)
            )
        if action == "card_choice":
            if self._cards is None or not hasattr(self._cards, "record_choice"):
                return {"ok": False, "error": "no_card_service"}
            coro = self._cards.record_choice(
                str(payload.get("card_id") or ""),
                str(payload.get("option") or ""),
            )
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(coro, self._app_loop)
            )
        if action == "approval":
            if self._gate is None or not hasattr(self._gate, "resolve"):
                return {"ok": False, "error": "no_gate"}
            coro = self._gate.resolve(
                str(payload.get("approval_id") or ""),
                str(payload.get("decision") or ""),
                nonce=str(payload.get("nonce") or ""),
                reason=str(payload.get("reason") or ""),
            )
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(coro, self._app_loop)
            )
        return {"ok": False, "error": "bad_action"}

    async def _event_pump(self) -> None:
        q = self._bus.subscribe_queue()
        try:
            while True:
                ev = await q.get()
                if self._subscribed:
                    self._enqueue_frame(self._event_frame(ev))
                else:
                    notify = self._notify_frame(ev)
                    if notify is not None:
                        self._enqueue_frame(notify)
        finally:
            self._bus.unsubscribe(q)

    def _event_frame(self, ev: AgentEvent) -> Envelope:
        return Envelope(
            kind=KIND_EVENT,
            id=ev.id or new_id(),
            payload={
                "id": ev.id or None,
                "session_id": ev.session_id,
                "task_id": ev.task_id,
                "type": ev.type,
                "source": ev.source,
                "payload": ev.payload,
                "ts": ev.ts,
            },
        )

    def _notify_frame(self, ev: AgentEvent) -> Envelope | None:
        if ev.type in {"approval_req", "action_proposed"}:
            ref = str(ev.payload.get("approval_id") or ev.payload.get("card_id") or ev.id or "")
            if not ref:
                return None
            return Envelope(
                kind=KIND_NOTIFY,
                id=new_id(),
                payload={
                    "kind": "decision_needed",
                    "ref": ref,
                    "title": str(ev.payload.get("summary") or "决策待处理")[:200],
                    "dedup_key": f"decision:{ref}",
                },
            )
        if ev.type in {"stop", "error"} and ev.session_id:
            title = "任务失败" if ev.type == "error" else "任务完成"
            return Envelope(
                kind=KIND_NOTIFY,
                id=new_id(),
                payload={
                    "kind": "result_ready",
                    "ref": ev.session_id,
                    "title": title,
                    "dedup_key": f"result:{ev.session_id}",
                },
            )
        return None

    def _enqueue_frame(self, env: Envelope) -> None:
        try:
            self._outgoing.put_nowait(env)
        except queue.Full:
            pass

    def connect(self, *, wait: float = 3.0) -> dict:
        """Start (or restart) the reconnect loop in a background thread. Blocks up to `wait`
        seconds for the first successful handshake so the UI gets an immediate verdict; the loop
        keeps retrying in the background regardless. Returns status()."""
        with self._lock:
            if not self.configured():
                # Tear down any live connection first — otherwise clearing the URL/key then pressing
                # Connect would leave the old connector dialing with stale credentials while the UI
                # reports "not configured" (codex review finding).
                self._stop_locked()
                self._connected = False
                self._error = "not_configured"
                return self.status()
            self._stop_locked()
            self._want = True
            self._error = ""
            self._connected_event.clear()
            url, key = normalize_relay_url(self._url()), self._key()

            def _run() -> None:
                loop = asyncio.new_event_loop()
                self._loop = loop
                asyncio.set_event_loop(loop)
                connector = self._build_connector(url, key)
                task = loop.create_task(connector.run())
                self._task = task
                try:
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass  # intentional disconnect — a clean stop, not an error
                except RelayAuthError:
                    self._connected = False
                    self._error = "auth"
                except Exception as exc:  # noqa: BLE001 — surface, don't crash the thread
                    self._connected = False
                    self._error = str(exc)[:160]
                finally:
                    self._connected = False
                    # Drain any child tasks (read/heartbeat loops) so the loop closes without a
                    # "Task was destroyed but it is pending" warning.
                    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    try:
                        loop.close()
                    except Exception:  # noqa: BLE001
                        pass

            thread = threading.Thread(target=_run, name="foreman-cloud", daemon=True)
            self._thread = thread
            thread.start()

        self._connected_event.wait(timeout=max(0.0, wait))
        # If the handshake is still in progress and no real error arrived, leave error empty so the
        # UI can keep showing "connecting" instead of racing a false timeout before auth completes.
        return self.status()

    def disconnect(self) -> dict:
        with self._lock:
            self._want = False
            self._stop_locked()
            self._connected = False
            self._error = ""  # an intentional disconnect is a clean offline state, not an error
        return self.status()

    def _stop_locked(self) -> None:
        loop, thread, task = self._loop, self._thread, self._task
        self._loop = None
        self._thread = None
        self._task = None
        # Cancel the connector task (not loop.stop): cancellation unwinds run_until_complete and
        # the child tasks cleanly, instead of aborting the future mid-flight.
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except Exception:  # noqa: BLE001 — loop may already be closed
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
