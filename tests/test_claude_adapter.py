"""Tests for ClaudeCodeAdapter lifecycle (TASKS T1.3) — no real claude binary.

A FakeProc + an adapter subclass overriding _spawn() make start/stop deterministic.
"""

from __future__ import annotations

from foreman.client.agents.claude_code import ClaudeCodeAdapter
from foreman.shared.config import AgentCfg


class FakeProc:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.stdout = None

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
