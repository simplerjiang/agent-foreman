from __future__ import annotations

import json

import pytest

from foreman.client.core import context_v2
from foreman.client.core.context_v2 import ContextCompactError, ContextManager
from foreman.client.store import Store
from foreman.client.store.models import Event, Session


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "checkpoint.db"))
    store.init()
    return store


def _seed(store: Store) -> None:
    store.add_session(Session(id="s1", goal="run tests", workspace="E:/repo", plan="old plan"))
    with store.session() as session:
        session.add(
            Event(
                id="e1",
                session_id="s1",
                task_id="t1",
                type="dispatch",
                source="user",
                payload_json=json.dumps({"goal": "run tests", "workspace": "E:/repo"}),
                ts="2026-07-01T00:00:00Z",
            )
        )
        session.add(
            Event(
                id="e2",
                session_id="s1",
                task_id="t1",
                type="tool_post",
                source="codex",
                payload_json=json.dumps(
                    {
                        "tool": "run_command",
                        "call_id": "c1",
                        "command": "pytest",
                        "exit_code": 0,
                        "stdout": "1 passed",
                        "cwd": "E:/repo",
                    }
                ),
                ts="2026-07-01T00:00:01Z",
            )
        )
        session.commit()


class _RemoteLLM:
    provider = "openai"

    async def responses_compact(self, input_items, *, instructions="", model="", metadata=None):
        assert input_items
        assert "run tests" in input_items[0]["content"][0]["text"]
        return {"summary_json": {"summary": "remote compact summary"}}


class _UnsupportedLLM:
    provider = "openai"

    async def responses_compact(self, *args, **kwargs):
        raise RuntimeError("unsupported")


async def test_remote_compact_mock_200_writes_context_checkpoint(tmp_path):
    store = _store(tmp_path)
    _seed(store)

    checkpoint = await ContextManager(store, llm=_RemoteLLM()).compact_now(
        "s1",
        trigger="manual",
        reason="test",
        window_tokens=1000,
    )

    session = store.get_session("s1")
    event_payloads = [json.loads(e.payload_json) for e in store.get_events("s1") if e.type == "context_compact"]
    assert checkpoint.method == "remote"
    assert session.latest_context_checkpoint_id == checkpoint.id
    assert "remote compact summary" in session.plan
    assert event_payloads[-1]["status"] == "completed"
    assert event_payloads[-1]["checkpoint_id"] == checkpoint.id


async def test_remote_compact_unsupported_falls_back_local(tmp_path):
    store = _store(tmp_path)
    _seed(store)

    checkpoint = await ContextManager(store, llm=_UnsupportedLLM()).compact_now(
        "s1",
        trigger="manual",
        reason="fallback",
        window_tokens=1000,
    )

    assert checkpoint.method == "local"
    token_usage = json.loads(checkpoint.token_usage_json)
    assert "remote_error" in token_usage
    assert store.get_session("s1").latest_context_checkpoint_id == checkpoint.id


async def test_local_compact_writes_non_empty_replacement_history_and_restores(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    manager = ContextManager(store)

    checkpoint = await manager.compact_now(
        "s1",
        trigger="manual",
        reason="local",
        window_tokens=1000,
    )

    history = json.loads(checkpoint.replacement_history_json)
    assert history["items"]
    assert json.loads(checkpoint.summary_json)["summary"]
    assert json.loads(checkpoint.runtime_state_json)["goal"] == "run tests"
    active = manager.build_active_context("s1", purpose="pm_plan")
    assert active.envelope["context"]["restore_mode"] == "checkpoint"
    assert active.replacement_history


async def test_empty_replacement_history_prevents_checkpoint_install(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed(store)
    before_plan = store.get_session("s1").plan

    monkeypatch.setattr(context_v2, "frames_to_replacement_history", lambda *a, **k: {"items": []})

    with pytest.raises(ContextCompactError):
        await ContextManager(store).compact_now(
            "s1",
            trigger="manual",
            reason="bad-history",
            window_tokens=1000,
        )

    assert store.get_session("s1").latest_context_checkpoint_id == ""
    assert store.get_session("s1").plan == before_plan
    assert store.get_context_checkpoints("s1") == []
    failed = [json.loads(e.payload_json) for e in store.get_events("s1") if e.type == "context_compact"]
    assert failed[-1]["status"] == "failed"


async def test_replacement_history_json_round_trips_and_passes_validation(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    manager = ContextManager(store)

    checkpoint = await manager.compact_now(
        "s1",
        trigger="manual",
        reason="round-trip",
        window_tokens=1000,
    )

    restored = manager.build_active_context("s1", purpose="pm_review")
    assert restored.degraded is False
    assert restored.replacement_history == json.loads(checkpoint.replacement_history_json)["items"]
