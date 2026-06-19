"""Shared base for subprocess-driven CLI agent adapters (Claude Code, Codex).

Both adapters spawn a CLI in the workspace, stream its stdout line-by-line into AgentEvents,
and stop it. Only the launch command (`_build_cmd`) and `name` differ — those are overridden by
subclasses. See docs/DESIGN.zh-CN.md §4.2.
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


class SubprocessCliAdapter:
    """Spawn → stream stdout → stop. Subclasses set `name` and `_build_cmd`."""

    name = "subprocess"

    def __init__(self, cfg: AgentCfg) -> None:
        self.cfg = cfg  # command, mode
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def _build_cmd(self, instruction: str) -> list[str]:
        raise NotImplementedError

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

    async def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        """Yield one AgentEvent per stdout line; capture the agent's native session id if present."""
        proc = self._procs.get(handle.id)
        if proc is None or proc.stdout is None:
            return
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            event = self._line_to_event(line, handle.session_id)
            if not handle.native_session_id:
                sid = event.payload.get("session_id")
                if sid:
                    handle.native_session_id = sid
            yield event

    def _line_to_event(self, line: str, session_id: str) -> AgentEvent:
        """Map one output line to an AgentEvent; non-JSON / non-object → raw agent_output.

        Conservative (CLI schemas drift, DESIGN §13.1): keep the full object in payload so detail
        views can extract tool calls later (§6.3); only a `result` line maps to `stop`.
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return make_event("agent_output", self.name, session_id, payload={"text": line})
        if not isinstance(obj, dict):
            return make_event("agent_output", self.name, session_id, payload={"text": line})
        etype = "stop" if obj.get("type") == "result" else "agent_output"
        return make_event(etype, self.name, session_id, payload=obj)

    async def send(self, handle: AgentHandle, text: str) -> None:
        raise NotImplementedError(f"{type(self).__name__}.send — roadmap P4")

    async def interrupt(self, handle: AgentHandle) -> None:
        raise NotImplementedError(f"{type(self).__name__}.interrupt — roadmap P3")

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
