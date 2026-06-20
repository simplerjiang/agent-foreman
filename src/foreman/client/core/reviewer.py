"""Reviewer — sends a diff + goal (+ QA standard) to YOUR LLM and returns a structured verdict.

Triggered at checkpoints (Claude Code Stop hook, task completion, a batch of diffs). See
docs/DESIGN.zh-CN.md §4.1, §5.3, §11(P2).

Complement to the Auditor (DESIGN §4.1): **Auditor judges "should this be done" *before* an action;
Reviewer judges "was it done well" *after*.** The hard, irreversible dangers (``rm -rf``,
``git push -f``, secrets…) are caught deterministically by the Gate, not by this LLM — the Reviewer
only weighs the gray "is this good / is this garbage" question and routes the outcome:

  - ``approve``         → record and continue.
  - ``request_changes`` → feed the notes back to the agent (Runner.send, P4).
  - ``escalate``        → hand to the Gate → you (a decision card; §5.3). On escalate the card offers
                          ``[⛔ 撤掉重来]`` (one-click undo to the pre-step checkpoint, T2.3).

**Conservative by default (DESIGN §6.7 "从严默认"):** when the LLM reply can't be parsed or names an
unknown verdict, we do NOT silently approve — we ``escalate`` with ``needs_human=True`` so a person
looks. Per DESIGN §15 the system prompt is suffixed with ``language_directive`` so every
human-facing field (summary / risks / suggestions) comes back in the user's chosen language.

Live API hookup needs the user's own LLM key (config.llm + .env); prompt-building and parsing are
exercised here via a mock transport (see tests). The diff itself comes from the Checkpoint Manager
(``CheckpointManager.diff``), which captures new files too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from foreman.shared.i18n import language_directive
from foreman.shared.llm import LLMClient, Message

# Verdicts (DESIGN §4.1). request_changes/escalate keep the agent honest; only approve continues.
APPROVE = "approve"
REQUEST_CHANGES = "request_changes"
ESCALATE = "escalate"
VALID_VERDICTS: frozenset[str] = frozenset({APPROVE, REQUEST_CHANGES, ESCALATE})

REVIEW_SYSTEM = (
    "You are a senior engineer reviewing an AI coding agent's work AFTER it ran (a post-hoc code "
    "review, not a pre-flight approval). You are given the task goal, an optional QA standard, and a "
    "git diff of what changed. Judge whether the change actually meets the goal and the standard, and "
    "whether it is correct, safe, and not garbage. Be adversarial: the agent may have written the "
    "diff, so do not take it on faith. Choose exactly one verdict: 'approve' (meets the goal, ship "
    "it), 'request_changes' (fixable problems the agent should redo), or 'escalate' (risky, "
    "ambiguous, or you are unsure — a human should look). When in doubt, prefer 'escalate' over "
    "'approve'. Respond with ONLY a JSON object: "
    '{"verdict": "approve|request_changes|escalate", "summary": str, "risks": [str], '
    '"suggestions": [str], "needs_human": bool}.'
)

# Keep the diff bounded so a huge change can't blow the token budget; the tail is dropped with a
# marker so the model knows the review was on a truncated diff.
DEFAULT_MAX_DIFF_CHARS = 20000


@dataclass
class ReviewResult:
    verdict: str  # approve | request_changes | escalate
    summary: str = ""
    risks: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    needs_human: bool = False


def _as_str(value: object) -> str:
    """Coerce any JSON scalar to a stripped string (None → "")."""
    return "" if value is None else str(value).strip()


def _as_str_list(value: object) -> list[str]:
    """Coerce to a clean list[str]: a list of scalars, or a lone string, dropping blanks."""
    if isinstance(value, list):
        return [s for s in (_as_str(v) for v in value) if s]
    s = _as_str(value)
    return [s] if s else []


def _extract_json_object(raw: str) -> dict | None:
    """Pull the first JSON object out of an LLM reply (handles ```json fences / surrounding prose)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        # Drop a leading ```/```json fence line and any trailing fence.
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if "```" in text:
            text = text[: text.rfind("```")]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    # Fallback: grab the outermost {...} span and try that.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def parse_review(raw: str) -> ReviewResult:
    """Parse an LLM reply into a validated ``ReviewResult``; escalate on anything unrecognized.

    DESIGN §6.7 "从严默认": an unparseable reply or an unknown verdict never becomes a silent
    ``approve`` — it returns ``escalate`` with ``needs_human=True`` so a person decides.
    """
    obj = _extract_json_object(raw)
    if obj is None:
        return ReviewResult(
            verdict=ESCALATE,
            summary="reviewer output was not valid JSON",
            needs_human=True,
        )
    verdict = _as_str(obj.get("verdict")).lower()
    risks = _as_str_list(obj.get("risks"))
    suggestions = _as_str_list(obj.get("suggestions"))
    if verdict not in VALID_VERDICTS:
        return ReviewResult(
            verdict=ESCALATE,
            summary=_as_str(obj.get("summary")) or f"unrecognized verdict: {verdict!r}",
            risks=risks,
            suggestions=suggestions,
            needs_human=True,
        )
    # escalate always implies a human is needed, regardless of what the model put in needs_human.
    needs_human = bool(obj.get("needs_human", False)) or verdict == ESCALATE
    return ReviewResult(
        verdict=verdict,
        summary=_as_str(obj.get("summary")),
        risks=risks,
        suggestions=suggestions,
        needs_human=needs_human,
    )


def build_review_prompt(
    goal: str, diff: str, *, context: str = "", qa_standard: str = "",
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> str:
    """Assemble the user prompt; truncate an over-long diff so token cost stays bounded."""
    body = diff or ""
    if len(body) > max_diff_chars:
        body = body[:max_diff_chars] + "\n…[diff truncated]…"
    parts = [f"# Goal\n{goal}"]
    if qa_standard:
        parts.append(f"# QA standard\n{qa_standard}")
    if context:
        parts.append(f"# Context\n{context}")
    parts.append(f"# Diff\n```diff\n{body}\n```")
    return "\n\n".join(parts)


class Reviewer:
    """Post-checkpoint LLM reviewer. ``language`` drives the output language (DESIGN §15)."""

    def __init__(self, llm: LLMClient, *, language: str = "zh") -> None:
        self.llm = llm
        self.language = language

    async def review(
        self, goal: str, diff: str, *, context: str = "", qa_standard: str = "",
        max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
    ) -> ReviewResult:
        """Review a diff against the task goal (+ optional QA standard); return a structured verdict."""
        system = REVIEW_SYSTEM + "\n" + language_directive(self.language)
        prompt = build_review_prompt(
            goal, diff, context=context, qa_standard=qa_standard, max_diff_chars=max_diff_chars
        )
        raw = await self.llm.complete(
            [Message("system", system), Message("user", prompt)], json_mode=True
        )
        return parse_review(raw)


__all__ = [
    "Reviewer",
    "ReviewResult",
    "parse_review",
    "build_review_prompt",
    "VALID_VERDICTS",
    "APPROVE",
    "REQUEST_CHANGES",
    "ESCALATE",
    "REVIEW_SYSTEM",
    "DEFAULT_MAX_DIFF_CHARS",
]
