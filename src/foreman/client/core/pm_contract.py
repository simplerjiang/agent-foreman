"""Single source of truth for PM submit_plan arguments."""

from __future__ import annotations

from typing import Any

from foreman.shared.config import PM_TOOLS_DEFAULT_ROUNDS, clamp_pm_tool_rounds


class PlanContract:
    ALLOWED_KINDS = ("agent_task", "direct_reply", "blocked", "error")
    DEFAULT_AGENTS = ("claude-code", "codex", "copilot-cli")
    ALLOWED_EFFORTS = ("low", "medium", "high", "")

    MAX_SUMMARY = 600
    MAX_AGENT_MODEL = 80
    MAX_WORKSPACE = 500
    MAX_INSTRUCTION = 6000
    MAX_REPLY = 2000
    MAX_TODO_ITEM = 200
    MAX_DELIBERATION_ITEM = 300

    COMMON_REQUIRED = (
        "summary",
        "agent",
        "effort",
        "instruction",
        "kind",
        "reply",
        "todo",
        "deliberation",
        "ready",
    )
    NON_EMPTY_BY_KIND = {
        "agent_task": ("agent", "instruction"),
        "direct_reply": ("agent", "instruction", "reply"),
        "blocked": ("agent", "summary"),
        "error": ("agent", "summary"),
    }

    def __init__(
        self,
        *,
        enabled_agents: list[str] | None = None,
        fallback_agent: str = "",
        max_plan_items: int = PM_TOOLS_DEFAULT_ROUNDS,
    ) -> None:
        self.allowed_agents = self._allowed_agents(enabled_agents, fallback_agent)
        self.max_plan_items = clamp_pm_tool_rounds(max_plan_items)

    def tool_spec(self) -> dict[str, Any]:
        return {
            "name": "submit_plan",
            "description": (
                "Emit exactly one PM plan. For direct_reply, instruction is still required and "
                "reply is the user-visible answer. For agent_task, instruction is sent to the "
                "coding CLI."
            ),
            "input_schema": self.schema(),
        }

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(self.COMMON_REQUIRED),
            "properties": {
                "summary": {"type": "string", "maxLength": self.MAX_SUMMARY},
                "agent": {"type": "string", "enum": self.allowed_agents},
                "model": {"type": "string", "maxLength": self.MAX_AGENT_MODEL},
                "effort": {"type": "string", "enum": list(self.ALLOWED_EFFORTS)},
                "instruction": {
                    "type": "string",
                    "maxLength": self.MAX_INSTRUCTION,
                    "description": "For agent_task this is the coding CLI instruction.",
                },
                "workspace": {
                    "type": "string",
                    "maxLength": self.MAX_WORKSPACE,
                    "description": (
                        "Optional existing workspace/worktree path where the coding agent must "
                        "launch. Leave empty unless verified from runtime tool output."
                    ),
                },
                "kind": {"type": "string", "enum": list(self.ALLOWED_KINDS)},
                "reply": {
                    "type": "string",
                    "maxLength": self.MAX_REPLY,
                    "description": "For direct_reply this is the user-visible answer.",
                },
                "todo": {
                    "type": "array",
                    "maxItems": self.max_plan_items,
                    "items": {"type": "string", "maxLength": self.MAX_TODO_ITEM},
                },
                "deliberation": {
                    "type": "array",
                    "maxItems": self.max_plan_items,
                    "items": {"type": "string", "maxLength": self.MAX_DELIBERATION_ITEM},
                },
                "ready": {"type": "boolean"},
            },
        }

    def validate(self, obj: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(obj, dict):
            raise ValueError("final_plan_not_object")
        kind = _text(obj.get("kind") or "agent_task")[:40]
        if kind not in self.ALLOWED_KINDS:
            raise ValueError("final_plan_bad_kind")
        agent = _text(obj.get("agent"))
        if agent not in self.allowed_agents:
            raise ValueError("final_plan_bad_agent")
        effort = _text(obj.get("effort")).lower()
        if effort not in self.ALLOWED_EFFORTS:
            raise ValueError("final_plan_bad_effort")
        instruction = _text(obj.get("instruction"))
        reply = _text(obj.get("reply"))
        summary = _text(obj.get("summary"))
        if "instruction" in self.NON_EMPTY_BY_KIND[kind] and not instruction:
            raise ValueError("final_plan_missing_instruction")
        if "reply" in self.NON_EMPTY_BY_KIND[kind] and not reply:
            raise ValueError("final_plan_missing_reply")
        if "summary" in self.NON_EMPTY_BY_KIND[kind] and not summary:
            raise ValueError("final_plan_missing_summary")
        return {
            "summary": summary[: self.MAX_SUMMARY],
            "agent": agent,
            "model": _text(obj.get("model"))[: self.MAX_AGENT_MODEL],
            "effort": effort,
            "workspace": _text(obj.get("workspace"))[: self.MAX_WORKSPACE],
            "instruction": instruction[: self.MAX_INSTRUCTION],
            "kind": kind,
            "reply": reply[: self.MAX_REPLY],
            "todo": _str_list(
                obj.get("todo"),
                max_items=self.max_plan_items,
                max_len=self.MAX_TODO_ITEM,
            ),
            "deliberation": _str_list(
                obj.get("deliberation"),
                max_items=self.max_plan_items,
                max_len=self.MAX_DELIBERATION_ITEM,
            ),
            "ready": bool(obj.get("ready", True)),
        }

    @classmethod
    def redact_arguments(cls, obj: object) -> dict[str, Any]:
        if not isinstance(obj, dict):
            return {"type": type(obj).__name__}
        out: dict[str, Any] = {}
        for key, value in obj.items():
            name = str(key)
            if name in {"agent", "effort", "kind", "ready"}:
                out[name] = value
            elif isinstance(value, str):
                out[name] = f"<redacted:{len(value)} chars>"
            elif isinstance(value, list):
                out[name] = f"<redacted_list:{len(value)} items>"
            elif isinstance(value, dict):
                out[name] = "<redacted_object>"
            else:
                out[name] = type(value).__name__
        return out

    @classmethod
    def _allowed_agents(cls, enabled_agents: list[str] | None, fallback_agent: str) -> list[str]:
        agents = [agent for agent in (enabled_agents or []) if agent]
        if agents:
            return agents
        if fallback_agent:
            return [fallback_agent]
        return list(cls.DEFAULT_AGENTS)


def _str_list(
    value: object,
    *,
    max_items: int = PM_TOOLS_DEFAULT_ROUNDS,
    max_len: int = 200,
) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = []
    return [str(item).strip()[:max_len] for item in items if str(item or "").strip()][:max_items]


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()
