"""Foreman CLI entrypoint.

    foreman serve              # start the PM Core + web backend (long-running)
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
    """Create a session and dispatch a task to an agent (P1+)."""
    rprint("[yellow]dispatch is not implemented yet (roadmap P1).[/] "
           "It will create a Root Session and launch the agent in the workspace.")
    raise typer.Exit(code=1)


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
