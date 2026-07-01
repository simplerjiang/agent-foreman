from __future__ import annotations

import json

import pytest

from foreman.client.core.context_v2 import ContextManager
from foreman.client.core.dispatch_service import (
    _advance_reviewed_event_id_from_active_context,
    _review_timeline_from_active_context,
)
from foreman.client.store import Store
from foreman.client.store.models import Event, Session


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "realistic.db"))
    store.init()
    return store


def _add_event(
    store: Store,
    event_id: str,
    event_type: str,
    payload: dict,
    *,
    ts: str,
    source: str = "codex",
) -> None:
    with store.session() as session:
        session.add(
            Event(
                id=event_id,
                session_id="s1",
                task_id="t1",
                type=event_type,
                source=source,
                payload_json=json.dumps(payload, ensure_ascii=False),
                ts=ts,
            )
        )
        session.commit()


async def test_compact_source_cursor_end_filters_next_review(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="ship it", workspace="E:/repo"))
    _add_event(store, "e1", "dispatch", {"goal": "ship it", "workspace": "E:/repo"}, ts="2026-07-01T00:00:00Z", source="user")
    _add_event(store, "e2", "tool_post", {"tool": "run_command", "command": "pytest", "exit_code": 0}, ts="2026-07-01T00:00:01Z")

    checkpoint = await ContextManager(store).compact_now(
        "s1",
        trigger="manual",
        reason="cursor",
        window_tokens=1000,
    )
    assert json.loads(checkpoint.source_cursor_json)["end"]["event_id"] == "e2"

    _add_event(store, "e3", "stop", {"result": "new output"}, ts="2026-07-01T00:00:02Z")
    manager = ContextManager(store)
    active = manager.build_active_context("s1", purpose="pm_review")
    rows = store.get_events("s1")
    reviewed = _advance_reviewed_event_id_from_active_context(rows, "e1", active)
    timeline = _review_timeline_from_active_context(active, rows, reviewed)

    assert reviewed == "e2"
    assert active.replacement_history
    assert "new output" in timeline
    assert "pytest" not in timeline
    assert "event:e1" not in timeline
    assert "event:e2" not in timeline


async def test_runtime_anchors_survive_compact_restore(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="fix flaky tests", workspace="E:/repo"))
    _add_event(store, "e1", "dispatch", {"goal": "fix flaky tests", "workspace": "E:/repo"}, ts="2026-07-01T00:00:00Z", source="user")
    _add_event(
        store,
        "e2",
        "agent_start",
        {"agent_id": "dev-1", "cwd": "E:/repo", "worktree": "E:/repo-wt", "branch": "fix/flaky"},
        ts="2026-07-01T00:00:01Z",
    )
    _add_event(store, "e3", "pm_reasoning", {"text": "noise " * 200}, ts="2026-07-01T00:00:02Z", source="pm-agent")
    _add_event(store, "e4", "file_change", {"changed_files": ["tests/test_app.py"]}, ts="2026-07-01T00:00:03Z")
    _add_event(
        store,
        "e5",
        "test_result",
        {"command": "pytest", "status": "failed", "failed": 1, "failures": ["test_app"]},
        ts="2026-07-01T00:00:04Z",
    )
    _add_event(
        store,
        "e6",
        "test_result",
        {"command": "pytest", "status": "passed", "passed": 1, "failed": 0},
        ts="2026-07-01T00:00:05Z",
    )
    _add_event(
        store,
        "e7",
        "stop",
        {
            "summary": "fixed tests",
            "changed_files": ["src/app.py"],
            "tests": [{"command": "pytest", "status": "passed"}],
            "next_actions": ["review diff"],
        },
        ts="2026-07-01T00:00:06Z",
    )

    manager = ContextManager(store)
    checkpoint = await manager.compact_now("s1", trigger="manual", reason="anchors", window_tokens=2000)
    restored = manager.build_active_context("s1", purpose="pm_plan")

    assert "fix flaky tests" in restored.rendered_text
    assert restored.runtime_state["cwd"] == "E:/repo"
    assert restored.runtime_state["worktree"] == "E:/repo-wt"
    assert restored.runtime_state["branch"] == "fix/flaky"
    assert any(agent["agent_id"] == "dev-1" for agent in restored.runtime_state["active_agents"])
    assert "tests/test_app.py" in restored.runtime_state["changed_files"]
    assert "src/app.py" in restored.runtime_state["changed_files"]
    assert restored.runtime_state["last_tests"][-1]["status"] == "passed"
    assert "review diff" in restored.runtime_state["next_steps"]
    assert "noise " * 20 not in restored.rendered_text
    assert restored.degraded is False
    assert json.loads(checkpoint.replacement_history_json)["items"] == restored.replacement_history


async def test_command_tool_pair_survives_compact_or_is_paired_in_replacement_history(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="run command", workspace="E:/repo"))
    _add_event(store, "e1", "dispatch", {"goal": "run command", "workspace": "E:/repo"}, ts="2026-07-01T00:00:00Z", source="user")
    _add_event(
        store,
        "e2",
        "tool_pre",
        {"tool": "run_command", "call_id": "call_1", "input": {"command": "pytest", "cwd": "E:/repo"}},
        ts="2026-07-01T00:00:01Z",
    )
    _add_event(
        store,
        "e3",
        "tool_post",
        {"tool": "run_command", "call_id": "call_1", "command": "pytest", "exit_code": 0, "stdout": "ok"},
        ts="2026-07-01T00:00:02Z",
    )

    checkpoint = await ContextManager(store).compact_now(
        "s1",
        trigger="manual",
        reason="tool-pair",
        window_tokens=1000,
    )
    items = json.loads(checkpoint.replacement_history_json)["items"]
    command_items = [item for item in items if item.get("kind") == "command_result"]

    assert command_items
    command_item = command_items[0]
    assert len(command_item["frame_ids"]) >= 2
    assert "event:e2" in command_item["source_refs"]
    assert "event:e3" in command_item["source_refs"]
    assert "pytest" in command_item["content"]


@pytest.mark.xfail(reason="threshold commit will implement soft compact failure surfacing")
def test_soft_compact_failure_fact_enters_active_context_when_available():
    raise NotImplementedError


@pytest.mark.xfail(reason="threshold commit will implement hard compact failure blocking")
def test_hard_compact_failure_blocks_pm_llm_call():
    raise NotImplementedError
