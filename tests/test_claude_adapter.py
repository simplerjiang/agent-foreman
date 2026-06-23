"""Tests for ClaudeCodeAdapter lifecycle + stream parsing (TASKS T1.3–T1.5).

Lifecycle is inherited from SubprocessCliAdapter; shared fakes live in _fakes.py.
"""

from __future__ import annotations

import shutil

from _fakes import FakeProc, fake_adapter

from foreman.client.agents import _subprocess as subproc
from foreman.client.agents.claude_code import ClaudeCodeAdapter
from foreman.shared.config import AgentCfg


def _cfg(model: str = "") -> AgentCfg:
    return AgentCfg(command="claude", model=model)


def test_build_cmd():
    a = ClaudeCodeAdapter(_cfg())
    assert a._build_cmd("do X") == [
        "claude", "-p", "do X", "--permission-mode", "bypassPermissions", "--tools", "default",
        "--output-format", "stream-json", "--verbose",
    ]
    assert a._build_cmd("do X", "sonnet") == [
        "claude", "-p", "do X", "--model", "sonnet",
        "--permission-mode", "bypassPermissions", "--tools", "default",
        "--output-format", "stream-json", "--verbose",
    ]


def test_build_resume_cmd():
    a = ClaudeCodeAdapter(_cfg())
    assert a._build_resume_cmd("do Y", "sess-1") == [
        "claude", "-p", "do Y", "--resume", "sess-1",
        "--permission-mode", "bypassPermissions", "--tools", "default",
        "--output-format", "stream-json", "--verbose",
    ]
    assert a._build_resume_cmd("do Y", "sess-1", "sonnet") == [
        "claude", "-p", "do Y", "--resume", "sess-1", "--model", "sonnet",
        "--permission-mode", "bypassPermissions", "--tools", "default",
        "--output-format", "stream-json", "--verbose",
    ]


def test_full_access_can_be_disabled():
    a = ClaudeCodeAdapter(AgentCfg(command="claude", full_access=False))
    assert a._build_cmd("do X") == [
        "claude", "-p", "do X", "--output-format", "stream-json", "--verbose",
    ]


async def test_start_registers_and_returns_handle(tmp_path):
    proc = FakeProc(pid=4321)
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), proc)
    h = await a.start("do X", tmp_path, "sess1")
    assert h.pid == 4321 and h.session_id == "sess1" and h.id == "sess1:4321"
    assert a._procs[h.id] is proc
    assert a.spawned_cmd[:2] == ["claude", "-p"]
    assert a.spawned_cwd == tmp_path


async def test_start_uses_config_model(tmp_path):
    proc = FakeProc(pid=4321)
    a = fake_adapter(ClaudeCodeAdapter, _cfg("sonnet"), proc)
    h = await a.start("do X", tmp_path, "sess1")
    assert h.model == "sonnet"
    assert "--model" in a.spawned_cmd and "sonnet" in a.spawned_cmd


async def test_effort_maps_to_env_not_cmd(tmp_path):
    # Claude has no headless flag for reasoning level — it rides the CLAUDE_CODE_EFFORT_LEVEL env var.
    proc = FakeProc(pid=4321)
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), proc)
    h = await a.start("do X", tmp_path, "sess1", effort="high")
    assert h.effort == "high"
    assert "high" not in a.spawned_cmd  # NOT a command flag
    assert a.spawned_env == {"CLAUDE_CODE_EFFORT_LEVEL": "high"}


async def test_no_effort_no_env(tmp_path):
    proc = FakeProc(pid=4321)
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), proc)
    await a.start("do X", tmp_path, "sess1")
    assert not a.spawned_env  # empty → no env override (inherit parent only)


def test_resolve_argv_uses_pathext_shim(monkeypatch):
    # Windows installs npm CLIs as claude.CMD; create_subprocess_exec("claude") → WinError 2 (issue
    # #3). We resolve argv[0] via shutil.which (PATHEXT-aware) and spawn the absolute path instead.
    a = ClaudeCodeAdapter(_cfg())
    monkeypatch.setattr(shutil, "which",
                        lambda name: r"C:\npm\claude.CMD" if name == "claude" else None)
    assert a._resolve_argv(["claude", "-p", "x"]) == [r"C:\npm\claude.CMD", "-p", "x"]


def test_resolve_argv_prefers_cmd_when_powershell_shim_exists(monkeypatch, tmp_path):
    cmd = tmp_path / "claude.cmd"
    ps1 = tmp_path / "claude.ps1"
    cmd.write_text("@echo off\n", encoding="utf-8")
    ps1.write_text("Write-Output nope\n", encoding="utf-8")
    a = ClaudeCodeAdapter(_cfg())
    monkeypatch.setattr(subproc, "_is_windows", lambda: True)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: str(ps1) if name == "claude" else None)
    assert a._resolve_argv(["claude", "-p", "x"]) == [str(cmd), "-p", "x"]


def test_resolve_argv_keeps_original_when_unresolved(monkeypatch):
    # Genuinely-not-installed: which returns None → keep the bare name so it still errors as missing.
    a = ClaudeCodeAdapter(_cfg())
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("PATH", "")
    assert a._resolve_argv(["claude", "-p", "x"]) == ["claude", "-p", "x"]


async def test_stop_terminates_and_deregisters(tmp_path):
    proc = FakeProc()
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), proc)
    h = await a.start("x", tmp_path, "s")
    await a.stop(h)
    assert proc.terminated is True
    assert h.id not in a._procs


async def test_stop_is_noop_when_already_exited(tmp_path):
    proc = FakeProc()
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), proc)
    h = await a.start("x", tmp_path, "s")
    proc.returncode = 0  # already exited before stop
    await a.stop(h)
    assert proc.terminated is False
    assert h.id not in a._procs


async def test_stream_parses_stream_json(tmp_path):
    lines = [
        b'{"type":"system","subtype":"init","session_id":"abc"}\n',
        b'{"type":"assistant","message":{"content":"hello"}}\n',
        b"not json at all\n",
        b"42\n",  # valid JSON but not an object -> raw fallback
        b'{"type":"result","result":"done"}\n',
    ]
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), FakeProc(stdout_lines=lines))
    h = await a.start("x", tmp_path, "s")
    events = [e async for e in a.stream(h)]

    assert [e.type for e in events] == [
        "agent_output", "agent_output", "agent_output", "agent_output", "stop",
    ]
    assert all(e.source == "claude-code" and e.session_id == "s" and e.ts for e in events)
    assert events[2].payload == {"text": "not json at all"}
    assert events[3].payload == {"text": "42"}
    assert events[4].payload["result"] == "done"


async def test_stream_captures_native_session_id(tmp_path):
    lines = [
        b'{"type":"system","subtype":"init","session_id":"claude-abc"}\n',
        b'{"type":"result","result":"done"}\n',
    ]
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), FakeProc(stdout_lines=lines))
    h = await a.start("x", tmp_path, "s")
    assert h.native_session_id is None
    _ = [e async for e in a.stream(h)]
    assert h.native_session_id == "claude-abc"


async def test_stream_classifies_thinking_blocks(tmp_path):
    lines = [
        b'{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"plan"}]}}\n',
        b'{"type":"result","result":"done"}\n',
    ]
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), FakeProc(stdout_lines=lines))
    h = await a.start("x", tmp_path, "s")
    events = [e async for e in a.stream(h)]

    assert [e.type for e in events] == ["agent_reasoning", "stop"]
    assert events[0].source == "claude-code"
