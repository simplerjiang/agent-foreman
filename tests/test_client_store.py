"""Tests for the client store r/w helpers (TASKS T1.8).

Uses a tmp_path sqlite FILE (not :memory:, which would give each connection its own db).
"""

from __future__ import annotations

import json

from foreman.client.store import Store
from foreman.client.store.models import Checkpoint, SchemaVersion, Session, Task
from foreman.shared.events import make_event


def _store(tmp_path) -> Store:
    st = Store(str(tmp_path / "t.db"))
    st.init()
    return st


def test_schema_version_recorded(tmp_path):
    st = _store(tmp_path)
    with st.session() as s:
        sv = s.get(SchemaVersion, 1)
    assert sv is not None and sv.applied_at


def test_session_and_task_roundtrip(tmp_path):
    st = _store(tmp_path)
    st.add_session(Session(id="s1", goal="do X", workspace="/w", agent_type="claude-code"))
    st.add_task(Task(id="t1", session_id="s1", instruction="do X"))
    sessions = st.get_sessions()
    assert [s.id for s in sessions] == ["s1"]
    assert sessions[0].goal == "do X"


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
