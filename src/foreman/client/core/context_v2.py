"""Context v2 frame materialization and runtime-state helpers.

This module is deliberately not wired into PM plan/review yet. Raw Event rows remain the source of
truth; ContextFrame rows are deterministic, replayable active-context material derived from them.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from foreman.shared.events import make_event, utc_now_iso

from .context_compression import extract_json_object, memory_items_from_pack
from .pm_contract import PlanContract
from ..store.models import ContextCheckpoint, ContextFrame, ContextSnapshot, Event, MemoryItem, Session, Task

LANE_SYSTEM = 1
LANE_TASK = 2
LANE_RUNTIME = 3
LANE_PLAN = 4
LANE_MEMORY = 5
LANE_DETAIL = 6
LANE_NOISE = 7
LANES = (1, 2, 3, 4, 5, 6, 7)

MAX_TEXT_CHARS = 1200
SUMMARY_EDGE_CHARS = 360
IMPORTANT_LINE_LIMIT = 12


@dataclass
class ReplacementHistoryItem:
    id: str
    type: str
    role: str
    kind: str
    content: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    frame_ids: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    agent_id: str = ""
    tool_call_id: str = ""
    model_visible: bool = True
    created_at: str = ""
    schema: str = "foreman.active_history.item.v1"


@dataclass
class RuntimeState:
    session_id: str = ""
    goal: str = ""
    workspace: str = ""
    main_workspace: str = ""
    cwd: str = ""
    worktree: str = ""
    branch: str = ""
    base_ref: str = ""
    head_sha: str = ""
    active_agents: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    last_tests: list[dict[str, Any]] = field(default_factory=list)
    last_commands: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ContextUsage:
    used_tokens: int = 0
    window_tokens: int = 0
    percent: float = 0.0
    tokens_until_soft_compact: int = 0
    tokens_until_hard_compact: int = 0
    soft_threshold: float = 0.70
    hard_threshold: float = 0.90
    run_count_threshold: int = 8
    lane_usage: dict[str, int] = field(default_factory=lambda: {str(lane): 0 for lane in LANES})


@dataclass
class ContextRestoreWarning:
    code: str
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActiveContext:
    session_id: str = ""
    purpose: str = ""
    envelope: dict[str, Any] = field(default_factory=dict)
    stable_prefix: list[dict[str, Any]] = field(default_factory=list)
    replacement_history: list[dict[str, Any]] = field(default_factory=list)
    frames_after_checkpoint: list[dict[str, Any]] = field(default_factory=list)
    runtime_state: dict[str, Any] = field(default_factory=dict)
    source_cursor: dict[str, Any] = field(default_factory=dict)
    token_usage: dict[str, Any] = field(default_factory=dict)
    degraded: bool = False
    warnings: list[dict[str, Any]] = field(default_factory=list)
    rendered_text: str = ""


class ContextCompactError(RuntimeError):
    """Raised when Context v2 compaction cannot produce a safe checkpoint."""


class ContextManager:
    """Thin Store-backed facade for Context v2 replay and runtime-state extraction."""

    def __init__(
        self,
        store: Any,
        *,
        runner: Any = None,
        clock=None,
        llm: Any = None,
        local_compactor: Any = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self._clock = clock or utc_now_iso
        self.llm = llm
        self.local_compactor = local_compactor

    def record_event(self, session_id: str, event: Event | Any) -> list[ContextFrame]:
        event_session_id = _event_attr(event, "session_id")
        target_session_id = _text(session_id)
        if event_session_id and target_session_id and event_session_id != target_session_id:
            raise ValueError("event_session_mismatch")
        if not event_session_id and not target_session_id:
            return []
        frames = materialize_event(event, session_id_override=target_session_id or None)
        if frames and hasattr(self.store, "add_context_frames"):
            self.store.add_context_frames(frames)
        return frames

    def materialize_session(self, session_id: str, force: bool = False) -> list[ContextFrame]:
        if not hasattr(self.store, "get_events") or not hasattr(self.store, "add_context_frames"):
            return []
        events = self.store.get_events(session_id)
        frames: list[ContextFrame] = []
        for event in events:
            frames.extend(materialize_event(event))
        if frames:
            self.store.add_context_frames(frames)
        return self.store.get_context_frames(session_id) if hasattr(self.store, "get_context_frames") else frames

    def extract_runtime_state(
        self,
        session: Session,
        frames: list[ContextFrame] | None = None,
        runner: Any = None,
    ) -> RuntimeState:
        return extract_runtime_state(
            session,
            frames or [],
            runner=runner or self.runner,
            tasks=_tasks_for_session(self.store, session.id),
        )

    def build_active_context(
        self,
        session_id: str,
        *,
        purpose: str,
        window_tokens: int = 0,
    ) -> ActiveContext:
        return self.restore_from_latest_checkpoint(
            session_id,
            purpose=purpose,
            window_tokens=window_tokens,
        )

    async def compact_now(
        self,
        session_id: str,
        *,
        trigger: str,
        reason: str,
        window_tokens: int,
        hard: bool = False,
    ) -> ContextCheckpoint:
        method_attempted = "local"
        before_plan = ""
        session = self.store.get_session(session_id) if hasattr(self.store, "get_session") else None
        if session is None:
            raise ContextCompactError("session_not_found")
        before_plan = _text(getattr(session, "plan", ""))
        try:
            active = self.build_active_context(
                session_id,
                purpose="compact",
                window_tokens=window_tokens,
            )
            before_tokens = _approx_tokens(active.rendered_text)
            method = "local"
            provider_payload: dict[str, Any] = {}
            remote_summary: dict[str, Any] = {}
            remote_error = ""
            if self.llm is not None and hasattr(self.llm, "responses_compact"):
                method_attempted = "remote"
                try:
                    provider_payload = await self.llm.responses_compact(
                        _compact_input_items(active),
                        instructions=_compact_instructions(),
                        metadata={"session_id": session_id, "trigger": trigger, "reason": reason},
                    )
                    remote_summary = _summary_from_provider_payload(provider_payload)
                    method = "remote"
                except Exception as exc:  # noqa: BLE001 - local fallback is the contract.
                    remote_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                    method = "local"
            if method == "local" and self.local_compactor is not None:
                method_attempted = "local"
                raw_summary = await self.local_compactor(active)
                remote_summary = _summary_from_local_text(raw_summary)
            summary_json = _summary_json(active, remote_summary, method=method, reason=reason)
            replacement_history = frames_to_replacement_history(active, summary_json=summary_json)
            valid_items, item_errors = _validate_replacement_history_items(
                replacement_history.get("items", [])
            )
            if item_errors or not valid_items:
                raise ContextCompactError("empty_replacement_history")
            replacement_history["items"] = valid_items
            source_cursor = _source_cursor_from_active_context(active)
            frame_ids = [
                _text(frame.get("id"))
                for frame in active.frames_after_checkpoint
                if isinstance(frame, dict) and _text(frame.get("id"))
            ]
            summary_text = _compat_summary_text(summary_json)
            after_tokens = _approx_tokens(summary_text)
            token_usage = {
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "window_tokens": int(window_tokens or 0),
                "method": method,
                "provider": _text(getattr(self.llm, "provider", "")) if self.llm is not None else "",
            }
            if remote_error:
                token_usage["remote_error"] = remote_error
            if provider_payload:
                summary_json["provider_payload"] = _compact_payload(provider_payload)
                replacement_history["provider_payload"] = _compact_payload(provider_payload)
            snapshot_id = self._store_compat_snapshot(session_id, active, summary_text, summary_json)
            checkpoint = ContextCheckpoint(
                id=f"ctxcp_{uuid.uuid4().hex}",
                session_id=session_id,
                schema_version=2,
                trigger=_text(trigger) or "manual",
                reason=_text(reason) or "compact",
                method=method,
                source_cursor_json=_dump(source_cursor),
                input_frame_ids_json=json.dumps(frame_ids, ensure_ascii=False),
                summary_json=_dump(summary_json),
                replacement_history_json=_dump(replacement_history),
                runtime_state_json=_dump(active.runtime_state),
                token_usage_json=_dump(token_usage),
                created_at=self._clock(),
            )
            installed, _event = self.store.install_context_checkpoint(
                session_id,
                checkpoint,
                summary_text,
                {
                    "status": "completed",
                    "schema_version": 2,
                    "hard": bool(hard),
                    "trigger": _text(trigger) or "manual",
                    "reason": _text(reason) or "compact",
                    "checkpoint_id": checkpoint.id,
                    "snapshot_id": snapshot_id,
                    "before_tokens": before_tokens,
                    "after_tokens": after_tokens,
                    "method": method,
                    "source": "pm-agent",
                },
            )
            try:
                restored = self.build_active_context(session_id, purpose="pm_plan", window_tokens=window_tokens)
                if restored.degraded:
                    self._emit_compact_warning(
                        session_id,
                        checkpoint_id=installed.id,
                        warning="post_install_restore_degraded",
                    )
            except Exception as exc:  # noqa: BLE001 - install succeeded; report recoverable warning.
                self._emit_compact_warning(
                    session_id,
                    checkpoint_id=installed.id,
                    warning=f"post_install_restore_failed: {type(exc).__name__}: {str(exc)[:200]}",
                )
            return installed
        except Exception as exc:
            self._emit_compact_failed(
                session_id,
                hard=hard,
                method_attempted=method_attempted,
                error=f"{type(exc).__name__}: {str(exc)[:240]}",
                reason=reason,
            )
            if hasattr(self.store, "update_session") and before_plan:
                current = self.store.get_session(session_id)
                if current is not None and _text(getattr(current, "plan", "")) != before_plan:
                    self.store.update_session(session_id, plan=before_plan, updated_at=self._clock())
            raise

    async def maybe_compact(
        self,
        session_id: str,
        *,
        reason: str,
        purpose: str,
        window_tokens: int,
        run_count: int = 0,
        hard: bool | None = None,
    ) -> ContextCheckpoint | None:
        try:
            active = self.build_active_context(
                session_id,
                purpose=purpose,
                window_tokens=window_tokens,
            )
            usage = estimate_context_usage(active, window_tokens)
        except Exception as exc:  # noqa: BLE001 - restore failure should fall back to legacy context.
            self._emit_compact_warning(
                session_id,
                checkpoint_id="",
                warning=f"maybe_compact_usage_estimate_failed: {type(exc).__name__}: {str(exc)[:200]}",
            )
            return None
        hard_trigger = bool(hard) or should_hard_compact(usage)
        soft_trigger = should_soft_compact(usage, run_count=run_count)
        if not hard_trigger and not soft_trigger:
            return None
        try:
            checkpoint = await self.compact_now(
                session_id,
                trigger="threshold" if hard_trigger or should_soft_compact(usage) else "run_count",
                reason=reason,
                window_tokens=window_tokens,
                hard=hard_trigger,
            )
            restored = self.build_active_context(session_id, purpose=purpose, window_tokens=window_tokens)
            if restored.degraded:
                self._emit_compact_warning(
                    session_id,
                    checkpoint_id=checkpoint.id,
                    warning="maybe_compact_restore_degraded",
                )
            return checkpoint
        except Exception as exc:
            if hard_trigger:
                raise ContextCompactError(str(exc) or type(exc).__name__) from exc
            return None

    def _emit_compact_failed(
        self,
        session_id: str,
        *,
        hard: bool,
        method_attempted: str,
        error: str,
        reason: str,
    ) -> None:
        if not hasattr(self.store, "add_event"):
            return
        self.store.add_event(
            make_event(
                "context_compact",
                "pm-agent",
                session_id,
                payload={
                    "status": "failed",
                    "schema_version": 2,
                    "hard": bool(hard),
                    "method_attempted": method_attempted,
                    "error": error,
                    "reason": _text(reason),
                },
            )
        )

    def _emit_compact_warning(self, session_id: str, *, checkpoint_id: str, warning: str) -> None:
        if not hasattr(self.store, "add_event"):
            return
        self.store.add_event(
            make_event(
                "context_compact",
                "pm-agent",
                session_id,
                payload={
                    "status": "warning",
                    "schema_version": 2,
                    "checkpoint_id": checkpoint_id,
                    "warning": warning,
                },
            )
        )

    def _store_compat_snapshot(
        self,
        session_id: str,
        active: ActiveContext,
        summary_text: str,
        summary_json: dict[str, Any],
    ) -> str:
        if not hasattr(self.store, "add_context_snapshot"):
            return ""
        event_ids = [
            _text(frame.get("event_id"))
            for frame in active.frames_after_checkpoint
            if isinstance(frame, dict) and _text(frame.get("event_id"))
        ]
        snapshot_id = uuid.uuid4().hex
        snapshot = ContextSnapshot(
            id=snapshot_id,
            session_id=session_id,
            kind="rolling",
            source_start_event_id=event_ids[0] if event_ids else "",
            source_end_event_id=event_ids[-1] if event_ids else "",
            source_event_ids_json=json.dumps(event_ids, ensure_ascii=False),
            summary_json=_dump(summary_json or {"text": summary_text}),
            summary_hash=hashlib.sha256(summary_text.encode("utf-8")).hexdigest(),
            created_at=self._clock(),
        )
        self.store.add_context_snapshot(snapshot)
        pack = extract_json_object(summary_text)
        if pack is not None and hasattr(self.store, "add_memory_item"):
            now = self._clock()
            for raw in memory_items_from_pack(pack):
                self.store.add_memory_item(
                    MemoryItem(
                        id=uuid.uuid4().hex,
                        session_id=session_id,
                        snapshot_id=snapshot_id,
                        scope="session",
                        kind=raw["kind"],
                        text=raw["text"],
                        status=raw["status"],
                        importance=raw["importance"],
                        confidence=raw["confidence"],
                        source_refs_json=json.dumps(raw["source_refs"], ensure_ascii=False),
                        tags_json=json.dumps(raw["tags"], ensure_ascii=False),
                        valid_from=raw["valid_from"],
                        valid_until=raw["valid_until"],
                        supersedes=raw["supersedes"],
                        superseded_by=raw["superseded_by"],
                        last_seen_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
        return snapshot_id

    def restore_from_latest_checkpoint(
        self,
        session_id: str,
        *,
        purpose: str = "pm_plan",
        window_tokens: int = 0,
    ) -> ActiveContext:
        self.materialize_session(session_id)
        session = self.store.get_session(session_id) if hasattr(self.store, "get_session") else None
        if session is None:
            return ActiveContext(session_id=session_id, purpose=purpose, degraded=True)
        frames = (
            self.store.get_context_frames(session_id)
            if hasattr(self.store, "get_context_frames")
            else []
        )
        runtime = self.extract_runtime_state(session, frames)
        runtime_dict = runtime_state_dict(runtime)
        checkpoint = (
            self.store.get_latest_context_checkpoint(session_id)
            if hasattr(self.store, "get_latest_context_checkpoint")
            else None
        )
        warnings: list[dict[str, Any]] = []
        degraded = False
        restore_mode = "raw_frames"
        replacement_history: list[dict[str, Any]] = []
        source_cursor: dict[str, Any] = {}
        frames_after = list(frames)
        stable_prefix = _stable_prefix(session, runtime_dict, purpose)

        if checkpoint is not None:
            parsed = _parse_checkpoint(checkpoint)
            if parsed["corrupted"] or parsed["invalid"]:
                degraded = True
                restore_mode = "raw_frames_degraded"
                warnings.extend(parsed["warnings"])
                frames_after = list(frames)
            else:
                restore_mode = "checkpoint"
                replacement_history = parsed["replacement_history"]
                source_cursor = parsed["source_cursor"]
                frames_after = _frames_after_cursor(frames, _cursor_end(source_cursor))
        else:
            legacy_summary = _legacy_summary(self.store, session)
            if legacy_summary:
                restore_mode = "legacy_summary"
                stable_prefix.append(
                    {
                        "type": "legacy_summary",
                        "content": _summarize_text(legacy_summary),
                        "source": "session.plan" if _text(getattr(session, "plan", "")) else "context_snapshot",
                    }
                )
                warnings.append({"code": "legacy_summary", "message": "Using legacy context summary."})

        frame_dicts = [_frame_to_context_item(frame) for frame in frames_after if _frame_model_visible(frame)]
        contract = PlanContract()
        envelope = build_pm_envelope(
            session,
            purpose=purpose,
            goal=_text(getattr(session, "goal", "")),
            user_intent_type=classify_user_intent(_text(getattr(session, "goal", ""))),
            runtime_state=runtime_dict,
            available_agents=[],
            tool_schema=[],
            output_contract=contract.output_contract(),
            validator_rules=contract.validator_rules(),
            stable_prefix=stable_prefix,
            replacement_history=replacement_history,
            frames_after_checkpoint=frame_dicts,
            warnings=warnings,
        )
        envelope["context"]["restore_mode"] = restore_mode
        if checkpoint is not None and not degraded:
            envelope["context"]["checkpoint_id"] = checkpoint.id
            envelope["context"]["source_cursor"] = source_cursor
        active = ActiveContext(
            session_id=session_id,
            purpose=purpose,
            envelope=envelope,
            stable_prefix=stable_prefix,
            replacement_history=replacement_history,
            frames_after_checkpoint=frame_dicts,
            runtime_state=runtime_dict,
            source_cursor=source_cursor,
            token_usage={"window_tokens": window_tokens},
            degraded=degraded,
            warnings=warnings,
        )
        active.rendered_text = render_active_context(active)
        return active


def make_frame_id(session_id: str, event_id: str, frame_type: str, payload: dict[str, Any]) -> str:
    canonical = _dump(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    session_short = _safe_id_part(session_id, 12)
    event_short = _safe_id_part(event_id, 12)
    type_part = _safe_id_part(frame_type, 32)
    return f"frame_{session_short}_{event_short}_{type_part}_{digest}"


def materialize_event(
    event: Event | Any,
    *,
    session_id_override: str | None = None,
) -> list[ContextFrame]:
    try:
        event_type = _event_attr(event, "type")
        payload = _event_payload(event)
        session_id = _event_attr(event, "session_id") or _text(session_id_override)
        event_id = _event_attr(event, "id")
        event_ts = _event_attr(event, "ts")
        task_id = _event_attr(event, "task_id")
        source = _event_attr(event, "source")
    except Exception:
        return []
    if not session_id:
        return []

    base = {
        "event_type": event_type,
        "event_source": source,
        "task_id": task_id,
    }
    if event_type == "dispatch":
        frames = [
            _frame(
                session_id,
                event_id,
                event_ts,
                "user_message",
                {**base, "goal": _text(payload.get("goal")), "payload": _compact_payload(payload)},
                role="user",
                lane=LANE_TASK,
                turn_id=task_id,
            )
        ]
        state = _workspace_payload(payload)
        if state:
            frames.append(
                _frame(
                    session_id,
                    event_id,
                    event_ts,
                    "worktree_state",
                    {**base, **state},
                    role="system",
                    lane=LANE_RUNTIME,
                    turn_id=task_id,
                )
            )
        return frames

    if event_type == "pm_plan":
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "pm_plan",
                {**base, "payload": _compact_payload(payload)},
                role="assistant",
                lane=LANE_PLAN,
                turn_id=task_id,
                agent_id="pm",
                agent_role="pm",
                agent_type="pm-agent",
            )
        ]

    if event_type == "pm_review":
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "pm_review",
                {**base, "payload": _compact_payload(payload)},
                role="assistant",
                lane=LANE_PLAN,
                turn_id=task_id,
                agent_id="pm",
                agent_role="pm",
                agent_type="pm-agent",
            )
        ]

    if event_type == "pm_validation_error":
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "previous_validation_error",
                {**base, "payload": _compact_payload(payload)},
                role="system",
                lane=LANE_PLAN,
                turn_id=task_id,
            )
        ]

    if event_type == "agent_input":
        message = _text(payload.get("message") or payload.get("instruction") or payload.get("task"))
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "agent_input",
                {
                    **base,
                    **_workspace_payload(payload),
                    "agent_id": _text(payload.get("agent_id")),
                    "agent_role": _text(payload.get("agent_role")),
                    "agent_type": _text(payload.get("agent_type")),
                    "parent_agent_id": _text(payload.get("parent_agent_id")),
                    "message": _summarize_text(message),
                    "expected_output": _summarize_text(_text(payload.get("expected_output"))),
                    "instruction": _summarize_text(_text(payload.get("instruction"))),
                },
                role="assistant",
                lane=LANE_PLAN,
                turn_id=task_id,
                agent_id=_text(payload.get("agent_id")),
                agent_role=_text(payload.get("agent_role")),
                agent_type=_text(payload.get("agent_type")),
                parent_agent_id=_text(payload.get("parent_agent_id")),
            )
        ]

    if event_type == "agent_start":
        payload_out = {**base, **_agent_payload(payload, source)}
        frames = [
            _frame(
                session_id,
                event_id,
                event_ts,
                "agent_start",
                payload_out,
                role="assistant",
                lane=LANE_RUNTIME,
                turn_id=task_id,
                agent_id=_agent_id(payload, event_id, source),
                agent_role=_text(payload.get("agent_role") or payload.get("role")),
                agent_type=_text(payload.get("agent_type") or payload.get("source") or source),
            )
        ]
        state = _workspace_payload(payload)
        if state:
            frames.append(
                _frame(
                    session_id,
                    event_id,
                    event_ts,
                    "worktree_state",
                    {**base, **state},
                    role="system",
                    lane=LANE_RUNTIME,
                    turn_id=task_id,
                )
            )
        return frames

    if event_type == "agent_output":
        command = _extract_command_payload(payload)
        if command:
            return [
                _frame(
                    session_id,
                    event_id,
                    event_ts,
                    "command_result",
                    {**base, **_agent_payload(payload, source), **command},
                    role="tool",
                    lane=LANE_DETAIL,
                    turn_id=task_id,
                    agent_id=_agent_id(payload, event_id, source),
                    agent_type=source,
                )
            ]
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "agent_output",
                {
                    **base,
                    **_agent_payload(payload, source),
                    "text": _summarize_text(_payload_text(payload)),
                    "payload": _compact_payload(payload),
                },
                role="assistant",
                lane=LANE_DETAIL,
                turn_id=task_id,
                agent_id=_agent_id(payload, event_id, source),
                agent_type=source,
            )
        ]

    if event_type in {"pm_output", "pm_reasoning", "agent_reasoning"}:
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                event_type,
                {
                    **base,
                    "text": _summarize_text(_payload_text(payload)),
                    "model_visible": False,
                },
                role="assistant",
                lane=LANE_NOISE,
                turn_id=task_id,
                agent_id="pm" if event_type.startswith("pm_") else _agent_id(payload, event_id, source),
                agent_type="pm-agent" if event_type.startswith("pm_") else source,
            )
        ]

    if event_type == "tool_pre":
        tool_name = _text(payload.get("tool") or payload.get("tool_name"))
        call_id = _text(payload.get("call_id") or payload.get("id"))
        tool_input = _as_dict(payload.get("input") or payload.get("tool_input") or payload.get("arguments"))
        frame_type = "command_call" if tool_name in {"run_command", "Bash"} or tool_input.get("command") else "tool_call"
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                frame_type,
                {
                    **base,
                    "tool": tool_name,
                    "call_id": call_id,
                    "input": _compact_payload(tool_input),
                    "command": _text(tool_input.get("command")),
                    "cwd": _text(tool_input.get("cwd")),
                },
                role="tool",
                lane=LANE_DETAIL,
                turn_id=task_id,
                agent_id=_text(payload.get("source") or source),
            )
        ]

    if event_type == "tool_post":
        result = _as_dict(payload.get("result"))
        data = _as_dict(result.get("data"))
        tool_name = _text(payload.get("tool") or payload.get("tool_name") or result.get("name"))
        call_id = _text(payload.get("call_id") or payload.get("id") or result.get("id"))
        command_payload = _command_result_from_tool(payload, result, data)
        frame_type = "command_result" if command_payload else "tool_result"
        out_payload = command_payload or {
            "tool": tool_name,
            "call_id": call_id,
            "ok": _boolish(payload.get("ok", result.get("ok"))),
            "result": _compact_payload(result or payload),
        }
        frames = [
            _frame(
                session_id,
                event_id,
                event_ts,
                frame_type,
                {**base, **out_payload, "call_id": call_id, "tool": tool_name},
                role="tool",
                lane=LANE_DETAIL,
                turn_id=task_id,
                agent_id=_text(payload.get("source") or source),
            )
        ]
        if command_payload and _is_test_command(_text(command_payload.get("command"))):
            frames.append(
                _frame(
                    session_id,
                    event_id,
                    event_ts,
                    "test_result",
                    {**base, **_test_result_payload(command_payload)},
                    role="tool",
                    lane=LANE_DETAIL,
                    turn_id=task_id,
                    agent_id=_text(payload.get("source") or source),
                )
            )
        return frames

    if event_type == "stop":
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "agent_stop",
                {
                    **base,
                    **_workspace_payload(payload),
                    "payload": _compact_payload(payload),
                    "status": _stop_status(payload),
                    "returncode": _int_or_none(payload.get("returncode")),
                    "error": _text(payload.get("error") or payload.get("msg")),
                    "summary": _text(payload.get("summary") or payload.get("result")),
                    "native_session_id": _text(payload.get("native_session_id")),
                    "handle_id": _text(payload.get("handle_id")),
                },
                role="assistant",
                lane=LANE_RUNTIME,
                turn_id=task_id,
                agent_id=_agent_id(payload, event_id, source),
                agent_type=source,
            )
        ]

    if event_type == "error":
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "agent_stop",
                {
                    **base,
                    **_workspace_payload(payload),
                    "payload": _compact_payload(payload),
                    "status": "failed",
                    "returncode": _int_or_none(payload.get("returncode")),
                    "error": _text(payload.get("error") or payload.get("msg")),
                    "summary": _text(payload.get("summary") or payload.get("msg")),
                    "native_session_id": _text(payload.get("native_session_id")),
                    "handle_id": _text(payload.get("handle_id")),
                },
                role="assistant",
                lane=LANE_RUNTIME,
                turn_id=task_id,
                agent_id=_agent_id(payload, event_id, source),
                agent_type=source,
            )
        ]

    if event_type in {"file_change", "git_diff", "diff_stat", "git_status"}:
        file_payload = _file_change_payload(payload)
        if not file_payload:
            return []
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "file_change",
                {**base, **file_payload},
                role="tool",
                lane=LANE_DETAIL,
                turn_id=task_id,
            )
        ]

    if event_type == "test_result":
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "test_result",
                {**base, **_test_result_payload(payload)},
                role="tool",
                lane=LANE_DETAIL,
                turn_id=task_id,
                agent_id=_agent_id(payload, event_id, source),
                agent_type=source,
            )
        ]

    if event_type == "context_compact":
        return [
            _frame(
                session_id,
                event_id,
                event_ts,
                "context_compaction",
                {**base, "payload": _compact_payload(payload)},
                role="system",
                lane=LANE_MEMORY,
                turn_id=task_id,
            )
        ]

    return []


def materialize_session(store: Any, session_id: str, force: bool = False) -> list[ContextFrame]:
    return ContextManager(store).materialize_session(session_id, force=force)


def record_event(store: Any, session_id: str, event: Event | Any) -> list[ContextFrame]:
    return ContextManager(store).record_event(session_id, event)


def extract_runtime_state(
    session: Session,
    frames: list[ContextFrame],
    runner: Any = None,
    tasks: list[Task] | None = None,
) -> RuntimeState:
    state = RuntimeState(
        session_id=_text(getattr(session, "id", "")),
        goal=_text(getattr(session, "goal", "")),
        workspace=_text(getattr(session, "workspace", "")),
        main_workspace=_text(getattr(session, "main_workspace", "")),
    )
    state.cwd = state.workspace
    state.worktree = state.workspace
    agents: dict[str, dict[str, Any]] = {}

    for frame in frames:
        payload = _loads(frame.payload_json)
        if frame.type == "worktree_state":
            _merge_runtime_anchor(state, payload)
        elif frame.type == "agent_input":
            agent_id = frame.agent_id or _text(payload.get("agent_id")) or frame.id
            agent = agents.setdefault(agent_id, _new_agent(agent_id))
            agent["agent_role"] = frame.agent_role or agent.get("agent_role", "")
            agent["agent_type"] = frame.agent_type or agent.get("agent_type", "")
            agent["parent_agent_id"] = frame.parent_agent_id or agent.get("parent_agent_id", "")
            _merge_agent_fields(agent, payload)
            agent["last_seen_at"] = frame.event_ts or frame.created_at or agent.get("last_seen_at", "")
            agent["last_meaningful_output"] = {"type": frame.type, "payload": payload}
            _merge_runtime_anchor(state, payload)
        elif frame.type == "agent_start":
            agent_id = frame.agent_id or _text(payload.get("agent_id")) or frame.id
            agent = agents.setdefault(agent_id, _new_agent(agent_id))
            agent["agent_role"] = frame.agent_role or _text(payload.get("agent_role")) or agent.get("agent_role", "")
            agent["agent_type"] = frame.agent_type or _text(payload.get("agent_type")) or agent.get("agent_type", "")
            agent["parent_agent_id"] = frame.parent_agent_id or _text(payload.get("parent_agent_id")) or agent.get("parent_agent_id", "")
            agent["status"] = _text(payload.get("status")) or "running"
            _merge_agent_fields(agent, payload)
            agent["last_seen_at"] = frame.event_ts or frame.created_at or agent.get("last_seen_at", "")
            _merge_runtime_anchor(state, payload)
        elif frame.type == "agent_stop":
            agent_id = frame.agent_id or _text(payload.get("agent_id")) or frame.id
            agent = agents.setdefault(agent_id, _new_agent(agent_id))
            agent["status"] = _text(payload.get("status")) or "completed"
            _merge_agent_fields(agent, payload)
            agent["last_seen_at"] = frame.event_ts or frame.created_at or agent.get("last_seen_at", "")
            stop_payload = _as_dict(payload.get("payload"))
            summary = _text(payload.get("summary") or stop_payload.get("summary"))
            agent["last_meaningful_output"] = {
                "type": frame.type,
                "status": agent["status"],
                "summary": summary,
                "payload": payload,
            }
            _merge_runtime_collections(state, payload)
            _merge_runtime_collections(state, stop_payload)
        elif frame.type == "file_change":
            _merge_changed_files(state, payload)
        elif frame.type in {"agent_output", "command_result", "tool_result", "test_result"} and frame.agent_id:
            agent = agents.setdefault(frame.agent_id, _new_agent(frame.agent_id))
            _merge_agent_fields(agent, payload)
            if frame.event_ts >= _text(agent.get("last_seen_at")):
                agent["last_seen_at"] = frame.event_ts
                agent["last_meaningful_output"] = {"type": frame.type, "payload": payload}
        if frame.type == "command_result":
            state.last_commands.append(_command_state(payload))
        if frame.type == "test_result":
            state.last_tests.append(payload)
        for path in _as_list(payload.get("changed_files") or payload.get("files")):
            text = _text(path)
            if text and text not in state.changed_files:
                state.changed_files.append(text)
        for step in _as_list(payload.get("next_steps")):
            text = _text(step)
            if text and text not in state.next_steps:
                state.next_steps.append(text)

    _merge_task_state(agents, tasks or [])
    _merge_runner_state(state, agents, runner, state.session_id)
    state.active_agents = sorted(agents.values(), key=lambda item: item.get("agent_id", ""))
    return state


def runtime_state_dict(state: RuntimeState) -> dict[str, Any]:
    return asdict(state)


def classify_user_intent(
    goal: str,
    *,
    explicit_agent: bool = False,
    workspace: str = "",
    context: dict | None = None,
) -> str:
    if explicit_agent:
        return "code_change"
    text = _text(goal).lower()
    if not text:
        return "planning_only"
    if any(marker in text for marker in ("http://", "https://", "browser", "webpage", "screenshot", "网页", "浏览器", "截图")):
        return "browser_task"
    if _has_english_word(text, ("fix", "implement", "test", "refactor", "modify", "patch", "bug")):
        return "code_change"
    if any(marker in text for marker in (
        "修复", "修改", "实现", "测试", "改代码",
    )):
        return "code_change"
    if any(marker in text for marker in (
        "explain code", "inspect repo", "find file", "search repo", "read code",
        "看仓库", "解释代码", "找文件", "阅读代码",
    )):
        return "repo_inspection"
    if any(marker in text for marker in (
        "hello", "hi", "what is", "explain concept", "你好", "是什么", "解释一个",
    )):
        return "direct_answer"
    return "planning_only"


def build_pm_envelope(
    session,
    *,
    purpose,
    goal,
    user_intent_type,
    runtime_state,
    available_agents,
    tool_schema,
    output_contract,
    validator_rules,
    stable_prefix,
    replacement_history,
    frames_after_checkpoint,
    warnings,
) -> dict[str, Any]:
    runtime = runtime_state if isinstance(runtime_state, dict) else asdict(runtime_state)
    return {
        "task": {
            "user_intent_type": user_intent_type,
            "original_user_request": _text(getattr(session, "goal", "")),
            "current_goal": _text(goal),
            "task_constraints": [],
            "purpose": _text(purpose),
        },
        "environment": {
            "cwd": _text(runtime.get("cwd")),
            "workspace": _text(runtime.get("workspace")),
            "worktree": _text(runtime.get("worktree")),
            "branch": _text(runtime.get("branch")),
            "base_ref": _text(runtime.get("base_ref")),
            "runtime_policy": {},
        },
        "agents": {
            "available": list(available_agents or []),
            "active": list(runtime.get("active_agents") or []),
        },
        "context": {
            "stable_prefix": list(stable_prefix or []),
            "checkpoint_replacement_history": list(replacement_history or []),
            "frames_after_checkpoint": list(frames_after_checkpoint or []),
            "runtime_state": runtime,
        },
        "tools": {
            "available": _tool_names(tool_schema),
            "schemas": _compact_payload(tool_schema or []),
        },
        "output_contract": dict(output_contract or {}),
        "validator_rules": dict(validator_rules or {}),
        "warnings": list(warnings or []),
    }


def render_active_context(active_context: ActiveContext) -> str:
    envelope = dict(active_context.envelope or {})
    envelope["degraded"] = bool(active_context.degraded)
    if active_context.source_cursor:
        envelope.setdefault("context", {})["source_cursor"] = active_context.source_cursor
    ordered = {
        "task": envelope.get("task", {}),
        "output_contract": envelope.get("output_contract", {}),
        "validator_rules": envelope.get("validator_rules", {}),
        "environment": envelope.get("environment", {}),
        "agents": envelope.get("agents", {}),
        "context": envelope.get("context", {}),
        "tools": envelope.get("tools", {}),
        "warnings": envelope.get("warnings", []),
        "degraded": envelope.get("degraded", False),
    }
    text = json.dumps(_compact_payload(ordered), ensure_ascii=False, indent=2)
    if len(text) <= 8000:
        return text
    return text[:8000] + "\n...[truncated active context]..."


def estimate_active_context_tokens(active_context: ActiveContext) -> int:
    envelope = active_context.envelope if isinstance(active_context.envelope, dict) else {}
    usage_payload = {
        "rendered_text": active_context.rendered_text or "",
        "stable_prefix": active_context.stable_prefix or [],
        "replacement_history": active_context.replacement_history or [],
        "frames_after_checkpoint": active_context.frames_after_checkpoint or [],
        "runtime_state": active_context.runtime_state or {},
        "task": _as_dict(envelope.get("task")),
        "environment": _as_dict(envelope.get("environment")),
        "agents": _as_dict(envelope.get("agents")),
        "context": {
            key: value
            for key, value in _as_dict(envelope.get("context")).items()
            if key not in {"stable_prefix", "checkpoint_replacement_history", "frames_after_checkpoint", "runtime_state"}
        },
    }
    return _approx_tokens(json.dumps(_compact_payload(usage_payload), ensure_ascii=False, sort_keys=True))


def estimate_context_usage(active_context: ActiveContext, window_tokens: int) -> ContextUsage:
    used = estimate_active_context_tokens(active_context)
    window = max(0, int(window_tokens or 0))
    soft_threshold = 0.70
    hard_threshold = 0.90
    lane_usage = {str(lane): 0 for lane in LANES}
    for frame in active_context.frames_after_checkpoint or []:
        if not isinstance(frame, dict):
            continue
        lane = str(frame.get("lane") or LANE_DETAIL)
        if lane not in lane_usage:
            continue
        lane_usage[lane] += _approx_tokens(json.dumps(frame, ensure_ascii=False, sort_keys=True))
    percent = (used / window) if window > 0 else 0.0
    soft_at = int(window * soft_threshold) if window > 0 else 0
    hard_at = int(window * hard_threshold) if window > 0 else 0
    return ContextUsage(
        used_tokens=used,
        window_tokens=window,
        percent=percent,
        tokens_until_soft_compact=max(0, soft_at - used) if window > 0 else 0,
        tokens_until_hard_compact=max(0, hard_at - used) if window > 0 else 0,
        soft_threshold=soft_threshold,
        hard_threshold=hard_threshold,
        run_count_threshold=8,
        lane_usage=lane_usage,
    )


def should_hard_compact(usage: ContextUsage) -> bool:
    return usage.window_tokens > 0 and usage.percent >= usage.hard_threshold


def should_soft_compact(usage: ContextUsage, run_count: int = 0) -> bool:
    if should_hard_compact(usage):
        return False
    if usage.window_tokens > 0 and usage.percent >= usage.soft_threshold:
        return True
    return run_count > 0 and usage.run_count_threshold > 0 and run_count % usage.run_count_threshold == 0


def frames_to_replacement_history(
    active_context: ActiveContext,
    *,
    summary_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary_json = summary_json if isinstance(summary_json, dict) else {}
    runtime = active_context.runtime_state if isinstance(active_context.runtime_state, dict) else {}
    frames = [frame for frame in active_context.frames_after_checkpoint or [] if isinstance(frame, dict)]
    items: list[dict[str, Any]] = []
    items.extend(_prior_replacement_history_items(active_context))
    source_refs = _frame_source_refs(frames)
    goal = _text(runtime.get("goal") or active_context.envelope.get("task", {}).get("current_goal"))
    if goal:
        items.append(
            {
                "id": "original_goal",
                "role": "user",
                "kind": "original_goal",
                "content": goal,
                "source_refs": source_refs[:8] or ["session:goal"],
            }
        )
    summary_text = _text(summary_json.get("summary") or summary_json.get("text"))
    if not summary_text:
        summary_text = _recent_frame_summary(frames) or "Context compacted."
    items.append(
        {
            "id": "checkpoint_summary",
            "role": "system",
            "kind": "checkpoint_summary",
            "content": _summarize_text(summary_text, max_chars=900),
            "payload": _compact_payload(summary_json),
            "source_refs": source_refs[:12],
        }
    )
    if runtime:
        items.append(
            {
                "id": "runtime_state",
                "role": "system",
                "kind": "runtime_state",
                "content": "Runtime state at compaction.",
                "payload": _compact_payload(runtime),
            }
        )
    for key, kind, label in (
        ("active_agents", "active_agents", "Active agents"),
        ("changed_files", "changed_files", "Changed files"),
        ("last_tests", "last_tests", "Last tests"),
        ("next_steps", "next_steps", "Next steps"),
        ("open_questions", "open_questions", "Open questions"),
    ):
        value = runtime.get(key)
        if value:
            items.append(
                {
                    "id": kind,
                    "role": "system",
                    "kind": kind,
                    "content": f"{label}: {_summarize_text(json.dumps(value, ensure_ascii=False), max_chars=700)}",
                    "payload": {key: _compact_payload(value)},
                    "source_refs": source_refs[:12],
                }
            )
    for idx, frame in enumerate(_anchor_frames(frames)[:12]):
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        content = _anchor_content(frame, payload, frames)
        if not content:
            continue
        frame_ids = _paired_frame_ids(frame, frames)
        refs = _as_list(frame.get("source_refs"))
        refs.extend(_paired_source_refs(frame, frames))
        if _text(frame.get("event_id")):
            refs.append(f"event:{_text(frame.get('event_id'))}")
        items.append(
            {
                "id": f"anchor_{idx}",
                "role": _text(frame.get("role")) or "system",
                "kind": _text(frame.get("type")) or "frame_anchor",
                "content": content,
                "frame_ids": frame_ids,
                "source_refs": _dedupe(refs),
            }
        )
    return {"items": items, "schema": "foreman.replacement_history.v2"}


def _compact_input_items(active_context: ActiveContext) -> list[dict[str, Any]]:
    return [
        {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": active_context.rendered_text,
                }
            ],
        }
    ]


def _compact_instructions() -> str:
    return (
        "Compact this Foreman active context into a concise, human-readable JSON summary. "
        "Do not expose encrypted_content as summary text. Preserve decisions, files, tests, "
        "commands, next steps, and validation errors. Avoid raw full stdout/stderr."
    )


def _summary_from_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("summary_json", "summary", "output_json"):
        value = payload.get(key)
        if isinstance(value, dict):
            return _compact_payload(value)
        if isinstance(value, str) and value.strip() and key != "encrypted_content":
            parsed = _loads_json(value)
            if isinstance(parsed, dict):
                return _compact_payload(parsed)
            return {"summary": _summarize_text(value, max_chars=1200)}
    output = payload.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("encrypted_content"):
                continue
            text = _text(item.get("text") or item.get("summary"))
            if text:
                texts.append(text)
        if texts:
            return {"summary": _summarize_text("\n".join(texts), max_chars=1200)}
    return {}


def _summary_from_local_text(text: str) -> dict[str, Any]:
    raw = _text(text)
    if not raw:
        raise ContextCompactError("no_context")
    parsed = extract_json_object(raw)
    if isinstance(parsed, dict):
        session_state = parsed.get("session_state") if isinstance(parsed.get("session_state"), dict) else {}
        summary = _text(session_state.get("summary") or parsed.get("summary") or parsed.get("text"))
        out = dict(parsed)
        out["summary"] = summary or _summarize_text(raw, max_chars=1400)
        out["text"] = raw
        return out
    return {"summary": _summarize_text(raw, max_chars=1400), "text": raw}


def _summary_json(active_context: ActiveContext, remote_summary: dict[str, Any], *, method: str, reason: str) -> dict[str, Any]:
    runtime = active_context.runtime_state if isinstance(active_context.runtime_state, dict) else {}
    frames = [frame for frame in active_context.frames_after_checkpoint or [] if isinstance(frame, dict)]
    summary = _text(remote_summary.get("summary") or remote_summary.get("text"))
    if not summary:
        summary = _recent_frame_summary(frames)
    if not summary:
        summary = _text(runtime.get("goal")) or "Context compacted."
    out = {
        "schema_version": 2,
        "summary": _summarize_text(summary, max_chars=1400),
        "method": method,
        "reason": _text(reason),
        "changed_files": runtime.get("changed_files") or [],
        "last_tests": runtime.get("last_tests") or [],
        "next_steps": runtime.get("next_steps") or [],
        "open_questions": runtime.get("open_questions") or [],
    }
    for key, value in remote_summary.items():
        if key not in out and key != "encrypted_content":
            out[key] = _compact_payload(value)
    return _compact_payload(out)


def _compat_summary_text(summary_json: dict[str, Any]) -> str:
    text = _text(summary_json.get("text"))
    if text:
        return text
    return json.dumps(_compact_payload(summary_json), ensure_ascii=False, indent=2)


def _source_cursor_from_active_context(active_context: ActiveContext) -> dict[str, Any]:
    frames = [
        frame
        for frame in active_context.frames_after_checkpoint or []
        if isinstance(frame, dict) and _text(frame.get("type")) != "context_compaction"
    ]
    if not frames:
        return {}
    first = frames[0]
    last = frames[-1]
    return {
        "start": {
            "event_ts": _text(first.get("event_ts")),
            "event_id": _text(first.get("event_id")),
        },
        "end": {
            "event_ts": _text(last.get("event_ts")),
            "event_id": _text(last.get("event_id")),
        },
    }


def _approx_tokens(text: str) -> int:
    return max(1, (len(text or "") + 3) // 4)


def _anchor_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = {
        "pm_plan",
        "pm_review",
        "command_result",
        "test_result",
        "file_change",
        "agent_stop",
        "previous_validation_error",
    }
    return [frame for frame in frames if _text(frame.get("type")) in wanted]


def _anchor_content(frame: dict[str, Any], payload: dict[str, Any], frames: list[dict[str, Any]] | None = None) -> str:
    frame_type = _text(frame.get("type"))
    payload = dict(payload)
    if frame_type == "command_result" and not _text(payload.get("command")):
        paired = _paired_command_payload(frame, frames or [])
        for key in ("command", "cwd"):
            if paired.get(key) and not payload.get(key):
                payload[key] = paired[key]
    keys = {
        "command_result": ("command", "exit_code", "cwd", "important_lines", "stdout_summary", "stderr_summary"),
        "test_result": ("command", "status", "passed", "failed", "exit_code", "failures", "important_lines"),
        "file_change": ("changed_files", "files", "paths", "diff_stat", "truncated"),
        "agent_stop": ("status", "summary", "result", "payload"),
        "previous_validation_error": ("error", "round", "arguments"),
        "pm_plan": ("summary", "payload"),
        "pm_review": ("summary", "payload"),
    }.get(frame_type, ())
    picked = {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", [], {})}
    if not picked:
        picked = {"type": frame_type, "payload": _compact_payload(payload)}
    return _summarize_text(json.dumps(picked, ensure_ascii=False, sort_keys=True), max_chars=900)


def _paired_frame_ids(frame: dict[str, Any], frames: list[dict[str, Any]]) -> list[str]:
    ids = [_text(frame.get("id"))]
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    call_id = _text(payload.get("call_id") or payload.get("tool_call_id"))
    if call_id:
        for candidate in frames:
            if candidate is frame:
                continue
            candidate_payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
            if _text(candidate_payload.get("call_id") or candidate_payload.get("tool_call_id")) == call_id:
                ids.append(_text(candidate.get("id")))
    return _dedupe(ids)


def _prior_replacement_history_items(active_context: ActiveContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, item in enumerate(active_context.replacement_history or []):
        if not isinstance(item, dict):
            continue
        cloned = _compact_payload(item)
        key = _text(cloned.get("id")) or json.dumps(cloned, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        cloned.setdefault("id", f"prior_{idx}")
        cloned.setdefault("role", "system")
        cloned.setdefault("kind", cloned.get("type") or "prior_checkpoint")
        if not _text(cloned.get("content")) and isinstance(cloned.get("payload"), dict):
            cloned["content"] = _summarize_text(
                json.dumps(cloned["payload"], ensure_ascii=False, sort_keys=True),
                max_chars=900,
            )
        out.append(cloned)
    return out


def _paired_source_refs(frame: dict[str, Any], frames: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    call_id = _text(payload.get("call_id") or payload.get("tool_call_id"))
    if not call_id:
        return refs
    for candidate in frames:
        candidate_payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        if _text(candidate_payload.get("call_id") or candidate_payload.get("tool_call_id")) != call_id:
            continue
        refs.extend(_as_list(candidate.get("source_refs")))
        event_id = _text(candidate.get("event_id"))
        if event_id:
            refs.append(f"event:{event_id}")
    return _dedupe(refs)


def _paired_command_payload(frame: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    call_id = _text(payload.get("call_id") or payload.get("tool_call_id"))
    if not call_id:
        return {}
    for candidate in frames:
        candidate_payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        if _text(candidate_payload.get("call_id") or candidate_payload.get("tool_call_id")) != call_id:
            continue
        if _text(candidate.get("type")) not in {"command_call", "tool_call"}:
            continue
        command = _text(candidate_payload.get("command"))
        cwd = _text(candidate_payload.get("cwd"))
        if command or cwd:
            return {"command": command, "cwd": cwd}
    return {}


def _recent_frame_summary(frames: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for frame in _anchor_frames(frames)[-8:]:
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        content = _anchor_content(frame, payload, frames)
        if content:
            parts.append(f"{_text(frame.get('type'))}: {content}")
    return "\n".join(parts)


def _frame_source_refs(frames: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for frame in frames:
        refs.extend(_as_list(frame.get("source_refs")))
        event_id = _text(frame.get("event_id"))
        if event_id:
            refs.append(f"event:{event_id}")
    return _dedupe(refs)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        text = _text(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _has_english_word(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)


def _stable_prefix(session: Session, runtime: dict[str, Any], purpose: str) -> list[dict[str, Any]]:
    prefix = [
        {
            "type": "task",
            "purpose": _text(purpose),
            "goal": _text(getattr(session, "goal", "")),
        }
    ]
    env = {
        key: _text(runtime.get(key))
        for key in ("cwd", "workspace", "worktree", "branch", "base_ref", "head_sha")
        if _text(runtime.get(key))
    }
    if env:
        prefix.append({"type": "environment", "payload": env})
    return prefix


def _legacy_summary(store: Any, session: Session) -> str:
    plan = _text(getattr(session, "plan", ""))
    if plan:
        return plan
    if not hasattr(store, "get_context_snapshots"):
        return ""
    snapshots = store.get_context_snapshots(_text(getattr(session, "id", "")))
    if not snapshots:
        return ""
    return _snapshot_summary(snapshots[0])


def _snapshot_summary(snapshot: ContextSnapshot) -> str:
    raw = _loads_or_none(snapshot.summary_json)
    if raw is None:
        return _text(getattr(snapshot, "summary_json", ""))
    for key in ("text", "summary", "content"):
        value = _text(raw.get(key))
        if value:
            return value
    return _summarize_text(json.dumps(raw, ensure_ascii=False, sort_keys=True))


def _parse_checkpoint(checkpoint: ContextCheckpoint) -> dict[str, Any]:
    corrupted = False
    invalid = False
    warnings: list[dict[str, Any]] = []
    replacement_raw = _loads_or_none(checkpoint.replacement_history_json)
    cursor_raw = _loads_or_none(checkpoint.source_cursor_json)
    summary_raw = _loads_or_none(checkpoint.summary_json)
    runtime_raw = _loads_or_none(checkpoint.runtime_state_json)
    for value in (replacement_raw, cursor_raw, summary_raw, runtime_raw):
        if value is None:
            corrupted = True
    if corrupted:
        warnings.append({"code": "corrupted_checkpoint", "message": "Latest context checkpoint JSON is corrupted."})
    replacement_history: list[dict[str, Any]] = []
    if isinstance(replacement_raw, dict):
        items = replacement_raw.get("items")
        if not isinstance(items, list) or not items:
            invalid = True
        else:
            replacement_history, item_errors = _validate_replacement_history_items(items)
            invalid = bool(item_errors)
    else:
        invalid = True
    if invalid:
        warnings.append({"code": "invalid_replacement_history", "message": "Checkpoint replacement history is not renderable."})
    return {
        "corrupted": corrupted,
        "invalid": invalid,
        "warnings": warnings,
        "replacement_history": replacement_history,
        "source_cursor": cursor_raw if isinstance(cursor_raw, dict) else {},
    }


def _validate_replacement_history_items(items: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"item_{idx}_not_object")
            continue
        role = _text(item.get("role"))
        kind = _text(item.get("kind") or item.get("type"))
        content = _text(item.get("content"))
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else None
        frame_ids = _as_list(item.get("frame_ids"))
        source_refs = _as_list(item.get("source_refs"))
        if not role:
            errors.append(f"item_{idx}_missing_role")
        if not kind:
            errors.append(f"item_{idx}_missing_kind")
        if not (content or payload):
            errors.append(f"item_{idx}_missing_body")
        if kind not in {"runtime_state", "checkpoint_summary"} and not (frame_ids or source_refs):
            errors.append(f"item_{idx}_missing_sources")
        if role and kind and (content or payload) and (
            kind in {"runtime_state", "checkpoint_summary"} or frame_ids or source_refs
        ):
            valid.append(_compact_payload(item))
    return valid, errors


def _loads_or_none(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _cursor_end(cursor: dict[str, Any]) -> dict[str, Any]:
    end = cursor.get("end") if isinstance(cursor.get("end"), dict) else cursor
    return {
        "event_ts": _text(
            end.get("event_ts")
            or end.get("ts")
            or cursor.get("end_event_ts")
            or cursor.get("source_end_event_ts")
        ),
        "event_id": _text(
            end.get("event_id")
            or end.get("id")
            or cursor.get("end_event_id")
            or cursor.get("source_end_event_id")
        ),
    }


def _frames_after_cursor(frames: list[ContextFrame], cursor: dict[str, Any]) -> list[ContextFrame]:
    event_ts = _text(cursor.get("event_ts"))
    event_id = _text(cursor.get("event_id"))
    if not event_ts or not event_id:
        return list(frames)
    out: list[ContextFrame] = []
    matched = False
    for frame in frames:
        if frame.event_ts > event_ts or (frame.event_ts == event_ts and event_id and frame.event_id > event_id):
            out.append(frame)
        if event_id and frame.event_ts == event_ts and frame.event_id == event_id:
            matched = True
    return out if matched else list(frames)


def _frame_model_visible(frame: ContextFrame) -> bool:
    if frame.lane == LANE_NOISE:
        return False
    payload = _loads(frame.payload_json)
    return payload.get("model_visible", True) is not False


def _frame_to_context_item(frame: ContextFrame) -> dict[str, Any]:
    return {
        "id": frame.id,
        "event_id": frame.event_id,
        "event_ts": frame.event_ts,
        "type": frame.type,
        "role": frame.role,
        "lane": frame.lane,
        "agent_id": frame.agent_id,
        "payload": _compact_payload(_loads(frame.payload_json)),
        "source_refs": _as_list(_loads_json(frame.source_refs_json)),
    }


def _loads_json(raw: str) -> Any:
    try:
        return json.loads(raw or "null")
    except (TypeError, ValueError):
        return None


def _tool_names(tool_schema: Any) -> list[str]:
    out: list[str] = []
    for item in tool_schema or []:
        name = ""
        if isinstance(item, dict):
            name = _text(item.get("name"))
        else:
            name = _text(getattr(item, "name", ""))
        if name:
            out.append(name)
    return out


def _frame(
    session_id: str,
    event_id: str,
    event_ts: str,
    frame_type: str,
    payload: dict[str, Any],
    *,
    role: str,
    lane: int,
    turn_id: str = "",
    agent_id: str = "",
    agent_role: str = "",
    agent_type: str = "",
    parent_agent_id: str = "",
) -> ContextFrame:
    clean_payload = _compact_payload(payload)
    payload_hash = hashlib.sha256(_dump(clean_payload).encode("utf-8")).hexdigest()
    return ContextFrame(
        id=make_frame_id(session_id, event_id, frame_type, clean_payload),
        session_id=session_id,
        event_id=event_id,
        event_ts=event_ts,
        turn_id=turn_id,
        type=frame_type,
        role=role,
        lane=lane,
        agent_id=agent_id,
        agent_role=agent_role,
        agent_type=agent_type,
        parent_agent_id=parent_agent_id,
        payload_json=_dump(clean_payload),
        source_refs_json=json.dumps([f"event:{event_id}"] if event_id else [], ensure_ascii=False),
        payload_hash=payload_hash,
        created_at=event_ts or utc_now_iso(),
    )


def _event_attr(event: Any, name: str) -> str:
    return _text(getattr(event, name, ""))


def _event_payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        return payload
    raw = getattr(event, "payload_json", "") or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _workspace_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("cwd", "workspace", "worktree", "branch", "base_ref", "head_sha"):
        value = _text(payload.get(key))
        if value:
            out[key] = value
    workspace = _text(payload.get("workspace"))
    if workspace and not out.get("cwd"):
        out["cwd"] = workspace
    if workspace and not out.get("worktree"):
        out["worktree"] = workspace
    return out


def _agent_payload(payload: dict[str, Any], source: str) -> dict[str, Any]:
    out = _workspace_payload(payload)
    for key in (
        "handle_id", "pid", "command", "model", "effort", "native_session_id", "transcript_path",
        "agent_id", "agent_role", "agent_type", "parent_agent_id",
        "status", "source", "base_ref", "head_sha",
    ):
        if key in payload and payload.get(key) not in (None, ""):
            out[key] = payload.get(key)
    if out.get("handle_id") and not out.get("agent_id"):
        out["agent_id"] = out["handle_id"]
    out.setdefault("source", source)
    out.setdefault("agent_type", source)
    out.setdefault("status", "running")
    return out


def _agent_id(payload: dict[str, Any], event_id: str, source: str) -> str:
    return _text(
        payload.get("agent_id")
        or payload.get("handle_id")
        or payload.get("id")
        or payload.get("pid")
        or source
        or event_id
    )


def _extract_command_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    item = _as_dict(payload.get("item") or payload.get("message"))
    candidates = [payload, item]
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        if _text(raw.get("type")) != "command_execution" and not raw.get("aggregated_output"):
            continue
        command = _text(raw.get("command"))
        output = _text(raw.get("aggregated_output") or raw.get("stdout") or raw.get("stderr"))
        return {
            "command": command,
            "cwd": _text(raw.get("cwd") or payload.get("cwd")),
            "exit_code": _int_or_none(raw.get("exit_code") if "exit_code" in raw else raw.get("returncode")),
            "status": _text(raw.get("status")),
            "stdout_summary": _summarize_text(_text(raw.get("stdout") or output)),
            "stderr_summary": _summarize_text(_text(raw.get("stderr"))),
            "important_lines": _important_lines("\n".join([output, _text(raw.get("stderr"))])),
            "truncated": len(output) > MAX_TEXT_CHARS or len(_text(raw.get("stderr"))) > MAX_TEXT_CHARS,
        }
    return None


def _command_result_from_tool(
    payload: dict[str, Any],
    result: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any] | None:
    tool_name = _text(payload.get("tool") or result.get("name"))
    if tool_name != "run_command" and not data.get("command") and not data.get("returncode"):
        return None
    stdout = _text(data.get("stdout") or data.get("stdout_summary"))
    stderr = _text(data.get("stderr") or data.get("stderr_summary"))
    return {
        "call_id": _text(payload.get("call_id") or result.get("id")),
        "command": _text(data.get("command")),
        "cwd": _text(data.get("cwd")),
        "exit_code": _int_or_none(data.get("exit_code") if "exit_code" in data else data.get("returncode")),
        "ok": _boolish(payload.get("ok", result.get("ok"))),
        "stdout_summary": _summarize_text(stdout),
        "stderr_summary": _summarize_text(stderr),
        "important_lines": _important_lines("\n".join([stdout, stderr])),
        "truncated": bool(data.get("truncated")) or len(stdout) > MAX_TEXT_CHARS or len(stderr) > MAX_TEXT_CHARS,
        "log_path": _text(data.get("log_path")),
    }


def _file_change_payload(payload: dict[str, Any]) -> dict[str, Any]:
    changed_files = [_text(item) for item in _as_list(payload.get("changed_files")) if _text(item)]
    files = [_text(item) for item in _as_list(payload.get("files")) if _text(item)]
    paths = [_text(item) for item in _as_list(payload.get("paths")) if _text(item)]
    changed = changed_files or files or paths
    diff_text = _text(payload.get("diff") or payload.get("patch") or payload.get("text"))
    diff_stat = _text(payload.get("diff_stat") or payload.get("stat") or payload.get("summary"))
    out: dict[str, Any] = {}
    if changed:
        out["changed_files"] = changed
    if files:
        out["files"] = files
    if paths:
        out["paths"] = paths
    if diff_stat:
        out["diff_stat"] = _summarize_text(diff_stat)
    if diff_text:
        out["diff_summary"] = _summarize_text(diff_text)
        out["truncated"] = len(diff_text) > MAX_TEXT_CHARS
    if payload.get("truncated") is not None:
        out["truncated"] = bool(payload.get("truncated"))
    return out


def _test_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
    stdout = _text(payload.get("stdout") or payload.get("stdout_summary"))
    stderr = _text(payload.get("stderr") or payload.get("stderr_summary"))
    exit_code = _int_or_none(
        payload.get("exit_code") if "exit_code" in payload else payload.get("returncode")
    )
    passed = payload.get("passed")
    failed = payload.get("failed")
    if passed is None and exit_code is not None:
        passed = exit_code == 0
    if failed is None and exit_code is not None:
        failed = exit_code != 0
    failures = _as_list(payload.get("failures")) or _important_lines("\n".join([stdout, stderr]))
    return {
        "command": _text(payload.get("command")),
        "cwd": _text(payload.get("cwd")),
        "status": _text(payload.get("status")) or ("passed" if passed else "failed" if failed else ""),
        "passed": passed,
        "failed": failed,
        "exit_code": exit_code,
        "failures": [_text(item) for item in failures if _text(item)][:IMPORTANT_LINE_LIMIT],
        "important_lines": _important_lines("\n".join([stdout, stderr])),
        "stdout_summary": _summarize_text(stdout),
        "stderr_summary": _summarize_text(stderr),
        "truncated": bool(payload.get("truncated")) or len(stdout) > MAX_TEXT_CHARS or len(stderr) > MAX_TEXT_CHARS,
    }


def _is_test_command(command: str) -> bool:
    text = _text(command).lower().strip()
    if not text:
        return False
    markers = (
        "pytest",
        "python -m pytest",
        "npm test",
        "npm run test",
        "pnpm test",
        "pnpm run test",
        "yarn test",
        "go test",
        "cargo test",
    )
    return any(marker in text for marker in markers)


def _payload_text(payload: dict[str, Any]) -> str:
    for key in ("text", "delta", "summary", "msg", "error", "result", "reasoning", "thinking"):
        value = _text(payload.get(key))
        if value:
            return value
    item = _as_dict(payload.get("item") or payload.get("message"))
    for key in ("text", "content", "aggregated_output", "summary"):
        value = _text(item.get(key))
        if value:
            return value
    return _dump(_compact_payload(payload)) if payload else ""


def _summarize_text(text: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    text = _text(text)
    if len(text) <= max_chars:
        return text
    important = _important_lines(text)
    parts = [f"[truncated {len(text)} chars]"]
    if important:
        parts.append("important lines:\n" + "\n".join(important))
    parts.append("head:\n" + text[:SUMMARY_EDGE_CHARS])
    parts.append("tail:\n" + text[-SUMMARY_EDGE_CHARS:])
    summary = "\n".join(parts)
    return summary[:max_chars]


def _important_lines(text: str) -> list[str]:
    markers = (
        "error", "failed", "failure", "traceback", "exception", "assert", "warning",
        ".py", ".js", ".ts", ".tsx", ".json", ".md", "passed", "failed",
    )
    out: list[str] = []
    for line in _text(text).splitlines():
        clean = line.strip()
        if not clean:
            continue
        low = clean.lower()
        if any(marker in low for marker in markers):
            out.append(clean[:500])
        if len(out) >= IMPORTANT_LINE_LIMIT:
            break
    return out


def _compact_payload(value: Any, *, max_text: int = MAX_TEXT_CHARS) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"stdout", "stderr", "aggregated_output", "output"} and isinstance(item, str):
                out[f"{key}_summary"] = _summarize_text(item, max_chars=max_text)
                out[f"{key}_truncated"] = len(item) > max_text
            else:
                out[str(key)] = _compact_payload(item, max_text=max_text)
        return out
    if isinstance(value, list):
        return [_compact_payload(item, max_text=max_text) for item in value[:50]]
    if isinstance(value, str):
        return _summarize_text(value, max_chars=max_text)
    return value


def _stop_status(payload: dict[str, Any]) -> str:
    status = _text(payload.get("status"))
    if status:
        return status
    if payload.get("cancelled"):
        return "cancelled"
    if payload.get("interrupted"):
        return "interrupted"
    hook = _text(payload.get("hook"))
    if hook in {"Stop", "SubagentStop"}:
        return "completed"
    if payload.get("error") or payload.get("msg"):
        return "failed"
    returncode = _int_or_none(payload.get("returncode"))
    if returncode is not None and returncode != 0:
        return "failed"
    return "completed"


def _merge_runtime_anchor(state: RuntimeState, payload: dict[str, Any]) -> None:
    for key in ("cwd", "workspace", "worktree", "branch", "base_ref", "head_sha"):
        value = _text(payload.get(key))
        if value:
            setattr(state, key, value)


def _merge_runtime_collections(state: RuntimeState, payload: dict[str, Any]) -> None:
    _merge_changed_files(state, payload)
    for test in _as_list(payload.get("tests") or payload.get("test_results")):
        if isinstance(test, dict):
            state.last_tests.append(_test_result_payload(test))
        elif _text(test):
            state.last_tests.append({"summary": _text(test)})
    for step in _as_list(payload.get("next_actions") or payload.get("next_steps")):
        text = _text(step)
        if text and text not in state.next_steps:
            state.next_steps.append(text)


def _merge_changed_files(state: RuntimeState, payload: dict[str, Any]) -> None:
    for path in _as_list(payload.get("changed_files") or payload.get("files") or payload.get("paths")):
        text = _text(path)
        if text and text not in state.changed_files:
            state.changed_files.append(text)


def _new_agent(agent_id: str) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "handle_id": "",
        "agent_role": "",
        "agent_type": "",
        "parent_agent_id": "",
        "status": "unknown",
        "cwd": "",
        "worktree": "",
        "branch": "",
        "base_ref": "",
        "head_sha": "",
        "native_session_id": "",
        "pid": None,
        "command": [],
        "model": "",
        "effort": "",
        "transcript_path": "",
        "returncode": None,
        "error": "",
        "task_status": "",
        "process_status": "",
        "last_seen_at": "",
        "last_meaningful_output": {},
    }


def _merge_agent_fields(agent: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in (
        "handle_id",
        "agent_role",
        "agent_type",
        "parent_agent_id",
        "cwd",
        "worktree",
        "branch",
        "base_ref",
        "head_sha",
        "native_session_id",
        "model",
        "effort",
        "transcript_path",
        "error",
    ):
        value = _text(payload.get(key))
        if value:
            agent[key] = value
    if _text(payload.get("cwd")) and not agent.get("worktree"):
        agent["worktree"] = _text(payload.get("cwd"))
    if payload.get("pid") is not None:
        agent["pid"] = payload.get("pid")
    if payload.get("returncode") is not None:
        agent["returncode"] = payload.get("returncode")
    command = payload.get("command")
    if command:
        agent["command"] = command


def _merge_task_state(agents: dict[str, dict[str, Any]], tasks: list[Task]) -> None:
    for task in tasks:
        handle_id = _text(getattr(task, "agent_handle", ""))
        if not handle_id:
            continue
        agent = agents.setdefault(handle_id, _new_agent(handle_id))
        agent["handle_id"] = handle_id
        status = _text(getattr(task, "status", ""))
        if status:
            agent["task_status"] = status
            if agent.get("status") in {"", "unknown"}:
                agent["status"] = status


def _merge_runner_state(
    state: RuntimeState,
    agents: dict[str, dict[str, Any]],
    runner: Any,
    session_id: str,
) -> None:
    if runner is None:
        return
    handle = None
    try:
        handle_for_session = getattr(runner, "handle_for_session", None)
        if callable(handle_for_session):
            handle = handle_for_session(session_id)
    except Exception:
        handle = None
    handles = []
    if handle is not None:
        handles.append(handle)
    try:
        for item in getattr(runner, "handles", {}).values():
            if getattr(item, "session_id", "") == session_id and item not in handles:
                handles.append(item)
    except Exception:
        pass
    for item in handles:
        agent_id = _text(getattr(item, "id", "")) or _text(getattr(item, "pid", ""))
        if not agent_id:
            continue
        agent = agents.setdefault(agent_id, _new_agent(agent_id))
        process_status = _runner_process_status(runner, item)
        if process_status == "alive":
            agent["status"] = "running"
        elif agent.get("status") in {"", "unknown"}:
            agent["status"] = "unknown"
        agent["handle_id"] = agent_id
        agent["cwd"] = _text(getattr(item, "cwd", "")) or agent.get("cwd", "")
        agent["worktree"] = _text(getattr(item, "worktree", "")) or agent.get("worktree", "") or agent.get("cwd", "")
        agent["branch"] = _text(getattr(item, "branch", "")) or agent.get("branch", "")
        agent["base_ref"] = _text(getattr(item, "base_ref", "")) or agent.get("base_ref", "")
        agent["head_sha"] = _text(getattr(item, "head_sha", "")) or agent.get("head_sha", "")
        agent["pid"] = getattr(item, "pid", None)
        agent["native_session_id"] = _text(getattr(item, "native_session_id", "")) or agent.get("native_session_id", "")
        agent["model"] = _text(getattr(item, "model", "")) or agent.get("model", "")
        agent["effort"] = _text(getattr(item, "effort", "")) or agent.get("effort", "")
        agent["agent_type"] = _text(getattr(item, "agent_type", "")) or _text(getattr(item, "source", "")) or agent.get("agent_type", "")
        if process_status:
            agent["process_status"] = process_status
        command = getattr(item, "command", None)
        if command:
            agent["command"] = command
            agent["last_meaningful_output"] = {"type": "handle", "command": command}


def _runner_process_status(runner: Any, handle: Any) -> str:
    watcher = getattr(runner, "process_watcher", None) or getattr(runner, "watcher", None)
    pid = getattr(handle, "pid", None)
    if watcher is None or pid is None or not hasattr(watcher, "poll"):
        return "alive"
    key = _text(getattr(handle, "id", "")) or _text(pid)
    try:
        status = watcher.poll(key, pid)
    except Exception:
        return "unknown"
    alive = getattr(status, "alive", None)
    if alive is True:
        return "alive"
    if alive is False:
        return "dead"
    return "unknown"


def _tasks_for_session(store: Any, session_id: str) -> list[Task]:
    if store is None or not hasattr(store, "session"):
        return []
    try:
        from sqlmodel import select

        with store.session() as db:
            return list(db.exec(select(Task).where(Task.session_id == session_id)).all())
    except Exception:
        return []


def _command_state(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "command": _text(payload.get("command")),
        "cwd": _text(payload.get("cwd")),
        "exit_code": payload.get("exit_code"),
        "ok": payload.get("ok"),
        "summary": _text(payload.get("stdout_summary") or payload.get("stderr_summary")),
    }


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    return text in {"true", "1", "yes", "ok", "success"}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_id_part(value: str, limit: int) -> str:
    text = "".join(ch if ch.isalnum() else "_" for ch in _text(value))[:limit].strip("_")
    return text or "none"


__all__ = [
    "ActiveContext",
    "ContextCompactError",
    "ContextManager",
    "ContextRestoreWarning",
    "ContextUsage",
    "LANE_DETAIL",
    "LANE_MEMORY",
    "LANE_NOISE",
    "LANE_PLAN",
    "LANE_RUNTIME",
    "LANE_SYSTEM",
    "LANE_TASK",
    "ReplacementHistoryItem",
    "RuntimeState",
    "build_pm_envelope",
    "classify_user_intent",
    "estimate_active_context_tokens",
    "estimate_context_usage",
    "extract_runtime_state",
    "make_frame_id",
    "materialize_event",
    "materialize_session",
    "record_event",
    "render_active_context",
    "runtime_state_dict",
    "should_hard_compact",
    "should_soft_compact",
]
