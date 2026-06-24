"""PM tool-calling loop."""

from __future__ import annotations

import inspect
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from foreman.shared.llm import LLMClient, Message

from .models import EXTERNAL_WEB, ToolCall, ToolResult
from .runtime import PMToolRuntime

ToolEventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]


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
    ) -> None:
        self.llm = llm
        self.runtime = runtime
        self.max_rounds = max(1, max_rounds)
        self.on_tool_event = on_tool_event

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
            response = await self._complete(transcript, model=model)
            calls = response["tool_calls"]
            raw = response["text"]
            obj = _extract_json_object(raw)
            if not calls:
                calls = _calls_from_json(obj)
            if obj and str(obj.get("type") or "").strip() == "final_plan":
                try:
                    plan = validate_final_plan(
                        obj,
                        enabled_agents=enabled_agents,
                        fallback_plan=fallback_plan,
                    )
                except ValueError as exc:
                    transcript.append(
                        Message(
                            "user",
                            f"Final plan validator rejected the response: {exc}. "
                            "Return a corrected final_plan or call tools for more evidence.",
                        )
                    )
                    rounds.append({"round": round_no, "error": str(exc)})
                    continue
                if search_needs_verification:
                    reason = "web_search_leads_unverified"
                    transcript.append(
                        Message(
                            "user",
                            "Final plan validator rejected the response: "
                            f"{reason}. web_search returns leads only; call fetch_url on a "
                            "source or use local evidence before final_plan.",
                        )
                    )
                    rounds.append({"round": round_no, "error": reason})
                    continue
                return ToolLoopOutcome(plan, rounds=rounds)
            if not calls:
                transcript.append(
                    Message(
                        "user",
                        "Protocol error: respond with JSON final_plan or tool_calls. "
                        "Do not invent tool results.",
                    )
                )
                rounds.append({"round": round_no, "error": "no_tool_calls_or_final_plan"})
                continue
            results: list[ToolResult] = []
            for idx, call in enumerate(calls, start=1):
                if not call.id:
                    call.id = f"call-{round_no}-{idx}"
                await self._emit("tool_pre", _call_payload(call, taint))
                result = await self.runtime.call(call, context_taint=taint)
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
                    "tool_calls": [_call_payload(call, taint) for call in calls],
                    "tool_results": [result.to_dict() for result in results],
                }
            )
            transcript.append(
                Message(
                    "assistant",
                    json.dumps(
                        {
                            "type": "tool_calls",
                            "tool_calls": [_transcript_call(call) for call in calls],
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
                    + "\nReturn final_plan when enough evidence exists. "
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

    async def _complete(self, messages: list[Message], *, model: str) -> dict[str, Any]:
        if hasattr(self.llm, "tool_complete"):
            native = await self.llm.tool_complete(
                messages,
                tools=[spec.to_native() for spec in self.runtime.specs()],
                model=model,
                json_mode=True,
            )
            return {
                "text": native.text,
                "tool_calls": [
                    ToolCall(id=call.id, name=call.name, arguments=call.arguments)
                    for call in native.tool_calls
                ],
            }
        text = await self.llm.complete(messages, json_mode=True, model=model)
        return {"text": text, "tool_calls": []}

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
                        {"id": "call_id", "name": "read_file", "arguments": {"path": "README.md"}}
                    ],
                },
                "final_plan": {
                    "type": "final_plan",
                    "summary": "evidence-backed summary",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "agent instruction",
                    "todo": ["inspect", "verify"],
                    "deliberation": ["short visible note"],
                    "ready": True,
                },
                "rule": "Only runtime-generated tool_results are evidence.",
            },
        },
        ensure_ascii=False,
    )


def validate_final_plan(
    obj: dict[str, Any],
    *,
    enabled_agents: list[str],
    fallback_plan: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValueError("final_plan_not_object")
    allowed = [agent for agent in enabled_agents if agent] or [str(fallback_plan.get("agent") or "")]
    agent = str(obj.get("agent") or "").strip()
    if agent not in allowed:
        raise ValueError("final_plan_bad_agent")
    instruction = str(obj.get("instruction") or "").strip()
    if not instruction:
        raise ValueError("final_plan_missing_instruction")
    effort = str(obj.get("effort") or fallback_plan.get("effort") or "").strip().lower()
    if effort not in {"", "low", "medium", "high"}:
        raise ValueError("final_plan_bad_effort")
    return {
        "summary": str(obj.get("summary") or "").strip(),
        "agent": agent,
        "model": str(obj.get("model") or "").strip(),
        "effort": effort,
        "instruction": instruction,
        "todo": _str_list(obj.get("todo")),
        "deliberation": _str_list(obj.get("deliberation")),
        "ready": bool(obj.get("ready", True)),
    }


def _extract_json_object(raw: str) -> dict[str, Any] | None:
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
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (TypeError, ValueError):
            return None
    return None


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


def _str_list(value: object) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = []
    return [str(item).strip() for item in items if str(item or "").strip()][:12]
