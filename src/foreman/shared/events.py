"""Events: the AgentEvent record, the event-type vocabulary, and the in-process bus.

Shared because both the PC app (client) and the relay/server speak in these events.
See docs/DESIGN.zh-CN.md §7.1 for the event-type enum.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

# The event-type vocabulary (DESIGN §7.1). Kept as a frozenset of plain strings (not an
# Enum) so payloads stay JSON-friendly across the wire; use it for cheap validation.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent_output", "tool_pre", "tool_post", "stop", "git_diff", "git_commit",
        "review", "action_proposed", "audit", "card_decided", "checkpoint", "undo",
        "approval_req", "approval_decided", "briefing", "error", "dispatch",
        "health", "stall", "recover",
    }
)


@dataclass
class AgentEvent:
    type: str          # one of EVENT_TYPES
    source: str        # claude-code|codex|hook|git|process|supervisor|...
    session_id: str
    task_id: str | None = None
    payload: dict = field(default_factory=dict)
    ts: str = ""       # UTC ISO8601; set by the publisher


class EventBus:
    """In-process async pub/sub. `publish` fans out to all live subscribers.

    The bus itself only fans out. Persistence (writing each event to the store) is
    layered on top by the caller — in the client, the Runner persists *then* publishes
    so a late-connecting UI can still backfill from the store (DESIGN P1).
    """

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
