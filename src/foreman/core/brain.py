"""PM Brain — the decision loop.

Deterministic facts (process alive? diff present? idle too long?) are decided in code.
Only semantic judgments (does this output look stuck? should we escalate?) call the LLM.
This keeps token cost down and behavior stable. See docs/DESIGN.zh-CN.md §4.1.
"""

from __future__ import annotations

from ..llm import LLMClient


class PMBrain:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def assess_session(self, session_id: str) -> str:
        """Decide a session's current state and the next action.

        Returns one of: continue | review | escalate | brief | redirect.
        Roadmap P1/P4. Not implemented yet.
        """
        raise NotImplementedError("PMBrain.assess_session — roadmap P1/P4")

    async def make_plan(self, goal: str, workspace: str) -> str:
        """Turn a freeform goal into a short plan to seed a Root Session (P4)."""
        raise NotImplementedError("PMBrain.make_plan — roadmap P4")

    async def brief(self, session_id: str, kind: str = "active-briefing") -> str:
        """Produce a human-readable briefing (markdown) for the phone (P4)."""
        raise NotImplementedError("PMBrain.brief — roadmap P4")
