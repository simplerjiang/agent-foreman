"""Tests for ClaudeCodeAdapter lifecycle (TASKS T1.3) — no real claude binary.

A FakeProc + an adapter subclass overriding _spawn() make start/stop deterministic.
"""

from __future__ import annotations

from foreman.client.agents.claude_code import ClaudeCodeAdapter
from foreman.shared.config import AgentCfg


class _FakeStdout:
    """Async-iterable mimic of asyncio StreamReader (yields bytes lines)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> "_FakeStdout":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeProc:
    def __init__(self, pid: int = 4321, stdout_lines: list[bytes] | None = None) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.stdout = _FakeStdout(stdout_lines) if stdout_lines is not None else None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


class FakeClaudeAdapter(ClaudeCodeAdapter):
    def __init__(self, cfg: AgentCfg, proc: FakeProc) -> None:
        super().__init__(cfg)
        self._fake = proc
        self.spawned_cmd: list[str] | None = None
        self.spawned_cwd = None

    async def _spawn(self, cmd, workspace):
        self.spawned_cmd = cmd
        self.spawned_cwd = workspace
        return self._fake


def _cfg() -> AgentCfg:
    return AgentCfg(command="claude")


def test_build_cmd():
    a = ClaudeCodeAdapter(_cfg())
    assert a._build_cmd("do X") == [
        "claude", "-p", "do X", "--output-format", "stream-json", "--verbose",
    ]


async def test_start_registers_and_returns_handle(tmp_path):
    proc = FakeProc(pid=4321)
    a = FakeClaudeAdapter(_cfg(), proc)
    h = await a.start("do X", tmp_path, "sess1")
    assert h.pid == 4321 and h.session_id == "sess1" and h.id == "sess1:4321"
    assert a._procs[h.id] is proc
    assert a.spawned_cmd[:2] == ["claude", "-p"]
    assert a.spawned_cwd == tmp_path


async def test_stop_terminates_and_deregisters(tmp_path):
    proc = FakeProc()
    a = FakeClaudeAdapter(_cfg(), proc)
    h = await a.start("x", tmp_path, "s")
    await a.stop(h)
    assert proc.terminated is True
    assert h.id not in a._procs


async def test_stop_is_noop_when_already_exited(tmp_path):
    proc = FakeProc()
    a = FakeClaudeAdapter(_cfg(), proc)
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
    a = FakeClaudeAdapter(_cfg(), FakeProc(stdout_lines=lines))
    h = await a.start("x", tmp_path, "s")
    events = [e async for e in a.stream(h)]

    assert [e.type for e in events] == [
        "agent_output", "agent_output", "agent_output", "agent_output", "stop",
    ]
    assert all(e.source == "claude-code" and e.session_id == "s" and e.ts for e in events)
    assert events[2].payload == {"text": "not json at all"}  # non-JSON fallback
    assert events[3].payload == {"text": "42"}               # non-object JSON fallback
    assert events[4].payload["result"] == "done"


async def test_stream_captures_native_session_id(tmp_path):
    lines = [
        b'{"type":"system","subtype":"init","session_id":"claude-abc"}\n',
        b'{"type":"result","result":"done"}\n',
    ]
    a = FakeClaudeAdapter(_cfg(), FakeProc(stdout_lines=lines))
    h = await a.start("x", tmp_path, "s")
    assert h.native_session_id is None
    _ = [e async for e in a.stream(h)]
    assert h.native_session_id == "claude-abc"
