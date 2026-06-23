"""CLI smoke tests (TASKS T0.4): the commands exist and the stubs behave.

Uses Typer's CliRunner — needs only base deps (typer/rich/pydantic), so it runs without the
client/server extras installed.
"""

from __future__ import annotations

from typer.testing import CliRunner

from foreman.__main__ import _CLI_CONSOLE, app

runner = CliRunner()


def test_version():
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0
    assert "Foreman v" in r.output


def test_cli_output_avoids_windows_legacy_renderer():
    assert _CLI_CONSOLE.legacy_windows is False


def test_app_command_registered():
    # `foreman app` now launches a blocking local app (server + native window), so we must NOT
    # invoke it in a test — just assert the command is registered (its presence in --help is
    # checked below too).
    names = {(c.name or c.callback.__name__) for c in app.registered_commands}
    assert "app" in names


def test_help_lists_core_commands():
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for cmd in ("app", "serve", "dispatch", "version"):
        assert cmd in r.output


def test_create_admin_bootstraps_first_admin(tmp_path):
    """The first-admin bootstrap path for team mode (issue #1 P1): creates an admin that can then
    log in. Needs the server extras (ServerStore/AuthManager); skip if not installed."""
    import pytest

    pytest.importorskip("sqlmodel")
    cfg_yaml = tmp_path / "config.yaml"
    db = tmp_path / "server.db"
    cfg_yaml.write_text(
        f"server:\n  mode: team\n  db_path: {db.as_posix()}\n", encoding="utf-8"
    )
    r = runner.invoke(
        app,
        ["create-admin", "boss", "--config", str(cfg_yaml), "--password", "sup3rsecret"],
    )
    assert r.exit_code == 0, r.output
    assert "admin 'boss' created" in r.output
    # the admin can authenticate
    from foreman.server.auth_manager import AuthManager
    from foreman.server.store import ServerStore

    store = ServerStore(str(db))
    store.init()
    login = AuthManager(store).login("boss", "sup3rsecret")
    assert login.get("ok") and login.get("role") == "admin"


def test_create_admin_duplicate_username_fails(tmp_path):
    import pytest

    pytest.importorskip("sqlmodel")
    cfg_yaml = tmp_path / "config.yaml"
    db = tmp_path / "server.db"
    cfg_yaml.write_text(
        f"server:\n  mode: team\n  db_path: {db.as_posix()}\n", encoding="utf-8"
    )
    args = ["create-admin", "boss", "--config", str(cfg_yaml), "--password", "sup3rsecret"]
    assert runner.invoke(app, args).exit_code == 0
    dup = runner.invoke(app, args)
    assert dup.exit_code == 1 and "exists" in dup.output
