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
from foreman.server.app import build_serve_app, create_app
from foreman.shared.config import Config, GatesCfg, load_config
from foreman.shared.events import EventBus, make_event

TOKEN = "s3cr3t-access-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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
@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/sessions"),
        ("get", "/api/sessions/s1/events"),
        ("get", "/api/overview"),
        ("get", "/api/approvals"),
        ("get", "/api/cards"),
        ("post", "/api/tasks"),
        ("get", "/api/definitions"),
        ("post", "/api/settings/autonomy"),
        ("post", "/hooks"),
    ],
)
def test_operational_endpoints_401_without_token(tmp_path, method, path):
    c = TestClient(_personal_app(tmp_path))
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
    cfg = load_config(tmp_path / "none.yaml")  # no auth_token
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


def test_ws_open_with_token(tmp_path):
    app = _personal_app(tmp_path)
    with TestClient(app) as c:
        with c.websocket_connect(f"/ws?session_id=s1&token={TOKEN}") as ws:
            msg = ws.receive_json()
            assert msg["session_id"] == "s1"


# ── P0: startup fails closed when exposed without protection ────────────────────────────────────
def test_build_serve_app_refuses_public_bind_without_token():
    cfg = Config()
    cfg.server.host = "0.0.0.0"  # exposed
    with pytest.raises(RuntimeError):
        build_serve_app(cfg)


def test_build_serve_app_refuses_public_base_url_without_token():
    cfg = Config()  # loopback host, but a public URL is advertised → exposed
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


def test_force_https_redirects_http(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.server.force_https = True
    c = TestClient(create_app(cfg))
    # X-Forwarded-Proto: http → redirect to https; don't follow (TestClient can't reach https).
    r = c.get("/health", headers={"X-Forwarded-Proto": "http"}, follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"].startswith("https://")


def test_force_https_passthrough_when_already_https(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    cfg.server.force_https = True
    c = TestClient(create_app(cfg))
    r = c.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200  # no redirect loop behind a TLS-terminating proxy
