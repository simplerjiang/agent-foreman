from __future__ import annotations

import json

from foreman.client.core.context_v2 import ActiveContext
from foreman.client.core.dispatch_service import (
    DispatchService,
    _advance_reviewed_event_id_from_active_context,
)
from foreman.client.core.pm_agent import PMPlan, PMReview
from foreman.client.store import Store
from foreman.client.store.models import Event, Session, Task
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import make_event


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "dispatch-context.db"))
    store.init()
    return store


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    return cfg


def _seed(store: Store, tmp_path):
    store.add_session(Session(id="s1", goal="goal", workspace=str(tmp_path), plan="LEGACY_CONTEXT"))
    store.add_task(Task(id="t1", session_id="s1", instruction="goal"))


class _FakeContextManager:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []
        self.by_purpose = {
            "pm_plan": ActiveContext(session_id="s1", purpose="pm_plan", rendered_text="ACTIVE_CONTEXT_PM_PLAN"),
            "pm_review": ActiveContext(
                session_id="s1",
                purpose="pm_review",
                rendered_text="ACTIVE_CONTEXT_PM_REVIEW",
                source_cursor={"end": {"event_id": "e2"}},
            ),
        }

    def build_active_context(self, session_id: str, *, purpose: str, window_tokens: int):
        self.calls.append((session_id, purpose, window_tokens))
        if self.fail:
            raise RuntimeError("restore failed")
        return self.by_purpose[purpose]


class _Handle:
    session_id = "s1"


class _Runner:
    def __init__(self, store: Store):
        self.store = store
        self.handle = _Handle()
        self.sent: list[str] = []

    async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
        self.handle.session_id = session_id
        self.store.add_event(make_event("stop", agent, session_id, payload={"result": "first"}))
        return self.handle

    async def wait(self, handle):
        return None

    async def send(self, handle, text):
        self.sent.append(text)


async def test_pm_plan_uses_context_manager(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    cm = _FakeContextManager()

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.context = ""

        async def plan(self, goal, **kw):
            self.context = kw["context"]
            return PMPlan(
                agent="codex",
                model="",
                effort="low",
                instruction="direct",
                kind="direct_reply",
                reply="done",
            )

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply should not review")

    pm = PM()
    svc = DispatchService(_cfg(tmp_path), store, runner=_Runner(store), pm_agent=pm, context_manager=cm)

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert pm.context == "ACTIVE_CONTEXT_PM_PLAN"
    assert cm.calls[0][1] == "pm_plan"


async def test_pm_plan_falls_back_to_legacy_context_on_context_manager_failure(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.context = ""

        async def plan(self, goal, **kw):
            self.context = kw["context"]
            return PMPlan(agent="codex", model="", effort="low", instruction="direct", kind="direct_reply", reply="done")

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply should not review")

    pm = PM()
    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=_Runner(store),
        pm_agent=pm,
        context_manager=_FakeContextManager(fail=True),
    )

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert pm.context == "LEGACY_CONTEXT"
    notifications = [json.loads(row.payload_json) for row in store.get_events("s1") if row.type == "notification"]
    assert any(item["kind"] == "context_restore_failed" for item in notifications)


async def test_pm_plan_passes_active_context_when_supported(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    cm = _FakeContextManager()

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.active_context = None

        async def plan(self, goal, *, active_context=None, **kw):
            self.active_context = active_context
            return PMPlan(agent="codex", model="", effort="low", instruction="direct", kind="direct_reply", reply="done")

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply should not review")

    pm = PM()
    svc = DispatchService(_cfg(tmp_path), store, runner=_Runner(store), pm_agent=pm, context_manager=cm)

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert pm.active_context is cm.by_purpose["pm_plan"]


async def test_pm_plan_does_not_pass_active_context_when_not_supported(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)

    class PM:
        language = "en"
        max_runs = 1

        async def plan(
            self,
            goal,
            *,
            workspace,
            available_agents,
            requested_agent,
            pm_model,
            requested_effort,
            fallback_instruction,
            context="",
        ):
            return PMPlan(agent="codex", model="", effort="low", instruction="direct", kind="direct_reply", reply="done")

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply should not review")

    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=_Runner(store),
        pm_agent=PM(),
        context_manager=_FakeContextManager(),
    )

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")


async def test_pm_review_uses_context_manager(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    cm = _FakeContextManager()

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.timeline = ""
            self.context = ""
            self.active_context = None

        async def plan(self, goal, **kw):
            return PMPlan(agent="codex", model="", effort="low", instruction="run", todo=["check"])

        async def review(self, goal, plan, timeline, *, active_context=None, context="", **kw):
            self.timeline = timeline
            self.context = context
            self.active_context = active_context
            return PMReview(done=True, summary="done")

    pm = PM()
    svc = DispatchService(_cfg(tmp_path), store, runner=_Runner(store), pm_agent=pm, context_manager=cm)

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert pm.timeline == "ACTIVE_CONTEXT_PM_REVIEW"
    assert pm.context == "ACTIVE_CONTEXT_PM_REVIEW"
    assert pm.active_context is cm.by_purpose["pm_review"]
    assert [call[1] for call in cm.calls] == ["pm_plan", "pm_review"]


async def test_pm_review_falls_back_to_legacy_timeline_on_context_failure(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.timeline = ""

        async def plan(self, goal, **kw):
            return PMPlan(agent="codex", model="", effort="low", instruction="run", todo=["check"])

        async def review(self, goal, plan, timeline, **kw):
            self.timeline = timeline
            return PMReview(done=True, summary="done")

    pm = PM()
    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=_Runner(store),
        pm_agent=pm,
        context_manager=_FakeContextManager(fail=True),
    )

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert "first" in pm.timeline


def test_reviewed_cursor_advances_from_checkpoint_source_cursor():
    rows = [
        Event(id="e1", session_id="s1", type="dispatch", source="test"),
        Event(id="e2", session_id="s1", type="pm_plan", source="test"),
        Event(id="e3", session_id="s1", type="pm_review", source="test"),
    ]
    active = ActiveContext(source_cursor={"end": {"event_id": "e2"}})

    assert _advance_reviewed_event_id_from_active_context(rows, "e1", active) == "e2"
    assert _advance_reviewed_event_id_from_active_context(rows, "e3", active) == "e3"
    missing = ActiveContext(source_cursor={"end": {"event_id": "missing"}})
    assert _advance_reviewed_event_id_from_active_context(rows, "e1", missing) == "e1"
