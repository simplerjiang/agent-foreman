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

from foreman.shared.autonomy import level_label, normalize_level
from foreman.shared.config import Config
from foreman.shared.events import AgentEvent, EventBus
from foreman.shared.i18n import normalize as normalize_lang

from .. import __version__


class _LanguageBody(BaseModel):
    language: str


class _AutonomyBody(BaseModel):
    """Autonomy dial setting (DESIGN §6.4): level 0..3 (coerced/clamped server-side)."""

    level: int


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


class _CardChoiceBody(BaseModel):
    """A one-tap decision on a card (§6.3): which option button the human pressed."""

    option: str  # approve | revise | undo | manual


class _LoginBody(BaseModel):
    """PWA user login (DESIGN §8.2). Distinct from a local process's access key."""

    username: str
    password: str


class _AccessKeyBody(BaseModel):
    label: str = ""

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


def _bearer_token(request: Request) -> str:
    """Extract the bearer token from the Authorization header ('' if absent/malformed)."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def create_app(
    cfg: Config,
    store: object | None = None,
    bus: EventBus | None = None,
    hooks: object | None = None,
    relay: object | None = None,
    gate: object | None = None,
    auth: object | None = None,
    cards: object | None = None,
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
    app.state.auth = auth
    app.state.cards = cards

    def require_account(request: Request):
        """Resolve the Authorization bearer token to an active account, or raise 401/503.

        Team-mode auth (DESIGN §8.2): the injected AuthManager validates the PWA login token.
        Personal mode injects no auth manager, so these endpoints return 503 (there are no
        accounts — the PC self-hosts its own UI; single-user remote access is the tunnel's job)."""
        if auth is None:
            raise HTTPException(status_code=503, detail="auth not configured")
        account = auth.resolve_token(_bearer_token(request))
        if account is None:
            raise HTTPException(status_code=401, detail="unauthorized")
        return account

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

    def _effective_lang() -> str:
        current = None
        if store is not None and hasattr(store, "get_setting"):
            current = store.get_setting("ui.language")
        return normalize_lang(current or cfg.ui.language)

    @app.get("/api/settings/autonomy")
    async def get_autonomy() -> dict:
        """Effective autonomy dial: config_kv override (if a store) else the config baseline (§6.4)."""
        current = None
        if store is not None and hasattr(store, "get_setting"):
            current = store.get_setting("autonomy.level")
        level = normalize_level(current if current is not None else cfg.autonomy.level)
        return {"level": level, "label": level_label(level, _effective_lang())}

    @app.post("/api/settings/autonomy")
    async def set_autonomy(body: _AutonomyBody) -> dict:
        if store is None or not hasattr(store, "set_setting"):
            raise HTTPException(status_code=503, detail="no local store")
        level = normalize_level(body.level)
        store.set_setting("autonomy.level", str(level))
        return {"level": level, "label": level_label(level, _effective_lang())}

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

    @app.get("/api/cards")
    async def list_cards(session_id: str | None = None) -> list[dict]:
        """Decision cards (the folded summaries you tap on). DESIGN §6.3.

        Delegates to the injected client-side CardService (which owns the decision_cards table +
        the local diff/raw-output assembly); app.py stays shared-only (DESIGN §14). No card
        service (e.g. team-cache server) → 503."""
        if cards is None or not hasattr(cards, "list_cards"):
            raise HTTPException(status_code=503, detail="no card service")
        return cards.list_cards(session_id)

    @app.post("/api/cards/{card_id}/choose")
    async def choose_card(card_id: str, body: _CardChoiceBody) -> dict:
        """Record the human's one-tap decision on a card (§6.3). Executing the chosen path is
        the two-way control layer (P4); this closes the decide half and emits `card_decided`."""
        if cards is None or not hasattr(cards, "record_choice"):
            raise HTTPException(status_code=503, detail="no card service")
        res = await cards.record_choice(card_id, body.option)
        if res.get("ok"):
            return res
        status = {"bad_option": 400, "no_store": 503, "not_found": 404}.get(
            res.get("error", ""), 400
        )
        raise HTTPException(status_code=status, detail=res.get("error", "decline"))

    @app.get("/api/actions/{action_id}/detail")
    async def action_detail(action_id: str) -> dict:
        """Step-detail drill-down for a card's [🔍 查看详情]: raw return + per-line diff (§6.3).

        Assembles ① the agent's raw events for this step and ② the per-file/per-line git diff
        from the step's checkpoint to the live worktree — both stay on the local process (§8.3)."""
        if cards is None or not hasattr(cards, "step_detail"):
            raise HTTPException(status_code=503, detail="no card service")
        detail = cards.step_detail(action_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="action not found")
        return detail

    @app.post("/api/auth/login")
    async def auth_login(body: _LoginBody) -> dict:
        """PWA user login → bearer token (DESIGN §8.2). 401 on bad credentials (generic, no
        leak of which field was wrong); 503 if no auth manager (personal mode)."""
        if auth is None:
            raise HTTPException(status_code=503, detail="auth not configured")
        res = auth.login(body.username, body.password)
        if not res.get("ok"):
            raise HTTPException(status_code=401, detail="invalid credentials")
        return {"token": res["token"], "account_id": res["account_id"], "role": res["role"]}

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request) -> dict:
        """Invalidate the caller's bearer token. Idempotent — always returns ok."""
        if auth is not None:
            auth.logout(_bearer_token(request))
        return {"ok": True}

    @app.get("/api/auth/me")
    async def auth_me(request: Request) -> dict:
        """Who am I (validates the token; the PWA uses this to confirm a stored login)."""
        account = require_account(request)
        return {
            "account_id": account.id, "username": account.username,
            "role": account.role, "display_name": account.display_name,
        }

    @app.get("/api/keys")
    async def list_keys(request: Request) -> list[dict]:
        """The caller's access keys (metadata only — never the hash/plaintext). DESIGN §8.2."""
        account = require_account(request)
        return auth.list_access_keys(account.id)

    @app.post("/api/keys")
    async def create_key(body: _AccessKeyBody, request: Request) -> dict:
        """Mint a new access key for the caller. The plaintext is returned exactly ONCE here —
        the user pastes it into their local process; only its hash is stored (§8.4)."""
        account = require_account(request)
        res = auth.create_access_key(account.id, label=body.label)
        return {"id": res["id"], "key": res["key"], "label": res["label"]}

    @app.delete("/api/keys/{key_id}")
    async def revoke_key(key_id: str, request: Request) -> dict:
        """Revoke one of the caller's keys (ownership-checked; 404 if not yours — §8.4)."""
        account = require_account(request)
        res = auth.revoke_access_key(account.id, key_id)
        if not res.get("ok"):
            raise HTTPException(status_code=404, detail="key not found")
        return {"ok": True}

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
