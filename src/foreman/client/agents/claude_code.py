"""Claude Code adapter.

Headless mode (preferred, esp. on Windows):
    claude -p "<instruction>" --output-format stream-json --verbose
Parse stream-json line-by-line into AgentEvents (T1.4). Continue a session with
    claude -p "<follow-up>" --resume <session_id>   (T1.5/P4)
Real-time tool visibility also comes from Claude Code hooks (monitor/hooks.py).
See docs/DESIGN.zh-CN.md §4.2 / §10.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from foreman.shared.config import AgentCfg
from foreman.shared.events import AgentEvent

from .base import AgentHandle


class ClaudeCodeAdapter:
    name = "claude-code"

    def __init__(self, cfg: AgentCfg) -> None:
        self.cfg = cfg  # command, mode
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def _build_cmd(self, instruction: str) -> list[str]:
        """Headless invocation. `cfg.command` is the launcher (e.g. "claude"; the Windows shim is
        usually "claude.cmd")."""
        return [self.cfg.command, "-p", instruction,
                "--output-format", "stream-json", "--verbose"]

    async def _spawn(self, cmd: list[str], workspace: Path) -> asyncio.subprocess.Process:
        """Spawn the agent process. Overridable seam so tests can inject a fake process."""
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # hide the child console window
        return await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )

    async def start(self, instruction: str, workspace: Path, session_id: str) -> AgentHandle:
        proc = await self._spawn(self._build_cmd(instruction), workspace)
        handle = AgentHandle(id=f"{session_id}:{proc.pid}", session_id=session_id, pid=proc.pid)
        self._procs[handle.id] = proc
        return handle

    async def send(self, handle: AgentHandle, text: str) -> None:
        # T1.5/P4: re-invoke with --resume handle.native_session_id (headless).
        raise NotImplementedError("ClaudeCodeAdapter.send — roadmap T1.5/P4")

    async def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        # T1.4: read stdout lines, json.loads each, map to AgentEvent.
        raise NotImplementedError("ClaudeCodeAdapter.stream — roadmap T1.4")
        yield  # pragma: no cover  (marks this as an async generator)

    async def interrupt(self, handle: AgentHandle) -> None:
        raise NotImplementedError("ClaudeCodeAdapter.interrupt — roadmap P3")

    async def stop(self, handle: AgentHandle) -> None:
        """Terminate the agent process (graceful → kill) and deregister it."""
        proc = self._procs.pop(handle.id, None)
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
        except ProcessLookupError:
            pass  # already gone
