from __future__ import annotations

import json

import pytest

from foreman.client.core import context_v2
from foreman.client.core import dispatch_service
from foreman.client.core.dispatch_service import DispatchService
from foreman.client.core.context_v2 import ActiveContext, ContextCompactError, ContextManager
from foreman.client.store import Store
from foreman.client.store.models import ContextCheckpoint, Event, Session
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg


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


class _EncryptedLLM:
    provider = "openai"

    async def responses_compact(self, *args, **kwargs):
        return {
            "object": "response.compaction",
            "output": [
                {"type": "message", "content": "readable provider message"},
                {"type": "compaction_summary", "encrypted_content": "SECRET_ENCRYPTED_BLOB"},
            ],
        }


class _FailingLLM:
    provider = "openai"

    async def responses_compact(self, *args, **kwargs):
        raise RuntimeError("remote down")


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    return cfg


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


async def test_compact_now_does_not_call_events_to_text(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed(store)

    def fail_events_to_text(*args, **kwargs):
        raise AssertionError("legacy events_to_text used")

    monkeypatch.setattr(dispatch_service, "events_to_text", fail_events_to_text)
    svc = DispatchService(_cfg(tmp_path), store, pm_agent=None)

    result = await svc.compact("s1", window_tokens=1000)

    assert result["ok"] is True
    assert result["checkpoint_id"]
    assert store.get_context_checkpoints("s1")


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


async def test_compact_failure_is_atomic(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    old_checkpoint = ContextCheckpoint(
        id="old_ctxcp",
        session_id="s1",
        trigger="manual",
        reason="old",
        source_cursor_json=json.dumps({"end": {"event_ts": "2026-07-01T00:00:00Z", "event_id": "e1"}}),
        summary_json=json.dumps({"summary": "old summary"}),
        replacement_history_json=json.dumps(
            {
                "items": [
                    {
                        "id": "old",
                        "role": "system",
                        "kind": "checkpoint_summary",
                        "content": "old summary",
                        "source_refs": ["event:e1"],
                    }
                ]
            }
        ),
        runtime_state_json="{}",
        token_usage_json="{}",
        created_at="2026-07-01T00:00:02Z",
    )
    store.add_context_checkpoint(old_checkpoint)
    store.set_latest_context_checkpoint("s1", "old_ctxcp", plan_summary="old plan")

    async def fail_local(active_context):
        raise RuntimeError("local down")

    with pytest.raises(RuntimeError):
        await ContextManager(store, llm=_FailingLLM(), local_compactor=fail_local).compact_now(
            "s1",
            trigger="manual",
            reason="atomic",
            window_tokens=1000,
        )

    session = store.get_session("s1")
    assert session.latest_context_checkpoint_id == "old_ctxcp"
    assert session.plan == "old plan"
    assert [cp.id for cp in store.get_context_checkpoints("s1") if cp.id != "old_ctxcp"] == []
    failed = [json.loads(e.payload_json) for e in store.get_events("s1") if e.type == "context_compact"]
    assert failed[-1]["status"] == "failed"
    assert ContextManager(store).build_active_context("s1", purpose="pm_plan").degraded is False


async def test_remote_compact_encrypted_content_not_used_as_summary(tmp_path):
    store = _store(tmp_path)
    _seed(store)

    checkpoint = await ContextManager(store, llm=_EncryptedLLM()).compact_now(
        "s1",
        trigger="manual",
        reason="encrypted",
        window_tokens=1000,
    )

    history = json.loads(checkpoint.replacement_history_json)
    summary = json.loads(checkpoint.summary_json)
    assert "SECRET_ENCRYPTED_BLOB" in json.dumps(history["provider_payload"])
    assert summary.get("summary") != "SECRET_ENCRYPTED_BLOB"
    assert "SECRET_ENCRYPTED_BLOB" not in store.get_session("s1").plan


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


async def test_second_compact_preserves_previous_replacement_history_semantics(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="ship feature", workspace="E:/repo"))
    with store.session() as session:
        session.add(
            Event(
                id="e1",
                session_id="s1",
                task_id="t1",
                type="dispatch",
                source="user",
                payload_json=json.dumps({"goal": "ship feature", "workspace": "E:/repo"}),
                ts="2026-07-01T00:00:00Z",
            )
        )
        session.add(
            Event(
                id="e2",
                session_id="s1",
                task_id="t1",
                type="pm_plan",
                source="pm-agent",
                payload_json=json.dumps({"summary": "FIRST_CHECKPOINT_DECISION"}),
                ts="2026-07-01T00:00:01Z",
            )
        )
        session.commit()

    manager = ContextManager(store)
    checkpoint_1 = await manager.compact_now(
        "s1",
        trigger="manual",
        reason="first",
        window_tokens=1000,
    )
    history_1 = json.loads(checkpoint_1.replacement_history_json)
    assert "FIRST_CHECKPOINT_DECISION" in json.dumps(history_1, ensure_ascii=False)

    with store.session() as session:
        session.add(
            Event(
                id="e3",
                session_id="s1",
                task_id="t1",
                type="tool_post",
                source="codex",
                payload_json=json.dumps({"tool": "run_command", "command": "pytest", "exit_code": 0}),
                ts="2026-07-01T00:00:02Z",
            )
        )
        session.add(
            Event(
                id="e4",
                session_id="s1",
                task_id="t1",
                type="stop",
                source="codex",
                payload_json=json.dumps({"result": "SECOND_CHECKPOINT_EVIDENCE"}),
                ts="2026-07-01T00:00:03Z",
            )
        )
        session.commit()

    checkpoint_2 = await manager.compact_now(
        "s1",
        trigger="manual",
        reason="second",
        window_tokens=1000,
    )
    active = manager.build_active_context("s1", purpose="pm_plan")

    assert store.get_session("s1").latest_context_checkpoint_id == checkpoint_2.id
    assert checkpoint_2.id != checkpoint_1.id
    assert "FIRST_CHECKPOINT_DECISION" in active.rendered_text
    assert "SECOND_CHECKPOINT_EVIDENCE" in active.rendered_text
    assert json.loads(checkpoint_2.source_cursor_json)["end"]["event_id"] == "e4"
    assert active.degraded is False


async def test_repeated_compact_preserves_semantics_without_unbounded_growth(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="ship feature", workspace="E:/repo"))

    def add_event(event_id: str, event_type: str, payload: dict, ts: str, *, source: str = "codex") -> None:
        with store.session() as session:
            session.add(
                Event(
                    id=event_id,
                    session_id="s1",
                    task_id="t1",
                    type=event_type,
                    source=source,
                    payload_json=json.dumps(payload),
                    ts=ts,
                )
            )
            session.commit()

    add_event("e1", "dispatch", {"goal": "ship feature", "workspace": "E:/repo"}, "2026-07-01T00:00:00Z", source="user")
    add_event("e2", "pm_plan", {"summary": "FIRST_CHECKPOINT_DECISION"}, "2026-07-01T00:00:01Z", source="pm-agent")
    manager = ContextManager(store)
    checkpoint_1 = await manager.compact_now("s1", trigger="manual", reason="first", window_tokens=1000)

    add_event("e3", "test_result", {"command": "pytest", "status": "passed"}, "2026-07-01T00:00:02Z")
    add_event("e4", "stop", {"result": "SECOND_CHECKPOINT_EVIDENCE"}, "2026-07-01T00:00:03Z")
    checkpoint_2 = await manager.compact_now("s1", trigger="manual", reason="second", window_tokens=1000)

    for index in range(5, 25):
        add_event(
            f"e{index}",
            "pm_reasoning",
            {"text": f"noise {index} " * 80},
            f"2026-07-01T00:00:{index:02d}Z",
            source="pm-agent",
        )
    add_event("e25", "file_change", {"changed_files": ["src/app.py"]}, "2026-07-01T00:00:25Z")
    add_event("e26", "stop", {"result": "THIRD_CHECKPOINT_EVIDENCE"}, "2026-07-01T00:00:26Z")
    checkpoint_3 = await manager.compact_now("s1", trigger="manual", reason="third", window_tokens=1000)
    active = manager.build_active_context("s1", purpose="pm_plan")

    history_1 = json.loads(checkpoint_1.replacement_history_json)["items"]
    history_2 = json.loads(checkpoint_2.replacement_history_json)["items"]
    history_3 = json.loads(checkpoint_3.replacement_history_json)["items"]
    token_usage = json.loads(checkpoint_3.token_usage_json)

    assert store.get_session("s1").latest_context_checkpoint_id == checkpoint_3.id
    assert "FIRST_CHECKPOINT_DECISION" in active.rendered_text
    assert "SECOND_CHECKPOINT_EVIDENCE" in active.rendered_text
    assert "THIRD_CHECKPOINT_EVIDENCE" in active.rendered_text
    assert "noise 5" not in json.dumps(history_3, ensure_ascii=False)
    assert len(history_3) <= len(history_2) + 8
    assert len(history_3) < len(history_1) + 30
    assert token_usage["after_tokens"] <= token_usage["before_tokens"]
    assert active.degraded is False


async def test_post_install_restore_failure_warns_without_losing_checkpoint(tmp_path):
    store = _store(tmp_path)
    _seed(store)

    class DegradedAfterInstallManager(ContextManager):
        def __init__(self, target_store):
            super().__init__(target_store)
            self.build_calls = 0

        def build_active_context(self, session_id: str, *, purpose: str, window_tokens: int = 0):
            self.build_calls += 1
            if self.build_calls > 1:
                return ActiveContext(
                    session_id=session_id,
                    purpose=purpose,
                    rendered_text="degraded restore",
                    degraded=True,
                )
            return super().build_active_context(session_id, purpose=purpose, window_tokens=window_tokens)

    manager = DegradedAfterInstallManager(store)
    checkpoint = await manager.compact_now("s1", trigger="manual", reason="restore-warning", window_tokens=1000)
    session = store.get_session("s1")
    payloads = [json.loads(event.payload_json) for event in store.get_events("s1") if event.type == "context_compact"]

    assert session.latest_context_checkpoint_id == checkpoint.id
    assert payloads[-2]["status"] == "completed"
    assert payloads[-2]["checkpoint_id"] == checkpoint.id
    assert payloads[-1]["status"] == "warning"
    assert payloads[-1]["checkpoint_id"] == checkpoint.id
    assert payloads[-1]["warning"] == "post_install_restore_degraded"
