"""`foreman dispatch` core — create a Session+Task and run an agent to completion.

Lives in the client package (not __main__) so `foreman serve` (the server) never imports client
code. See docs/DESIGN.zh-CN.md §4.2 / §7.1.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from foreman.shared.config import Config
from foreman.shared.events import EventBus, utc_now_iso

from .agents.runner import Runner
from .store import Store
from .store.models import Session, Task


def build_session_task(store: Store, task: str, workspace: str, agent: str) -> tuple[Session, Task]:
    """Create + persist a Root Session and its first Task (DESIGN §7.1)."""
    now = utc_now_iso()
    session = Session(
        id=uuid.uuid4().hex,
        goal=task,
        workspace=str(workspace),
        agent_type=agent,
        status="running",
        created_at=now,
        updated_at=now,
    )
    store.add_session(session)
    task_row = Task(
        id=uuid.uuid4().hex,
        session_id=session.id,
        instruction=task,
        status="running",
        created_at=now,
        updated_at=now,
    )
    store.add_task(task_row)
    return session, task_row


async def run_dispatch(
    cfg: Config,
    task: str,
    workspace: str,
    agent: str,
    model: str = "",
    effort: str = "",
    *,
    store: Store | None = None,
    bus: EventBus | None = None,
    runner: Runner | None = None,
) -> tuple[str, int]:
    """Build a session+task and run the agent to completion; return (session_id, n_events).

    store/bus/runner are injectable for tests (so real claude/codex is never spawned in CI).
    """
    if store is None:
        store = Store(cfg.store.db_path)
        store.init()
    bus = bus or EventBus()
    runner = runner or Runner(cfg, bus, store)
    session, _task = build_session_task(store, task, workspace, agent)
    try:
        handle = await runner.launch(agent, task, Path(workspace), session.id, model=model, effort=effort)
        await runner.wait(handle)
    except Exception:
        store.update_session(session.id, status="failed", updated_at=utc_now_iso())
        raise
    store.update_session(session.id, status="done", updated_at=utc_now_iso())
    return session.id, len(store.get_events(session.id))
