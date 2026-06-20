"""Server database session/engine wrapper + r/w helpers (team/relay mode).

Separate from the client's local store. This is the SERVER side of DESIGN §7.2: it knows
WHO (accounts), WHICH MACHINE may connect (access_keys, hash-only), and WHAT IS ONLINE
(process_registry) so the relay can route a PWA to the right local process (§8.5).

It deliberately holds NO 秘方 (definitions), no full diffs/raw output, and NO per-user LLM
keys — those live in each user's local .env (§8.3/§8.4).

invites / cache_sessions / cache_cards are placeholder tables for later phases (admin
invites T7.2, display caches T7.5); only their tables are created here, no helpers yet.
"""

from __future__ import annotations

from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, col, create_engine, select

from foreman.shared.events import utc_now_iso

from . import models  # noqa: F401  (registers server tables on SQLModel.metadata)
from .models import Account, AccessKey, AuthSession, ProcessRegistry, ServerSchemaVersion

# v2 adds Account.password_hash + the auth_sessions table (T3.5 user login / access-key mgmt).
SERVER_SCHEMA_VERSION = 2


class ServerStore:
    def __init__(self, db_path: str = "foreman-server.db") -> None:
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )

    def init(self) -> None:
        """Create ONLY the server tables (scoped, so a shared metadata never pulls in client
        tables) and record the current schema version (DESIGN §7.2 / §11.1)."""
        SQLModel.metadata.create_all(
            self.engine, tables=[m.__table__ for m in models.SERVER_TABLES]
        )
        with self.session() as s:
            if s.get(ServerSchemaVersion, SERVER_SCHEMA_VERSION) is None:
                s.add(ServerSchemaVersion(version=SERVER_SCHEMA_VERSION, applied_at=utc_now_iso()))
                s.commit()

    def session(self) -> DBSession:
        # expire_on_commit=False: returned ORM rows stay readable after the session closes.
        return DBSession(self.engine, expire_on_commit=False)

    # ── accounts (DESIGN §7.2) ───────────────────────────────────────────────────────────────
    def add_account(self, account: Account) -> Account:
        """Admin creates an account (no self-signup — DESIGN §8). Stamps created_at if unset."""
        if not account.created_at:
            account.created_at = utc_now_iso()
        with self.session() as s:
            s.add(account)
            s.commit()
        return account

    def get_account(self, account_id: str) -> Account | None:
        with self.session() as s:
            return s.get(Account, account_id)

    def get_account_by_username(self, username: str) -> Account | None:
        with self.session() as s:
            return s.exec(select(Account).where(Account.username == username)).first()

    def get_accounts(self) -> list[Account]:
        with self.session() as s:
            return list(s.exec(select(Account).order_by(col(Account.created_at))).all())

    def set_account_status(self, account_id: str, status: str) -> None:
        """Disable/re-enable an account ('active' | 'disabled'). No-op if it doesn't exist."""
        with self.session() as s:
            row = s.get(Account, account_id)
            if row is not None:
                row.status = status
                s.add(row)
                s.commit()

    def set_account_password(self, account_id: str, password_hash: str) -> None:
        """Store a pbkdf2 password hash for an account (admin set / user change). No-op if the
        account is missing. The caller hashes the plaintext (auth.hash_password) — only the hash
        is ever persisted (DESIGN §8.2)."""
        with self.session() as s:
            row = s.get(Account, account_id)
            if row is not None:
                row.password_hash = password_hash
                s.add(row)
                s.commit()

    # ── access keys (one machine per key; hash only — DESIGN §7.2 / §8.3) ────────────────────
    def add_access_key(self, key: AccessKey) -> AccessKey:
        """Register an access key. ONLY the hash is persisted (caller hashes the plaintext,
        which is shown to the user exactly once). Stamps created_at if unset."""
        if not key.created_at:
            key.created_at = utc_now_iso()
        with self.session() as s:
            s.add(key)
            s.commit()
        return key

    def get_access_keys(self, account_id: str) -> list[AccessKey]:
        """All keys for an account (active + revoked), newest first."""
        with self.session() as s:
            return list(
                s.exec(
                    select(AccessKey)
                    .where(AccessKey.account_id == account_id)
                    .order_by(col(AccessKey.created_at).desc())
                ).all()
            )

    def get_access_key(self, key_id: str) -> AccessKey | None:
        """Look up one key by id (the ownership-checked revoke path — DESIGN §8.2)."""
        with self.session() as s:
            return s.get(AccessKey, key_id)

    def get_access_key_by_hash(self, key_hash: str) -> AccessKey | None:
        """Look up a key by its hash (the relay handshake path — DESIGN §8.5). Returns the row
        regardless of status; the caller checks status/expiry so revoked keys can be reported."""
        with self.session() as s:
            return s.exec(select(AccessKey).where(AccessKey.key_hash == key_hash)).first()

    def revoke_access_key(self, key_id: str) -> None:
        """Revoke a single key (one machine cut off, others keep working — DESIGN §7.2)."""
        with self.session() as s:
            row = s.get(AccessKey, key_id)
            if row is not None:
                row.status = "revoked"
                s.add(row)
                s.commit()

    def touch_access_key(self, key_id: str, when: str | None = None) -> None:
        """Record that this key was just seen (last_seen_at), for the admin console."""
        with self.session() as s:
            row = s.get(AccessKey, key_id)
            if row is not None:
                row.last_seen_at = when or utc_now_iso()
                s.add(row)
                s.commit()

    # ── auth sessions (PWA user login — DESIGN §8.2) ─────────────────────────────────────────
    def add_auth_session(self, sess: AuthSession) -> AuthSession:
        """Persist a logged-in session. ONLY the token hash is stored (caller hashes the
        plaintext, shown to the browser once). Stamps created_at if unset."""
        if not sess.created_at:
            sess.created_at = utc_now_iso()
        with self.session() as s:
            s.add(sess)
            s.commit()
        return sess

    def get_auth_session_by_hash(self, token_hash: str) -> AuthSession | None:
        """Resolve a bearer token (by its hash) to its session row. Returns regardless of
        expiry; the caller checks expires_at so expired tokens can be reported/pruned."""
        with self.session() as s:
            return s.exec(select(AuthSession).where(AuthSession.token_hash == token_hash)).first()

    def delete_auth_session(self, token_hash: str) -> None:
        """Log out: drop the session for this token hash (no-op if already gone)."""
        with self.session() as s:
            row = s.exec(
                select(AuthSession).where(AuthSession.token_hash == token_hash)
            ).first()
            if row is not None:
                s.delete(row)
                s.commit()

    # ── process registry (online local processes — DESIGN §7.2 / §8.5) ───────────────────────
    def register_process(self, process: ProcessRegistry) -> ProcessRegistry:
        """Upsert a local process by id (an outbound long-conn registers/refreshes itself).
        Stamps created_at on first insert.

        Defense-in-depth (DESIGN §8.4 multi-tenant): a process row is NEVER re-homed to a
        different account. If an existing row with this id belongs to another account, the upsert
        is refused (returns the existing row untouched) — so even a buggy/hostile caller can't
        overwrite another tenant's registry entry. The relay already derives the id from the
        access key, so this only ever triggers on a genuine collision."""
        with self.session() as s:
            existing = s.get(ProcessRegistry, process.id)
            if existing is None:
                if not process.created_at:
                    process.created_at = utc_now_iso()
                s.add(process)
                s.commit()
                return process
            if existing.account_id != process.account_id:
                return existing  # ownership guard: refuse cross-account re-home
            existing.account_id = process.account_id
            existing.access_key_id = process.access_key_id
            existing.name = process.name
            existing.online = process.online
            existing.last_heartbeat = process.last_heartbeat
            s.add(existing)
            s.commit()
            return existing

    def set_process_online(
        self, process_id: str, online: bool, last_heartbeat: str | None = None
    ) -> None:
        """Flip a process online/offline and bump its heartbeat (App opens=online, closes=offline
        — DESIGN §4.6). No-op if the process isn't registered."""
        with self.session() as s:
            row = s.get(ProcessRegistry, process_id)
            if row is not None:
                row.online = online
                row.last_heartbeat = last_heartbeat or utc_now_iso()
                s.add(row)
                s.commit()

    def get_processes(self, account_id: str) -> list[ProcessRegistry]:
        """All processes registered to an account (a person may run several machines)."""
        with self.session() as s:
            return list(
                s.exec(
                    select(ProcessRegistry)
                    .where(ProcessRegistry.account_id == account_id)
                    .order_by(col(ProcessRegistry.created_at))
                ).all()
            )

    def get_online_processes(self, account_id: str | None = None) -> list[ProcessRegistry]:
        """Currently-online processes, optionally scoped to one account (relay routing — §8.5)."""
        with self.session() as s:
            stmt = select(ProcessRegistry).where(col(ProcessRegistry.online).is_(True))
            if account_id is not None:
                stmt = stmt.where(ProcessRegistry.account_id == account_id)
            return list(s.exec(stmt).all())
