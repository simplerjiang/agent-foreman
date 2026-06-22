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


class PortInUseError(RuntimeError):
    """The configured local port is already bound — usually another Foreman instance is already
    running, or another program holds the port. Raised *before* serving so the caller can react
    (open the existing window / pick another --port) instead of crashing deep inside the server
    thread with an opaque "did not start in time" timeout."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(
            f"port {port} on {host} is already in use — Foreman may already be running"
        )
        self.host = host
        self.port = port


def start_local_app(cfg: Config, host: str = "127.0.0.1", port: int = 8788) -> LocalApp:
    """Start the local engine + web server in a background thread; return a LocalApp handle.

    Raises PortInUseError if the port is already taken (e.g. the app is already running), checked
    before the engine is built so we never open the SQLite store the other instance already holds.
    """
    import uvicorn

    from foreman.server.app import _ensure_safe_exposure, create_app

    # Fail closed: never serve the fully-wired operational API on a public bind without a token
    # (issue #1 P0). Exposing `foreman app` via a tunnel requires setting FOREMAN_AUTH_TOKEN.
    _ensure_safe_exposure(cfg, host=host)

    # Single-instance / fail-fast: bail before building the engine if the port is taken. open=online
    # means one engine per machine (it owns the local store + gates); a second `foreman app` (or a
    # double-clicked exe) must not race to bind the port and fight over the same DB (issue: local
    # exe "did not start in time" was really uvicorn hitting EADDRINUSE in its daemon thread).
    if _port_in_use(host, port):
        raise PortInUseError(host, port)
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

    # PM 大脑 settings page (§15): provider/model/base_url can be switched at runtime from the UI
    # (stored in config_kv). The resolver is read per LLM request so a change takes effect WITHOUT
    # restarting the app. The api key stays in .env (a secret never surfaced in the UI).
    def _llm_settings() -> dict:
        if not hasattr(store, "get_setting"):
            return {}
        return {
            "provider": store.get_setting("llm.provider") or "",
            "model": store.get_setting("llm.model") or "",
            "base_url": store.get_setting("llm.base_url") or "",
        }

    def _llm() -> LLMClient:
        return LLMClient(cfg, settings_resolver=_llm_settings)

    toolbelt = Toolbelt(gate=gate)
    loop = DecisionLoop(
        store=store,
        gate=gate,
        cards=cards,
        operator=Operator(_llm(), language=language),
        auditor=Auditor(_llm(), language=language),
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
        session_id: str, goal: str, workspace: str, agent: str, model: str, effort: str = ""
    ) -> None:
        await runner.launch(agent, goal, Path(workspace), session_id, model=model, effort=effort)

    dispatcher = DispatchService(cfg, store, bus=bus, launcher=_launcher)
    # BriefingService summarizes a session's activity with YOUR LLM → reports table + Web Push
    # (§5.5). Output language follows the runtime ui.language setting (§15, resolved above).
    briefings = BriefingService(
        _llm(), store, bus=bus, pusher=pusher, language=language
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
    # uvicorn runs in a daemon thread; a failed bind makes it log the OSError and call sys.exit()
    # *inside that thread*, so the error never reaches us — capture it so the caller sees the real
    # cause (a port race the pre-flight check above can't fully close) instead of a blank timeout.
    error: list[BaseException] = []

    def _run() -> None:
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 — relay ANY thread-fatal error to the caller
            error.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _wait_until_started(server, thread, error, host, port)
    return LocalApp(
        url=f"http://{host}:{port}/",
        store=store,
        bus=bus,
        runner=runner,
        _server=server,
        _thread=thread,
    )


def _port_in_use(host: str, port: int) -> bool:
    """True if (host, port) is already bound. Probe-and-release: bind a throwaway socket; if that
    fails the port is taken. (Never accepts a connection, so it releases immediately on close.)"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def is_running(host: str = "127.0.0.1", port: int = 8788, timeout: float = 0.5) -> bool:
    """True if a Foreman local server already answers on (host, port). Used for single-instance:
    a second `foreman app` opens the existing window instead of starting a rival engine."""
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            data = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("ok") and "version" in data)


def _wait_until_started(server, thread, error, host, port, timeout: float = 30.0) -> None:
    """Block until the background uvicorn server is serving, or fail loudly with the real cause.

    A daemon thread that dies before flipping `started` almost always means the bind failed — most
    commonly the port is already taken (uvicorn swallows that OSError into sys.exit()). Translate
    that into an actionable PortInUseError rather than waiting out the timeout on a dead thread."""
    deadline = time.monotonic() + timeout
    while not getattr(server, "started", False):
        if not thread.is_alive():
            exc = error[0] if error else None
            if exc is None or isinstance(exc, SystemExit):
                raise PortInUseError(host, port)
            raise exc
        if time.monotonic() > deadline:
            raise RuntimeError("local server did not start in time")
        time.sleep(0.05)
