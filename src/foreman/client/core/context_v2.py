"""Context v2 frame materialization and runtime-state helpers.

This module is deliberately not wired into PM plan/review yet. Raw Event rows remain the source of
truth; ContextFrame rows are deterministic, replayable active-context material derived from them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from foreman.shared.events import utc_now_iso

from .pm_contract import PlanContract
from ..store.models import ContextCheckpoint, ContextFrame, ContextSnapshot, Event, Session

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


class ContextManager:
    """Thin Store-backed facade for Context v2 replay and runtime-state extraction."""

    def __init__(self, store: Any, *, runner: Any = None, clock=None) -> None:
        self.store = store
        self.runner = runner
        self._clock = clock or utc_now_iso

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
        return extract_runtime_state(session, frames or [], runner=runner or self.runner)

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
            if parsed["corrupted"]:
                degraded = True
                restore_mode = "raw_frames_degraded"
                warnings.append({"code": "corrupted_checkpoint", "message": "Latest context checkpoint JSON is corrupted."})
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
                    {**base, **command},
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
                {**base, "text": _summarize_text(_payload_text(payload)), "payload": _compact_payload(payload)},
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
                {**base, "payload": _compact_payload(payload), "status": _stop_status(payload)},
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
            agent["last_seen_at"] = frame.event_ts or frame.created_at or agent.get("last_seen_at", "")
            agent["last_meaningful_output"] = {"type": frame.type, "payload": payload}
            _merge_runtime_anchor(state, payload)
        elif frame.type == "agent_start":
            agent_id = frame.agent_id or _text(payload.get("agent_id")) or frame.id
            agent = agents.setdefault(agent_id, _new_agent(agent_id))
            agent.update(
                {
                    "agent_role": frame.agent_role or _text(payload.get("agent_role")),
                    "agent_type": frame.agent_type or _text(payload.get("agent_type")),
                    "parent_agent_id": frame.parent_agent_id,
                    "status": _text(payload.get("status")) or "running",
                    "cwd": _text(payload.get("cwd")) or agent.get("cwd", ""),
                    "worktree": _text(payload.get("worktree")) or _text(payload.get("cwd")) or agent.get("worktree", ""),
                    "branch": _text(payload.get("branch")) or agent.get("branch", ""),
                    "native_session_id": _text(payload.get("native_session_id")) or agent.get("native_session_id", ""),
                    "pid": payload.get("pid") if payload.get("pid") is not None else agent.get("pid"),
                    "model": _text(payload.get("model")) or agent.get("model", ""),
                    "effort": _text(payload.get("effort")) or agent.get("effort", ""),
                    "transcript_path": _text(payload.get("transcript_path")) or agent.get("transcript_path", ""),
                    "last_seen_at": frame.event_ts or frame.created_at or agent.get("last_seen_at", ""),
                }
            )
            _merge_runtime_anchor(state, payload)
        elif frame.type == "agent_stop":
            agent_id = frame.agent_id or _text(payload.get("agent_id")) or frame.id
            agent = agents.setdefault(agent_id, _new_agent(agent_id))
            agent["status"] = _text(payload.get("status")) or "completed"
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
    if any(marker in text for marker in (
        "fix", "implement", "test", "refactor", "modify", "patch", "bug",
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
        "environment": envelope.get("environment", {}),
        "agents": envelope.get("agents", {}),
        "context": envelope.get("context", {}),
        "output_contract": envelope.get("output_contract", {}),
        "validator_rules": envelope.get("validator_rules", {}),
        "tools": envelope.get("tools", {}),
        "warnings": envelope.get("warnings", []),
        "degraded": envelope.get("degraded", False),
    }
    text = json.dumps(_compact_payload(ordered), ensure_ascii=False, indent=2)
    return _summarize_text(text, max_chars=8000)


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
    replacement_raw = _loads_or_none(checkpoint.replacement_history_json)
    cursor_raw = _loads_or_none(checkpoint.source_cursor_json)
    summary_raw = _loads_or_none(checkpoint.summary_json)
    runtime_raw = _loads_or_none(checkpoint.runtime_state_json)
    for value in (replacement_raw, cursor_raw, summary_raw, runtime_raw):
        if value is None:
            corrupted = True
    replacement_history: list[dict[str, Any]] = []
    if isinstance(replacement_raw, dict):
        items = replacement_raw.get("items")
        if isinstance(items, list):
            replacement_history = [_compact_payload(item) for item in items if isinstance(item, dict)]
    return {
        "corrupted": corrupted,
        "replacement_history": replacement_history,
        "source_cursor": cursor_raw if isinstance(cursor_raw, dict) else {},
    }


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
    if not event_ts:
        return list(frames)
    out: list[ContextFrame] = []
    matched = not event_id
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
        "pid", "command", "model", "effort", "native_session_id", "transcript_path",
        "agent_id", "agent_role", "agent_type", "parent_agent_id",
    ):
        if key in payload and payload.get(key) not in (None, ""):
            out[key] = payload.get(key)
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
    hook = _text(payload.get("hook"))
    if hook in {"Stop", "SubagentStop"}:
        return "completed"
    if payload.get("error"):
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
        "agent_role": "",
        "agent_type": "",
        "parent_agent_id": "",
        "status": "unknown",
        "cwd": "",
        "worktree": "",
        "branch": "",
        "native_session_id": "",
        "pid": None,
        "model": "",
        "effort": "",
        "transcript_path": "",
        "last_seen_at": "",
        "last_meaningful_output": {},
    }


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
        agent["status"] = "running"
        agent["cwd"] = _text(getattr(item, "cwd", "")) or agent.get("cwd", "")
        agent["pid"] = getattr(item, "pid", None)
        agent["native_session_id"] = _text(getattr(item, "native_session_id", "")) or agent.get("native_session_id", "")
        agent["model"] = _text(getattr(item, "model", "")) or agent.get("model", "")
        agent["effort"] = _text(getattr(item, "effort", "")) or agent.get("effort", "")
        command = getattr(item, "command", None)
        if command:
            agent["last_meaningful_output"] = {"type": "handle", "command": command}


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
    "extract_runtime_state",
    "make_frame_id",
    "materialize_event",
    "materialize_session",
    "record_event",
    "render_active_context",
    "runtime_state_dict",
]
