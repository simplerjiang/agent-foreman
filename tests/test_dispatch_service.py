"""Tests for the DispatchService (T4.6, DESIGN §5.1): phone task dispatch + multi-session overview.

No real claude/codex is ever spawned — the launcher is injectable (a fake records the call). The
store is a real client SQLite Store so persistence + the `dispatch` event are exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import httpx
import pytest

from foreman.client.core.context_v2 import ContextManager
from foreman.client.core.dispatch_service import (
    DispatchService,
    _explicit_agent_targets,
    _pm_failure_text,
)
from foreman.client.core.pm_agent import (
    PMAgent,
    PMPlan,
    PMRecovery,
    PMReview,
    events_to_text,
    parse_plan,
)
from foreman.client.core.worktree_manager import WorktreeManager
from foreman.client.store import Store
from foreman.client.store.models import (
    Approval,
    DecisionCard,
    Session,
    Task,
    WorktreeLease,
)
from foreman.client.tools import PMToolRuntime
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import EventBus, make_event
from foreman.shared.llm import LLMStalledError, LLMToolCall, LLMToolResponse


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


async def test_cancelled_session_is_not_overwritten_by_background_completion(tmp_path):
    store = _store(tmp_path)
    launched = asyncio.Event()
    release = asyncio.Event()

    class FakePM:
        max_runs = 1

        async def plan(self, goal, **_kw):
            return PMPlan(agent="codex", model="", effort="", instruction="do it")

        async def review(self, goal, plan, timeline, **_kw):
            return PMReview(done=True, summary="done")

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            launched.set()
            return object()

        async def wait(self, handle):
            await release.wait()

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("long task")
    await asyncio.wait_for(launched.wait(), timeout=1)
    tasks = list(svc._session_tasks.get(res["session_id"], ()))
    assert (await svc.cancel(res["session_id"]))["status"] == "cancelled"

    # T2.3: cancel aborts the in-flight task (held in runner.wait); it doesn't run to completion, so
    # the background path can't flip the cancelled status back to "done". `release` is never set.
    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(t.cancelled() for t in tasks)
    assert store.get_session(res["session_id"]).status == "cancelled"
    with store.session() as db:
        assert db.get(Task, res["task_id"]).status == "cancelled"
    assert (await svc.delete(res["session_id"]))["ok"] is True
    assert store.get_session(res["session_id"]) is None


async def test_session_done_and_delete_release_active_worktree_lock(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(tmp_path), status="running"))
    store.add_worktree_lease(
        WorktreeLease(
            id="lease-1",
            repo_root=str(tmp_path),
            main_workspace=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch="feature",
            base_ref="main",
            base_sha="base",
            head_sha="head",
            session_id="s1",
            task_id="t1",
            locked=True,
        )
    )
    svc = DispatchService(_cfg(workspaces=[WorkspaceCfg(path=str(tmp_path))]), store)

    svc._mark_session("s1", "done")
    released = store.get_worktree_lease("lease-1")

    assert released.status == "released"
    assert released.locked is False

    store.update_worktree_lease("lease-1", status="active", locked=True)
    assert (await svc.delete("s1"))["ok"] is True
    deleted_release = store.get_worktree_lease("lease-1")
    assert deleted_release.status == "released"
    assert deleted_release.locked is False


async def test_cancel_interrupts_running_agent_handle(tmp_path):
    store = _store(tmp_path)
    launched = asyncio.Event()
    interrupted = asyncio.Event()
    handle = object()

    class FakePM:
        max_runs = 1

        async def plan(self, goal, **_kw):
            return PMPlan(agent="codex", model="", effort="", instruction="do it")

        async def review(self, goal, plan, timeline, **_kw):
            return PMReview(done=True, summary="done")

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            launched.set()
            return handle

        async def wait(self, _handle):
            await asyncio.Event().wait()

        def handle_for_session(self, session_id):
            return handle

        async def interrupt(self, _handle):
            interrupted.set()

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("long task")
    await asyncio.wait_for(launched.wait(), timeout=1)
    out = await svc.cancel(res["session_id"])

    assert out["interrupted_agent"] is True
    assert interrupted.is_set()
    note = json.loads(next(e.payload_json for e in store.get_events(res["session_id"]) if e.type == "notification"))
    assert note["interrupted_agent"] is True


async def test_interrupt_runner_handle_interrupts_all_live_session_handles(tmp_path):
    store = _store(tmp_path)
    interrupted = []

    class Handle:
        def __init__(self, handle_id):
            self.id = handle_id

    handles = [Handle("h1"), Handle("h2")]

    class FakeRunner:
        def handles_for_session(self, session_id):
            assert session_id == "s1"
            return handles

        async def interrupt(self, handle):
            interrupted.append(handle.id)

    svc = DispatchService(Config(), store, bus=EventBus(), runner=FakeRunner())

    assert await svc._interrupt_runner_handle("s1") is True
    assert interrupted == ["h1", "h2"]


async def test_pm_tool_bind_updates_session_and_launches_from_git_worktree(tmp_path):
    store = _store(tmp_path)
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init"], cwd=main, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    )
    (main / "README.md").write_text("main\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=main, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=main, check=True, capture_output=True, text=True)
    launched = asyncio.Event()
    launched_workspaces: list[str] = []
    bind_results: list[dict] = []

    class FakePM:
        max_runs = 1

        async def plan(
            self,
            goal,
            *,
            store=None,
            session_id="",
            task_id="",
            workspace="",
            main_workspace="",
            worktree_manager=None,
            **_kw,
        ):
            bound = worktree_manager.create(
                {
                    "store": store,
                    "session_id": session_id,
                    "task_id": task_id,
                    "workspace": workspace,
                    "main_workspace": main_workspace,
                    "branch_prefix": "foreman/",
                    "default_base_ref": "HEAD",
                },
                goal="Selected Worktree",
                bind_session=True,
            )
            bind_results.append(bound)
            assert bound["ok"] is True
            assert bound["session_bound"] is True
            return PMPlan(
                agent="codex",
                model="",
                effort="",
                workspace="",
                instruction="do it from the selected worktree",
            )

        async def review(self, goal, plan, timeline, **_kw):
            return PMReview(done=True, summary="done")

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            launched_workspaces.append(str(workspace))
            launched.set()
            return type(
                "Handle",
                (),
                {
                    "id": "h1",
                    "cwd": str(workspace),
                    "worktree": str(workspace),
                    "branch": "foreman/test",
                },
            )()

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(main))],
    )
    svc = DispatchService(
        cfg,
        store,
        bus=EventBus(),
        runner=FakeRunner(),
        pm_agent=FakePM(),
        worktree_manager=WorktreeManager(),
    )

    res = await svc.create("use the existing worktree", workspace=str(main))
    await asyncio.wait_for(launched.wait(), timeout=1)

    worktree = bind_results[0]["workspace"]
    session = store.get_session(res["session_id"])
    assert session is not None
    assert session.workspace == worktree
    assert session.main_workspace == str(main)
    assert launched_workspaces == [worktree]
    agent_input = [
        json.loads(event.payload_json)
        for event in store.get_events(res["session_id"])
        if event.type == "agent_input"
    ][-1]
    assert agent_input["cwd"] == worktree
    assert agent_input["worktree"] == worktree


async def test_existing_session_worktree_workspace_is_used_for_followup_launch(tmp_path):
    store = _store(tmp_path)
    main = tmp_path / "main"
    worktree = tmp_path / "followup-worktree"
    main.mkdir()
    subprocess.run(["git", "init"], cwd=main, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    )
    (main / "README.md").write_text("main\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=main, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=main, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/followup", str(worktree)],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    )
    session = store.add_session(
        Session(
            id="s1",
            goal="existing",
            workspace=str(worktree),
            main_workspace=str(main),
            agent_type="pm-agent",
            status="idle",
        )
    )
    launched = asyncio.Event()
    launched_workspaces: list[str] = []

    class FakePM:
        max_runs = 1

        async def plan(self, goal, **_kw):
            return PMPlan(agent="codex", model="", effort="", instruction="continue in worktree")

        async def review(self, goal, plan, timeline, **_kw):
            return PMReview(done=True, summary="done")

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            launched_workspaces.append(str(workspace))
            launched.set()
            return type(
                "Handle",
                (),
                {"id": "h1", "cwd": str(workspace), "worktree": str(workspace)},
            )()

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(main))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("follow up", session_id=session.id)
    await asyncio.wait_for(launched.wait(), timeout=1)

    assert res["continued"] is True
    assert res["workspace"] == str(worktree)
    assert launched_workspaces == [str(worktree)]
    agent_input = [
        json.loads(event.payload_json)
        for event in store.get_events(session.id)
        if event.type == "agent_input"
    ][-1]
    assert agent_input["cwd"] == str(worktree)
    assert agent_input["worktree"] == str(worktree)


async def test_dispatch_injects_pm_runtime_context_dependencies(tmp_path):
    store = _store(tmp_path)
    launched = asyncio.Event()
    manager = object()
    seen: dict[str, object] = {}

    class FakePM:
        max_runs = 1

        async def plan(
            self,
            goal,
            *,
            store=None,
            session_id="",
            task_id="",
            workspace="",
            main_workspace="",
            worktree_manager=None,
            **_kw,
        ):
            seen.update(
                {
                    "store": store,
                    "session_id": session_id,
                    "task_id": task_id,
                    "workspace": workspace,
                    "main_workspace": main_workspace,
                    "worktree_manager": worktree_manager,
                }
            )
            return PMPlan(agent="codex", model="", effort="", instruction="do it")

        async def review(self, goal, plan, timeline, **_kw):
            return PMReview(done=True, summary="done")

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            launched.set()
            return object()

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(
        cfg,
        store,
        bus=EventBus(),
        runner=FakeRunner(),
        pm_agent=FakePM(),
        worktree_manager=manager,
    )

    res = await svc.create("inject context")
    await asyncio.wait_for(launched.wait(), timeout=1)

    assert seen["store"] is store
    assert seen["session_id"] == res["session_id"]
    assert seen["task_id"] == res["task_id"]
    assert seen["workspace"] == str(tmp_path)
    assert seen["main_workspace"] == str(tmp_path)
    assert seen["worktree_manager"] is manager


async def test_cancelled_session_is_not_overwritten_by_background_failure(tmp_path):
    store = _store(tmp_path)
    launched = asyncio.Event()
    release = asyncio.Event()

    class FakePM:
        max_runs = 1

        async def plan(self, goal, **_kw):
            return PMPlan(agent="codex", model="", effort="", instruction="do it")

        async def review(self, goal, plan, timeline, **_kw):
            return PMReview(done=True, summary="done")

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            launched.set()
            return object()

        async def wait(self, handle):
            await release.wait()
            raise RuntimeError("agent failed after cancel")

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("long task")
    await asyncio.wait_for(launched.wait(), timeout=1)
    tasks = list(svc._session_tasks.get(res["session_id"], ()))
    assert (await svc.cancel(res["session_id"]))["status"] == "cancelled"

    # T2.3: the held runner.wait is cancelled before its post-`release` failure can fire, so the
    # cancelled status is not overwritten by a background error either. `release` is never set.
    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(t.cancelled() for t in tasks)
    assert store.get_session(res["session_id"]).status == "cancelled"


async def test_cancel_aborts_in_flight_pm_call(tmp_path):
    """Stop truly cancels the running PM call, not just flags the session (T2.3).

    A PM whose plan() hangs (a stuck ws/LLM round) must be cancelled by `cancel()`: the launch task
    ends cancelled and plan() never returns, instead of running to completion in the background.
    """
    store = _store(tmp_path)
    planning = asyncio.Event()
    state = {"plan_completed": False}

    class HangingPM:
        max_runs = 1

        async def plan(self, goal, **_kw):
            planning.set()
            await asyncio.Event().wait()  # never resolves — models a stuck PM ws call
            state["plan_completed"] = True
            return PMPlan(agent="codex", model="", effort="", instruction="never")

        async def review(self, *_a, **_kw):
            return PMReview(done=True, summary="done")

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            return object()

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=HangingPM())

    res = await svc.create("stuck task")
    sid = res["session_id"]
    await asyncio.wait_for(planning.wait(), timeout=1)
    tasks = list(svc._session_tasks.get(sid, ()))
    assert tasks and not all(t.done() for t in tasks)
    assert (await svc.delete(sid))["error"] == "session_busy"  # a live PM task blocks delete

    out = await svc.cancel(sid)
    assert out["status"] == "cancelled"
    assert out["aborted_tasks"] >= 1

    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(t.cancelled() for t in tasks)
    assert state["plan_completed"] is False
    assert store.get_session(sid).status == "cancelled"


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


async def test_continue_pm_agent_queue_waits_for_current_launch(tmp_path):
    store = _store(tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    launches: list[str] = []

    class FakePM:
        language = "zh"
        max_runs = 1

        async def plan(self, goal, **_kw):
            return PMPlan(agent="codex", model="", effort="", instruction=f"do {goal}")

        async def review(self, goal, *_args, **_kw):
            assert goal != "first", "queued follow-up should stop the old PM loop after reply"
            return PMReview(done=True, summary="done")

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            launches.append(instruction)
            if len(launches) == 1:
                first_started.set()
            return object()

        async def wait(self, handle):
            if len(launches) == 1:
                await release_first.wait()
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    first = await svc.create("first")
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = await svc.create("second", session_id=first["session_id"], continue_mode="queue")
    await asyncio.sleep(0.02)

    assert second["continue_mode"] == "queue"
    assert launches == ["do first"]
    release_first.set()
    await asyncio.gather(*list(svc._tasks), return_exceptions=True)
    assert launches == ["do first", "do second"]


async def test_continue_pm_agent_interrupt_cancels_current_launch(tmp_path):
    store = _store(tmp_path)
    first_started = asyncio.Event()
    interrupted = asyncio.Event()
    launches: list[str] = []

    class FakePM:
        language = "zh"
        max_runs = 1

        async def plan(self, goal, **_kw):
            return PMPlan(agent="codex", model="", effort="", instruction=f"do {goal}")

        async def review(self, *_args, **_kw):
            return PMReview(done=True, summary="done")

    class FakeRunner:
        def handle_for_session(self, session_id):
            return object()

        async def interrupt(self, handle):
            interrupted.set()

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            launches.append(instruction)
            first_started.set()
            return object()

        async def wait(self, handle):
            if len(launches) == 1:
                await asyncio.Event().wait()
            return None

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    first = await svc.create("first")
    await asyncio.wait_for(first_started.wait(), timeout=1)
    old_tasks = list(svc._session_tasks.get(first["session_id"], ()))
    second = await svc.create("second", session_id=first["session_id"], continue_mode="interrupt")
    await asyncio.gather(*old_tasks, return_exceptions=True)
    await asyncio.gather(*list(svc._tasks), return_exceptions=True)

    assert second["continue_mode"] == "interrupt"
    assert interrupted.is_set()
    assert all(t.cancelled() for t in old_tasks)
    assert "do second" in launches


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
            assert "frames_after_checkpoint" in timeline
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


def test_events_to_text_extracts_codex_command_execution_output():
    rows = [
        make_event(
            "agent_output",
            "codex",
            "s1",
            payload={
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "git worktree add E:/repo-wt origin/main",
                    "aggregated_output": "Preparing worktree (detached HEAD)",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
        )
    ]

    text = events_to_text(rows)
    assert "git worktree add E:/repo-wt origin/main" in text
    assert "Preparing worktree" in text


def test_events_to_text_keeps_agent_output_before_pm_stream_noise():
    rows = [
        make_event(
            "agent_output",
            "codex",
            "s1",
            payload={
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "rg WHOLE_COMPUTER_CONTROL",
                    "aggregated_output": "docs/WHOLE_COMPUTER_CONTROL.zh-CN.md",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
        )
    ]
    rows.extend(
        make_event("pm_output", "pm-agent", "s1", payload={"delta": "noise"})
        for _ in range(160)
    )
    rows.append(
        make_event(
            "pm_review",
            "pm-agent",
            "s1",
            payload={"summary": "reviewed after noisy PM streaming"},
        )
    )

    text = events_to_text(rows)
    assert "docs/WHOLE_COMPUTER_CONTROL.zh-CN.md" in text
    assert "reviewed after noisy PM streaming" in text
    assert "noise" not in text


def test_events_to_text_preserves_early_worktree_context_when_truncated():
    rows = [
        make_event(
            "agent_output",
            "codex",
            "s1",
            payload={
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "git worktree add E:/AutoWorkAgent-worktrees/feature origin/main",
                    "aggregated_output": "Preparing worktree (new branch 'docs/whole-computer-control')",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
        )
    ]
    rows.extend(
        make_event(
            "agent_output",
            "codex",
            "s1",
            payload={"type": "item.completed", "item": {"type": "agent_message", "text": f"tail {i} " + ("x" * 120)}},
        )
        for i in range(40)
    )

    text = events_to_text(rows, max_chars=1200)
    assert "git worktree add E:/AutoWorkAgent-worktrees/feature origin/main" in text
    assert "tail 39" in text
    assert "timeline truncated" in text


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


def test_parse_plan_recovers_from_repetition_loop():
    # Regression for #39: the PM fell into a loop and emitted the same plan JSON
    # 47x. The old "first { .. last }" slice spanned all 47 copies, failed to
    # parse, and degraded to an empty fallback plan (instruction/todo lost). The
    # early-cut must take the FIRST complete object instead.
    one = {
        "summary": "use codex",
        "agent": "codex",
        "effort": "high",
        "instruction": "do the thing",
        "todo": ["inspect", "test"],
        "deliberation": ["codex has repo tools"],
        "ready": True,
    }
    blob = (json.dumps(one) + "\n") * 47
    plan = parse_plan(
        blob,
        enabled_agents=["codex"],
        fallback_agent="codex",
        fallback_model="",
        fallback_effort="low",
        fallback_instruction="FALLBACK-should-not-appear",
    )
    assert plan.summary == "use codex"
    assert plan.instruction == "do the thing"  # real plan, not the fallback
    assert plan.todo == ["inspect", "test"]


def test_parse_plan_accepts_tool_arguments_dict():
    # T1.5: a submit_plan tool call hands parse_plan a dict directly (no regex over text). The
    # PMPlan field semantics must be identical to the legacy text path.
    args = {
        "summary": "from tool",
        "agent": "codex",
        "model": "gpt-5.5",
        "effort": "high",
        "instruction": "do it",
        "kind": "direct_reply",
        "reply": "done directly",
        "todo": ["a", "b"],
        "deliberation": ["because evidence"],
        "ready": True,
    }
    plan = parse_plan(
        args,
        enabled_agents=["codex"],
        fallback_agent="codex",
        fallback_model="",
        fallback_effort="low",
        fallback_instruction="FALLBACK-should-not-appear",
    )
    assert plan.summary == "from tool"
    assert plan.instruction == "do it"
    assert plan.todo == ["a", "b"]
    assert plan.model == "gpt-5.5"
    assert plan.effort == "high"
    assert plan.kind == "direct_reply"
    assert plan.reply == "done directly"
    assert plan.deliberation == ["because evidence"]


def test_parse_plan_accepts_enabled_copilot_cli():
    plan = parse_plan(
        {
            "summary": "use copilot",
            "agent": "copilot-cli",
            "model": "",
            "effort": "high",
            "instruction": "do it",
            "todo": ["verify"],
            "ready": True,
        },
        enabled_agents=["copilot-cli"],
        fallback_agent="copilot-cli",
        fallback_model="",
        fallback_effort="high",
        fallback_instruction="fallback",
    )

    assert plan.agent == "copilot-cli"
    assert plan.instruction == "do it"


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
                assert store.get_session(state_key.split(":", 1)[0]).status == "running"
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
            assert "agent_stop" in timeline
            assert "PM follow-up" in timeline
            assert "prior_reviews" in review_state
            return PMReview(done=True, summary="done")

    class FakeHandle:
        session_id = "s"
        attempt_id = ""

    class FakeRunner:
        def __init__(self):
            self.launched = []
            self.sent = []
            self.handle = FakeHandle()
            self.attempts = 0

        def prepare_attempt(self, handle):
            self.attempts += 1
            handle.attempt_id = f"attempt-{self.attempts}"
            return handle.attempt_id

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.handle.session_id = session_id
            self.prepare_attempt(self.handle)
            self.launched.append((agent, instruction, str(workspace), model, effort))
            store.add_event(
                make_event(
                    "stop",
                    agent,
                    session_id,
                    payload={"result": "first", "attempt_id": self.handle.attempt_id},
                )
            )
            return self.handle

        async def wait(self, handle):
            return None

        async def send(self, handle, text, *, attempt_id=""):
            self.sent.append((text, attempt_id))
            store.add_event(
                make_event(
                    "stop",
                    "codex",
                    handle.session_id,
                    payload={"result": text, "attempt_id": attempt_id},
                )
            )

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

    res = await svc.create("raw user task")
    assert res["pm_agent"] is True
    assert res["agent"] == "pm-agent"
    await asyncio.gather(*list(svc._tasks))

    assert pm.plan_goal == "raw user task"
    assert pm.state_keys == [f"{res['session_id']}:{res['task_id']}:pm-review"] * 2
    assert runner.launched == [("codex", "PM planned instruction", str(tmp_path), "gpt-5", "high")]
    assert runner.sent == [("PM follow-up", "attempt-2")]
    events = store.get_events(res["session_id"])
    assert "pm_plan" in [e.type for e in events]
    agent_inputs = [json.loads(e.payload_json) for e in events if e.type == "agent_input"]
    assert [p["attempt_id"] for p in agent_inputs] == ["attempt-1", "attempt-2"]
    reviews = [json.loads(e.payload_json) for e in events if e.type == "pm_review"]
    assert len(reviews) == 2
    assert [x["status"] for x in reviews[0]["todo_status"]] == ["done", "in_progress"]
    assert [x["status"] for x in reviews[1]["todo_status"]] == ["done", "done"]
    assert store.get_session(res["session_id"]).status == "done"
    with store.session() as db:
        assert db.get(Task, res["task_id"]).status == "done"


async def test_pm_review_and_recover_share_pm_tools_with_decision_context(tmp_path):
    cfg = _cfg(workspaces=[WorkspaceCfg(path=str(tmp_path))])

    class FakeCards:
        def __init__(self):
            self.calls = []

        async def ask_question(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "choice": "continue", "label": "Continue"}

    class FakeLLM:
        def __init__(self):
            self.round = 0
            self.tools_seen = []
            self.prompts = []

        async def tool_complete(
            self, messages, *, tools, model="", json_mode=False, tool_choice="auto", on_stream=None
        ):
            self.tools_seen.append([tool["name"] for tool in tools])
            self.prompts.append(messages[-1].content)
            self.round += 1
            if self.round in {1, 3}:
                return LLMToolResponse(
                    text="",
                    tool_calls=[
                        LLMToolCall(
                            id=f"q{self.round}",
                            name="ask_question",
                            arguments={
                                "question": "Need user choice?",
                                "options": [
                                    {"label": "Continue", "value": "continue"},
                                    {"label": "Stop", "value": "stop"},
                                ],
                            },
                        )
                    ],
                )
            if self.round == 2:
                return LLMToolResponse(
                    text=json.dumps({"done": False, "summary": "blocked", "reason": "needs user"}),
                    tool_calls=[],
                )
            return LLMToolResponse(
                text=json.dumps(
                    {
                        "action": "switch_agent",
                        "summary": "switch",
                        "reason": "failed",
                        "agent": "codex",
                        "model": "",
                        "effort": "high",
                        "instruction": "continue with evidence",
                    }
                ),
                tool_calls=[],
            )

    cards = FakeCards()
    pm = PMAgent(
        FakeLLM(),
        language="zh",
        tool_runtime_factory=lambda workspace: PMToolRuntime.from_config(
            cfg, workspace, cards=cards
        ),
    )
    plan = PMPlan(agent="claude-code", model="", effort="high", instruction="do work")

    review = await pm.review(
        "goal",
        plan,
        "timeline",
        run_count=1,
        workspace=str(tmp_path),
        session_id="s1",
        task_id="t1",
    )
    recovery = await pm.recover(
        "goal",
        plan,
        "failure",
        failed_agent="claude-code",
        available_agents=[{"name": "codex"}],
        workspace=str(tmp_path),
        session_id="s1",
        task_id="t1",
    )

    assert review.done is False and review.summary == "blocked"
    assert recovery.action == "switch_agent" and recovery.agent == "codex"
    assert [call["session_id"] for call in cards.calls] == ["s1", "s1"]
    assert all("ask_question" in tools for tools in pm.llm.tools_seen)
    assert all("submit_plan" not in tools for tools in pm.llm.tools_seen)
    assert "submit_plan" not in pm.llm.prompts[0]
    assert "submit_plan" not in pm.llm.prompts[2]


async def test_pm_direct_reply_does_not_launch_agent(tmp_path):
    store = _store(tmp_path)
    bus = EventBus()
    live = bus.subscribe_queue()

    class FakePM:
        language = "zh"
        max_runs = 2

        async def plan(self, goal, **_kw):
            assert goal == "hey，早上好"
            return PMPlan(
                agent="codex",
                model="",
                effort="low",
                instruction="unused",
                kind="direct_reply",
                reply="早上好，需要我帮你处理什么？",
                summary="simple greeting",
                todo=["reply"],
            )

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply must not enter PM review")

    class FakeRunner:
        async def launch(self, *_args, **_kw):
            raise AssertionError("direct reply must not launch an execution agent")

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=bus, runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("hey，早上好")
    await asyncio.gather(*list(svc._tasks))

    rows = store.get_events(res["session_id"])
    assert [e.type for e in rows if e.type in {"pm_plan", "pm_reply", "agent_start"}] == [
        "pm_reply"
    ]
    reply = json.loads(next(e.payload_json for e in rows if e.type == "pm_reply"))
    assert reply["text"] == "早上好，需要我帮你处理什么？"
    assert store.get_session(res["session_id"]).status == "done"
    with store.session() as db:
        assert db.get(Task, res["task_id"]).status == "done"
    live_types = [event.type for event in list(live._queue)]
    assert "pm_reply" in live_types


async def test_pm_terminal_plan_does_not_launch_agent(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        language = "zh"
        max_runs = 2

        async def plan(self, *_args, **_kw):
            return PMPlan(
                agent="codex",
                model="",
                effort="low",
                instruction="认证缺失",
                kind="blocked",
                reply="需要先登录 Claude CLI。",
            )

        async def review(self, *_args, **_kw):
            raise AssertionError("blocked terminal plan must not enter PM review")

    class FakeRunner:
        async def launch(self, *_args, **_kw):
            raise AssertionError("blocked terminal plan must not launch an execution agent")

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("do blocked task")
    await asyncio.gather(*list(svc._tasks))

    rows = store.get_events(res["session_id"])
    assert "agent_start" not in [e.type for e in rows]
    err = json.loads(next(e.payload_json for e in rows if e.type == "error"))
    assert "需要先登录" in err["msg"]
    assert store.get_session(res["session_id"]).status == "failed"


@pytest.mark.parametrize(
    "auth_text",
    [
        "API Error: 401 Invalid authentication credentials",
        "API Error: 403 Forbidden",
    ],
)
async def test_pm_agent_auth_failure_is_fatal_and_skips_review(tmp_path, auth_text):
    store = _store(tmp_path)

    class FakePM:
        language = "zh"
        max_runs = 3

        async def plan(self, *_args, **_kw):
            return PMPlan(
                agent="claude-code",
                model="",
                effort="high",
                instruction="do work",
                summary="use claude",
                todo=["run"],
            )

        async def review(self, *_args, **_kw):
            raise AssertionError("auth fatal exit must not enter PM review")

    class FakeHandle:
        session_id = ""

    class FakeRunner:
        def __init__(self):
            self.launched = 0

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.launched += 1
            handle = FakeHandle()
            handle.session_id = session_id
            store.add_event(
                make_event(
                    "agent_output",
                    agent,
                    session_id,
                    payload={"text": auth_text},
                )
            )
            return handle

        async def wait(self, handle):
            return None

    runner = FakeRunner()
    cfg = _cfg(
        agents={"claude-code": AgentCfg(command="claude", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=FakePM())

    res = await svc.create("run with broken auth")
    await asyncio.gather(*list(svc._tasks))

    rows = store.get_events(res["session_id"])
    assert runner.launched == 1
    assert "pm_review" not in [e.type for e in rows]
    err = json.loads(next(e.payload_json for e in rows if e.type == "error"))
    assert "认证失败" in err["msg"]
    assert store.get_session(res["session_id"]).status == "failed"


async def test_pm_agent_rotates_after_fatal_agent_failure(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        language = "zh"
        max_runs = 2

        def __init__(self):
            self.recoveries = []

        async def plan(self, *_args, **_kw):
            return PMPlan(
                agent="copilot-cli",
                model="gpt-5.5",
                effort="high",
                instruction="do work with copilot",
                summary="use copilot",
                todo=["run"],
            )

        async def recover(self, goal, plan, failure_timeline, **kw):
            self.recoveries.append((goal, plan.agent, failure_timeline, kw["available_agents"]))
            assert kw["failed_agent"] == "copilot-cli"
            assert [row["name"] for row in kw["available_agents"]] == ["codex"]
            assert "401 Invalid authentication credentials" in failure_timeline
            return PMRecovery(
                action="switch_agent",
                agent="codex",
                model="",
                effort="high",
                instruction="Continue with codex after copilot auth failure.",
                summary="改派给 codex",
                todo=["run"],
            )

        async def review(self, goal, plan, timeline, **_kw):
            assert plan.agent == "codex"
            assert "Continue with codex" in plan.instruction
            assert "codex done" in timeline
            return PMReview(done=True, summary="done")

    class FakeHandle:
        session_id = ""

    class FakeRunner:
        def __init__(self):
            self.launched = []

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.launched.append((agent, instruction, model, effort))
            handle = FakeHandle()
            handle.session_id = session_id
            if agent == "copilot-cli":
                store.add_event(
                    make_event(
                        "agent_output",
                        agent,
                        session_id,
                        payload={"text": "API Error: 401 Invalid authentication credentials"},
                    )
                )
            elif agent == "codex":
                store.add_event(make_event("stop", agent, session_id, payload={"result": "codex done"}))
            return handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={
            "codex": AgentCfg(command="codex", enabled=True),
            "copilot-cli": AgentCfg(command="copilot", enabled=True, model="gpt-5.5"),
        },
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    pm = FakePM()
    runner = FakeRunner()
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=pm)

    res = await svc.create("run with fallback")
    await asyncio.gather(*list(svc._tasks))

    assert runner.launched == [
        ("copilot-cli", "do work with copilot", "gpt-5.5", "high"),
        ("codex", "Continue with codex after copilot auth failure.", "", "high"),
    ]
    assert len(pm.recoveries) == 1
    rows = store.get_events(res["session_id"])
    plans = [json.loads(e.payload_json) for e in rows if e.type == "pm_plan"]
    assert [p["agent"] for p in plans] == ["copilot-cli", "codex"]
    assert "error" not in [e.type for e in rows]
    assert store.get_session(res["session_id"]).status == "done"


async def test_pm_agent_stops_after_fatal_failure_when_no_other_agent_available(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        language = "zh"
        max_runs = 2

        async def plan(self, *_args, **_kw):
            return PMPlan(
                agent="copilot-cli",
                model="gpt-5.5",
                effort="high",
                instruction="do work with copilot",
                summary="use copilot",
            )

        async def recover(self, *_args, **_kw):
            raise AssertionError("no alternatives means PM recovery should not be called")

        async def review(self, *_args, **_kw):
            raise AssertionError("fatal local failure must not enter normal review")

    class FakeHandle:
        session_id = ""

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            handle = FakeHandle()
            handle.session_id = session_id
            store.add_event(
                make_event(
                    "agent_output",
                    agent,
                    session_id,
                    payload={"text": "API Error: 401 Invalid authentication credentials"},
                )
            )
            return handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"copilot-cli": AgentCfg(command="copilot", enabled=True, model="gpt-5.5")},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("run without fallback")
    await asyncio.gather(*list(svc._tasks))

    rows = store.get_events(res["session_id"])
    err = json.loads(next(e.payload_json for e in rows if e.type == "error"))
    assert "所有已启用的本地 coding agent 都不可用" in err["msg"]
    assert "认证失败" in err["msg"]
    assert store.get_session(res["session_id"]).status == "failed"


async def test_rate_limit_failure_still_enters_pm_review(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        language = "en"
        max_runs = 1

        async def plan(self, *_args, **_kw):
            return PMPlan(
                agent="claude-code",
                model="",
                effort="high",
                instruction="do work",
                summary="use claude",
                todo=["run"],
            )

        async def review(self, *_args, **_kw):
            return PMReview(done=True, summary="retryable failure was reviewed")

    class FakeHandle:
        session_id = ""

    class FakeRunner:
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            handle = FakeHandle()
            handle.session_id = session_id
            store.add_event(
                make_event(
                    "agent_output",
                    agent,
                    session_id,
                    payload={"text": "HTTP 429 Too Many Requests rate limit"},
                )
            )
            return handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={"claude-code": AgentCfg(command="claude", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("run with rate limit")
    await asyncio.gather(*list(svc._tasks))

    rows = store.get_events(res["session_id"])
    reviews = [json.loads(e.payload_json) for e in rows if e.type == "pm_review"]
    assert len(reviews) == 1
    assert reviews[0]["summary"] == "retryable failure was reviewed"
    assert store.get_session(res["session_id"]).status == "done"


async def test_pm_enabled_explicit_copilot_cli_dispatches_directly(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        language = "zh"
        max_runs = 1

        async def plan(self, *_args, **_kw):
            raise AssertionError("explicit API agent must not be replaced by PM auto-pick")

        async def review(self, *_args, **_kw):
            raise AssertionError("direct explicit dispatch should not enter PM review")

    class FakeHandle:
        session_id = ""

    class FakeRunner:
        def __init__(self):
            self.launched = []

        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.launched.append((agent, instruction, str(workspace), model, effort))
            handle = FakeHandle()
            handle.session_id = session_id
            store.add_event(make_event("agent_start", agent, session_id, payload={"pid": 123}))
            store.add_event(make_event("agent_output", agent, session_id, payload={"text": "hi"}))
            store.add_event(make_event("stop", agent, session_id, payload={"result": "done"}))
            return handle

        async def wait(self, handle):
            return None

    cfg = _cfg(
        agents={
            "codex": AgentCfg(command="codex", enabled=True),
            "copilot-cli": AgentCfg(
                command="copilot", enabled=True, model="gpt-copilot", effort="high"
            ),
        },
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    runner = FakeRunner()
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=FakePM())

    res = await svc.create("run with copilot", agent="copilot-cli")
    await asyncio.gather(*list(svc._tasks))

    assert res["ok"] is True
    assert res["pm_agent"] is True
    assert res["agent"] == "copilot-cli"
    assert res["direct_agents"] == ["copilot-cli"]
    assert store.get_session(res["session_id"]).agent_type == "copilot-cli"
    assert [call[0] for call in runner.launched] == ["copilot-cli"]
    assert runner.launched[0][3:] == ("gpt-copilot", "high")
    rows = store.get_events(res["session_id"])
    dispatch = json.loads(next(e.payload_json for e in rows if e.type == "dispatch"))
    assert dispatch["agent"] == "copilot-cli"
    assert dispatch["direct_agents"] == ["copilot-cli"]
    assert [e.source for e in rows if e.type in {"agent_start", "agent_output", "stop"}] == [
        "copilot-cli",
        "copilot-cli",
        "copilot-cli",
    ]


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
            if on_stream is not None:
                await on_stream({"kind": "output", "delta": "tool-loop-stream", "event_type": "chunk"})
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
    pm_streams = [json.loads(e.payload_json) for e in rows if e.type == "pm_output"]
    assert any(p.get("delta") == "tool-loop-stream" for p in pm_streams)


async def test_pm_tool_event_sink_persists_pm_validation_error(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    store.add_task(Task(id="t1", session_id="s1", instruction="plan"))
    svc = DispatchService(_cfg(workspaces=[WorkspaceCfg(path=str(tmp_path))]), store, bus=EventBus())
    sink = svc._pm_tool_event_sink("s1", "t1")

    await sink(
        "pm_validation_error",
        {
            "error": "final_plan_missing_reply",
            "round": 1,
            "arguments": {"kind": "direct_reply", "reply": ""},
        },
    )

    rows = store.get_events("s1")
    validation_events = [event for event in rows if event.type == "pm_validation_error"]
    assert len(validation_events) == 1
    assert validation_events[0].source == "pm-agent"
    assert validation_events[0].task_id == "t1"
    payload = json.loads(validation_events[0].payload_json)
    assert payload["error"] == "final_plan_missing_reply"

    frames = ContextManager(store).materialize_session("s1")
    validation_frames = [frame for frame in frames if frame.type == "previous_validation_error"]
    assert len(validation_frames) == 1
    frame_payload = json.loads(validation_frames[0].payload_json)
    assert frame_payload["payload"]["error"] == "final_plan_missing_reply"


async def test_pm_tool_event_sink_still_ignores_unknown_events(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    svc = DispatchService(_cfg(workspaces=[WorkspaceCfg(path=str(tmp_path))]), store, bus=EventBus())
    sink = svc._pm_tool_event_sink("s1", "t1")

    await sink("unexpected_event", {"x": 1})

    assert store.get_events("s1") == []


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


async def test_pm_agent_shortcuts_simple_reply_without_tool_loop(tmp_path):
    class NoLLM:
        async def complete(self, *_args, **_kwargs):
            raise AssertionError("simple reply planning should not call the LLM")

    def no_runtime(_workspace):
        raise AssertionError("simple reply planning should not create the PM tool runtime")

    pm = PMAgent(NoLLM(), language="zh", tool_runtime_factory=no_runtime)
    plan = await pm.plan(
        "请仅用一句中文确认收到本消息",
        workspace=str(tmp_path),
        available_agents=[{"name": "codex", "model": "", "effort": "", "full_access": True}],
        requested_agent="codex",
        pm_model="gpt-5.5",
        requested_effort="high",
        fallback_instruction="fallback",
    )

    assert plan.agent == "codex"
    assert plan.kind == "direct_reply"
    assert plan.reply == "收到。"
    assert plan.todo == ["直接按要求回复用户"]
    assert plan.summary == "简单回复请求，已跳过 PM 工具规划。"
    assert "不要查看文件" in plan.instruction


async def test_pm_agent_simple_reply_only_copilot_does_not_fallback_to_claude(tmp_path):
    class NoLLM:
        async def complete(self, *_args, **_kwargs):
            raise AssertionError("simple reply planning should not call the LLM")

    pm = PMAgent(NoLLM(), language="zh")
    plan = await pm.plan(
        "请仅用一句中文确认收到本消息",
        workspace=str(tmp_path),
        available_agents=[{"name": "copilot-cli", "model": "", "effort": "high"}],
        requested_agent="",
        pm_model="",
        requested_effort="high",
        fallback_instruction="fallback",
    )

    assert plan.agent == "copilot-cli"
    assert plan.kind == "direct_reply"


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
    assert _explicit_agent_targets("use copilot to report status", ["copilot-cli"]) == [
        "copilot-cli"
    ]


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


async def test_pm_watchdog_failure_marks_session_stalled_with_reason(tmp_path):
    store = _store(tmp_path)

    class FakePM:
        async def plan(self, *_a, **_kw):
            raise LLMStalledError("wall_clock_timeout", "responses ws did not finish")

    class FakeRunner:
        async def launch(self, *_a, **_kw):
            raise AssertionError("stalled PM plan must not launch an agent")

    cfg = _cfg(
        agents={"codex": AgentCfg(command="codex", enabled=True)},
        workspaces=[WorkspaceCfg(path=str(tmp_path))],
    )
    svc = DispatchService(cfg, store, bus=EventBus(), runner=FakeRunner(), pm_agent=FakePM())

    res = await svc.create("do x")
    await asyncio.gather(*list(svc._tasks))

    assert store.get_session(res["session_id"]).status == "stalled"
    errors = [json.loads(e.payload_json) for e in store.get_events(res["session_id"]) if e.type == "error"]
    assert errors[-1]["status"] == "stalled"
    assert errors[-1]["reason"] == "wall_clock_timeout"
    assert "LLMStalledError" in errors[-1]["msg"]


def test_pm_provider_failures_are_localized_without_losing_safe_detail():
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    auth = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=request,
        response=httpx.Response(401, request=request),
    )
    upstream = httpx.HTTPStatusError(
        "502 Bad Gateway",
        request=request,
        response=httpx.Response(502, request=request),
    )

    zh_msg, zh_detail = _pm_failure_text(auth, "zh")
    en_msg, en_detail = _pm_failure_text(upstream, "en")

    assert zh_msg == "Provider 认证失败，请检查 API Key 和认证配置。 (HTTP 401)"
    assert en_msg.startswith("The provider or upstream model is unavailable.")
    assert en_msg.endswith("(HTTP 502)")
    assert zh_detail == "HTTPStatusError: 401 Unauthorized"
    assert en_detail == "HTTPStatusError: 502 Bad Gateway"
    assert "API Key" not in zh_detail


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


async def test_existing_session_allows_recorded_worktree_outside_allowlist(tmp_path):
    store = _store(tmp_path)
    main = tmp_path / "main"
    worktree = tmp_path / "pm-worktree"
    main.mkdir()
    worktree.mkdir()
    cfg = _cfg(workspaces=[WorkspaceCfg(path=str(main))])
    svc = DispatchService(cfg, store)

    first = await svc.create("do x", workspace=str(main))
    store.update_session(first["session_id"], workspace=str(worktree))
    follow = await svc.create(
        "do y",
        workspace=str(worktree),
        session_id=first["session_id"],
    )

    assert follow["ok"] is True
    assert follow["continued"] is True
    assert follow["workspace"] == str(worktree)


async def test_existing_session_rejects_missing_recorded_worktree_outside_allowlist(tmp_path):
    store = _store(tmp_path)
    main = tmp_path / "main"
    missing = tmp_path / "missing-worktree"
    main.mkdir()
    cfg = _cfg(workspaces=[WorkspaceCfg(path=str(main))])
    svc = DispatchService(cfg, store)

    first = await svc.create("do x", workspace=str(main))
    store.update_session(first["session_id"], workspace=str(missing))
    follow = await svc.create(
        "do y",
        workspace=str(missing),
        session_id=first["session_id"],
    )

    assert follow["error"] == "workspace_not_allowed"


def test_pm_review_diff_evidence_uses_worktree_manager_summary(tmp_path):
    store = _store(tmp_path)
    main = tmp_path / "main"
    worktree = tmp_path / "pm-worktree"
    main.mkdir()
    worktree.mkdir()
    store.add_session(Session(id="s1", goal="g", workspace=str(worktree), main_workspace=str(main)))

    class FakeWorktreeManager:
        def __init__(self):
            self.context = None

        def diff(self, context, *, max_patch_chars: int = 0, include_patch: bool = False):
            self.context = context
            return {
                "ok": True,
                "clean": False,
                "base_ref": "main",
                "base_sha": "base123",
                "compare_to": "base123",
                "head_sha": "head456",
                "branch": "foreman/s1/t9",
                "lease_id": "lease-1",
                "lease_status": "active",
                "files_changed": 1,
                "additions": 2,
                "deletions": 1,
                "patch_truncated": True,
                "patch_artifact": str(worktree / ".foreman" / "tool-logs" / "diff.patch"),
                "changed_files": [{"path": "a.txt", "status": "modified"}],
            }

    manager = FakeWorktreeManager()
    svc = DispatchService(_cfg(workspaces=[WorkspaceCfg(path=str(main))]), store, worktree_manager=manager)

    evidence = svc._worktree_diff_review_evidence("s1", "t1", str(worktree))

    assert "# Worktree diff evidence" in evidence
    assert '"source": "worktree_diff"' in evidence
    assert '"compare_to": "base123"' in evidence
    assert '"patch_truncated": true' in evidence
    assert '"path": "a.txt"' in evidence
    assert manager.context["session_id"] == "s1"
    assert manager.context["task_id"] == "t1"
    assert manager.context["main_workspace"] == str(main)


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
    store.add_session(Session(id="s1", goal="older", status="running", plan="ordinary PM plan",
                              agent_type="claude-code", created_at="2026-01-01T00:00:00Z"))
    store.add_session(Session(id="s2", goal="newer", status="idle", plan="short context",
                              agent_type="codex", created_at="2026-02-01T00:00:00Z"))
    store.add_event(make_event("agent_output", "claude-code", "s1", payload={"t": "a"}))
    store.add_event(make_event("stop", "claude-code", "s1", payload={"r": "done"}))
    store.add_event(make_event("agent_output", "codex", "s2", payload={"t": "b"}))
    store.add_event(make_event(
        "context_compact", "pm-agent", "s2",
        payload={"summary": "short context", "after_tokens": 3},
    ))
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
    assert s1["context_compacted"] is False
    assert s1["context_tokens"] == 0
    s2 = next(d for d in ov if d["id"] == "s2")
    assert s2["events"] == 2 and s2["open_cards"] == 0 and s2["pending_approvals"] == 0
    assert s2["context_compacted"] is True
    assert s2["context_tokens"] == 4


def test_overview_no_store_is_empty():
    assert DispatchService(_cfg(), None).overview() == []
