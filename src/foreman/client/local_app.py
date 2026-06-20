"""`foreman app` core — the PC app: engine + an embedded local web server.

`start_local_app()` builds the engine (store / bus / runner) and serves the local UI on
127.0.0.1 in a background thread; the native window (pywebview) is opened by __main__.app_cmd.
open = online / close = offline (DESIGN §3.1 / §4.6). This module has no GUI dependency, so it
is unit-testable headlessly; pywebview/pystray are imported only in the CLI command.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from foreman.shared.config import Config
from foreman.shared.events import EventBus

from .agents.runner import Runner
from .store import Store


@dataclass
class LocalApp:
    """A running local app: the engine + a background web server. Call stop() to go offline."""

    url: str
    store: Store
    bus: EventBus
    runner: Runner
    _server: object       # uvicorn.Server
    _thread: threading.Thread

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def start_local_app(cfg: Config, host: str = "127.0.0.1", port: int = 8788) -> LocalApp:
    """Start the local engine + web server in a background thread; return a LocalApp handle."""
    import uvicorn

    from foreman.server.app import create_app

    from .core.gate import Gate
    from .monitor.hooks import HookReceiver

    store = Store(cfg.store.db_path)
    store.init()
    bus = EventBus()
    runner = Runner(cfg, bus, store)
    hooks = HookReceiver(store, bus, Gate(cfg.gates))  # /hooks is local-only (DESIGN §4.3)
    app = create_app(cfg, store, bus, hooks=hooks)

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_until_started(server)
    return LocalApp(
        url=f"http://{host}:{port}/",
        store=store,
        bus=bus,
        runner=runner,
        _server=server,
        _thread=thread,
    )


def _wait_until_started(server, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not getattr(server, "started", False):
        if time.monotonic() > deadline:
            raise RuntimeError("local server did not start in time")
        time.sleep(0.05)
