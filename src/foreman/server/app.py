"""FastAPI app factory.

Serves /health, the local REST API (sessions/events), a WS live stream, and the PWA static
files. Store + bus are INJECTED (personal mode = the client's local store; team server = its cache
store), so this module imports only shared — never the client. See docs/ARCHITECTURE.md / DESIGN §14.
"""

from __future__ import annotations

import asyncio
import json
import secrets as _secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

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
    """Mint an access key for the caller's own account (DESIGN §8.2). `expires_in_days` is the
    optional expiry knob (§8.4 "可设有效期"); 0/absent → a key that never expires by time."""

    label: str = ""
    # Bounded so a huge value can't overflow timedelta into an unhandled 500 (max 10y; 0 = never).
    expires_in_days: int = Field(default=0, ge=0, le=3650)


class _AdminAccountBody(BaseModel):
    """Admin creates a user (DESIGN §8.2 — no self-signup). `password` is optional: given →
    the account is active immediately (admin-set initial password); omitted → a one-time invite
    code is issued instead and returned once."""

    username: str
    display_name: str = ""
    role: str = "member"  # member | admin
    password: str = ""


class _AccountStatusBody(BaseModel):
    enabled: bool


class _RedeemBody(BaseModel):
    """A new user redeems an admin's invite to set their own password (the only non-admin path
    to a usable password — §8.2)."""

    code: str
    password: str


class _DispatchBody(BaseModel):
    """A task dispatched from the phone (DESIGN §5.1). workspace/agent fall back to config."""

    goal: str
    workspace: str = ""
    agent: str = ""


class _BriefBody(BaseModel):
    """Generate a briefing (DESIGN §5.5). Empty session_id → a roster of all sessions (daily)."""

    session_id: str = ""
    kind: str = "active-briefing"


class _DefinitionCreateBody(BaseModel):
    """Create a 秘方 block (workflow/skill/code_standard/qa_rubric) from the UI editor (§11.2)."""

    kind: str
    name: str
    body: str = ""
    scope_json: str = "{}"
    metadata_json: str = "{}"
    version: int | None = None
    activate: bool = True


class _DefinitionUpdateBody(BaseModel):
    """Edit a definition in place (only the passed fields change; identity is not editable)."""

    body: str | None = None
    scope_json: str | None = None
    metadata_json: str | None = None
    status: str | None = None


class _DefinitionImportBody(BaseModel):
    """Restore definitions from a backup bundle (T6.2). `bundle` is the exported envelope."""

    bundle: dict

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


# Hosts that mean "only this machine" — a personal app bound here is not network-exposed (the
# tunnel case binds loopback too, so the request-layer token is what protects an exposed app).
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}

# Operational endpoints are everything EXCEPT these public ones. The PWA shell (static files,
# manifest, service worker) is served by the StaticFiles mount and is public by design; only
# /health and the unauthenticated auth-bootstrap endpoints are public among the routed paths.
_PUBLIC_API_PATHS = frozenset(
    {"/api/auth/login", "/api/auth/redeem", "/api/push/vapid-public-key"}
)


def _is_operational_path(path: str) -> bool:
    """True if a request path must clear the access guard (issue #1 P0).

    Operational = the local REST surface (/api/*) plus the hook sink (/hooks). The static PWA
    shell and /health are public. WebSockets (/ws, /relay) authorize inside their own handlers
    (HTTP middleware never sees a websocket scope)."""
    if path in _PUBLIC_API_PATHS:
        return False
    return path.startswith("/api/") or path == "/hooks"


def _effective_scheme(request: Request) -> str:
    """Client-facing scheme, trusting a proxy's X-Forwarded-Proto (Cloudflare terminates TLS, so
    request.url.scheme is http on the proxy→app hop). Used for the http→https redirect so it does
    not loop behind a TLS-terminating proxy."""
    fwd = request.headers.get("x-forwarded-proto", "")
    if fwd:
        return fwd.split(",")[0].strip().lower()
    return (request.url.scheme or "http").lower()


def _ensure_safe_exposure(cfg: Config, host: str | None = None) -> None:
    """Fail closed if personal-mode operational APIs would be exposed without protection (P0).

    Personal mode has no per-account auth; its only gate is the shared access token
    (FOREMAN_AUTH_TOKEN). Binding to a non-loopback host — or advertising a public_base_url —
    without that token would let anyone with the URL read sessions, dispatch work, or approve
    actions, so we refuse to start. Team mode (per-account auth) and an explicit
    `server.allow_insecure_bind` opt-out are exempt. `host` overrides cfg.server.host for callers
    (e.g. `foreman app`) that bind a host of their own. Raises RuntimeError when unsafe."""
    if (cfg.server.mode or "personal").strip().lower() == "team":
        return
    if cfg.secrets.auth_token or cfg.server.allow_insecure_bind:
        return
    bind = (host if host is not None else cfg.server.host or "").strip().lower()
    exposed = bind not in _LOOPBACK_HOSTS or bool((cfg.server.public_base_url or "").strip())
    # 0.0.0.0 binds every interface — treat it as exposed even though it is in the set above.
    if bind == "0.0.0.0":
        exposed = True
    if exposed:
        raise RuntimeError(
            "Refusing to expose personal-mode operational APIs without an access token. "
            "Set FOREMAN_AUTH_TOKEN, switch server.mode to 'team', or (trusted LAN only) set "
            "server.allow_insecure_bind: true. See deploy/README.md (issue #1 P0)."
        )


def create_app(
    cfg: Config,
    store: Any = None,
    bus: EventBus | None = None,
    hooks: Any = None,
    relay: Any = None,
    gate: Any = None,
    auth: Any = None,
    cards: Any = None,
    dispatcher: Any = None,
    briefings: Any = None,
    definitions: Any = None,
    cache: Any = None,
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
    app.state.dispatcher = dispatcher
    app.state.briefings = briefings
    app.state.definitions = definitions
    app.state.cache = cache

    # ── access guard (issue #1 P0): no operational endpoint is reachable unauthenticated ───────
    # Personal mode (no AuthManager injected): a configured shared token (FOREMAN_AUTH_TOKEN) gates
    # every /api/* call and /hooks; the PWA pastes it once and sends it as a bearer. With no token
    # the app is single-user local — startup (_ensure_safe_exposure) refuses to bind it to a public
    # interface, so "open" only ever means loopback.
    # Team mode (AuthManager injected): each operational endpoint already enforces a per-ACCOUNT
    # token via require_account/require_admin, so the shared-token middleware stays out of the way.
    _shared_token = (cfg.secrets.auth_token or "").strip()

    def _access_authorized(request: Request) -> bool:
        if auth is not None:
            return True  # team mode: per-route account guards enforce auth
        if not _shared_token:
            return True  # personal local mode (exposure blocked at startup)
        return _secrets.compare_digest(_bearer_token(request), _shared_token)

    def _ws_authorized(token: str) -> bool:
        """Authorize a websocket (browsers can't set Authorization headers, so the token rides a
        query param). Team mode requires a valid account token; personal mode the shared token."""
        if auth is not None:
            return auth.resolve_token(token) is not None
        if not _shared_token:
            return True
        return _secrets.compare_digest(token or "", _shared_token)

    def _security_headers(response: Response) -> None:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if cfg.server.csp:
            response.headers.setdefault("Content-Security-Policy", cfg.server.csp)
        if cfg.server.hsts:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={int(cfg.server.hsts_max_age)}; includeSubDomains",
            )

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        # 1) optional http→https redirect (defense-in-depth; prefer doing this at the proxy — P2).
        if cfg.server.force_https and _effective_scheme(request) == "http":
            target = request.url.replace(scheme="https")
            resp: Response = RedirectResponse(str(target), status_code=308)
            _security_headers(resp)
            return resp
        # 2) fail-closed access guard on operational endpoints (P0).
        if _is_operational_path(request.url.path) and not _access_authorized(request):
            resp = JSONResponse({"detail": "unauthorized"}, status_code=401)
            resp.headers["WWW-Authenticate"] = "Bearer"
            _security_headers(resp)
            return resp
        # 3) hardening headers on every response (P2).
        response = await call_next(request)
        _security_headers(response)
        return response

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

    def require_admin(request: Request):
        """Like require_account, but also 403s a non-admin. Gates the admin console (build
        users / invite — DESIGN §8.2): only an admin may create accounts (no self-signup)."""
        account = require_account(request)
        if account.role != "admin":
            raise HTTPException(status_code=403, detail="admin only")
        return account

    @app.get("/health")
    async def health() -> dict:
        """Public readiness probe. Non-sensitive by default — the DB path is only included when
        server.health_show_db is set, so the public endpoint doesn't leak deployment paths (P2)."""
        out: dict = {
            "ok": True,
            "version": __version__,
            "agents": sorted(k for k, a in cfg.agents.items() if a.enabled),
        }
        if cfg.server.health_show_db:
            out["db"] = cfg.store.db_path
        return out

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

    @app.post("/api/tasks")
    async def dispatch_task(body: _DispatchBody) -> dict:
        """Dispatch a task from the phone → a new Root Session (DESIGN §5.1). Delegates to the
        injected client-side DispatchService; app.py stays shared-only (§14). No dispatcher
        (e.g. team-cache server) → 503."""
        if dispatcher is None or not hasattr(dispatcher, "create"):
            raise HTTPException(status_code=503, detail="no dispatcher")
        res = await dispatcher.create(
            body.goal, workspace=body.workspace or None, agent=body.agent or None
        )
        if res.get("ok"):
            return res
        status = {
            "empty_goal": 400,
            "unknown_agent": 400,
            "no_workspace": 400,
            "workspace_not_allowed": 400,
            "no_store": 503,
        }.get(res.get("error", ""), 400)
        raise HTTPException(status_code=status, detail=res.get("error", "decline"))

    @app.get("/api/overview")
    async def overview() -> list[dict]:
        """Multi-session dashboard: every session + its activity counts (newest first). §5.1/§6."""
        if dispatcher is None or not hasattr(dispatcher, "overview"):
            raise HTTPException(status_code=503, detail="no dispatcher")
        return dispatcher.overview()

    @app.get("/api/reports")
    async def list_reports(session_id: str | None = None) -> list[dict]:
        """Briefings (the phone's status-report feed). DESIGN §5.5. No briefing service → 503."""
        if briefings is None or not hasattr(briefings, "list_reports"):
            raise HTTPException(status_code=503, detail="no briefing service")
        return briefings.list_reports(session_id)

    @app.post("/api/reports/generate")
    async def generate_report(body: _BriefBody) -> dict:
        """Generate a briefing now (DESIGN §5.5) and store/push it. Uses YOUR LLM via the injected
        client-side BriefingService; app.py stays shared-only (§14)."""
        if briefings is None or not hasattr(briefings, "generate"):
            raise HTTPException(status_code=503, detail="no briefing service")
        res = await briefings.generate(session_id=body.session_id or None, kind=body.kind)
        if res.get("ok"):
            return res
        status = {"no_store": 503, "no_llm": 503}.get(res.get("error", ""), 400)
        raise HTTPException(status_code=status, detail=res.get("error", "decline"))

    # ── Definition editor: CRUD the four 秘方 blocks from the phone/web (DESIGN §11.2, T6.1) ──
    # Delegates to the injected client-side DefinitionService (which owns the local definitions
    # table); app.py stays shared-only and the 秘方 never leave the local process (§8.3 / §14).
    # No service (e.g. team-cache server) → 503: definitions are local-only by design.
    _DEFN_ERR_STATUS = {
        "bad_kind": 400, "bad_name": 400, "body_too_large": 400,
        "bad_scope_json": 400, "bad_metadata_json": 400, "bad_status": 400,
        "version_exists": 409, "not_found": 404, "no_store": 503,
    }

    @app.get("/api/definitions")
    async def list_definitions(
        kind: str | None = None, name: str | None = None, active_only: bool = False
    ) -> list[dict]:
        """List 秘方 blocks (optionally filtered by kind/name, or to active versions). §11.2."""
        if definitions is None or not hasattr(definitions, "list_definitions"):
            raise HTTPException(status_code=503, detail="no definition service")
        return definitions.list_definitions(kind=kind, name=name, active_only=active_only)

    # NOTE: these literal-path routes MUST be registered before /api/definitions/{definition_id}
    # so "export"/"import" aren't captured as a definition_id path param.
    @app.get("/api/definitions/export")
    async def export_definitions(encrypt: bool = False) -> dict:
        """Download a backup bundle of all 秘方 + wiring (T6.2). `encrypt=true` encrypts each body
        with the configured cipher so the file can be carried without leaking recipes (§765)."""
        if definitions is None or not hasattr(definitions, "export_bundle"):
            raise HTTPException(status_code=503, detail="no definition service")
        res = definitions.export_bundle(encrypt=encrypt)
        if res.get("ok"):
            return res["bundle"]
        status = {"no_store": 503, "no_cipher": 400}.get(res.get("error", ""), 400)
        raise HTTPException(status_code=status, detail=res.get("error", "decline"))

    @app.post("/api/definitions/import")
    async def import_definitions(body: _DefinitionImportBody) -> dict:
        """Restore 秘方 from a backup bundle (T6.2). Merge semantics: existing rows are skipped,
        so re-import is idempotent and never clobbers live recipes."""
        if definitions is None or not hasattr(definitions, "import_bundle"):
            raise HTTPException(status_code=503, detail="no definition service")
        res = await definitions.import_bundle(body.bundle)
        if res.get("ok"):
            return res
        status = {
            "no_store": 503, "bad_bundle": 400, "bad_format": 400,
            "unsupported_version": 400, "too_large": 413, "needs_key": 400, "bad_key": 400,
        }.get(res.get("error", ""), 400)
        raise HTTPException(status_code=status, detail=res.get("error", "decline"))

    @app.get("/api/definitions/{definition_id}")
    async def get_definition(definition_id: str) -> dict:
        """One definition (the editor opens it to edit its body). §11.2."""
        if definitions is None or not hasattr(definitions, "get_definition"):
            raise HTTPException(status_code=503, detail="no definition service")
        row = definitions.get_definition(definition_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        return row

    @app.post("/api/definitions")
    async def create_definition(body: _DefinitionCreateBody) -> dict:
        """Create a 秘方 block / a new version (the 增 path). §11.2."""
        if definitions is None or not hasattr(definitions, "create_definition"):
            raise HTTPException(status_code=503, detail="no definition service")
        res = await definitions.create_definition(
            kind=body.kind, name=body.name, body=body.body,
            scope_json=body.scope_json, metadata_json=body.metadata_json,
            version=body.version, activate=body.activate,
        )
        if res.get("ok"):
            return res
        raise HTTPException(
            status_code=_DEFN_ERR_STATUS.get(res.get("error", ""), 400),
            detail=res.get("error", "decline"),
        )

    @app.patch("/api/definitions/{definition_id}")
    async def update_definition(definition_id: str, body: _DefinitionUpdateBody) -> dict:
        """Edit a definition in place (the 改 path). §11.2."""
        if definitions is None or not hasattr(definitions, "update_definition"):
            raise HTTPException(status_code=503, detail="no definition service")
        res = await definitions.update_definition(
            definition_id, body=body.body, scope_json=body.scope_json,
            metadata_json=body.metadata_json, status=body.status,
        )
        if res.get("ok"):
            return res
        raise HTTPException(
            status_code=_DEFN_ERR_STATUS.get(res.get("error", ""), 400),
            detail=res.get("error", "decline"),
        )

    @app.post("/api/definitions/{definition_id}/activate")
    async def activate_definition(definition_id: str) -> dict:
        """Make this version THE live one for its (kind, name) — enable/rollback knob. §11.2."""
        if definitions is None or not hasattr(definitions, "activate_definition"):
            raise HTTPException(status_code=503, detail="no definition service")
        res = await definitions.activate_definition(definition_id)
        if res.get("ok"):
            return res
        raise HTTPException(
            status_code=_DEFN_ERR_STATUS.get(res.get("error", ""), 400),
            detail=res.get("error", "decline"),
        )

    @app.delete("/api/definitions/{definition_id}")
    async def delete_definition(definition_id: str) -> dict:
        """Delete a definition + its links (the 删 path). §11.2."""
        if definitions is None or not hasattr(definitions, "delete_definition"):
            raise HTTPException(status_code=503, detail="no definition service")
        res = await definitions.delete_definition(definition_id)
        if res.get("ok"):
            return res
        raise HTTPException(
            status_code=_DEFN_ERR_STATUS.get(res.get("error", ""), 400),
            detail=res.get("error", "decline"),
        )

    @app.post("/api/auth/login")
    async def auth_login(body: _LoginBody) -> dict:
        """PWA user login → bearer token (DESIGN §8.2). 401 on bad credentials (generic, no
        leak of which field was wrong); 503 if no auth manager (personal mode)."""
        if auth is None:
            raise HTTPException(status_code=503, detail="auth not configured")
        res = auth.login(body.username, body.password)
        if res.get("ok"):
            return {"token": res["token"], "account_id": res["account_id"], "role": res["role"]}
        if res.get("error") == "locked":
            # Brute-force throttle tripped — uniform per submitted username, so no enumeration leak.
            raise HTTPException(status_code=429, detail="too many attempts, try again later")
        raise HTTPException(status_code=401, detail="invalid credentials")

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
        the user pastes it into their local process; only its hash is stored (§8.4). An optional
        `expires_in_days` sets a time limit (§8.4); the relay refuses the key once it lapses."""
        account = require_account(request)
        days = body.expires_in_days if body.expires_in_days > 0 else None
        res = auth.create_access_key(account.id, label=body.label, expires_in_days=days)
        return {
            "id": res["id"], "key": res["key"],
            "label": res["label"], "expires_at": res["expires_at"],
        }

    @app.delete("/api/keys/{key_id}")
    async def revoke_key(key_id: str, request: Request) -> dict:
        """Revoke one of the caller's keys (ownership-checked; 404 if not yours — §8.4)."""
        account = require_account(request)
        res = auth.revoke_access_key(account.id, key_id)
        if not res.get("ok"):
            raise HTTPException(status_code=404, detail="key not found")
        return {"ok": True}

    @app.get("/api/processes")
    async def list_processes(request: Request) -> list[dict]:
        """The caller's OWN machines (online + offline), metadata only. Multi-tenant isolation
        (§8.4, T7.4): scoped to the logged-in account — another tenant's processes never show."""
        account = require_account(request)
        return auth.list_processes(account.id)

    # ── Display cache: the PWA reads a read-only copy while the PC is offline (§8.5 ③, T7.5) ───
    # Served from the relay box's display cache (cache_sessions / cache_cards), which the local
    # process pushes up the link. Scoped to the logged-in account (§8.4) — another tenant's cache
    # never shows. Personal mode injects no cache → 503. (When the PC is online the PWA is routed
    # live to it; that live proxy is the deferred team rollout — see TASKS T7.1.)
    @app.get("/api/cache/sessions")
    async def cache_sessions(request: Request) -> list[dict]:
        """The caller's cached session summaries (offline read-only copy). §8.5 ③."""
        account = require_account(request)
        if cache is None or not hasattr(cache, "list_sessions"):
            raise HTTPException(status_code=503, detail="no display cache")
        return cache.list_sessions(account.id)

    @app.get("/api/cache/cards")
    async def cache_cards(request: Request, session_id: str | None = None) -> list[dict]:
        """The caller's cached decision cards (offline read-only copy), optionally per session."""
        account = require_account(request)
        if cache is None or not hasattr(cache, "list_cards"):
            raise HTTPException(status_code=503, detail="no display cache")
        return cache.list_cards(account.id, session_id)

    # ── Admin console: build users + invite (no self-signup — DESIGN §8.2, T7.2) ──────────────
    @app.get("/api/admin/accounts")
    async def admin_list_accounts(request: Request) -> list[dict]:
        """Every account (admin only; metadata, never password hashes — §8.4)."""
        require_admin(request)
        return auth.list_accounts()

    @app.post("/api/admin/accounts")
    async def admin_create_account(body: _AdminAccountBody, request: Request) -> dict:
        """Build a user (admin only). With a password → active immediately. Without → an invite
        code is minted and returned ONCE (the admin hands it to the user, who redeems it to set
        their own password). 409 if the username is taken, 400 on bad input."""
        require_admin(request)
        if body.password:
            res = auth.create_account(
                body.username, body.password, role=body.role, display_name=body.display_name
            )
        else:
            res = auth.invite_account(
                body.username, role=body.role, display_name=body.display_name
            )
        if res.get("ok"):
            return res
        status = {"exists": 409, "bad_input": 400}.get(res.get("error", ""), 400)
        raise HTTPException(status_code=status, detail=res.get("error", "decline"))

    @app.post("/api/admin/accounts/{account_id}/invite")
    async def admin_reinvite(account_id: str, request: Request) -> dict:
        """Re-issue a one-time invite for an existing account (re-invite / password reset). Admin
        only. Returns {invite_code, expires_at} once; any prior unused code is burned."""
        require_admin(request)
        res = auth.reinvite_account(account_id)
        if res.get("ok"):
            return res
        raise HTTPException(status_code=404, detail=res.get("error", "not found"))

    @app.post("/api/admin/accounts/{account_id}/status")
    async def admin_set_status(
        account_id: str, body: _AccountStatusBody, request: Request
    ) -> dict:
        """Enable/disable an account (admin only). Refuses to disable your OWN account (so an
        admin can't lock themselves out) — 400. 404 if the account is gone."""
        admin = require_admin(request)
        if not body.enabled and account_id == admin.id:
            raise HTTPException(status_code=400, detail="cannot disable self")
        res = auth.set_account_enabled(account_id, body.enabled)
        if res.get("ok"):
            return res
        raise HTTPException(status_code=404, detail=res.get("error", "not found"))

    @app.get("/api/admin/health")
    async def admin_health(request: Request) -> dict:
        """System-wide health for the admin (admin only). AGGREGATE counts only — account
        totals by status + online-process count — and NEVER any tenant's content or secrets
        (§8.4: "管理员看系统健康，看不到他人内容")."""
        require_admin(request)
        return auth.system_health()

    @app.post("/api/auth/redeem")
    async def auth_redeem(body: _RedeemBody) -> dict:
        """Redeem an admin's invite (NO auth — this is how a new user bootstraps): set the
        password, activate the account, and get logged straight in (§8.2). 400 on a bad/spent/
        expired code or a too-short password; 503 if no auth manager (personal mode)."""
        if auth is None:
            raise HTTPException(status_code=503, detail="auth not configured")
        res = auth.redeem_invite(body.code, body.password)
        if res.get("ok"):
            return {"token": res["token"], "account_id": res["account_id"], "role": res["role"]}
        raise HTTPException(status_code=400, detail=res.get("error", "decline"))

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
    async def ws_endpoint(
        websocket: WebSocket, session_id: str | None = None, token: str = ""
    ) -> None:
        """Stream events: backlog for ?session_id (from the store), then live from the bus.

        Authorized like the REST surface (issue #1 P0): the access token rides a ?token= query
        param since browsers can't set an Authorization header on a WebSocket. Unauthorized →
        accept-then-close(1008) rather than a bare upgrade refusal."""
        if not _ws_authorized(token):
            await websocket.accept()
            await websocket.close(code=1008)
            return
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


def build_serve_app(cfg: Config) -> FastAPI:
    """Assemble the app that `foreman serve` runs (DESIGN §8.5 live wiring, T7.1).

    Personal mode (default, `server.mode != "team"`): no relay/accounts — just /health + PWA +
    the single-user REST/WS the tunnel exposes. Identical to the previous `create_app(cfg)`, so
    the deployed server keeps behaving exactly as before unless team mode is opted into.

    Team mode (`server.mode == "team"`): build the server store (accounts / access_keys /
    process_registry), an AuthManager (user login + key mgmt), a Relay, and a DisplayCache, and
    inject them so local processes dial in at /relay and the PWA routes BY ACCOUNT to the right
    machine — or, while that machine is offline, reads the relay's display cache (T7.5, §8.5 ③).
    The team server holds NO 秘方 / diffs / per-user LLM keys (§8.3) — those stay on each local
    process; `store` stays None (the relay box has no client-style local store), so the personal
    session/event endpoints 503 while the account-scoped /api/cache/* endpoints serve the cache.
    """
    # Fail closed before binding: never expose unprotected personal-mode operational APIs (P0).
    _ensure_safe_exposure(cfg)
    if (cfg.server.mode or "personal").strip().lower() != "team":
        return create_app(cfg)

    # Lazy imports: keep create_app's import surface unchanged for personal mode / tests.
    from .auth_manager import AuthManager
    from .display_cache import DisplayCacheService
    from .relay import Relay
    from .store import ServerStore

    server_store = ServerStore(cfg.server.db_path)
    server_store.init()
    bus = EventBus()
    cache = DisplayCacheService(server_store)
    relay = Relay(server_store, bus, cache=cache)
    auth = AuthManager(server_store)
    # store stays None: the team relay box has no client-style local store (秘方/events live on
    # each user's machine). relay + auth + cache carry the ServerStore. The PWA reads the display
    # cache (§8.5 ③) while a machine is offline; the live proxy-when-online is the deferred rollout.
    return create_app(cfg, bus=bus, relay=relay, auth=auth, cache=cache)
