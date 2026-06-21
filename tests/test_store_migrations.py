"""Integration tests: the real client/server migration lists upgrade legacy DBs (TASKS T5.5).

Proves the §11.1 promise on the actual stores — a DB created by an *older* release (missing a
column create_all won't add) is brought up smoothly when the new code runs its migrations, and a
fresh `init()` is idempotent and lands at the expected schema version.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlmodel import create_engine

from foreman.client.store import Store
from foreman.client.store.migrations import CLIENT_MIGRATIONS, CLIENT_VERSION_TABLE
from foreman.server.store import SERVER_SCHEMA_VERSION, ServerStore
from foreman.server.store.migrations import SERVER_MIGRATIONS
from foreman.shared.migrations import column_exists, current_version, run_migrations


def _engine(path):
    return create_engine(f"sqlite:///{path}")


def _exec(engine, sql):
    with engine.begin() as conn:
        conn.execute(text(sql))


# ── client ───────────────────────────────────────────────────────────────────────────────────
def test_client_init_is_idempotent_and_at_v1(tmp_path):
    st = Store(str(tmp_path / "c.db"))
    st.init()
    st.init()  # re-run must not duplicate ledger rows or raise
    assert st.schema_version() == 1
    with st.engine.connect() as conn:  # client ledger table is `schemaversion` (see migrations)
        rows = conn.execute(text(f"SELECT version FROM {CLIENT_VERSION_TABLE}")).fetchall()
    assert sorted(r[0] for r in rows) == [1]


def test_client_migration_adds_diff_stat_to_legacy_decisioncard(tmp_path):
    """A pre-diff_stat `decisioncard` table (older release) gains the column on upgrade."""
    engine = _engine(tmp_path / "legacy.db")
    _exec(engine, "CREATE TABLE decisioncard (id TEXT PRIMARY KEY, summary TEXT)")
    with engine.connect() as conn:
        assert not column_exists(conn, "decisioncard", "diff_stat")

    applied = run_migrations(engine, CLIENT_MIGRATIONS, version_table=CLIENT_VERSION_TABLE)
    assert applied == [1]
    with engine.connect() as conn:
        assert column_exists(conn, "decisioncard", "diff_stat")
        assert current_version(conn, CLIENT_VERSION_TABLE) == 1


# ── server ───────────────────────────────────────────────────────────────────────────────────
def test_server_init_is_idempotent_and_at_v2(tmp_path):
    st = ServerStore(str(tmp_path / "s.db"))
    st.init()
    st.init()
    assert st.schema_version() == SERVER_SCHEMA_VERSION == 2
    with st.engine.connect() as conn:
        rows = conn.execute(text("SELECT version FROM schema_version")).fetchall()
    assert sorted(r[0] for r in rows) == [1, 2]  # ledger = one row per applied migration


def test_server_migration_adds_password_hash_to_legacy_accounts(tmp_path):
    """An old v1 server DB whose `accounts` predates password_hash gains it on upgrade to v2."""
    engine = _engine(tmp_path / "legacy-srv.db")
    _exec(engine, "CREATE TABLE accounts (id TEXT PRIMARY KEY, username TEXT)")
    _exec(engine, "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    _exec(engine, "INSERT INTO schema_version (version, applied_at) VALUES (1, '2020-01-01')")
    with engine.connect() as conn:
        assert not column_exists(conn, "accounts", "password_hash")

    applied = run_migrations(engine, SERVER_MIGRATIONS)
    assert applied == [2]  # v1 already ledgered → only v2 runs
    with engine.connect() as conn:
        assert column_exists(conn, "accounts", "password_hash")
        assert current_version(conn) == 2
