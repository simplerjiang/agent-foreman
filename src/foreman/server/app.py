"""FastAPI app factory.

P0: boots, opens the DB, serves /health and the PWA static files.
P1+: adds REST (sessions/tasks/events/approvals/reports), WS live stream, and /hooks.
See docs/ARCHITECTURE.md for the full API surface.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Config
from ..core.events import EventBus
from ..store import Store

WEB_DIR = Path(__file__).resolve().parents[3] / "web"


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="Foreman", version=__version__)

    store = Store(cfg.store.db_path)
    store.init()
    bus = EventBus()

    # Stash shared singletons for routes/components to reach (P1+ will add a real DI/state layer).
    app.state.cfg = cfg
    app.state.store = store
    app.state.bus = bus

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "agents": sorted(k for k, a in cfg.agents.items() if a.enabled),
            "db": cfg.store.db_path,
        }

    # P1+: app.include_router(api_router); app.add_api_websocket_route("/ws", ws_endpoint)
    #      app.post("/hooks")(hooks_endpoint)

    # Serve the PWA if present (mounted last so it doesn't shadow API routes).
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app
