"""Operator — condenses an agent's raw output and PROPOSES the next action(s) to run.

The Operator is the "driver" of the decision loop (DESIGN §6.1 / §4.1): it reads the latest
agent output, **condenses** it into a one-line human summary, judges the session state, and
**proposes** the next command(s) to execute. It is given the "hands" (a full MCP toolbelt, §4.7) —
but it can NEVER self-approve or self-execute. Every proposal it emits flows on to the Auditor
(independent pre-execution review, §6.2 / T4.2) and the Gate (deterministic reversibility gating,
§6.6) before anything runs, then to a decision card for you. So the Operator proposes freely; the
safety comes from the layers after it.

Complement to the other two LLM roles:
  - **Operator** (here)   — "what should we do next?" (proposes).
  - **Auditor** (T4.2)    — "should we do this?"   (judges a proposal *before* it runs).
  - **Reviewer** (T2.7)   — "was it done well?"     (judges a diff *after* it ran).

Each proposed action maps to the ``actions`` row schema (DESIGN §7.1):
``{kind, command, rationale, expected_effect, reversible}``. The ``reversible`` hint is
**conservative by default**: when the model omits it or it isn't a clear "yes", we treat the action
as irreversible (``reversible=False``) so the Gate forces an approval rather than risking an
auto-run of something unrecoverable (DESIGN §6.6 "可回退的放手，不可逆的必先问").

Parsing is conservative too (DESIGN §6.7 "从严默认"): an unparseable reply yields **no proposals**
(so nothing dangerous is invented from garbage) and a ``blocked`` state so a human looks. Per
DESIGN §15 the system prompt is suffixed with ``language_directive`` so the human-facing ``summary``
comes back in the user's chosen language.

Live API hookup needs the user's own LLM key (config.llm + .env); prompt-building and parsing are
exercised here via a mock transport (see tests) — no network, no tokens spent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from foreman.shared.i18n import language_directive
from foreman.shared.llm import LLMClient, Message
from foreman.shared.llm.trace import trace_context

# Session states the Operator may report (DESIGN §4.1). Underscore form matches the DB columns.
RUNNING = "running"
IDLE = "idle"
BLOCKED = "blocked"
WAITING_APPROVAL = "waiting_approval"
DONE = "done"
FAILED = "failed"
VALID_STATES: frozenset[str] = frozenset(
    {RUNNING, IDLE, BLOCKED, WAITING_APPROVAL, DONE, FAILED}
)

OPERATOR_SYSTEM = (
    "You are the Operator driving an AI coding agent (Claude Code / Codex). You are given the "
    "session goal and the agent's latest raw output. Do three things: (1) CONDENSE the output into "
    "one short human-readable sentence; (2) judge the session STATE as exactly one of "
    "'running', 'idle', 'blocked', 'waiting_approval', 'done', 'failed'; (3) PROPOSE the next "
    "action(s) to run — zero or more, ordered, most important first. You do NOT execute anything: "
    "every proposal is independently audited and gated before it runs, so propose honestly and do "
    "not pad. For each action set 'reversible' to true ONLY if it is clearly recoverable (editing "
    "files in the workspace, running tests, reading). Set it to false for anything irreversible — "
    "git push, deploy, deleting data, changing secrets, global installs, privileged/admin commands. "
    "When in doubt, reversible:false. Respond with ONLY a JSON object: "
    '{"summary": str, "state": "running|idle|blocked|waiting_approval|done|failed", '
    '"proposals": [{"kind": str, "command": str, "rationale": str, "expected_effect": str, '
    '"reversible": bool}]}.'
)

# Keep the observed output bounded so a chatty agent can't blow the token budget; drop the head
# (the tail is the most recent / most relevant part) with a marker.
DEFAULT_MAX_OUTPUT_CHARS = 20000


@dataclass
class ProposedAction:
    """One action the Operator wants to run next — maps to an ``actions`` row (DESIGN §7.1)."""

    command: str
    kind: str = "shell"  # shell | file_edit | agent_instruction | mcp_tool | ...
    rationale: str = ""
    expected_effect: str = ""
    reversible: bool = False  # conservative: unknown reversibility → gated, not auto-run


@dataclass
class OperatorResult:
    summary: str = ""
    state: str = BLOCKED  # conservative default when unknown — surfaces for a human look
    proposals: list[ProposedAction] = field(default_factory=list)


def _as_str(value: object) -> str:
    """Coerce any JSON scalar to a stripped string (None → "")."""
    return "" if value is None else str(value).strip()


def _as_reversible(value: object) -> bool:
    """Conservative bool coercion: only an explicit true / 'true' / 'yes' counts as reversible."""
    if isinstance(value, bool):
        return value
    return _as_str(value).lower() in ("true", "yes", "1")


def _extract_json_object(raw: str) -> dict | None:
    """Pull the first JSON object out of an LLM reply (handles ```json fences / surrounding prose)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if "```" in text:
            text = text[: text.rfind("```")]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _parse_proposal(obj: object) -> ProposedAction | None:
    """Coerce one raw proposal dict into a ProposedAction; drop it if it has no command."""
    if not isinstance(obj, dict):
        return None
    command = _as_str(obj.get("command"))
    if not command:
        return None  # a proposal with nothing to run is meaningless — drop it
    kind = _as_str(obj.get("kind")) or "shell"
    return ProposedAction(
        command=command,
        kind=kind,
        rationale=_as_str(obj.get("rationale")),
        expected_effect=_as_str(obj.get("expected_effect")),
        reversible=_as_reversible(obj.get("reversible")),
    )


def parse_operator(raw: str) -> OperatorResult:
    """Parse an LLM reply into a validated ``OperatorResult``; conservative on anything unrecognized.

    DESIGN §6.7 "从严默认": an unparseable reply or an unknown state never invents proposals — it
    returns no proposals and a ``blocked`` state so a human looks rather than something running off a
    garbled command.
    """
    obj = _extract_json_object(raw)
    if obj is None:
        return OperatorResult(summary="operator output was not valid JSON", state=BLOCKED)
    state = _as_str(obj.get("state")).lower()
    if state not in VALID_STATES:
        state = BLOCKED
    raw_proposals = obj.get("proposals")
    proposals: list[ProposedAction] = []
    if isinstance(raw_proposals, list):
        for item in raw_proposals:
            action = _parse_proposal(item)
            if action is not None:
                proposals.append(action)
    return OperatorResult(summary=_as_str(obj.get("summary")), state=state, proposals=proposals)


def build_operator_prompt(
    goal: str,
    agent_output: str,
    *,
    context: str = "",
    recent_actions: str = "",
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> str:
    """Assemble the user prompt; keep the *tail* of an over-long output so token cost stays bounded."""
    body = agent_output or ""
    if len(body) > max_output_chars:
        body = "…[output truncated]…\n" + body[-max_output_chars:]
    parts = [f"# Goal\n{goal}"]
    if context:
        parts.append(f"# Context\n{context}")
    if recent_actions:
        parts.append(f"# Recent actions\n{recent_actions}")
    parts.append(f"# Agent output\n{body}")
    return "\n\n".join(parts)


class Operator:
    """The LLM "driver": condense output + propose next action(s). ``language`` drives output (§15)."""

    def __init__(self, llm: LLMClient, *, language: str = "zh") -> None:
        self.llm = llm
        self.language = language

    async def observe(
        self,
        goal: str,
        agent_output: str,
        *,
        context: str = "",
        recent_actions: str = "",
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> OperatorResult:
        """Condense the agent's output and propose the next action(s). Proposals are NOT executed."""
        system = OPERATOR_SYSTEM + "\n" + language_directive(self.language)
        prompt = build_operator_prompt(
            goal,
            agent_output,
            context=context,
            recent_actions=recent_actions,
            max_output_chars=max_output_chars,
        )
        with trace_context(phase="operator"):
            raw = await self.llm.complete(
                [Message("system", system), Message("user", prompt)], json_mode=True
            )
        return parse_operator(raw)


__all__ = [
    "Operator",
    "OperatorResult",
    "ProposedAction",
    "parse_operator",
    "build_operator_prompt",
    "VALID_STATES",
    "RUNNING",
    "IDLE",
    "BLOCKED",
    "WAITING_APPROVAL",
    "DONE",
    "FAILED",
    "OPERATOR_SYSTEM",
    "DEFAULT_MAX_OUTPUT_CHARS",
]
