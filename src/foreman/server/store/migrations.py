"""Ordered schema migrations for the server store (team/relay mode; DESIGN §11.1 point 3).

Mirror of the client migrator: `ServerStore.init()` runs `create_all` (scoped to SERVER_TABLES,
builds *missing whole tables*) and then `run_migrations(self.engine, SERVER_MIGRATIONS)` to apply
the in-place table changes create_all can't, and to stamp the `schema_version` ledger.

Every `upgrade` MUST be idempotent (the helpers no-op when the change is already present) so it's
safe on a fresh DB — where create_all already built the latest columns — and as a re-run.

History:
- v1 — baseline (§7.2): accounts / access_keys / process_registry (+ invites / cache placeholders).
  All whole tables, so create_all covers them; this is a history marker with no in-place DDL.
- v2 — accounts.password_hash (T3.5 user login). create_all builds it on a fresh DB; for an
  existing v1 server DB whose `accounts` table predates the column, this ADD COLUMN brings the
  old rows up smoothly. The `auth_sessions` table that T3.5 also added is a *new whole table*,
  so create_all handles it — no migration needed.
"""

from __future__ import annotations

from foreman.shared.migrations import Migration, add_column


def _v1_baseline(conn) -> None:
    # Original §7.2 schema — all whole tables, created by create_all. No in-place change.
    pass


def _v2_account_password_hash(conn) -> None:
    add_column(conn, "accounts", "password_hash", "TEXT NOT NULL DEFAULT ''")


SERVER_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline §7.2 (accounts / access_keys / process_registry)", _v1_baseline),
    Migration(2, "accounts.password_hash (T3.5 user login)", _v2_account_password_hash),
]
