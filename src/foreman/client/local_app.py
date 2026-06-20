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
from pathlib import Path

from foreman.shared.config import Config
from foreman.shared.events import EventBus
from foreman.shared.i18n import normalize as normalize_lang

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
    from foreman.server.push import Pusher
    from foreman.shared.llm import LLMClient

    from .core.briefing import BriefingService
    from .core.cards import CardService
    from .core.dispatch_service import DispatchService
    from .core.gate import Gate
    from .monitor.hooks import HookReceiver

    store = Store(cfg.store.db_path)
    store.init()
    bus = EventBus()
    runner = Runner(cfg, bus, store)
    pusher = Pusher(cfg)
    # One Gate, shared between the hook receiver (which holds dangerous tool calls) and the
    # approval API (which closes the loop). It pushes cards via the Pusher; both /hooks and the
    # Gate are local-only (DESIGN §4.3 / §6.6). local_app is the wiring seam that may touch both
    # client + server — the Gate itself never imports server (it gets the Pusher injected, §14).
    gate = Gate(cfg.gates, store=store, bus=bus, pusher=pusher)
    hooks = HookReceiver(store, bus, gate)
    # CardService owns the decision_cards table + the step-detail drill-down (raw return +
    # per-line diff, §6.3). Injected like the Gate so app.py stays shared-only; the diff/raw
    # output it assembles never leaves the local process (§8.3 / §14).
    cards = CardService(store, bus=bus)
    # DispatchService creates Root Sessions from the phone (§5.1); its launcher drives the real
    # Runner so a phone tap actually starts an agent (multiple sessions run concurrently, T1.7).
    async def _launcher(session_id: str, goal: str, workspace: str, agent: str) -> None:
        await runner.launch(agent, goal, Path(workspace), session_id)

    dispatcher = DispatchService(cfg, store, bus=bus, launcher=_launcher)
    # BriefingService summarizes a session's activity with YOUR LLM → reports table + Web Push
    # (§5.5). Output language follows the runtime ui.language setting (§15). Injected like the Gate.
    language = normalize_lang(store.get_setting("ui.language") or cfg.ui.language)
    briefings = BriefingService(
        LLMClient(cfg), store, bus=bus, pusher=pusher, language=language
    )
    app = create_app(
        cfg, store, bus, hooks=hooks, gate=gate, cards=cards,
        dispatcher=dispatcher, briefings=briefings,
    )

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
