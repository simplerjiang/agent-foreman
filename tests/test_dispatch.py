"""Tests for foreman dispatch (TASKS T1.9) — fakes only, never spawns real claude/codex."""

from __future__ import annotations

from typer.testing import CliRunner

from _fakes import FakeProc, fake_adapter

from foreman.client.agents.claude_code import ClaudeCodeAdapter
from foreman.client.agents.runner import Runner
from foreman.client.dispatch import build_session_task, run_dispatch
from foreman.client.store import Store
from foreman.shared.config import AgentCfg, Config
from foreman.shared.events import EventBus


def _store(tmp_path) -> Store:
    st = Store(str(tmp_path / "t.db"))
    st.init()
    return st


def test_build_session_task(tmp_path):
    st = _store(tmp_path)
    session, task = build_session_task(st, "do X", str(tmp_path), "claude-code")
    assert session.goal == "do X" and session.agent_type == "claude-code"
    assert task.session_id == session.id and task.instruction == "do X"
    assert [s.id for s in st.get_sessions()] == [session.id]


async def test_run_dispatch_persists_events(tmp_path):
    st = _store(tmp_path)
    bus = EventBus()
    runner = Runner(Config(), bus, st)
    proc = FakeProc(stdout_lines=[b'{"type":"assistant"}\n', b'{"type":"result"}\n'])
    runner.adapters["claude-code"] = fake_adapter(
        ClaudeCodeAdapter, AgentCfg(command="claude"), proc
    )
    sid, n = await run_dispatch(
        Config(), "do X", str(tmp_path), "claude-code", store=st, bus=bus, runner=runner
    )
    assert n == 3
    assert [e.type for e in st.get_events(sid)] == ["agent_start", "agent_output", "stop"]


def test_cli_dispatch_wires(monkeypatch, tmp_path):
    import foreman.client.dispatch as dmod
    from foreman.__main__ import app

    seen = {}

    async def fake_run(cfg, task, workspace, agent, **kw):
        seen.update(kw)
        return "sid123", 5

    monkeypatch.setattr(dmod, "run_dispatch", fake_run)
    r = CliRunner().invoke(
        app, ["dispatch", "do X", "--workspace", str(tmp_path), "--model", "gpt-5"]
    )
    assert r.exit_code == 0
    assert "sid123" in r.output and "5 events" in r.output
    assert seen["model"] == "gpt-5"
