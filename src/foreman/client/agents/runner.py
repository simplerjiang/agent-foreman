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
        self.handles: dict[str, AgentHandle] = {}
        self._pumps: dict[str, asyncio.Task] = {}
        # Which adapter drives each live handle — so send/interrupt (two-way control) can route
        # back to the right CLI (DESIGN §4.2 send/interrupt). Keyed by handle.id, like handles.
        self._adapter_by_handle: dict[str, AgentAdapter] = {}
        # Most recent live handle per session — the decision loop addresses agents by session id.
        self._handle_by_session: dict[str, AgentHandle] = {}
        self.sync_config()

    def sync_config(self) -> None:
        """Refresh adapters after the local settings page changes agent config."""
        self.adapters = {}
        if (c := self.cfg.agents.get("claude-code")) and c.enabled:
            self.adapters["claude-code"] = ClaudeCodeAdapter(c)
        if (c := self.cfg.agents.get("codex")) and c.enabled:
            self.adapters["codex"] = CodexAdapter(c)

    async def launch(
        self,
        agent: str,
        instruction: str,
        workspace: Path,
        session_id: str,
        model: str = "",
        effort: str = "",
    ) -> AgentHandle:
        """Start an agent; stream its events to store+bus in the background. Returns immediately."""
        adapter = self.adapters.get(agent)
        if adapter is None:
            raise ValueError(f"agent not enabled: {agent!r} (enabled: {sorted(self.adapters)})")
        handle = await adapter.start(instruction, workspace, session_id, model=model, effort=effort)
        self.handles[handle.id] = handle
        self._adapter_by_handle[handle.id] = adapter
        self._handle_by_session[session_id] = handle
        self._pumps[handle.id] = asyncio.create_task(self._pump(adapter, handle))
        return handle

    def handle_for_session(self, session_id: str) -> AgentHandle | None:
        """The most recent live handle for a session (the decision loop's `agent_instruction` target)."""
        return self._handle_by_session.get(session_id)

    def _adapter_of(self, handle: AgentHandle) -> AgentAdapter:
        adapter = self._adapter_by_handle.get(handle.id)
        if adapter is None:
            raise ValueError(f"no live adapter for handle {handle.id!r}")
        return adapter

    async def send(self, handle: AgentHandle, text: str) -> None:
        """Append a follow-up instruction to a running agent — two-way control (DESIGN §4.2).

        Delegates to the per-handle adapter (which resumes the session, e.g. `--resume`), then
        restarts the background pump so the resumed output streams to store+bus like the first run.
        """
        adapter = self._adapter_of(handle)
        await adapter.send(handle, text)
        # The original stream ended when the one-shot run finished; resume produced a fresh process,
        # so re-pump to wire its output back to store+bus. Cancel any still-running prior pump first
        # so two pumps never write the same handle's stream concurrently.
        self._cancel_pump(handle.id)
        self._pumps[handle.id] = asyncio.create_task(self._pump(adapter, handle))

    async def interrupt(self, handle: AgentHandle) -> None:
        """Pause/interrupt a running agent (e.g. while awaiting approval). DESIGN §4.2 / §5.6."""
        await self._adapter_of(handle).interrupt(handle)
        self._cancel_pump(handle.id)

    def _cancel_pump(self, handle_id: str) -> None:
        """Cancel a handle's background pump task if it is still running."""
        task = self._pumps.get(handle_id)
        if task is not None and not task.done():
            task.cancel()

    async def stop(self, handle: AgentHandle) -> None:
        """Terminate an agent and drop all its bookkeeping (so a long-lived app doesn't leak)."""
        self._cancel_pump(handle.id)
        adapter = self._adapter_by_handle.get(handle.id)
        if adapter is not None:
            await adapter.stop(handle)
        self._pumps.pop(handle.id, None)
        self.handles.pop(handle.id, None)
        self._adapter_by_handle.pop(handle.id, None)
        if self._handle_by_session.get(handle.session_id) is handle:
            self._handle_by_session.pop(handle.session_id, None)

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
