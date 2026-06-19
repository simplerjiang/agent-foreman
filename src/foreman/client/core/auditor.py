"""Auditor — an independent second LLM that reviews each proposed command BEFORE it runs.

Complements the Reviewer: the Auditor judges "should we do this?" (pre-execution); the Reviewer
judges "was it done well?" (post-execution). The Auditor is prompted to be adversarial — its job is
to BLOCK garbage / off-target / dangerous / wasteful commands, defaulting to reject when unsure.
It runs in an independent context so it doesn't inherit the Operator's "I want to do this" bias.
See docs/DESIGN.zh-CN.md §6.1 / §6.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from foreman.shared.llm import LLMClient, Message

AUDIT_SYSTEM = (
    "You are an independent command auditor for an AI operator. Before any command runs, decide "
    "whether it should. BLOCK it if it is garbage, off-target, destructive, or wasteful. Default to "
    "reject when unsure. Respond as JSON: "
    '{"verdict": "pass|reject", "reasons": [str], "suggestions": [str]}.'
)


@dataclass
class AuditResult:
    verdict: str  # pass | reject
    reasons: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class Auditor:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def audit(self, command: str, rationale: str, context: str = "") -> AuditResult:
        """Independently review a proposed command. Roadmap P4 (LLM wired; parsing TODO)."""
        prompt = f"# Proposed command\n{command}\n\n# Rationale\n{rationale}\n\n# Context\n{context}"
        _raw = await self.llm.complete(
            [Message("system", AUDIT_SYSTEM), Message("user", prompt)], json_mode=True
        )
        # TODO(P4): parse _raw JSON into AuditResult with validation; default to reject on parse fail.
        raise NotImplementedError("Auditor.audit parsing — roadmap P4")
