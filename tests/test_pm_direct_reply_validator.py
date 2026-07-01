from __future__ import annotations

from pathlib import Path

import pytest

from foreman.client.core.gate import Gate
from foreman.client.tools import PMToolLoop, PMToolRuntime
from foreman.client.tools.loop import validate_final_plan
from foreman.client.tools.models import ToolRuntimeConfig
from foreman.shared.config import Config
from foreman.shared.llm import LLMToolCall, LLMToolResponse, Message


def _runtime(tmp_path: Path) -> PMToolRuntime:
    cfg = ToolRuntimeConfig(workspace=tmp_path, allowed_roots=[tmp_path])
    return PMToolRuntime(cfg, gate=Gate(Config().gates))


def _plan(**overrides):
    data = {
        "summary": "direct answer",
        "agent": "codex",
        "model": "",
        "effort": "low",
        "workspace": "",
        "instruction": "direct reply only",
        "kind": "direct_reply",
        "reply": "hello",
        "todo": ["reply"],
        "deliberation": ["no coding needed"],
        "ready": True,
    }
    data.update(overrides)
    return data


def test_direct_reply_validator_requires_reply_and_instruction():
    with pytest.raises(ValueError, match="final_plan_missing_reply"):
        validate_final_plan(
            _plan(reply=""),
            enabled_agents=["codex"],
            fallback_plan={"agent": "codex"},
        )
    with pytest.raises(ValueError, match="final_plan_missing_instruction"):
        validate_final_plan(
            _plan(instruction=""),
            enabled_agents=["codex"],
            fallback_plan={"agent": "codex"},
        )


def test_direct_reply_validator_uses_reply_as_user_visible_answer():
    plan = validate_final_plan(
        _plan(reply="This is the user-visible answer."),
        enabled_agents=["codex"],
        fallback_plan={"agent": "codex"},
    )

    assert plan["kind"] == "direct_reply"
    assert plan["instruction"] == "direct reply only"
    assert plan["reply"] == "This is the user-visible answer."


async def test_pm_tool_loop_emits_pm_validation_error_on_bad_direct_reply(tmp_path):
    events: list[tuple[str, dict]] = []

    class FakeLLM:
        def __init__(self):
            self.round = 0

        async def tool_complete(self, messages, *, tools, model="", json_mode=False, tool_choice="auto"):
            self.round += 1
            args = _plan(reply="") if self.round == 1 else _plan(reply="fixed")
            return LLMToolResponse(
                text="",
                tool_calls=[
                    LLMToolCall(
                        id=f"call-{self.round}",
                        name="submit_plan",
                        arguments=args,
                    )
                ],
            )

    outcome = await PMToolLoop(
        FakeLLM(),
        _runtime(tmp_path),
        max_rounds=2,
        on_tool_event=lambda event_type, payload: events.append((event_type, payload)),
    ).run(
        [Message("user", "answer directly")],
        fallback_plan={"agent": "codex", "model": "", "effort": "low", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.final_plan["reply"] == "fixed"
    assert events[0][0] == "pm_validation_error"
    assert events[0][1]["error"] == "final_plan_missing_reply"
    assert events[0][1]["round"] == 1
    assert events[0][1]["arguments"]["reply"] == "<redacted:0 chars>"
    assert events[0][1]["arguments"]["kind"] == "direct_reply"
