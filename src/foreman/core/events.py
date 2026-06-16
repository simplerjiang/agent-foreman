"""In-process async event bus. Events are persisted first, then fanned out.

Subscribers include: PM Brain, Reviewer, Gate, and the WebSocket broadcaster.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class AgentEvent:
    type: str          # agent_output|tool_pre|tool_post|stop|git_diff|review|approval_req|error|...
    source: str        # claude-code|codex|hook|git|process
    session_id: str
    task_id: str | None = None
    payload: dict = field(default_factory=dict)
    ts: str = ""       # UTC ISO8601; set by the publisher


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[AgentEvent]] = set()

    async def publish(self, event: AgentEvent) -> None:
        for q in list(self._subscribers):
            await q.put(event)

    async def subscribe(self) -> AsyncIterator[AgentEvent]:
        q: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)
