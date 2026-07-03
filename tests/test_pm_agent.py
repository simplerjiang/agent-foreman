from __future__ import annotations

import json

from foreman.client.core.context_v2 import ActiveContext
from foreman.client.core.pm_agent import PMAgent


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
