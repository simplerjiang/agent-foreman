from __future__ import annotations

import json

from foreman.client.core.context_v2 import ContextManager
from foreman.client.store import Store
from foreman.client.store.models import ContextCheckpoint, ContextSnapshot, Event, Session


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "active-context.db"))
    store.init()
    return store


def _event(event_id: str, event_type: str, payload: dict, *, ts: str) -> Event:
    return Event(
        id=event_id,
        session_id="s1",
        task_id="t1",
        type=event_type,
        source="test",
        payload_json=json.dumps(payload, ensure_ascii=False),
        ts=ts,
    )


def _add_event(store: Store, event: Event) -> None:
    with store.session() as session:
        session.add(event)
        session.commit()


def _checkpoint(**overrides) -> ContextCheckpoint:
    data = {
        "id": "cp1",
        "session_id": "s1",
        "trigger": "manual",
        "reason": "test",
        "source_cursor_json": json.dumps(
            {"end": {"event_ts": "2026-07-01T00:00:00Z", "event_id": "e1"}}
        ),
        "summary_json": "{}",
        "replacement_history_json": json.dumps({"items": [{"id": "rh1", "content": "checkpoint summary"}]}),
        "runtime_state_json": "{}",
        "created_at": "2026-07-01T00:00:02Z",
    }
    data.update(overrides)
    return ContextCheckpoint(**data)


def test_build_active_context_without_checkpoint_uses_raw_frames(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="fix bug", workspace="E:/repo"))
    _add_event(
        store,
        _event("e1", "dispatch", {"goal": "fix bug", "workspace": "E:/repo"}, ts="2026-07-01T00:00:00Z"),
    )

    active = ContextManager(store).build_active_context("s1", purpose="pm_plan")

    assert active.envelope["context"]["restore_mode"] == "raw_frames"
    assert active.replacement_history == []
    assert active.runtime_state["goal"] == "fix bug"
    assert any(item["type"] == "task" and item["goal"] == "fix bug" for item in active.stable_prefix)


def test_build_active_context_with_valid_checkpoint_uses_replacement_history(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    _add_event(store, _event("e1", "dispatch", {"goal": "goal"}, ts="2026-07-01T00:00:00Z"))
    _add_event(store, _event("e2", "pm_plan", {"summary": "after checkpoint"}, ts="2026-07-01T00:00:01Z"))
    store.add_context_checkpoint(_checkpoint())
    store.set_latest_context_checkpoint("s1", "cp1")

    active = ContextManager(store).build_active_context("s1", purpose="pm_review")

    assert active.envelope["context"]["restore_mode"] == "checkpoint"
    assert active.replacement_history == [{"id": "rh1", "content": "checkpoint summary"}]
    assert [item["event_id"] for item in active.frames_after_checkpoint] == ["e2"]
    assert "checkpoint summary" in active.rendered_text
    assert "cp1" in active.rendered_text


def test_checkpoint_cursor_filters_covered_frames_without_duplication(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    _add_event(store, _event("e1", "dispatch", {"goal": "goal"}, ts="2026-07-01T00:00:00Z"))
    _add_event(store, _event("e2", "pm_plan", {"summary": "covered"}, ts="2026-07-01T00:00:01Z"))
    _add_event(store, _event("e3", "pm_review", {"summary": "tail"}, ts="2026-07-01T00:00:02Z"))
    store.add_context_checkpoint(
        _checkpoint(
            source_cursor_json=json.dumps(
                {"end": {"event_ts": "2026-07-01T00:00:01Z", "event_id": "e2"}}
            )
        )
    )
    store.set_latest_context_checkpoint("s1", "cp1")

    active = ContextManager(store).build_active_context("s1", purpose="pm_review")

    assert [item["event_id"] for item in active.frames_after_checkpoint] == ["e3"]


def test_corrupted_checkpoint_degrades_to_raw_frames_without_overwrite(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    _add_event(store, _event("e1", "dispatch", {"goal": "goal"}, ts="2026-07-01T00:00:00Z"))
    store.add_context_checkpoint(_checkpoint(replacement_history_json="{bad json"))
    store.set_latest_context_checkpoint("s1", "cp1")

    active = ContextManager(store).build_active_context("s1", purpose="pm_plan")

    assert active.degraded is True
    assert active.envelope["context"]["restore_mode"] == "raw_frames_degraded"
    assert active.warnings[0]["code"] == "corrupted_checkpoint"
    assert store.get_context_checkpoint("cp1").replacement_history_json == "{bad json"
    assert [item["event_id"] for item in active.frames_after_checkpoint] == ["e1"]
    assert "corrupted_checkpoint" in active.rendered_text
    assert '"degraded": true' in active.rendered_text


def test_legacy_session_plan_is_summary_not_replacement_history(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal", plan="legacy summary"))
    _add_event(store, _event("e1", "dispatch", {"goal": "goal"}, ts="2026-07-01T00:00:00Z"))

    active = ContextManager(store).build_active_context("s1", purpose="pm_plan")

    assert active.envelope["context"]["restore_mode"] == "legacy_summary"
    assert active.replacement_history == []
    assert any(item["type"] == "legacy_summary" for item in active.stable_prefix)


def test_legacy_context_snapshot_is_summary_not_replacement_history(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    store.add_context_snapshot(
        ContextSnapshot(
            id="snap1",
            session_id="s1",
            kind="rolling",
            summary_json=json.dumps({"text": "snapshot summary"}),
            created_at="2026-07-01T00:00:01Z",
        )
    )

    active = ContextManager(store).build_active_context("s1", purpose="pm_plan")

    assert active.envelope["context"]["restore_mode"] == "legacy_summary"
    assert active.replacement_history == []
    assert any(
        item["type"] == "legacy_summary" and item["source"] == "context_snapshot"
        for item in active.stable_prefix
    )
