"""Tests for QA-standard-driven review (T5.4, DESIGN §11.2 step 3 / §5.3 / §88-89).

The hybrid engine (T5.2) parks a step in ``qa``; ``WorkflowQAReviewer`` closes that loop: it pulls
the step's resolved QA rubric + a diff, runs YOUR LLM Reviewer, maps the verdict to pass/fail
(only ``approve`` advances — §6.7 从严默认), feeds it back to the engine, and emits a ``review`` event.

Covered over a REAL client Store (tmp_path sqlite) + the real WorkflowEngine:
  - approve → step advances (qa_passed=True); request_changes / escalate → step fails (no advance);
  - the QA rubric body + goal + diff actually reach the Reviewer;
  - fail-closed guards: no_run / not_in_qa / no_qa_rubric;
  - the ``review`` event is persisted (metadata only) and published;
  - end-to-end through the real Reviewer via a mock LLM transport (no network, no tokens).
"""

from __future__ import annotations

import json

import httpx

from foreman.client.core.cards import CardService
from foreman.client.core.qa_review import WorkflowQAReviewer
from foreman.client.core.reviewer import APPROVE, ESCALATE, REQUEST_CHANGES, ReviewResult, Reviewer
from foreman.client.core.workflow_engine import FAILED, PENDING, QA, WorkflowEngine
from foreman.client.store import Store
from foreman.client.store.models import Definition, Session
from foreman.shared.config import Config
from foreman.shared.llm import LLMClient

# A two-step workflow whose first step carries a QA rubric (the §585 YAML body format).
WF_YAML = """
name: add-feature
steps:
  - name: write-tests
    instruction: Write failing tests first.
    qa: covers-happy-path
  - name: implement
    instruction: Make the tests pass.
"""


class _RecBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


class _FakeReviewer:
    """Stand-in for the LLM Reviewer: returns a canned result and records what it was asked."""

    def __init__(self, result: ReviewResult):
        self.result = result
        self.calls: list[dict] = []

    async def review(self, goal, diff, *, context="", qa_standard="", **kw):
        self.calls.append({"goal": goal, "diff": diff, "context": context, "qa_standard": qa_standard})
        return self.result


def _store(tmp_path) -> Store:
    st = Store(str(tmp_path / "t.db"))
    st.init()
    return st


def _seed(st: Store) -> None:
    st.add_session(Session(id="s1", goal="add feature", workspace=""))
    wf = st.add_definition(
        Definition(id="wf1", kind="workflow", name="add-feature", version=1, body=WF_YAML)
    )
    st.set_definition_active(wf.id)
    rubric = st.add_definition(
        Definition(id="qa1", kind="qa_rubric", name="covers-happy-path", body="# covers the happy path?")
    )
    st.set_definition_active(rubric.id)


def _engine(st: Store, bus=None) -> WorkflowEngine:
    return WorkflowEngine(st, cards=CardService(st), bus=bus, clock=lambda: "2026-06-21T00:00:00+00:00")


async def _park_in_qa(engine: WorkflowEngine) -> str:
    """Start the workflow and drive its first step to the ``qa`` parked state; return the run id."""
    started = await engine.start("s1", "add-feature")
    run_id = started["run_id"]
    engine.begin_step(run_id)
    parked = await engine.submit_step(run_id)  # qa step, no qa_passed yet → parks in QA
    assert parked["status"] == QA
    return run_id


# ── verdict → outcome mapping (§11.2 step 3, §6.7) ──────────────────────────────────────────────


async def test_approve_advances_step(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    run_id = await _park_in_qa(engine)

    qa = WorkflowQAReviewer(engine, _FakeReviewer(ReviewResult(verdict=APPROVE, summary="ok")))
    res = await qa.review_step(run_id, diff="some diff")

    assert res["ok"] is True
    assert res["verdict"] == APPROVE and res["qa_passed"] is True
    # Engine advanced to the second step (index 1, pending).
    run = st.get_workflow_run(run_id)
    assert run.step_index == 1 and run.step_status == PENDING


async def test_request_changes_fails_step(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    run_id = await _park_in_qa(engine)

    qa = WorkflowQAReviewer(engine, _FakeReviewer(ReviewResult(verdict=REQUEST_CHANGES, summary="redo")))
    res = await qa.review_step(run_id, diff="d")

    assert res["qa_passed"] is False and res["verdict"] == REQUEST_CHANGES
    run = st.get_workflow_run(run_id)
    assert run.step_status == FAILED and run.step_index == 0  # did not advance


async def test_escalate_fails_step_and_surfaces_needs_human(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    run_id = await _park_in_qa(engine)

    qa = WorkflowQAReviewer(
        engine, _FakeReviewer(ReviewResult(verdict=ESCALATE, summary="unsure", needs_human=True))
    )
    res = await qa.review_step(run_id, diff="d")

    assert res["qa_passed"] is False and res["needs_human"] is True
    assert st.get_workflow_run(run_id).step_status == FAILED


# ── inputs reach the Reviewer ───────────────────────────────────────────────────────────────────


async def test_rubric_goal_and_diff_reach_reviewer(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    run_id = await _park_in_qa(engine)

    rv = _FakeReviewer(ReviewResult(verdict=APPROVE))
    qa = WorkflowQAReviewer(engine, rv)
    await qa.review_step(run_id, diff="THE DIFF")

    call = rv.calls[-1]
    assert call["qa_standard"] == "# covers the happy path?"
    assert call["diff"] == "THE DIFF"
    assert call["goal"] == "add feature"  # falls back to the session goal


async def test_explicit_goal_overrides_session_goal(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    run_id = await _park_in_qa(engine)

    rv = _FakeReviewer(ReviewResult(verdict=APPROVE))
    qa = WorkflowQAReviewer(engine, rv)
    await qa.review_step(run_id, goal="custom goal", diff="d")
    assert rv.calls[-1]["goal"] == "custom goal"


# ── fail-closed guards ──────────────────────────────────────────────────────────────────────────


async def test_unknown_run_is_no_run(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    qa = WorkflowQAReviewer(engine, _FakeReviewer(ReviewResult(verdict=APPROVE)))
    assert (await qa.review_step("nope", diff="d"))["error"] == "no_run"


async def test_not_in_qa_is_rejected(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    started = await engine.start("s1", "add-feature")
    run_id = started["run_id"]  # still PENDING, not parked in QA
    qa = WorkflowQAReviewer(engine, _FakeReviewer(ReviewResult(verdict=APPROVE)))
    res = await qa.review_step(run_id, diff="d")
    assert res["error"] == "not_in_qa"


async def test_missing_rubric_is_fail_closed(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    # The step parks in QA off the workflow body's rubric NAME (no definition lookup needed yet).
    run_id = await _park_in_qa(engine)
    # Now drop the active rubric so step_view's material resolves qa=None — we must not advance a
    # step we cannot judge (fail-closed).
    _deactivate_rubric(st, "qa1")

    qa = WorkflowQAReviewer(engine, _FakeReviewer(ReviewResult(verdict=APPROVE)))
    res = await qa.review_step(run_id, diff="d")
    assert res["error"] == "no_qa_rubric"


def _deactivate_rubric(st: Store, def_id: str) -> None:
    """Flip a definition's is_active off (so get_active_definition returns None)."""
    from foreman.client.store.models import Definition as _D

    with st.session() as s:
        row = s.get(_D, def_id)
        if row is not None:
            row.is_active = False
            s.add(row)
            s.commit()


# ── event emission ──────────────────────────────────────────────────────────────────────────────


async def test_review_event_persisted_and_published(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    bus = _RecBus()
    engine = _engine(st, bus=bus)
    run_id = await _park_in_qa(engine)

    qa = WorkflowQAReviewer(
        engine, _FakeReviewer(ReviewResult(verdict=APPROVE, summary="ok")), bus=bus
    )
    await qa.review_step(run_id, diff="SECRET_DIFF_CONTENT")

    # A `review` event was published with metadata only (no diff/body).
    review_events = [e for e in bus.events if e.type == "review" and e.source == "qa-reviewer"]
    assert len(review_events) == 1
    payload = review_events[0].payload
    assert payload["verdict"] == APPROVE and payload["qa_passed"] is True
    assert payload["qa_rubric"] == "covers-happy-path"
    assert "SECRET_DIFF_CONTENT" not in json.dumps(payload)  # diff content never enters the event

    # And it was persisted to the store.
    stored = [e for e in st.get_events("s1") if e.type == "review" and e.source == "qa-reviewer"]
    assert len(stored) == 1


# ── end-to-end through the real Reviewer (mock LLM, no network) ──────────────────────────────────


async def test_end_to_end_with_real_reviewer_mock_llm(tmp_path):
    st = _store(tmp_path)
    _seed(st)
    engine = _engine(st)
    run_id = await _park_in_qa(engine)

    captured: dict = {}
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "test-model"
    cfg.secrets.llm_api_key = "secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        reply = json.dumps({"verdict": "approve", "summary": "meets the rubric"})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    llm = LLMClient(cfg, transport=httpx.MockTransport(handler))
    reviewer = Reviewer(llm, language="zh")
    qa = WorkflowQAReviewer(engine, reviewer)

    res = await qa.review_step(run_id, diff="diff body here")
    await llm.aclose()

    assert res["ok"] is True and res["qa_passed"] is True
    # The QA rubric body + goal + diff all reached the LLM, and §15 language directive is present.
    user = captured["json"]["messages"][-1]["content"]
    assert "covers the happy path" in user and "add feature" in user and "diff body here" in user
    system = captured["json"]["messages"][0]["content"]
    assert "请始终用简体中文回答" in system
    # Step advanced.
    assert st.get_workflow_run(run_id).step_index == 1
