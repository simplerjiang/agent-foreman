"""Agent Runner — owns the lifecycle of agent handles and wires their streams to the bus.

Picks the right adapter by name, starts an agent in a workspace, and forwards every
AgentEvent into the EventBus (and the Store). See docs/DESIGN.zh-CN.md §4.2.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..core.events import EventBus
from .base import AgentAdapter, AgentHandle
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter


class Runner:
    def __init__(self, cfg: Config, bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.adapters: dict[str, AgentAdapter] = {}
        if (c := cfg.agents.get("claude-code")) and c.enabled:
            self.adapters["claude-code"] = ClaudeCodeAdapter(c)
        if (c := cfg.agents.get("codex")) and c.enabled:
            self.adapters["codex"] = CodexAdapter(c)
        self.handles: dict[str, AgentHandle] = {}

    async def launch(self, agent: str, instruction: str, workspace: Path, session_id: str):
        """Start an agent and begin forwarding its events to the bus (P1)."""
        raise NotImplementedError("Runner.launch — roadmap P1")
