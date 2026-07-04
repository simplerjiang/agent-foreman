from __future__ import annotations

import json

from foreman.client.core.context_v2 import ActiveContext
from foreman.client.core.pm_agent import PLAN_SYSTEM, PMAgent, build_plan_prompt


class _LLM:
    async def complete(self, messages, *, json_mode=False, model="", on_stream=None, **kwargs):
        return json.dumps(
            {
                "summary": "planned",
                "agent": "codex",
                "model": "",
                "effort": "high",
                "instruction": "do it",
                "todo": ["verify"],
                "ready": True,
            }
        )


def test_pm_prompt_contains_worktree_tool_rules():
    prompt = build_plan_prompt(
        "edit files",
        workspace="E:/repo-worktree",
        available_agents=[{"name": "codex", "model": "", "effort": ""}],
        requested_agent="codex",
        pm_model="gpt-5",
        requested_effort="high",
        main_workspace="E:/repo",
    )

    assert "prefer a dedicated Foreman worktree" in PLAN_SYSTEM
    assert "run_command" in PLAN_SYSTEM
    assert "`main_workspace`" in PLAN_SYSTEM
    assert "`workspace`" in PLAN_SYSTEM
    assert "E:/repo-worktree" in prompt
    assert "E:/repo" in prompt
    assert "submit_plan.workspace" in prompt
    assert "worktree_create" in prompt
    assert "worktree_bind_session" in prompt
    assert "Do not create, delete, bind, or switch worktrees with run_command" in prompt


async def test_pm_agent_plan_accepts_without_active_context(tmp_path):
    agent = PMAgent(_LLM(), language="en", min_plan_rounds=1, max_plan_rounds=1)

    plan = await agent.plan(
        "implement x",
        workspace=str(tmp_path),
        available_agents=[{"name": "codex", "model": "", "effort": ""}],
        requested_agent="codex",
        pm_model="",
        requested_effort="high",
        fallback_instruction="fallback",
        context="context text",
    )

    assert plan.instruction == "do it"


async def test_pm_agent_direct_reply_does_not_create_tool_runtime(tmp_path):
    def fail_runtime_factory(*_args, **_kwargs):
        raise AssertionError("direct_reply must not create PM tool runtime")

    agent = PMAgent(
        _LLM(),
        language="en",
        min_plan_rounds=1,
        max_plan_rounds=1,
        tool_runtime_factory=fail_runtime_factory,
    )

    plan = await agent.plan(
        "say hello",
        workspace=str(tmp_path),
        available_agents=[{"name": "codex", "model": "", "effort": ""}],
        requested_agent="codex",
        pm_model="",
        requested_effort="low",
        fallback_instruction="fallback",
        session_id="s1",
        task_id="t1",
    )

    assert plan.kind == "direct_reply"
    assert plan.reply == "Hello. What would you like help with next?"


async def test_pm_agent_plan_accepts_active_context(tmp_path):
    agent = PMAgent(_LLM(), language="en", min_plan_rounds=1, max_plan_rounds=1)

    plan = await agent.plan(
        "implement x",
        workspace=str(tmp_path),
        available_agents=[{"name": "codex", "model": "", "effort": ""}],
        requested_agent="codex",
        pm_model="",
        requested_effort="high",
        fallback_instruction="fallback",
        context="rendered active context",
        active_context=ActiveContext(rendered_text="rendered active context"),
    )

    assert plan.instruction == "do it"


class _ReviewLLM:
    async def complete(self, messages, *, json_mode=False, model="", on_stream=None, **kwargs):
        return json.dumps({"done": True, "summary": "reviewed"})


async def test_pm_agent_review_accepts_active_context():
    agent = PMAgent(_ReviewLLM(), language="en")
    plan = await PMAgent(_LLM(), language="en", min_plan_rounds=1, max_plan_rounds=1).plan(
        "implement x",
        workspace="",
        available_agents=[{"name": "codex", "model": "", "effort": ""}],
        requested_agent="codex",
        pm_model="",
        requested_effort="high",
        fallback_instruction="fallback",
        context="context text",
    )

    review = await agent.review(
        "implement x",
        plan,
        "rendered active context",
        run_count=1,
        context="rendered active context",
        active_context=ActiveContext(rendered_text="rendered active context"),
    )

    assert review.done is True
    assert review.summary == "reviewed"
