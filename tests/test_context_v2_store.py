from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from foreman.client.store import Store
from foreman.client.store.models import (
    ContextCheckpoint,
    ContextFrame,
    ContextSnapshot,
    Event,
    MemoryItem,
    Session,
)


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "context-v2.db"))
    store.init()
    return store


def _frame(frame_id: str, *, session_id: str = "s1", event_id: str = "e1") -> ContextFrame:
    return ContextFrame(
        id=frame_id,
        session_id=session_id,
        event_id=event_id,
        event_ts="2026-07-01T00:00:00Z",
        type="user_message",
        role="user",
        lane=2,
        payload_json=json.dumps({"text": frame_id}),
        source_refs_json=json.dumps([f"event:{event_id}"]),
        payload_hash=f"hash-{frame_id}",
        created_at="2026-07-01T00:00:01Z",
    )


def _checkpoint(checkpoint_id: str = "cp1", *, session_id: str = "s1") -> ContextCheckpoint:
    return ContextCheckpoint(
        id=checkpoint_id,
        session_id=session_id,
        trigger="manual",
        reason="user_requested",
        method="local",
        source_cursor_json=json.dumps(
            {
                "start": {"event_ts": "2026-07-01T00:00:00Z", "event_id": "e1"},
                "end": {"event_ts": "2026-07-01T00:00:02Z", "event_id": "e2"},
            }
        ),
        input_frame_ids_json=json.dumps(["f1"]),
        summary_json=json.dumps({"current_progress": ["done"]}),
        replacement_history_json=json.dumps(
            {"schema": "foreman.replacement_history.v1", "items": [{"content": "summary"}]}
        ),
        runtime_state_json=json.dumps({"cwd": "E:/AutoWorkAgent"}),
        token_usage_json=json.dumps({"before_tokens": 100, "after_tokens": 20}),
        created_at="2026-07-01T00:00:03Z",
    )


def _snapshot(snapshot_id: str = "snap1", *, session_id: str = "s1") -> ContextSnapshot:
    return ContextSnapshot(
        id=snapshot_id,
        session_id=session_id,
        kind="rolling",
        summary_json=json.dumps({"summary": "compat"}),
        summary_hash=f"hash-{snapshot_id}",
        created_at="2026-07-01T00:00:04Z",
    )


def _memory_item(item_id: str = "mem1", *, session_id: str = "s1", snapshot_id: str = "snap1") -> MemoryItem:
    return MemoryItem(
        id=item_id,
        session_id=session_id,
        snapshot_id=snapshot_id,
        kind="fact",
        text="remember this",
        status="verified",
        created_at="2026-07-01T00:00:04Z",
        updated_at="2026-07-01T00:00:04Z",
    )


def test_fresh_db_has_context_v2_tables_and_session_pointer(tmp_path):
    store = _store(tmp_path)

    with store.engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        session_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(session)")).fetchall()
        }
        indexes = {
            row[1]
            for table in ("event", "context_frames", "context_checkpoints")
            for row in conn.execute(text(f"PRAGMA index_list({table})")).fetchall()
        }

    assert "context_frames" in tables
    assert "context_checkpoints" in tables
    assert "latest_context_checkpoint_id" in session_cols
    assert "context_materialized_until_ts" in session_cols
    assert "context_materialized_until_event_id" in session_cols
    assert "ix_event_session_ts_id" in indexes
    assert "ix_context_frames_session_event_order" in indexes
    assert "ix_context_checkpoints_session_created_id" in indexes


def test_context_frame_roundtrip_and_duplicate_replay_upserts(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))

    first = _frame("f1")
    duplicate = _frame("f1")
    duplicate.payload_json = json.dumps({"text": "updated"})

    store.add_context_frames([first])
    store.add_context_frames([duplicate])

    rows = store.get_context_frames("s1")
    assert [row.id for row in rows] == ["f1"]
    assert json.loads(rows[0].payload_json) == {"text": "updated"}
    assert store.get_context_frame("f1").id == "f1"


def test_context_frames_sort_by_event_cursor_created_at_and_id(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    rows = [
        _frame("late", event_id="e2"),
        _frame("early-b", event_id="e1"),
        _frame("early-a", event_id="e1"),
    ]
    rows[0].event_ts = "2026-07-01T00:00:02Z"
    rows[1].created_at = "2026-07-01T00:00:01Z"
    rows[2].created_at = "2026-07-01T00:00:00Z"
    store.add_context_frames(rows)

    assert [row.id for row in store.get_context_frames("s1")] == ["early-a", "early-b", "late"]
    after = store.get_context_frames(
        "s1", after_cursor={"event_ts": "2026-07-01T00:00:00Z", "event_id": "e1"}
    )
    assert [row.id for row in after] == ["late"]
    assert [row.id for row in store.get_context_frames("s1", limit=1)] == ["early-a"]


def test_context_checkpoint_roundtrip_and_latest_pointer(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))

    cp1 = store.add_context_checkpoint(_checkpoint("cp1"))
    cp2 = _checkpoint("cp2")
    cp2.created_at = "2026-07-01T00:00:04Z"
    store.add_context_checkpoint(cp2)
    store.set_latest_context_checkpoint("s1", "cp2", plan_summary="compat summary")

    assert store.get_context_checkpoint("cp1").id == cp1.id
    assert [row.id for row in store.get_context_checkpoints("s1", limit=1)] == ["cp2"]
    assert store.get_latest_context_checkpoint("s1").id == "cp2"
    session = store.get_session("s1")
    assert session.latest_context_checkpoint_id == "cp2"
    assert session.plan == "compat summary"


def test_get_events_after_cursor_uses_text_event_id_order(tmp_path):
    store = _store(tmp_path)
    with store.session() as s:
        s.add(Event(id="10", session_id="s1", type="agent_output", source="test", ts="t1"))
        s.add(Event(id="2", session_id="s1", type="agent_output", source="test", ts="t1"))
        s.add(Event(id="1", session_id="s1", type="agent_output", source="test", ts="t2"))
        s.commit()

    rows = store.get_events_after_cursor("s1", {"event_ts": "t1", "event_id": "10"})

    assert [row.id for row in rows] == ["2", "1"]


def test_install_context_checkpoint_updates_checkpoint_session_and_event_atomically(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))

    checkpoint, event = store.install_context_checkpoint(
        "s1",
        _checkpoint("cp1"),
        "compat summary",
        {"status": "completed", "method": "local", "event_id": "evt-compact"},
    )

    session = store.get_session("s1")
    assert checkpoint.id == "cp1"
    assert session.latest_context_checkpoint_id == "cp1"
    assert session.plan == "compat summary"
    assert event.id == "evt-compact"
    assert event.type == "context_compact"
    assert json.loads(event.payload_json)["checkpoint_id"] == "cp1"
    assert store.get_events("s1")[-1].id == "evt-compact"


def test_install_context_checkpoint_writes_compat_records_in_same_transaction(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))

    checkpoint, event = store.install_context_checkpoint(
        "s1",
        _checkpoint("cp1"),
        "compat summary",
        {"status": "completed", "event_id": "evt-compact"},
        compat_snapshot=_snapshot("snap1"),
        memory_items=[_memory_item("mem1", snapshot_id="snap1")],
    )

    assert checkpoint.id == "cp1"
    assert event.id == "evt-compact"
    assert [row.id for row in store.get_context_snapshots("s1")] == ["snap1"]
    assert [row.id for row in store.get_memory_items("s1")] == ["mem1"]
    assert store.get_memory_items("s1")[0].snapshot_id == "snap1"


def test_install_context_checkpoint_rolls_back_compat_records_on_commit_failure(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    store.add_context_checkpoint(_checkpoint("old"))
    store.set_latest_context_checkpoint("s1", "old", plan_summary="old plan")
    store.add_context_snapshot(_snapshot("snap-dupe"))

    with pytest.raises(Exception):
        store.install_context_checkpoint(
            "s1",
            _checkpoint("cp-new"),
            "new summary",
            {"status": "completed", "event_id": "evt-new"},
            compat_snapshot=_snapshot("snap-dupe"),
            memory_items=[_memory_item("mem-new", snapshot_id="snap-dupe")],
        )

    session = store.get_session("s1")
    assert session.latest_context_checkpoint_id == "old"
    assert session.plan == "old plan"
    assert store.get_context_checkpoint("cp-new") is None
    assert [event.id for event in store.get_events("s1") if event.type == "context_compact"] == []
    assert [row.id for row in store.get_memory_items("s1")] == []


def test_install_context_checkpoint_rolls_back_when_session_missing(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="session_not_found"):
        store.install_context_checkpoint(
            "missing", _checkpoint("cp1", session_id="missing"), "summary", {}
        )

    assert store.get_context_checkpoint("cp1") is None
    assert store.get_events("missing") == []


def test_install_context_checkpoint_rejects_mismatched_session_id(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="one"))
    checkpoint = _checkpoint("cp-mismatch", session_id="s2")

    with pytest.raises(ValueError, match="checkpoint_session_mismatch"):
        store.install_context_checkpoint(
            "s1",
            checkpoint,
            "summary",
            {"event_id": "evt-mismatch", "status": "completed"},
        )

    assert store.get_context_checkpoint("cp-mismatch") is None
    assert [row for row in store.get_events("s1") if row.type == "context_compact"] == []
    assert store.get_session("s1").latest_context_checkpoint_id == ""


def test_add_context_frames_and_cursor_update_are_atomic(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))

    rows = store.add_context_frames_and_update_materialization_cursor(
        "s1",
        [_frame("f1")],
        {"event_ts": "2026-07-01T00:00:00Z", "event_id": "e1"},
    )

    assert [row.id for row in rows] == ["f1"]
    assert store.get_context_materialization_cursor("s1") == {
        "event_ts": "2026-07-01T00:00:00Z",
        "event_id": "e1",
    }


def test_set_latest_context_checkpoint_rejects_missing_or_mismatched_checkpoint(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="one"))
    store.add_session(Session(id="s2", goal="two"))
    store.add_context_checkpoint(_checkpoint("cp-s2", session_id="s2"))

    assert store.set_latest_context_checkpoint("s1", "missing") is None
    assert store.get_session("s1").latest_context_checkpoint_id == ""

    with pytest.raises(ValueError, match="checkpoint_session_mismatch"):
        store.set_latest_context_checkpoint("s1", "cp-s2")

    assert store.get_session("s1").latest_context_checkpoint_id == ""


def test_delete_session_cleans_context_v2_rows(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="one"))
    store.add_session(Session(id="s2", goal="two"))
    store.add_context_frame(_frame("f1", session_id="s1"))
    store.add_context_frame(_frame("f2", session_id="s2"))
    store.add_context_checkpoint(_checkpoint("cp1", session_id="s1"))
    store.add_context_checkpoint(_checkpoint("cp2", session_id="s2"))

    assert store.delete_session("s1") is True

    assert store.get_context_frame("f1") is None
    assert store.get_context_checkpoint("cp1") is None
    assert store.get_context_frame("f2") is not None
    assert store.get_context_checkpoint("cp2") is not None
