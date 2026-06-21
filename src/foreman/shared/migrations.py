"""A tiny, idempotent, ledger-based schema migrator (DESIGN §11.1 point 3).

The single-machine promise: "升级后你的历史还在" — when the code's schema moves ahead of an
existing `foreman.db`, bring the old DB up smoothly without losing a row. Both the client and
server stores use this (it lives in `shared` because both ends need it — §14 boundary).

How it fits with `SQLModel.metadata.create_all`:

- `create_all` only ever creates *missing whole tables*. It handles a fresh install (builds every
  table at the latest shape) and a release that adds a brand-new table. It will NOT alter an
  existing table (add a column, change a default).
- This migrator handles exactly what create_all can't: in-place changes to tables that already
  exist (ADD COLUMN, data backfills), AND it records which versions have run in the
  `schema_version` ledger so an upgrade knows where it left off.

The two run together: `create_all` first, then `run_migrations`. Because every migration's
`upgrade` is written to be **idempotent** (the helpers below no-op when the change is already
present), a migration is safe to run on a fresh DB (where create_all already made the latest
columns) and safe to re-run after a partial failure. That idempotency is the core safety net.

The `schema_version` table is a *ledger*: one row per applied version (not a single mutable row),
so it doubles as an audit trail of when each migration ran.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from foreman.shared.events import utc_now_iso

# An upgrade step gets a live connection inside an open transaction; it raises to abort.
Upgrade = Callable[[Connection], None]


@dataclass(frozen=True)
class Migration:
    """One ordered schema step. `upgrade` must be idempotent (use the helpers below)."""

    version: int
    description: str
    upgrade: Upgrade


# ── idempotent DDL helpers (the building blocks an `upgrade` uses) ───────────────────────────────
def table_exists(conn: Connection, table: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table}
    ).first()
    return row is not None


def column_exists(conn: Connection, table: str, column: str) -> bool:
    # PRAGMA can't be parameterized, but `table` here is always an internal literal, never input.
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def add_column(conn: Connection, table: str, column: str, decl: str) -> bool:
    """ADD COLUMN if (and only if) it's not already there. Returns True if it actually added it.

    Idempotent: a no-op when the column exists (fresh DB created by create_all, or a re-run after
    a partial failure). `table`/`column`/`decl` are internal literals from a migration list — never
    request data — so the f-string can't be an injection vector."""
    if not table_exists(conn, table) or column_exists(conn, table, column):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))
    return True


# ── the ledger ───────────────────────────────────────────────────────────────────────────────
def _ensure_ledger(conn: Connection, version_table: str) -> None:
    conn.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {version_table} "
            "(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
    )


def applied_versions(conn: Connection, version_table: str = "schema_version") -> set[int]:
    """The set of schema versions already recorded in the ledger (empty for a pre-ledger DB)."""
    if not table_exists(conn, version_table):
        return set()
    rows = conn.execute(text(f"SELECT version FROM {version_table}")).fetchall()
    return {int(r[0]) for r in rows}


def current_version(conn: Connection, version_table: str = "schema_version") -> int:
    """The highest applied version, or 0 if none — the DB's effective schema version."""
    applied = applied_versions(conn, version_table)
    return max(applied) if applied else 0


# ── the runner ───────────────────────────────────────────────────────────────────────────────
def run_migrations(
    engine: Engine,
    migrations: list[Migration],
    *,
    version_table: str = "schema_version",
) -> list[int]:
    """Apply every migration whose version isn't recorded yet, in ascending version order.

    Each migration runs in its own transaction and its version is stamped into the ledger only
    after the upgrade succeeds — so a crash mid-way leaves the DB at the last fully-applied
    version, and the next startup resumes from there (the unfinished step re-runs, which is safe
    because upgrades are idempotent). Returns the versions applied during this call (in order).
    """
    if not migrations:
        return []
    ordered = sorted(migrations, key=lambda m: m.version)
    versions = [m.version for m in ordered]
    if len(set(versions)) != len(versions):
        raise ValueError(f"duplicate migration version(s): {versions}")

    with engine.begin() as conn:
        _ensure_ledger(conn, version_table)
        done = applied_versions(conn, version_table)

    applied_now: list[int] = []
    for m in ordered:
        if m.version in done:
            continue
        with engine.begin() as conn:  # one transaction per migration
            m.upgrade(conn)
            conn.execute(
                text(f"INSERT INTO {version_table} (version, applied_at) VALUES (:v, :a)"),
                {"v": m.version, "a": utc_now_iso()},
            )
        applied_now.append(m.version)
    return applied_now
