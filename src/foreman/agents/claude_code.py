"""Claude Code adapter.

Headless mode (preferred, esp. on Windows):
    claude -p "<instruction>" --output-format stream-json --verbose
Parse stream-json line-by-line into AgentEvents. Continue a session with
    claude -p "<follow-up>" --resume <session_id>
Real-time tool visibility comes from Claude Code hooks (see monitor/hooks.py),
not only from stdout. See docs/DESIGN.zh-CN.md §4.2 and §10.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from ..config import AgentCfg
from ..core.events import AgentEvent
from .base import AgentHandle


class ClaudeCodeAdapter:
    name = "claude-code"

    def __init__(self, cfg: AgentCfg) -> None:
        self.cfg = cfg  # command, mode

    async def start(self, instruction: str, workspace: Path, session_id: str) -> AgentHandle:
        # P1: spawn `claude -p ... --output-format stream-json` as an async subprocess
        # in `workspace`, capture stdout. On Windows, command resolves to claude.cmd.
        raise NotImplementedError("ClaudeCodeAdapter.start — roadmap P1")

    async def send(self, handle: AgentHandle, text: str) -> None:
        # P1/P4: re-invoke with --resume handle.native_session_id (headless), or write to pty.
        raise NotImplementedError("ClaudeCodeAdapter.send — roadmap P1")

    async def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        # P1: read stdout lines, json.loads each, map to AgentEvent.
        raise NotImplementedError("ClaudeCodeAdapter.stream — roadmap P1")
        yield  # pragma: no cover  (marks this as an async generator)

    async def interrupt(self, handle: AgentHandle) -> None:
        raise NotImplementedError("ClaudeCodeAdapter.interrupt — roadmap P3")

    async def stop(self, handle: AgentHandle) -> None:
        raise NotImplementedError("ClaudeCodeAdapter.stop — roadmap P1")
