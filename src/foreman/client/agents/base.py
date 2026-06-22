"""AgentAdapter protocol — unify how we start/feed/observe/stop CLI agents.

See docs/ARCHITECTURE.md for the contract and docs/DESIGN.zh-CN.md §4.2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from foreman.shared.events import AgentEvent


@dataclass
class AgentHandle:
    """Opaque handle to a running agent process/session."""

    id: str
    session_id: str
    pid: int | None = None
    native_session_id: str | None = None  # e.g. Claude Code --resume id
    model: str = ""
    # Reasoning effort/速度档位 for this run (low|medium|high; "" = the CLI default). Remembered on
    # the handle so a resume (`send`) re-spawns with the same level. How it reaches the CLI differs
    # per adapter: codex passes a `-c model_reasoning_effort=` flag; claude sets an env var (§4.2).
    effort: str = ""


class AgentAdapter(Protocol):
    name: str  # "claude-code" | "codex"

    async def start(
        self,
        instruction: str,
        workspace: Path,
        session_id: str,
        model: str = "",
        effort: str = "",
    ) -> AgentHandle:
        """Launch the agent in `workspace` with the initial instruction."""
        ...

    async def send(self, handle: AgentHandle, text: str) -> None:
        """Append a follow-up instruction to a running agent (two-way control)."""

    def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        """Yield structured events parsed from the agent's output."""

    async def interrupt(self, handle: AgentHandle) -> None:
        """Pause/interrupt the agent (e.g. while awaiting approval)."""

    async def stop(self, handle: AgentHandle) -> None:
        """Terminate the agent process/session."""
