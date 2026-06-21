"""Tests for the team-mode admin console (T7.2, DESIGN §8.2): admin builds users + one-time
invites, with NO self-signup — the only non-admin path to a usable password is redeeming an
admin's invite. Covers the AuthManager methods and the admin/redeem REST endpoints.

No network; the AuthManager runs against a real on-disk ServerStore (tmp file so every
connection sees the same db). Time/secret generators are injected for determinism.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.server.auth import hash_access_key, verify_password
from foreman.server.auth_manager import AuthManager
from foreman.server.store import ServerStore
from foreman.shared.config import load_config


def _store(tmp_path) -> ServerStore:
    st = ServerStore(str(tmp_path / "srv.db"))
    st.init()
    return st


def _mgr(tmp_path, **kw) -> AuthManager:
    return AuthManager(_store(tmp_path), **kw)


# ── invite_account (admin builds a passwordless user + one-time code) ─────────────────────────────
def test_invite_account_creates_invited_account_with_code(tmp_path):
    m = _mgr(tmp_path)
    res = m.invite_account("alice", role="member", display_name="Alice")
    assert res["ok"] and res["account_id"] and res["invite_code"] and res["expires_at"]
    acct = m.store.get_account(res["account_id"])
    assert acct.status == "invited" and acct.password_hash == ""  # no usable password yet
    # the code is stored only as a hash, never plaintext
    inv = m.store.get_invite_by_hash(hash_access_key(res["invite_code"]))
    assert inv is not None and inv.account_id == res["account_id"] and inv.used_at == ""


def test_invited_account_cannot_login_until_redeemed(tmp_path):
    m = _mgr(tmp_path)
    m.invite_account("alice")
    assert m.login("alice", "anything") == {"error": "invalid"}  # invited != active, no password


def test_invite_account_rejects_duplicate_and_blank(tmp_path):
    m = _mgr(tmp_path)
    m.invite_account("alice")
    assert m.invite_account("alice") == {"error": "exists"}
    assert m.invite_account("") == {"error": "bad_input"}
    # a pre-existing password account also blocks an invite of the same username
    m.create_account("bob", "pw")
    assert m.invite_account("bob") == {"error": "exists"}


# ── redeem_invite (the only non-admin path to a usable password) ──────────────────────────────────
def test_redeem_sets_password_activates_and_logs_in(tmp_path):
    m = _mgr(tmp_path)
    code = m.invite_account("alice", role="admin")["invite_code"]
    res = m.redeem_invite(code, "s3cret-pw")
    assert res["ok"] and res["token"] and res["role"] == "admin"
    acct = m.store.get_account(res["account_id"])
    assert acct.status == "active" and verify_password("s3cret-pw", acct.password_hash)
    # the returned token resolves to the now-active account
    assert m.resolve_token(res["token"]).username == "alice"
    # and a normal login now works
    assert m.login("alice", "s3cret-pw")["ok"]


def test_redeem_is_single_use(tmp_path):
    m = _mgr(tmp_path)
    code = m.invite_account("alice")["invite_code"]
    assert m.redeem_invite(code, "s3cret-pw")["ok"]
    assert m.redeem_invite(code, "another-pw") == {"error": "bad_code"}  # already spent


def test_redeem_bad_code_is_generic(tmp_path):
    m = _mgr(tmp_path)
    m.invite_account("alice")
    assert m.redeem_invite("not-a-real-code", "s3cret-pw") == {"error": "bad_code"}


def test_redeem_rejects_short_password_without_burning_code(tmp_path):
    m = _mgr(tmp_path)
    code = m.invite_account("alice")["invite_code"]
    assert m.redeem_invite(code, "short") == {"error": "bad_password"}
    # the code is NOT spent — a typo doesn't waste the invite
    assert m.redeem_invite(code, "long-enough-pw")["ok"]


def test_redeem_expired_code_fails(tmp_path):
    clock = {"t": "2026-06-20T00:00:00+00:00"}
    m = _mgr(tmp_path, now=lambda: clock["t"], invite_ttl_seconds=3600)
    code = m.invite_account("alice")["invite_code"]
    clock["t"] = "2026-06-20T02:00:00+00:00"  # two hours later -> expired
    assert m.redeem_invite(code, "s3cret-pw") == {"error": "bad_code"}


def test_redeem_fails_if_admin_disabled_account_first(tmp_path):
    m = _mgr(tmp_path)
    res = m.invite_account("alice")
    m.set_account_enabled(res["account_id"], False)
    assert m.redeem_invite(res["invite_code"], "s3cret-pw") == {"error": "bad_code"}


# ── reinvite (re-issue, burning any prior unused code) ────────────────────────────────────────────
def test_reinvite_burns_old_code_and_issues_new(tmp_path):
    m = _mgr(tmp_path)
    first = m.invite_account("alice")
    second = m.reinvite_account(first["account_id"])
    assert second["ok"] and second["invite_code"] != first["invite_code"]
    # the original code no longer works; the fresh one does
    assert m.redeem_invite(first["invite_code"], "s3cret-pw") == {"error": "bad_code"}
    assert m.redeem_invite(second["invite_code"], "s3cret-pw")["ok"]


def test_reinvite_unknown_account(tmp_path):
    m = _mgr(tmp_path)
    assert m.reinvite_account("ghost") == {"error": "not_found"}


# ── list_accounts / enable-disable ───────────────────────────────────────────────────────────────
def test_list_accounts_metadata_only(tmp_path):
    m = _mgr(tmp_path)
    m.create_account("admin", "pw", role="admin")
    m.invite_account("alice")
    listed = m.list_accounts()
    assert {a["username"] for a in listed} == {"admin", "alice"}
    for a in listed:
        assert "password_hash" not in a and "password" not in a
        assert set(a) == {"id", "username", "display_name", "role", "status", "created_at"}
    alice = next(a for a in listed if a["username"] == "alice")
    assert alice["status"] == "invited"


def test_set_account_enabled_round_trip_and_missing(tmp_path):
    m = _mgr(tmp_path)
    aid = m.create_account("alice", "pw")["account_id"]
    assert m.set_account_enabled(aid, False) == {"ok": True}
    assert m.store.get_account(aid).status == "disabled"
    assert m.login("alice", "pw") == {"error": "invalid"}  # disabled -> locked out
    assert m.set_account_enabled(aid, True) == {"ok": True}
    assert m.login("alice", "pw")["ok"]
    assert m.set_account_enabled("ghost", False) == {"error": "not_found"}


# ── REST endpoints ───────────────────────────────────────────────────────────────────────────────
def _client(tmp_path, with_auth=True):
    cfg = load_config(tmp_path / "none.yaml")
    auth = AuthManager(_store(tmp_path)) if with_auth else None
    return TestClient(create_app(cfg, auth=auth)), auth


def _admin_token(client, auth):
    auth.create_account("root", "rootpw", role="admin")
    return client.post("/api/auth/login", json={"username": "root", "password": "rootpw"}).json()[
        "token"
    ]


def test_admin_endpoints_require_admin(tmp_path):
    client, auth = _client(tmp_path)
    auth.create_account("member", "pw", role="member")
    member_token = client.post(
        "/api/auth/login", json={"username": "member", "password": "pw"}
    ).json()["token"]

    # no token -> 401
    assert client.get("/api/admin/accounts").status_code == 401
    # a member -> 403 (not admin)
    hdr = {"Authorization": f"Bearer {member_token}"}
    assert client.get("/api/admin/accounts", headers=hdr).status_code == 403
    assert client.post("/api/admin/accounts", json={"username": "x"}, headers=hdr).status_code == 403


def test_admin_create_via_invite_then_redeem(tmp_path):
    client, auth = _client(tmp_path)
    hdr = {"Authorization": f"Bearer {_admin_token(client, auth)}"}

    # admin invites a user (no password) -> invite code returned once
    r = client.post("/api/admin/accounts", json={"username": "alice"}, headers=hdr)
    assert r.status_code == 200
    code = r.json()["invite_code"]
    assert code and r.json()["account_id"]

    # the account shows up as 'invited' in the console
    accounts = client.get("/api/admin/accounts", headers=hdr).json()
    alice = next(a for a in accounts if a["username"] == "alice")
    assert alice["status"] == "invited"

    # NO self-signup: redeem needs a real admin-issued code
    assert client.post(
        "/api/auth/redeem", json={"code": "bogus", "password": "s3cret-pw"}
    ).status_code == 400

    # the user redeems the real code -> logged in, account active
    red = client.post("/api/auth/redeem", json={"code": code, "password": "s3cret-pw"})
    assert red.status_code == 200 and red.json()["token"]
    me = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {red.json()['token']}"}
    ).json()
    assert me["username"] == "alice"


def test_admin_create_with_initial_password_is_active(tmp_path):
    client, auth = _client(tmp_path)
    hdr = {"Authorization": f"Bearer {_admin_token(client, auth)}"}
    r = client.post(
        "/api/admin/accounts",
        json={"username": "bob", "password": "bob-initial-pw", "role": "member"},
        headers=hdr,
    )
    assert r.status_code == 200 and "invite_code" not in r.json()
    # active immediately — can log in with the admin-set password
    assert client.post(
        "/api/auth/login", json={"username": "bob", "password": "bob-initial-pw"}
    ).status_code == 200


def test_admin_create_duplicate_is_409(tmp_path):
    client, auth = _client(tmp_path)
    hdr = {"Authorization": f"Bearer {_admin_token(client, auth)}"}
    client.post("/api/admin/accounts", json={"username": "alice"}, headers=hdr)
    assert client.post(
        "/api/admin/accounts", json={"username": "alice"}, headers=hdr
    ).status_code == 409


def test_admin_reinvite_and_status_endpoints(tmp_path):
    client, auth = _client(tmp_path)
    hdr = {"Authorization": f"Bearer {_admin_token(client, auth)}"}
    aid = client.post("/api/admin/accounts", json={"username": "alice"}, headers=hdr).json()[
        "account_id"
    ]

    # re-invite issues a fresh code
    r = client.post(f"/api/admin/accounts/{aid}/invite", headers=hdr)
    assert r.status_code == 200 and r.json()["invite_code"]

    # disable / enable
    assert client.post(
        f"/api/admin/accounts/{aid}/status", json={"enabled": False}, headers=hdr
    ).status_code == 200
    assert client.post(
        f"/api/admin/accounts/{aid}/status", json={"enabled": True}, headers=hdr
    ).status_code == 200
    # unknown account -> 404
    assert client.post(
        "/api/admin/accounts/ghost/status", json={"enabled": False}, headers=hdr
    ).status_code == 404


def test_admin_cannot_disable_self(tmp_path):
    client, auth = _client(tmp_path)
    auth.create_account("root", "rootpw", role="admin")
    me = client.post("/api/auth/login", json={"username": "root", "password": "rootpw"}).json()
    hdr = {"Authorization": f"Bearer {me['token']}"}
    r = client.post(
        f"/api/admin/accounts/{me['account_id']}/status", json={"enabled": False}, headers=hdr
    )
    assert r.status_code == 400  # an admin can't lock themselves out


def test_admin_and_redeem_503_without_auth_manager(tmp_path):
    client, _ = _client(tmp_path, with_auth=False)
    assert client.get("/api/admin/accounts").status_code == 503
    assert client.post("/api/auth/redeem", json={"code": "x", "password": "yyyyyyyy"}).status_code == 503
