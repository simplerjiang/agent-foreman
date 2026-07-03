"""AgentAdapter protocol — unify how we start/feed/observe/stop CLI agents.

See docs/ARCHITECTURE.md for the contract and docs/DESIGN.zh-CN.md §4.2.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
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
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    worktree: str = ""
    branch: str = ""
    base_ref: str = ""
    head_sha: str = ""
    agent_type: str = ""
    source: str = ""
    status: str = "running"
    # Reasoning effort/速度档位 for this run (low|medium|high; "" = the CLI default). Remembered on
    # the handle so a resume (`send`) re-spawns with the same level. How it reaches the CLI differs
    # per adapter: codex passes a `-c model_reasoning_effort=` flag; claude sets an env var (§4.2).
    effort: str = ""


def detect_git_refs(workspace: Path) -> dict[str, str]:
    """Best-effort git metadata for runtime context; empty fields when unavailable."""

    def run_git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(workspace), *args],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=2,
            ).strip()
        except Exception:
            return ""

    return {
        "branch": run_git("branch", "--show-current"),
        "head_sha": run_git("rev-parse", "HEAD"),
        "base_ref": run_git("merge-base", "HEAD", "origin/main"),
    }


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
