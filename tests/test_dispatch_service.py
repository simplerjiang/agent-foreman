"""Tests for the DispatchService (T4.6, DESIGN §5.1): phone task dispatch + multi-session overview.

No real claude/codex is ever spawned — the launcher is injectable (a fake records the call). The
store is a real client SQLite Store so persistence + the `dispatch` event are exercised end to end.
"""

from __future__ import annotations

import asyncio
import json

from foreman.client.core.dispatch_service import DispatchService
from foreman.client.core.pm_agent import PMPlan, PMReview
from foreman.client.store import Store
from foreman.client.store.models import (
    Approval,
    DecisionCard,
    Session,
)
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

        async def plan(self, goal, **kw):
            self.plan_goal = goal
            assert kw["requested_agent"] == "claude-code"
            return PMPlan(
                agent="codex",
                model="gpt-5",
                effort="high",
                instruction="PM planned instruction",
                summary="use codex",
            )

        async def review(self, goal, plan, timeline, *, run_count, context=""):
            self.reviews += 1
            assert "PM planned instruction" in plan.instruction
            if self.reviews == 1:
                return PMReview(done=False, summary="not done", follow_up="PM follow-up")
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
    await asyncio.gather(*list(svc._tasks))

    assert pm.plan_goal == "raw user task"
    assert runner.launched == [("codex", "PM planned instruction", str(tmp_path), "gpt-5", "high")]
    assert runner.sent == ["PM follow-up"]
    events = store.get_events(res["session_id"])
    assert "pm_plan" in [e.type for e in events]
    assert [e.type for e in events].count("pm_review") == 2


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
