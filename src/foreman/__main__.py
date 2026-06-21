"""Foreman CLI entrypoint.

    foreman app                # PC app: engine + native window + tray (personal mode)
    foreman serve              # start the server-side component (relay + PWA; long-running)
    foreman dispatch "<task>"  # create a session and hand a task to an agent
    foreman token --rotate     # rotate the phone auth token
    foreman version
"""

from __future__ import annotations

import typer
from rich import print as rprint

from . import __version__
from foreman.shared.config import load_config

app = typer.Typer(add_completion=False, help="Foreman — a PM agent for your local coding agents.")


@app.command("app")  # command name "app"; function renamed so it doesn't shadow the Typer instance
def app_cmd(
    config: str = typer.Option("config.yaml", help="Path to config.yaml"),
    host: str = typer.Option("127.0.0.1", help="Local bind host"),
    port: int = typer.Option(8788, help="Local bind port"),
) -> None:
    """Start the PC app: engine + local UI in a native window (open=online, close=offline)."""
    from foreman.client.local_app import start_local_app  # lazy: keeps `foreman serve` client-free

    cfg = load_config(config)
    local = start_local_app(cfg, host=host, port=port)
    rprint(f"[bold green]Foreman[/] online — {local.url}  (close the window to go offline)")

    try:
        import webview  # pywebview, in the .[client] extra (imported lazily — desktop only)
    except ImportError:
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

    webview.create_window("Foreman", local.url, width=1000, height=760)
    try:
        webview.start()  # blocks until the window is closed
    finally:
        local.stop()
        rprint("[dim]Foreman offline.[/]")


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
    workspace: str = typer.Option(..., help="Workspace path the agent runs in"),
    config: str = typer.Option("config.yaml", help="Path to config.yaml"),
) -> None:
    """Create a session and run a task on an agent to completion (events persisted locally)."""
    import asyncio

    from foreman.client.dispatch import run_dispatch  # lazy: keeps `foreman serve` client-free

    cfg = load_config(config)
    try:
        session_id, n_events = asyncio.run(run_dispatch(cfg, task, workspace, agent))
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
