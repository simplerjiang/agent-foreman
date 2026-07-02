from __future__ import annotations

import json
from dataclasses import dataclass

from foreman.client.agents.base import detect_git_refs
from foreman.client.core.context_v2 import ContextManager, extract_runtime_state
from foreman.client.store import Store
from foreman.client.store.models import Event, Session, Task


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "subagents.db"))
    store.init()
    return store


def _event(
    event_id: str,
    event_type: str,
    payload: dict,
    *,
    ts: str,
    source: str = "codex",
) -> Event:
    return Event(
        id=event_id,
        session_id="s1",
        task_id="t1",
        type=event_type,
        source=source,
        payload_json=json.dumps(payload, ensure_ascii=False),
        ts=ts,
    )


def _add_event(store: Store, event: Event) -> None:
    with store.session() as session:
        session.add(event)
        session.commit()


def test_multi_agent_multi_worktree_runtime_state_active_agents(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/main"))
    _add_event(
        store,
        _event(
            "e1",
            "agent_start",
            {
                "handle_id": "h-dev",
                "agent_id": "h-dev",
                "agent_type": "codex",
                "pid": 101,
                "cwd": "E:/wt-dev",
                "worktree": "E:/wt-dev",
                "branch": "dev/context",
                "native_session_id": "codex-native",
                "status": "running",
            },
            ts="2026-07-01T00:00:00Z",
        ),
    )
    _add_event(
        store,
        _event(
            "e2",
            "agent_start",
            {
                "handle_id": "h-test",
                "agent_id": "h-test",
                "agent_type": "claude-code",
                "pid": 202,
                "cwd": "E:/wt-test",
                "worktree": "E:/wt-test",
                "branch": "test/context",
                "status": "running",
            },
            ts="2026-07-01T00:00:01Z",
            source="claude-code",
        ),
    )

    frames = ContextManager(store).materialize_session("s1")
    state = extract_runtime_state(session, frames)

    by_id = {agent["agent_id"]: agent for agent in state.active_agents}
    assert by_id["h-dev"]["worktree"] == "E:/wt-dev"
    assert by_id["h-dev"]["branch"] == "dev/context"
    assert by_id["h-dev"]["native_session_id"] == "codex-native"
    assert by_id["h-test"]["worktree"] == "E:/wt-test"
    assert by_id["h-test"]["branch"] == "test/context"


def test_detect_git_refs_returns_empty_for_non_git_workspace(tmp_path):
    assert detect_git_refs(tmp_path) == {"branch": "", "head_sha": "", "base_ref": ""}


def test_agent_input_captures_instruction_expected_output_and_worktree(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/main"))
    _add_event(
        store,
        _event(
            "e1",
            "agent_input",
            {
                "agent_id": "h-dev",
                "agent_type": "codex",
                "instruction": "fix failing tests",
                "expected_output": "green pytest",
                "cwd": "E:/wt-dev",
                "worktree": "E:/wt-dev",
                "branch": "fix/tests",
            },
            ts="2026-07-01T00:00:00Z",
            source="pm-agent",
        ),
    )

    frames = ContextManager(store).materialize_session("s1")
    state = extract_runtime_state(session, frames)
    agent = state.active_agents[0]

    assert frames[0].type == "agent_input"
    assert agent["agent_id"] == "h-dev"
    assert agent["worktree"] == "E:/wt-dev"
    assert agent["branch"] == "fix/tests"
    assert agent["last_meaningful_output"]["payload"]["expected_output"] == "green pytest"


@dataclass
class _Handle:
    id: str
    session_id: str
    pid: int
    cwd: str
    worktree: str
    branch: str = ""
    native_session_id: str = ""
    model: str = ""
    effort: str = ""
    status: str = ""
    command: list[str] | None = None


class _Runner:
    def __init__(self, handle, watcher=None):
        self.handles = {handle.id: handle}
        self.process_watcher = watcher

    def handle_for_session(self, session_id):
        return next((h for h in self.handles.values() if h.session_id == session_id), None)


class _Watcher:
    def __init__(self, alive):
        self.alive = alive

    def poll(self, _key, _pid):
        return type("Status", (), {"alive": self.alive, "active": False})()


def test_missing_agent_stop_live_runner_handle_is_running_or_unknown(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    handle = _Handle(
        id="h-live",
        session_id="s1",
        pid=303,
        cwd="E:/wt-live",
        worktree="E:/wt-live",
        command=["codex", "exec"],
    )

    state = extract_runtime_state(session, [], runner=_Runner(handle, _Watcher(True)))

    assert state.active_agents[0]["agent_id"] == "h-live"
    assert state.active_agents[0]["status"] == "running"
    assert state.active_agents[0]["process_status"] == "alive"


def test_completed_handle_not_resurrected_as_running_without_watcher(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    _add_event(
        store,
        _event(
            "e1",
            "stop",
            {"agent_id": "h-dev", "status": "completed", "summary": "done"},
            ts="2026-07-01T00:00:00Z",
        ),
    )
    frames = ContextManager(store).materialize_session("s1")
    handle = _Handle(id="h-dev", session_id="s1", pid=1, cwd="E:/repo", worktree="E:/repo", status="completed")

    state = extract_runtime_state(session, frames, runner=_Runner(handle))
    agent = state.active_agents[0]

    assert agent["status"] == "completed"
    assert agent["process_status"] in {"", "unknown"}
    assert agent["process_status"] != "alive"


def test_completed_handle_not_resurrected_as_running_with_dead_watcher(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    _add_event(
        store,
        _event("e1", "stop", {"agent_id": "h-dev", "status": "completed"}, ts="2026-07-01T00:00:00Z"),
    )
    frames = ContextManager(store).materialize_session("s1")
    handle = _Handle(id="h-dev", session_id="s1", pid=1, cwd="E:/repo", worktree="E:/repo", status="")

    state = extract_runtime_state(session, frames, runner=_Runner(handle, _Watcher(False)))
    agent = state.active_agents[0]

    assert agent["status"] == "completed"
    assert agent["process_status"] == "dead"


def test_running_handle_with_alive_watcher_is_running(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    handle = _Handle(id="h-live", session_id="s1", pid=1, cwd="E:/repo", worktree="E:/repo", status="running")

    state = extract_runtime_state(session, [], runner=_Runner(handle, _Watcher(True)))
    agent = state.active_agents[0]

    assert agent["status"] == "running"
    assert agent["process_status"] == "alive"


def test_task_terminal_status_can_override_unknown_handle(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    store.add_task(Task(id="t1", session_id="s1", instruction="run", status="failed", agent_handle="h-task"))
    handle = _Handle(id="h-task", session_id="s1", pid=1, cwd="E:/repo", worktree="E:/repo", status="unknown")

    state = ContextManager(store, runner=_Runner(handle)).extract_runtime_state(session, [])
    agent = state.active_agents[0]

    assert agent["task_status"] == "failed"
    assert agent["status"] == "failed"


def test_task_row_status_merges_into_runtime_agent(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    store.add_task(Task(id="t1", session_id="s1", instruction="run", status="running", agent_handle="h-task"))

    state = ContextManager(store).extract_runtime_state(session, [])

    agent = state.active_agents[0]
    assert agent["agent_id"] == "h-task"
    assert agent["handle_id"] == "h-task"
    assert agent["task_status"] == "running"
    assert agent["status"] == "running"


def test_stop_returncode_nonzero_materializes_failed_agent_stop(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    _add_event(store, _event("e1", "stop", {"agent_id": "h-dev", "returncode": 2}, ts="2026-07-01T00:00:00Z"))

    frames = ContextManager(store).materialize_session("s1")
    state = extract_runtime_state(session, frames)
    payload = json.loads(frames[0].payload_json)

    assert payload["status"] == "failed"
    assert state.active_agents[0]["status"] == "failed"


def test_stop_status_completed_returncode_nonzero_prefers_failed(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    _add_event(
        store,
        _event(
            "e1",
            "stop",
            {"agent_id": "h-dev", "status": "completed", "returncode": 2},
            ts="2026-07-01T00:00:00Z",
        ),
    )

    frames = ContextManager(store).materialize_session("s1")
    state = extract_runtime_state(session, frames)

    assert state.active_agents[0]["status"] == "failed"


def test_completed_agent_stop_later_output_updates_latest_evidence(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    _add_event(
        store,
        _event(
            "e1",
            "stop",
            {"agent_id": "h-dev", "status": "completed", "summary": "finished"},
            ts="2026-07-01T00:00:00Z",
        ),
    )
    _add_event(
        store,
        _event(
            "e2",
            "agent_output",
            {"agent_id": "h-dev", "text": "late useful output"},
            ts="2026-07-01T00:00:01Z",
        ),
    )

    frames = ContextManager(store).materialize_session("s1")
    state = extract_runtime_state(session, frames)
    agent = state.active_agents[0]

    assert agent["status"] == "completed"
    assert agent["last_seen_at"] == "2026-07-01T00:00:01Z"
    assert agent["last_meaningful_output"]["type"] == "agent_output"
    assert "late useful output" in agent["last_meaningful_output"]["payload"]["text"]


async def test_compact_restore_preserves_agent_worktree_status_and_native_session(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal", workspace="E:/repo"))
    _add_event(
        store,
        _event(
            "e1",
            "agent_start",
            {
                "agent_id": "h-dev",
                "handle_id": "h-dev",
                "cwd": "E:/wt-dev",
                "worktree": "E:/wt-dev",
                "branch": "feature/context",
                "native_session_id": "native-123",
                "status": "running",
            },
            ts="2026-07-01T00:00:00Z",
        ),
    )
    _add_event(
        store,
        _event(
            "e2",
            "stop",
            {"agent_id": "h-dev", "status": "completed", "summary": "done"},
            ts="2026-07-01T00:00:01Z",
        ),
    )

    manager = ContextManager(store)
    await manager.compact_now("s1", trigger="manual", reason="agent-restore", window_tokens=1000)
    active = manager.build_active_context("s1", purpose="pm_plan")
    agent = next(item for item in active.runtime_state["active_agents"] if item["agent_id"] == "h-dev")

    assert agent["cwd"] == "E:/wt-dev"
    assert agent["worktree"] == "E:/wt-dev"
    assert agent["branch"] == "feature/context"
    assert agent["status"] == "completed"
    assert agent["native_session_id"] == "native-123"
    assert "active_agents" in active.rendered_text
    assert "feature/context" in active.rendered_text
