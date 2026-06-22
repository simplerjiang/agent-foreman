"""Server database session/engine wrapper + r/w helpers (team/relay mode).

Separate from the client's local store. This is the SERVER side of DESIGN §7.2: it knows
WHO (accounts), WHICH MACHINE may connect (access_keys, hash-only), and WHAT IS ONLINE
(process_registry) so the relay can route a PWA to the right local process (§8.5).

It deliberately holds NO 秘方 (definitions), no full diffs/raw output, and NO per-user LLM
keys — those live in each user's local .env (§8.3/§8.4).

cache_sessions / cache_cards are the display cache (T7.5, DESIGN §8.5 ③): a read-only copy
of each account's session summaries + decision cards, pushed up by the local process so the
PWA can still view them while the PC is offline. They hold ONLY display summaries — never full
diffs / raw output / 秘方 (§8.3) — and every read/write is scoped by account_id (§8.4).
"""

from __future__ import annotations

import os

from sqlalchemy import text
from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, col, create_engine, select

from foreman.shared.events import utc_now_iso
from foreman.shared.migrations import current_version, run_migrations

from . import models  # noqa: F401  (registers server tables on SQLModel.metadata)
from .migrations import SERVER_MIGRATIONS
from .models import (
    Account,
    AccessKey,
    AuthSession,
    CacheCard,
    CacheSession,
    Invite,
    ProcessRegistry,
)

# v2 adds Account.password_hash + the auth_sessions table (T3.5 user login / access-key mgmt).
SERVER_SCHEMA_VERSION = 2

# Tables the admin console may inspect (数据库管理). A fixed allowlist so a path param can never
# reach an arbitrary/unknown table name in raw SQL (the names come from our own schema, but we
# never interpolate a client-supplied string we haven't matched against this set).
_BROWSABLE_TABLES = frozenset(
    {
        "accounts", "access_keys", "process_registry", "auth_sessions",
        "cache_sessions", "cache_cards", "invites", "schema_version",
    }
)


class ServerStore:
    def __init__(self, db_path: str = "foreman-server.db") -> None:
        self.db_path = db_path
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )

    def init(self) -> None:
        """Bring the DB to the current schema and stamp the version ledger (DESIGN §7.2 / §11.1).

        `create_all` is scoped to SERVER_TABLES (so a shared metadata never pulls in client
        tables) and builds any *missing whole tables*; the migrator then applies in-place table
        changes create_all can't (e.g. accounts.password_hash) and records each applied version
        in `schema_version`. Both steps are idempotent — safe to re-run and crash-resumable.
        """
        SQLModel.metadata.create_all(
            self.engine, tables=[m.__table__ for m in models.SERVER_TABLES]
        )
        run_migrations(self.engine, SERVER_MIGRATIONS)

    def schema_version(self) -> int:
        """The DB's effective schema version — the highest applied migration, or 0 if none."""
        with self.engine.connect() as conn:
            return current_version(conn)

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

    # ── invites (admin builds a user → one-time code → user sets password — DESIGN §8.2) ──────
    def add_invite(self, invite: Invite) -> Invite:
        """Persist an admin-issued invite. ONLY the code hash is stored (caller hashes the
        plaintext, shown to the admin once); the code is the only non-admin path to a usable
        password (no self-signup, §8.2)."""
        with self.session() as s:
            s.add(invite)
            s.commit()
        return invite

    def get_invite_by_hash(self, code_hash: str) -> Invite | None:
        """Resolve an invite code (by its hash) to its row. Returns regardless of used/expiry;
        the caller enforces single-use + expiry so a spent/expired code can be reported."""
        with self.session() as s:
            return s.exec(select(Invite).where(Invite.code_hash == code_hash)).first()

    def mark_invite_used(self, invite_id: str, when: str | None = None) -> None:
        """Burn an invite (single-use). No-op if it's missing."""
        with self.session() as s:
            row = s.get(Invite, invite_id)
            if row is not None:
                row.used_at = when or utc_now_iso()
                s.add(row)
                s.commit()

    def invalidate_account_invites(self, account_id: str, when: str | None = None) -> int:
        """Burn every still-unused invite for an account (so a re-invite leaves exactly one live
        code). Returns how many were invalidated."""
        stamp = when or utc_now_iso()
        with self.session() as s:
            rows = list(
                s.exec(
                    select(Invite)
                    .where(Invite.account_id == account_id)
                    .where(Invite.used_at == "")
                ).all()
            )
            for row in rows:
                row.used_at = stamp
                s.add(row)
            if rows:
                s.commit()
            return len(rows)

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

    # ── display cache (read-only copy for the PWA while the PC is offline — §8.5 ③) ───────────
    def upsert_cache_session(self, row: CacheSession) -> CacheSession:
        """Insert/refresh one cached session summary, keyed by (account_id, session_id) so a
        re-sync overwrites the prior copy instead of piling up duplicates. Stamps updated_at if
        unset. Holds only a display summary — no diffs/raw output (§8.3)."""
        if not row.updated_at:
            row.updated_at = utc_now_iso()
        with self.session() as s:
            existing = s.exec(
                select(CacheSession)
                .where(CacheSession.account_id == row.account_id)
                .where(CacheSession.session_id == row.session_id)
            ).first()
            if existing is None:
                s.add(row)
                s.commit()
                return row
            existing.summary_json = row.summary_json
            existing.updated_at = row.updated_at
            s.add(existing)
            s.commit()
            return existing

    def get_cache_sessions(self, account_id: str) -> list[CacheSession]:
        """An account's cached session summaries, newest first. Scoped to the account (§8.4)."""
        with self.session() as s:
            return list(
                s.exec(
                    select(CacheSession)
                    .where(CacheSession.account_id == account_id)
                    .order_by(col(CacheSession.updated_at).desc())
                ).all()
            )

    def upsert_cache_card(self, row: CacheCard) -> CacheCard:
        """Insert/refresh one cached decision card, keyed by (account_id, card_id). Stamps
        updated_at if unset. Holds only the card's display payload (§8.3)."""
        if not row.updated_at:
            row.updated_at = utc_now_iso()
        with self.session() as s:
            existing = s.exec(
                select(CacheCard)
                .where(CacheCard.account_id == row.account_id)
                .where(CacheCard.card_id == row.card_id)
            ).first()
            if existing is None:
                s.add(row)
                s.commit()
                return row
            existing.payload_json = row.payload_json
            existing.status = row.status
            existing.updated_at = row.updated_at
            s.add(existing)
            s.commit()
            return existing

    def get_cache_cards(self, account_id: str) -> list[CacheCard]:
        """An account's cached decision cards, newest first. Scoped to the account (§8.4)."""
        with self.session() as s:
            return list(
                s.exec(
                    select(CacheCard)
                    .where(CacheCard.account_id == account_id)
                    .order_by(col(CacheCard.updated_at).desc())
                ).all()
            )

    # ── admin console: cross-tenant operational views (admin only — gated in app.py) ──────────
    # These power the admin dashboard (概览/在线会话/进程/数据库). They surface operational
    # metadata (who is logged in, which machines are online, table sizes) for the deployment's
    # operator. Secret columns (anything ending in `_hash`) are NEVER returned — browse_table
    # redacts them so even an admin can't read a password/token/key hash out of the DB.
    def get_active_auth_sessions(self, now: str) -> list[dict]:
        """Currently-valid PWA login sessions joined to their account (在线会话 / 登录账户).

        Newest first. Returns account metadata + session timestamps only — never the token hash.
        ``now`` is an ISO8601 UTC string; expiry is a lexical compare (AuthSession.expires_at is
        stored lexically comparable to utc_now_iso())."""
        out: list[dict] = []
        with self.session() as s:
            accounts = {a.id: a for a in s.exec(select(Account)).all()}
            rows = s.exec(
                select(AuthSession).order_by(col(AuthSession.created_at).desc())
            ).all()
            for r in rows:
                if r.expires_at and r.expires_at <= now:
                    continue  # expired
                a = accounts.get(r.account_id)
                out.append(
                    {
                        "account_id": r.account_id,
                        "username": a.username if a else "(deleted)",
                        "display_name": a.display_name if a else "",
                        "role": a.role if a else "",
                        "created_at": r.created_at,
                        "expires_at": r.expires_at,
                    }
                )
        return out

    def get_all_processes(self) -> list[ProcessRegistry]:
        """Every registered process across all accounts (admin system-wide 进程 view), oldest
        first. Holds no secrets (the registry never stores key hashes/plaintext — §8.3)."""
        with self.session() as s:
            return list(
                s.exec(
                    select(ProcessRegistry).order_by(col(ProcessRegistry.created_at))
                ).all()
            )

    def db_size_bytes(self) -> int:
        """On-disk size of the SQLite file (0 if it can't be stat'd, e.g. an :memory: db)."""
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0

    def table_stats(self) -> list[dict]:
        """Per-table row counts for the 数据库管理 overview. Names come from sqlite_master (our
        own schema), sorted by name."""
        out: list[dict] = []
        with self.engine.connect() as conn:
            names = [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    )
                )
            ]
            for n in names:
                try:
                    cnt = conn.execute(text(f'SELECT COUNT(*) FROM "{n}"')).scalar() or 0
                except Exception:  # noqa: BLE001 — a weird/legacy table shouldn't break the view
                    cnt = -1
                out.append({"name": n, "rows": int(cnt)})
        return out

    def browse_table(self, name: str, *, limit: int = 50, offset: int = 0) -> dict:
        """Read a page of rows from one allowlisted table (read-only). Any column whose name ends
        in ``_hash`` is redacted to ``***`` so a password/token/invite/key hash is never returned
        to the UI (§8.4). Returns {"error": "unknown_table"} for a name not in the allowlist."""
        if name not in _BROWSABLE_TABLES:
            return {"error": "unknown_table"}
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self.engine.connect() as conn:
            result = conn.execute(
                text(f'SELECT * FROM "{name}" LIMIT :limit OFFSET :offset'),
                {"limit": limit, "offset": offset},
            )
            cols = list(result.keys())
            rows: list[dict] = []
            for rec in result:
                row: dict = {}
                for c, v in zip(cols, rec):
                    row[c] = ("***" if v else "") if c.endswith("_hash") else v
                rows.append(row)
            total = conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0
        return {
            "name": name, "columns": cols, "rows": rows,
            "total": int(total), "limit": limit, "offset": offset,
        }

    def vacuum(self) -> None:
        """Run VACUUM (reclaim space / defragment). Must run outside a transaction, so we flip the
        connection to AUTOCOMMIT first."""
        with self.engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT").execute(text("VACUUM"))

    def integrity_check(self) -> str:
        """Run PRAGMA integrity_check; returns 'ok' on a healthy DB, else the first problem."""
        with self.engine.connect() as conn:
            return str(conn.execute(text("PRAGMA integrity_check")).scalar())
