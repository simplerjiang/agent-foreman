"""Agent Runner — owns the lifecycle of agent handles and wires their streams to store + bus.

Picks the adapter by name, starts an agent in a workspace, and (in a background task per
session, so multiple CLIs run concurrently) PERSISTS each AgentEvent to the store THEN
publishes it to the EventBus — persist-first so a late-connecting UI can still backfill.
See docs/DESIGN.zh-CN.md §4.2.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from foreman.shared.config import Config
from foreman.shared.events import EventBus

from .base import AgentAdapter, AgentHandle
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter


class Runner:
    def __init__(self, cfg: Config, bus: EventBus, store) -> None:
        self.cfg = cfg
        self.bus = bus
        self.store = store  # foreman.client.store.Store (needs .add_event)
        self.adapters: dict[str, AgentAdapter] = {}
        if (c := cfg.agents.get("claude-code")) and c.enabled:
            self.adapters["claude-code"] = ClaudeCodeAdapter(c)
        if (c := cfg.agents.get("codex")) and c.enabled:
            self.adapters["codex"] = CodexAdapter(c)
        self.handles: dict[str, AgentHandle] = {}
        self._pumps: dict[str, asyncio.Task] = {}

    async def launch(
        self, agent: str, instruction: str, workspace: Path, session_id: str
    ) -> AgentHandle:
        """Start an agent; stream its events to store+bus in the background. Returns immediately."""
        adapter = self.adapters.get(agent)
        if adapter is None:
            raise ValueError(f"agent not enabled: {agent!r} (enabled: {sorted(self.adapters)})")
        handle = await adapter.start(instruction, workspace, session_id)
        self.handles[handle.id] = handle
        self._pumps[handle.id] = asyncio.create_task(self._pump(adapter, handle))
        return handle

    async def _pump(self, adapter: AgentAdapter, handle: AgentHandle) -> None:
        """Persist each streamed event THEN publish it."""
        async for event in adapter.stream(handle):
            self.store.add_event(event)
            await self.bus.publish(event)

    async def wait(self, handle: AgentHandle) -> None:
        """Await the background pump for a handle (shutdown / tests)."""
        task = self._pumps.get(handle.id)
        if task is not None:
            await task
