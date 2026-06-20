"""CLI smoke tests (TASKS T0.4): the commands exist and the stubs behave.

Uses Typer's CliRunner — needs only base deps (typer/rich/pydantic), so it runs without the
client/server extras installed.
"""

from __future__ import annotations

from typer.testing import CliRunner

from foreman.__main__ import app

runner = CliRunner()


def test_version():
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0
    assert "Foreman v" in r.output


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
