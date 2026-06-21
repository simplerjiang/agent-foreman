"""Tests for the ledger-based schema migrator (TASKS T5.5, DESIGN §11.1 point 3).

The promise being verified: "升级后你的历史还在" — an upgrade that moves the schema ahead of an
existing DB applies the missing in-place changes (ADD COLUMN) without losing a row, records which
versions ran in the `schema_version` ledger, is idempotent (safe to re-run), and resumes cleanly
after a crash (one transaction per migration). Uses tmp_path sqlite FILEs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlmodel import create_engine

from foreman.shared.migrations import (
    Migration,
    add_column,
    applied_versions,
    column_exists,
    current_version,
    run_migrations,
    table_exists,
)


def _engine(tmp_path, name="m.db"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def _exec(engine, sql: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql))


# ── DDL helpers ────────────────────────────────────────────────────────────────────────────────
def test_table_and_column_exists(tmp_path):
    engine = _engine(tmp_path)
    _exec(engine, "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
    with engine.connect() as conn:
        assert table_exists(conn, "t")
        assert not table_exists(conn, "nope")
        assert column_exists(conn, "t", "a")
        assert not column_exists(conn, "t", "b")


def test_add_column_adds_once_and_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    _exec(engine, "CREATE TABLE t (id INTEGER PRIMARY KEY)")
    with engine.begin() as conn:
        assert add_column(conn, "t", "x", "TEXT NOT NULL DEFAULT ''") is True
    with engine.begin() as conn:
        # already present → no-op, returns False, doesn't raise
        assert add_column(conn, "t", "x", "TEXT NOT NULL DEFAULT ''") is False
    with engine.connect() as conn:
        assert column_exists(conn, "t", "x")


def test_add_column_noop_when_table_absent(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        assert add_column(conn, "missing", "x", "TEXT") is False


# ── ledger ───────────────────────────────────────────────────────────────────────────────────
def test_applied_versions_empty_for_pre_ledger_db(tmp_path):
    engine = _engine(tmp_path)  # no schema_version table at all
    with engine.connect() as conn:
        assert applied_versions(conn) == set()
        assert current_version(conn) == 0


def test_run_migrations_applies_in_order_and_stamps_ledger(tmp_path):
    engine = _engine(tmp_path)
    _exec(engine, "CREATE TABLE t (id INTEGER PRIMARY KEY)")
    seen: list[int] = []

    def mk(v):
        def up(conn):
            seen.append(v)
            add_column(conn, "t", f"c{v}", "TEXT NOT NULL DEFAULT ''")

        return Migration(v, f"add c{v}", up)

    # deliberately out of order — run_migrations must sort ascending
    applied = run_migrations(engine, [mk(2), mk(1), mk(3)])
    assert applied == [1, 2, 3]
    assert seen == [1, 2, 3]
    with engine.connect() as conn:
        assert applied_versions(conn) == {1, 2, 3}
        assert current_version(conn) == 3
        assert column_exists(conn, "t", "c1")


def test_run_migrations_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    _exec(engine, "CREATE TABLE t (id INTEGER PRIMARY KEY)")
    m = Migration(1, "add x", lambda c: add_column(c, "t", "x", "TEXT NOT NULL DEFAULT ''"))
    assert run_migrations(engine, [m]) == [1]
    assert run_migrations(engine, [m]) == []  # second run: nothing left to do
    with engine.connect() as conn:
        # ledger has exactly one row for v1 — no duplicate stamping
        rows = conn.execute(text("SELECT version FROM schema_version")).fetchall()
    assert [r[0] for r in rows] == [1]


def test_run_migrations_empty_list_noop(tmp_path):
    engine = _engine(tmp_path)
    assert run_migrations(engine, []) == []


def test_duplicate_versions_raise(tmp_path):
    engine = _engine(tmp_path)
    a = Migration(1, "a", lambda c: None)
    b = Migration(1, "b", lambda c: None)
    with pytest.raises(ValueError, match="duplicate migration version"):
        run_migrations(engine, [a, b])


def test_only_missing_versions_apply_resume(tmp_path):
    """Simulate an old DB at v1 missing a later column: only v2+ should apply."""
    engine = _engine(tmp_path)
    _exec(engine, "CREATE TABLE t (id INTEGER PRIMARY KEY)")
    # pre-existing ledger says v1 already ran (but the v2 column is missing)
    _exec(engine, "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    _exec(engine, "INSERT INTO schema_version (version, applied_at) VALUES (1, '2020-01-01')")

    v1 = Migration(1, "should be skipped", lambda c: add_column(c, "t", "old", "TEXT"))
    v2 = Migration(2, "add new", lambda c: add_column(c, "t", "new", "TEXT NOT NULL DEFAULT ''"))
    applied = run_migrations(engine, [v1, v2])
    assert applied == [2]  # v1 already in the ledger → skipped
    with engine.connect() as conn:
        assert column_exists(conn, "t", "new")
        assert not column_exists(conn, "t", "old")  # skipped v1's upgrade never ran
        assert current_version(conn) == 2


def test_crash_leaves_db_at_last_good_version(tmp_path):
    """A migration that raises is NOT stamped, earlier ones survive, and a re-run resumes.

    The resume guarantee rests on each upgrade being *idempotent* (not on transactional DDL —
    pysqlite doesn't roll back an ALTER): a half-applied change is harmless because the re-run's
    helpers no-op on what's already there. So the contract verified here is "the failed version
    isn't recorded, so it re-runs next startup", which is what keeps history intact.
    """
    engine = _engine(tmp_path)
    _exec(engine, "CREATE TABLE t (id INTEGER PRIMARY KEY)")
    attempts: list[int] = []

    def v2(conn):
        attempts.append(1)
        add_column(conn, "t", "b", "TEXT NOT NULL DEFAULT ''")
        if len(attempts) == 1:  # first attempt blows up *after* a partial change
            raise RuntimeError("boom")

    v1 = Migration(1, "ok", lambda c: add_column(c, "t", "a", "TEXT NOT NULL DEFAULT ''"))
    m2 = Migration(2, "flaky", v2)

    with pytest.raises(RuntimeError, match="boom"):
        run_migrations(engine, [v1, m2])

    with engine.connect() as conn:
        assert applied_versions(conn) == {1}  # v1 stamped, v2 NOT (so it will re-run)
        assert current_version(conn) == 1
        assert column_exists(conn, "t", "a")  # v1's change persisted

    # re-run resumes from v2; its idempotent upgrade succeeds the second time and is stamped once
    assert run_migrations(engine, [v1, m2]) == [2]
    with engine.connect() as conn:
        assert current_version(conn) == 2
        assert column_exists(conn, "t", "b")
