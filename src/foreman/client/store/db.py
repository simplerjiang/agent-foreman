"""Local (client) SQLite store: engine, session, and r/w helpers for the PM Core.

Holds sessions / tasks / events and the 秘方 definitions (DESIGN §7.1). See models.py.
"""

from __future__ import annotations

import json
import uuid

from sqlmodel import Session as DBSession
from sqlmodel import SQLModel, col, create_engine, select

from foreman.shared.events import AgentEvent, utc_now_iso

from .models import (
    Action,
    Approval,
    Audit,
    Checkpoint,
    ConfigKV,
    DecisionCard,
    Event,
    PushSubscription,
    Report,
    SchemaVersion,
    Session,
    Task,
)

SCHEMA_VERSION = 1


class Store:
    def __init__(self, db_path: str = "foreman.db") -> None:
        self.engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )

    def init(self) -> None:
        """Create tables if absent and record the current schema version (DESIGN §11.1)."""
        SQLModel.metadata.create_all(self.engine)
        self._ensure_columns()
        with self.session() as s:
            if s.get(SchemaVersion, SCHEMA_VERSION) is None:
                s.add(SchemaVersion(version=SCHEMA_VERSION, applied_at=utc_now_iso()))
                s.commit()

    def _ensure_columns(self) -> None:
        """Add columns that postdate a table's creation (create_all only makes *missing* tables).

        A tiny stop-gap until the real migrator (T5.5): SQLite can't add a column create_all won't,
        so a dev DB whose `decisioncard` table predates `diff_stat` would error on SELECT. Idempotent.
        """
        from sqlalchemy import text

        wanted = {"decisioncard": [("diff_stat", "TEXT NOT NULL DEFAULT ''")]}
        with self.engine.begin() as conn:
            for table, cols in wanted.items():
                existing = {
                    row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
                }
                for name, decl in cols:
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))

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

    def get_session(self, session_id: str) -> Session | None:
        with self.session() as s:
            return s.get(Session, session_id)

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

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self.session() as s:
            return s.get(Checkpoint, checkpoint_id)

    # ── actions / decision cards (decision loop, §6.1/§6.3/§7.1) ──────────────────────────────
    def add_action(self, action: Action) -> Action:
        """Record an Operator-proposed action (its checkpoint anchors the step-detail diff, §6.3)."""
        with self.session() as s:
            s.add(action)
            s.commit()
        return action

    def get_action(self, action_id: str) -> Action | None:
        with self.session() as s:
            return s.get(Action, action_id)

    def update_action(
        self,
        action_id: str,
        *,
        status: str | None = None,
        checkpoint_id: str | None = None,
        executed_at: str | None = None,
    ) -> Action | None:
        """Advance an action through the decision loop (proposed→audited→carded→executed, §6.2).

        Only the passed fields are written, so callers can stamp a checkpoint at execute-time
        without clobbering the status set earlier. None if the action id is unknown."""
        with self.session() as s:
            row = s.get(Action, action_id)
            if row is None:
                return None
            if status is not None:
                row.status = status
            if checkpoint_id is not None:
                row.checkpoint_id = checkpoint_id
            if executed_at is not None:
                row.executed_at = executed_at
            s.add(row)
            s.commit()
        return row

    def add_audit(self, audit: Audit) -> Audit:
        """Record the Auditor's independent pre-execution verdict on an action (§6.7 / §7.1)."""
        with self.session() as s:
            s.add(audit)
            s.commit()
        return audit

    def get_audits(self, action_id: str) -> list[Audit]:
        """The audit verdict(s) recorded for an action (newest first)."""
        with self.session() as s:
            return list(
                s.exec(
                    select(Audit)
                    .where(Audit.action_id == action_id)
                    .order_by(col(Audit.ts).desc())
                ).all()
            )

    def add_decision_card(self, card: DecisionCard) -> DecisionCard:
        """Record a decision card pushed to PC/phone for a one-tap decision (§6.3)."""
        with self.session() as s:
            s.add(card)
            s.commit()
        return card

    def get_decision_card(self, card_id: str) -> DecisionCard | None:
        with self.session() as s:
            return s.get(DecisionCard, card_id)

    def get_decision_cards(self, session_id: str | None = None) -> list[DecisionCard]:
        """Decision cards, newest first (optionally scoped to one session) — the phone's card feed."""
        with self.session() as s:
            stmt = select(DecisionCard)
            if session_id is not None:
                stmt = stmt.where(DecisionCard.session_id == session_id)
            return list(s.exec(stmt.order_by(col(DecisionCard.ts).desc())).all())

    def set_card_choice(
        self, card_id: str, *, chosen: str, decided_at: str
    ) -> DecisionCard | None:
        """Record which option the human tapped on a card (§6.3). None if the card is unknown."""
        with self.session() as s:
            row = s.get(DecisionCard, card_id)
            if row is None:
                return None
            row.chosen = chosen
            row.decided_at = decided_at
            s.add(row)
            s.commit()
        return row

    # ── approvals (Gate, DESIGN §6.6 / §7.1) ─────────────────────────────────────────────────
    def add_approval(self, approval: Approval) -> Approval:
        """Record a pending approval the Gate is holding a dangerous action on (§6.6)."""
        with self.session() as s:
            s.add(approval)
            s.commit()
        return approval

    def get_approval(self, approval_id: str) -> Approval | None:
        with self.session() as s:
            return s.get(Approval, approval_id)

    def get_pending_approvals(self) -> list[Approval]:
        """Approvals still waiting on the human (oldest first) — the phone's approval queue."""
        with self.session() as s:
            return list(
                s.exec(
                    select(Approval)
                    .where(Approval.status == "pending")
                    .order_by(col(Approval.requested_at))
                ).all()
            )

    def decide_approval(
        self, approval_id: str, *, status: str, reason: str, decided_at: str
    ) -> Approval | None:
        """Apply an approve/reject decision IFF the approval is still pending (one-shot).

        Returns the updated row, or None when the id is unknown OR already decided — the
        caller treats "already decided" as a replay and refuses it (DESIGN §6.8 nonce/replay)."""
        with self.session() as s:
            row = s.get(Approval, approval_id)
            if row is None or row.status != "pending":
                return None
            row.status = status
            row.reason = reason
            row.decided_at = decided_at
            s.add(row)
            s.commit()
        return row

    # ── reports / briefings (DESIGN §4.1 Briefing / §5.5 / §7.1) ─────────────────────────────
    def add_report(self, report: Report) -> Report:
        """Persist a briefing (handoff | active-briefing | daily) for the phone (§5.5)."""
        with self.session() as s:
            s.add(report)
            s.commit()
        return report

    def get_reports(self, session_id: str | None = None) -> list[Report]:
        """Briefings newest first (optionally scoped to one session) — the phone's briefing feed."""
        with self.session() as s:
            stmt = select(Report)
            if session_id is not None:
                stmt = stmt.where(Report.session_id == session_id)
            return list(s.exec(stmt.order_by(col(Report.ts).desc())).all())

    # ── push subscriptions (Web Push, DESIGN §4.6 / §7.1) ────────────────────────────────────
    def add_push_subscription(
        self, *, endpoint: str, p256dh: str, auth: str, ua: str = ""
    ) -> PushSubscription:
        """Store (or refresh) a browser push subscription, keyed by its endpoint (one row per
        browser). Re-subscribing with the same endpoint updates the keys rather than duplicating.

        Takes primitives, not a model, so the server app.py (shared-only) can call it without
        importing client models (DESIGN §14)."""
        with self.session() as s:
            row = s.exec(
                select(PushSubscription).where(PushSubscription.endpoint == endpoint)
            ).first()
            if row is None:
                row = PushSubscription(
                    id=uuid.uuid4().hex,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    ua=ua,
                    created_at=utc_now_iso(),
                )
            else:
                row.p256dh = p256dh
                row.auth = auth
                row.ua = ua
            s.add(row)
            s.commit()
        return row

    def get_push_subscriptions(self) -> list[PushSubscription]:
        with self.session() as s:
            return list(
                s.exec(
                    select(PushSubscription).order_by(col(PushSubscription.created_at))
                ).all()
            )

    def delete_push_subscription(self, endpoint: str) -> None:
        """Drop a subscription (user unsubscribed, or the push service returned 404/410)."""
        with self.session() as s:
            row = s.exec(
                select(PushSubscription).where(PushSubscription.endpoint == endpoint)
            ).first()
            if row is not None:
                s.delete(row)
                s.commit()

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
