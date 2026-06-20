"""Tests for ClaudeCodeAdapter lifecycle + stream parsing (TASKS T1.3–T1.5).

Lifecycle is inherited from SubprocessCliAdapter; shared fakes live in _fakes.py.
"""

from __future__ import annotations

from _fakes import FakeProc, fake_adapter

from foreman.client.agents.claude_code import ClaudeCodeAdapter
from foreman.shared.config import AgentCfg


def _cfg() -> AgentCfg:
    return AgentCfg(command="claude")


def test_build_cmd():
    a = ClaudeCodeAdapter(_cfg())
    assert a._build_cmd("do X") == [
        "claude", "-p", "do X", "--output-format", "stream-json", "--verbose",
    ]


def test_build_resume_cmd():
    a = ClaudeCodeAdapter(_cfg())
    assert a._build_resume_cmd("do Y", "sess-1") == [
        "claude", "-p", "do Y", "--resume", "sess-1", "--output-format", "stream-json", "--verbose",
    ]


async def test_start_registers_and_returns_handle(tmp_path):
    proc = FakeProc(pid=4321)
    a = fake_adapter(ClaudeCodeAdapter, _cfg(), proc)
    h = await a.start("do X", tmp_path, "sess1")
    assert h.pid == 4321 and h.session_id == "sess1" and h.id == "sess1:4321"
    assert a._procs[h.id] is proc
    assert a.spawned_cmd[:2] == ["claude", "-p"]
    assert a.spawned_cwd == tmp_path


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
