"""Tests for the new admin dashboard endpoints (Ant Design console backend):
overview / sessions / processes / database (read-only, hash-redacted) / logs.

All admin-only, all team-mode (an injected AuthManager). Mirrors the team-app wiring in
test_auth.py: a real on-disk ServerStore so every connection sees the same DB.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.server.auth_manager import AuthManager
from foreman.server.store import ServerStore
from foreman.shared.config import load_config


def _setup(tmp_path):
    cfg = load_config(tmp_path / "none.yaml")
    store = ServerStore(str(tmp_path / "srv.db"))
    store.init()
    auth = AuthManager(store)
    return TestClient(create_app(cfg, auth=auth)), auth, store


def _login(client, auth, username, password, role):
    auth.create_account(username, password, role=role)
    tok = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_overview_aggregates(tmp_path):
    client, auth, _ = _setup(tmp_path)
    h = _login(client, auth, "boss", "sup3rsecret", "admin")
    auth.invite_account("pending")  # an invited (not-yet-active) account
    d = client.get("/api/admin/overview", headers=h).json()
    assert d["accounts"]["total"] == 2
    assert d["accounts"]["active"] == 1 and d["accounts"]["invited"] == 1
    assert d["active_sessions"] >= 1  # the admin's own login session
    assert d["processes"] == {"online": 0, "total": 0}
    assert "size_bytes" in d["db"] and d["version"]


def test_admin_dashboard_requires_admin(tmp_path):
    client, auth, _ = _setup(tmp_path)
    member = _login(client, auth, "alice", "pw-alice-12", "member")
    for path in (
        "/api/admin/overview", "/api/admin/sessions", "/api/admin/processes",
        "/api/admin/db", "/api/admin/db/accounts", "/api/admin/logs",
    ):
        assert client.get(path, headers=member).status_code == 403, path
    # unauthenticated → 401 (the middleware gate), not 403
    assert client.get("/api/admin/overview").status_code == 401


def test_sessions_lists_logged_in_account_no_token_leak(tmp_path):
    client, auth, _ = _setup(tmp_path)
    h = _login(client, auth, "boss", "sup3rsecret", "admin")
    sess = client.get("/api/admin/sessions", headers=h).json()
    assert any(s["username"] == "boss" and s["role"] == "admin" for s in sess)
    assert all("token_hash" not in s and "token" not in s for s in sess)


def test_db_overview_and_browse_redacts_hashes(tmp_path):
    client, auth, _ = _setup(tmp_path)
    h = _login(client, auth, "boss", "sup3rsecret", "admin")
    overview = client.get("/api/admin/db", headers=h).json()
    assert "accounts" in {t["name"] for t in overview["tables"]}
    assert overview["size_bytes"] > 0

    rows = client.get("/api/admin/db/accounts", headers=h).json()
    assert "password_hash" in rows["columns"]
    for row in rows["rows"]:
        assert row["password_hash"] in ("***", "")  # redacted, never the real hash
        assert "pbkdf2" not in str(row["password_hash"])

    # an unknown / non-allowlisted table name never reaches arbitrary SQL → 404
    assert client.get("/api/admin/db/sqlite_master", headers=h).status_code == 404
    assert client.get("/api/admin/db/does_not_exist", headers=h).status_code == 404


def test_db_maintenance_is_safe_only(tmp_path):
    client, auth, _ = _setup(tmp_path)
    h = _login(client, auth, "boss", "sup3rsecret", "admin")
    r = client.post("/api/admin/db/maintenance", json={"action": "integrity_check"}, headers=h)
    assert r.status_code == 200 and r.json()["result"] == "ok"
    assert client.post("/api/admin/db/maintenance", json={"action": "vacuum"}, headers=h).json()["ok"]
    # no destructive verbs exposed
    assert client.post(
        "/api/admin/db/maintenance", json={"action": "drop"}, headers=h
    ).status_code == 400


def test_logs_captures_recent_records(tmp_path):
    client, auth, _ = _setup(tmp_path)
    h = _login(client, auth, "boss", "sup3rsecret", "admin")
    logging.getLogger("foreman.test").warning("hello-admin-log-marker")
    recs = client.get("/api/admin/logs?limit=200", headers=h).json()["records"]
    assert any("hello-admin-log-marker" in r["msg"] for r in recs)
    # level filter narrows it
    errs = client.get("/api/admin/logs?level=ERROR", headers=h).json()["records"]
    assert all(r["level"] == "ERROR" for r in errs)


def test_logs_no_duplicate_across_logger_hierarchy(tmp_path):
    """A record on a child logger (e.g. uvicorn.error → uvicorn → root) must be buffered ONCE,
    even though the same handler is attached at multiple levels of the chain (codex finding)."""
    client, auth, _ = _setup(tmp_path)
    h = _login(client, auth, "boss", "sup3rsecret", "admin")
    logging.getLogger("uvicorn.error").warning("dup-check-marker-7f3a")
    recs = client.get("/api/admin/logs?limit=300", headers=h).json()["records"]
    matches = [r for r in recs if "dup-check-marker-7f3a" in r["msg"]]
    assert len(matches) == 1, f"expected exactly one buffered copy, got {len(matches)}"
