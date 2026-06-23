"""Shared test doubles for subprocess CLI adapters (TASKS T1.3–T1.6)."""

from __future__ import annotations


class FakeStdout:
    """Async-iterable mimic of asyncio StreamReader (yields bytes lines)."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> "FakeStdout":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self) -> bytes:
        out = b"".join(self._lines)
        self._lines = []
        return out


class FakeProc:
    def __init__(
        self,
        pid: int = 4321,
        stdout_lines: list[bytes] | None = None,
        stderr_lines: list[bytes] | None = None,
        returncode: int | None = None,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = returncode
        self.terminated = False
        self.killed = False
        self.stdout = FakeStdout(stdout_lines) if stdout_lines is not None else None
        self.stderr = FakeStdout(stderr_lines) if stderr_lines is not None else None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def fake_adapter(adapter_cls, cfg, proc: FakeProc):
    """Build an adapter whose _spawn returns `proc` (and records the spawned cmd/cwd/env)."""
    a = adapter_cls(cfg)
    a.spawned_cmd = None
    a.spawned_cwd = None
    a.spawned_env = None

    async def _spawn(cmd, workspace, env=None):
        a.spawned_cmd = cmd
        a.spawned_cwd = workspace
        a.spawned_env = env
        return proc

    a._spawn = _spawn  # instance attribute: not bound, so self isn't passed
    return a
