from __future__ import annotations

import json
from dataclasses import dataclass

from foreman.client.core.context_v2 import ContextManager, extract_runtime_state, runtime_state_dict
from foreman.client.store import Store
from foreman.client.store.models import Event, Session


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "runtime.db"))
    store.init()
    return store


def _event(
    event_id: str,
    event_type: str,
    payload: dict,
    *,
    ts: str = "2026-07-01T00:00:00Z",
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


def test_agent_start_with_cwd_worktree_branch_becomes_runtime_state(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal"))
    _add_event(
        store,
        _event(
            "e1",
            "agent_start",
            {
                "agent_id": "dev-1",
                "cwd": "E:/worktree",
                "worktree": "E:/worktree",
                "branch": "codex/context-v2",
                "pid": 42,
                "model": "gpt-test",
                "effort": "high",
            },
        )
    )
    manager = ContextManager(store)
    frames = manager.materialize_session("s1")

    state = manager.extract_runtime_state(session, frames)
    data = runtime_state_dict(state)

    assert data["cwd"] == "E:/worktree"
    assert data["worktree"] == "E:/worktree"
    assert data["branch"] == "codex/context-v2"
    assert data["head_sha"] == ""
    assert data["active_agents"][0]["agent_id"] == "dev-1"
    assert data["active_agents"][0]["status"] == "running"
    assert data["active_agents"][0]["pid"] == 42
    assert data["active_agents"][0]["native_session_id"] == ""


def test_runtime_state_merges_agent_stop_and_last_command(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal"))
    _add_event(
        store,
        _event(
            "e1",
            "agent_start",
            {"agent_id": "dev-1", "cwd": "E:/worktree", "branch": "feature"},
            ts="2026-07-01T00:00:00Z",
        )
    )
    _add_event(
        store,
        _event(
            "e2",
            "tool_post",
            {
                "tool": "run_command",
                "call_id": "cmd-1",
                "ok": True,
                "result": {
                    "ok": True,
                    "data": {"command": "pytest", "returncode": 0, "stdout": "1 passed"},
                },
            },
            ts="2026-07-01T00:00:01Z",
        )
    )
    _add_event(
        store,
        _event(
            "e3",
            "stop",
            {"agent_id": "dev-1", "hook": "SubagentStop"},
            ts="2026-07-01T00:00:02Z",
        )
    )
    frames = ContextManager(store).materialize_session("s1")

    state = extract_runtime_state(session, frames)

    dev_agent = next(agent for agent in state.active_agents if agent["agent_id"] == "dev-1")
    assert dev_agent["status"] == "completed"
    assert state.last_commands[-1]["command"] == "pytest"
    assert state.last_commands[-1]["exit_code"] == 0


@dataclass
class _Handle:
    id: str
    session_id: str
    pid: int
    cwd: str
    native_session_id: str = ""
    model: str = ""
    effort: str = ""
    command: list[str] | None = None


class _Runner:
    def __init__(self, handle):
        self.handles = {handle.id: handle}
        self._handle = handle

    def handle_for_session(self, session_id):
        return self._handle if session_id == self._handle.session_id else None


def test_runtime_state_merges_runner_handle_without_inventing_branch_or_native_session(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(Session(id="s1", goal="goal"))
    handle = _Handle(
        id="s1:123",
        session_id="s1",
        pid=123,
        cwd="E:/runner-worktree",
        model="gpt-test",
        effort="medium",
        command=["codex", "exec"],
    )

    state = extract_runtime_state(session, [], runner=_Runner(handle))

    agent = state.active_agents[0]
    assert agent["agent_id"] == "s1:123"
    assert agent["status"] == "running"
    assert agent["cwd"] == "E:/runner-worktree"
    assert agent["model"] == "gpt-test"
    assert agent["effort"] == "medium"
    assert agent["branch"] == ""
    assert agent["native_session_id"] == ""
    assert state.branch == ""
    assert state.head_sha == ""


def test_runtime_state_prefers_observable_session_workspace_without_frames(tmp_path):
    store = _store(tmp_path)
    session = store.add_session(
        Session(id="s1", goal="goal", workspace="E:/repo", main_workspace="E:/main")
    )

    state = extract_runtime_state(session, [])

    assert state.cwd == "E:/repo"
    assert state.worktree == "E:/repo"
    assert state.workspace == "E:/repo"
    assert state.main_workspace == "E:/main"
