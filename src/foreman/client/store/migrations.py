"""Ordered schema migrations for the local (client) store (DESIGN §11.1 point 3).

The promise: "升级后你的历史还在" — when the code's schema moves ahead of an existing
`foreman.db`, bring it up smoothly without losing a row. `Store.init()` runs `create_all`
(builds *missing whole tables*) and then `run_migrations(self.engine, CLIENT_MIGRATIONS)`
(applies in-place table changes create_all can't, e.g. ADD COLUMN, and stamps the ledger).

Every `upgrade` MUST be idempotent (use the `add_column`/… helpers, which no-op when the
change is already present) so it's safe both on a fresh DB — where create_all already built
the latest columns — and as a re-run after a partial failure.

History:
- v1 — decisioncard.diff_stat. The 📎 changes line (§6.3) was retrofitted onto `decisioncard`
  after its first release; this formalizes the old `_ensure_columns` stop-gap into a real,
  ledgered migration so an upgrade of an old client DB picks it up exactly once.
- v2 — session.main_workspace. Session.workspace can move to a PM-created worktree; this keeps
  the original main workspace available for fallback when that worktree disappears.
- v3 — session.latest_context_checkpoint_id. Context v2 stores the latest recoverable active
  context checkpoint pointer on the session while keeping Session.plan as display/compat summary.
"""

from __future__ import annotations

from sqlalchemy import text

from foreman.shared.migrations import Migration, add_column, column_exists, table_exists

# The client's ledger table is named `schemaversion` (the SQLModel default for the local
# `SchemaVersion` model), kept for backward-compat with existing local DBs. The server's is
# `schema_version` (§7.1). Pass this so the migrator stamps the SAME table the ORM model reads.
CLIENT_VERSION_TABLE = "schemaversion"


def _v1_decisioncard_diff_stat(conn) -> None:
    add_column(conn, "decisioncard", "diff_stat", "TEXT NOT NULL DEFAULT ''")


def _v2_session_main_workspace(conn) -> None:
    add_column(conn, "session", "main_workspace", "TEXT NOT NULL DEFAULT ''")
    if table_exists(conn, "session") and column_exists(conn, "session", "main_workspace"):
        conn.execute(
            text("UPDATE session SET main_workspace = COALESCE(workspace, '') WHERE main_workspace = ''")
        )


def _v3_session_latest_context_checkpoint_id(conn) -> None:
    add_column(conn, "session", "latest_context_checkpoint_id", "TEXT NOT NULL DEFAULT ''")


CLIENT_MIGRATIONS: list[Migration] = [
    Migration(1, "decisioncard.diff_stat (📎 changes line, §6.3)", _v1_decisioncard_diff_stat),
    Migration(2, "session.main_workspace fallback for PM worktrees", _v2_session_main_workspace),
    Migration(
        3,
        "session.latest_context_checkpoint_id for Context v2 restore",
        _v3_session_latest_context_checkpoint_id,
    ),
]
