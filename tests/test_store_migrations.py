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
def test_client_init_is_idempotent_and_at_v3(tmp_path):
    st = Store(str(tmp_path / "c.db"))
    st.init()
    st.init()  # re-run must not duplicate ledger rows or raise
    assert st.schema_version() == 3
    with st.engine.connect() as conn:  # client ledger table is `schemaversion` (see migrations)
        rows = conn.execute(text(f"SELECT version FROM {CLIENT_VERSION_TABLE}")).fetchall()
    assert sorted(r[0] for r in rows) == [1, 2, 3]


def test_client_migration_adds_diff_stat_to_legacy_decisioncard(tmp_path):
    """A pre-diff_stat `decisioncard` table (older release) gains the column on upgrade."""
    engine = _engine(tmp_path / "legacy.db")
    _exec(engine, "CREATE TABLE decisioncard (id TEXT PRIMARY KEY, summary TEXT)")
    with engine.connect() as conn:
        assert not column_exists(conn, "decisioncard", "diff_stat")

    applied = run_migrations(engine, CLIENT_MIGRATIONS, version_table=CLIENT_VERSION_TABLE)
    assert applied == [1, 2, 3]
    with engine.connect() as conn:
        assert column_exists(conn, "decisioncard", "diff_stat")
        assert column_exists(conn, "session", "latest_context_checkpoint_id") is False
        assert current_version(conn, CLIENT_VERSION_TABLE) == 3


def test_client_migration_adds_session_main_workspace_and_backfills(tmp_path):
    """A pre-main_workspace session table keeps the original workspace as its fallback root."""
    engine = _engine(tmp_path / "legacy-main-workspace.db")
    _exec(engine, "CREATE TABLE session (id TEXT PRIMARY KEY, goal TEXT, workspace TEXT)")
    _exec(engine, "INSERT INTO session (id, goal, workspace) VALUES ('s1', 'g', 'E:/AutoWorkAgent')")
    _exec(engine, f"CREATE TABLE {CLIENT_VERSION_TABLE} (version INTEGER PRIMARY KEY, applied_at TEXT)")
    _exec(engine, f"INSERT INTO {CLIENT_VERSION_TABLE} (version, applied_at) VALUES (1, '2020-01-01')")
    with engine.connect() as conn:
        assert not column_exists(conn, "session", "main_workspace")

    applied = run_migrations(engine, CLIENT_MIGRATIONS, version_table=CLIENT_VERSION_TABLE)
    assert applied == [2, 3]
    with engine.connect() as conn:
        assert column_exists(conn, "session", "main_workspace")
        assert column_exists(conn, "session", "latest_context_checkpoint_id")
        row = conn.execute(text("SELECT workspace, main_workspace FROM session WHERE id='s1'")).first()
        assert row == ("E:/AutoWorkAgent", "E:/AutoWorkAgent")
        assert current_version(conn, CLIENT_VERSION_TABLE) == 3


def test_client_migration_main_workspace_backfill_tolerates_null_workspace(tmp_path):
    engine = _engine(tmp_path / "legacy-null-workspace.db")
    _exec(engine, "CREATE TABLE session (id TEXT PRIMARY KEY, goal TEXT, workspace TEXT)")
    _exec(engine, "INSERT INTO session (id, goal, workspace) VALUES ('s1', 'g', NULL)")
    _exec(engine, f"CREATE TABLE {CLIENT_VERSION_TABLE} (version INTEGER PRIMARY KEY, applied_at TEXT)")
    _exec(engine, f"INSERT INTO {CLIENT_VERSION_TABLE} (version, applied_at) VALUES (1, '2020-01-01')")

    applied = run_migrations(engine, CLIENT_MIGRATIONS, version_table=CLIENT_VERSION_TABLE)

    assert applied == [2, 3]
    with engine.connect() as conn:
        row = conn.execute(text("SELECT workspace, main_workspace FROM session WHERE id='s1'")).first()
        assert row == (None, "")
        assert column_exists(conn, "session", "latest_context_checkpoint_id")
        assert current_version(conn, CLIENT_VERSION_TABLE) == 3


def test_client_migration_v3_adds_latest_context_checkpoint_id(tmp_path):
    engine = _engine(tmp_path / "legacy-context-v2.db")
    _exec(
        engine,
        "CREATE TABLE session ("
        "id TEXT PRIMARY KEY, goal TEXT, workspace TEXT, main_workspace TEXT"
        ")",
    )
    _exec(
        engine,
        "INSERT INTO session (id, goal, workspace, main_workspace) "
        "VALUES ('s1', 'g', '/w', '/w')",
    )
    _exec(engine, f"CREATE TABLE {CLIENT_VERSION_TABLE} (version INTEGER PRIMARY KEY, applied_at TEXT)")
    _exec(engine, f"INSERT INTO {CLIENT_VERSION_TABLE} (version, applied_at) VALUES (1, '2020-01-01')")
    _exec(engine, f"INSERT INTO {CLIENT_VERSION_TABLE} (version, applied_at) VALUES (2, '2020-01-02')")
    with engine.connect() as conn:
        assert not column_exists(conn, "session", "latest_context_checkpoint_id")

    applied = run_migrations(engine, CLIENT_MIGRATIONS, version_table=CLIENT_VERSION_TABLE)

    assert applied == [3]
    with engine.connect() as conn:
        assert column_exists(conn, "session", "latest_context_checkpoint_id")
        value = conn.execute(
            text("SELECT latest_context_checkpoint_id FROM session WHERE id='s1'")
        ).scalar_one()
        assert value == ""
        assert current_version(conn, CLIENT_VERSION_TABLE) == 3


# ── server ───────────────────────────────────────────────────────────────────────────────────
def test_server_init_is_idempotent_and_at_v3(tmp_path):
    st = ServerStore(str(tmp_path / "s.db"))
    st.init()
    st.init()
    assert st.schema_version() == SERVER_SCHEMA_VERSION == 3
    with st.engine.connect() as conn:
        rows = conn.execute(text("SELECT version FROM schema_version")).fetchall()
    assert sorted(r[0] for r in rows) == [1, 2, 3]  # ledger = one row per applied migration


def test_server_migration_adds_password_hash_to_legacy_accounts(tmp_path):
    """An old v1 server DB whose `accounts` predates password_hash gains it on upgrade to v2."""
    engine = _engine(tmp_path / "legacy-srv.db")
    _exec(engine, "CREATE TABLE accounts (id TEXT PRIMARY KEY, username TEXT)")
    _exec(engine, "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    _exec(engine, "INSERT INTO schema_version (version, applied_at) VALUES (1, '2020-01-01')")
    with engine.connect() as conn:
        assert not column_exists(conn, "accounts", "password_hash")

    applied = run_migrations(engine, SERVER_MIGRATIONS)
    assert applied == [2, 3]  # v1 already ledgered -> password migration, then cache cleanup
    with engine.connect() as conn:
        assert column_exists(conn, "accounts", "password_hash")
        assert current_version(conn) == 3


def test_server_migration_v3_drops_legacy_display_cache_tables(tmp_path):
    engine = _engine(tmp_path / "legacy-cache.db")
    _exec(engine, "CREATE TABLE cache_sessions (id TEXT PRIMARY KEY)")
    _exec(engine, "CREATE TABLE cache_cards (id TEXT PRIMARY KEY)")
    _exec(engine, "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
    _exec(engine, "INSERT INTO schema_version (version, applied_at) VALUES (1, '2020-01-01')")
    _exec(engine, "INSERT INTO schema_version (version, applied_at) VALUES (2, '2020-01-02')")

    applied = run_migrations(engine, SERVER_MIGRATIONS)
    assert applied == [3]
    with engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert "cache_sessions" not in names
        assert "cache_cards" not in names
        assert current_version(conn) == 3
