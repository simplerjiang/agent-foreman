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


# ── Decision & execution loop (see DESIGN.zh-CN.md §6): Operator proposes → Auditor reviews →
# Gate classifies → Decision Card → you approve → checkpoint → execute → (if garbage) undo.


class Action(SQLModel, table=True):
    """A command/action the Operator proposes to execute."""

    id: str = Field(primary_key=True)
    session_id: str = Field(index=True, foreign_key="session.id")
    task_id: str | None = None
    kind: str = ""                 # shell | file_edit | agent_instruction | mcp_tool | ...
    command: str = ""
    rationale: str = ""
    expected_effect: str = ""
    reversible: bool = True        # reversible → can be autonomous; irreversible → must be approved
    status: str = "proposed"       # proposed|audited|carded|approved|rejected|executed|undone
    checkpoint_id: str | None = None
    created_at: str = ""
    executed_at: str = ""


class Audit(SQLModel, table=True):
    """The Auditor LLM's independent verdict on a proposed Action (pre-execution)."""

    id: str = Field(primary_key=True)
    action_id: str = Field(index=True, foreign_key="action.id")
    verdict: str                   # pass | reject
    reasons_json: str = "[]"
    suggestions_json: str = "[]"
    model: str = ""
    ts: str = ""


class DecisionCard(SQLModel, table=True):
    """What gets pushed to PC/phone for a one-tap decision."""

    id: str = Field(primary_key=True)
    action_id: str = Field(index=True, foreign_key="action.id")
    session_id: str = Field(index=True)
    summary: str = ""
    audit_note: str = ""
    options_json: str = "[]"       # the candidate actions offered as buttons
    chosen: str = ""
    decided_at: str = ""
    ts: str = ""


class Checkpoint(SQLModel, table=True):
    """A per-step git snapshot enabling one-click undo (see §6.5)."""

    id: str = Field(primary_key=True)
    session_id: str = Field(index=True, foreign_key="session.id")
    task_id: str | None = None
    step_index: int = 0
    vcs_ref: str = ""              # git commit/stash/tag to reset back to
    label: str = ""
    created_at: str = ""


# ── Extensibility layer: your "secret sauce" — workflows / skills / code standards / QA rubrics.
# Stored in the DB, edited in the UI, and git-ignored, so open-sourcing the engine never leaks them.
# One table holds all four kinds (add a new `kind` to extend — no schema change). See DESIGN.zh-CN.md §11.2.


class Definition(SQLModel, table=True):
    id: str = Field(primary_key=True)
    kind: str = Field(index=True)   # workflow | skill | code_standard | qa_rubric
    name: str = Field(index=True)
    version: int = 1
    status: str = "draft"           # draft | active | archived
    is_active: bool = False         # the currently-active version for (kind, name)
    scope_json: str = "{}"          # when it applies: languages, path globs, triggers
    body: str = ""                  # content: Markdown (skill/standard) or YAML/JSON (workflow/rubric)
    metadata_json: str = "{}"
    created_at: str = ""
    updated_at: str = ""


class DefinitionLink(SQLModel, table=True):
    """Wires a workflow step to the skill/standard/rubric it uses."""

    id: str = Field(primary_key=True)
    from_id: str = Field(index=True, foreign_key="definition.id")  # e.g. a workflow
    to_id: str = Field(foreign_key="definition.id")                # e.g. a skill/standard/rubric
    relation: str                                                  # uses_skill | uses_standard | judged_by
    step_index: int | None = None                                  # which workflow step this belongs to


class WorkflowRun(SQLModel, table=True):
    """Tracks how far a session has progressed through a workflow (hybrid engine, §11.2)."""

    id: str = Field(primary_key=True)
    session_id: str = Field(index=True, foreign_key="session.id")
    workflow_id: str = Field(foreign_key="definition.id")
    step_index: int = 0
    step_status: str = "pending"  # pending | running | qa | passed | failed | blocked
    started_at: str = ""
    ended_at: str = ""


class SchemaVersion(SQLModel, table=True):
    """Single-row-ish table recording the DB schema version, for migrations (§11.1)."""

    version: int = Field(primary_key=True)
    applied_at: str = ""


class ConfigKV(SQLModel, table=True):
    """Runtime-mutable key/value settings (e.g. ui.language). DESIGN §7.1 config_kv / §15."""

    key: str = Field(primary_key=True)
    value: str = ""
