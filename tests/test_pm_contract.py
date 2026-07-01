from __future__ import annotations

import pytest

from foreman.client.core.pm_contract import PlanContract
from foreman.client.tools.loop import submit_plan_tool_spec, validate_final_plan


def _plan(**overrides):
    data = {
        "summary": "summary",
        "agent": "codex",
        "model": "",
        "effort": "high",
        "workspace": "",
        "instruction": "do the work",
        "kind": "agent_task",
        "reply": "",
        "todo": ["inspect"],
        "deliberation": ["enough evidence"],
        "ready": True,
    }
    data.update(overrides)
    return data


def _validate(data):
    return validate_final_plan(data, enabled_agents=["codex"], fallback_plan={"agent": "codex"})


def _raises(data, code: str) -> None:
    with pytest.raises(ValueError, match=code):
        _validate(data)


def test_submit_plan_schema_comes_from_plan_contract():
    contract = PlanContract(enabled_agents=["codex"], max_plan_items=17)
    spec = submit_plan_tool_spec(["codex"], max_plan_items=17)

    assert spec == contract.tool_spec()
    schema = spec["input_schema"]
    assert schema["required"] == list(PlanContract.COMMON_REQUIRED)
    assert schema["properties"]["kind"]["enum"] == list(PlanContract.ALLOWED_KINDS)
    assert schema["properties"]["agent"]["enum"] == ["codex"]
    assert schema["properties"]["todo"]["maxItems"] == 17
    assert "instruction is still required" in spec["description"]
    assert "user-visible answer" in schema["properties"]["reply"]["description"]


def test_plan_contract_validator_error_codes_are_stable():
    _raises(_plan(kind="direct_reply", reply="", instruction="direct reply only"), "final_plan_missing_reply")
    _raises(_plan(kind="direct_reply", reply="hello", instruction=""), "final_plan_missing_instruction")
    _raises(_plan(kind="agent_task", instruction=""), "final_plan_missing_instruction")
    _raises(_plan(agent="bad"), "final_plan_bad_agent")
    _raises(_plan(effort="turbo"), "final_plan_bad_effort")
    _raises(_plan(kind="surprise"), "final_plan_bad_kind")


def test_direct_reply_with_instruction_and_reply_is_valid():
    plan = _validate(
        _plan(
            kind="direct_reply",
            instruction="direct reply only",
            reply="The answer is 42.",
            effort="low",
        )
    )

    assert plan["kind"] == "direct_reply"
    assert plan["instruction"] == "direct reply only"
    assert plan["reply"] == "The answer is 42."


def test_plan_contract_clamps_output_bounds():
    plan = _validate(
        _plan(
            summary="s" * 800,
            model="m" * 120,
            workspace="w" * 700,
            instruction="i" * 7000,
            reply="r" * 3000,
            todo=["t" * 400 for _ in range(20)],
            deliberation=["d" * 400 for _ in range(20)],
        )
    )

    assert len(plan["summary"]) == 600
    assert len(plan["model"]) == 80
    assert len(plan["workspace"]) == 500
    assert len(plan["instruction"]) == 6000
    assert len(plan["reply"]) == 2000
    assert len(plan["todo"]) == 6 and all(len(item) <= 200 for item in plan["todo"])
    assert len(plan["deliberation"]) == 6 and all(len(item) <= 300 for item in plan["deliberation"])
