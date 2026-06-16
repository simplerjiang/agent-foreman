"""Gate — classifies actions and holds dangerous ones for human approval.

Levels: safe | needs-strategy | requires-approval. See docs/DESIGN.zh-CN.md §6.
Wired to Claude Code's PreToolUse hook so dangerous tool calls can be blocked pending approval.
"""

from __future__ import annotations

from ..config import GatesCfg


class Gate:
    def __init__(self, cfg: GatesCfg) -> None:
        self.cfg = cfg

    def classify(self, action_text: str) -> str:
        """Return safe | needs-strategy | requires-approval based on configured patterns."""
        low = action_text.lower()
        if any(p.lower() in low for p in self.cfg.requires_approval):
            return "requires-approval"
        if any(p.lower() in low for p in self.cfg.needs_strategy):
            return "needs-strategy"
        return "safe"

    async def request_approval(self, session_id: str, action: str, diff_summary: str) -> str:
        """Create a pending approval, push a card to the phone, return approval id (P3)."""
        raise NotImplementedError("Gate.request_approval — roadmap P3")

    async def resolve(self, approval_id: str, decision: str, reason: str | None = None) -> None:
        """Apply approve/reject: resume or interrupt the agent accordingly (P3)."""
        raise NotImplementedError("Gate.resolve — roadmap P3")
