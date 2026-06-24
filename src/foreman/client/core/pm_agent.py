"""PM agent orchestration for user-dispatched tasks.

This is intentionally small: the PM LLM turns the user's goal into the first agent prompt, then
reviews the captured agent timeline after each run and decides whether to stop or continue with a
follow-up. It does not execute tools directly; it only chooses how to steer the existing CLI agent.
"""

from __future__ import annotations

import json
import inspect
from dataclasses import dataclass, field
from typing import Any

from foreman.shared.i18n import language_directive, normalize as normalize_lang
from foreman.shared.llm import LLMClient, Message

from .context_compression import context_pack_to_text, parse_context_pack
from ..tools.loop import PMToolLoop, build_tool_prompt_context

VALID_AGENTS = {"claude-code", "codex"}
VALID_EFFORTS = {"low", "medium", "high"}
MAX_EVENT_CHARS = 20000
MAX_COMPACT_CHARS = 12000

PLAN_SYSTEM = (
    "You are the PM agent for Foreman. Analyze the user's task before any coding CLI is launched. "
    "Choose which enabled coding agent should run, which coding-agent model/effort to use, and "
    "write the exact instruction for that agent. Deliberate before dispatch: produce concise "
    "visible decision notes and an atomic todo list, then mark ready only when no more planning would "
    "change the launch decision. Do not reveal hidden chain-of-thought; decision notes must be "
    "short, evidence-oriented summaries. The dispatch model is already being used for you, "
    "the PM brain; never copy it into the coding-agent model field. Choose the coding agent "
    "yourself. Prefer the highest-capability enabled agent/model/effort over saving tokens; use "
    "high effort by default unless the task is clearly trivial. If no coding-agent model is "
    "configured, leave model empty so the CLI uses its own default/profile. Keep the instruction "
    "actionable, include acceptance checks, and tell the agent not to push, merge, or deploy unless "
    "the user explicitly requested it. Never tell one coding agent to launch or shell out to another "
    "coding agent; Foreman owns all Claude Code and Codex process launches. Assume the selected "
    "coding agent may use its available file read/write/edit, shell command, and web/search tools "
    "when its full_access setting is true. Human-facing JSON string "
    "values must follow the selected output language; keep only identifiers, paths, commands, code, "
    "and quoted user text as-is. Respond with ONLY JSON: "
    '{"summary": str, "agent": "claude-code|codex", "model": str, "effort": "low|medium|high|", '
    '"instruction": str, "todo": [str], "deliberation": [str], "ready": bool}.'
)

REVIEW_SYSTEM = (
    "You are the PM agent reviewing a coding CLI's returned timeline. Decide whether the original "
    "user task is actually complete. If it is not complete, write the next follow-up instruction to "
    "send to the same agent. Be strict: missing tests, unverified behavior, obvious errors, or partial "
    "implementation means done=false. Update the todo list every review: mark completed items as "
    "done immediately, mark the next active item in_progress, and mark blocked items blocked. "
    "Human-facing JSON string values must follow the selected output "
    "language; keep only identifiers, paths, commands, code, and quoted user text as-is. Respond with "
    "ONLY JSON: "
    '{"done": bool, "summary": str, "reason": str, "follow_up": str, '
    '"todo_status": [{"title": str, "status": "pending|in_progress|done|blocked"}]}.'
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
    todo: list[str] = field(default_factory=list)
    deliberation: list[str] = field(default_factory=list)
    ready: bool = True
    planning_rounds: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PMReview:
    done: bool
    summary: str = ""
    reason: str = ""
    follow_up: str = ""
    todo_status: list[dict[str, str]] = field(default_factory=list)


def _as_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def _as_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _as_str(value).lower()
    if text in {"true", "yes", "1"}:
        return True
    if text in {"false", "no", "0"}:
        return False
    return default


def _as_str_list(value: object, *, max_items: int = 12) -> list[str]:
    if isinstance(value, str):
        items = [line.strip(" -\t") for line in value.splitlines()]
    elif isinstance(value, list):
        items = [_as_str(item) for item in value]
    else:
        items = []
    return [item for item in items if item][:max_items]


def _as_todo_status(value: object, *, max_items: int = 12) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value[:max_items]:
        if isinstance(item, str):
            title, status = item, "pending"
        elif isinstance(item, dict):
            title = _as_str(item.get("title") or item.get("content") or item.get("task"))
            status = _as_str(item.get("status")).lower()
        else:
            continue
        if not title:
            continue
        if status == "completed":
            status = "done"
        elif status in {"active", "running"}:
            status = "in_progress"
        if status not in {"pending", "in_progress", "done", "blocked"}:
            status = "pending"
        out.append({"title": title, "status": status})
    return out


def _accepts_keyword(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    if name in sig.parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


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
        todo=_as_str_list(obj.get("todo")),
        deliberation=_as_str_list(obj.get("deliberation")),
        ready=_as_bool(obj.get("ready"), default=True),
    )


def parse_review(raw: str, *, language: str = "") -> PMReview:
    obj = _extract_json_object(raw)
    if obj is None:
        summary = (
            "PM review was not valid JSON"
            if normalize_lang(language) == "en"
            else "PM 复查返回的内容不是有效 JSON"
        )
        return PMReview(done=False, summary=summary)
    return PMReview(
        done=bool(obj.get("done", False)),
        summary=_as_str(obj.get("summary")),
        reason=_as_str(obj.get("reason")),
        follow_up=_as_str(obj.get("follow_up")),
        todo_status=_as_todo_status(obj.get("todo_status") or obj.get("todo")),
    )


def build_plan_prompt(
    goal: str,
    *,
    workspace: str,
    available_agents: list[dict[str, Any]],
    requested_agent: str,
    pm_model: str,
    requested_effort: str,
    context: str = "",
    planning_rounds: list[dict[str, Any]] | None = None,
    round_no: int = 1,
    min_rounds: int = 1,
    max_rounds: int = 1,
) -> str:
    parts = [
        f"# User task\n{goal}",
        f"# Workspace\n{workspace}",
        "# PM planning round\n"
        + json.dumps(
            {
                "round": round_no,
                "min_rounds_before_dispatch": min_rounds,
                "max_rounds": max_rounds,
                "ready_rule": (
                    "Set ready=false if another planning round is likely to change agent choice, "
                    "todo, acceptance checks, or launch instruction."
                ),
            },
            ensure_ascii=False,
        ),
    ]
    if context:
        parts.append(f"# Existing session context\n{context}")
    if planning_rounds:
        parts.append(
            "# Prior PM planning rounds\n"
            + json.dumps(planning_rounds[-4:], ensure_ascii=False)
        )
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
                    "capability_rule": (
                        "When an enabled agent has full_access=true, Foreman launches that CLI "
                        "with permission flags intended to allow file read/write/edit, shell, "
                        "and web/search use where the CLI supports those tools."
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
        if getattr(row, "type", "") in {"pm_output", "pm_reasoning"}:
            continue
        try:
            raw_payload = getattr(row, "payload", None)
            payload = (
                raw_payload
                if isinstance(raw_payload, dict)
                else json.loads(getattr(row, "payload_json", "") or "{}")
            )
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
    for key in ("text", "delta", "thinking", "reasoning", "result", "summary", "msg", "error"):
        value = _as_str(payload.get(key))
        if value:
            return value
    for key in ("message", "item"):
        nested = _content_summary(payload.get(key))
        if nested:
            return nested
    return json.dumps(payload, ensure_ascii=False)[:2000]


def _content_summary(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("text", "delta", "thinking", "reasoning", "summary"):
        direct = _as_str(value.get(key))
        if direct:
            return direct
    content = value.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        for key in ("text", "delta", "thinking", "reasoning", "summary"):
            text = _as_str(block.get(key))
            if text:
                parts.append(text)
                break
        else:
            nested = _content_summary(block)
            if nested:
                parts.append(nested)
    return "\n".join(parts).strip()


def build_review_prompt(
    goal: str,
    plan: PMPlan,
    timeline: str,
    *,
    run_count: int,
    max_runs: int,
    context: str = "",
    review_state: str = "",
    todo_status: list[dict[str, str]] | None = None,
) -> str:
    parts = [
        f"# Original user task\n{goal}",
    ]
    if context:
        parts.append(f"# Existing session context\n{context}")
    if review_state:
        parts.append(f"# Prior PM review state\n{review_state}")
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
                    "todo": plan.todo,
                    "todo_status": todo_status or [],
                    "deliberation": plan.deliberation,
                },
                ensure_ascii=False,
            ),
            f"# Review budget\nThis is completed agent run {run_count} of maximum {max_runs}.",
            f"# Captured timeline since last PM review\n{timeline or '(no new agent output captured)'}",
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
    def __init__(
        self,
        llm: LLMClient,
        *,
        language: str = "zh",
        max_runs: int = 3,
        min_plan_rounds: int = 2,
        max_plan_rounds: int = 3,
        tool_runtime_factory=None,
    ) -> None:
        self.llm = llm
        self.language = language
        self.max_runs = max(1, int(max_runs))
        self.min_plan_rounds = max(1, int(min_plan_rounds))
        self.max_plan_rounds = max(self.min_plan_rounds, int(max_plan_rounds))
        self.tool_runtime_factory = tool_runtime_factory

    async def plan(
        self,
        goal: str,
        *,
        workspace: str,
        available_agents: list[dict[str, Any]],
        requested_agent: str,
        pm_model: str,
        requested_effort: str,
        fallback_instruction: str,
        context: str = "",
        on_stream=None,
        on_tool_event=None,
    ) -> PMPlan:
        system = PLAN_SYSTEM + "\n" + language_directive(self.language)
        enabled = [_as_str(a.get("name")) for a in available_agents]
        if self.tool_runtime_factory is not None:
            runtime = self.tool_runtime_factory(workspace)
            try:
                fallback_plan = {
                    "agent": requested_agent or (enabled[0] if enabled else "claude-code"),
                    "model": "",
                    "effort": requested_effort,
                    "instruction": fallback_instruction,
                }
                prompt = build_plan_prompt(
                    goal,
                    workspace=workspace,
                    available_agents=available_agents,
                    requested_agent=requested_agent,
                    pm_model=pm_model,
                    requested_effort=requested_effort,
                    context=context,
                    round_no=1,
                    min_rounds=1,
                    max_rounds=max(1, int(getattr(runtime.cfg, "max_rounds", 6))),
                )
                prompt = (
                    prompt
                    + "\n\n# PM tool runtime\n"
                    + build_tool_prompt_context(runtime)
                    + "\n\nUse tools for repository evidence before final_plan when useful. "
                    + "Tool results are only valid when supplied by the runtime."
                )
                loop = PMToolLoop(
                    self.llm,
                    runtime,
                    max_rounds=int(getattr(runtime.cfg, "max_rounds", 6)),
                    on_tool_event=on_tool_event,
                )
                outcome = await loop.run(
                    [Message("system", system), Message("user", prompt)],
                    model=pm_model,
                    fallback_plan=fallback_plan,
                    enabled_agents=enabled,
                )
                tool_plan = PMPlan(
                    agent=_as_str(outcome.final_plan.get("agent")) or fallback_plan["agent"],
                    model=_as_str(outcome.final_plan.get("model")),
                    effort=_as_str(outcome.final_plan.get("effort")),
                    instruction=_as_str(outcome.final_plan.get("instruction"))
                    or fallback_instruction,
                    summary=_as_str(outcome.final_plan.get("summary")),
                    todo=_as_str_list(outcome.final_plan.get("todo")),
                    deliberation=_as_str_list(outcome.final_plan.get("deliberation")),
                    ready=_as_bool(outcome.final_plan.get("ready"), default=True),
                    planning_rounds=outcome.rounds,
                )
                if outcome.incomplete:
                    tool_plan.deliberation.append(
                        "PM tool loop hit max rounds; using fallback plan."
                        if normalize_lang(self.language) == "en"
                        else "PM 工具循环已达到轮次上限；将使用降级计划。"
                    )
                return tool_plan
            finally:
                if hasattr(runtime, "aclose"):
                    await runtime.aclose()
        rounds: list[dict[str, Any]] = []
        plan: PMPlan | None = None
        for round_no in range(1, self.max_plan_rounds + 1):
            prompt = build_plan_prompt(
                goal,
                workspace=workspace,
                available_agents=available_agents,
                requested_agent=requested_agent,
                pm_model=pm_model,
                requested_effort=requested_effort,
                context=context,
                planning_rounds=rounds,
                round_no=round_no,
                min_rounds=self.min_plan_rounds,
                max_rounds=self.max_plan_rounds,
            )
            raw = await self.llm.complete(
                [Message("system", system), Message("user", prompt)],
                json_mode=True,
                model=pm_model,
                on_stream=on_stream,
            )
            plan = parse_plan(
                raw,
                enabled_agents=enabled,
                fallback_agent=requested_agent or (enabled[0] if enabled else "claude-code"),
                fallback_model="",
                fallback_effort=requested_effort,
                fallback_instruction=fallback_instruction,
            )
            rounds.append(
                {
                    "round": round_no,
                    "ready": plan.ready,
                    "summary": plan.summary,
                    "todo": plan.todo,
                    "deliberation": plan.deliberation,
                    "agent": plan.agent,
                    "effort": plan.effort,
                }
            )
            if round_no >= self.min_plan_rounds and plan.ready:
                break
        if plan is None:
            plan = PMPlan(
                agent=requested_agent or (enabled[0] if enabled else "claude-code"),
                model="",
                effort=requested_effort,
                instruction=fallback_instruction,
            )
        plan.planning_rounds = rounds
        return plan

    async def review(
        self,
        goal: str,
        plan: PMPlan,
        timeline: str,
        *,
        run_count: int,
        context: str = "",
        pm_model: str = "",
        review_state: str = "",
        todo_status: list[dict[str, str]] | None = None,
        on_stream=None,
        state_key: str = "",
    ) -> PMReview:
        system = REVIEW_SYSTEM + "\n" + language_directive(self.language)
        prompt = build_review_prompt(
            goal,
            plan,
            timeline,
            run_count=run_count,
            max_runs=self.max_runs,
            context=context,
            review_state=review_state,
            todo_status=todo_status,
        )
        kwargs = {"json_mode": True, "model": pm_model, "on_stream": on_stream}
        if state_key and _accepts_keyword(self.llm.complete, "state_key"):
            kwargs["state_key"] = state_key
        raw = await self.llm.complete([Message("system", system), Message("user", prompt)], **kwargs)
        return parse_review(raw, language=self.language)

    async def compact(
        self,
        goal: str,
        timeline: str,
        *,
        existing_context: str = "",
        pm_model: str = "",
        on_stream=None,
    ) -> str:
        system = COMPACT_SYSTEM + "\n" + language_directive(self.language)
        prompt = build_compact_prompt(goal, timeline, existing_context=existing_context)
        raw = await self.llm.complete(
            [Message("system", system), Message("user", prompt)],
            json_mode=True,
            model=pm_model,
            on_stream=on_stream,
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
