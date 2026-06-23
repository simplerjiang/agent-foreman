"""FastAPI app factory.

Serves /health, the local REST API (sessions/events), a WS live stream, and the PWA static
files. Store + bus are INJECTED (personal mode = the client's local store; team server = its cache
store), so this module imports only shared — never the client. See docs/ARCHITECTURE.md / DESIGN §14.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets as _secrets
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from foreman.shared.autonomy import level_label, normalize_level
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg, default_agents
from foreman.shared.events import AgentEvent, EventBus, utc_now_iso
from foreman.shared.i18n import normalize as normalize_lang
from foreman.shared.ratelimit import SlidingWindowLimiter

from .. import __version__

# Auth brute-force speed bump (DESIGN §8.2): at most N login/redeem attempts per client IP per
# window, then 429. Generous enough never to bite a real user; tight enough to make online password
# guessing impractical on top of the PBKDF2 per-attempt cost.
_AUTH_RL_MAX_ATTEMPTS = 10
_AUTH_RL_WINDOW_SECONDS = 300
_WORKSPACES_SETTING = "workspaces.json"
_AGENTS_SETTING = "agents.json"
_LLM_KEY_ENV = "FOREMAN_LLM_API_KEY"
_CLOUD_KEY_ENV = "FOREMAN_CLOUD_ACCESS_KEY"
_VALID_AGENT_EFFORTS = frozenset({"", "low", "medium", "high"})
_SUPPORTED_AGENTS = frozenset(default_agents())


class _LanguageBody(BaseModel):
    language: str


class _AutonomyBody(BaseModel):
    """Autonomy dial setting (DESIGN §6.4): level 0..3 (coerced/clamped server-side)."""

    level: int


class _LLMSettingsBody(BaseModel):
    """PM 大脑 settings (DESIGN §15): switch the brain's provider/model/base_url at runtime. The api
    key is accepted for local save but never returned. Empty field = clear that override/key."""

    provider: str | None = None  # "openai" | "anthropic"
    model: str | None = None
    base_url: str | None = None
    transport: str | None = None  # "http" | "ws"
    api_key: str | None = None


class _WorkspaceBody(BaseModel):
    path: str
    name: str = ""


class _AgentSettingsRow(BaseModel):
    name: str
    enabled: bool = True
    command: str = ""
    model: str = ""
    effort: str = ""
    mode: str = "headless"
    full_access: bool = True


class _AgentSettingsBody(BaseModel):
    agents: list[_AgentSettingsRow]


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


class _DbMaintenanceBody(BaseModel):
    """A safe DB maintenance op from the admin console (数据库管理): 'vacuum' | 'integrity_check'."""

    action: str


class _RedeemBody(BaseModel):
    """A new user redeems an admin's invite to set their own password (the only non-admin path
    to a usable password — §8.2)."""

    code: str
    password: str


class _DispatchBody(BaseModel):
    """A task dispatched from the web/API. workspace/agent fall back to config/session."""

    goal: str
    workspace: str = ""
    agent: str = ""
    model: str = ""
    effort: str = ""  # reasoning level / 速度档位: low | medium | high ("" = the CLI default)
    session_id: str = ""  # when set, append a new task to an existing conversation
    source: str = ""  # desktop | phone | api


class _CloudSettingsBody(BaseModel):
    """Cloud relay connection settings (DESIGN §8.5). `url` is the relay 总机 base URL; `access_key`
    is the per-machine key (stored in local .env, never returned). None leaves a field as-is; an
    empty string clears it."""

    url: str | None = None
    access_key: str | None = None


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


def _compute_asset_ver() -> str:
    """Cache-busting token stamped into the PWA's ``?v=…`` asset URLs. Changes every deploy so
    Cloudflare's edge cache (and browsers) refetch the CSS/JS instead of serving a stale copy.
    Prefers the deployed git commit (the server deploy does ``git reset --hard origin/main``),
    falling back to the package version if git isn't available (e.g. a non-repo install)."""
    repo = Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, timeout=3, check=True,
        )
        sha = out.stdout.strip()
        if sha:
            return sha
    except Exception:  # noqa: BLE001 — git missing / not a repo / timeout → fall back
        pass
    return __version__


# Computed once at import (server start). The deploy restarts the service after the git reset,
# so this reflects the just-deployed commit.
ASSET_VER = _compute_asset_ver()


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
        "id": ev.id or None, "session_id": ev.session_id, "task_id": ev.task_id,
        "type": ev.type, "source": ev.source, "payload": ev.payload, "ts": ev.ts,
    }


def _bearer_token(request: Request) -> str:
    """Extract the bearer token from the Authorization header ('' if absent/malformed)."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _ct_eq(a: str, b: str) -> bool:
    """Constant-time string compare that tolerates non-ASCII input. ``secrets.compare_digest`` on
    ``str`` raises TypeError if either side has a non-ASCII char (e.g. a `?token=%C3%A9` query or a
    latin-1 Authorization header), which would turn a malformed unauthenticated attempt into a 500
    instead of a clean reject (codex finding). Encoding both to UTF-8 bytes first avoids that."""
    return _secrets.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8"))


# Hosts that mean "only this machine" — a personal app bound here is not network-exposed (the
# tunnel case binds loopback too, so the request-layer token is what protects an exposed app).
# NB: "0.0.0.0" and "" both mean ALL interfaces (an empty host binds INADDR_ANY) — they are NOT
# loopback and must be treated as exposed (codex acceptance finding).
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

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


def _effective_scheme(request: Request, *, trust_proxy: bool = False) -> str:
    """Client-facing scheme. X-Forwarded-Proto is consulted ONLY when ``trust_proxy`` is set (a
    trusted proxy like the Cloudflare tunnel fronts the app and terminates TLS, so request.url.scheme
    is http on the proxy→app hop). That header is client-spoofable, so on a directly-reachable app an
    attacker could otherwise send `X-Forwarded-Proto: https` to skip the http→https redirect (codex
    finding). Without trust_proxy, use the socket scheme."""
    if trust_proxy:
        fwd = request.headers.get("x-forwarded-proto", "")
        if fwd:
            return fwd.split(",")[0].strip().lower()
    return (request.url.scheme or "http").lower()


def _ensure_safe_exposure(
    cfg: Config, host: str | None = None, *, account_auth: bool = False
) -> None:
    """Fail closed if operational APIs would be exposed without protection (P0).

    `account_auth` is whether the caller will inject a per-account AuthManager that gates every
    operational endpoint (true only for the team relay built by `build_serve_app`). When it is
    false — which includes `foreman app`, whose local server is ALWAYS wired with `auth=None`
    regardless of `server.mode` (codex acceptance finding) — the only gate is the shared access
    token (FOREMAN_AUTH_TOKEN). Binding to a non-loopback host (0.0.0.0, an empty host, or a LAN
    IP) — or advertising a public_base_url — without that token would let anyone with the URL read
    sessions, dispatch work, or approve actions, so we refuse to start. An explicit
    `server.allow_insecure_bind` opt-out is exempt. `host` overrides cfg.server.host for callers
    that bind a host of their own. Raises RuntimeError when unsafe."""
    if account_auth:
        return  # per-account auth (the team relay) protects every operational endpoint
    # Strip before testing: create_app() also strips, so a whitespace-only token would otherwise
    # pass this gate here yet leave the request-layer guard wide open (codex acceptance finding).
    if (cfg.secrets.auth_token or "").strip() or cfg.server.allow_insecure_bind:
        return
    # Empty host and 0.0.0.0 both bind all interfaces → not loopback → exposed (codex finding).
    bind = (host if host is not None else cfg.server.host or "").strip().lower()
    exposed = bind not in _LOOPBACK_HOSTS or bool((cfg.server.public_base_url or "").strip())
    if exposed:
        raise RuntimeError(
            "Refusing to expose operational APIs without an access token. "
            "Set FOREMAN_AUTH_TOKEN, switch server.mode to 'team', or (trusted LAN only) set "
            "server.allow_insecure_bind: true. See deploy/README.md (issue #1 P0)."
        )


def _client_ip(request: Request, *, trust_proxy: bool = False) -> str:
    """Client IP for rate-limit bucketing. Only consult CF-Connecting-IP / X-Forwarded-For when
    ``trust_proxy`` is set (a trusted proxy like the Cloudflare tunnel fronts the app) — those
    headers are client-spoofable, so an attacker on a directly-reachable server could otherwise
    rotate them to evade the limiter and bloat its key map. Otherwise use the socket peer."""
    if trust_proxy:
        cf = request.headers.get("cf-connecting-ip", "").strip()
        if cf:
            return cf
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def _event_visible_to(ev: AgentEvent, *, session_id: str | None, account_id: str | None) -> bool:
    """Whether a bus event should be streamed to a /ws subscriber.

    ``session_id`` (optional) narrows to one session. ``account_id`` is set only in team mode, where
    the relay box's bus carries cross-tenant ``health`` events: it HARD-scopes the stream to the
    caller's account — an event must be tagged with that account_id to be forwarded — so one tenant
    never sees another's machine/health frames (DESIGN §8.4)."""
    if account_id is not None and (ev.payload or {}).get("account_id") != account_id:
        return False
    return session_id is None or ev.session_id == session_id


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
    cloud: Any = None,
) -> FastAPI:
    # In-memory log tail for the admin console's 日志管理 view (process-wide singleton). Re-attached
    # on startup because uvicorn applies its own logging dictConfig AFTER the app is built, which
    # would otherwise drop a handler attached here — so the tail stays empty under a real
    # `foreman serve` without this (caught in live preview testing). Idempotent.
    from .logbuffer import get_log_buffer

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        get_log_buffer()  # re-attach once uvicorn's logging config is in place
        yield

    app = FastAPI(title="Foreman", version=__version__, lifespan=_lifespan)

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
    app.state.cloud = cloud
    app.state.started_at = time.time()  # for the admin overview's uptime stat
    app.state.log_buffer = get_log_buffer()
    # One limiter per app instance, shared across requests (team-mode brute-force speed bump, §8.2).
    auth_limiter = SlidingWindowLimiter(_AUTH_RL_MAX_ATTEMPTS, _AUTH_RL_WINDOW_SECONDS)
    app.state.auth_limiter = auth_limiter

    def _workspace_key(path: str) -> str:
        return path.strip().replace("\\", "/").rstrip("/").casefold()

    def _clean_workspaces(items: list[Any]) -> list[WorkspaceCfg]:
        out: list[WorkspaceCfg] = []
        seen: set[str] = set()
        for item in items:
            if isinstance(item, WorkspaceCfg):
                raw_path, raw_name = item.path, item.name
            elif isinstance(item, dict):
                raw_path, raw_name = item.get("path", ""), item.get("name", "")
            else:
                continue
            path = str(raw_path or "").strip()
            if not path:
                continue
            key = _workspace_key(path)
            if key in seen:
                continue
            seen.add(key)
            out.append(WorkspaceCfg(path=path, name=str(raw_name or "").strip()))
        return out

    def _effective_workspaces() -> list[WorkspaceCfg]:
        rows: list[WorkspaceCfg] | None = None
        if store is not None and hasattr(store, "get_setting"):
            raw = store.get_setting(_WORKSPACES_SETTING)
            if raw is not None:
                try:
                    data = json.loads(raw or "[]")
                except (TypeError, ValueError):
                    data = None
                if isinstance(data, list):
                    rows = _clean_workspaces(data)
        if rows is None:
            rows = _clean_workspaces(list(cfg.workspaces))
        cfg.workspaces = rows
        return rows

    def _workspace_dicts() -> list[dict]:
        return [{"path": w.path, "name": w.name} for w in _effective_workspaces()]

    def _save_workspaces(rows: list[WorkspaceCfg]) -> list[dict]:
        if store is None or not hasattr(store, "set_setting"):
            raise HTTPException(status_code=503, detail="no local store")
        cfg.workspaces = _clean_workspaces(rows)
        data = [{"path": w.path, "name": w.name} for w in cfg.workspaces]
        store.set_setting(_WORKSPACES_SETTING, json.dumps(data, ensure_ascii=False))
        return data

    def _bool_setting(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        return default

    def _clean_agents(items: list[Any]) -> dict[str, AgentCfg]:
        base = {**default_agents(), **(cfg.agents or {})}
        out: dict[str, AgentCfg] = {}
        for name in sorted(_SUPPORTED_AGENTS):
            current = base[name]
            out[name] = AgentCfg(
                command=current.command,
                enabled=current.enabled,
                mode=current.mode,
                model=current.model,
                effort=getattr(current, "effort", ""),
                full_access=_bool_setting(getattr(current, "full_access", True), True),
            )
        for item in items:
            if isinstance(item, _AgentSettingsRow):
                raw = item.model_dump()
            elif isinstance(item, dict):
                raw = item
            else:
                continue
            name = str(raw.get("name", "")).strip()
            if name not in _SUPPORTED_AGENTS:
                continue
            current = out[name]
            mode = str(raw.get("mode", current.mode) or current.mode).strip().lower()
            effort = str(raw.get("effort", "") or "").strip().lower()
            out[name] = AgentCfg(
                command=str(raw.get("command", current.command) or current.command).strip(),
                enabled=bool(raw.get("enabled", current.enabled)),
                mode=mode if mode in {"headless", "pty"} else "headless",
                model=str(raw.get("model", "") or "").strip(),
                effort=effort if effort in _VALID_AGENT_EFFORTS else "",
                full_access=_bool_setting(
                    raw.get("full_access"), getattr(current, "full_access", True)
                ),
            )
        return out

    def _agent_config_rows() -> list[dict]:
        return [
            {
                "name": name,
                "enabled": a.enabled,
                "command": a.command,
                "mode": a.mode,
                "model": a.model,
                "effort": getattr(a, "effort", ""),
                "full_access": bool(getattr(a, "full_access", True)),
            }
            for name, a in sorted(cfg.agents.items())
            if name in _SUPPORTED_AGENTS
        ]

    def _effective_agents() -> dict[str, AgentCfg]:
        rows: dict[str, AgentCfg] | None = None
        if store is not None and hasattr(store, "get_setting"):
            raw = store.get_setting(_AGENTS_SETTING)
            if raw is not None:
                try:
                    data = json.loads(raw or "[]")
                except (TypeError, ValueError):
                    data = None
                if isinstance(data, list):
                    rows = _clean_agents(data)
        if rows is None:
            rows = _clean_agents(_agent_config_rows())
        cfg.agents = rows
        return rows

    def _sync_runner_agents() -> None:
        runner = getattr(dispatcher, "runner", None)
        if runner is not None and hasattr(runner, "sync_config"):
            runner.sync_config()

    def _save_agents(rows: list[_AgentSettingsRow]) -> list[dict]:
        if store is None or not hasattr(store, "set_setting"):
            raise HTTPException(status_code=503, detail="no local store")
        next_agents = _clean_agents(rows)
        if not any(a.enabled for a in next_agents.values()):
            raise HTTPException(status_code=400, detail="no_enabled_agent")
        cfg.agents = next_agents
        data = _agent_config_rows()
        store.set_setting(_AGENTS_SETTING, json.dumps(data, ensure_ascii=False))
        _sync_runner_agents()
        return _agent_setting_dicts()

    def _agent_setting_dicts() -> list[dict]:
        return [_agent_status(name, a) for name, a in sorted(_effective_agents().items())]

    def _which_spawnable(command: str) -> str:
        name = (command or "").strip()
        if not name:
            return ""
        if os.name != "nt":
            return shutil.which(name) or ""
        found = shutil.which(name)
        spawnable = {".exe", ".cmd", ".bat", ".com"}
        if found and Path(found).suffix.lower() in spawnable:
            return found
        path = Path(name)
        if path.suffix.lower() in spawnable and path.exists():
            return str(path)
        if path.suffix:
            return ""
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            base = Path(directory) / name
            for ext in (".cmd", ".exe", ".bat", ".com"):
                candidate = base.with_suffix(ext)
                if candidate.is_file():
                    return str(candidate)
        return ""

    def _agent_status(name: str, a: AgentCfg) -> dict:
        resolved = _which_spawnable(a.command)
        row = {
            "name": name,
            "enabled": a.enabled,
            "command": a.command,
            "mode": a.mode,
            "model": a.model,
            "effort": getattr(a, "effort", ""),
            "full_access": bool(getattr(a, "full_access", True)),
            "resolved_path": resolved,
            "ok": False,
            "version": "",
            "error": "",
        }
        if not a.enabled:
            row["error"] = "disabled"
            return row
        if not resolved:
            row["error"] = "not_found"
            return row
        try:
            proc = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            row["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            return row
        output = (proc.stdout or proc.stderr or "").strip()
        row["version"] = output.splitlines()[0] if output else ""
        row["ok"] = proc.returncode == 0
        if proc.returncode != 0:
            row["error"] = row["version"] or f"exit_{proc.returncode}"
        return row

    def _env_path() -> Path:
        return Path(getattr(cfg, "env_path", "") or ".env")

    def _save_llm_api_key(value: str) -> None:
        key = (value or "").strip()
        cfg.secrets.llm_api_key = key
        path = _env_path()
        if not key and not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        next_lines: list[str] = []
        written = False
        for line in lines:
            stripped = line.lstrip()
            is_key = stripped.startswith(f"{_LLM_KEY_ENV}=") or stripped.startswith(
                f"export {_LLM_KEY_ENV}="
            )
            if not is_key:
                next_lines.append(line)
                continue
            if key and not written:
                next_lines.append(f"{_LLM_KEY_ENV}={key}")
                written = True
        if key and not written:
            next_lines.append(f"{_LLM_KEY_ENV}={key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(next_lines) + ("\n" if next_lines else ""), encoding="utf-8")

    def _save_cloud_key(value: str) -> None:
        """Persist the relay access key to local .env (same pattern as the LLM key — never config.
        yaml, never git). Stays on the machine; the relay only ever sees its hash (§8.3 / §8.5)."""
        key = (value or "").strip()
        cfg.secrets.cloud_access_key = key
        path = _env_path()
        if not key and not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        next_lines: list[str] = []
        written = False
        for line in lines:
            stripped = line.lstrip()
            is_key = stripped.startswith(f"{_CLOUD_KEY_ENV}=") or stripped.startswith(
                f"export {_CLOUD_KEY_ENV}="
            )
            if not is_key:
                next_lines.append(line)
                continue
            if key and not written:
                next_lines.append(f"{_CLOUD_KEY_ENV}={key}")
                written = True
        if key and not written:
            next_lines.append(f"{_CLOUD_KEY_ENV}={key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(next_lines) + ("\n" if next_lines else ""), encoding="utf-8")

    def _runtime_llm_settings() -> dict:
        provider = cfg.llm.provider
        model = cfg.llm.model
        base_url = cfg.llm.base_url
        transport = cfg.llm.transport
        if store is not None and hasattr(store, "get_setting"):
            provider = store.get_setting("llm.provider") or provider
            model = store.get_setting("llm.model") or model
            base_url = store.get_setting("llm.base_url") or base_url
            transport = store.get_setting("llm.transport") or transport
        return {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "transport": transport,
            "api_key": cfg.secrets.llm_api_key,
        }

    def _preview_llm_settings(body: _LLMSettingsBody) -> dict:
        settings = _runtime_llm_settings()
        if body.provider is not None and body.provider.strip():
            settings["provider"] = body.provider.strip()
        if body.model is not None:
            settings["model"] = body.model.strip()
        if body.base_url is not None and body.base_url.strip():
            settings["base_url"] = body.base_url.strip()
        if body.transport is not None and body.transport.strip():
            settings["transport"] = body.transport.strip().lower()
        if body.api_key is not None and body.api_key.strip():
            settings["api_key"] = body.api_key.strip()
        return settings

    async def _list_model_choices(agent: str | None = None, settings: dict | None = None) -> dict:
        from foreman.shared.llm import LLMClient

        models: list[dict] = []
        seen: set[str] = set()

        def add(model_id: str, source: str) -> None:
            mid = (model_id or "").strip()
            if mid and mid not in seen:
                seen.add(mid)
                models.append({"id": mid, "source": source})

        agent_cfg = _effective_agents().get((agent or "").strip())
        if agent_cfg is not None:
            add(agent_cfg.model, "agent")
        effective = settings or _runtime_llm_settings()
        add(effective.get("model", ""), "pm")
        error = ""
        client = LLMClient(cfg, settings_resolver=lambda: effective)
        try:
            for model_id in await client.list_models():
                add(model_id, "provider")
        except Exception as exc:  # noqa: BLE001 - optional UI discovery must not block the form
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            await client.aclose()
        return {
            "models": models,
            "default": (agent_cfg.model if agent_cfg is not None else "")
            or effective.get("model", ""),
            "error": error,
        }

    _effective_workspaces()
    _effective_agents()
    _sync_runner_agents()

    def _auth_bucket(request: Request, scope: str) -> str:
        ip = _client_ip(request, trust_proxy=cfg.server.trust_proxy_headers)
        return f"{scope}:{ip}"

    def _guard_auth(request: Request, scope: str) -> str:
        """429 if this client IP is over the recent-FAILURE budget for ``scope`` (login/redeem);
        otherwise return the bucket key. Peeks only — the check itself never counts, and only
        failures are recorded (by the caller), so a correct credential is never pre-blocked unless
        the IP already failed too many times, and personal-mode 503s never accrue toward the limit.

        POLICY (intentional, not an oversight): once an IP exceeds the failure budget it is locked for
        the rest of the window — a *correct* credential from that IP is also 429'd until the window
        rolls off. Checking the credential first instead would (a) re-enable unbounded PBKDF2 CPU
        burn per guess and (b) make the throttle pointless against online guessing. We keep it
        per-IP (never per-account) so an attacker can't lock a victim out by guessing their username;
        reset-on-success + failure-only counting keep the blast radius to a genuinely abusive IP."""
        bucket = _auth_bucket(request, scope)
        if auth_limiter.over_limit(bucket):
            raise HTTPException(status_code=429, detail="too many attempts")
        return bucket

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
            # Team mode: require a VALID account token at the middleware. Most routes also call
            # require_account, but a few (e.g. GET /api/settings/language|autonomy) don't — gating
            # here closes that gap so no operational endpoint answers unauthenticated (codex finding).
            return auth.resolve_token(_bearer_token(request)) is not None
        if not _shared_token:
            return True  # personal local mode (exposure blocked at startup)
        return _ct_eq(_bearer_token(request), _shared_token)

    def _ws_authorized(token: str) -> bool:
        """Authorize a websocket (browsers can't set Authorization headers, so the token rides a
        query param). Team mode requires a valid account token; personal mode the shared token."""
        if auth is not None:
            return auth.resolve_token(token) is not None
        if not _shared_token:
            return True
        return _ct_eq(token or "", _shared_token)

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
        # Only trust X-Forwarded-Proto behind a trusted proxy, else a direct client could spoof it.
        scheme = _effective_scheme(request, trust_proxy=cfg.server.trust_proxy_headers)
        if cfg.server.force_https and scheme == "http":
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
            "agents": sorted(k for k, a in _effective_agents().items() if a.enabled),
        }
        if cfg.server.health_show_db:
            out["db"] = cfg.store.db_path
        return out

    @app.get("/api/agents")
    async def list_agents() -> list[dict]:
        """Enabled CLI agents + their configured model/effort defaults — the dispatch form's pickers
        (DESIGN §5.1). Shared-only: reads cfg, like /health (never touches the store)."""
        return [
            {
                "name": name,
                "model": a.model,
                "effort": getattr(a, "effort", ""),
                "full_access": bool(getattr(a, "full_access", True)),
            }
            for name, a in sorted(_effective_agents().items())
            if a.enabled
        ]

    @app.get("/api/settings/agents")
    async def get_agent_settings() -> list[dict]:
        """Local CLI agent settings for the Settings page, with best-effort command diagnostics."""
        return _agent_setting_dicts()

    @app.post("/api/settings/agents")
    async def set_agent_settings(body: _AgentSettingsBody) -> list[dict]:
        """Persist local CLI agent settings and refresh the live Runner adapters."""
        return _save_agents(body.agents)

    @app.get("/api/models")
    async def list_models(agent: str | None = None) -> dict:
        """Model choices for the dispatch form. Provider discovery is best-effort; configured
        defaults are returned even when `/models` is unavailable or no PM API key is set."""
        return await _list_model_choices(agent=agent)

    @app.post("/api/models/preview")
    async def preview_models(body: _LLMSettingsBody) -> dict:
        """Model choices for the settings form using unsaved provider/base URL/key fields."""
        if body.provider is not None and body.provider.strip() not in ("openai", "anthropic"):
            raise HTTPException(status_code=400, detail="bad_provider")
        return await _list_model_choices(settings=_preview_llm_settings(body))

    @app.get("/api/workspaces")
    async def list_workspaces() -> list[dict]:
        """Effective workspace allowlist for the local UI's workspace menu."""
        return _workspace_dicts()

    @app.post("/api/workspaces")
    async def save_workspace(body: _WorkspaceBody) -> list[dict]:
        """Add/update one local workspace allowlist entry from the Settings page."""
        path = body.path.strip()
        if not path:
            raise HTTPException(status_code=400, detail="bad_workspace")
        rows = _effective_workspaces()
        key = _workspace_key(path)
        updated = False
        next_rows: list[WorkspaceCfg] = []
        for row in rows:
            if _workspace_key(row.path) == key:
                next_rows.append(WorkspaceCfg(path=path, name=body.name.strip()))
                updated = True
            else:
                next_rows.append(row)
        if not updated:
            next_rows.append(WorkspaceCfg(path=path, name=body.name.strip()))
        return _save_workspaces(next_rows)

    @app.delete("/api/workspaces")
    async def delete_workspace(path: str) -> list[dict]:
        """Remove one local workspace allowlist entry from the Settings page."""
        key = _workspace_key(path)
        rows = _effective_workspaces()
        next_rows = [row for row in rows if _workspace_key(row.path) != key]
        if len(next_rows) == len(rows):
            raise HTTPException(status_code=404, detail="workspace_not_found")
        return _save_workspaces(next_rows)

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

    @app.post("/api/sessions/{session_id}/compact")
    async def compact_session(session_id: str) -> dict:
        """Compact a session timeline into a stored context summary for later follow-ups."""
        if dispatcher is None or not hasattr(dispatcher, "compact"):
            raise HTTPException(status_code=503, detail="no dispatcher")
        res = await dispatcher.compact(session_id)
        if res.get("ok"):
            return res
        status = {
            "session_not_found": 404,
            "no_context": 400,
            "no_store": 503,
        }.get(res.get("error", ""), 400)
        raise HTTPException(status_code=status, detail=res.get("error", "decline"))

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

    @app.get("/api/settings/llm")
    async def get_llm_settings() -> dict:
        """Effective PM 大脑 brain settings: a config_kv override (if a store) else the config
        default (§15). Never returns the api key; only whether one is configured."""
        settings = _runtime_llm_settings()
        return {
            "provider": settings["provider"],
            "model": settings["model"],
            "base_url": settings["base_url"],
            "transport": settings["transport"],
            "api_key_set": bool((cfg.secrets.llm_api_key or "").strip()),
        }

    @app.post("/api/settings/llm")
    async def set_llm_settings(body: _LLMSettingsBody) -> dict:
        """Switch the brain's provider/model/base_url/key at runtime (§15). Each field: a non-empty
        value sets it, an empty string clears it, None leaves it as-is."""
        if store is None or not hasattr(store, "set_setting"):
            raise HTTPException(status_code=503, detail="no local store")
        if body.provider is not None:
            provider = body.provider.strip().lower()
            if provider and provider not in ("openai", "anthropic"):
                raise HTTPException(status_code=400, detail="bad_provider")
            store.set_setting("llm.provider", provider)
        if body.model is not None:
            store.set_setting("llm.model", body.model.strip())
        if body.base_url is not None:
            store.set_setting("llm.base_url", body.base_url.strip())
        if body.transport is not None:
            transport = body.transport.strip().lower()
            if transport and transport not in ("http", "ws"):
                raise HTTPException(status_code=400, detail="bad_transport")
            store.set_setting("llm.transport", transport)
            if transport:
                cfg.llm.transport = transport
        if body.api_key is not None:
            _save_llm_api_key(body.api_key)
        return await get_llm_settings()

    def _cloud_state(extra: dict | None = None) -> dict:
        """Cloud relay connection state for the Settings card. `available` is False when this
        process can't dial a relay (e.g. the team cache server) — the UI then disables the card.
        The access key is never returned, only whether one is set."""
        url = ""
        if store is not None and hasattr(store, "get_setting"):
            url = (store.get_setting("cloud.url") or "").strip()
        out = {
            "available": cloud is not None,
            "url": url,
            "access_key_set": bool((cfg.secrets.cloud_access_key or "").strip()),
            "connected": bool(cloud.status().get("connected")) if cloud is not None else False,
            "error": (cloud.status().get("error") if cloud is not None else "") or "",
        }
        if extra:
            out.update(extra)
        return out

    @app.get("/api/settings/cloud")
    async def get_cloud_settings() -> dict:
        return _cloud_state()

    @app.post("/api/settings/cloud")
    async def set_cloud_settings(body: _CloudSettingsBody) -> dict:
        """Save the relay URL (config_kv) + access key (.env). Non-empty sets, empty clears, None
        leaves as-is — same convention as the PM brain settings."""
        if store is None or not hasattr(store, "set_setting"):
            raise HTTPException(status_code=503, detail="no local store")
        if body.url is not None:
            store.set_setting("cloud.url", body.url.strip())
        if body.access_key is not None:
            _save_cloud_key(body.access_key)
        # Reconcile the live link with the saved config: a changed/cleared URL or key must not leave
        # the old relay connection — connected OR still retrying in the background — alive with stale
        # credentials (codex review). Drop any dialer on save; the user reconnects via Connect.
        if cloud is not None:
            await asyncio.to_thread(cloud.disconnect)
        return _cloud_state()

    @app.post("/api/settings/cloud/connect")
    async def connect_cloud() -> dict:
        """Dial the configured relay (DESIGN §8.5). Opt-in — the app never connects on its own."""
        if cloud is None:
            raise HTTPException(status_code=503, detail="cloud_unavailable")
        state = await asyncio.to_thread(cloud.connect)
        return _cloud_state({"connected": bool(state.get("connected")), "error": state.get("error", "")})

    @app.post("/api/settings/cloud/disconnect")
    async def disconnect_cloud() -> dict:
        if cloud is None:
            raise HTTPException(status_code=503, detail="cloud_unavailable")
        state = await asyncio.to_thread(cloud.disconnect)
        return _cloud_state({"connected": bool(state.get("connected"))})

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
        _effective_workspaces()
        res = await dispatcher.create(
            body.goal,
            workspace=body.workspace or None,
            agent=body.agent or None,
            model=body.model or None,
            effort=body.effort or None,
            session_id=body.session_id or None,
            source=body.source or None,
        )
        if res.get("ok"):
            return res
        status = {
            "empty_goal": 400,
            "unknown_agent": 400,
            "no_workspace": 400,
            "workspace_not_allowed": 400,
            "session_not_found": 404,
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
    async def auth_login(body: _LoginBody, request: Request) -> dict:
        """PWA user login → bearer token (DESIGN §8.2). 401 on bad credentials (generic, no
        leak of which field was wrong); 429 when a client IP exceeds the attempt budget (brute-force
        guard, §8.2); 503 if no auth manager (personal mode)."""
        bucket = _guard_auth(request, "login")
        if auth is None:
            raise HTTPException(status_code=503, detail="auth not configured")
        # client_ip scopes the per-account lockout so it can't be used to lock a victim globally.
        client_ip = _client_ip(request, trust_proxy=cfg.server.trust_proxy_headers)
        res = auth.login(body.username, body.password, client_ip=client_ip)
        if res.get("ok"):
            # A successful login clears this IP's bucket, so a legitimate user (incl. a shared
            # NAT/egress IP) isn't throttled by their own earlier failures — only consecutive
            # failures accumulate.
            auth_limiter.reset(bucket)
            return {"token": res["token"], "account_id": res["account_id"], "role": res["role"]}
        auth_limiter.record(bucket)  # count the failure toward the per-IP budget (main #11)
        if res.get("error") == "locked":
            # Per-USERNAME lockout tripped (defense-in-depth alongside the per-IP limiter): uniform
            # per submitted username, so no enumeration leak (issue #1 follow-up).
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

    # ── admin dashboard (新版 Web 管理后台 — overview / sessions / processes / DB / logs) ──────
    # All admin-only. They surface OPERATIONAL metadata for the deployment's operator (who is
    # logged in, which machines are online, table sizes, recent server log lines). Secret columns
    # (any *_hash) are redacted by the store; the DB browse is read-only with a fixed table
    # allowlist. No tenant content (秘方/diffs/raw output) exists on the relay box to leak (§8.3).
    def _server_store():
        return getattr(auth, "store", None) if auth is not None else None

    @app.get("/api/admin/overview")
    async def admin_overview(request: Request) -> dict:
        """Dashboard summary cards: account counts by status, online/total processes, active login
        sessions, DB size, schema version, server version + uptime."""
        require_admin(request)
        health = auth.system_health()
        st = _server_store()
        sessions = 0
        total_procs = health["processes"]["online"]
        db: dict = {}
        if st is not None:
            sessions = len(st.get_active_auth_sessions(utc_now_iso()))
            total_procs = len(st.get_all_processes())
            db = {
                "size_bytes": st.db_size_bytes(),
                "schema_version": st.schema_version(),
                "path": getattr(st, "db_path", ""),
            }
        return {
            "version": __version__,
            "mode": "team",
            "uptime_seconds": int(time.time() - getattr(app.state, "started_at", time.time())),
            "accounts": health["accounts"],
            "processes": {"online": health["processes"]["online"], "total": total_procs},
            "active_sessions": sessions,
            "db": db,
        }

    @app.get("/api/admin/sessions")
    async def admin_sessions(request: Request) -> list[dict]:
        """Currently-logged-in PWA sessions (在线会话 / 登录账户) — account + timestamps, no token."""
        require_admin(request)
        st = _server_store()
        return st.get_active_auth_sessions(utc_now_iso()) if st is not None else []

    @app.get("/api/admin/processes")
    async def admin_processes(request: Request) -> list[dict]:
        """Every registered local process across accounts (system-wide 进程 view). Metadata only —
        the registry holds no secrets (§8.3). The owning account's username is joined in so the
        admin can see who runs what; no diffs/秘方/raw output are ever exposed."""
        require_admin(request)
        st = _server_store()
        if st is None:
            return []
        usernames = {a["id"]: a["username"] for a in auth.list_accounts()}
        return [
            {
                "id": p.id,
                "account_id": p.account_id,
                "username": usernames.get(p.account_id, "(unknown)"),
                "name": p.name,
                "online": p.online,
                "last_heartbeat": p.last_heartbeat,
                "created_at": p.created_at,
            }
            for p in st.get_all_processes()
        ]

    @app.get("/api/admin/db")
    async def admin_db(request: Request) -> dict:
        """DB overview (数据库管理): file path/size, schema version, and per-table row counts."""
        require_admin(request)
        st = _server_store()
        if st is None:
            raise HTTPException(status_code=503, detail="no server store")
        return {
            "path": getattr(st, "db_path", ""),
            "size_bytes": st.db_size_bytes(),
            "schema_version": st.schema_version(),
            "tables": st.table_stats(),
        }

    @app.get("/api/admin/db/{table}")
    async def admin_db_table(
        table: str, request: Request, limit: int = 50, offset: int = 0
    ) -> dict:
        """Read a page of one allowlisted table (read-only). *_hash columns are redacted by the
        store; an unknown/non-allowlisted table name → 404 (never reaches arbitrary SQL)."""
        require_admin(request)
        st = _server_store()
        if st is None:
            raise HTTPException(status_code=503, detail="no server store")
        res = st.browse_table(table, limit=limit, offset=offset)
        if res.get("error"):
            raise HTTPException(status_code=404, detail=res["error"])
        return res

    @app.post("/api/admin/db/maintenance")
    async def admin_db_maintenance(body: _DbMaintenanceBody, request: Request) -> dict:
        """Run a safe maintenance op: 'vacuum' (reclaim space) or 'integrity_check'. No destructive
        actions (no DROP/DELETE) are exposed over HTTP — those stay an SSH-only operation."""
        require_admin(request)
        st = _server_store()
        if st is None:
            raise HTTPException(status_code=503, detail="no server store")
        action = (body.action or "").strip().lower()
        if action == "vacuum":
            st.vacuum()
            return {"ok": True, "action": "vacuum"}
        if action == "integrity_check":
            return {"ok": True, "action": "integrity_check", "result": st.integrity_check()}
        raise HTTPException(status_code=400, detail="unknown action")

    @app.get("/api/admin/logs")
    async def admin_logs(request: Request, limit: int = 200, level: str | None = None) -> dict:
        """Recent server log lines from the in-memory ring buffer (日志管理), newest first."""
        require_admin(request)
        buf = getattr(app.state, "log_buffer", None)
        if buf is None:
            return {"records": []}
        return {"records": buf.records(limit=limit, level=level)}

    @app.post("/api/auth/redeem")
    async def auth_redeem(body: _RedeemBody, request: Request) -> dict:
        """Redeem an admin's invite (NO auth — this is how a new user bootstraps): set the
        password, activate the account, and get logged straight in (§8.2). 400 on a bad/spent/
        expired code or a too-short password; 429 when a client IP exceeds the attempt budget
        (brute-force guard, §8.2); 503 if no auth manager (personal mode)."""
        bucket = _guard_auth(request, "redeem")
        if auth is None:
            raise HTTPException(status_code=503, detail="auth not configured")
        res = auth.redeem_invite(body.code, body.password)
        if res.get("ok"):
            auth_limiter.reset(bucket)  # a good redemption clears this IP's bucket
            return {"token": res["token"], "account_id": res["account_id"], "role": res["role"]}
        auth_limiter.record(bucket)  # count only the failed redemption
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
        websocket: WebSocket, session_id: str | None = None, token: str | None = None
    ) -> None:
        """Stream events: backlog for ?session_id (from the store), then live from the bus.

        Authorized like the REST surface — the access token rides a ?token= query param since
        browsers can't set an Authorization header on a WebSocket; unauthorized → accept-then-
        close(1008). Two layers (defense-in-depth):
          • Team mode (auth manager injected): the relay box's bus carries cross-tenant ``health``
            events, so the token MUST resolve to an account and the stream is hard-filtered to it —
            never an open firehose (§8.4, main #11).
          • Personal mode (no auth manager): the shared access token gates the connection when one
            is configured (issue #1 P0)."""
        caller_account_id: str | None = None
        if auth is not None:
            account = auth.resolve_token(token or "")
            if account is None:
                await websocket.accept()
                await websocket.close(code=1008)  # policy violation: unauthenticated
                return
            caller_account_id = account.id
        elif not _ws_authorized(token or ""):
            await websocket.accept()
            await websocket.close(code=1008)  # personal-mode shared-token guard (issue #1 P0)
            return
        await websocket.accept()
        # Backlog replay is personal-mode only: team-mode events aren't account-tagged at the row
        # level, so replaying a store here couldn't be account-scoped. In team mode `store` is None
        # anyway; gating on caller_account_id is None makes that a hard invariant, not a coincidence
        # (defence-in-depth against a future auth+store combo — §8.4).
        if store is not None and session_id and caller_account_id is None:
            for e in store.get_events(session_id):
                await websocket.send_json(_row_to_dict(e))
        q = bus.subscribe_queue()

        async def pump() -> None:
            while True:
                ev = await q.get()
                if _event_visible_to(
                    ev, session_id=session_id, account_id=caller_account_id
                ):
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

    # Serve the PWA if present (mounted last so it doesn't shadow API routes). The HTML entry
    # pages go through a thin wrapper that stamps ASSET_VER into their ``?v=__VER__`` asset tokens
    # so every deploy busts Cloudflare's edge cache for the CSS/JS. HTML itself is served dynamically
    # (never edge-cached), so the new tokens reach browsers immediately. Other assets (css/js/icons)
    # fall through to StaticFiles below.
    if WEB_DIR.exists():
        # Team mode is login-gated end to end: the root serves the new Ant Design console SPA
        # (app.html), which shows a login screen until /api/auth/me succeeds — so visiting the
        # site without a session never exposes any page content. Personal mode keeps serving the
        # local dashboard (index.html) unchanged.
        is_team = (cfg.server.mode or "personal").strip().lower() == "team"

        def _render_page(name: str) -> HTMLResponse:
            html = (WEB_DIR / name).read_text(encoding="utf-8").replace("__VER__", ASSET_VER)
            return HTMLResponse(html)

        async def _serve_root() -> HTMLResponse:
            return _render_page("app.html" if is_team else "index.html")

        async def _serve_app() -> HTMLResponse:
            return _render_page("app.html")

        app.add_api_route("/", _serve_root, include_in_schema=False)
        # The new console SPA. /admin.html kept as a back-compat alias (the old admin URL).
        for _alias in ("/app.html", "/admin.html"):
            app.add_api_route(_alias, _serve_app, include_in_schema=False)
        # Legacy entry pages still version-stamped so their ?v= asset tokens bust the edge cache.
        for _page in ("index.html", "keys.html", "redeem.html"):
            def _make(name: str):
                async def _handler() -> HTMLResponse:
                    return _render_page(name)
                return _handler
            app.add_api_route(f"/{_page}", _make(_page), include_in_schema=False)

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
    # Fail closed before binding: never expose unprotected operational APIs (P0). The team relay
    # builds a per-account AuthManager below, so it's exempt; personal mode must clear the
    # token/loopback check.
    is_team = (cfg.server.mode or "personal").strip().lower() == "team"
    _ensure_safe_exposure(cfg, account_auth=is_team)
    if not is_team:
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
