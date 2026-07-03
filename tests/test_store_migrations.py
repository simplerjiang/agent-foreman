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
from foreman.client.store.models import WorktreeLease
from foreman.server.store import SERVER_SCHEMA_VERSION, ServerStore
from foreman.server.store.migrations import SERVER_MIGRATIONS
from foreman.shared.migrations import column_exists, current_version, run_migrations, table_exists


def _engine(path):
    return create_engine(f"sqlite:///{path}")


def _exec(engine, sql):
    with engine.begin() as conn:
        conn.execute(text(sql))


# ── client ───────────────────────────────────────────────────────────────────────────────────
def test_client_init_is_idempotent_and_at_v7(tmp_path):
    st = Store(str(tmp_path / "c.db"))
    st.init()
    st.init()  # re-run must not duplicate ledger rows or raise
    assert st.schema_version() == 7
    with st.engine.connect() as conn:  # client ledger table is `schemaversion` (see migrations)
        rows = conn.execute(text(f"SELECT version FROM {CLIENT_VERSION_TABLE}")).fetchall()
        indexes = {
            row[1] for row in conn.execute(text("PRAGMA index_list(worktree_leases)")).fetchall()
        }
    assert sorted(r[0] for r in rows) == [1, 2, 3, 4, 5, 6, 7]
    assert "ux_worktree_leases_active_write_path" in indexes


def test_client_migration_adds_diff_stat_to_legacy_decisioncard(tmp_path):
    """A pre-diff_stat `decisioncard` table (older release) gains the column on upgrade."""
    engine = _engine(tmp_path / "legacy.db")
    _exec(engine, "CREATE TABLE decisioncard (id TEXT PRIMARY KEY, summary TEXT)")
    with engine.connect() as conn:
        assert not column_exists(conn, "decisioncard", "diff_stat")

    applied = run_migrations(engine, CLIENT_MIGRATIONS, version_table=CLIENT_VERSION_TABLE)
    assert applied == [1, 2, 3, 4, 5, 6, 7]
    with engine.connect() as conn:
        assert column_exists(conn, "decisioncard", "diff_stat")
        assert column_exists(conn, "session", "latest_context_checkpoint_id") is False
        assert table_exists(conn, "worktree_leases")
        assert current_version(conn, CLIENT_VERSION_TABLE) == 7


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
    assert applied == [2, 3, 4, 5, 6, 7]
    with engine.connect() as conn:
        assert column_exists(conn, "session", "main_workspace")
        assert column_exists(conn, "session", "latest_context_checkpoint_id")
        assert column_exists(conn, "session", "context_materialized_until_ts")
        assert column_exists(conn, "session", "context_materialized_until_event_id")
        assert table_exists(conn, "worktree_leases")
        row = conn.execute(text("SELECT workspace, main_workspace FROM session WHERE id='s1'")).first()
        assert row == ("E:/AutoWorkAgent", "E:/AutoWorkAgent")
        assert current_version(conn, CLIENT_VERSION_TABLE) == 7


def test_client_migration_main_workspace_backfill_tolerates_null_workspace(tmp_path):
    engine = _engine(tmp_path / "legacy-null-workspace.db")
    _exec(engine, "CREATE TABLE session (id TEXT PRIMARY KEY, goal TEXT, workspace TEXT)")
    _exec(engine, "INSERT INTO session (id, goal, workspace) VALUES ('s1', 'g', NULL)")
    _exec(engine, f"CREATE TABLE {CLIENT_VERSION_TABLE} (version INTEGER PRIMARY KEY, applied_at TEXT)")
    _exec(engine, f"INSERT INTO {CLIENT_VERSION_TABLE} (version, applied_at) VALUES (1, '2020-01-01')")

    applied = run_migrations(engine, CLIENT_MIGRATIONS, version_table=CLIENT_VERSION_TABLE)

    assert applied == [2, 3, 4, 5, 6, 7]
    with engine.connect() as conn:
        row = conn.execute(text("SELECT workspace, main_workspace FROM session WHERE id='s1'")).first()
        assert row == (None, "")
        assert column_exists(conn, "session", "latest_context_checkpoint_id")
        assert table_exists(conn, "worktree_leases")
        assert current_version(conn, CLIENT_VERSION_TABLE) == 7


def test_client_migration_v3_to_v7_adds_context_checkpoint_cursor_and_indexes(tmp_path):
    engine = _engine(tmp_path / "legacy-context-v2.db")
    _exec(
        engine,
        "CREATE TABLE session ("
        "id TEXT PRIMARY KEY, goal TEXT, workspace TEXT, main_workspace TEXT"
        ")",
    )
    _exec(
        engine,
        "CREATE TABLE event ("
        "id TEXT PRIMARY KEY, session_id TEXT, task_id TEXT, type TEXT, source TEXT, "
        "payload_json TEXT, ts TEXT"
        ")",
    )
    _exec(
        engine,
        "CREATE TABLE context_frames ("
        "id TEXT PRIMARY KEY, session_id TEXT, event_id TEXT, event_ts TEXT, created_at TEXT"
        ")",
    )
    _exec(
        engine,
        "CREATE TABLE context_checkpoints ("
        "id TEXT PRIMARY KEY, session_id TEXT, created_at TEXT"
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

    assert applied == [3, 4, 5, 6, 7]
    with engine.connect() as conn:
        assert column_exists(conn, "session", "latest_context_checkpoint_id")
        assert column_exists(conn, "session", "context_materialized_until_ts")
        assert column_exists(conn, "session", "context_materialized_until_event_id")
        assert table_exists(conn, "worktree_leases")
        value = conn.execute(
            text("SELECT latest_context_checkpoint_id FROM session WHERE id='s1'")
        ).scalar_one()
        assert value == ""
        indexes = {
            row[1]
            for table in ("event", "context_frames", "context_checkpoints")
            for row in conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
        }
        assert "ix_event_session_ts_id" in indexes
        assert "ix_context_frames_session_event_order" in indexes
        assert "ix_context_checkpoints_session_created_id" in indexes
        assert current_version(conn, CLIENT_VERSION_TABLE) == 7


def test_client_store_upgrade_from_v5_keeps_session_and_creates_worktree_lease(tmp_path):
    db_path = tmp_path / "legacy-v5.db"
    engine = _engine(db_path)
    _exec(
        engine,
        "CREATE TABLE session ("
        "id TEXT PRIMARY KEY, goal TEXT, plan TEXT NOT NULL DEFAULT '', "
        "latest_context_checkpoint_id TEXT NOT NULL DEFAULT '', "
        "status TEXT NOT NULL DEFAULT 'idle', workspace TEXT NOT NULL DEFAULT '', "
        "main_workspace TEXT NOT NULL DEFAULT '', "
        "context_materialized_until_ts TEXT NOT NULL DEFAULT '', "
        "context_materialized_until_event_id TEXT NOT NULL DEFAULT '', "
        "agent_type TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', "
        "updated_at TEXT NOT NULL DEFAULT ''"
        ")",
    )
    _exec(
        engine,
        "INSERT INTO session (id, goal, workspace, main_workspace) "
        "VALUES ('s1', 'g', 'E:/AutoWorkAgent', 'E:/AutoWorkAgent')",
    )
    _exec(engine, f"CREATE TABLE {CLIENT_VERSION_TABLE} (version INTEGER PRIMARY KEY, applied_at TEXT)")
    for version in range(1, 6):
        _exec(
            engine,
            f"INSERT INTO {CLIENT_VERSION_TABLE} (version, applied_at) "
            f"VALUES ({version}, '2020-01-0{version}')",
        )

    st = Store(str(db_path))
    st.init()
    st.init()

    session = st.get_session("s1")
    assert session.workspace == "E:/AutoWorkAgent"
    assert session.main_workspace == "E:/AutoWorkAgent"
    assert st.schema_version() == 7
    lease = st.add_worktree_lease(
        WorktreeLease(
            id="lease-1",
            repo_root="E:/AutoWorkAgent",
            main_workspace="E:/AutoWorkAgent",
            worktree_path="E:/AutoWorkAgent-worktrees/s1",
            branch="foreman/s1/task",
            base_ref="main",
            base_sha="base123",
            head_sha="base123",
            session_id="s1",
            task_id="t1",
        )
    )
    assert lease.id == "lease-1"
    assert st.get_active_worktree_lease(session_id="s1").base_sha == "base123"


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
