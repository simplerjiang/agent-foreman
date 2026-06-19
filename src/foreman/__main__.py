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
) -> None:
    """Start the PC app: engine + native window + tray + computer-use (personal mode)."""
    rprint("[yellow]`foreman app` (PC app: engine + pywebview window + tray) is not implemented "
           "yet (roadmap P1).[/] Use `foreman serve` for the server-side component meanwhile.")
    raise typer.Exit(code=1)


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

    from .server.app import create_app

    rprint(f"[bold green]Foreman[/] v{__version__} starting on "
           f"http://{cfg.server.host}:{cfg.server.port}")
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port)


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
