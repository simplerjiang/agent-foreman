from __future__ import annotations

import json

from foreman.client.core.context_v2 import (
    ContextManager,
    build_pm_envelope,
    classify_user_intent,
)
from foreman.client.core.pm_contract import PlanContract
from foreman.client.store import Store
from foreman.client.store.models import Event, Session


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "pm-envelope.db"))
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


def _add_event(store: Store, event: Event) -> None:
    with store.session() as session:
        session.add(event)
        session.commit()


def test_user_intent_classifier_is_deterministic():
    assert classify_user_intent("hello, what is Python?") == "direct_answer"
    assert classify_user_intent("fix the failing test") == "code_change"
    assert classify_user_intent("inspect repo and find file") == "repo_inspection"
    assert classify_user_intent("open https://example.com screenshot") == "browser_task"
    assert classify_user_intent("say hi", explicit_agent=True) == "code_change"


def test_pm_envelope_required_sections_and_contract_helpers():
    session = Session(id="s1", goal="hello")
    contract = PlanContract(enabled_agents=["codex"])
    envelope = build_pm_envelope(
        session,
        purpose="pm_plan",
        goal="hello",
        user_intent_type="direct_answer",
        runtime_state={"cwd": "E:/repo", "worktree": "E:/repo", "branch": "main", "active_agents": []},
        available_agents=["codex"],
        tool_schema=[{"name": "read_file", "input_schema": {"type": "object"}}],
        output_contract=contract.output_contract(),
        validator_rules=contract.validator_rules(),
        stable_prefix=[],
        replacement_history=[],
        frames_after_checkpoint=[],
        warnings=[],
    )

    assert list(envelope.keys()) == [
        "task",
        "environment",
        "agents",
        "context",
        "tools",
        "output_contract",
        "validator_rules",
        "warnings",
    ]
    assert envelope["task"]["user_intent_type"] == "direct_answer"
    assert envelope["tools"]["available"] == ["read_file"]
    assert envelope["output_contract"] == contract.output_contract()
    assert envelope["validator_rules"] == contract.validator_rules()
    assert envelope["output_contract"]["direct_reply_instruction_required"] is True
    assert envelope["output_contract"]["direct_reply_reply_required"] is True


def test_active_context_envelope_includes_validation_error_runtime_and_agents(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="fix bug", workspace="E:/repo"))
    _add_event(
        store,
        _event(
            "e1",
            "agent_start",
            {"agent_id": "dev-1", "cwd": "E:/worktree", "worktree": "E:/worktree", "branch": "feature"},
            ts="2026-07-01T00:00:00Z",
        ),
    )
    _add_event(
        store,
        _event(
            "e2",
            "pm_validation_error",
            {"error": "final_plan_missing_reply", "round": 1},
            ts="2026-07-01T00:00:01Z",
        ),
    )

    active = ContextManager(store).build_active_context("s1", purpose="pm_plan")
    envelope = active.envelope

    assert envelope["environment"]["cwd"] == "E:/worktree"
    assert envelope["environment"]["worktree"] == "E:/worktree"
    assert envelope["environment"]["branch"] == "feature"
    assert envelope["agents"]["active"][0]["agent_id"] == "dev-1"
    validation_frames = [
        item for item in envelope["context"]["frames_after_checkpoint"]
        if item["type"] == "previous_validation_error"
    ]
    assert validation_frames
    assert validation_frames[0]["payload"]["payload"]["error"] == "final_plan_missing_reply"
    assert "previous_validation_error" in active.rendered_text


def test_rendered_text_contains_sections_and_caps_lane_7_noise(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    _add_event(store, _event("e1", "dispatch", {"goal": "goal"}, ts="2026-07-01T00:00:00Z"))
    _add_event(store, _event("e2", "pm_reasoning", {"delta": "secret " * 1000}, ts="2026-07-01T00:00:01Z"))

    active = ContextManager(store).build_active_context("s1", purpose="pm_plan")

    for section in ("task", "environment", "agents", "context", "output_contract", "validator_rules"):
        assert f'"{section}"' in active.rendered_text
    assert "secret secret secret secret" not in active.rendered_text
