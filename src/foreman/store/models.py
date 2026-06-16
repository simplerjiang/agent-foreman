"""SQLModel tables. See docs/DESIGN.zh-CN.md §7 for the data model."""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class Session(SQLModel, table=True):
    id: str = Field(primary_key=True)
    goal: str
    plan: str = ""
    status: str = "planning"  # planning|running|idle|blocked|waiting_approval|done|failed|paused
    workspace: str = ""
    agent_type: str = ""  # claude-code|codex
    created_at: str = ""
    updated_at: str = ""


class Task(SQLModel, table=True):
    id: str = Field(primary_key=True)
    session_id: str = Field(index=True, foreign_key="session.id")
    instruction: str
    status: str = "pending"  # pending|running|done|failed|cancelled
    agent_handle: str = ""
    created_at: str = ""
    updated_at: str = ""


class Event(SQLModel, table=True):
    id: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    task_id: str | None = None
    type: str  # agent_output|tool_pre|tool_post|stop|git_diff|review|approval_req|...
    source: str  # claude-code|codex|hook|git|process
    payload_json: str = "{}"
    ts: str = ""


class Review(SQLModel, table=True):
    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    verdict: str  # approve|request_changes|escalate
    summary: str = ""
    risks_json: str = "[]"
    suggestions_json: str = "[]"
    needs_human: bool = False
    ts: str = ""


class Approval(SQLModel, table=True):
    id: str = Field(primary_key=True)
    session_id: str = Field(index=True)
    task_id: str | None = None
    action: str = ""
    risk_level: str = "requires-approval"
    diff_summary: str = ""
    status: str = "pending"  # pending|approved|rejected|expired
    reason: str = ""
    nonce: str = ""
    requested_at: str = ""
    decided_at: str = ""


class Report(SQLModel, table=True):
    id: str = Field(primary_key=True)
    session_id: str | None = None
    kind: str = "active-briefing"  # handoff|active-briefing|daily
    title: str = ""
    body_md: str = ""
    sent: bool = False
    ts: str = ""


class PushSubscription(SQLModel, table=True):
    id: str = Field(primary_key=True)
    endpoint: str
    p256dh: str
    auth: str
    ua: str = ""
    created_at: str = ""
