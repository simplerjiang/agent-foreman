"""PM agent orchestration for user-dispatched tasks.

This is intentionally small: the PM LLM turns the user's goal into the first agent prompt, then
reviews the captured agent timeline after each run and decides whether to stop or continue with a
follow-up. It does not execute tools directly; it only chooses how to steer the existing CLI agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from foreman.shared.i18n import language_directive
from foreman.shared.llm import LLMClient, Message

from .context_compression import context_pack_to_text, parse_context_pack

VALID_AGENTS = {"claude-code", "codex"}
VALID_EFFORTS = {"low", "medium", "high"}
MAX_EVENT_CHARS = 20000
MAX_COMPACT_CHARS = 12000

PLAN_SYSTEM = (
    "You are the PM agent for Foreman. Analyze the user's task before any coding CLI is launched. "
    "Choose which enabled coding agent should run, which coding-agent model/effort to use, and "
    "write the exact instruction for that agent. The dispatch model is already being used for you, "
    "the PM brain; never copy it into the coding-agent model field. Choose the coding agent "
    "yourself. Prefer the highest-capability enabled agent/model/effort over saving tokens; use "
    "high effort by default unless the task is clearly trivial. If no coding-agent model is "
    "configured, leave model empty so the CLI uses its own default/profile. Keep the instruction "
    "actionable, include acceptance checks, and tell the agent not to push, merge, or deploy unless "
    "the user explicitly requested it. Respond with ONLY JSON: "
    '{"summary": str, "agent": "claude-code|codex", "model": str, "effort": "low|medium|high|", '
    '"instruction": str}.'
)

REVIEW_SYSTEM = (
    "You are the PM agent reviewing a coding CLI's returned timeline. Decide whether the original "
    "user task is actually complete. If it is not complete, write the next follow-up instruction to "
    "send to the same agent. Be strict: missing tests, unverified behavior, obvious errors, or partial "
    "implementation means done=false. Respond with ONLY JSON: "
    '{"done": bool, "summary": str, "reason": str, "follow_up": str}.'
)

COMPACT_SYSTEM = (
    "You are the PM agent compacting a Foreman coding session. The raw event log remains the source "
    "of truth; your output is only a derived ContextPack for future LLM calls. Extract facts from "
    "the given timeline only. Do not invent completion. Separate verified facts from agent claims. "
    "Keep source_refs like event:<id> whenever available. Preserve user constraints, decisions, "
    "files, commands, tests, failures, approvals/rejections, open questions, risks, and next steps. "
    "If evidence is omitted because of budget, list it in omitted. Respond with ONLY JSON matching "
    "this shape: "
    '{"version": 1, "session_state": {"goal_quote": str, "summary": str, "status": str, '
    '"current_step": str}, "working_memory": {"verified_facts": [{"text": str, '
    '"source_refs": [str], "status": "verified"}], "claims": [{"text": str, '
    '"source_refs": [str], "status": "claimed"}], "decisions": [], "constraints": [], '
    '"open_questions": [], "risks": [], "next_steps": [], "files": [], "commands": [], '
    '"tests": []}, "retrieved_evidence": [], "dynamic_tail": [], "omitted": []}.'
)


@dataclass
class PMPlan:
    agent: str
    model: str
    effort: str
    instruction: str
    summary: str = ""


@dataclass
class PMReview:
    done: bool
    summary: str = ""
    reason: str = ""
    follow_up: str = ""


def _as_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def _extract_json_object(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if "```" in text:
            text = text[: text.rfind("```")]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (TypeError, ValueError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (TypeError, ValueError):
            return None
    return None


def parse_plan(
    raw: str,
    *,
    enabled_agents: list[str],
    fallback_agent: str,
    fallback_model: str,
    fallback_effort: str,
    fallback_instruction: str,
) -> PMPlan:
    obj = _extract_json_object(raw) or {}
    allowed = [a for a in enabled_agents if a in VALID_AGENTS] or [fallback_agent]
    agent = _as_str(obj.get("agent"))
    if agent not in allowed:
        agent = fallback_agent if fallback_agent in allowed else allowed[0]
    effort = _as_str(obj.get("effort")).lower()
    if effort not in VALID_EFFORTS:
        effort = fallback_effort if fallback_effort in VALID_EFFORTS else ""
    instruction = _as_str(obj.get("instruction")) or fallback_instruction
    return PMPlan(
        agent=agent,
        model=_as_str(obj.get("model")) or fallback_model,
        effort=effort,
        instruction=instruction,
        summary=_as_str(obj.get("summary")),
    )


def parse_review(raw: str) -> PMReview:
    obj = _extract_json_object(raw)
    if obj is None:
        return PMReview(done=False, summary="PM review was not valid JSON")
    return PMReview(
        done=bool(obj.get("done", False)),
        summary=_as_str(obj.get("summary")),
        reason=_as_str(obj.get("reason")),
        follow_up=_as_str(obj.get("follow_up")),
    )


def build_plan_prompt(
    goal: str,
    *,
    workspace: str,
    available_agents: list[dict[str, str]],
    requested_agent: str,
    pm_model: str,
    requested_effort: str,
    context: str = "",
) -> str:
    parts = [
        f"# User task\n{goal}",
        f"# Workspace\n{workspace}",
    ]
    if context:
        parts.append(f"# Existing session context\n{context}")
    parts.extend(
        [
            "# Enabled agents\n" + json.dumps(available_agents, ensure_ascii=False),
            "# Dispatch context\n"
            + json.dumps(
                {
                    "user_requested_agent": requested_agent,
                    "pm_model": pm_model,
                    "requested_effort": requested_effort,
                    "model_rule": (
                        "pm_model is for the PM brain only; do not pass it to the coding CLI"
                    ),
                },
                ensure_ascii=False,
            ),
        ]
    )
    return "\n\n".join(parts)


def events_to_text(rows: list[Any], *, max_chars: int = MAX_EVENT_CHARS) -> str:
    parts: list[str] = []
    for row in rows[-120:]:
        try:
            payload = json.loads(getattr(row, "payload_json", "") or "{}")
        except (TypeError, ValueError):
            payload = {}
        summary = _payload_summary(payload)
        if not summary:
            continue
        event_id = _as_str(getattr(row, "id", ""))
        event_ref = f"event:{event_id}" if event_id else "event:unknown"
        parts.append(
            f"[{event_ref} ts={getattr(row, 'ts', '')}] "
            f"{getattr(row, 'source', '')}/{getattr(row, 'type', '')}: {summary}"
        )
    text = "\n".join(parts)
    if len(text) > max_chars:
        return "...[timeline truncated]...\n" + text[-max_chars:]
    return text


def _payload_summary(payload: object) -> str:
    if not isinstance(payload, dict):
        return _as_str(payload)
    for key in ("text", "result", "summary", "msg", "error"):
        value = _as_str(payload.get(key))
        if value:
            return value
    message = payload.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts = [_as_str(block.get("text")) for block in content if isinstance(block, dict)]
            joined = "\n".join(t for t in texts if t)
            if joined:
                return joined
    return json.dumps(payload, ensure_ascii=False)[:2000]


def build_review_prompt(
    goal: str,
    plan: PMPlan,
    timeline: str,
    *,
    run_count: int,
    max_runs: int,
    context: str = "",
) -> str:
    parts = [
        f"# Original user task\n{goal}",
    ]
    if context:
        parts.append(f"# Existing session context\n{context}")
    parts.extend(
        [
            "# PM plan\n"
            + json.dumps(
                {
                    "summary": plan.summary,
                    "agent": plan.agent,
                    "model": plan.model,
                    "effort": plan.effort,
                    "instruction": plan.instruction,
                },
                ensure_ascii=False,
            ),
            f"# Review budget\nThis is completed agent run {run_count} of maximum {max_runs}.",
            f"# Captured timeline\n{timeline or '(no agent output captured)'}",
        ]
    )
    return "\n\n".join(parts)


def build_compact_prompt(goal: str, timeline: str, *, existing_context: str = "") -> str:
    parts = [f"# Session goal\n{goal}"]
    if existing_context:
        parts.append(f"# Prior compacted context\n{existing_context}")
    parts.append(f"# Timeline to compact\n{timeline}")
    return "\n\n".join(parts)


class PMAgent:
    def __init__(self, llm: LLMClient, *, language: str = "zh", max_runs: int = 3) -> None:
        self.llm = llm
        self.language = language
        self.max_runs = max(1, int(max_runs))

    async def plan(
        self,
        goal: str,
        *,
        workspace: str,
        available_agents: list[dict[str, str]],
        requested_agent: str,
        pm_model: str,
        requested_effort: str,
        fallback_instruction: str,
        context: str = "",
    ) -> PMPlan:
        system = PLAN_SYSTEM + "\n" + language_directive(self.language)
        prompt = build_plan_prompt(
            goal,
            workspace=workspace,
            available_agents=available_agents,
            requested_agent=requested_agent,
            pm_model=pm_model,
            requested_effort=requested_effort,
            context=context,
        )
        raw = await self.llm.complete(
            [Message("system", system), Message("user", prompt)], json_mode=True, model=pm_model
        )
        enabled = [_as_str(a.get("name")) for a in available_agents]
        return parse_plan(
            raw,
            enabled_agents=enabled,
            fallback_agent=requested_agent or (enabled[0] if enabled else "claude-code"),
            fallback_model="",
            fallback_effort=requested_effort,
            fallback_instruction=fallback_instruction,
        )

    async def review(
        self,
        goal: str,
        plan: PMPlan,
        timeline: str,
        *,
        run_count: int,
        context: str = "",
        pm_model: str = "",
    ) -> PMReview:
        system = REVIEW_SYSTEM + "\n" + language_directive(self.language)
        prompt = build_review_prompt(
            goal, plan, timeline, run_count=run_count, max_runs=self.max_runs, context=context
        )
        raw = await self.llm.complete(
            [Message("system", system), Message("user", prompt)], json_mode=True, model=pm_model
        )
        return parse_review(raw)

    async def compact(
        self, goal: str, timeline: str, *, existing_context: str = "", pm_model: str = ""
    ) -> str:
        system = COMPACT_SYSTEM + "\n" + language_directive(self.language)
        prompt = build_compact_prompt(goal, timeline, existing_context=existing_context)
        raw = await self.llm.complete(
            [Message("system", system), Message("user", prompt)], json_mode=True, model=pm_model
        )
        pack = parse_context_pack(
            raw, goal=goal, timeline=timeline, existing_context=existing_context
        )
        return context_pack_to_text(pack, max_chars=MAX_COMPACT_CHARS)


__all__ = [
    "PMAgent",
    "PMPlan",
    "PMReview",
    "parse_plan",
    "parse_review",
    "events_to_text",
    "build_plan_prompt",
    "build_review_prompt",
    "build_compact_prompt",
]
