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
from typing import Any

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
    _server: Any          # uvicorn.Server
    _thread: threading.Thread

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def start_local_app(cfg: Config, host: str = "127.0.0.1", port: int = 8788) -> LocalApp:
    """Start the local engine + web server in a background thread; return a LocalApp handle."""
    import uvicorn

    from foreman.server.app import _ensure_safe_exposure, create_app

    # Fail closed: never serve the fully-wired operational API on a public bind without a token
    # (issue #1 P0). Exposing `foreman app` via a tunnel requires setting FOREMAN_AUTH_TOKEN.
    _ensure_safe_exposure(cfg, host=host)
    from foreman.server.push import Pusher
    from foreman.shared.crypto import cipher_from_config
    from foreman.shared.llm import LLMClient

    from .computer_use.toolbelt import Toolbelt
    from .core.auditor import Auditor
    from .core.briefing import BriefingService
    from .core.cards import CardService
    from .core.decision_loop import DecisionLoop
    from .core.definition_service import DefinitionService
    from .core.dispatch_service import DispatchService
    from .core.gate import Gate
    from .core.operator import Operator
    from .monitor.hooks import HookReceiver

    # Optional at-rest encryption for definition bodies (DESIGN §765, T6.2). The key lives in a
    # secret (.env: FOREMAN_DEFINITION_KEY), never config.yaml; empty → bodies stay plaintext.
    cipher = cipher_from_config(cfg.secrets.definition_key)
    store = Store(cfg.store.db_path, cipher=cipher)
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
    # The Decision Loop串联 (P4 acceptance, §6.2): Operator → Auditor → Gate → card → checkpoint →
    # execute. It wires the local Store/Runner/Toolbelt/Gate so a tapped card actually runs the
    # chosen path. Output language follows ui.language (§15). The Operator's "hands" are the
    # Toolbelt (shell/screenshot/mouse/keyboard, §4.7); the Gate classifies its shell commands.
    language = normalize_lang(store.get_setting("ui.language") or cfg.ui.language)
    toolbelt = Toolbelt(gate=gate)
    loop = DecisionLoop(
        store=store,
        gate=gate,
        cards=cards,
        operator=Operator(LLMClient(cfg), language=language),
        auditor=Auditor(LLMClient(cfg), language=language),
        bus=bus,
        runner=runner,
        toolbelt=toolbelt,
        language=language,
        # Config baseline autonomy dial (§6.4): honoured by the loop until a DB override is written,
        # so a config of level 2/3 isn't silently demoted to level 1 at runtime (issue #1 P1).
        autonomy_level=cfg.autonomy.level,
    )
    # Close the loop: a tapped card executes the chosen path (approve→checkpoint+execute / undo /
    # revise) instead of only recording the decision (the "你点→检查点→执行" half, §6.2).
    cards.executor = loop.on_card_decision
    # DispatchService creates Root Sessions from the phone (§5.1); its launcher drives the real
    # Runner so a phone tap actually starts an agent (multiple sessions run concurrently, T1.7).
    async def _launcher(
        session_id: str, goal: str, workspace: str, agent: str, model: str
    ) -> None:
        await runner.launch(agent, goal, Path(workspace), session_id, model=model)

    dispatcher = DispatchService(cfg, store, bus=bus, launcher=_launcher)
    # BriefingService summarizes a session's activity with YOUR LLM → reports table + Web Push
    # (§5.5). Output language follows the runtime ui.language setting (§15, resolved above).
    briefings = BriefingService(
        LLMClient(cfg), store, bus=bus, pusher=pusher, language=language
    )
    # DefinitionService is the UI editor for the four 秘方 blocks (workflow/skill/code_standard/
    # qa_rubric, §11.2). Injected like the Gate/CardService so app.py stays shared-only; definitions
    # live ONLY in the local store and never reach the shared server (§8.3 / §14).
    definitions = DefinitionService(store, bus=bus, cipher=cipher)
    app = create_app(
        cfg, store, bus, hooks=hooks, gate=gate, cards=cards,
        dispatcher=dispatcher, briefings=briefings, definitions=definitions,
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
