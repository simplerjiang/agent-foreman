from __future__ import annotations

import json

from foreman.client.core.context_v2 import ActiveContext, ContextCompactError, ContextManager
from foreman.client.core.dispatch_service import (
    DispatchService,
    _advance_reviewed_event_id_from_active_context,
    _review_timeline_from_active_context,
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
    def __init__(self, *, fail: bool = False, review_context: ActiveContext | None = None):
        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []
        self.by_purpose = {
            "pm_plan": ActiveContext(session_id="s1", purpose="pm_plan", rendered_text="ACTIVE_CONTEXT_PM_PLAN"),
            "pm_review": review_context or ActiveContext(
                session_id="s1",
                purpose="pm_review",
                rendered_text="ACTIVE_CONTEXT_PM_REVIEW",
                frames_after_checkpoint=[
                    {
                        "type": "agent_stop",
                        "lane": 6,
                        "payload": {"summary": "review evidence"},
                        "source_refs": [],
                    }
                ],
            ),
        }

    def build_active_context(self, session_id: str, *, purpose: str, window_tokens: int):
        self.calls.append((session_id, purpose, window_tokens))
        if self.fail:
            raise RuntimeError("restore failed")
        return self.by_purpose[purpose]


class _MaybeContextManager:
    def __init__(
        self,
        *,
        store: Store | None = None,
        fail: bool = False,
        hard: bool = False,
        raise_on_fail: bool | None = None,
    ):
        self.store = store
        self.fail = fail
        self.hard = hard
        self.raise_on_fail = hard if raise_on_fail is None else raise_on_fail
        self.calls: list[dict] = []
        self.active = ActiveContext(session_id="s1", purpose="pm_plan", rendered_text="BEFORE_COMPACT")

    async def maybe_compact(self, session_id: str, *, reason: str, purpose: str, window_tokens: int, run_count: int = 0):
        self.calls.append(
            {
                "session_id": session_id,
                "reason": reason,
                "purpose": purpose,
                "window_tokens": window_tokens,
                "run_count": run_count,
            }
        )
        if self.fail:
            if self.store is not None:
                self.store.add_event(
                    make_event(
                        "context_compact",
                        "pm-agent",
                        session_id,
                        payload={
                            "status": "failed",
                            "schema_version": 2,
                            "hard": self.hard,
                            "reason": reason,
                        },
                    )
                )
            if self.raise_on_fail:
                raise ContextCompactError("hard compact failed" if self.hard else "soft compact failed")
            return None
        self.active = ActiveContext(
            session_id=session_id,
            purpose=purpose,
            rendered_text=f"AFTER_COMPACT_{purpose}",
            source_cursor={"end": {"event_id": "e2"}},
            frames_after_checkpoint=[
                {
                    "event_id": "e3",
                    "type": "agent_stop",
                    "lane": 6,
                    "payload": {"summary": "new review evidence"},
                    "source_refs": ["event:e3"],
                }
            ],
        )
        return object()

    def build_active_context(self, session_id: str, *, purpose: str, window_tokens: int):
        self.active.purpose = purpose
        return self.active


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


async def test_direct_answer_active_context_does_not_dispatch_agent(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="what is pytest?", workspace=str(tmp_path), plan="prior context"))
    store.add_task(Task(id="t1", session_id="s1", instruction="what is pytest?"))
    active = ActiveContext(
        session_id="s1",
        purpose="pm_plan",
        envelope={"task": {"user_intent_type": "direct_answer"}},
        rendered_text='{"task": {"user_intent_type": "direct_answer"}, "context": {}}',
    )

    class Runner(_Runner):
        async def launch(self, *args, **kwargs):
            raise AssertionError("direct answer should not launch a coding agent")

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.context = ""
            self.active_context = None

        async def plan(self, goal, *, active_context=None, **kw):
            self.context = kw["context"]
            self.active_context = active_context
            return PMPlan(
                agent="codex",
                model="",
                effort="low",
                instruction="direct reply only",
                kind="direct_reply",
                reply="pytest is a Python test runner.",
            )

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply should not review")

    pm = PM()
    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=Runner(store),
        pm_agent=pm,
        context_manager=_FakeContextManager(review_context=active),
    )
    svc.context_manager.by_purpose["pm_plan"] = active

    await svc._pm_launch("s1", "t1", "what is pytest?", str(tmp_path), "codex", "", "low")

    assert pm.context == active.rendered_text
    assert pm.active_context is active
    assert store.get_session("s1").status == "done"
    event_types = [event.type for event in store.get_events("s1")]
    assert "pm_reply" in event_types
    assert "pm_plan" not in event_types
    assert "agent_start" not in event_types
    assert "agent_input" not in event_types


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


async def test_pm_plan_invokes_maybe_compact_and_uses_rebuilt_context(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    cm = _MaybeContextManager()

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
                instruction="direct reply only",
                kind="direct_reply",
                reply="done",
            )

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply should not review")

    pm = PM()
    svc = DispatchService(_cfg(tmp_path), store, runner=_Runner(store), pm_agent=pm, context_manager=cm)

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert cm.calls[0]["reason"] == "pre_turn"
    assert cm.calls[0]["purpose"] == "pm_plan"
    assert pm.context == "AFTER_COMPACT_pm_plan"


async def test_pm_plan_hard_compact_failure_blocks_plan_call(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)

    class PM:
        language = "en"
        max_runs = 1

        async def plan(self, *_args, **_kwargs):
            raise AssertionError("plan must not be called")

        async def review(self, *_args, **_kw):
            raise AssertionError("review must not be called")

    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=_Runner(store),
        pm_agent=PM(),
        context_manager=_MaybeContextManager(store=store, fail=True, hard=True),
    )

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert store.get_session("s1").status == "failed"
    assert any(event.type == "error" for event in store.get_events("s1"))
    failed = [json.loads(event.payload_json) for event in store.get_events("s1") if event.type == "context_compact"]
    assert failed[-1]["status"] == "failed"
    assert failed[-1]["hard"] is True


async def test_pm_plan_soft_compact_failure_allows_plan_call(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.called = False

        async def plan(self, goal, **kw):
            self.called = True
            return PMPlan(
                agent="codex",
                model="",
                effort="low",
                instruction="direct reply only",
                kind="direct_reply",
                reply="done",
            )

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply should not review")

    pm = PM()
    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=_Runner(store),
        pm_agent=pm,
        context_manager=_MaybeContextManager(store=store, fail=True, hard=False, raise_on_fail=False),
    )

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert pm.called is True
    failed = [json.loads(event.payload_json) for event in store.get_events("s1") if event.type == "context_compact"]
    assert failed[-1]["status"] == "failed"
    assert failed[-1]["hard"] is False


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
    matching = [item for item in notifications if item["kind"] == "context_restore_failed"]
    assert len(matching) == 1
    assert matching[0]["purpose"] == "pm_plan"
    assert matching[0]["fallback"] == "legacy_session_context"


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


async def test_pm_review_uses_active_context_as_context_not_timeline(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    cm = _FakeContextManager(
        review_context=ActiveContext(
            session_id="s1",
            purpose="pm_review",
            rendered_text='{"output_contract": {}, "validator_rules": {}, "context": "FULL ACTIVE CONTEXT"}',
            frames_after_checkpoint=[
                {
                    "event_id": "",
                    "type": "command_result",
                    "lane": 6,
                    "agent_id": "codex",
                    "payload": {
                        "command": "pytest",
                        "exit_code": 0,
                        "important_lines": ["1 passed"],
                    },
                    "source_refs": [],
                }
            ],
        )
    )

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

    assert "FULL ACTIVE CONTEXT" in pm.context
    assert "command_result" in pm.timeline
    assert "pytest" in pm.timeline
    assert pm.timeline != pm.context
    assert "output_contract" not in pm.timeline
    assert "validator_rules" not in pm.timeline
    assert pm.active_context is cm.by_purpose["pm_review"]
    assert [call[1] for call in cm.calls] == ["pm_plan", "pm_review"]


async def test_pm_review_invokes_maybe_compact_and_uses_rebuilt_context(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)
    cm = _MaybeContextManager()

    class Runner(_Runner):
        async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
            self.handle.session_id = session_id
            with self.store.session() as session:
                session.add(Event(id="e2", session_id=session_id, task_id="t1", type="pm_plan", source="pm-agent", ts="2026-07-01T00:00:00Z"))
                session.add(Event(id="e3", session_id=session_id, task_id="t1", type="stop", source=agent, payload_json=json.dumps({"result": "new review evidence"}), ts="2026-07-01T00:00:01Z"))
                session.commit()
            return self.handle

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.timeline = ""
            self.context = ""

        async def plan(self, goal, **kw):
            return PMPlan(agent="codex", model="", effort="low", instruction="run", todo=["check"])

        async def review(self, goal, plan, timeline, *, context="", **kw):
            self.timeline = timeline
            self.context = context
            return PMReview(done=True, summary="done")

    pm = PM()
    svc = DispatchService(_cfg(tmp_path), store, runner=Runner(store), pm_agent=pm, context_manager=cm)

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert [call["purpose"] for call in cm.calls] == ["pm_plan", "pm_review"]
    assert cm.calls[-1]["reason"] == "pre_review"
    assert cm.calls[-1]["run_count"] == 1
    assert pm.context == "AFTER_COMPACT_pm_review"
    assert "new review evidence" in pm.timeline
    assert "event:e2" not in pm.timeline


async def test_pm_review_hard_compact_failure_blocks_review_call(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)

    class PM:
        language = "en"
        max_runs = 1

        async def plan(self, goal, **kw):
            return PMPlan(agent="codex", model="", effort="low", instruction="run", todo=["check"])

        async def review(self, *_args, **_kw):
            raise AssertionError("review must not be called")

    class CM(_MaybeContextManager):
        async def maybe_compact(self, session_id: str, *, reason: str, purpose: str, window_tokens: int, run_count: int = 0):
            if purpose == "pm_plan":
                self.calls.append({"purpose": purpose, "reason": reason, "run_count": run_count})
                return None
            return await super().maybe_compact(
                session_id,
                reason=reason,
                purpose=purpose,
                window_tokens=window_tokens,
                run_count=run_count,
            )

    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=_Runner(store),
        pm_agent=PM(),
        context_manager=CM(store=store, fail=True, hard=True),
    )

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert store.get_session("s1").status == "failed"
    failed = [json.loads(event.payload_json) for event in store.get_events("s1") if event.type == "context_compact"]
    assert failed[-1]["hard"] is True


async def test_pm_review_no_new_frames_returns_no_new_output(tmp_path):
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
        context_manager=_FakeContextManager(
            review_context=ActiveContext(
                session_id="s1",
                purpose="pm_review",
                rendered_text="FULL ACTIVE CONTEXT",
                frames_after_checkpoint=[],
            )
        ),
    )

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert pm.timeline == "(no new agent output captured)"
    assert pm.timeline != "FULL ACTIVE CONTEXT"


async def test_pm_review_lane7_noise_returns_no_new_output(tmp_path):
    store = _store(tmp_path)
    _seed(store, tmp_path)

    class PM:
        language = "en"
        max_runs = 1

        def __init__(self):
            self.timeline = ""
            self.context = ""

        async def plan(self, goal, **kw):
            return PMPlan(agent="codex", model="", effort="low", instruction="run", todo=["check"])

        async def review(self, goal, plan, timeline, *, context="", **kw):
            self.timeline = timeline
            self.context = context
            return PMReview(done=True, summary="done")

    pm = PM()
    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=_Runner(store),
        pm_agent=pm,
        context_manager=_FakeContextManager(
            review_context=ActiveContext(
                session_id="s1",
                purpose="pm_review",
                rendered_text="FULL ACTIVE CONTEXT",
                frames_after_checkpoint=[
                    {"event_id": "", "type": "pm_reasoning", "lane": 7, "payload": {"text": "noise"}}
                ],
            )
        ),
    )

    await svc._pm_launch("s1", "t1", "goal", str(tmp_path), "codex", "", "low")

    assert pm.context == "FULL ACTIVE CONTEXT"
    assert pm.timeline == "(no new agent output captured)"


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


def test_review_cursor_with_same_timestamp_text_event_ids():
    rows = [
        Event(id="evt_a", session_id="s1", type="dispatch", source="test", ts="2026-07-01T00:00:00Z"),
        Event(id="evt_b", session_id="s1", type="pm_plan", source="test", ts="2026-07-01T00:00:00Z"),
        Event(id="evt_c", session_id="s1", type="stop", source="test", ts="2026-07-01T00:00:01Z"),
    ]
    active = ActiveContext(
        source_cursor={"end": {"event_ts": "2026-07-01T00:00:00Z", "event_id": "evt_b"}},
        frames_after_checkpoint=[
            {
                "event_id": "evt_a",
                "type": "agent_stop",
                "lane": 6,
                "payload": {"summary": "covered a"},
                "source_refs": ["event:evt_a"],
            },
            {
                "event_id": "evt_b",
                "type": "agent_stop",
                "lane": 6,
                "payload": {"summary": "covered b"},
                "source_refs": ["event:evt_b"],
            },
            {
                "event_id": "evt_c",
                "type": "command_result",
                "lane": 6,
                "payload": {"command": "pytest", "exit_code": 0},
                "source_refs": ["event:evt_c"],
            },
        ],
    )

    reviewed = _advance_reviewed_event_id_from_active_context(rows, "evt_a", active)
    timeline = _review_timeline_from_active_context(active, rows, reviewed)

    assert reviewed == "evt_b"
    assert "evt_c" in timeline
    assert "pytest" in timeline
    assert "covered a" not in timeline
    assert "covered b" not in timeline


def test_pm_review_checkpoint_cursor_does_not_duplicate_covered_frames():
    rows = [
        Event(id="e1", session_id="s1", type="dispatch", source="test"),
        Event(id="e2", session_id="s1", type="pm_plan", source="test"),
        Event(id="e3", session_id="s1", type="stop", source="codex"),
    ]
    active = ActiveContext(
        source_cursor={"end": {"event_id": "e2"}},
        frames_after_checkpoint=[
            {
                "event_id": "e1",
                "type": "agent_stop",
                "lane": 6,
                "payload": {"summary": "old dispatch"},
                "source_refs": ["event:e1"],
            },
            {
                "event_id": "e2",
                "type": "command_result",
                "lane": 6,
                "payload": {"command": "covered"},
                "source_refs": ["event:e2"],
            },
            {
                "event_id": "e3",
                "type": "command_result",
                "lane": 6,
                "payload": {"command": "pytest", "exit_code": 0},
                "source_refs": ["event:e3"],
            },
        ],
    )

    reviewed = _advance_reviewed_event_id_from_active_context(rows, "e1", active)
    timeline = _review_timeline_from_active_context(active, rows, reviewed)

    assert reviewed == "e2"
    assert "pytest" in timeline
    assert "e3" in timeline
    assert "old dispatch" not in timeline
    assert "covered" not in timeline


async def test_previous_validation_error_is_visible_in_next_pm_plan_context(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="answer directly", workspace=str(tmp_path)))
    store.add_task(Task(id="t1", session_id="s1", instruction="answer directly"))
    store.add_event(
        make_event(
            "pm_validation_error",
            "pm-agent",
            "s1",
            task_id="t1",
            payload={
                "error": "final_plan_missing_reply",
                "round": 1,
                "arguments": {
                    "kind": "direct_reply",
                    "instruction": "direct reply only",
                    "reply": "",
                },
            },
        )
    )

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
                instruction="direct reply only",
                kind="direct_reply",
                reply="fixed",
            )

        async def review(self, *_args, **_kw):
            raise AssertionError("direct reply should not review")

    pm = PM()
    svc = DispatchService(
        _cfg(tmp_path),
        store,
        runner=_Runner(store),
        pm_agent=pm,
        context_manager=ContextManager(store),
    )

    await svc._pm_launch("s1", "t1", "answer directly", str(tmp_path), "codex", "", "low")

    assert "previous_validation_error" in pm.context
    assert "final_plan_missing_reply" in pm.context
    assert "frames_after_checkpoint" in pm.context
