"""Tests for the client store r/w helpers (TASKS T1.8).

Uses a tmp_path sqlite FILE (not :memory:, which would give each connection its own db).
"""

from __future__ import annotations

import json

from foreman.client.store import Store
from foreman.client.store.models import (
    Action,
    Approval,
    Audit,
    Checkpoint,
    ContextSnapshot,
    DecisionCard,
    Event,
    MemoryItem,
    Report,
    Review,
    SchemaVersion,
    Session,
    Task,
    WorkflowRun,
)
from foreman.shared.events import make_event


def _store(tmp_path) -> Store:
    st = Store(str(tmp_path / "t.db"))
    st.init()
    return st


def test_schema_version_recorded(tmp_path):
    st = _store(tmp_path)
    with st.session() as s:
        sv = s.get(SchemaVersion, 2)
    assert sv is not None and sv.applied_at


def test_session_and_task_roundtrip(tmp_path):
    st = _store(tmp_path)
    st.add_session(
        Session(id="s1", goal="do X", workspace="/w", main_workspace="/main", agent_type="claude-code")
    )
    st.add_task(Task(id="t1", session_id="s1", instruction="do X"))
    sessions = st.get_sessions()
    assert [s.id for s in sessions] == ["s1"]
    assert sessions[0].goal == "do X"
    assert sessions[0].main_workspace == "/main"

    st.update_session("s1", workspace="/tmp/worktree")
    updated = st.get_session("s1")
    assert updated.workspace == "/tmp/worktree"
    assert updated.main_workspace == "/main"


def test_event_roundtrip_serializes_payload(tmp_path):
    st = _store(tmp_path)
    event = make_event("agent_output", "claude-code", "s1", payload={"text": "hi"})
    row = st.add_event(event)
    assert row.id and json.loads(row.payload_json) == {"text": "hi"}
    assert event.id == row.id

    events = st.get_events("s1")
    assert len(events) == 1
    assert events[0].type == "agent_output" and events[0].source == "claude-code"
    assert json.loads(events[0].payload_json) == {"text": "hi"}


def test_event_updates_session_activity_without_inferring_terminal_status(tmp_path):
    st = _store(tmp_path)
    st.add_session(Session(id="s1", goal="do X", status="running", updated_at="old"))

    st.add_event(make_event("agent_output", "codex", "s1", payload={"text": "working"}))
    mid = st.get_session("s1")
    assert mid.status == "running"
    assert mid.updated_at != "old"

    st.add_event(make_event("stop", "codex", "s1", payload={"result": "done"}))
    assert st.get_session("s1").status == "running"

    st.add_event(make_event("error", "codex", "s1", payload={"msg": "failed"}))
    assert st.get_session("s1").status == "running"


def test_delete_session_removes_related_records_only(tmp_path):
    st = _store(tmp_path)
    st.add_session(Session(id="s1", goal="one"))
    st.add_session(Session(id="s2", goal="two"))
    st.add_task(Task(id="t1", session_id="s1", instruction="one"))
    st.add_task(Task(id="t2", session_id="s2", instruction="two"))

    with st.session() as s:
        s.add(Action(id="a1", session_id="s1", task_id="t1"))
        s.add(Action(id="a2", session_id="s2", task_id="t2"))
        s.add(Audit(id="au1", action_id="a1", verdict="pass"))
        s.add(Audit(id="au2", action_id="a2", verdict="pass"))
        s.add(Review(id="r1", task_id="t1", verdict="approve"))
        s.add(Review(id="r2", task_id="t2", verdict="approve"))
        s.add(Approval(id="ap1", session_id="s1"))
        s.add(Approval(id="ap2", session_id="s2"))
        s.add(Checkpoint(id="c1", session_id="s1"))
        s.add(Checkpoint(id="c2", session_id="s2"))
        s.add(ContextSnapshot(id="cs1", session_id="s1"))
        s.add(ContextSnapshot(id="cs2", session_id="s2"))
        s.add(DecisionCard(id="dc1", action_id="a1", session_id="s1"))
        s.add(DecisionCard(id="dc2", action_id="a2", session_id="s2"))
        s.add(Event(id="e1", session_id="s1", type="agent_output", source="codex"))
        s.add(Event(id="e2", session_id="s2", type="agent_output", source="codex"))
        s.add(MemoryItem(id="m1", session_id="s1", text="one"))
        s.add(MemoryItem(id="m2", session_id="s2", text="two"))
        s.add(Report(id="rp1", session_id="s1"))
        s.add(Report(id="rp2", session_id="s2"))
        s.add(WorkflowRun(id="w1", session_id="s1", workflow_id="def1"))
        s.add(WorkflowRun(id="w2", session_id="s2", workflow_id="def2"))
        s.commit()

    assert st.delete_session("s1") is True
    assert st.delete_session("missing") is False
    with st.session() as s:
        for model, row_id in (
            (Session, "s1"),
            (Task, "t1"),
            (Action, "a1"),
            (Audit, "au1"),
            (Review, "r1"),
            (Approval, "ap1"),
            (Checkpoint, "c1"),
            (ContextSnapshot, "cs1"),
            (DecisionCard, "dc1"),
            (Event, "e1"),
            (MemoryItem, "m1"),
            (Report, "rp1"),
            (WorkflowRun, "w1"),
        ):
            assert s.get(model, row_id) is None
        for model, row_id in (
            (Session, "s2"),
            (Task, "t2"),
            (Action, "a2"),
            (Audit, "au2"),
            (Review, "r2"),
            (Approval, "ap2"),
            (Checkpoint, "c2"),
            (ContextSnapshot, "cs2"),
            (DecisionCard, "dc2"),
            (Event, "e2"),
            (MemoryItem, "m2"),
            (Report, "rp2"),
            (WorkflowRun, "w2"),
        ):
            assert s.get(model, row_id) is not None


def test_get_events_filters_by_session(tmp_path):
    st = _store(tmp_path)
    st.add_event(make_event("agent_output", "codex", "sA", payload={}))
    st.add_event(make_event("agent_output", "codex", "sB", payload={}))
    assert len(st.get_events("sA")) == 1
    assert len(st.get_events("sB")) == 1


def test_checkpoint_roundtrip_ordered_by_step(tmp_path):
    st = _store(tmp_path)
    st.add_checkpoint(Checkpoint(id="c2", session_id="s1", step_index=1, vcs_ref="deadbeef"))
    st.add_checkpoint(Checkpoint(id="c1", session_id="s1", step_index=0, vcs_ref="cafe"))
    st.add_checkpoint(Checkpoint(id="cx", session_id="s2", step_index=0, vcs_ref="other"))

    rows = st.get_checkpoints("s1")
    assert [r.step_index for r in rows] == [0, 1]   # ordered by step, not insert order
    assert [r.vcs_ref for r in rows] == ["cafe", "deadbeef"]
    assert len(st.get_checkpoints("s2")) == 1
