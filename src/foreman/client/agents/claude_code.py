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
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from foreman.shared.config import AgentCfg
from foreman.shared.events import AgentEvent, make_event

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
        """Yield one AgentEvent per stdout line (claude --output-format stream-json)."""
        proc = self._procs.get(handle.id)
        if proc is None or proc.stdout is None:
            return
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                yield self._line_to_event(line, handle.session_id)

    def _line_to_event(self, line: str, session_id: str) -> AgentEvent:
        """Map one stream-json line to an AgentEvent; non-JSON / non-object → raw agent_output.

        Conservative mapping (claude's schema can drift, DESIGN §13.1): the full object is kept in
        payload so detail views can extract tool calls etc. later (§6.3); only `result` is special.
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return make_event("agent_output", self.name, session_id, payload={"text": line})
        if not isinstance(obj, dict):
            return make_event("agent_output", self.name, session_id, payload={"text": line})
        etype = "stop" if obj.get("type") == "result" else "agent_output"
        return make_event(etype, self.name, session_id, payload=obj)

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
