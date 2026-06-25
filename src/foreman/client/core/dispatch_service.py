"""Dispatch service — create a Root Session from the phone, and a multi-session overview.

This is the server-API side of "下发任务（手机 → PC）" (DESIGN §5.1) and the multi-session
dashboard (T4.6). The phone POSTs a goal; this service validates it, resolves the agent +
workspace, persists a Root Session + first Task (reusing ``build_session_task``), emits a
``dispatch`` event (persist-first, mirrors the Runner/Gate), and — when a ``launcher`` is wired —
kicks the agent off in the background so multiple sessions run concurrently (the Runner already
supports parallel CLIs, T1.7).

Like the Gate / CardService this is **client-side core**, INJECTED into ``server.app.create_app``
as ``dispatcher`` so app.py stays shared-only (DESIGN §14). The ``launcher`` is injectable: in the
local app it drives the real Runner; in tests a fake records the call (no real claude/codex spawned).
With no launcher the agent launch is **deferred** (``execution_deferred=True``) — the same live-hookup
convention used across P2–P4: the intake + persistence + event are delivered and unit-tested here.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import uuid
from pathlib import Path
from typing import Any

from foreman.shared.config import Config
from foreman.shared.events import make_event, utc_now_iso
from foreman.shared.i18n import normalize as normalize_lang
from foreman.shared.llm import LLMStalledError

from ..dispatch import build_session_task
from ..store.models import ContextSnapshot, MemoryItem, Task
from .context_compression import extract_json_object, memory_items_from_pack
from .pm_agent import PMPlan, events_to_text

# Bound the goal so a multi-megabyte string can't inflate every later briefing's token cost (and
# keeps the argv passed to the agent CLI sane). Truncated, not rejected, to stay friendly.
MAX_GOAL_CHARS = 8000

# Reasoning levels accepted from the phone/web (速度档位). Constrained to the set BOTH CLIs support
# (claude CLAUDE_CODE_EFFORT_LEVEL / codex model_reasoning_effort) so a level can never make codex
# reject the run. Anything else (incl. "") → "" = the CLI/model default. (DESIGN §4.2.)
VALID_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high"})
VALID_SOURCES: frozenset[str] = frozenset({"desktop", "phone", "api"})
MAX_CONTEXT_CHARS = 12000
PM_AUTO_AGENT = "pm-agent"
LIVE_SESSION_STATUSES = {"planning", "running", "active", "waiting_approval", "queued"}
TERMINAL_SESSION_STATUSES = {"failed", "cancelled", "stalled"}
AGENT_ALIASES: dict[str, tuple[str, ...]] = {
    "claude-code": ("claude-code", "claude code", "claude"),
    "codex": ("codex",),
}
CJK_AGENT_DISPATCH_TRIGGERS: tuple[str, ...] = (
    "\u7528", "\u53eb", "\u8ba9", "\u555f\u52d5", "\u542f\u52a8",
    "\u5524\u8d77", "\u904b\u884c", "\u8fd0\u884c", "\u8dd1",
    "\u8c03\u7528", "\u8abf\u7528", "\u6d3e", "\u4ea4\u7ed9",
    "\u4ea4\u7d66", "\u62a5\u5230", "\u5831\u5230",
)


def _within_any(path: str, roots: list[str]) -> bool:
    """True if ``path`` is one of ``roots`` or nested under one (workspace allowlist, §6.6)."""
    try:
        p = Path(path).resolve(strict=False)
    except (OSError, ValueError):
        return False
    for r in roots:
        try:
            rp = Path(r).resolve(strict=False)
        except (OSError, ValueError):
            continue
        if p == rp or p.is_relative_to(rp):
            return True
    return False


class DispatchService:
    """Create sessions from the phone (§5.1) and summarize all active sessions (multi-session)."""

    def __init__(
        self,
        cfg: Config,
        store: Any,
        *,
        bus: Any = None,
        launcher=None,
        runner=None,
        pm_agent=None,
        language_getter=None,
        clock=None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        # launcher(session_id, goal, workspace, agent, model, effort) -> awaitable; None = deferred.
        self.launcher = launcher
        self.runner = runner
        self.pm_agent = pm_agent
        self.language_getter = language_getter
        self._clock = clock or utc_now_iso
        self._tasks: set[asyncio.Task] = set()  # strong refs so fire-and-forget launches aren't GC'd
        self._session_tasks: dict[str, set[asyncio.Task]] = {}

    # ── create a session (下发任务, §5.1) ─────────────────────────────────────────────────────
    async def create(
        self,
        goal: str,
        *,
        workspace: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session_id: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Validate + persist a new Root Session/Task; emit ``dispatch``; optionally launch.

        Returns ``{"ok": True, session_id, task_id, goal, workspace, agent, model, effort,
        execution_deferred}``
        or ``{"ok": False, "error": ...}`` with error ∈ {empty_goal, no_store, unknown_agent,
        no_workspace}. Launching the agent (when a launcher is wired) happens in the background so a
        phone tap returns immediately and several sessions can run at once.
        """
        goal = (goal or "").strip()[:MAX_GOAL_CHARS]
        if not goal:
            return {"ok": False, "error": "empty_goal"}
        if self.store is None or not hasattr(self.store, "add_session"):
            return {"ok": False, "error": "no_store"}
        existing_session = self._get_existing_session(session_id)
        if session_id and existing_session is None:
            return {"ok": False, "error": "session_not_found"}
        pm_enabled = self.pm_agent is not None and self.runner is not None
        self._sync_pm_language()
        existing_agent = "" if pm_enabled else (existing_session.agent_type if existing_session else "")
        resolved_agent, err = self._resolve_agent(
            agent or existing_agent
        )
        if err:
            return {"ok": False, "error": err}
        ws, err = self._resolve_workspace(
            workspace or (existing_session.workspace if existing_session else "")
        )
        if err:
            return {"ok": False, "error": err}
        direct_agents: list[str] = []
        resolved_model = (
            ""
            if direct_agents
            else (model or "").strip() if pm_enabled else self._resolve_model(resolved_agent, model)
        )
        resolved_effort = "" if pm_enabled else self._resolve_effort(resolved_agent, effort)

        session_agent = "+".join(direct_agents) if direct_agents else (
            PM_AUTO_AGENT if pm_enabled else resolved_agent
        )
        if existing_session is None:
            session, task = build_session_task(self.store, goal, ws, session_agent)
            continued = False
        else:
            session = existing_session
            task = self._append_task(session.id, goal)
            continued = True
            if hasattr(self.store, "update_session"):
                self.store.update_session(session.id, status="running", updated_at=self._clock())
        deferred = self.launcher is None and not pm_enabled
        await self._emit_dispatch(
            session.id,
            task.id,
            goal,
            ws,
            session_agent,
            resolved_model,
            resolved_effort,
            deferred,
            pm_enabled,
            self._resolve_source(source),
            continued,
            direct_agents,
        )
        if direct_agents:
            launch_task = asyncio.create_task(
                self._safe_direct_launch(
                    session.id,
                    task.id,
                    goal,
                    ws,
                    direct_agents,
                    None,
                    effort,
                )
            )
            self._track_launch_task(session.id, launch_task)
        elif pm_enabled:
            launch_task = asyncio.create_task(
                self._safe_pm_launch(
                    session.id,
                    task.id,
                    goal,
                    ws,
                    resolved_agent,
                    resolved_model,
                    resolved_effort,
                )
            )
            self._track_launch_task(session.id, launch_task)
        elif self.launcher is not None:
            # Fire-and-forget: a phone dispatch returns immediately; the agent runs in the
            # background (Runner pumps its events to store+bus, T1.7). Failures emit an `error`.
            # Keep a strong ref (discarded on completion) so the task isn't GC'd mid-flight.
            launch_task = asyncio.create_task(
                self._safe_launch(
                    session.id, goal, ws, resolved_agent, resolved_model, resolved_effort
                )
            )
            self._track_launch_task(session.id, launch_task)
        return {
            "ok": True,
            "session_id": session.id,
            "task_id": task.id,
            "goal": goal,
            "workspace": ws,
            "agent": session_agent,
            "model": resolved_model,
            "effort": resolved_effort,
            "execution_deferred": deferred,
            "pm_agent": pm_enabled,
            "direct_agents": direct_agents,
            "continued": continued,
        }

    async def compact(self, session_id: str) -> dict:
        """Compact a session's event timeline into ``Session.plan`` for later follow-up prompts."""
        if self.store is None or not hasattr(self.store, "get_session"):
            return {"ok": False, "error": "no_store"}
        self._sync_pm_language()
        session = self.store.get_session(session_id)
        if session is None:
            return {"ok": False, "error": "session_not_found"}
        rows = self.store.get_events(session_id) if hasattr(self.store, "get_events") else []
        timeline = events_to_text(rows)
        if not timeline:
            return {"ok": False, "error": "no_context"}
        existing = (session.plan or "").strip()
        if self.pm_agent is not None and hasattr(self.pm_agent, "compact"):
            kwargs = {"existing_context": existing}
            if _accepts_keyword(self.pm_agent.compact, "on_stream"):
                kwargs["on_stream"] = self._pm_stream_sink(session_id, None, "compact")
            summary = await self.pm_agent.compact(session.goal, timeline, **kwargs)
        else:
            summary = _fallback_compact(timeline, existing)
        summary = (summary or "").strip()[:MAX_CONTEXT_CHARS]
        if not summary:
            return {"ok": False, "error": "no_context"}
        if hasattr(self.store, "update_session"):
            self.store.update_session(session_id, plan=summary, updated_at=self._clock())
        snapshot_id = self._store_context_derivatives(session_id, rows, summary)
        await self._persist_then_publish(
            make_event(
                "context_compact",
                "pm-agent" if self.pm_agent is not None else "foreman",
                session_id,
                payload={
                    "summary": summary,
                    "original_chars": len(timeline),
                    "summary_chars": len(summary),
                    "snapshot_id": snapshot_id,
                    "format": "context_pack_v1" if extract_json_object(summary) else "text",
                },
            )
        )
        return {
            "ok": True,
            "session_id": session_id,
            "summary": summary,
            "original_chars": len(timeline),
            "summary_chars": len(summary),
            "snapshot_id": snapshot_id,
        }

    async def cancel(self, session_id: str) -> dict:
        """Mark one session cancelled AND abort its in-flight PM ws call so Stop really stops (T2.3)."""
        if self.store is None or not hasattr(self.store, "get_session"):
            return {"ok": False, "error": "no_store"}
        session = self.store.get_session(session_id)
        if session is None:
            return {"ok": False, "error": "session_not_found"}
        # Mark terminal first so the task, as it unwinds from CancelledError, can't flip the status
        # back via `_mark_session_unless_terminal` (cancelled ∈ TERMINAL_SESSION_STATUSES).
        self._mark_session(session_id, "cancelled")
        aborted = self._cancel_session_tasks(session_id)
        msg = (
            "用户已取消会话：已请求中止正在运行的 PM 规划调用（关闭 ws）。"
            "已启动的外部 CLI 进程不在本次强杀范围内。"
            if aborted
            else "用户已取消会话。当前没有正在运行的 PM 调用；已启动的外部 CLI 进程不在本次强杀范围内。"
        )
        await self._persist_then_publish(
            make_event(
                "notification",
                "dispatch",
                session_id,
                payload={"kind": "cancelled", "msg": msg, "aborted_tasks": aborted},
            )
        )
        return {
            "ok": True,
            "session_id": session_id,
            "status": "cancelled",
            "aborted_tasks": aborted,
        }

    async def delete(self, session_id: str) -> dict:
        """Delete one local session and its local records."""
        if self.store is None or not hasattr(self.store, "delete_session"):
            return {"ok": False, "error": "no_store"}
        session = self.store.get_session(session_id) if hasattr(self.store, "get_session") else None
        if session is None:
            return {"ok": False, "error": "session_not_found"}
        if _is_live_session_status(session.status) or self._session_has_live_task(session_id):
            return {"ok": False, "error": "session_busy"}
        if not self.store.delete_session(session_id):
            return {"ok": False, "error": "session_not_found"}
        return {"ok": True, "session_id": session_id}

    def _resolve_agent(self, agent: str | None) -> tuple[str, str]:
        """Pick the agent: an explicit one must be enabled; else default to the first enabled.

        When config declares no agents (minimal/test configs) we accept any name and default to
        ``claude-code`` so dispatch still works; a real config gates explicit names to enabled ones.
        """
        enabled = sorted(k for k, a in self.cfg.agents.items() if a.enabled)
        if agent and agent.strip():
            name = agent.strip()
            if enabled and name not in enabled:
                return "", "unknown_agent"
            return name, ""
        if enabled:
            return enabled[0], ""
        return "claude-code", ""

    def _get_existing_session(self, session_id: str | None):
        sid = (session_id or "").strip()
        if not sid:
            return None
        if self.store is None or not hasattr(self.store, "get_session"):
            return None
        return self.store.get_session(sid)

    def _append_task(self, session_id: str, instruction: str) -> Task:
        now = self._clock()
        task = Task(
            id=uuid.uuid4().hex,
            session_id=session_id,
            instruction=instruction,
            status="running",
            created_at=now,
            updated_at=now,
        )
        return self.store.add_task(task)

    def _resolve_source(self, source: str | None) -> str:
        value = (source or "").strip().lower()
        return value if value in VALID_SOURCES else "api"

    def _current_language(self) -> str:
        if self.language_getter is None:
            return normalize_lang(getattr(self.pm_agent, "language", ""))
        try:
            return normalize_lang(self.language_getter())
        except Exception:  # noqa: BLE001 - language lookup must not block dispatch
            return normalize_lang(getattr(self.pm_agent, "language", ""))

    def _sync_pm_language(self) -> str:
        lang = self._current_language()
        if self.pm_agent is not None and hasattr(self.pm_agent, "language"):
            self.pm_agent.language = lang
        return lang

    def _resolve_model(self, agent: str, model: str | None) -> str:
        override = (model or "").strip()
        if override:
            return override
        cfg = self.cfg.agents.get(agent)
        return (cfg.model if cfg else "").strip()

    def _resolve_effort(self, agent: str, effort: str | None) -> str:
        """Pick the reasoning level: a valid per-dispatch override wins; else the agent config's
        default; else "" (the CLI default). An unrecognized value is ignored, never passed through."""
        override = (effort or "").strip().lower()
        if override in VALID_EFFORTS:
            return override
        cfg = self.cfg.agents.get(agent)
        cfg_effort = (getattr(cfg, "effort", "") if cfg else "").strip().lower()
        return cfg_effort if cfg_effort in VALID_EFFORTS else ""

    def _resolve_workspace(self, workspace: str | None) -> tuple[str, str]:
        """Resolve the workspace; an explicit one must sit inside an approved root (§6.6 白名单).

        Fail closed (issue #1 P2): with an allowlist, a path outside every approved root is
        rejected; with NO allowlist configured, an explicit path is rejected too — unless the
        explicit dev flag ``allow_unlisted_workspaces_for_dev`` is set — so a dispatch can never
        launch the agent in an arbitrary cwd on a server that simply forgot to declare its roots."""
        roots = [w.path for w in self.cfg.workspaces]
        allow_unlisted = getattr(self.cfg, "allow_unlisted_workspaces_for_dev", False)
        if workspace and str(workspace).strip():
            ws = str(workspace).strip()
            if roots:
                return ("", "workspace_not_allowed") if not _within_any(ws, roots) else (ws, "")
            # No allowlist: accept the explicit path ONLY in dev; otherwise fail closed.
            return (ws, "") if allow_unlisted else ("", "workspace_not_allowed")
        if roots:
            return roots[0], ""
        return "", "no_workspace"

    async def _emit_dispatch(
        self,
        session_id: str,
        task_id: str,
        goal: str,
        workspace: str,
        agent: str,
        model: str,
        effort: str,
        deferred: bool,
        pm_enabled: bool = False,
        source: str = "api",
        continued: bool = False,
        direct_agents: list[str] | None = None,
    ) -> None:
        event = make_event(
            "dispatch",
            source,
            session_id,
            task_id=task_id,
            payload={
                "goal": goal,
                "agent": agent,
                "model": model,
                "effort": effort,
                "workspace": workspace,
                # launching the agent is the two-way control layer (P4) when no launcher is wired.
                "execution_deferred": deferred,
                "pm_agent": pm_enabled,
                "continued": continued,
                "direct_agents": direct_agents or [],
            },
        )
        await self._persist_then_publish(event)

    async def _safe_pm_launch(
        self,
        session_id: str,
        task_id: str,
        goal: str,
        workspace: str,
        agent: str,
        pm_model: str,
        effort: str,
    ) -> None:
        try:
            await self._pm_launch(session_id, task_id, goal, workspace, agent, pm_model, effort)
        except Exception as exc:  # noqa: BLE001 — PM failure must be visible, not crash the server
            status = "stalled" if isinstance(exc, LLMStalledError) else "failed"
            reason = getattr(exc, "reason", "") if isinstance(exc, LLMStalledError) else ""
            self._mark_session_unless_terminal(session_id, status)
            event = make_event(
                "error",
                "pm-agent",
                session_id,
                task_id=task_id,
                payload={
                    "msg": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "status": status,
                    "reason": reason,
                },
            )
            await self._persist_then_publish(event)

    async def _safe_direct_launch(
        self,
        session_id: str,
        task_id: str,
        goal: str,
        workspace: str,
        agents: list[str],
        model_override: str | None,
        effort_override: str | None,
    ) -> None:
        try:
            await self._direct_launch(
                session_id, task_id, goal, workspace, agents, model_override, effort_override
            )
        except Exception as exc:  # noqa: BLE001 - visible background failure, not a server crash
            self._mark_session_unless_terminal(session_id, "failed")
            event = make_event(
                "error",
                "pm-agent",
                session_id,
                task_id=task_id,
                payload={"msg": f"{type(exc).__name__}: {str(exc)[:200]}"},
            )
            await self._persist_then_publish(event)

    async def _direct_launch(
        self,
        session_id: str,
        task_id: str,
        goal: str,
        workspace: str,
        agents: list[str],
        model_override: str | None,
        effort_override: str | None,
    ) -> None:
        multi = len(agents) > 1
        language = self._sync_pm_language()
        handles = []
        for agent in agents:
            model = self._resolve_model(agent, model_override)
            effort = self._resolve_effort(agent, effort_override)
            instruction = _direct_agent_instruction(goal, agent, multi=multi, language=language)
            await self._emit_pm_plan(
                session_id,
                task_id,
                PMPlan(
                    agent=agent,
                    model=model,
                    effort=effort,
                    instruction=instruction,
                    summary=_direct_agent_summary(agent, language),
                ),
            )
            handle = await self.runner.launch(
                agent, instruction, Path(workspace), session_id, model=model, effort=effort
            )
            handles.append(handle)
        await asyncio.gather(*(self.runner.wait(handle) for handle in handles))
        self._mark_session_unless_terminal(session_id, "done")

    async def _pm_launch(
        self,
        session_id: str,
        task_id: str,
        goal: str,
        workspace: str,
        agent: str,
        pm_model: str,
        effort: str,
    ) -> None:
        language = self._sync_pm_language()
        enabled_agents = [
            {
                "name": name,
                "model": cfg.model,
                "effort": getattr(cfg, "effort", ""),
                "full_access": bool(getattr(cfg, "full_access", True)),
            }
            for name, cfg in sorted(self.cfg.agents.items())
            if cfg.enabled
        ] or [{"name": agent, "model": "", "effort": effort, "full_access": True}]
        context = self._session_context(session_id)
        await self._emit_pm_status(
            session_id,
            task_id,
            "plan",
            _pm_status_text(language, "plan"),
        )
        plan_kwargs = {
            "workspace": workspace,
            "available_agents": enabled_agents,
            "requested_agent": "",
            "pm_model": pm_model,
            "requested_effort": "high",
            "fallback_instruction": _fallback_instruction(goal, context, language),
            "context": context,
        }
        if _accepts_keyword(self.pm_agent.plan, "on_stream"):
            plan_kwargs["on_stream"] = self._pm_stream_sink(session_id, task_id, "plan")
        if _accepts_keyword(self.pm_agent.plan, "on_tool_event"):
            plan_kwargs["on_tool_event"] = self._pm_tool_event_sink(session_id, task_id)
        plan = await self.pm_agent.plan(goal, **plan_kwargs)
        plan = self._sanitize_pm_plan(plan, pm_model)
        todo_status = _initial_todo_status(plan.todo)
        await self._emit_pm_plan(session_id, task_id, plan, todo_status=todo_status)
        await self._emit_pm_status(
            session_id,
            task_id,
            "launch",
            _pm_status_text(language, "launch", plan.agent),
        )
        handle = await self.runner.launch(
            plan.agent, plan.instruction, Path(workspace), session_id,
            model=plan.model, effort=plan.effort,
        )
        run_count = 1
        reviewed_event_id = ""
        review_notes: list[dict[str, Any]] = []
        review_state_key = f"{session_id}:{task_id}:pm-review"
        await self.runner.wait(handle)
        while True:
            rows = self.store.get_events(session_id)
            timeline = events_to_text(_events_after(rows, reviewed_event_id))
            review_cutoff_id = _last_event_id(rows)
            review_kwargs = {
                "run_count": run_count,
                "context": context,
                "pm_model": pm_model,
            }
            if _accepts_keyword(self.pm_agent.review, "review_state"):
                review_kwargs["review_state"] = _review_state_text(todo_status, review_notes)
            if _accepts_keyword(self.pm_agent.review, "todo_status"):
                review_kwargs["todo_status"] = todo_status
            if _accepts_keyword(self.pm_agent.review, "on_stream"):
                review_kwargs["on_stream"] = self._pm_stream_sink(
                    session_id, task_id, f"review-{run_count}"
                )
            if _accepts_keyword(self.pm_agent.review, "state_key"):
                review_kwargs["state_key"] = review_state_key
            review = await self.pm_agent.review(goal, plan, timeline, **review_kwargs)
            todo_status = _merge_todo_status(todo_status, review.todo_status, done=review.done)
            review.todo_status = todo_status
            reviewed_event_id = (
                await self._emit_pm_review(session_id, task_id, review, run_count)
            ) or review_cutoff_id
            review_notes.append(
                {
                    "run_count": run_count,
                    "done": review.done,
                    "summary": review.summary,
                    "reason": review.reason,
                    "follow_up": review.follow_up,
                }
            )
            if review.done:
                self._mark_session_unless_terminal(session_id, "done")
                return
            if run_count >= self.pm_agent.max_runs:
                await self._emit_pm_error(
                    session_id,
                    task_id,
                    _run_limit_text(language),
                )
                return
            if not review.follow_up:
                await self._emit_pm_error(
                    session_id,
                    task_id,
                    _empty_followup_text(language),
                )
                return
            await self.runner.send(handle, review.follow_up)
            self._mark_session_unless_terminal(session_id, "running")
            run_count += 1
            await self.runner.wait(handle)

    def _sanitize_pm_plan(self, plan: PMPlan, pm_model: str) -> PMPlan:
        if not pm_model or plan.model != pm_model:
            return plan
        cfg = self.cfg.agents.get(plan.agent)
        cfg_model = (cfg.model if cfg is not None else "").strip()
        if cfg_model == pm_model:
            return plan
        return PMPlan(
            agent=plan.agent,
            model=cfg_model,
            effort=plan.effort,
            instruction=plan.instruction,
            summary=plan.summary,
            todo=plan.todo,
            deliberation=plan.deliberation,
            ready=plan.ready,
            planning_rounds=plan.planning_rounds,
        )

    async def _emit_pm_plan(
        self,
        session_id: str,
        task_id: str,
        plan: PMPlan,
        *,
        todo_status: list[dict[str, str]] | None = None,
    ) -> None:
        event = make_event(
            "pm_plan",
            "pm-agent",
            session_id,
            task_id=task_id,
            payload={
                "summary": plan.summary,
                "agent": plan.agent,
                "model": plan.model,
                "effort": plan.effort,
                "instruction": plan.instruction,
                "todo": plan.todo,
                "todo_status": todo_status or [],
                "deliberation": plan.deliberation,
                "ready": plan.ready,
                "planning_rounds": plan.planning_rounds,
            },
        )
        await self._persist_then_publish(event)

    async def _emit_pm_review(self, session_id: str, task_id: str, review, run_count: int) -> str:
        event = make_event(
            "pm_review",
            "pm-agent",
            session_id,
            task_id=task_id,
            payload={
                "run_count": run_count,
                "done": review.done,
                "summary": review.summary,
                "reason": review.reason,
                "follow_up": review.follow_up,
                "todo_status": review.todo_status,
            },
        )
        await self._persist_then_publish(event)
        return event.id

    async def _emit_pm_status(
        self, session_id: str, task_id: str, phase: str, text: str
    ) -> None:
        await self._persist_then_publish(
            make_event(
                "pm_output",
                "pm-agent",
                session_id,
                task_id=task_id,
                payload={
                    "phase": phase,
                    "stream_id": f"status:{phase}",
                    "seq": 0,
                    "delta": text,
                    "event_type": "status",
                    "status": "working",
                },
            )
        )

    async def _emit_pm_error(self, session_id: str, task_id: str, msg: str) -> None:
        self._mark_session_unless_terminal(session_id, "failed")
        event = make_event(
            "error", "pm-agent", session_id, task_id=task_id, payload={"msg": msg}
        )
        await self._persist_then_publish(event)

    def _pm_stream_sink(self, session_id: str, task_id: str | None, phase: str):
        stream_id = f"{phase}:{uuid.uuid4().hex}"
        seq = 0

        async def emit(chunk: dict) -> None:
            nonlocal seq
            delta = _stream_delta(chunk)
            if not delta:
                return
            seq += 1
            kind = str(chunk.get("kind") or "output").strip().lower()
            event_type = "pm_reasoning" if kind == "reasoning" else "pm_output"
            await self._persist_then_publish(
                make_event(
                    event_type,
                    "pm-agent",
                    session_id,
                    task_id=task_id,
                    payload={
                        "phase": phase,
                        "stream_id": stream_id,
                        "seq": seq,
                        "delta": delta,
                        "event_type": str(chunk.get("event_type") or ""),
                    },
                )
            )

        return emit

    def _pm_tool_event_sink(self, session_id: str, task_id: str | None):
        async def emit(event_type: str, payload: dict[str, Any]) -> None:
            if event_type not in {"tool_pre", "tool_post"}:
                return
            await self._persist_then_publish(
                make_event(
                    event_type,
                    "pm-agent",
                    session_id,
                    task_id=task_id,
                    payload=payload,
                )
            )

        return emit

    async def _safe_launch(
        self, session_id: str, goal: str, workspace: str, agent: str, model: str, effort: str
    ) -> None:
        """Run the injected launcher; an agent that can't start records an `error` event, not a crash."""
        try:
            await self.launcher(session_id, goal, workspace, agent, model, effort)
        except Exception as exc:  # noqa: BLE001 — a launch failure must not take down the server loop
            self._mark_session_unless_terminal(session_id, "failed")
            event = make_event(
                "error",
                "dispatch",
                session_id,
                payload={"msg": f"{type(exc).__name__}: {exc}"[:200]},
            )
            await self._persist_then_publish(event)

    def _session_context(self, session_id: str) -> str:
        if self.store is None or not hasattr(self.store, "get_session"):
            return ""
        session = self.store.get_session(session_id)
        return (session.plan or "").strip()[:MAX_CONTEXT_CHARS] if session else ""

    def _mark_session(self, session_id: str, status: str) -> None:
        if self.store is not None and hasattr(self.store, "update_session"):
            self.store.update_session(session_id, status=status, updated_at=self._clock())

    def _mark_session_unless_terminal(self, session_id: str, status: str) -> None:
        if self.store is not None and hasattr(self.store, "get_session"):
            session = self.store.get_session(session_id)
            if session is not None and (session.status or "").strip().lower() in TERMINAL_SESSION_STATUSES:
                return
        self._mark_session(session_id, status)

    def _track_launch_task(self, session_id: str, task: asyncio.Task) -> None:
        self._tasks.add(task)
        self._session_tasks.setdefault(session_id, set()).add(task)

        def discard(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            session_tasks = self._session_tasks.get(session_id)
            if session_tasks is None:
                return
            session_tasks.discard(done)
            if not session_tasks:
                self._session_tasks.pop(session_id, None)

        task.add_done_callback(discard)

    def _session_has_live_task(self, session_id: str) -> bool:
        return any(not task.done() for task in self._session_tasks.get(session_id, ()))

    def _cancel_session_tasks(self, session_id: str) -> int:
        """Cancel the session's in-flight launch tasks (e.g. a running PM ws plan call).

        Cancelling the asyncio task raises CancelledError inside the awaited ``ws.recv()``, so the
        ``async with ws`` context in ``_responses_ws_once`` exits and the socket actually closes —
        the PM LLM call stops instead of streaming to completion in the background after the UI
        already marked the session cancelled (T2.3). CancelledError is a BaseException, so the
        ``_safe_*`` launch wrappers' ``except Exception`` does not swallow it; the task ends
        cancelled and its done-callback prunes ``_session_tasks``.
        """
        tasks = [t for t in self._session_tasks.get(session_id, ()) if not t.done()]
        for task in tasks:
            task.cancel()
        return len(tasks)

    async def _persist_then_publish(self, event) -> None:
        """Persist-first (so a late UI can backfill) then publish — mirrors Runner/Gate."""
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)

    # ── multi-session overview (dashboard) ────────────────────────────────────────────────────
    def _store_context_derivatives(self, session_id: str, rows: list[Any], summary: str) -> str:
        if self.store is None or not hasattr(self.store, "add_context_snapshot"):
            return ""
        now = self._clock()
        event_ids = [_event_id(row) for row in rows if _event_id(row)]
        pack = extract_json_object(summary)
        snapshot_id = uuid.uuid4().hex
        snapshot = ContextSnapshot(
            id=snapshot_id,
            session_id=session_id,
            task_id=_as_optional_str(getattr(rows[-1], "task_id", None)) if rows else None,
            kind="rolling",
            source_start_event_id=event_ids[0] if event_ids else "",
            source_end_event_id=event_ids[-1] if event_ids else "",
            source_event_ids_json=json.dumps(event_ids, ensure_ascii=False),
            summary_json=json.dumps(pack or {"text": summary}, ensure_ascii=False),
            summary_hash=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
            created_at=now,
        )
        self.store.add_context_snapshot(snapshot)
        if pack is not None and hasattr(self.store, "add_memory_item"):
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

    def overview(self) -> list[dict]:
        """All sessions with activity counts (events, last event, open cards, pending approvals).

        The phone's multi-session dashboard: see several concurrent root sessions at a glance,
        newest first. JSON-friendly dicts (caller is server app.py, shared-only, DESIGN §14)."""
        if self.store is None or not hasattr(self.store, "get_sessions"):
            return []
        pending: dict[str, int] = {}
        if hasattr(self.store, "get_pending_approvals"):
            for a in self.store.get_pending_approvals():
                pending[a.session_id] = pending.get(a.session_id, 0) + 1
        out: list[dict] = []
        for s in self.store.get_sessions():
            events = self.store.get_events(s.id) if hasattr(self.store, "get_events") else []
            last = events[-1] if events else None
            cards = (
                self.store.get_decision_cards(s.id)
                if hasattr(self.store, "get_decision_cards")
                else []
            )
            out.append(
                {
                    "id": s.id,
                    "goal": s.goal,
                    "status": s.status,
                    "agent_type": s.agent_type,
                    "workspace": s.workspace,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                    "events": len(events),
                    "last_event_ts": last.ts if last else "",
                    "last_event_type": last.type if last else "",
                    "open_cards": sum(1 for c in cards if not c.chosen),
                    "pending_approvals": pending.get(s.id, 0),
                }
            )
        out.sort(key=lambda d: d["created_at"], reverse=True)
        return out


__all__ = ["DispatchService"]


def _accepts_keyword(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    if name in sig.parameters:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def _stream_delta(chunk: dict) -> str:
    value = chunk.get("delta", "") if isinstance(chunk, dict) else ""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _pm_status_text(language: str, phase: str, agent: str = "") -> str:
    if normalize_lang(language) == "en":
        if phase == "launch":
            return f"PM selected {agent}; launching the coding agent..."
        return "PM is planning agent choice, todo list, and launch instruction..."
    if phase == "launch":
        return f"PM 已选择 {agent}，正在启动执行 agent..."
    return "PM 正在规划 agent 选择、任务清单和执行指令..."


def _run_limit_text(language: str) -> str:
    if normalize_lang(language) == "en":
        return "PM review still thinks the task is incomplete, but the run limit was reached."
    return "PM 复查仍认为任务未完成，但已达到运行次数上限。"


def _empty_followup_text(language: str) -> str:
    if normalize_lang(language) == "en":
        return "PM review asked to continue but did not provide a follow-up prompt."
    return "PM 复查要求继续，但没有给出后续指令。"


def _fallback_instruction(goal: str, context: str = "", language: str = "") -> str:
    if normalize_lang(language) == "en":
        parts = [
            "You are working under Foreman's PM supervision. Complete the user task, verify the "
            "result, and report honestly what changed and what could not be verified. Use your "
            "available file read/write/edit, shell command, and web/search tools as needed. Do not "
            "push, merge, or deploy unless the user explicitly requested it.",
        ]
    else:
        parts = [
            "你正在 Foreman 的 PM 监督下工作。完成用户任务，验证结果，并诚实汇报改动内容和"
            "无法验证的部分。按需使用可用的文件读写编辑、命令行、网页/搜索工具。除非用户"
            "明确要求，不要推送、合并或部署。",
        ]
    if context:
        label = "Existing session context" if normalize_lang(language) == "en" else "已有会话上下文"
        parts.append(f"{label}:\n{context}")
    label = "User task" if normalize_lang(language) == "en" else "用户任务"
    parts.append(f"{label}:\n{goal}")
    return "\n\n".join(parts)


def _fallback_compact(timeline: str, existing: str = "") -> str:
    text = "\n".join(part for part in [existing, timeline] if part).strip()
    if len(text) <= MAX_CONTEXT_CHARS:
        return text
    return "...[context compacted to latest events]...\n" + text[-MAX_CONTEXT_CHARS:]


def _initial_todo_status(items: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i, title in enumerate(items):
        text = str(title or "").strip()
        if text:
            out.append({"title": text, "status": "in_progress" if i == 0 else "pending"})
    return out


def _merge_todo_status(
    current: list[dict[str, str]], update: list[dict[str, str]], *, done: bool
) -> list[dict[str, str]]:
    valid = {"pending", "in_progress", "done", "blocked"}
    by_title = {
        str(item.get("title") or "").strip(): str(item.get("status") or "pending").strip()
        for item in current
        if str(item.get("title") or "").strip()
    }
    order = [str(item.get("title") or "").strip() for item in current]
    for item in update or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        status = str(item.get("status") or "pending").strip().lower()
        if status == "completed":
            status = "done"
        elif status in {"active", "running"}:
            status = "in_progress"
        if status not in valid:
            status = "pending"
        if title not in by_title:
            order.append(title)
        by_title[title] = status
    if done:
        for title in order:
            by_title[title] = "done"
    return [{"title": title, "status": by_title[title]} for title in order if title]


def _explicit_agent_targets(goal: str, enabled_agents: list[str]) -> list[str]:
    text = (goal or "").casefold()
    if not text:
        return []
    enabled = set(enabled_agents)
    found: list[tuple[int, str]] = []
    for agent, aliases in AGENT_ALIASES.items():
        if agent not in enabled:
            continue
        hits = [text.find(alias) for alias in aliases if text.find(alias) >= 0]
        if hits:
            found.append((min(hits), agent))
    if not found:
        return []
    found.sort()
    ordered = [agent for _, agent in found]
    if len(ordered) > 1:
        return ordered if _looks_like_agent_dispatch(text) else []
    return ordered if _looks_like_single_agent_dispatch(text, found[0][0], ordered[0]) else []


def _looks_like_agent_dispatch(text: str) -> bool:
    triggers = (
        *CJK_AGENT_DISPATCH_TRIGGERS,
        "use ", "run ", "launch ", "start ", "ask ", "have ", "dispatch ",
        "report", "check in", "say hi",
    )
    return any(trigger in text for trigger in triggers)


def _looks_like_single_agent_dispatch(text: str, index: int, agent: str) -> bool:
    window = text[max(0, index - 8): index + 32]
    if any(trigger in window for trigger in CJK_AGENT_DISPATCH_TRIGGERS):
        return True
    aliases = "|".join(re.escape(alias) for alias in AGENT_ALIASES[agent])
    return bool(
        re.search(rf"\b(use|run|launch|start|ask|have|dispatch|via|with)\s+(the\s+)?({aliases})\b", text)
        or re.search(rf"\b({aliases})\b.{{0,30}}\b(report|check in|say hi)\b", text)
    )


def _direct_agent_summary(agent: str, language: str = "") -> str:
    if normalize_lang(language) == "en":
        return f"Foreman direct dispatch to {agent}."
    return f"Foreman 直接下发给 {agent}。"


def _is_live_session_status(status: str | None) -> bool:
    return (status or "").strip().lower() in LIVE_SESSION_STATUSES


def _direct_agent_instruction(
    goal: str, agent: str, *, multi: bool, language: str = ""
) -> str:
    if not multi:
        if normalize_lang(language) == "zh":
            return (
                f"{goal}\n\n"
                "Foreman 已用这个 CLI 可用的文件读写编辑、命令行和网页/搜索工具启动你。"
                "按需使用这些能力，验证结果，并且除非用户明确要求，不要推送、合并或部署。"
            )
        return (
            f"{goal}\n\n"
            "Foreman has launched you with the available file read/write/edit, shell command, "
            "and web/search tools for this CLI. Use them as needed, verify the result, and do not "
            "push, merge, or deploy unless the user explicitly requested it."
        )
    if normalize_lang(language) == "zh":
        return (
            f"{goal}\n\n"
            f"Foreman 正在直接同时派发多个指定 agent。你是 {agent}。"
            "不要通过 shell 调用其他编码 agent；只完成你自己的部分，并简短报告结果。"
        )
    return (
        f"{goal}\n\n"
        f"Foreman is dispatching multiple requested agents directly. You are {agent}. "
        "Do not invoke another coding agent from shell; complete only your own part and report "
        "your result succinctly."
    )


def _events_after(rows: list[Any], event_id: str) -> list[Any]:
    marker = (event_id or "").strip()
    if not marker:
        return rows
    for idx, row in enumerate(rows):
        if _event_id(row) == marker:
            return rows[idx + 1:]
    return rows


def _last_event_id(rows: list[Any]) -> str:
    return _event_id(rows[-1]) if rows else ""


def _review_state_text(
    todo_status: list[dict[str, str]], review_notes: list[dict[str, Any]]
) -> str:
    state = {
        "todo_status": todo_status,
        "prior_reviews": review_notes[-4:],
        "timeline_rule": (
            "The captured timeline below is only new activity since the prior PM review. "
            "Use prior_reviews and todo_status as the carried loop state."
        ),
    }
    return json.dumps(state, ensure_ascii=False)


def _event_id(row: Any) -> str:
    return str(getattr(row, "id", "") or "").strip()


def _as_optional_str(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
