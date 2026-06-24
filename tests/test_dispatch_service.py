"""Tests for the DispatchService (T4.6, DESIGN §5.1): phone task dispatch + multi-session overview.

No real claude/codex is ever spawned — the launcher is injectable (a fake records the call). The
store is a real client SQLite Store so persistence + the `dispatch` event are exercised end to end.
"""

from __future__ import annotations

import asyncio
import json

from foreman.client.core.dispatch_service import DispatchService, _explicit_agent_targets
from foreman.client.core.pm_agent import PMAgent, PMPlan, PMReview, events_to_text, parse_plan
from foreman.client.store import Store
from foreman.client.store.models import (
    Approval,
    DecisionCard,
    Session,
)
from foreman.client.tools import PMToolRuntime
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import EventBus, make_event


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _cfg(*, agents=None, workspaces=None) -> Config:
    cfg = Config()
    if agents is not None:
        cfg.agents = agents
    if workspaces is not None:
        cfg.workspaces = workspaces
    return cfg


# ── create: validation (从严默认 inputs) ─────────────────────────────────────────────────────────


async def test_create_empty_goal_errors(tmp_path):
    svc = DispatchService(_cfg(workspaces=[WorkspaceCfg(path="D:/p")]), _store(tmp_path))
    assert (await svc.create("   "))["error"] == "empty_goal"


async def test_create_no_store_errors():
    svc = DispatchService(_cfg(workspaces=[WorkspaceCfg(path="D:/p")]), None)
    assert (await svc.create("do x"))["error"] == "no_store"


async def test_create_unknown_agent_errors(tmp_path):
    cfg = _cfg(
        agents={"claude-code": AgentCfg(command="claude", enabled=True)},
        workspaces=[WorkspaceCfg(path="D:/p")],
    )
    svc = DispatchService(cfg, _store(tmp_path))
    assert (await svc.create("do x", agent="codex"))["error"] == "unknown_agent"


async def test_create_no_workspace_errors(tmp_path):
    svc = DispatchService(_cfg(), _store(tmp_path))  # no workspaces configured, none passed
    assert (await svc.create("do x"))["error"] == "no_workspace"


# ── create: happy path + persistence + dispatch event ────────────────────────────────────────────


async def test_create_persists_session_task_and_event(tmp_path):
    store = _store(tmp_path)
    bus = EventBus()
    cfg = _cfg(
        agents={"claude-code": AgentCfg(command="claude", enabled=True, model="sonnet")},
        workspaces=[WorkspaceCfg(path="D:/proj")],
    )
    svc = DispatchService(cfg, store, bus=bus)  # no launcher → execution deferred
    res = await svc.create("refactor auth")

    assert res["ok"] is True
    assert res["agent"] == "claude-code"  # defaulted to the only enabled agent
    assert res["model"] == "sonnet"  # defaulted to the agent config model
    assert res["workspace"] == "D:/proj"  # defaulted to the configured workspace
    assert res["execution_deferred"] is True

    session = store.get_session(res["session_id"])
    assert session is not None and session.goal == "refactor auth"
    events = store.get_events(res["session_id"])
    dispatch_events = [e for e in events if e.type == "dispatch"]
    assert len(dispatch_events) == 1
    assert dispatch_events[0].task_id == res["task_id"]
    assert dispatch_events[0].source == "api"
    assert json.loads(dispatch_events[0].payload_json)["model"] == "sonnet"


async def test_create_can_continue_existing_session_with_source(tmp_path):
    store = _store(tmp_path)
    cfg = _cfg(workspaces=[WorkspaceCfg(path="D:/proj")])
    svc = DispatchService(cfg, store)
    first = await svc.create("first task", source="desktop")
    second = await svc.create(
        "follow up", session_id=first["session_id"], source="desktop"
    )

    assert second["ok"] is True
    assert second["continued"] is True
    assert second["session_id"] == first["session_id"]
    assert second["task_id"] != first["task_id"]
    assert store.get_session(first["session_id"]).goal == "first task"
    dispatch_events = [e for e in store.get_events(first["session_id"]) if e.type == "dispatch"]
    assert [e.source for e in dispatch_events] == ["desktop", "desktop"]
    assert json.loads(dispatch_events[-1].payload_json)["continued"] is True


async def test_continue_pm_agent_session_stays_with_pm(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        language = "zh"
        max_runs = 1

        async def plan(self, goal, **_kw):
            return PMPlan(
                agent="codex",
                model="",
                effort="high",
                instruction=f"执行：{goal}",
                summary="PM 已规划。",
                todo=["执行任务"],
            )

        async def review(self, *_args, **_kw):
            return PMReview(done=True, summary="完成")

    class FakeHandle:
        session_id = ""

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            handle = FakeHandle()
            handle.session_id = session_id
            store.add_event(make_event("stop", agent, session_id, payload={"result": "done"}))
            return handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    first = await svc.create("第一条", source="desktop")
    await asyncio.gather(*list(svc._tasks))
    second = await svc.create("继续说明", session_id=first["session_id"], source="desktop")
    await asyncio.gather(*list(svc._tasks))

    assert second["ok"] is True
    assert second["agent"] == "pm-agent"
    assert second["continued"] is True
    dispatches = [json.loads(e.payload_json) for e in store.get_events(first["session_id"]) if e.type == "dispatch"]
    assert dispatches[-1]["pm_agent"] is True


async def test_continue_missing_session_errors(tmp_path):
    svc = DispatchService(_cfg(workspaces=[WorkspaceCfg(path="D:/proj")]), _store(tmp_path))
    res = await svc.create("follow up", session_id="missing")
    assert res["error"] == "session_not_found"


async def test_compact_stores_context_and_emits_event(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        async def compact(self, goal, timeline, *, existing_context=""):
            assert goal == "first task"
            assert "agent said x" in timeline
            return "short context"

    cfg = _cfg(workspaces=[WorkspaceCfg(path="D:/proj")])
    svc = DispatchService(cfg, store, bus=EventBus(), pm_agent=FakePM())
    res = await svc.create("first task")
    store.add_event(make_event("agent_output", "claude-code", res["session_id"], payload={"text": "agent said x"}))

    compacted = await svc.compact(res["session_id"])

    assert compacted["ok"] is True
    assert store.get_session(res["session_id"]).plan == "short context"
    events = store.get_events(res["session_id"])
    assert any(e.type == "context_compact" for e in events)


async def test_compact_stores_context_snapshot_and_memory_items(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        async def compact(self, goal, timeline, *, existing_context=""):
            assert "[event:" in timeline
            return json.dumps(
                {
                    "version": 1,
                    "session_state": {
                        "goal_quote": goal,
                        "summary": "tests failed after refactor",
                        "status": "blocked",
                    },
                    "working_memory": {
                        "verified_facts": [
                            {
                                "text": "pytest failed in test_auth.py",
                                "source_refs": ["event:e1"],
                                "status": "verified",
                                "importance": 90,
                            }
                        ],
                        "claims": [
                            {
                                "text": "agent claimed the refactor was done",
                                "source_refs": ["event:e2"],
                                "status": "claimed",
                            }
                        ],
                        "decisions": [],
                        "constraints": [],
                        "open_questions": [],
                        "risks": [],
                        "next_steps": [],
                        "files": [],
                        "commands": [],
                        "tests": [],
                    },
                    "retrieved_evidence": [],
                    "dynamic_tail": [],
                    "omitted": [{"kind": "timeline", "reason": "token_budget"}],
                }
            )

    cfg = _cfg(workspaces=[WorkspaceCfg(path="D:/proj")])
    svc = DispatchService(cfg, store, bus=EventBus(), pm_agent=FakePM())
    res = await svc.create("first task")
    store.add_event(
        make_event("agent_output", "claude-code", res["session_id"], payload={"text": "done"})
    )

    compacted = await svc.compact(res["session_id"])

    snapshots = store.get_context_snapshots(res["session_id"])
    memories = store.get_memory_items(res["session_id"])
    payload = json.loads(
        [e for e in store.get_events(res["session_id"]) if e.type == "context_compact"][-1]
        .payload_json
    )
    assert compacted["snapshot_id"] == snapshots[0].id == payload["snapshot_id"]
    assert json.loads(snapshots[0].summary_json)["session_state"]["status"] == "blocked"
    assert sorted((m.kind, m.status, m.text) for m in memories) == [
        ("fact", "claimed", "agent claimed the refactor was done"),
        ("fact", "verified", "pytest failed in test_auth.py"),
    ]


def test_events_to_text_extracts_nested_agent_item_text():
    rows = [
        make_event(
            "agent_output",
            "codex",
            "s1",
            payload={
                "type": "item.completed",
                "item": {"content": [{"type": "output_text", "text": "nested text"}]},
            },
        )
    ]

    assert "nested text" in events_to_text(rows)


def test_parse_plan_extracts_todo_and_deliberation():
    plan = parse_plan(
        json.dumps(
            {
                "summary": "use codex",
                "agent": "codex",
                "effort": "high",
                "instruction": "do it",
                "todo": ["inspect", "test"],
                "deliberation": ["codex has the right repo tools"],
                "ready": False,
            }
        ),
        enabled_agents=["codex"],
        fallback_agent="codex",
        fallback_model="",
        fallback_effort="high",
        fallback_instruction="fallback",
    )

    assert plan.todo == ["inspect", "test"]
    assert plan.deliberation == ["codex has the right repo tools"]
    assert plan.ready is False


async def test_create_runs_launcher_in_background(tmp_path):
    calls: list[tuple] = []

    async def launcher(session_id, goal, workspace, agent, model, effort):
        calls.append((session_id, goal, workspace, agent, model, effort))

    cfg = _cfg(workspaces=[WorkspaceCfg(path="D:/p")])
    svc = DispatchService(cfg, _store(tmp_path), launcher=launcher)
    res = await svc.create("do x", model="run-model", effort="high")
    assert res["execution_deferred"] is False
    assert res["effort"] == "high"
    await asyncio.sleep(0.02)  # let the fire-and-forget launch run
    assert calls and calls[0][0] == res["session_id"] and calls[0][1] == "do x"
    assert calls[0][4] == "run-model"
    assert calls[0][5] == "high"  # reasoning level threads to the launcher


async def test_pm_agent_plans_before_launch_and_reviews_until_done(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        max_runs = 2

        def __init__(self):
            self.plan_goal = ""
            self.reviews = 0
            self.timelines = []
            self.review_states = []
            self.state_keys = []

        async def plan(self, goal, **kw):
            self.plan_goal = goal
            assert kw["requested_agent"] == ""
            return PMPlan(
                agent="codex",
                model="gpt-5",
                effort="high",
                instruction="PM planned instruction",
                summary="use codex",
                todo=["inspect", "test"],
            )

        async def review(
            self, goal, plan, timeline, *, run_count, context="", pm_model="", review_state="",
            todo_status=None, state_key=""
        ):
            self.reviews += 1
            self.timelines.append(timeline)
            self.review_states.append(review_state)
            self.state_keys.append(state_key)
            assert "PM planned instruction" in plan.instruction
            if self.reviews == 1:
                assert todo_status == [
                    {"title": "inspect", "status": "in_progress"},
                    {"title": "test", "status": "pending"},
                ]
                assert "first" in timeline
            if self.reviews == 1:
                return PMReview(
                    done=False,
                    summary="not done",
                    follow_up="PM follow-up",
                    todo_status=[
                        {"title": "inspect", "status": "done"},
                        {"title": "test", "status": "in_progress"},
                    ],
                )
            assert "first" not in timeline
            assert "PM follow-up" in timeline
            assert "prior_reviews" in review_state
            return PMReview(done=True, summary="done")

    class FakeHandle:
        session_id = "s"

    class FakeRunner:
        def __init__(self):
            self.launched = []
            self.sent = []
            self.handle = FakeHandle()

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.handle.session_id = session_id
            self.launched.append((agent, instruction, str(workspace), model, effort))
            store.add_event(make_event("stop", agent, session_id, payload={"result": "first"}))
            return self.handle

        async def wait(self, handle):
            return None

        async def send(self, handle, text):
            self.sent.append(text)
            store.add_event(make_event("stop", "codex", handle.session_id, payload={"result": text}))

    cfg = _cfg(
        agents={
            "claude-code": AgentCfg(command="claude", enabled=True),
            "codex": AgentCfg(command="codex", enabled=True),
        },
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    pm = FakePM()
    runner = FakeRunner()
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=pm)

    res = await svc.create("raw user task", agent="claude-code")
    assert res["pm_agent"] is True
    assert res["agent"] == "pm-agent"
    await asyncio.gather(*list(svc._tasks))

    assert pm.plan_goal == "raw user task"
    assert pm.state_keys == [f"{res['session_id']}:{res['task_id']}:pm-review"] * 2
    assert runner.launched == [("codex", "PM planned instruction", str(tmp_path), "gpt-5", "high")]
    assert runner.sent == ["PM follow-up"]
    events = store.get_events(res["session_id"])
    assert "pm_plan" in [e.type for e in events]
    reviews = [json.loads(e.payload_json) for e in events if e.type == "pm_review"]
    assert len(reviews) == 2
    assert [x["status"] for x in reviews[0]["todo_status"]] == ["done", "in_progress"]
    assert [x["status"] for x in reviews[1]["todo_status"]] == ["done", "done"]


async def test_real_pm_agent_stream_chunks_are_persisted_and_published(tmp_path):
    store = _store(tmp_path)

    class FakeLLM:
        model = "pm-model"

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            if on_stream is not None:
                await on_stream({"kind": "reasoning", "delta": "thinking", "event_type": "r"})
                await on_stream({"kind": "output", "delta": "partial", "event_type": "o"})
            system = messages[0].content
            if "Analyze the user's task" in system:
                return json.dumps(
                    {
                        "summary": "use codex",
                        "agent": "codex",
                        "model": "",
                        "effort": "high",
                        "instruction": "do it",
                    }
                )
            return json.dumps({"done": True, "summary": "done", "reason": "", "follow_up": ""})

    class FakeHandle:
        session_id = "s"

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            handle = FakeHandle()
            handle.session_id = session_id
            store.add_event(make_event("stop", agent, session_id, payload={"result": "done"}))
            return handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    bus = EventBus()
    q = bus.subscribe_queue()
    svc = DispatchService(
        cfg, store, bus=bus, runner=FakeRunner(), pm_agent=PMAgent(FakeLLM())
    )

    res = await svc.create("raw user task")
    await asyncio.gather(*list(svc._tasks))

    rows = store.get_events(res["session_id"])
    stream_rows = [e for e in rows if e.type in {"pm_output", "pm_reasoning"}]
    assert {e.type for e in stream_rows} == {"pm_output", "pm_reasoning"}
    payloads = [json.loads(e.payload_json) for e in stream_rows]
    assert all(p["stream_id"] and p["delta"] for p in payloads)
    assert {"plan", "review-1"}.issubset({p["phase"] for p in payloads})
    assert "thinking" not in events_to_text(rows)

    published = []
    while not q.empty():
        published.append(q.get_nowait().type)
    assert "pm_output" in published and "pm_reasoning" in published


async def test_pm_agent_tool_loop_persists_tool_events_before_launch(tmp_path):
    store = _store(tmp_path)
    (tmp_path / "notes.txt").write_text("launch evidence", encoding="utf-8")
    captured = {"plan_prompt": ""}

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            if "reviewing a coding CLI" in messages[0].content:
                return json.dumps({"done": True, "summary": "done", "reason": "", "follow_up": ""})
            if not captured["plan_prompt"]:
                captured["plan_prompt"] = messages[-1].content
            if "Runtime-generated tool_results" not in messages[-1].content:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "read",
                                "name": "read_file",
                                "arguments": {"path": "notes.txt"},
                            }
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "read evidence",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "Use the evidence from notes.txt and verify.",
                    "todo": ["verify"],
                    "ready": True,
                }
            )

    class FakeHandle:
        session_id = "s"

    class FakeRunner:
        def __init__(self):
            self.launched = []

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.launched.append((agent, instruction))
            handle = FakeHandle()
            handle.session_id = session_id
            store.add_event(make_event("stop", agent, session_id, payload={"result": "done"}))
            return handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    pm = PMAgent(
        FakeLLM(),
        tool_runtime_factory=lambda workspace: PMToolRuntime.from_config(cfg, workspace),
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=pm)

    res = await svc.create("inspect then run")
    await asyncio.gather(*list(svc._tasks))

    rows = store.get_events(res["session_id"])
    assert "tool_schema" in captured["plan_prompt"]
    assert "runtime_context" in captured["plan_prompt"]
    assert "policy_context" in captured["plan_prompt"]
    assert [e.type for e in rows if e.type in {"tool_pre", "tool_post"}] == [
        "tool_pre",
        "tool_post",
    ]
    assert [e.type for e in rows].index("tool_post") < [e.type for e in rows].index("pm_plan")


async def test_pm_agent_plan_prompt_requires_selected_language(tmp_path):
    captured: dict = {}

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            captured["system"] = messages[0].content
            return json.dumps(
                {
                    "summary": "使用 codex",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "完成任务并验证。",
                }
            )

    pm = PMAgent(FakeLLM(), language="zh")
    plan = await pm.plan(
        "修复问题",
        workspace=str(tmp_path),
        available_agents=[{"name": "codex", "model": "", "effort": ""}],
        requested_agent="codex",
        pm_model="",
        requested_effort="high",
        fallback_instruction="fallback",
    )

    assert plan.summary == "使用 codex"
    assert "请始终用简体中文回答" in captured["system"]
    assert "Human-facing JSON string values must follow the selected output language" in captured["system"]


async def test_pm_agent_plans_for_at_least_two_rounds(tmp_path):
    captured: dict = {"calls": 0, "prompts": []}

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            captured["calls"] += 1
            captured["prompts"].append(messages[-1].content)
            return json.dumps(
                {
                    "summary": f"round {captured['calls']}",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "do it",
                    "todo": ["inspect", "test"],
                    "deliberation": [f"note {captured['calls']}"],
                    "ready": True,
                }
            )

    pm = PMAgent(FakeLLM(), min_plan_rounds=2, max_plan_rounds=3)
    plan = await pm.plan(
        "fix x",
        workspace=str(tmp_path),
        available_agents=[{"name": "codex", "model": "", "effort": "", "full_access": True}],
        requested_agent="codex",
        pm_model="",
        requested_effort="high",
        fallback_instruction="fallback",
    )

    assert captured["calls"] == 2
    assert len(plan.planning_rounds) == 2
    assert "# Prior PM planning rounds" in captured["prompts"][1]
    assert plan.todo == ["inspect", "test"]


async def test_pm_model_override_is_not_passed_to_coding_agent(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        max_runs = 1

        async def plan(self, goal, **kw):
            assert kw["pm_model"] == "gpt-5.5"
            assert kw["requested_agent"] == ""
            assert kw["requested_effort"] == "high"
            return PMPlan(
                agent="claude-code",
                model="gpt-5.5",
                effort="high",
                instruction="PM planned instruction",
            )

        async def review(self, goal, plan, timeline, *, run_count, context="", pm_model=""):
            assert pm_model == "gpt-5.5"
            return PMReview(done=True, summary="done")

    class FakeHandle:
        session_id = "s"

    class FakeRunner:
        def __init__(self):
            self.launched = []
            self.handle = FakeHandle()

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.handle.session_id = session_id
            self.launched.append((agent, model, effort))
            store.add_event(make_event("stop", agent, session_id, payload={"result": "first"}))
            return self.handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"claude-code": AgentCfg(command="claude", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    runner = FakeRunner()
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=FakePM())

    res = await svc.create("raw user task", model="gpt-5.5")
    assert res["model"] == "gpt-5.5"
    await asyncio.gather(*list(svc._tasks))

    assert runner.launched == [("claude-code", "", "high")]
    pm_plan = [e for e in store.get_events(res["session_id"]) if e.type == "pm_plan"][0]
    assert json.loads(pm_plan.payload_json)["model"] == ""


async def test_explicit_agent_names_still_go_through_pm(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        language = "zh"
        max_runs = 1

        def __init__(self):
            self.plan_goal = ""

        async def plan(self, goal, **kw):
            self.plan_goal = goal
            assert "codex" in goal and "claude" in goal
            assert {row["name"] for row in kw["available_agents"]} == {"claude-code", "codex"}
            return PMPlan(
                agent="codex",
                model="",
                effort="high",
                instruction="由 PM 统一规划后交给 codex 执行。",
                summary="PM 已统一规划。",
                todo=["执行任务"],
            )

        async def review(self, *_args, **_kw):
            return PMReview(done=True, summary="完成")

    class FakeHandle:
        session_id = ""

    class FakeRunner:
        def __init__(self):
            self.launched = []
            self.handle = FakeHandle()

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.handle.session_id = session_id
            self.launched.append((agent, instruction, str(workspace), model, effort))
            store.add_event(make_event("stop", agent, session_id, payload={"result": agent}))
            return self.handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={
            "claude-code": AgentCfg(command="claude", enabled=True),
            "codex": AgentCfg(command="codex", enabled=True),
        },
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    runner = FakeRunner()
    pm = FakePM()
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=pm)

    res = await svc.create("\u53ebcodex\u548cclaude\u7ed9\u6211\u62a5\u4e2a\u5230", model="gpt-5.5")
    assert res["agent"] == "pm-agent"
    assert res["model"] == "gpt-5.5"
    assert res["direct_agents"] == []
    assert store.get_session(res["session_id"]).agent_type == "pm-agent"
    await asyncio.gather(*list(svc._tasks))

    assert pm.plan_goal == "\u53ebcodex\u548cclaude\u7ed9\u6211\u62a5\u4e2a\u5230"
    assert [call[0] for call in runner.launched] == ["codex"]
    assert [call[3] for call in runner.launched] == [""]  # PM model is not a CLI model
    plans = [json.loads(e.payload_json) for e in store.get_events(res["session_id"]) if e.type == "pm_plan"]
    assert [p["agent"] for p in plans] == ["codex"]
    assert plans[0]["summary"] == "PM 已统一规划。"


async def test_pm_launches_selected_agent_before_waiting(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        language = "en"

        async def plan(self, *_a, **_kw):
            return PMPlan(
                agent="codex",
                model="",
                effort="high",
                instruction="PM-selected work",
                summary="PM picked codex.",
            )

        async def review(self, *_args, **_kw):
            return PMReview(done=True, summary="done")

    class FakeHandle:
        def __init__(self, agent, session_id):
            self.agent = agent
            self.session_id = session_id

    class FakeRunner:
        def __init__(self):
            self.launched = []
            self.wait_started = asyncio.Event()
            self.release_waits = asyncio.Event()

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.launched.append(agent)
            return FakeHandle(agent, session_id)

        async def wait(self, handle):
            self.wait_started.set()
            await self.release_waits.wait()

    cfg = _cfg(
        agents={
            "claude-code": AgentCfg(command="claude", enabled=True),
            "codex": AgentCfg(command="codex", enabled=True),
        },
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    runner = FakeRunner()
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=FakePM())

    await svc.create("ask codex and claude to report", model="pm-model")
    await asyncio.wait_for(runner.wait_started.wait(), timeout=1)
    try:
        assert runner.launched == ["codex"]
    finally:
        runner.release_waits.set()
        await asyncio.gather(*list(svc._tasks))


def test_explicit_agent_target_detection_is_conservative():
    enabled = ["claude-code", "codex"]
    assert _explicit_agent_targets("\u53ebcodex\u548cclaude\u7ed9\u6211\u62a5\u4e2a\u5230", enabled) == [
        "codex", "claude-code",
    ]
    assert _explicit_agent_targets("use codex to report status", enabled) == ["codex"]
    assert _explicit_agent_targets("fix Codex adapter bug", enabled) == []


async def test_create_ignores_bad_effort(tmp_path):
    # An unrecognized level is dropped (never passed to the CLI), falling back to the default ("").
    cfg = _cfg(workspaces=[WorkspaceCfg(path="D:/p")])
    svc = DispatchService(cfg, _store(tmp_path))
    res = await svc.create("do x", effort="turbo")
    assert res["effort"] == ""


async def test_launcher_failure_records_error_event(tmp_path):
    async def launcher(*_a):
        raise RuntimeError("boom")

    store = _store(tmp_path)
    cfg = _cfg(workspaces=[WorkspaceCfg(path="D:/p")])
    svc = DispatchService(cfg, store, launcher=launcher)
    res = await svc.create("do x")
    await asyncio.sleep(0.02)
    errors = [e for e in store.get_events(res["session_id"]) if e.type == "error"]
    assert errors and "RuntimeError" in (errors[0].payload_json or "")


async def test_default_agent_when_no_agents_configured(tmp_path):
    svc = DispatchService(_cfg(workspaces=[WorkspaceCfg(path="D:/p")]), _store(tmp_path))
    res = await svc.create("do x")
    assert res["agent"] == "claude-code"  # lenient default for minimal configs


async def test_explicit_workspace_and_agent_win(tmp_path):
    cfg = _cfg(
        agents={
            "claude-code": AgentCfg(command="claude", enabled=True),
            "codex": AgentCfg(command="codex", enabled=True),
        },
        workspaces=[WorkspaceCfg(path="D:/default")],
    )
    svc = DispatchService(cfg, _store(tmp_path))
    # an explicit workspace nested under an approved root is allowed (§6.6 白名单).
    res = await svc.create("do x", workspace="D:/default/sub", agent="codex")
    assert res["workspace"] == "D:/default/sub" and res["agent"] == "codex"


async def test_workspace_outside_allowlist_rejected(tmp_path):
    cfg = _cfg(workspaces=[WorkspaceCfg(path="D:/default")])
    svc = DispatchService(cfg, _store(tmp_path))
    res = await svc.create("do x", workspace="E:/somewhere-else")
    assert res["error"] == "workspace_not_allowed"


async def test_explicit_workspace_rejected_when_no_allowlist(tmp_path):
    # No workspaces configured → fail closed: an explicit path is rejected, not run in an arbitrary
    # cwd (issue #1 P2). Previously this failed open and accepted the path as-is.
    svc = DispatchService(_cfg(), _store(tmp_path))
    res = await svc.create("do x", workspace="E:/anywhere")
    assert res["error"] == "workspace_not_allowed"


async def test_explicit_workspace_accepted_when_no_allowlist_with_dev_flag(tmp_path):
    # The escape hatch: opting into allow_unlisted_workspaces_for_dev restores accept-as-is (P2).
    cfg = _cfg()
    cfg.allow_unlisted_workspaces_for_dev = True
    svc = DispatchService(cfg, _store(tmp_path))
    res = await svc.create("do x", workspace="E:/anywhere")
    assert res["ok"] and res["workspace"] == "E:/anywhere"


# ── multi-session overview ───────────────────────────────────────────────────────────────────────


async def test_overview_counts_and_newest_first(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="older", status="running",
                              agent_type="claude-code", created_at="2026-01-01T00:00:00Z"))
    store.add_session(Session(id="s2", goal="newer", status="idle",
                              agent_type="codex", created_at="2026-02-01T00:00:00Z"))
    store.add_event(make_event("agent_output", "claude-code", "s1", payload={"t": "a"}))
    store.add_event(make_event("stop", "claude-code", "s1", payload={"r": "done"}))
    store.add_event(make_event("agent_output", "codex", "s2", payload={"t": "b"}))
    # an open (undecided) card + a pending approval on s1
    store.add_decision_card(DecisionCard(id="c1", action_id="a1", session_id="s1", ts="t"))
    store.add_approval(Approval(id="ap1", session_id="s1", status="pending", requested_at="t"))

    svc = DispatchService(_cfg(), store)
    ov = svc.overview()

    assert [d["id"] for d in ov] == ["s2", "s1"]  # newest (created_at) first
    s1 = next(d for d in ov if d["id"] == "s1")
    assert s1["events"] == 2
    assert s1["last_event_type"] == "stop"
    assert s1["open_cards"] == 1
    assert s1["pending_approvals"] == 1
    s2 = next(d for d in ov if d["id"] == "s2")
    assert s2["events"] == 1 and s2["open_cards"] == 0 and s2["pending_approvals"] == 0


def test_overview_no_store_is_empty():
    assert DispatchService(_cfg(), None).overview() == []
