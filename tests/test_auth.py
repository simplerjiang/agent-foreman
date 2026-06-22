"""Tests for team-mode auth (TASKS T3.5, DESIGN §8.2): password hashing, the AuthManager
(user login + access-key management), and the REST endpoints.

No network; the AuthManager runs against a real on-disk ServerStore (tmp file so every
connection sees the same db). Time is injected for deterministic token-expiry checks.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.server.auth import hash_access_key, hash_password, verify_password
from foreman.server.auth_manager import AuthManager
from foreman.server.store import ServerStore
from foreman.shared.config import load_config


def _store(tmp_path) -> ServerStore:
    st = ServerStore(str(tmp_path / "srv.db"))
    st.init()
    return st


def _mgr(tmp_path, **kw) -> AuthManager:
    return AuthManager(_store(tmp_path), **kw)


# ── password hashing (DESIGN §8.2) ─────────────────────────────────────────────────────────────
def test_password_hash_roundtrip_and_salting():
    h1 = hash_password("hunter2")
    h2 = hash_password("hunter2")
    assert h1 != h2  # per-password random salt
    assert h1.startswith("pbkdf2_sha256$")
    assert "hunter2" not in h1  # plaintext never present
    assert verify_password("hunter2", h1)
    assert verify_password("hunter2", h2)
    assert not verify_password("wrong", h1)


def test_verify_password_tolerates_bad_input():
    assert not verify_password("", "")
    assert not verify_password("x", "")
    assert not verify_password("x", "garbage-not-a-hash")
    assert not verify_password("x", "md5$1$aa$bb")  # unsupported algo
    assert not verify_password("x", "pbkdf2_sha256$notanint$aa$bb")


# ── accounts (admin op) ────────────────────────────────────────────────────────────────────────
def test_create_account_and_duplicate_username(tmp_path):
    m = _mgr(tmp_path)
    res = m.create_account("alice", "pw", role="admin", display_name="Alice")
    assert res["ok"] and res["account_id"]
    acct = m.store.get_account(res["account_id"])
    assert acct.username == "alice" and acct.role == "admin"
    assert acct.password_hash and acct.password_hash != "pw"  # only the hash is stored

    assert m.create_account("alice", "pw2") == {"error": "exists"}
    assert m.create_account("", "pw") == {"error": "bad_input"}
    assert m.create_account("bob", "") == {"error": "bad_input"}


def test_create_account_defaults_role_to_member(tmp_path):
    m = _mgr(tmp_path)
    res = m.create_account("bob", "pw", role="superadmin")  # unknown role -> member
    assert m.store.get_account(res["account_id"]).role == "member"


# ── login + token resolution (DESIGN §8.2) ───────────────────────────────────────────────────────
def test_login_success_issues_token(tmp_path):
    m = _mgr(tmp_path)
    m.create_account("alice", "pw")
    res = m.login("alice", "pw")
    assert res["ok"] and res["token"] and res["role"] == "member"
    # token stored only as a hash, never plaintext
    sess = m.store.get_auth_session_by_hash(hash_access_key(res["token"]))
    assert sess is not None and sess.account_id == res["account_id"]


def test_login_invalid_is_generic(tmp_path):
    m = _mgr(tmp_path)
    m.create_account("alice", "pw")
    assert m.login("alice", "wrong") == {"error": "invalid"}
    assert m.login("ghost", "pw") == {"error": "invalid"}  # unknown user, same message


def test_login_rejects_disabled_account(tmp_path):
    m = _mgr(tmp_path)
    res = m.create_account("alice", "pw")
    m.store.set_account_status(res["account_id"], "disabled")
    assert m.login("alice", "pw") == {"error": "invalid"}


# ── login brute-force throttle (issue #1 follow-up) ──────────────────────────────────────────────
def test_login_locks_out_after_repeated_failures(tmp_path):
    m = _mgr(tmp_path, max_login_failures=3, login_lockout_seconds=900)
    m.create_account("alice", "pw")
    assert m.login("alice", "wrong") == {"error": "invalid"}
    assert m.login("alice", "wrong") == {"error": "invalid"}
    assert m.login("alice", "wrong") == {"error": "invalid"}  # 3rd failure arms the lock
    # now even the CORRECT password is refused while locked
    assert m.login("alice", "pw") == {"error": "locked"}


def test_login_lockout_expires_after_window(tmp_path):
    clock = {"t": "2026-06-21T00:00:00+00:00"}
    m = AuthManager(_store(tmp_path), now=lambda: clock["t"],
                    max_login_failures=2, login_lockout_seconds=600)
    m.create_account("alice", "pw")
    m.login("alice", "wrong")
    m.login("alice", "wrong")
    assert m.login("alice", "pw") == {"error": "locked"}
    clock["t"] = "2026-06-21T00:11:00+00:00"  # 11 min later, past the 10-min window
    res = m.login("alice", "pw")
    assert res["ok"] and res["token"]


def test_login_success_resets_failure_counter(tmp_path):
    m = _mgr(tmp_path, max_login_failures=3, login_lockout_seconds=900)
    m.create_account("alice", "pw")
    m.login("alice", "wrong")
    m.login("alice", "wrong")
    assert m.login("alice", "pw")["ok"]  # a success clears the count before the lock arms
    # the counter reset, so two more failures don't immediately lock
    assert m.login("alice", "wrong") == {"error": "invalid"}
    assert m.login("alice", "wrong") == {"error": "invalid"}
    assert m.login("alice", "pw")["ok"]


def test_login_lockout_does_not_leak_account_existence(tmp_path):
    # The throttle is keyed on the submitted username — a non-existent one locks identically,
    # so "locked" never reveals whether the account exists.
    m = _mgr(tmp_path, max_login_failures=2, login_lockout_seconds=900)
    m.login("ghost", "x")
    m.login("ghost", "x")
    assert m.login("ghost", "x") == {"error": "locked"}


def test_login_failure_map_is_bounded_under_username_scan(tmp_path):
    # A credential-stuffing scan over many distinct usernames must not grow the throttle map
    # without bound (codex finding): the hard cap evicts the oldest-seen entries.
    clock = {"t": "2026-06-21T00:00:00+00:00"}
    m = AuthManager(_store(tmp_path), now=lambda: clock["t"],
                    max_login_failures=5, login_lockout_seconds=600,
                    max_tracked_login_usernames=8)
    for i in range(200):
        m._record_login_failure(f"user{i}", clock["t"])
    assert len(m._login_failures) <= 8


def test_login_failure_entries_expire_by_time(tmp_path):
    # A still-counting entry idle longer than the lockout window is pruned (sliding window), so a
    # slow scan doesn't accumulate stale entries either.
    clock = {"t": "2026-06-21T00:00:00+00:00"}
    m = AuthManager(_store(tmp_path), now=lambda: clock["t"],
                    max_login_failures=5, login_lockout_seconds=600)
    m._record_login_failure("alice", clock["t"])
    assert "alice" in m._login_failures
    clock["t"] = "2026-06-21T00:11:00+00:00"  # 11 min later, past the 10-min window
    m._record_login_failure("bob", clock["t"])  # triggers a prune
    assert "alice" not in m._login_failures and "bob" in m._login_failures


def test_login_stale_count_does_not_carry_over(tmp_path):
    # Sliding window (codex finding): two old mistypes, wait past the window, one fresh mistype must
    # NOT lock — the stale count is pruned before incrementing, so the next correct password works.
    clock = {"t": "2026-06-21T00:00:00+00:00"}
    m = AuthManager(_store(tmp_path), now=lambda: clock["t"],
                    max_login_failures=3, login_lockout_seconds=600)
    m.create_account("alice", "pw")
    m.login("alice", "wrong")  # count 1
    m.login("alice", "wrong")  # count 2
    clock["t"] = "2026-06-21T00:11:00+00:00"  # past the window → the count-2 entry is stale
    assert m.login("alice", "wrong") == {"error": "invalid"}  # fresh count 1, NOT a 3rd → no lock
    assert m.login("alice", "pw")["ok"]  # correct password still works (not locked)


def test_login_throttle_key_is_length_bounded(tmp_path):
    # The throttle key is the attacker-controlled username; cap its length so a megabyte-username
    # spray can't blow up memory despite the entry-count cap (codex finding).
    m = _mgr(tmp_path, max_login_failures=100)
    m.login("x" * 1_000_000, "wrong")
    assert m._login_failures  # an entry was recorded
    assert all(len(k) <= 256 for k in m._login_failures)


def test_resolve_token_valid_and_logout(tmp_path):
    m = _mgr(tmp_path)
    m.create_account("alice", "pw")
    token = m.login("alice", "pw")["token"]
    assert m.resolve_token(token).username == "alice"
    assert m.resolve_token("nonsense") is None
    assert m.resolve_token("") is None
    m.logout(token)
    assert m.resolve_token(token) is None  # session dropped


def test_resolve_token_expired(tmp_path):
    clock = {"t": "2026-06-20T00:00:00+00:00"}
    m = _mgr(tmp_path, now=lambda: clock["t"], token_ttl_seconds=3600)
    m.create_account("alice", "pw")
    token = m.login("alice", "pw")["token"]
    assert m.resolve_token(token) is not None  # still within the hour
    clock["t"] = "2026-06-20T02:00:00+00:00"  # two hours later -> expired
    assert m.resolve_token(token) is None
    # expired session is pruned so the table can't grow unbounded
    assert m.store.get_auth_session_by_hash(hash_access_key(token)) is None


def test_resolve_token_locked_out_when_account_disabled(tmp_path):
    m = _mgr(tmp_path)
    res = m.create_account("alice", "pw")
    token = m.login("alice", "pw")["token"]
    m.store.set_account_status(res["account_id"], "disabled")
    assert m.resolve_token(token) is None  # disabled mid-session -> immediate lockout


# ── access-key management (DESIGN §8.2 / §8.4) ───────────────────────────────────────────────────
def test_create_access_key_returns_plaintext_once_stores_hash(tmp_path):
    m = _mgr(tmp_path)
    aid = m.create_account("alice", "pw")["account_id"]
    res = m.create_access_key(aid, label="desktop")
    assert res["ok"] and res["key"] and res["label"] == "desktop"
    # the relay handshake path can find it by hash; plaintext is not stored
    row = m.store.get_access_key_by_hash(hash_access_key(res["key"]))
    assert row is not None and row.account_id == aid and row.id == res["id"]
    assert res["key"] not in row.key_hash


def test_list_access_keys_metadata_only(tmp_path):
    m = _mgr(tmp_path)
    aid = m.create_account("alice", "pw")["account_id"]
    m.create_access_key(aid, label="desktop")
    m.create_access_key(aid, label="laptop")
    listed = m.list_access_keys(aid)
    assert {k["label"] for k in listed} == {"desktop", "laptop"}
    for k in listed:  # never expose the hash or plaintext
        assert "key_hash" not in k and "key" not in k
        assert set(k) == {"id", "label", "status", "active", "last_seen_at", "expires_at", "created_at"}


def test_revoke_access_key_ownership_checked(tmp_path):
    m = _mgr(tmp_path)
    a = m.create_account("alice", "pw")["account_id"]
    b = m.create_account("bob", "pw")["account_id"]
    ka = m.create_access_key(a, label="alice-box")["id"]
    kb = m.create_access_key(b, label="bob-box")["id"]

    # bob cannot revoke alice's key -> not_found, alice's key untouched
    assert m.revoke_access_key(b, ka) == {"error": "not_found"}
    assert m.store.get_access_key(ka).status == "active"
    # bob revokes his own
    assert m.revoke_access_key(b, kb) == {"ok": True}
    assert m.store.get_access_key(kb).status == "revoked"
    # missing key -> not_found
    assert m.revoke_access_key(a, "ghost") == {"error": "not_found"}


# ── REST endpoints ───────────────────────────────────────────────────────────────────────────────
def _client(tmp_path, with_auth=True, **auth_kw):
    cfg = load_config(tmp_path / "none.yaml")
    auth = AuthManager(_store(tmp_path), **auth_kw) if with_auth else None
    app = create_app(cfg, auth=auth)
    return TestClient(app), auth


def test_login_endpoint_429_after_lockout(tmp_path):
    client, auth = _client(tmp_path, max_login_failures=2, login_lockout_seconds=900)
    auth.create_account("alice", "pw")
    body = {"username": "alice", "password": "wrong"}
    assert client.post("/api/auth/login", json=body).status_code == 401
    assert client.post("/api/auth/login", json=body).status_code == 401  # arms the lock
    # locked → 429 even with the correct password (uniform, no enumeration leak)
    r = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert r.status_code == 429


def test_endpoints_login_and_key_lifecycle(tmp_path):
    client, auth = _client(tmp_path)
    auth.create_account("alice", "pw")

    # bad creds -> 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": "x"}).status_code == 401
    # good creds -> token
    r = client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    assert r.status_code == 200
    token = r.json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}

    # protected endpoints reject missing/bad tokens
    assert client.get("/api/keys").status_code == 401
    assert client.get("/api/keys", headers={"Authorization": "Bearer nope"}).status_code == 401

    # me
    me = client.get("/api/auth/me", headers=hdr).json()
    assert me["username"] == "alice"

    # mint a key (plaintext returned once)
    created = client.post("/api/keys", json={"label": "desktop"}, headers=hdr)
    assert created.status_code == 200 and created.json()["key"]
    key_id = created.json()["id"]

    # list (no plaintext/hash)
    listed = client.get("/api/keys", headers=hdr).json()
    assert listed[0]["label"] == "desktop" and "key" not in listed[0]

    # revoke
    assert client.delete(f"/api/keys/{key_id}", headers=hdr).status_code == 200
    assert client.get("/api/keys", headers=hdr).json()[0]["status"] == "revoked"

    # logout invalidates the token
    assert client.post("/api/auth/logout", headers=hdr).status_code == 200
    assert client.get("/api/auth/me", headers=hdr).status_code == 401


def test_endpoints_cross_account_revoke_is_404(tmp_path):
    client, auth = _client(tmp_path)
    auth.create_account("alice", "pw")
    auth.create_account("bob", "pw")
    alice_key = auth.create_access_key(auth.store.get_account_by_username("alice").id)["id"]

    bob_token = client.post("/api/auth/login", json={"username": "bob", "password": "pw"}).json()["token"]
    r = client.delete(f"/api/keys/{alice_key}", headers={"Authorization": f"Bearer {bob_token}"})
    assert r.status_code == 404
    # alice's key remains active
    assert auth.store.get_access_key(alice_key).status == "active"


def test_login_is_rate_limited_per_ip(tmp_path):
    """A burst of login attempts from one IP is throttled (online brute-force guard, issue #10).

    The first _AUTH_RL_MAX_ATTEMPTS attempts get the normal 401; the next is refused with 429 before
    the password is even checked. (TestClient shares one client host, so all land in one IP bucket.)"""
    from foreman.server.app import _AUTH_RL_MAX_ATTEMPTS

    client, auth = _client(tmp_path)
    auth.create_account("alice", "pw")
    codes = [
        client.post("/api/auth/login", json={"username": "alice", "password": "wrong"}).status_code
        for _ in range(_AUTH_RL_MAX_ATTEMPTS + 1)
    ]
    assert codes[:_AUTH_RL_MAX_ATTEMPTS] == [401] * _AUTH_RL_MAX_ATTEMPTS
    assert codes[_AUTH_RL_MAX_ATTEMPTS] == 429


def test_redeem_is_rate_limited_per_ip(tmp_path):
    """Invite redemption is throttled the same way (separate bucket from login)."""
    from foreman.server.app import _AUTH_RL_MAX_ATTEMPTS

    client, _ = _client(tmp_path)
    codes = [
        client.post("/api/auth/redeem", json={"code": "nope", "password": "longenough"}).status_code
        for _ in range(_AUTH_RL_MAX_ATTEMPTS + 1)
    ]
    assert codes[_AUTH_RL_MAX_ATTEMPTS] == 429


def test_rate_limit_not_evaded_by_spoofed_forwarded_headers(tmp_path):
    """With trust_proxy_headers off (default), CF-Connecting-IP / X-Forwarded-For are ignored, so an
    attacker can't rotate them to mint fresh buckets and bypass the limiter (issue #10 hardening)."""
    from foreman.server.app import _AUTH_RL_MAX_ATTEMPTS

    client, auth = _client(tmp_path)
    auth.create_account("alice", "pw")
    codes = [
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "x"},
            headers={"X-Forwarded-For": f"10.0.0.{i}", "CF-Connecting-IP": f"10.1.0.{i}"},
        ).status_code
        for i in range(_AUTH_RL_MAX_ATTEMPTS + 1)
    ]
    assert codes[_AUTH_RL_MAX_ATTEMPTS] == 429  # all land in one (socket-peer) bucket regardless


def test_successful_login_resets_the_bucket(tmp_path):
    """A success clears the IP's budget so a legitimate user (incl. shared NAT/egress) isn't locked
    out by their own earlier attempts — only consecutive failures accumulate (issue #10 hardening)."""
    client, auth = _client(tmp_path)
    auth.create_account("alice", "pw")
    for _ in range(9):
        client.post("/api/auth/login", json={"username": "alice", "password": "x"})  # 9 failures
    assert client.post("/api/auth/login", json={"username": "alice", "password": "pw"}).status_code == 200
    # bucket reset on success → the next failure is a normal 401, not a 429
    assert client.post("/api/auth/login", json={"username": "alice", "password": "x"}).status_code == 401


def test_personal_mode_login_stays_503_under_load(tmp_path):
    """With no auth manager, repeated login posts keep returning 503 — never flip to 429. Failures
    are recorded only AFTER the credential check, which personal mode 503s before reaching, so the
    limiter never accrues (issue #10 hardening: no 503→429 inconsistency)."""
    from foreman.server.app import _AUTH_RL_MAX_ATTEMPTS

    client, _ = _client(tmp_path, with_auth=False)
    codes = {
        client.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code
        for _ in range(_AUTH_RL_MAX_ATTEMPTS + 5)
    }
    assert codes == {503}


def test_endpoints_503_without_auth_manager(tmp_path):
    client, _ = _client(tmp_path, with_auth=False)
    assert client.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 503
    assert client.get("/api/keys").status_code == 503
    assert client.get("/api/auth/me").status_code == 503
    # logout is always ok (idempotent), even with no auth manager
    assert client.post("/api/auth/logout").status_code == 200


# ── access-key expiry (DESIGN §8.4 "可设有效期") ──────────────────────────────────────────────────
def test_access_key_expiry_computed_and_enforced_by_relay(tmp_path):
    """`expires_in_days` stamps an absolute expiry; the relay handshake (§8.5) refuses the key
    once it lapses — so a stale key stops working on its own, even before it's revoked."""
    from foreman.server.relay import Relay

    store = _store(tmp_path)
    m = AuthManager(store, now=lambda: "2026-06-21T00:00:00+00:00")
    acct = m.create_account("alice", "pw")["account_id"]
    res = m.create_access_key(acct, label="laptop", expires_in_days=2)
    assert res["expires_at"] == "2026-06-23T00:00:00+00:00"
    key = res["key"]

    # before expiry: the relay authenticates the key to its account
    relay_ok = Relay(store, now=lambda: "2026-06-22T00:00:00+00:00")
    auth_ok = relay_ok.authenticate({"access_key": key})
    assert auth_ok.ok and auth_ok.account_id == acct

    # after expiry: rejected (never marks the process online)
    relay_late = Relay(store, now=lambda: "2026-06-24T00:00:00+00:00")
    auth_late = relay_late.authenticate({"access_key": key})
    assert not auth_late.ok and "expired" in auth_late.reason


def test_access_key_no_expiry_by_default(tmp_path):
    """Omitting the knob → a key with no time limit (expires_at empty); the relay never expires it."""
    m = _mgr(tmp_path)
    acct = m.create_account("alice", "pw")["account_id"]
    res = m.create_access_key(acct, label="server")
    assert res["expires_at"] == ""
    # a 0 / negative day count is treated as "no expiry" (UI sends 0 for "never")
    assert m.create_access_key(acct, expires_in_days=0)["expires_at"] == ""
    assert m.create_access_key(acct, expires_in_days=-5)["expires_at"] == ""


def test_endpoints_mint_key_with_expiry(tmp_path):
    client, auth = _client(tmp_path)
    auth.create_account("alice", "pw")
    token = client.post("/api/auth/login", json={"username": "alice", "password": "pw"}).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}

    # mint with an expiry → response + listing both carry expires_at
    created = client.post("/api/keys", json={"label": "phone", "expires_in_days": 30}, headers=hdr)
    assert created.status_code == 200
    assert created.json()["expires_at"]  # non-empty ISO timestamp
    listed = client.get("/api/keys", headers=hdr).json()
    assert listed[0]["expires_at"] == created.json()["expires_at"]

    # mint without an expiry → no time limit
    plain = client.post("/api/keys", json={"label": "desktop"}, headers=hdr)
    assert plain.status_code == 200 and plain.json()["expires_at"] == ""

    # an absurd expiry is rejected (422), not a timedelta-overflow 500
    huge = client.post("/api/keys", json={"expires_in_days": 10**8}, headers=hdr)
    assert huge.status_code == 422
