"""Foreman CLI entrypoint.

    foreman app                # PC app: engine + native window + tray (personal mode)
    foreman serve              # start the server-side component (relay + PWA; long-running)
    foreman dispatch "<task>"  # create a session and hand a task to an agent
    foreman token --rotate     # rotate the phone auth token
    foreman version
"""

from __future__ import annotations

import os
from typing import cast

import typer
from rich.console import Console

from . import __version__
from foreman.shared.config import load_config

_CLI_CONSOLE = Console(color_system=None, legacy_windows=False, highlight=False)
rprint = _CLI_CONSOLE.print

app = typer.Typer(add_completion=False, help="Foreman — a PM agent for your local coding agents.")
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 800
WINDOW_MIN_SIZE = (1120, 680)


@app.command("app")  # command name "app"; function renamed so it doesn't shadow the Typer instance
def app_cmd(
    config: str = typer.Option("config.yaml", help="Path to config.yaml"),
    host: str = typer.Option("127.0.0.1", help="Local bind host"),
    port: int = typer.Option(8788, help="Local bind port"),
) -> None:
    """Start the PC app: engine + local UI in a native window (open=online, close=offline)."""
    from foreman.client.local_app import (  # lazy: keeps `foreman serve` client-free
        PortInUseError,
        is_running,
        start_local_app,
    )
    from foreman.client.splash import close_splash, show_splash

    # Finish any pending self-update: delete the leftover foreman.old.exe once it's unlocked
    # (便携版一键自更新). No-op from source / when nothing pending.
    from foreman.client.update import cleanup_stale

    cleanup_stale()

    url = f"http://{host}:{port}/"
    # Single instance: the engine owns the local SQLite store + gates, so only one may run per
    # machine. If Foreman already answers on the port (e.g. the exe was double-clicked again), open
    # that instance's window instead of starting a rival engine that can't bind the port — the bug
    # where a second launch died with "local server did not start in time" (really EADDRINUSE).
    if is_running(host, port):
        focused = _focus_existing_window("Foreman")
        if focused:
            rprint("[yellow]Foreman is already running[/] — focused the existing window")
            return
        rprint(f"[yellow]Foreman is already running[/] — opening {url}")
        _open_window(url)
        return

    # Splash screen: show immediately so the user gets visual feedback while the engine boots.
    show_splash()

    cfg = load_config(config)
    try:
        local = start_local_app(cfg, host=host, port=port)
    except PortInUseError:
        close_splash()
        rprint(f"[red]Port {port} is already in use[/] by another program — close it, or start "
               "Foreman on a different port with [cyan]--port[/].")
        raise typer.Exit(code=1) from None
    rprint(f"[bold green]Foreman[/] online — {local.url}  (close the window to go offline)")

    _apply_webview_capture_safe_flags()
    try:
        import webview  # pywebview, in the .[client] extra (imported lazily — desktop only)
    except ImportError:
        close_splash()
        rprint("[yellow]pywebview not installed — serving headless.[/] Open "
               f"{local.url} in a browser; Ctrl+C to stop. (pip install \".[client]\" for the window.)")
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            local.stop()
        return

    window = webview.create_window(
        "Foreman",
        local.url,
        js_api=_DesktopApi(),
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=WINDOW_MIN_SIZE,
    )
    if window is not None:
        window.events.shown += close_splash
        # Self-update restart: a one-click update closes this window (clean stop → process exits) so
        # the swap helper can replace the exe, then it relaunches us (便携版一键自更新).
        from foreman.client.update import register_shutdown_hook

        register_shutdown_hook(window.destroy)
    else:
        close_splash()
    try:
        webview.start()  # blocks until the window is closed
    finally:
        local.stop()
        rprint("[dim]Foreman offline.[/]")


def _open_window(url: str) -> None:
    """Open a native window onto an already-running local server (no engine of our own). Falls back
    to a hint when pywebview isn't installed (headless build). Closing this window leaves the
    running instance untouched — we never started it, so we don't stop it."""
    _apply_webview_capture_safe_flags()
    try:
        import webview  # pywebview, in the .[client] extra (imported lazily — desktop only)
    except ImportError:
        rprint(f"[yellow]pywebview not installed[/] — open {url} in a browser.")
        return
    webview.create_window(
        "Foreman",
        url,
        js_api=_DesktopApi(),
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=WINDOW_MIN_SIZE,
    )
    webview.start()  # blocks until this window is closed


def _apply_webview_capture_safe_flags() -> None:
    """Opt-in WebView2 software-composition fallback for capture tools that see a black window."""
    if os.environ.get("FOREMAN_WEBVIEW_CAPTURE_SAFE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    flags = ["--disable-gpu", "--disable-gpu-compositing"]
    existing = os.environ.get(key, "")
    parts = existing.split()
    for flag in flags:
        if flag not in parts:
            parts.append(flag)
    os.environ[key] = " ".join(parts).strip()


def _focus_existing_window(title: str) -> bool:
    """Best-effort Windows focus for the already-running pywebview window."""
    import sys

    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = wintypes.HWND()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(hwnd, _lparam):
            nonlocal found
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if title.lower() in buf.value.lower():
                found = hwnd
                return False
            return True

        user32.EnumWindows(enum_proc, 0)
        if not found:
            return False
        user32.ShowWindow(found, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(found))
    except Exception:
        return False


class _DesktopApi:
    """Small pywebview bridge used by the local settings page."""

    def select_workspace_folder(self) -> str:
        try:
            import webview

            window = webview.windows[0] if webview.windows else None
            if window is None:
                return ""
            file_dialog = getattr(webview, "FileDialog", None)
            folder_dialog = getattr(file_dialog, "FOLDER", None)
            if folder_dialog is None:
                folder_dialog = getattr(webview, "FOLDER_DIALOG", 20)
            paths = window.create_file_dialog(cast(int, folder_dialog), allow_multiple=False)
        except Exception:
            return ""
        if not paths:
            return ""
        return str(paths[0])


@app.command()
def serve(
    config: str = typer.Option("config.yaml", help="Path to config.yaml"),
    host: str = typer.Option("", help="Override server host"),
    port: int = typer.Option(0, help="Override server port"),
) -> None:
    """Start the PM Core and web backend (blocking)."""
    import uvicorn

    cfg = load_config(config)
    if host:
        cfg.server.host = host
    if port:
        cfg.server.port = port

    from .server.app import build_serve_app

    mode = "team (relay 总机)" if (cfg.server.mode or "").lower() == "team" else "personal"
    rprint(f"[bold green]Foreman[/] v{__version__} [{mode}] starting on "
           f"http://{cfg.server.host}:{cfg.server.port}")
    uvicorn.run(build_serve_app(cfg), host=cfg.server.host, port=cfg.server.port)


@app.command()
def dispatch(
    task: str = typer.Argument(..., help="The task instruction"),
    agent: str = typer.Option("claude-code", help="Agent to use: claude-code | codex"),
    model: str = typer.Option("", help="Override the driven agent model for this dispatch"),
    effort: str = typer.Option("", help="Reasoning level for this dispatch: low | medium | high"),
    workspace: str = typer.Option(..., help="Workspace path the agent runs in"),
    config: str = typer.Option("config.yaml", help="Path to config.yaml"),
) -> None:
    """Create a session and run a task on an agent to completion (events persisted locally)."""
    import asyncio

    from foreman.client.dispatch import run_dispatch  # lazy: keeps `foreman serve` client-free

    cfg = load_config(config)
    try:
        session_id, n_events = asyncio.run(
            run_dispatch(cfg, task, workspace, agent, model=model, effort=effort)
        )
    except ValueError as e:
        rprint(f"[red]{e}[/]")
        raise typer.Exit(code=1) from e
    rprint(f"[green]session {session_id}[/] — {n_events} events captured "
           f"([cyan]{agent}[/] in {workspace})")


@app.command("seed-examples")
def seed_examples_cmd(
    config: str = typer.Option("config.yaml", help="Path to config.yaml"),
    activate: bool = typer.Option(True, help="Make each seeded example the active version"),
) -> None:
    """Seed the built-in, generic, redacted starter definitions into the local DB (idempotent)."""
    from foreman.client.core.examples import seed_examples  # lazy: keeps `foreman serve` client-free
    from foreman.client.store import Store
    from foreman.shared.crypto import cipher_from_config

    cfg = load_config(config)
    cipher = cipher_from_config(cfg.secrets.definition_key)
    store = Store(cfg.store.db_path, cipher=cipher)
    store.init()
    result = seed_examples(store, activate=activate)
    added, skipped = result["added"], result["skipped"]
    rprint(f"[green]seeded {len(added)} example definition(s)[/] into {cfg.store.db_path}")
    for label in added:
        rprint(f"  [cyan]+[/] {label}")
    if skipped:
        rprint(f"[dim]skipped {len(skipped)} already present: {', '.join(skipped)}[/]")


@app.command("backfill-descriptions")
def backfill_descriptions_cmd(
    config: str = typer.Option("config.yaml", help="Path to config.yaml"),
    apply: bool = typer.Option(
        False, "--apply", help="Persist the proposals (default: dry-run — propose, write nothing)"
    ),
) -> None:
    """LLM-backfill metadata.description for legacy definitions that lack one (P0 / D3).

    Defaults to a DRY RUN: it prints the description each definition WOULD get, so you can eyeball
    them before committing. Re-run with [cyan]--apply[/] to write them back. Needed so the
    description-required gate doesn't strand pre-existing rows out of auto-selection (DESIGN §4.3).
    Uses YOUR configured PM LLM; the 秘方 bodies never leave the local process."""
    import asyncio

    from foreman.client.core.description_backfill import backfill_descriptions
    from foreman.client.core.definition_service import DefinitionService
    from foreman.client.store import Store
    from foreman.shared.crypto import cipher_from_config
    from foreman.shared.llm import LLMClient

    cfg = load_config(config)
    cipher = cipher_from_config(cfg.secrets.definition_key)
    store = Store(cfg.store.db_path, cipher=cipher)
    store.init()

    def _llm_settings() -> dict:
        return {
            "provider": store.get_setting("llm.provider") or "",
            "model": store.get_setting("llm.model") or "",
            "base_url": store.get_setting("llm.base_url") or "",
            "transport": store.get_setting("llm.transport") or cfg.llm.transport,
            "context_window_tokens": store.get_setting("llm.context_window_tokens") or "",
            "reasoning_effort": store.get_setting("llm.reasoning_effort") or "",
            "api_key": cfg.secrets.llm_api_key,
        }

    llm = LLMClient(cfg, settings_resolver=_llm_settings)
    service = DefinitionService(store, cipher=cipher)
    result = asyncio.run(backfill_descriptions(store, service, llm, apply=apply))

    proposals = result["proposals"]
    if not proposals and not result["errors"]:
        rprint("[green]nothing to backfill[/] — every definition already has a description.")
        return
    mode = "[green]applied[/]" if apply else "[yellow]dry-run[/] (re-run with --apply to write)"
    rprint(f"backfill {mode}: {len(proposals)} proposal(s), {result['written']} written")
    for p in proposals:
        rprint(f"  [cyan]{p['kind']}/{p['name']}[/]: {p['description']}")
    for e in result["errors"]:
        rprint(f"  [red]error[/] {e.get('id')}: {e.get('error')}")


@app.command("create-admin")
def create_admin_cmd(
    username: str = typer.Argument(..., help="Admin username"),
    config: str = typer.Option("config.yaml", help="Path to config.yaml"),
    password: str = typer.Option(
        "", help="Initial password (omit to be prompted securely; never pass on a shared shell)"
    ),
    display_name: str = typer.Option("", help="Display name (defaults to the username)"),
) -> None:
    """Create the FIRST admin in the team-mode server store (DESIGN §8.2 — there is no self-signup).

    This is the bootstrap path for a team deployment: run it once on the server (where config.yaml
    sets server.mode: team and server.db_path), then that admin logs into the PWA and builds the
    rest of the users via the admin console. Idempotent-safe: a taken username is reported, not
    overwritten. Server-only import surface (no client) — safe to run on the relay box."""
    from getpass import getpass

    from foreman.server.auth_manager import AuthManager  # lazy: server-only, no client import
    from foreman.server.store import ServerStore

    cfg = load_config(config)
    if (cfg.server.mode or "personal").strip().lower() != "team":
        rprint(
            "[yellow]warning:[/] server.mode is not 'team' in this config — accounts are only used "
            "in team mode. Creating the admin in the server DB anyway so a later flip to team works."
        )
    if not password:
        password = getpass("Initial admin password: ")
        if password != getpass("Confirm password: "):
            rprint("[red]passwords do not match[/]")
            raise typer.Exit(code=1)
    store = ServerStore(cfg.server.db_path)
    store.init()
    auth = AuthManager(store)
    res = auth.create_account(username, password, role="admin", display_name=display_name)
    if not res.get("ok"):
        rprint(f"[red]could not create admin:[/] {res.get('error')}")
        raise typer.Exit(code=1)
    rprint(
        f"[green]admin '{username}' created[/] in {cfg.server.db_path}. "
        "Log into the PWA and build the rest of the team from the admin console."
    )


@app.command()
def token(rotate: bool = typer.Option(False, "--rotate", help="Generate a new auth token")) -> None:
    """Show or rotate the phone auth token (P3+)."""
    rprint("[yellow]token management is not implemented yet (roadmap P3).[/]")
    raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Print version."""
    rprint(f"Foreman v{__version__}")


if __name__ == "__main__":
    app()
