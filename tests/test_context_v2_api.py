from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from foreman.client.core.context_v2 import ContextManager
from foreman.client.core.dispatch_service import DispatchService
from foreman.client.store import Store
from foreman.client.store.models import ContextCheckpoint, Event, Session
from foreman.server.app import create_app
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "context-api.db"))
    store.init()
    return store


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    return cfg


def _seed_session(store: Store, tmp_path, *, session_id: str = "s1") -> None:
    store.add_session(
        Session(
            id=session_id,
            goal="run pytest and summarize",
            workspace=str(tmp_path),
            main_workspace=str(tmp_path),
            status="idle",
        )
    )
    with store.session() as s:
        s.add(
            Event(
                id="e1",
                session_id=session_id,
                task_id="t1",
                type="dispatch",
                source="user",
                payload_json=json.dumps({"goal": "run pytest", "workspace": str(tmp_path)}),
                ts="2026-07-01T00:00:00Z",
            )
        )
        s.add(
            Event(
                id="e2",
                session_id=session_id,
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
                        "cwd": str(tmp_path),
                    }
                ),
                ts="2026-07-01T00:00:01Z",
            )
        )
        s.commit()


def _checkpoint(checkpoint_id: str, *, session_id: str = "s1", created_at: str = "2026-07-01T00:00:02Z") -> ContextCheckpoint:
    return ContextCheckpoint(
        id=checkpoint_id,
        session_id=session_id,
        schema_version=2,
        trigger="manual",
        reason="user_requested",
        method="local",
        source_cursor_json=json.dumps(
            {
                "start": {"event_ts": "2026-07-01T00:00:00Z", "event_id": "e1"},
                "end": {"event_ts": "2026-07-01T00:00:01Z", "event_id": "e2"},
            }
        ),
        input_frame_ids_json=json.dumps(["f1", "f2"]),
        summary_json=json.dumps(
            {
                "summary": "safe summary",
                "provider_payload": {"encrypted_content": "SECRET"},
                "changed_files": ["src/app.py"],
                "last_tests": [{"command": "pytest", "status": "passed", "stdout": "SECRET_STDOUT"}],
            }
        ),
        replacement_history_json=json.dumps(
            {
                "items": [
                    {
                        "role": "system",
                        "kind": "checkpoint_summary",
                        "content": "safe summary",
                        "payload": {"provider_payload": "SECRET_PROVIDER"},
                    }
                ],
                "provider_payload": {"encrypted_content": "SECRET_BLOB"},
            }
        ),
        runtime_state_json=json.dumps({"cwd": "E:/repo", "branch": "feature", "active_agents": []}),
        token_usage_json=json.dumps({"before_tokens": 1200, "after_tokens": 120, "window_tokens": 268000}),
        created_at=created_at,
    )


def _client(store: Store, cfg: Config, *, manager: ContextManager | None = None) -> TestClient:
    dispatcher = DispatchService(cfg, store, context_manager=manager)
    return TestClient(create_app(cfg, store=store, dispatcher=dispatcher))


@pytest.mark.asyncio
async def test_get_context_returns_usage_runtime_and_latest_checkpoint(tmp_path):
    store = _store(tmp_path)
    _seed_session(store, tmp_path)
    manager = ContextManager(store)
    checkpoint = await manager.compact_now("s1", trigger="manual", reason="user_requested", window_tokens=268000)

    res = _client(store, _cfg(tmp_path), manager=manager).get("/api/sessions/s1/context")

    assert res.status_code == 200
    data = res.json()
    assert data["usage"]["used_tokens"] >= 0
    assert data["usage"]["window_tokens"] == 268000
    assert data["runtime_state"]["workspace"] == str(tmp_path)
    assert data["latest_checkpoint"]["id"] == checkpoint.id
    assert data["latest_checkpoint"]["replacement_history_items_count"] > 0


def test_get_context_without_checkpoint_returns_raw_frames_mode(tmp_path):
    store = _store(tmp_path)
    _seed_session(store, tmp_path)

    data = _client(store, _cfg(tmp_path)).get("/api/sessions/s1/context").json()

    assert data["latest_checkpoint"] is None
    assert data["restore_mode"] == "raw_frames"
    assert data["degraded"] is False


def test_get_context_with_corrupted_checkpoint_returns_degraded_warning(tmp_path):
    store = _store(tmp_path)
    _seed_session(store, tmp_path)
    cp = _checkpoint("cp_bad")
    cp.replacement_history_json = "{}"
    store.add_context_checkpoint(cp)
    store.set_latest_context_checkpoint("s1", "cp_bad", plan_summary="bad")

    data = _client(store, _cfg(tmp_path)).get("/api/sessions/s1/context").json()

    assert data["degraded"] is True
    assert data["restore_mode"] == "raw_frames_degraded"
    assert any(w["code"] == "invalid_replacement_history" for w in data["warnings"])


def test_checkpoint_list_returns_latest_first(tmp_path):
    store = _store(tmp_path)
    _seed_session(store, tmp_path)
    store.add_context_checkpoint(_checkpoint("cp1", created_at="2026-07-01T00:00:02Z"))
    store.add_context_checkpoint(_checkpoint("cp2", created_at="2026-07-01T00:00:03Z"))

    data = _client(store, _cfg(tmp_path)).get("/api/sessions/s1/context/checkpoints").json()

    assert [row["id"] for row in data["items"][:2]] == ["cp2", "cp1"]
    assert "replacement_history_json" not in json.dumps(data)


def test_checkpoint_detail_excludes_provider_payload_and_encrypted_content(tmp_path):
    store = _store(tmp_path)
    _seed_session(store, tmp_path)
    store.add_context_checkpoint(_checkpoint("cp_secret"))

    res = _client(store, _cfg(tmp_path)).get("/api/sessions/s1/context/checkpoints/cp_secret")
    body = json.dumps(res.json())

    assert res.status_code == 200
    assert "provider_payload" not in body
    assert "encrypted_content" not in body
    assert "SECRET" not in body
    assert "replacement_history_json" not in body


@pytest.mark.asyncio
async def test_manual_compact_writes_checkpoint_and_updates_latest_pointer(tmp_path):
    store = _store(tmp_path)
    _seed_session(store, tmp_path)

    res = _client(store, _cfg(tmp_path)).post(
        "/api/sessions/s1/context/compact",
        json={"trigger": "manual", "reason": "user_requested"},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    checkpoint_id = data["checkpoint"]["id"]
    assert store.get_session("s1").latest_context_checkpoint_id == checkpoint_id
    statuses = [json.loads(e.payload_json).get("status") for e in store.get_events("s1") if e.type == "context_compact"]
    assert "started" in statuses
    assert "completed" in statuses


@pytest.mark.asyncio
async def test_manual_compact_failure_does_not_update_pointer(tmp_path):
    store = _store(tmp_path)
    _seed_session(store, tmp_path)
    store.add_context_checkpoint(_checkpoint("cp_old"))
    store.set_latest_context_checkpoint("s1", "cp_old", plan_summary="old")

    async def fail_compactor(_active):
        raise RuntimeError("compact broke")

    manager = ContextManager(store, local_compactor=fail_compactor)
    res = _client(store, _cfg(tmp_path), manager=manager).post(
        "/api/sessions/s1/context/compact",
        json={"trigger": "manual", "reason": "user_requested"},
    )

    data = res.json()
    assert data["ok"] is False
    assert data["latest_checkpoint_unchanged"] is True
    assert store.get_session("s1").latest_context_checkpoint_id == "cp_old"
    statuses = [json.loads(e.payload_json).get("status") for e in store.get_events("s1") if e.type == "context_compact"]
    assert "failed" in statuses


@pytest.mark.asyncio
async def test_manual_compact_returns_structured_error(tmp_path):
    store = _store(tmp_path)
    _seed_session(store, tmp_path)

    async def fail_compactor(_active):
        raise RuntimeError("compact broke")

    res = _client(store, _cfg(tmp_path), manager=ContextManager(store, local_compactor=fail_compactor)).post(
        "/api/sessions/s1/context/compact",
        json={"trigger": "manual", "reason": "user_requested"},
    )

    data = res.json()
    assert data["ok"] is False
    assert data["error"]["code"] == "context_compact_failed"
    assert data["error"]["hard"] is False
