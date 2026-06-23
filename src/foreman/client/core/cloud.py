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
import threading
import uuid
from typing import Any, Callable

from foreman.client.relay import RelayAuthError, RelayConnector


class CloudManager:
    def __init__(
        self,
        *,
        store: Any,
        cfg: Any,
        name: str = "",
        connector_factory: Callable[..., RelayConnector] | None = None,
    ) -> None:
        self._store = store
        self._cfg = cfg
        self._name = name or "foreman"
        self._connector_factory = connector_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._error = ""
        self._want = False
        self._connected_event = threading.Event()

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

    def _build_connector(self, url: str, key: str) -> RelayConnector:
        if self._connector_factory is not None:
            return self._connector_factory(
                url=url, access_key=key, process_id=self._process_id(),
                name=self._name, on_status=self._on_status,
            )
        return RelayConnector(
            url, key, process_id=self._process_id(), name=self._name, on_status=self._on_status,
        )

    def connect(self, *, wait: float = 3.0) -> dict:
        """Start (or restart) the reconnect loop in a background thread. Blocks up to `wait`
        seconds for the first successful handshake so the UI gets an immediate verdict; the loop
        keeps retrying in the background regardless. Returns status()."""
        with self._lock:
            if not self.configured():
                self._connected = False
                self._error = "not_configured"
                return self.status()
            self._stop_locked()
            self._want = True
            self._error = ""
            self._connected_event.clear()
            url, key = self._url(), self._key()

            def _run() -> None:
                loop = asyncio.new_event_loop()
                self._loop = loop
                asyncio.set_event_loop(loop)
                connector = self._build_connector(url, key)
                try:
                    loop.run_until_complete(connector.run())
                except RelayAuthError:
                    self._connected = False
                    self._error = "auth"
                except Exception as exc:  # noqa: BLE001 — surface, don't crash the thread
                    self._connected = False
                    self._error = str(exc)[:160]
                finally:
                    self._connected = False
                    try:
                        loop.close()
                    except Exception:  # noqa: BLE001
                        pass

            thread = threading.Thread(target=_run, name="foreman-cloud", daemon=True)
            self._thread = thread
            thread.start()

        self._connected_event.wait(timeout=max(0.0, wait))
        return self.status()

    def disconnect(self) -> dict:
        with self._lock:
            self._want = False
            self._stop_locked()
            self._connected = False
        return self.status()

    def _stop_locked(self) -> None:
        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001 — loop may already be closed
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
