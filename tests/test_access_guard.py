"""Access guard + hardening tests (issue #1 P0 / P2).

Proves the fail-closed posture the acceptance review asked for:
  - personal-mode operational APIs (/api/*, /hooks, /ws) are unreachable without the shared
    access token once one is configured (FOREMAN_AUTH_TOKEN);
  - the public surface (/health, the PWA shell, login/redeem) stays open;
  - `foreman serve` / `foreman app` refuse to bind a public interface unprotected;
  - hardening headers are emitted and /health no longer leaks the DB path.

create_app stays shared-only — services are INJECTED (personal-mode wiring), exactly as the
local app wires them, so the guard is exercised against the real fully-wired API surface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from foreman.client.core.gate import Gate
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.server.app import _ensure_safe_exposure, build_serve_app, create_app
from foreman.shared.config import Config, GatesCfg, load_config
from foreman.shared.events import EventBus, make_event

TOKEN = "s3cr3t-access-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    """Keep the suite deterministic regardless of the runner's environment (codex finding): clear
    any ambient FOREMAN_AUTH_TOKEN so load_config()/Config() don't silently pick one up. The
    no-token tests below ALSO set cfg.secrets.auth_token = "" explicitly to cover a repo .env."""
    monkeypatch.delenv("FOREMAN_AUTH_TOKEN", raising=False)


def _personal_app(tmp_path, *, token: str = TOKEN):
    """A fully-wired personal-mode app (store + gate + cards) with a shared access token set."""
    cfg = load_config(tmp_path / "none.yaml")
    cfg.secrets.auth_token = token
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g1"))
    store.add_event(make_event("agent_output", "claude-code", "s1", payload={"text": "hi"}))
    gate = Gate(GatesCfg())
    return create_app(cfg, store, EventBus(), gate=gate)


# ── P0: operational endpoints fail closed without the token ────────────────────────────────────
def test_operational_endpoints_401_without_token(tmp_path):
    endpoints = [
        ("get", "/api/sessions"),
        ("get", "/api/sessions/s1/events"),
        ("get", "/api/overview"),
        ("get", "/api/approvals"),
        ("get", "/api/cards"),
        ("post", "/api/tasks"),
        ("get", "/api/workspaces"),
        ("post", "/api/workspaces"),
        ("delete", "/api/workspaces?path=D%3A%2Fproj"),
        ("get", "/api/workspaces/git-status?path=D%3A%2Fproj"),
        ("post", "/api/workspaces/init-git"),
        ("get", "/api/definitions"),
        ("post", "/api/settings/autonomy"),
        ("patch", "/api/sessions/s1"),
        ("post", "/api/sessions/s1/cancel"),
        ("delete", "/api/sessions/s1"),
        ("post", "/hooks"),
    ]
    c = TestClient(_personal_app(tmp_path))
    for method, path in endpoints:
        r = c.request(method.upper(), path, json={})
        assert r.status_code == 401, f"{method} {path} was reachable unauthenticated"
        assert r.headers.get("WWW-Authenticate") == "Bearer"


def test_operational_endpoint_ok_with_token(tmp_path):
    c = TestClient(_personal_app(tmp_path))
    r = c.get("/api/sessions", headers=AUTH)
    assert r.status_code == 200
    assert any(s["id"] == "s1" for s in r.json())


def test_wrong_token_rejected(tmp_path):
    c = TestClient(_personal_app(tmp_path))
    r = c.get("/api/sessions", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


# ── P0: the public surface stays open ──────────────────────────────────────────────────────────
def test_public_endpoints_open_without_token(tmp_path):
    c = TestClient(_personal_app(tmp_path))
    assert c.get("/health").status_code == 200
    assert c.get("/").status_code == 200  # PWA shell (static)
    assert c.get("/app.js").status_code == 200
    # auth bootstrap endpoints are public (they 503 in personal mode, but never 401)
    assert c.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 503
    assert c.get("/api/push/vapid-public-key").status_code == 200


def test_no_token_configured_is_open_for_local_use(tmp_path):
    """With no token (single-user local), operational endpoints stay open — startup is what blocks
    binding such an app to a public interface."""
    cfg = load_config(tmp_path / "none.yaml")
    cfg.secrets.auth_token = ""  # explicit no-token (deterministic regardless of env/.env)
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g"))
    c = TestClient(create_app(cfg, store, EventBus()))
    assert c.get("/api/sessions").status_code == 200


# ── P0: websocket honours the token (rides a ?token= query param) ───────────────────────────────
def test_ws_closed_without_token(tmp_path):
    app = _personal_app(tmp_path)
    with TestClient(app) as c:
        with c.websocket_connect("/ws?session_id=s1") as ws:
            with pytest.raises(Exception):
                ws.receive_json()  # closed (1008) before any backlog is sent


def test_ws_non_ascii_token_rejected_cleanly(tmp_path):
    # A non-ASCII ?token= must close (1008), not 500 via a compare_digest TypeError (codex finding).
    app = _personal_app(tmp_path)
    with TestClient(app) as c:
        with c.websocket_connect("/ws?session_id=s1&token=%C3%A9") as ws:
            with pytest.raises(Exception):
                ws.receive_json()


def test_ws_open_with_token(tmp_path):
    app = _personal_app(tmp_path)
    with TestClient(app) as c:
        with c.websocket_connect(f"/ws?session_id=s1&token={TOKEN}") as ws:
            msg = ws.receive_json()
            assert msg["session_id"] == "s1"


# ── P0: startup fails closed when exposed without protection ────────────────────────────────────
def test_build_serve_app_refuses_public_bind_without_token():
    cfg = Config()
    cfg.secrets.auth_token = ""  # explicit no-token (deterministic regardless of env/.env)
    cfg.server.host = "0.0.0.0"  # exposed
    with pytest.raises(RuntimeError):
        build_serve_app(cfg)


def test_build_serve_app_refuses_public_base_url_without_token():
    cfg = Config()  # loopback host, but a public URL is advertised → exposed
    cfg.secrets.auth_token = ""
    cfg.server.public_base_url = "https://foreman.example.com"
    with pytest.raises(RuntimeError):
        build_serve_app(cfg)


def test_build_serve_app_ok_public_bind_with_token():
    cfg = Config()
    cfg.server.host = "0.0.0.0"
    cfg.secrets.auth_token = TOKEN
    assert build_serve_app(cfg) is not None  # token protects it → allowed


def test_build_serve_app_refuses_public_bind_with_whitespace_token():
    # A whitespace-only token is stripped to "" by the request-layer guard, so it provides no
    # protection — the startup check must treat it as absent and fail closed (codex finding).
    cfg = Config()
    cfg.server.host = "0.0.0.0"
    cfg.secrets.auth_token = "   "
    with pytest.raises(RuntimeError):
        build_serve_app(cfg)


def test_build_serve_app_refuses_empty_host_without_token():
    # An empty host binds all interfaces (INADDR_ANY), not loopback — it must fail closed without a
    # token, not be mistaken for "this machine only" (codex finding).
    cfg = Config()
    cfg.secrets.auth_token = ""
    cfg.server.host = ""
    with pytest.raises(RuntimeError):
        build_serve_app(cfg)


def test_local_app_context_not_exempted_by_team_mode():
    # `foreman app` always wires the local server with auth=None regardless of server.mode, so a
    # team-mode CONFIG must not exempt the local-app exposure check (codex finding): account_auth
    # defaults False (the local-app path), so a public bind without a token still fails closed.
    cfg = Config()
    cfg.secrets.auth_token = ""
    cfg.server.mode = "team"
    with pytest.raises(RuntimeError):
        _ensure_safe_exposure(cfg, host="0.0.0.0")


def test_team_relay_with_account_auth_is_exempt():
    # The team relay (build_serve_app) DOES inject a per-account AuthManager, so it's exempt even
    # on a public bind with no shared token.
    cfg = Config()
    cfg.server.mode = "team"
    _ensure_safe_exposure(cfg, host="0.0.0.0", account_auth=True)  # must not raise


def test_build_serve_app_ok_with_insecure_opt_in():
    cfg = Config()
    cfg.server.host = "0.0.0.0"
    cfg.server.allow_insecure_bind = True
    assert build_serve_app(cfg) is not None  # operator accepted the risk (trusted LAN)


def test_loopback_personal_bind_ok_without_token():
    cfg = Config()  # default host 127.0.0.1, no token → fine (local single-user)
    assert build_serve_app(cfg) is not None


# ── P2: hardening headers + /health no longer leaks the DB path ─────────────────────────────────
def test_security_headers_present(tmp_path):
    c = TestClient(_personal_app(tmp_path))
    r = c.get("/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    # the live timeline opens a same-origin WebSocket — the default CSP must not block ws/wss
    # (codex acceptance finding: 'self' alone isn't honored for ws in every browser).
    assert "ws:" in csp and "wss:" in csp


def test_health_does_not_leak_db_path_by_default(tmp_path):
    c = TestClient(_personal_app(tmp_path))
    body = c.get("/health").json()
    assert body["ok"] is True
    assert "db" not in body


def test_health_can_opt_in_to_db_path(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.server.health_show_db = True
    c = TestClient(create_app(cfg))
    assert "db" in c.get("/health").json()


def test_hsts_emitted_only_when_enabled(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    assert "Strict-Transport-Security" not in TestClient(create_app(cfg)).get("/health").headers
    cfg.server.hsts = True
    assert "Strict-Transport-Security" in TestClient(create_app(cfg)).get("/health").headers


def test_force_https_redirects_http_behind_trusted_proxy(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.server.force_https = True
    cfg.server.trust_proxy_headers = True  # a trusted proxy fronts the app
    c = TestClient(create_app(cfg))
    # X-Forwarded-Proto: http → redirect to https; don't follow (TestClient can't reach https).
    r = c.get("/health", headers={"X-Forwarded-Proto": "http"}, follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"].startswith("https://")


def test_force_https_passthrough_when_already_https_behind_trusted_proxy(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.server.force_https = True
    cfg.server.trust_proxy_headers = True
    c = TestClient(create_app(cfg))
    r = c.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200  # no redirect loop behind a TLS-terminating proxy


def test_force_https_ignores_spoofed_proto_from_direct_client(tmp_path):
    # Without a trusted proxy, X-Forwarded-Proto is attacker-spoofable and must be ignored — a
    # direct http client can't send `X-Forwarded-Proto: https` to skip the redirect (codex finding).
    cfg = load_config(tmp_path / "none.yaml")
    cfg.server.force_https = True  # trust_proxy_headers stays False (direct exposure)
    c = TestClient(create_app(cfg))
    r = c.get("/health", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
    assert r.status_code == 308  # socket scheme is http → still redirected, spoof ignored
