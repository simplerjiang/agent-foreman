"""FastAPI app factory.

Serves /health, the local REST API (sessions/events), a WS live stream, and the PWA static
files. Store + bus are INJECTED (personal mode = the client's local store; team server = its cache
store), so this module imports only shared — never the client. See docs/ARCHITECTURE.md / DESIGN §14.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from foreman.shared.config import Config
from foreman.shared.events import AgentEvent, EventBus

from .. import __version__

WEB_DIR = Path(__file__).resolve().parent / "web"  # PWA front-end ships inside server/ (DESIGN §14)


def _session_to_dict(s) -> dict:
    return {
        "id": s.id, "goal": s.goal, "status": s.status, "workspace": s.workspace,
        "agent_type": s.agent_type, "created_at": s.created_at, "updated_at": s.updated_at,
    }


def _row_to_dict(row) -> dict:
    """A stored Event row → JSON-friendly dict (payload_json parsed back to an object)."""
    return {
        "id": row.id, "session_id": row.session_id, "task_id": row.task_id,
        "type": row.type, "source": row.source,
        "payload": json.loads(row.payload_json or "{}"), "ts": row.ts,
    }


def _event_to_dict(ev: AgentEvent) -> dict:
    """A live AgentEvent → JSON-friendly dict (same shape as _row_to_dict)."""
    return {
        "id": None, "session_id": ev.session_id, "task_id": ev.task_id,
        "type": ev.type, "source": ev.source, "payload": ev.payload, "ts": ev.ts,
    }


def create_app(cfg: Config, store: object | None = None, bus: EventBus | None = None) -> FastAPI:
    app = FastAPI(title="Foreman", version=__version__)

    # Store + bus are INJECTED by the caller (personal mode: client store; team server: cache store).
    # This module never imports the client — 秘方 stays local (DESIGN §8.3, §14 boundary).
    bus = bus or EventBus()
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

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict]:
        if store is None:
            raise HTTPException(status_code=503, detail="no local store")
        return [_session_to_dict(s) for s in store.get_sessions()]

    @app.get("/api/sessions/{session_id}/events")
    async def list_events(session_id: str) -> list[dict]:
        if store is None:
            raise HTTPException(status_code=503, detail="no local store")
        return [_row_to_dict(e) for e in store.get_events(session_id)]

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket, session_id: str | None = None) -> None:
        """Stream events: backlog for ?session_id (from the store), then live from the bus."""
        await websocket.accept()
        if store is not None and session_id:
            for e in store.get_events(session_id):
                await websocket.send_json(_row_to_dict(e))
        q = bus.subscribe_queue()

        async def pump() -> None:
            while True:
                ev = await q.get()
                if session_id is None or ev.session_id == session_id:
                    await websocket.send_json(_event_to_dict(ev))

        async def watch_disconnect() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                return

        try:
            tasks = [asyncio.create_task(pump()), asyncio.create_task(watch_disconnect())]
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        finally:
            bus.unsubscribe(q)

    # Serve the PWA if present (mounted last so it doesn't shadow API routes).
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app
