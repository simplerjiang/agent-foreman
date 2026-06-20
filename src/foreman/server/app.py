"""FastAPI app factory.

Serves /health, the local REST API (sessions/events), a WS live stream, and the PWA static
files. Store + bus are INJECTED (personal mode = the client's local store; team server = its cache
store), so this module imports only shared — never the client. See docs/ARCHITECTURE.md / DESIGN §14.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from foreman.shared.config import Config
from foreman.shared.events import AgentEvent, EventBus
from foreman.shared.i18n import normalize as normalize_lang

from .. import __version__


class _LanguageBody(BaseModel):
    language: str


class _PushKeys(BaseModel):
    p256dh: str = ""
    auth: str = ""


class _PushSubBody(BaseModel):
    """Browser PushSubscription.toJSON() shape: {endpoint, expirationTime, keys:{p256dh, auth}}."""

    endpoint: str
    keys: _PushKeys = _PushKeys()


class _PushUnsubBody(BaseModel):
    endpoint: str


class _ApprovalDecision(BaseModel):
    """A one-tap approve/reject from the PC/phone. `nonce` is the one-time replay guard (§6.8)."""

    decision: str  # "approve" | "reject"
    nonce: str = ""
    reason: str = ""

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


def create_app(
    cfg: Config,
    store: object | None = None,
    bus: EventBus | None = None,
    hooks: object | None = None,
    relay: object | None = None,
    gate: object | None = None,
) -> FastAPI:
    app = FastAPI(title="Foreman", version=__version__)

    # Store + bus + hooks + relay are INJECTED by the caller (personal mode: client store + a
    # Gate-aware HookReceiver, no relay; team server: cache store + a Relay, no hooks). This
    # module never imports the client — 秘方 stays local and /hooks stays local (DESIGN §4.3,
    # §8.3, §8.5, §14 boundary).
    bus = bus or EventBus()
    app.state.cfg = cfg
    app.state.store = store
    app.state.bus = bus
    app.state.hooks = hooks
    app.state.relay = relay
    app.state.gate = gate

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

    @app.get("/api/settings/language")
    async def get_language() -> dict:
        """Effective UI/output language: config_kv override (if a store) else the config default."""
        current = None
        if store is not None and hasattr(store, "get_setting"):
            current = store.get_setting("ui.language")
        return {"language": normalize_lang(current or cfg.ui.language)}

    @app.post("/api/settings/language")
    async def set_language(body: _LanguageBody) -> dict:
        if store is None or not hasattr(store, "set_setting"):
            raise HTTPException(status_code=503, detail="no local store")
        lang = normalize_lang(body.language)
        store.set_setting("ui.language", lang)
        return {"language": lang}

    @app.get("/api/push/vapid-public-key")
    async def push_public_key() -> dict:
        """The VAPID application-server public key the PWA needs for PushManager.subscribe.

        `enabled` is False when no key is configured (the front-end then skips subscribing).
        Public by design — the VAPID public key is meant to be shared; the private key never
        leaves the server .env (DESIGN §4.6 / deploy/README)."""
        key = cfg.push.vapid_public_key
        return {"key": key, "enabled": bool(cfg.push.enabled and key)}

    @app.post("/api/push/subscribe")
    async def push_subscribe(body: _PushSubBody, request: Request) -> dict:
        """Persist a browser's push subscription so approval cards / briefings can reach it.

        Stored in the injected local store (personal mode); a server-cache store without these
        helpers returns 503 (team-mode push is part of the live rollout — DESIGN §8)."""
        if store is None or not hasattr(store, "add_push_subscription"):
            raise HTTPException(status_code=503, detail="no local store")
        store.add_push_subscription(
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh,
            auth=body.keys.auth,
            ua=request.headers.get("user-agent", ""),
        )
        return {"ok": True}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(body: _PushUnsubBody) -> dict:
        if store is None or not hasattr(store, "delete_push_subscription"):
            raise HTTPException(status_code=503, detail="no local store")
        store.delete_push_subscription(body.endpoint)
        return {"ok": True}

    @app.get("/api/approvals")
    async def list_approvals() -> list[dict]:
        """Pending approvals waiting on the human (the phone's queue). DESIGN §6.6 / §5.4.

        Delegates to the injected client-side Gate (which owns the approvals table); app.py stays
        shared-only (DESIGN §14). No Gate (e.g. team-cache server) → empty queue."""
        if gate is None or not hasattr(gate, "list_pending"):
            raise HTTPException(status_code=503, detail="no gate")
        return gate.list_pending()

    @app.post("/api/approvals/{approval_id}")
    async def decide_approval(approval_id: str, body: _ApprovalDecision) -> dict:
        """Approve/reject a held action (one-tap close of the loop). The nonce is the one-time
        replay guard (§6.8): an old captured request carries a stale nonce and is refused."""
        if gate is None or not hasattr(gate, "resolve"):
            raise HTTPException(status_code=503, detail="no gate")
        res = await gate.resolve(
            approval_id, body.decision, nonce=body.nonce, reason=body.reason
        )
        if res.get("ok"):
            return res
        status = {
            "bad_decision": 400,
            "no_store": 503,
            "not_found": 404,
            "bad_nonce": 403,
            "not_pending": 409,
        }.get(res.get("error", ""), 400)
        raise HTTPException(status_code=status, detail=res.get("error", "decline"))

    @app.post("/hooks")
    async def receive_hooks(request: Request) -> dict:
        """Claude Code hook sink (PreToolUse/PostToolUse/Stop/Notification). DESIGN §4.3.

        The hook name comes from the X-Hook header (see hooks/claude-hooks.example.json),
        falling back to the payload's hook_event_name. The reply is the hook *result* curl
        pipes back to Claude Code — a deny here blocks a dangerous tool call (§6.6).
        """
        if hooks is None:
            raise HTTPException(status_code=503, detail="hooks receiver not configured")
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"raw": payload}
        hook_name = (
            request.headers.get("x-hook")
            or payload.get("hook_event_name")
            or "Unknown"
        )
        session_id = request.query_params.get("session_id")
        return await hooks.handle(hook_name, payload, session_id)

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

    @app.websocket("/relay")
    async def relay_endpoint(websocket: WebSocket) -> None:
        """Outbound long-conn from a local process (team mode). Delegates to the injected Relay
        (handshake + per-account routing + heartbeat — DESIGN §8.5). Personal mode has no relay,
        so we accept and close politely (1008) instead of 404'ing the upgrade."""
        if relay is None:
            await websocket.accept()
            await websocket.close(code=1008)
            return
        await relay.serve(websocket)

    # Serve the PWA if present (mounted last so it doesn't shadow API routes).
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app
