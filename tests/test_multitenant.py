"""Multi-tenant isolation (T7.4, DESIGN §8.4): every account-bound record is scoped to its
`account_id` — a user only ever sees their OWN data, and an admin sees system *health*
(aggregate counts) but never another tenant's content or secrets.

These tests are the isolation matrix that proves the invariant end-to-end across every
account-bound record type the server holds: access_keys, process_registry, auth_sessions,
invites — plus the user/admin REST surfaces. No network; the AuthManager + app run against a
real on-disk ServerStore (tmp file so every connection sees the same db).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.server.auth_manager import AuthManager
from foreman.server.store import ServerStore
from foreman.server.store.models import ProcessRegistry
from foreman.shared.config import load_config


def _store(tmp_path) -> ServerStore:
    st = ServerStore(str(tmp_path / "srv.db"))
    st.init()
    return st


def _mgr(tmp_path) -> AuthManager:
    return AuthManager(_store(tmp_path))


def _register(store, *, account_id, key_id, name, online=True) -> ProcessRegistry:
    return store.register_process(
        ProcessRegistry(
            id=key_id, account_id=account_id, access_key_id=key_id,
            name=name, online=online,
        )
    )


# ── access keys: each account lists/revokes only its own (§8.4) ───────────────────────────────────
def test_access_keys_are_scoped_per_account(tmp_path):
    m = _mgr(tmp_path)
    a = m.create_account("alice", "pw")["account_id"]
    b = m.create_account("bob", "pw")["account_id"]
    ka = m.create_access_key(a, label="alice-box")["id"]
    kb = m.create_access_key(b, label="bob-box")["id"]

    # each sees only their own
    assert [k["id"] for k in m.list_access_keys(a)] == [ka]
    assert [k["id"] for k in m.list_access_keys(b)] == [kb]

    # alice cannot revoke bob's key — generic not_found, no cross-account leak, bob's key intact
    assert m.revoke_access_key(a, kb) == {"error": "not_found"}
    assert m.store.get_access_key(kb).status == "active"
    # alice revokes her own → ok
    assert m.revoke_access_key(a, ka) == {"ok": True}
    assert m.store.get_access_key(ka).status == "revoked"


def test_listed_keys_never_expose_hash_or_plaintext(tmp_path):
    m = _mgr(tmp_path)
    a = m.create_account("alice", "pw")["account_id"]
    m.create_access_key(a, label="box")
    blob = json.dumps(m.list_access_keys(a))
    assert "key_hash" not in blob and "hash" not in blob  # metadata only (§8.4)


# ── processes / machines: a user sees only their own (§8.4 "用户只看自己的") ────────────────────────
def test_list_processes_scoped_to_account(tmp_path):
    store = _store(tmp_path)
    m = AuthManager(store)
    a = m.create_account("alice", "pw")["account_id"]
    b = m.create_account("bob", "pw")["account_id"]
    _register(store, account_id=a, key_id="ka1", name="alice-desktop", online=True)
    _register(store, account_id=a, key_id="ka2", name="alice-laptop", online=False)
    _register(store, account_id=b, key_id="kb1", name="bob-box", online=True)

    a_procs = m.list_processes(a)
    assert {p["name"] for p in a_procs} == {"alice-desktop", "alice-laptop"}
    assert all(p["id"] in {"ka1", "ka2"} for p in a_procs)  # never bob's
    assert {p["name"] for p in m.list_processes(b)} == {"bob-box"}


def test_register_process_refuses_cross_account_rehome(tmp_path):
    """Defense-in-depth (§8.4): a process row is never re-homed to another account, so a
    buggy/hostile caller can't overwrite another tenant's registry entry."""
    store = _store(tmp_path)
    _register(store, account_id="a", key_id="p1", name="alice", online=True)
    # attempt to hijack p1 for account "b" → refused, original untouched
    store.register_process(
        ProcessRegistry(id="p1", account_id="b", access_key_id="p1", name="evil", online=True)
    )
    rows = store.get_processes("a")
    assert len(rows) == 1 and rows[0].name == "alice"
    assert store.get_processes("b") == []


# ── auth sessions: a token resolves only to its own account (§8.2/§8.4) ───────────────────────────
def test_token_resolves_only_to_its_own_account(tmp_path):
    m = _mgr(tmp_path)
    m.create_account("alice", "pw")
    m.create_account("bob", "pw")
    ta = m.login("alice", "pw")["token"]
    tb = m.login("bob", "pw")["token"]
    assert m.resolve_token(ta).username == "alice"
    assert m.resolve_token(tb).username == "bob"
    assert m.resolve_token(ta).id != m.resolve_token(tb).id


# ── invites: invalidating one account's invites never touches another's (§8.2) ────────────────────
def test_invite_invalidation_is_account_scoped(tmp_path):
    m = _mgr(tmp_path)
    ca = m.invite_account("alice")["invite_code"]
    cb = m.invite_account("bob")["invite_code"]
    a = m.store.get_account_by_username("alice")
    # re-inviting alice burns ONLY alice's unused code; bob's stays redeemable
    m.reinvite_account(a.id)
    assert m.redeem_invite(ca, "s3cret-pw") == {"error": "bad_code"}  # alice's old code burned
    assert m.redeem_invite(cb, "s3cret-pw")["ok"]                      # bob's code untouched


# ── admin system health: aggregate counts only, NEVER any tenant's content (§8.4) ─────────────────
def test_system_health_is_counts_only_no_content(tmp_path):
    store = _store(tmp_path)
    m = AuthManager(store)
    a = m.create_account("alice", "pw")["account_id"]
    b = m.create_account("bob", "pw")["account_id"]
    m.invite_account("carol")                       # invited (no password yet)
    m.set_account_enabled(b, False)                 # bob disabled
    m.create_access_key(a, label="secret-machine-name")
    _register(store, account_id=a, key_id="ka1", name="secret-machine-name", online=True)
    _register(store, account_id=b, key_id="kb1", name="bob-box", online=False)

    h = m.system_health()
    assert h["accounts"]["total"] == 3
    assert h["accounts"]["active"] == 1            # only alice
    assert h["accounts"]["disabled"] == 1          # bob
    assert h["accounts"]["invited"] == 1           # carol
    assert h["processes"]["online"] == 1           # alice's machine only

    # NO content/secrets anywhere in the payload (no usernames, machine names, hashes, ids).
    blob = json.dumps(h)
    for leak in ("alice", "bob", "carol", "secret-machine-name", "hash", "password", "token"):
        assert leak not in blob


# ── REST surfaces: scoped reads + admin gate + personal-mode 503 ──────────────────────────────────
def _client(tmp_path, with_auth=True):
    cfg = load_config(tmp_path / "none.yaml")
    auth = AuthManager(_store(tmp_path)) if with_auth else None
    return TestClient(create_app(cfg, auth=auth)), auth


def _token(client, auth, username, password, role="member"):
    auth.create_account(username, password, role=role)
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]


def test_api_processes_returns_only_callers_machines(tmp_path):
    client, auth = _client(tmp_path)
    ta = _token(client, auth, "alice", "pw")
    tb = _token(client, auth, "bob", "pw")
    a = auth.resolve_token(ta).id
    b = auth.resolve_token(tb).id
    _register(auth.store, account_id=a, key_id="ka1", name="alice-box", online=True)
    _register(auth.store, account_id=b, key_id="kb1", name="bob-box", online=True)

    r = client.get("/api/processes", headers={"Authorization": f"Bearer {ta}"})
    assert r.status_code == 200
    names = {p["name"] for p in r.json()}
    assert names == {"alice-box"}  # never bob's

    # unauthenticated → 401
    assert client.get("/api/processes").status_code == 401


def test_api_admin_health_requires_admin(tmp_path):
    client, auth = _client(tmp_path)
    member = _token(client, auth, "member", "pw", role="member")
    admin = _token(client, auth, "root", "pw", role="admin")

    assert client.get("/api/admin/health").status_code == 401          # no token
    assert client.get(                                                 # member forbidden
        "/api/admin/health", headers={"Authorization": f"Bearer {member}"}
    ).status_code == 403
    r = client.get("/api/admin/health", headers={"Authorization": f"Bearer {admin}"})
    assert r.status_code == 200
    assert r.json()["accounts"]["total"] == 2 and "active" in r.json()["accounts"]


def test_isolation_endpoints_503_in_personal_mode(tmp_path):
    """Personal mode injects no AuthManager → no accounts → these team-only reads 503."""
    client, _ = _client(tmp_path, with_auth=False)
    assert client.get("/api/processes").status_code == 503
    assert client.get("/api/admin/health").status_code == 503
