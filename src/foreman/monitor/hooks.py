"""Claude Code hook receiver.

Claude Code POSTs hook events (PreToolUse / PostToolUse / Stop / Notification) to
/hooks on the local backend. This module maps each payload into an AgentEvent and,
for PreToolUse on dangerous tools, can return a blocking decision routed to the Gate.

The FastAPI route lives in server/app.py; the mapping logic lives here.
See docs/ARCHITECTURE.md and docs/DESIGN.zh-CN.md §10.
"""

from __future__ import annotations

from ..core.events import AgentEvent


def hook_to_event(hook_name: str, payload: dict, session_id: str) -> AgentEvent:
    """Map a Claude Code hook payload to an AgentEvent. (P2)"""
    type_map = {
        "PreToolUse": "tool_pre",
        "PostToolUse": "tool_post",
        "Stop": "stop",
        "SubagentStop": "stop",
        "Notification": "notification",
    }
    return AgentEvent(
        type=type_map.get(hook_name, "agent_output"),
        source="hook",
        session_id=session_id,
        payload={"hook": hook_name, **payload},
    )
