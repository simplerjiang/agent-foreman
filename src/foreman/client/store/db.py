"""Local (client) SQLite store: engine, session, and r/w helpers for the PM Core.

Holds sessions / tasks / events and the 秘方 definitions (DESIGN §7.1). See models.py.
"""

from __future__ import annotations

import json
import uuid

from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, col, create_engine, select

from foreman.shared.events import AgentEvent, utc_now_iso

from .models import Checkpoint, ConfigKV, Event, SchemaVersion, Session, Task

SCHEMA_VERSION = 1


class Store:
    def __init__(self, db_path: str = "foreman.db") -> None:
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )

    def init(self) -> None:
        """Create tables if absent and record the current schema version (DESIGN §11.1)."""
        SQLModel.metadata.create_all(self.engine)
        with self.session() as s:
            if s.get(SchemaVersion, SCHEMA_VERSION) is None:
                s.add(SchemaVersion(version=SCHEMA_VERSION, applied_at=utc_now_iso()))
                s.commit()

    def session(self) -> DBSession:
        # expire_on_commit=False: returned ORM rows stay readable after the session closes.
        return DBSession(self.engine, expire_on_commit=False)

    # ── sessions / tasks ───────────────────────────────────────────────────────────────────
    def add_session(self, session: Session) -> Session:
        with self.session() as s:
            s.add(session)
            s.commit()
        return session

    def get_sessions(self) -> list[Session]:
        with self.session() as s:
            return list(s.exec(select(Session)).all())

    def add_task(self, task: Task) -> Task:
        with self.session() as s:
            s.add(task)
            s.commit()
        return task

    # ── events ─────────────────────────────────────────────────────────────────────────────
    def add_event(self, event: AgentEvent) -> Event:
        """Persist an AgentEvent as an Event row (payload serialized to JSON)."""
        row = Event(
            id=uuid.uuid4().hex,
            session_id=event.session_id,
            task_id=event.task_id,
            type=event.type,
            source=event.source,
            payload_json=json.dumps(event.payload),
            ts=event.ts or utc_now_iso(),
        )
        with self.session() as s:
            s.add(row)
            s.commit()
        return row

    def get_events(self, session_id: str) -> list[Event]:
        with self.session() as s:
            return list(
                s.exec(
                    select(Event).where(Event.session_id == session_id).order_by(Event.ts)
                ).all()
            )

    # ── checkpoints (§6.5 / §7.1) ────────────────────────────────────────────────────────────
    def add_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        """Record a per-step git snapshot so the undo timeline can list/replay it (§6.5)."""
        with self.session() as s:
            s.add(checkpoint)
            s.commit()
        return checkpoint

    def get_checkpoints(self, session_id: str) -> list[Checkpoint]:
        """This session's checkpoints, oldest step first (the undo timeline)."""
        with self.session() as s:
            return list(
                s.exec(
                    select(Checkpoint)
                    .where(Checkpoint.session_id == session_id)
                    .order_by(col(Checkpoint.step_index))
                ).all()
            )

    # ── settings (config_kv) ─────────────────────────────────────────────────────────────────
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.session() as s:
            row = s.get(ConfigKV, key)
            return row.value if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self.session() as s:
            row = s.get(ConfigKV, key)
            if row is None:
                s.add(ConfigKV(key=key, value=value))
            else:
                row.value = value
                s.add(row)
            s.commit()
