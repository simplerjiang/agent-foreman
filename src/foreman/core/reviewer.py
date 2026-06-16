"""Reviewer — sends a diff + goal to YOUR LLM and returns a structured verdict.

Triggered at checkpoints (Claude Code Stop hook, task completion, a batch of diffs).
See docs/DESIGN.zh-CN.md §4.1 and §5.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm import LLMClient, Message

REVIEW_SYSTEM = (
    "You are a senior engineer reviewing an AI coding agent's work. "
    "Given the task goal and a git diff, judge whether to approve. "
    "Respond as JSON: {\"verdict\": \"approve|request_changes|escalate\", "
    "\"summary\": str, \"risks\": [str], \"suggestions\": [str], \"needs_human\": bool}."
)


@dataclass
class ReviewResult:
    verdict: str  # approve | request_changes | escalate
    summary: str = ""
    risks: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    needs_human: bool = False


class Reviewer:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def review(self, goal: str, diff: str, context: str = "") -> ReviewResult:
        """Review a diff against the task goal. Roadmap P2 (LLM call wired; parsing TODO)."""
        prompt = f"# Goal\n{goal}\n\n# Context\n{context}\n\n# Diff\n```diff\n{diff}\n```"
        _raw = await self.llm.complete(
            [Message("system", REVIEW_SYSTEM), Message("user", prompt)], json_mode=True
        )
        # TODO(P2): parse _raw JSON into ReviewResult with validation + fallback.
        raise NotImplementedError("Reviewer.review parsing — roadmap P2")
