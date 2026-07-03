"""PM tool-calling loop."""

from __future__ import annotations

import inspect
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from foreman.shared.jsonscan import first_json_object
from foreman.shared.llm import LLMClient, Message
from foreman.shared.llm.trace import trace_context
from foreman.shared.config import PM_TOOLS_DEFAULT_ROUNDS, clamp_pm_tool_rounds
from foreman.client.core.pm_contract import PlanContract

from .models import EXTERNAL_WEB, ToolCall, ToolResult
from .runtime import PMToolRuntime

ToolEventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]
StreamSink = Callable[[dict[str, Any]], Awaitable[None] | None]

# A1 terminal tool (design §5). The PM gathers evidence with the runtime tools on `auto` rounds,
# then calls `submit_plan` exactly once to emit the launch plan. Forcing `tool_choice` to it on the
# submit round makes plan repetition impossible at the protocol level — the turn ends on one
# complete tool_use block, not on parsing free text that a stalled model may repeat (#39).
SUBMIT_PLAN_TOOL = "submit_plan"
_SUBMIT_PLAN_CHOICE = {"type": "function", "name": SUBMIT_PLAN_TOOL}


def submit_plan_tool_spec(
    enabled_agents: list[str] | None = None,
    *,
    max_plan_items: int = PM_TOOLS_DEFAULT_ROUNDS,
) -> dict[str, Any]:
    """Schema for the terminal ``submit_plan`` tool; ``agent`` is constrained to the enabled set.

    Fields mirror ``PMPlan`` so ``validate_final_plan`` (and downstream ``parse_plan``) need no
    change: the only difference is the plan dict now arrives as validated tool arguments instead of
    a regex-sliced text blob. ``maxItems``/``maxLength`` are the structural bounds that replace the
    old token ceiling.
    """
    return PlanContract(
        enabled_agents=enabled_agents,
        max_plan_items=max_plan_items,
    ).tool_spec()


def _submit_plan_args(calls: list[ToolCall]) -> dict[str, Any] | None:
    """Return the arguments of the first ``submit_plan`` tool call (the plan), else None."""
    for call in calls:
        if call.name == SUBMIT_PLAN_TOOL and isinstance(call.arguments, dict):
            return call.arguments
    return None


@dataclass
class ToolLoopOutcome:
    final_plan: dict[str, Any]
    rounds: list[dict[str, Any]] = field(default_factory=list)
    incomplete: bool = False


class PMToolLoop:
    def __init__(
        self,
        llm: LLMClient,
        runtime: PMToolRuntime,
        *,
        max_rounds: int = 6,
        on_tool_event: ToolEventSink | None = None,
        on_stream: StreamSink | None = None,
    ) -> None:
        self.llm = llm
        self.runtime = runtime
        self.max_rounds = clamp_pm_tool_rounds(max_rounds)
        self.on_tool_event = on_tool_event
        self.on_stream = on_stream

    async def run(
        self,
        messages: list[Message],
        *,
        model: str = "",
        fallback_plan: dict[str, Any],
        enabled_agents: list[str],
    ) -> ToolLoopOutcome:
        taint: list[str] = []
        transcript = list(messages)
        rounds: list[dict[str, Any]] = []
        search_needs_verification = False
        for round_no in range(1, self.max_rounds + 1):
            # Evidence rounds let the model pick any tool (auto); the final round forces the terminal
            # submit_plan so a repeated/stalled stream still ends with a real plan instead of silently
            # degrading to the conservative fallback (A1, T1.4 — root fix for #39).
            force_submit = round_no >= self.max_rounds
            # Relabel the trace phase per round (keeps the outer plan session/task — §8C.3).
            with trace_context(phase=f"tool-round-{round_no}"):
                response = await self._complete(
                    transcript,
                    model=model,
                    enabled_agents=enabled_agents,
                    tool_choice=_SUBMIT_PLAN_CHOICE if force_submit else "auto",
                )
            calls = response["tool_calls"]
            raw = response["text"]
            native = bool(response.get("native"))
            obj = _extract_json_object(raw)
            if not calls:
                calls = _calls_from_json(obj)
            # Terminal plan: a native submit_plan tool call — args ARE the plan, no regex (A1).
            # The legacy final_plan JSON in text terminates ONLY on a non-native transport (one with
            # no native tool calls). On the production ws/A1 path the plan must arrive as a
            # submit_plan call, never as repeatable free text, so #39's repetition can't sneak back
            # in through the text terminator (design §0.5-1; §11.1-B). Both shapes validate through
            # the same schema, so PMPlan semantics are unchanged.
            plan_args = _submit_plan_args(calls)
            if (
                plan_args is None
                and not native
                and obj
                and str(obj.get("type") or "").strip() == "final_plan"
            ):
                plan_args = obj
            if plan_args is not None:
                try:
                    plan = validate_final_plan(
                        plan_args,
                        enabled_agents=enabled_agents,
                        fallback_plan=fallback_plan,
                        max_plan_items=self.max_rounds,
                    )
                except ValueError as exc:
                    await self._emit(
                        "pm_validation_error",
                        {
                            "error": str(exc),
                            "round": round_no,
                            "arguments": PlanContract.redact_arguments(plan_args),
                        },
                    )
                    transcript.append(
                        Message(
                            "user",
                            f"Plan validator rejected the response: {exc}. "
                            "Call submit_plan with corrected arguments or call tools for evidence.",
                        )
                    )
                    rounds.append({"round": round_no, "error": str(exc)})
                    continue
                if search_needs_verification:
                    reason = "web_search_leads_unverified"
                    transcript.append(
                        Message(
                            "user",
                            "Plan validator rejected the response: "
                            f"{reason}. web_search returns leads only; call fetch_url on a "
                            "source or use local evidence before submit_plan.",
                        )
                    )
                    rounds.append({"round": round_no, "error": reason})
                    continue
                return ToolLoopOutcome(plan, rounds=rounds)
            # Otherwise run the requested evidence tools (submit_plan, if any, was terminal above).
            evidence_calls = [call for call in calls if call.name != SUBMIT_PLAN_TOOL]
            if not evidence_calls:
                transcript.append(
                    Message(
                        "user",
                        "Protocol error: call submit_plan to finish or request evidence tools. "
                        "Do not invent tool results.",
                    )
                )
                rounds.append({"round": round_no, "error": "no_tool_calls_or_final_plan"})
                continue
            results: list[ToolResult] = []
            for idx, call in enumerate(evidence_calls, start=1):
                if not call.id:
                    call.id = f"call-{round_no}-{idx}"
                await self._emit("tool_pre", _call_payload(call, taint))
                result = await self.runtime.call(
                    call,
                    context_taint=taint,
                    event_sink=self._emit,
                )
                if EXTERNAL_WEB in result.taint and EXTERNAL_WEB not in taint:
                    taint.append(EXTERNAL_WEB)
                if result.name == "web_search" and result.ok:
                    search_needs_verification = True
                elif search_needs_verification and _verifies_search_leads(result):
                    search_needs_verification = False
                await self._emit("tool_post", _result_payload(result))
                results.append(result)
            rounds.append(
                {
                    "round": round_no,
                    "tool_calls": [_call_payload(call, taint) for call in evidence_calls],
                    "tool_results": [result.to_dict() for result in results],
                }
            )
            transcript.append(
                Message(
                    "assistant",
                    json.dumps(
                        {
                            "type": "tool_calls",
                            "tool_calls": [_transcript_call(call) for call in evidence_calls],
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            transcript.append(
                Message(
                    "user",
                    "# Runtime-generated tool_results\n"
                    + json.dumps([result.to_dict() for result in results], ensure_ascii=False)
                    + "\nCall submit_plan when enough evidence exists. "
                    + "Never fabricate tool results. If a result has invalid_args or "
                    + "missing_or_unknown_ref, correct the next tool call arguments. "
                    + "For browser_click/browser_type, use a ref or exact name from the latest "
                    + "browser_snapshot elements.",
                )
            )
        plan = dict(fallback_plan)
        plan["summary"] = (
            "PM tool loop reached max rounds before a final plan; dispatching a conservative "
            "fallback instruction."
        )
        plan["tool_loop_incomplete"] = True
        return ToolLoopOutcome(plan, rounds=rounds, incomplete=True)

    async def _complete(
        self,
        messages: list[Message],
        *,
        model: str,
        enabled_agents: list[str],
        tool_choice: object = "auto",
    ) -> dict[str, Any]:
        if hasattr(self.llm, "tool_complete"):
            tools = [spec.to_native() for spec in self.runtime.specs()]
            tools.append(submit_plan_tool_spec(enabled_agents, max_plan_items=self.max_rounds))
            kwargs: dict[str, Any] = {"tools": tools, "model": model, "json_mode": True}
            if _accepts_keyword(self.llm.tool_complete, "tool_choice"):
                kwargs["tool_choice"] = tool_choice
            if self.on_stream is not None and _accepts_keyword(self.llm.tool_complete, "on_stream"):
                kwargs["on_stream"] = self.on_stream
            native = await self.llm.tool_complete(messages, **kwargs)
            return {
                "text": native.text,
                "tool_calls": [
                    ToolCall(id=call.id, name=call.name, arguments=call.arguments)
                    for call in native.tool_calls
                ],
                "native": True,
            }
        if self.on_stream is not None and _accepts_keyword(self.llm.complete, "on_stream"):
            text = await self.llm.complete(
                messages, json_mode=True, model=model, on_stream=self.on_stream
            )
        else:
            text = await self.llm.complete(messages, json_mode=True, model=model)
        return {"text": text, "tool_calls": [], "native": False}

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.on_tool_event is None:
            return
        res = self.on_tool_event(event_type, payload)
        if inspect.isawaitable(res):
            await res


def build_tool_prompt_context(runtime: PMToolRuntime) -> str:
    return json.dumps(
        {
            "tool_schema": runtime.tool_schema(),
            "runtime_context": runtime.runtime_context(),
            "policy_context": runtime.policy_context(),
            "protocol": {
                "tool_call": {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "call_id",
                            "name": "read_file",
                            "arguments": {
                                "path": "README.md",
                                "public_note": "Reading README.md for the user-visible activity log",
                            },
                        }
                    ],
                },
                "final_plan": {
                    "type": "final_plan",
                    "summary": "evidence-backed summary",
                    "agent": "<enabled-agent-name>",
                    "model": "",
                    "effort": "high",
                    "workspace": "",
                    "instruction": "agent instruction",
                    "kind": "agent_task",
                    "reply": "",
                    "todo": ["inspect", "verify"],
                    "deliberation": ["short visible note"],
                    "ready": True,
                },
                "rule": (
                    "Only runtime-generated tool_results are evidence. To finish, call the "
                    "submit_plan tool with the plan fields (preferred); the final_plan JSON above "
                    "is a legacy fallback for transports without native tool calls. "
                    "Tool arguments may include public_note or purpose for the visible activity log; "
                    "omit it if you do not have a concise user-facing sentence."
                ),
            },
        },
        ensure_ascii=False,
    )


def _accepts_keyword(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    if name in sig.parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def validate_final_plan(
    obj: dict[str, Any],
    *,
    enabled_agents: list[str],
    fallback_plan: dict[str, Any],
    max_plan_items: int = PM_TOOLS_DEFAULT_ROUNDS,
) -> dict[str, Any]:
    return PlanContract(
        enabled_agents=enabled_agents,
        fallback_agent=str(fallback_plan.get("agent") or ""),
        max_plan_items=max_plan_items,
    ).validate(obj)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """First balanced JSON object in an LLM reply (fences / prose / repeats)."""
    return first_json_object(raw)


def _calls_from_json(obj: dict[str, Any] | None) -> list[ToolCall]:
    if not isinstance(obj, dict):
        return []
    if str(obj.get("type") or "") == "tool_call":
        raw_calls: list[object] = [obj]
    else:
        value = obj.get("tool_calls") or obj.get("tools")
        raw_calls = value if isinstance(value, list) else []
    out: list[ToolCall] = []
    for idx, item in enumerate(raw_calls, start=1):
        call = ToolCall.from_obj(item, fallback_id=f"call-{uuid.uuid4().hex[:8]}-{idx}")
        if call is not None:
            out.append(call)
        elif isinstance(item, dict) and str(item.get("name") or item.get("tool") or "").strip():
            out.append(
                ToolCall(
                    id=str(item.get("id") or f"call-{uuid.uuid4().hex[:8]}-{idx}"),
                    name=str(item.get("name") or item.get("tool") or "").strip(),
                    arguments={"__invalid_args__": True},
                )
            )
    return out


def _call_payload(call: ToolCall, context_taint: list[str]) -> dict[str, Any]:
    return {
        "tool": call.name,
        "call_id": call.id,
        "input": call.arguments,
        "context_taint": list(context_taint),
        "source": "pm-agent",
    }


def _transcript_call(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "name": call.name,
        "arguments": call.arguments,
    }


def _result_payload(result: ToolResult) -> dict[str, Any]:
    return {
        "tool": result.name,
        "call_id": result.id,
        "ok": result.ok,
        "output": json.dumps(result.to_dict(), ensure_ascii=False),
        "result": result.to_dict(),
        "source": "pm-agent",
    }


def _verifies_search_leads(result: ToolResult) -> bool:
    return result.ok and result.name in {
        "fetch_url",
        "list_files",
        "read_file",
        "search_repo",
        "run_command",
    }
