"""Codex CLI adapter.

Non-interactive:  codex exec "<instruction>"
Codex has no hook mechanism today, so observation leans on output parsing + the git watcher.
Interactive follow-ups use a PTY (pywinpty on Windows). See docs/DESIGN.zh-CN.md §4.2/§4.3.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from foreman.shared.config import AgentCfg
from foreman.shared.events import AgentEvent
from .base import AgentHandle


class CodexAdapter:
    name = "codex"

    def __init__(self, cfg: AgentCfg) -> None:
        self.cfg = cfg

    async def start(self, instruction: str, workspace: Path, session_id: str) -> AgentHandle:
        raise NotImplementedError("CodexAdapter.start — roadmap P1")

    async def send(self, handle: AgentHandle, text: str) -> None:
        raise NotImplementedError("CodexAdapter.send — roadmap P1/P4")

    async def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError("CodexAdapter.stream — roadmap P1")
        yield  # pragma: no cover

    async def interrupt(self, handle: AgentHandle) -> None:
        raise NotImplementedError("CodexAdapter.interrupt — roadmap P3")

    async def stop(self, handle: AgentHandle) -> None:
        raise NotImplementedError("CodexAdapter.stop — roadmap P1")
