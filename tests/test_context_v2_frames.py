from __future__ import annotations

import json

from foreman.client.core.context_v2 import (
    LANE_NOISE,
    ContextManager,
    make_frame_id,
    materialize_event,
)
from foreman.client.store import Store
from foreman.client.store.models import Event, Session


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "frames.db"))
    store.init()
    return store


def _event(event_id: str, event_type: str, payload: dict, *, ts: str = "2026-07-01T00:00:00Z") -> Event:
    return Event(
        id=event_id,
        session_id="s1",
        task_id="t1",
        type=event_type,
        source="test",
        payload_json=json.dumps(payload, ensure_ascii=False),
        ts=ts,
    )


def _payload(frame):
    return json.loads(frame.payload_json)


def _add_event(store: Store, event: Event) -> None:
    with store.session() as session:
        session.add(event)
        session.commit()


def test_make_frame_id_is_deterministic_for_same_payload_order():
    left = make_frame_id("s1", "event-10", "user_message", {"b": 2, "a": 1})
    right = make_frame_id("s1", "event-10", "user_message", {"a": 1, "b": 2})

    assert left == right
    assert left.startswith("frame_s1_event_10_user_message_")


def test_materialize_session_replay_twice_does_not_duplicate_frames(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    _add_event(store, _event("e1", "dispatch", {"goal": "fix bug", "workspace": "E:/repo"}))
    manager = ContextManager(store)

    first = manager.materialize_session("s1")
    second = manager.materialize_session("s1")

    assert len(first) == 2
    assert len(second) == 2
    assert [row.id for row in store.get_context_frames("s1")] == [row.id for row in first]


def test_dispatch_materializes_user_message_and_worktree_state():
    frames = materialize_event(
        _event("e1", "dispatch", {"goal": "implement X", "workspace": "E:/AutoWorkAgent"})
    )

    assert [frame.type for frame in frames] == ["user_message", "worktree_state"]
    assert _payload(frames[0])["goal"] == "implement X"
    assert _payload(frames[1])["cwd"] == "E:/AutoWorkAgent"


def test_command_execution_aggregated_output_extracts_command_exit_cwd_and_important_lines():
    frames = materialize_event(
        _event(
            "e1",
            "agent_output",
            {
                "cwd": "E:/repo",
                "item": {
                    "type": "command_execution",
                    "command": "pytest tests/test_a.py",
                    "exit_code": 1,
                    "status": "failed",
                    "aggregated_output": "collecting\nFAILED tests/test_a.py::test_x\nAssertionError",
                },
            },
        )
    )

    assert [frame.type for frame in frames] == ["command_result"]
    payload = _payload(frames[0])
    assert payload["command"] == "pytest tests/test_a.py"
    assert payload["exit_code"] == 1
    assert payload["cwd"] == "E:/repo"
    assert any("FAILED tests/test_a.py::test_x" in line for line in payload["important_lines"])


def test_long_stdout_stderr_are_capped_and_summarized():
    huge = "start\n" + ("noise\n" * 700) + "ERROR tests/test_x.py failed\n" + ("tail\n" * 700)
    frames = materialize_event(
        _event(
            "e1",
            "tool_post",
            {
                "tool": "run_command",
                "call_id": "c1",
                "ok": False,
                "result": {
                    "ok": False,
                    "data": {
                        "command": "pytest",
                        "returncode": 1,
                        "stdout": huge,
                        "stderr": huge,
                    },
                },
            },
        )
    )

    payload = _payload(frames[0])
    assert frames[0].type == "command_result"
    assert len(payload["stdout_summary"]) <= 1200
    assert len(payload["stderr_summary"]) <= 1200
    assert payload["truncated"] is True
    assert any("ERROR tests/test_x.py failed" in line for line in payload["important_lines"])
    assert "noise\n" * 500 not in payload["stdout_summary"]


def test_pm_output_and_reasoning_are_lane_7_not_model_visible():
    for event_type in ("pm_output", "pm_reasoning", "agent_reasoning"):
        frames = materialize_event(_event("e1", event_type, {"delta": "thinking"}))
        assert len(frames) == 1
        assert frames[0].lane == LANE_NOISE
        assert _payload(frames[0])["model_visible"] is False


def test_tool_pre_and_post_with_same_call_id_become_paired_tool_frames():
    pre = materialize_event(
        _event("e1", "tool_pre", {"tool": "read_file", "call_id": "call-1", "input": {"path": "README.md"}})
    )
    post = materialize_event(
        _event(
            "e2",
            "tool_post",
            {
                "tool": "read_file",
                "call_id": "call-1",
                "ok": True,
                "result": {"ok": True, "data": {"text": "hello"}},
            },
        )
    )

    assert pre[0].type == "tool_call"
    assert post[0].type == "tool_result"
    assert _payload(pre[0])["call_id"] == "call-1"
    assert _payload(post[0])["call_id"] == "call-1"


def test_context_compact_event_becomes_context_compaction_frame():
    frames = materialize_event(_event("e1", "context_compact", {"checkpoint_id": "cp1"}))

    assert [frame.type for frame in frames] == ["context_compaction"]
    assert _payload(frames[0])["payload"]["checkpoint_id"] == "cp1"


def test_unknown_event_does_not_crash_materializer():
    frames = materialize_event(_event("e1", "unknown_future_event", {"x": "future"}))

    assert frames == []
