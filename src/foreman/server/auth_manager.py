"""Team-mode auth service: user login + access-key management (DESIGN §8.2 / T3.5).

Two distinct credential kinds (§8.2), both handled here on the SERVER side:

  • **User login** — a human signs into the PWA with username + password and gets a bearer
    token (an `auth_sessions` row, hash stored). Used to call the REST API as that account.
  • **Access key** — a logged-in user mints keys (one per machine, many per account) and
    pastes them into their local process; the relay handshake (§8.5) authenticates by hash.

Accounts are created by an admin (no self-signup, §8.2); the admin console UI is T7.2.

This module imports only the server store + server.auth + shared, so app.py can stay
shared-only and inject an AuthManager the same way it injects the Gate/Relay (DESIGN §14).
The server holds ONLY hashes — never a password, login token, or access key in plaintext
(§8.4). Time / id / secret generators are injectable for deterministic tests.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

from foreman.shared.events import utc_now_iso

from .auth import (
    generate_access_key,
    generate_token,
    hash_access_key,
    hash_password,
    verify_password,
)
from .store.models import AccessKey, Account, AuthSession, Invite

# 30 days: a phone PWA stays logged in for weeks (the device, not the password, is the factor).
DEFAULT_TOKEN_TTL_SECONDS = 30 * 24 * 3600

# 7 days: an admin's invite link is valid for a week, then the admin must re-issue one.
DEFAULT_INVITE_TTL_SECONDS = 7 * 24 * 3600

# Minimum length for a self-set password (redeem path). Admin-set initial passwords go through
# create_account, which only requires non-empty (the admin owns that choice); but a user setting
# their OWN password via an invite gets a modest floor so a one-char password can't slip in.
MIN_PASSWORD_LEN = 8

# A throwaway hash used to spend the same PBKDF2 time on the missing/disabled-user login path,
# so response timing can't be used to enumerate valid usernames (computed once at import).
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def _iso_plus_seconds(iso: str, seconds: int) -> str:
    """Add `seconds` to an ISO8601 UTC timestamp, returning ISO8601."""
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat()


def _is_expired(expires_at: str, now: str) -> bool:
    """True if `expires_at` is at/before `now`. Parses both to datetimes (robust to the optional
    microsecond field that makes a naive lexical compare fragile); a malformed/blank expiry is
    treated as expired so a corrupt row fails closed."""
    if not expires_at:
        return False  # no expiry set -> never expires by time
    try:
        return datetime.fromisoformat(expires_at) <= datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return True


class AuthManager:
    def __init__(
        self,
        store,
        *,
        now=utc_now_iso,
        gen_id=lambda: uuid.uuid4().hex,
        gen_token=generate_token,
        gen_key=generate_access_key,
        gen_invite=generate_access_key,
        token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        invite_ttl_seconds: int = DEFAULT_INVITE_TTL_SECONDS,
    ) -> None:
        self.store = store
        self._now = now
        self._gen_id = gen_id
        self._gen_token = gen_token
        self._gen_key = gen_key
        self._gen_invite = gen_invite
        self._ttl = token_ttl_seconds
        self._invite_ttl = invite_ttl_seconds

    # ── accounts (admin op — DESIGN §8.2: no self-signup) ────────────────────────────────────
    def create_account(
        self, username: str, password: str, *, role: str = "member", display_name: str = ""
    ) -> dict:
        """Create an account with a hashed password. Returns {"ok", "account_id"} or an error
        ({"error": "exists" | "bad_input"}). Usernames are unique."""
        username = (username or "").strip()
        if not username or not password:
            return {"error": "bad_input"}
        if self.store.get_account_by_username(username) is not None:
            return {"error": "exists"}
        account_id = self._gen_id()
        self.store.add_account(
            Account(
                id=account_id,
                username=username,
                display_name=display_name or username,
                role="admin" if role == "admin" else "member",
                status="active",
                password_hash=hash_password(password),
                created_at=self._now(),
            )
        )
        return {"ok": True, "account_id": account_id}

    def list_accounts(self) -> list[dict]:
        """Every account for the admin console — metadata only, NEVER the password hash (§8.4
        admin sees system health, not anyone's secrets). Oldest first (creation order)."""
        return [
            {
                "id": a.id,
                "username": a.username,
                "display_name": a.display_name,
                "role": a.role,
                "status": a.status,
                "created_at": a.created_at,
            }
            for a in self.store.get_accounts()
        ]

    def set_account_enabled(self, account_id: str, enabled: bool) -> dict:
        """Admin disables/re-enables an account. A disabled account can't log in and any live
        token is rejected immediately (resolve_token re-checks status). Returns {"ok"} or
        {"error": "not_found"}."""
        if self.store.get_account(account_id) is None:
            return {"error": "not_found"}
        self.store.set_account_status(account_id, "active" if enabled else "disabled")
        return {"ok": True}

    # ── invites (admin builds a user, hands out a one-time code — DESIGN §8.2) ────────────────
    def invite_account(
        self, username: str, *, role: str = "member", display_name: str = "",
        invite_ttl_seconds: int | None = None,
    ) -> dict:
        """Admin creates a passwordless account in 'invited' status and issues a one-time invite
        code (DESIGN §8.2: no self-signup — redeeming an admin's invite is the ONLY way to a
        usable password). The account can't log in until the code is redeemed. Returns
        {"ok", "account_id", "invite_code", "expires_at"} — invite_code is plaintext shown ONCE
        (only its hash is stored, §8.4) — or {"error": "exists" | "bad_input"}."""
        username = (username or "").strip()
        if not username:
            return {"error": "bad_input"}
        if self.store.get_account_by_username(username) is not None:
            return {"error": "exists"}
        account_id = self._gen_id()
        self.store.add_account(
            Account(
                id=account_id,
                username=username,
                display_name=display_name or username,
                role="admin" if role == "admin" else "member",
                status="invited",          # no usable password yet → can't log in until redeemed
                password_hash="",
                created_at=self._now(),
            )
        )
        invite = self._issue_invite(account_id, invite_ttl_seconds)
        return {"ok": True, "account_id": account_id, **invite}

    def reinvite_account(
        self, account_id: str, *, invite_ttl_seconds: int | None = None
    ) -> dict:
        """Re-issue an invite for an existing account (re-invite / admin-driven password reset).
        Any prior UNused invite for this account is burned first, so there's always exactly one
        live code. Does NOT change the account's status (redeeming the new code activates it).
        Returns {"ok", "invite_code", "expires_at"} or {"error": "not_found"}."""
        if self.store.get_account(account_id) is None:
            return {"error": "not_found"}
        self.store.invalidate_account_invites(account_id, self._now())
        invite = self._issue_invite(account_id, invite_ttl_seconds)
        return {"ok": True, **invite}

    def _issue_invite(self, account_id: str, ttl_seconds: int | None) -> dict:
        """Mint + persist one invite (hash only). Returns {"invite_code", "expires_at"}."""
        ttl = self._invite_ttl if ttl_seconds is None else ttl_seconds
        code = self._gen_invite()
        now = self._now()
        expires_at = _iso_plus_seconds(now, ttl) if ttl and ttl > 0 else ""
        self.store.add_invite(
            Invite(
                id=self._gen_id(),
                code_hash=hash_access_key(code),
                account_id=account_id,
                expires_at=expires_at,
                used_at="",
            )
        )
        return {"invite_code": code, "expires_at": expires_at}

    def redeem_invite(self, code: str, password: str) -> dict:
        """Redeem an admin-issued invite: set the account's password, activate it, burn the code,
        and log the user straight in (DESIGN §8.2). This is the ONLY non-admin path to a usable
        password — what 'no self-signup' means.

        Fail-closed: a missing / already-used / expired code returns a generic {"error":
        "bad_code"} (never leaks whether a code existed); a too-short password returns
        {"error": "bad_password"} BEFORE the code is spent, so a typo doesn't burn the invite.
        Returns a login result {"ok", "token", "account_id", "role"} on success."""
        if len(password or "") < MIN_PASSWORD_LEN:
            return {"error": "bad_password"}
        invite = self.store.get_invite_by_hash(hash_access_key(code or ""))
        now = self._now()
        if invite is None or invite.used_at or _is_expired(invite.expires_at, now):
            return {"error": "bad_code"}
        account = self.store.get_account(invite.account_id)
        if account is None or account.status == "disabled":
            return {"error": "bad_code"}  # admin disabled/deleted the account before redemption
        self.store.set_account_password(account.id, hash_password(password))
        self.store.set_account_status(account.id, "active")
        self.store.mark_invite_used(invite.id, now)
        return self._issue_login_token(account)

    # ── user login (DESIGN §8.2) ─────────────────────────────────────────────────────────────
    def login(self, username: str, password: str) -> dict:
        """Verify credentials and issue a bearer token. Returns {"ok", "token", "account_id",
        "role"} or {"error": "invalid"}.

        A single generic "invalid" is returned for unknown user / wrong password / disabled
        account — never leak which one failed. To avoid username enumeration via response timing,
        the missing/disabled-user path still spends one PBKDF2 verify against a throwaway hash, so
        every login costs the same regardless of whether the account exists."""
        account = self.store.get_account_by_username((username or "").strip())
        usable = account is not None and account.status == "active"
        pw_hash = account.password_hash if usable else _DUMMY_PASSWORD_HASH
        password_ok = verify_password(password or "", pw_hash)
        if not usable or not password_ok:
            return {"error": "invalid"}
        return self._issue_login_token(account)

    def _issue_login_token(self, account: Account) -> dict:
        """Mint + persist a bearer token (only its hash) and return the login result. Shared by
        login() and redeem_invite() so a freshly-redeemed user lands logged in."""
        token = self._gen_token()
        now = self._now()
        self.store.add_auth_session(
            AuthSession(
                id=self._gen_id(),
                account_id=account.id,
                token_hash=hash_access_key(token),  # sha256 — high-entropy token, like keys
                created_at=now,
                expires_at=_iso_plus_seconds(now, self._ttl),
            )
        )
        return {"ok": True, "token": token, "account_id": account.id, "role": account.role}

    def resolve_token(self, token: str) -> Account | None:
        """Resolve a bearer token to its (active, non-expired) account, or None.

        Returns None for missing/unknown/expired tokens and for disabled accounts, so a
        disabled or deleted user is locked out immediately even if their token is unexpired."""
        if not token:
            return None
        sess = self.store.get_auth_session_by_hash(hash_access_key(token))
        if sess is None:
            return None
        if _is_expired(sess.expires_at, self._now()):
            self.store.delete_auth_session(sess.token_hash)  # prune so the table can't grow forever
            return None
        account = self.store.get_account(sess.account_id)
        if account is None or account.status != "active":
            return None
        return account

    def logout(self, token: str) -> None:
        """Invalidate a bearer token (drop its session row)."""
        if token:
            self.store.delete_auth_session(hash_access_key(token))

    # ── access-key management (a logged-in user mints/lists/revokes their own — §8.2) ─────────
    def create_access_key(
        self, account_id: str, label: str = "", *,
        expires_at: str = "", expires_in_days: int | None = None,
    ) -> dict:
        """Mint an access key for an account. The plaintext is returned exactly ONCE here and
        never stored (only its hash is) — DESIGN §8.4.

        Expiry (DESIGN §8.4 "可设有效期"): pass `expires_in_days` (the UI knob) to expire N days
        from now, or an explicit ISO `expires_at`; omit both for a key that never expires by time.
        The relay handshake (§8.5) refuses an expired key, so a lost/stale key stops working on its
        own even before it's revoked. Returns {"ok", "id", "key", "label", "expires_at"}."""
        now = self._now()
        if not expires_at and expires_in_days and expires_in_days > 0:
            expires_at = _iso_plus_seconds(now, expires_in_days * 24 * 3600)
        plaintext = self._gen_key()
        key_id = self._gen_id()
        self.store.add_access_key(
            AccessKey(
                id=key_id,
                account_id=account_id,
                key_hash=hash_access_key(plaintext),
                label=label or "",
                status="active",
                expires_at=expires_at or "",
                created_at=now,
            )
        )
        return {
            "ok": True, "id": key_id, "key": plaintext,
            "label": label or "", "expires_at": expires_at or "",
        }

    def list_access_keys(self, account_id: str) -> list[dict]:
        """An account's keys for the management UI — metadata only, NEVER the hash or plaintext
        (the plaintext was shown once at creation and isn't recoverable)."""
        out: list[dict] = []
        for k in self.store.get_access_keys(account_id):
            out.append(
                {
                    "id": k.id,
                    "label": k.label,
                    "status": k.status,
                    "active": k.status == "active",
                    "last_seen_at": k.last_seen_at,
                    "expires_at": k.expires_at,
                    "created_at": k.created_at,
                }
            )
        return out

    def revoke_access_key(self, account_id: str, key_id: str) -> dict:
        """Revoke ONE of the caller's own keys (others keep working — §7.2). Ownership-checked:
        revoking a key you don't own returns {"error": "not_found"} (no cross-account leak —
        §8.4) rather than touching another tenant's key. Returns {"ok": True} on success."""
        row = self.store.get_access_key(key_id)
        if row is None or row.account_id != account_id:
            return {"error": "not_found"}
        self.store.revoke_access_key(key_id)
        return {"ok": True}
