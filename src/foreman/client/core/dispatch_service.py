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
import subprocess
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from foreman.shared.config import Config
from foreman.shared.events import make_event, utc_now_iso
from foreman.shared.llm.trace import trace_context
from foreman.shared.i18n import normalize as normalize_lang
from foreman.shared.llm import LLMStalledError

from ..dispatch import build_session_task
from ..store.models import ContextSnapshot, MemoryItem, Task
from .context_budget import (
    approx_tokens as _ctx_approx_tokens,
    resolve_window_tokens,
    should_auto_compact,
)
from .context_compression import extract_json_object, memory_items_from_pack
from .context_v2 import ActiveContext, ContextManager
from .pm_agent import PMPlan, events_to_text
from .supervisor import ERRORED, classify_tail
from .work_mode_context import (
    WORKMODE_BODY_MAX_CHARS,
    WORKMODE_INDEX_MAX_TOKENS,
    WorkModeResolver,
    approx_tokens,
    fit_l0_index,
    make_scorer,
    render_l0_index,
)

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
CONTINUE_MODES = {"queue", "interrupt"}
FATAL_AGENT_MARKERS = (
    "401",
    "403",
    "unauthorized",
    "authentication_failed",
    "invalid authentication credentials",
    "invalid api key",
    "login expired",
    "executable not found",
    "agent disabled",
)
AGENT_ALIASES: dict[str, tuple[str, ...]] = {
    "claude-code": ("claude-code", "claude code", "claude"),
    "codex": ("codex",),
    "copilot-cli": ("copilot-cli", "copilot cli", "copilot"),
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
        injector=None,
        embedder=None,
        workflow_engine=None,
        context_manager=None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        # launcher(session_id, goal, workspace, agent, model, effort) -> awaitable; None = deferred.
        self.launcher = launcher
        self.runner = runner
        self.pm_agent = pm_agent
        # WorkspaceInjector (P2 §7): writes selected work modes into the workspace before launch and
        # clears them after. None = no coding-agent channel (zero injection, zero residue).
        self.injector = injector
        # Optional async embedder (P3): enables semantic work-mode retrieval when work_mode.
        # semantic_search is on. None or off → pure lexical (default).
        self._embedder = embedder
        # Optional WorkflowEngine (P5 §10) for lightweight per-step dispatch (set after construction
        # in local_app since the two are built together). None → no workflow step dispatch.
        self.workflow_engine = workflow_engine
        self.language_getter = language_getter
        self._clock = clock or utc_now_iso
        self.context_manager = context_manager
        if self.context_manager is None and self.store is not None:
            self.context_manager = ContextManager(self.store, runner=runner, clock=self._clock)
        self._tasks: set[asyncio.Task] = set()  # strong refs so fire-and-forget launches aren't GC'd
        self._session_tasks: dict[str, set[asyncio.Task]] = {}
        self._session_queue_tails: dict[str, asyncio.Future[None]] = {}
        self._session_queue_locks: dict[str, asyncio.Lock] = {}
        self._stop_after_reply_counts: dict[str, int] = {}

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
        continue_mode: str | None = None,
        work_mode_ids: list[str] | None = None,
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
        mode = self._continue_mode(continue_mode)
        live_tasks = (
            [task for task in self._session_tasks.get(existing_session.id, ()) if not task.done()]
            if existing_session is not None
            else []
        )
        if existing_session is not None and live_tasks and mode == "interrupt":
            self._clear_stop_after_reply(existing_session.id)
            await self._interrupt_active_work(existing_session.id)
            live_tasks = []
        pm_enabled = self.pm_agent is not None and self.runner is not None
        self._sync_pm_language()
        explicit_agent = bool((agent or "").strip())
        existing_agent = "" if pm_enabled else (existing_session.agent_type if existing_session else "")
        resolved_agent, err = self._resolve_agent(
            agent or existing_agent
        )
        if err:
            return {"ok": False, "error": err}
        requested_workspace = workspace or (
            self._effective_session_workspace(existing_session) if existing_session else ""
        )
        ws, err = self._resolve_workspace(requested_workspace, session=existing_session)
        if err:
            return {"ok": False, "error": err}
        direct_agents: list[str] = [resolved_agent] if pm_enabled and explicit_agent else []
        if direct_agents:
            resolved_model = self._resolve_model(resolved_agent, model)
            resolved_effort = self._resolve_effort(resolved_agent, effort)
        else:
            resolved_model = (model or "").strip() if pm_enabled else self._resolve_model(
                resolved_agent, model
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
            mode,
        )
        if direct_agents:
            launch_task = asyncio.create_task(
                self._safe_direct_launch(
                    session.id,
                    task.id,
                    goal,
                    ws,
                    direct_agents,
                    resolved_model,
                    resolved_effort,
                )
            )
            self._track_launch_task(session.id, launch_task)
        elif pm_enabled:
            wait_for_tasks = None
            queue_tail = None
            if existing_session is not None and mode == "queue":
                wait_for_tasks, queue_tail = await self._reserve_queue_wait(existing_session.id)
            launch_task = asyncio.create_task(
                self._safe_pm_launch(
                    session.id,
                    task.id,
                    goal,
                    ws,
                    resolved_agent,
                    resolved_model,
                    resolved_effort,
                    wait_for_tasks=wait_for_tasks,
                    work_mode_ids=list(work_mode_ids or []),
                )
            )
            self._track_launch_task(session.id, launch_task)
            if queue_tail is not None:
                self._attach_queue_tail(session.id, queue_tail, launch_task)
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
            "continue_mode": mode,
        }

    async def _resolve_window_tokens(self, pm_model: str = "") -> int:
        """The PM model's effective context window (tokens), resolved once per dispatch. Falls back to
        the budgeter's default when the brain/llm can't report it (§8B / context_budget)."""
        llm = getattr(self.pm_agent, "llm", None) if self.pm_agent is not None else None
        if llm is None:
            from .context_budget import DEFAULT_CTX_WINDOW_TOKENS, OUTPUT_RESERVE_TOKENS
            return DEFAULT_CTX_WINDOW_TOKENS - OUTPUT_RESERVE_TOKENS
        return await resolve_window_tokens(llm, pm_model)

    async def _safe_compact(self, session_id: str, window_tokens: int) -> None:
        """Auto-compact wrapper: a compact failure must never break the live dispatch loop."""
        try:
            await self.compact(session_id, window_tokens=window_tokens)
        except Exception:  # noqa: BLE001 — auto-compaction is best-effort
            return

    async def compact(self, session_id: str, *, window_tokens: int | None = None) -> dict:
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
        if window_tokens is None:
            window_tokens = await self._resolve_window_tokens("")
        existing = (session.plan or "").strip()
        if self.pm_agent is not None and hasattr(self.pm_agent, "compact"):
            kwargs = {"existing_context": existing}
            if _accepts_keyword(self.pm_agent.compact, "on_stream"):
                kwargs["on_stream"] = self._pm_stream_sink(session_id, None, "compact")
            if _accepts_keyword(self.pm_agent.compact, "window_tokens"):
                kwargs["window_tokens"] = window_tokens
            with trace_context(session_id=session_id, phase="compact"):
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
                    "before_tokens": _ctx_approx_tokens(timeline),
                    "after_tokens": _ctx_approx_tokens(summary),
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

    def _continue_mode(self, value: str | None) -> str:
        mode = (value or "queue").strip().lower()
        return mode if mode in CONTINUE_MODES else "queue"

    def _request_stop_after_reply(self, session_id: str) -> None:
        self._stop_after_reply_counts[session_id] = self._stop_after_reply_counts.get(session_id, 0) + 1

    def _clear_stop_after_reply(self, session_id: str) -> None:
        self._stop_after_reply_counts.pop(session_id, None)

    def _consume_stop_after_reply(self, session_id: str) -> bool:
        remaining = self._stop_after_reply_counts.get(session_id, 0)
        if remaining <= 0:
            return False
        if remaining == 1:
            self._stop_after_reply_counts.pop(session_id, None)
        else:
            self._stop_after_reply_counts[session_id] = remaining - 1
        return True

    def _queue_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_queue_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_queue_locks[session_id] = lock
        return lock

    async def _reserve_queue_wait(
        self, session_id: str
    ) -> tuple[list[Awaitable[Any]] | None, asyncio.Future[None] | None]:
        """Reserve this follow-up's place in a per-session queue chain.

        A queued PM follow-up should start as soon as the previous reply boundary completes, not
        after the whole PM loop. Multiple queue requests can arrive while the same live task is
        still busy; without a tail, each one would wait only for that original live task and then
        race to start together. The placeholder tail is installed before the new launch task exists,
        so concurrent queue requests chain behind it deterministically.
        """
        async with self._queue_lock(session_id):
            live_tasks = [task for task in self._session_tasks.get(session_id, ()) if not task.done()]
            if not live_tasks:
                self._session_queue_tails.pop(session_id, None)
                return None, None
            self._request_stop_after_reply(session_id)
            previous_tail = self._session_queue_tails.get(session_id)
            if previous_tail is not None and previous_tail.done():
                previous_tail = None
            wait_for_tasks: list[Awaitable[Any]] = (
                [previous_tail] if previous_tail is not None else list(live_tasks)
            )
            tail: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._session_queue_tails[session_id] = tail
            return wait_for_tasks, tail

    def _attach_queue_tail(
        self, session_id: str, tail: asyncio.Future[None], launch_task: asyncio.Task[Any]
    ) -> None:
        def release_tail(_done: asyncio.Task[Any]) -> None:
            if not tail.done():
                tail.set_result(None)
            if self._session_queue_tails.get(session_id) is tail:
                self._session_queue_tails.pop(session_id, None)

        launch_task.add_done_callback(release_tail)

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

    def _resolve_workspace(self, workspace: str | None, *, session=None) -> tuple[str, str]:
        """Resolve the workspace; an explicit one must sit inside an approved root (§6.6 白名单).

        Fail closed (issue #1 P2): with an allowlist, a path outside every approved root is
        rejected; with NO allowlist configured, an explicit path is rejected too — unless the
        explicit dev flag ``allow_unlisted_workspaces_for_dev`` is set — so a dispatch can never
        launch the agent in an arbitrary cwd on a server that simply forgot to declare its roots."""
        roots = [w.path for w in self.cfg.workspaces]
        allow_unlisted = getattr(self.cfg, "allow_unlisted_workspaces_for_dev", False)
        if workspace and str(workspace).strip():
            ws = str(workspace).strip()
            if self._is_recorded_session_workspace(ws, session):
                return ws, ""
            if roots:
                return ("", "workspace_not_allowed") if not _within_any(ws, roots) else (ws, "")
            # No allowlist: accept the explicit path ONLY in dev; otherwise fail closed.
            return (ws, "") if allow_unlisted else ("", "workspace_not_allowed")
        if roots:
            return roots[0], ""
        return "", "no_workspace"

    def _is_recorded_session_workspace(self, workspace: str, session) -> bool:
        """Allow a session-owned worktree even when it is outside the main workspace allowlist."""
        if session is None:
            return False
        try:
            workspace_path = Path(workspace).expanduser()
            if not workspace_path.is_dir():
                return False
            ws = workspace_path.resolve(strict=False)
        except (OSError, ValueError):
            return False
        for value in (
            getattr(session, "workspace", "") or "",
            getattr(session, "main_workspace", "") or "",
        ):
            if not value:
                continue
            try:
                recorded = Path(str(value)).expanduser()
                if recorded.is_dir() and ws == recorded.resolve(strict=False):
                    return True
            except (OSError, ValueError):
                continue
        return False

    def _effective_session_workspace(self, session) -> str:
        """Use the live session worktree when it exists; otherwise return the recorded main root."""
        if session is None:
            return ""
        workspace = (getattr(session, "workspace", "") or "").strip()
        if workspace and Path(workspace).expanduser().is_dir():
            return workspace
        return (getattr(session, "main_workspace", "") or workspace).strip()

    def _resolve_plan_workspace(self, requested: str, current: str) -> tuple[str, str]:
        """Accept a PM-selected workspace only when it is an allowed root or git worktree."""
        candidate = str(requested or "").strip()
        if not candidate:
            return current, ""
        try:
            candidate_path = Path(candidate).expanduser()
            if not candidate_path.is_dir():
                return current, "PM selected workspace does not exist."
            current_path = Path(current).expanduser()
            candidate_resolved = candidate_path.resolve(strict=False)
            current_resolved = current_path.resolve(strict=False)
        except (OSError, ValueError):
            return current, "PM selected workspace is not a valid path."
        if candidate_resolved == current_resolved:
            return current, ""
        roots = [w.path for w in self.cfg.workspaces]
        if roots and _within_any(str(candidate_resolved), roots):
            return str(candidate_path), ""
        if self._is_git_worktree_of(str(candidate_resolved), str(current_resolved)):
            return str(candidate_path), ""
        return current, "PM selected workspace is outside the configured workspace roots."

    def _is_git_worktree_of(self, candidate: str, main_workspace: str) -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", main_workspace, "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0:
            return False
        try:
            candidate_path = Path(candidate).resolve(strict=False)
            for line in result.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                worktree = Path(line[len("worktree "):].strip()).expanduser()
                if worktree.resolve(strict=False) == candidate_path:
                    return True
        except (OSError, ValueError):
            return False
        return False

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
        continue_mode: str = "queue",
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
                "continue_mode": continue_mode,
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
        *,
        wait_for_tasks: list[Awaitable[Any]] | None = None,
        work_mode_ids: list[str] | None = None,
    ) -> None:
        try:
            if wait_for_tasks:
                await asyncio.gather(*wait_for_tasks, return_exceptions=True)
                self._mark_session_unless_terminal(session_id, "running")
            await self._pm_launch(
                session_id, task_id, goal, workspace, agent, pm_model, effort,
                work_mode_ids=work_mode_ids,
            )
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
        finally:
            # P2 (§7.3): clear THIS task's injected work-mode files once the task truly ends (not per
            # follow-up). task_id-scoped so a concurrent task's scaffolding is untouched; best-effort
            # so cleanup never masks the real outcome. agents=None strips both guidance files' blocks.
            if self.injector is not None:
                try:
                    self.injector.clear(workspace, agents=None, task_id=task_id)
                except Exception:  # noqa: BLE001 — cleanup is best-effort
                    pass

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

    async def launch_workflow_step(
        self, run_id: str, *, agent: str = "", model: str = "", effort: str = ""
    ) -> dict:
        """Lightweight workflow step dispatch (P5 §10): begin_step (which injects the step's material
        via the P2 injector) → turn the step goal + L0 index into ONE coding instruction → launch →
        wait. Deliberately NOT the full PM plan→review loop (avoids ×steps cost). The caller advances
        with submit_step afterwards; this method does NOT clear (the engine clears at run end)."""
        eng = self.workflow_engine
        if eng is None:
            return {"ok": False, "error": "no_workflow_engine"}
        if self.runner is None:
            return {"ok": False, "error": "no_runner"}
        begun = eng.begin_step(run_id)  # SYNC; injects step material into the workspace
        if not begun.get("ok"):
            return begun
        step = begun["step"]
        run = step.get("run") or {}
        session_id = run.get("session_id") or ""
        session = self.store.get_session(session_id) if (
            self.store is not None and session_id and hasattr(self.store, "get_session")
        ) else None
        workspace = (getattr(session, "workspace", "") or "") if session else ""
        if not workspace:
            return {"ok": False, "error": "no_workspace"}
        resolved_agent = (
            agent or (getattr(session, "agent_type", "") if session else "") or "claude-code"
        )
        language = self._sync_pm_language()
        instruction = _workflow_step_instruction(step, language=language)
        handle = await self.runner.launch(
            resolved_agent, instruction, Path(workspace), session_id, model=model, effort=effort
        )
        await self.runner.wait(handle)
        return {"ok": True, "run_id": run_id, "step_index": run.get("step_index", 0)}

    async def _pm_launch(
        self,
        session_id: str,
        task_id: str,
        goal: str,
        workspace: str,
        agent: str,
        pm_model: str,
        effort: str,
        *,
        work_mode_ids: list[str] | None = None,
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
        # Resolve the PM model window ONCE per dispatch (§8B.8; never per-loop — it can hit /models).
        window_tokens = await self._resolve_window_tokens(pm_model)
        context, active_context = await self._pm_context_text(
            session_id,
            purpose="pm_plan",
            window_tokens=window_tokens,
        )
        # Pre-plan auto-compact: if the carried session memory alone is already near the window,
        # compact before planning so the plan call doesn't start over budget.
        if should_auto_compact(
            _ctx_approx_tokens(context), 0, 0, window_tokens=window_tokens, run_count=0
        ):
            await self._safe_compact(session_id, window_tokens)
            context, active_context = await self._pm_context_text(
                session_id,
                purpose="pm_plan",
                window_tokens=window_tokens,
            )
        await self._emit_pm_status(
            session_id,
            task_id,
            "plan",
            _pm_status_text(language, "plan"),
        )
        # Work-mode L0 selection (P1, §6/§8): resolve the applicable definitions ONCE. The fitted L0
        # index (no bodies) goes into the plan prompt; the same resolver backs the work_mode_search /
        # work_mode_get tools so the PM can pull bodies on demand. Manual picks (work_mode_ids) pass
        # straight through the funnel. With no active definitions this is a cheap empty resolve.
        wm_mode = getattr(getattr(self.cfg, "work_mode", None), "semantic_search", "off")
        work_mode_resolver = WorkModeResolver(
            self.store, workspace=workspace, goal=goal, agent=agent,
            manual_ids=list(work_mode_ids or []),
            scorer=make_scorer(wm_mode, self._embedder),
        )
        wm_resolved = await work_mode_resolver.aresolve()
        wm_index = fit_l0_index(wm_resolved["selected"], max_tokens=WORKMODE_INDEX_MAX_TOKENS)
        # P4 (§9, D2): soft-constraint review guidance (selected rubric bodies + standard check refs).
        # rubric_fed reflects whether it will ACTUALLY reach review (guidance present AND the review
        # signature accepts it) — telemetry must not claim a rubric drove a decision it never saw.
        review_guidance = self._build_review_guidance(wm_index)
        rubric_fed = bool(review_guidance) and _accepts_keyword(self.pm_agent.review, "qa_rubric")
        plan_kwargs: dict[str, Any] = {
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
        if _accepts_keyword(self.pm_agent.plan, "work_mode_index"):
            plan_kwargs["work_mode_index"] = wm_index
        if _accepts_keyword(self.pm_agent.plan, "work_mode_resolver"):
            plan_kwargs["work_mode_resolver"] = work_mode_resolver
        if _accepts_keyword(self.pm_agent.plan, "session_id"):
            plan_kwargs["session_id"] = session_id
        if _accepts_keyword(self.pm_agent.plan, "task_id"):
            plan_kwargs["task_id"] = task_id
        if active_context is not None and _accepts_keyword(self.pm_agent.plan, "active_context"):
            plan_kwargs["active_context"] = active_context
        with trace_context(session_id=session_id, task_id=task_id, phase="plan"):
            plan = await self.pm_agent.plan(goal, **plan_kwargs)
        # Telemetry: one work_mode event per dispatch (after plan, so pulls/body_tokens are counted).
        await self._emit_work_mode(
            session_id, task_id, wm_index, wm_resolved["dropped"], work_mode_resolver,
            session_memory_tokens=_ctx_approx_tokens(context),
        )
        plan = self._sanitize_pm_plan(plan, pm_model)
        if plan.kind == "direct_reply":
            if not (plan.reply or "").strip():
                await self._emit_pm_error(
                    session_id, task_id, _empty_direct_reply_text(language)
                )
                return
            await self._emit_pm_reply(session_id, task_id, plan)
            self._mark_session_unless_terminal(session_id, "done")
            return
        if plan.kind in {"blocked", "error"}:
            await self._emit_pm_error(session_id, task_id, _terminal_plan_text(plan, language))
            return
        plan_workspace, workspace_error = self._resolve_plan_workspace(plan.workspace, workspace)
        if workspace_error:
            await self._emit_pm_error(session_id, task_id, workspace_error)
            return
        if plan_workspace != workspace:
            workspace = plan_workspace
            if self.store is not None and hasattr(self.store, "update_session"):
                self.store.update_session(
                    session_id,
                    workspace=workspace,
                    updated_at=self._clock(),
                )
        todo_status = _initial_todo_status(plan.todo)
        await self._emit_pm_plan(session_id, task_id, plan, todo_status=todo_status)
        await self._emit_pm_status(
            session_id,
            task_id,
            "launch",
            _pm_status_text(language, "launch", plan.agent),
        )
        # P2 (§7): inject the selected work modes into the workspace BEFORE launch so the coding agent
        # reads them on startup (claude-code native .claude/skills / codex .foreman/skills + managed
        # block). Only when there's actual material — a task with NO selected skills/standards gets
        # ZERO injection / ZERO residue (P2 §4 back-compat; the plan instruction already goes to the
        # CLI directly). Best-effort: an injection failure must never abort the dispatch.
        self._inject_work_modes_for_plan(workspace, task_id, plan, wm_index)
        agent_run_cursor = _last_event_id(self.store.get_events(session_id)) if self.store else ""
        handle = await self.runner.launch(
            plan.agent, plan.instruction, Path(workspace), session_id,
            model=plan.model, effort=plan.effort,
        )
        run_count = 1
        reviewed_event_id = ""
        review_notes: list[dict[str, Any]] = []
        review_state_key = f"{session_id}:{task_id}:pm-review"
        failed_agents: set[str] = set()
        await self.runner.wait(handle)
        if self._consume_stop_after_reply(session_id):
            self._mark_session_unless_terminal(session_id, "running")
            return
        while True:
            fatal_rows = (
                _events_after(self.store.get_events(session_id), agent_run_cursor)
                if self.store else []
            )
            fatal_msg = _fatal_agent_exit_text(fatal_rows, language=language, agent=plan.agent)
            if not fatal_msg:
                break
            recovered = await self._recover_from_fatal_agent_exit(
                session_id=session_id,
                task_id=task_id,
                goal=goal,
                workspace=workspace,
                plan=plan,
                fatal_rows=fatal_rows,
                fatal_msg=fatal_msg,
                failed_agents=failed_agents,
                enabled_agents=enabled_agents,
                context=context,
                pm_model=pm_model,
                language=language,
                wm_index=wm_index,
                todo_status=todo_status,
            )
            if recovered is None:
                return
            handle, plan, agent_run_cursor, todo_status = recovered
            await self.runner.wait(handle)
            if self._consume_stop_after_reply(session_id):
                self._mark_session_unless_terminal(session_id, "running")
                return
        while True:
            rows = self.store.get_events(session_id)
            review_context, review_active_context = await self._pm_context_text(
                session_id,
                purpose="pm_review",
                window_tokens=window_tokens,
            )
            reviewed_event_id = _advance_reviewed_event_id_from_active_context(
                rows,
                reviewed_event_id,
                review_active_context,
            )
            timeline = _review_timeline_from_active_context(
                review_active_context,
                rows,
                reviewed_event_id,
            )
            review_cutoff_id = _last_event_id(rows)
            review_kwargs = {
                "run_count": run_count,
                "context": review_context,
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
            if review_active_context is not None and _accepts_keyword(self.pm_agent.review, "active_context"):
                review_kwargs["active_context"] = review_active_context
            if rubric_fed:
                review_kwargs["qa_rubric"] = review_guidance
            with trace_context(
                session_id=session_id, task_id=task_id, phase=f"review-{run_count}"
            ):
                review = await self.pm_agent.review(goal, plan, timeline, **review_kwargs)
            todo_status = _merge_todo_status(todo_status, review.todo_status, done=review.done)
            review.todo_status = todo_status
            reviewed_event_id = (
                await self._emit_pm_review(
                    session_id, task_id, review, run_count, rubric_active=rubric_fed
                )
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
            if self._consume_stop_after_reply(session_id):
                self._mark_session_unless_terminal(session_id, "running")
                return
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
            # Auto-compact (§8B.8) BETWEEN rounds — AFTER this review consumed its raw increment, and
            # BEFORE the next follow-up runs. Compact when (session memory + pulled bodies + the
            # just-reviewed timeline) crosses the window threshold OR every N runs, then fold history
            # into Session.plan and advance the cursor (no double-count; no review ever loses its
            # increment — the rolling-plan ↔ incremental-review reconciliation).
            if should_auto_compact(
                _ctx_approx_tokens(context),
                (work_mode_resolver.body_chars + 3) // 4,
                _ctx_approx_tokens(timeline),
                window_tokens=window_tokens,
                run_count=run_count,
            ):
                await self._safe_compact(session_id, window_tokens)
                context, active_context = await self._pm_context_text(
                    session_id,
                    purpose="pm_plan",
                    window_tokens=window_tokens,
                )
                reviewed_event_id = _last_event_id(self.store.get_events(session_id))
            agent_run_cursor = _last_event_id(self.store.get_events(session_id)) if self.store else ""
            await self.runner.send(handle, review.follow_up)
            self._mark_session_unless_terminal(session_id, "running")
            run_count += 1
            await self.runner.wait(handle)
            if self._consume_stop_after_reply(session_id):
                self._mark_session_unless_terminal(session_id, "running")
                return
            while True:
                fatal_rows = (
                    _events_after(self.store.get_events(session_id), agent_run_cursor)
                    if self.store else []
                )
                fatal_msg = _fatal_agent_exit_text(
                    fatal_rows, language=language, agent=plan.agent
                )
                if not fatal_msg:
                    break
                recovered = await self._recover_from_fatal_agent_exit(
                    session_id=session_id,
                    task_id=task_id,
                    goal=goal,
                    workspace=workspace,
                    plan=plan,
                    fatal_rows=fatal_rows,
                    fatal_msg=fatal_msg,
                    failed_agents=failed_agents,
                    enabled_agents=enabled_agents,
                    context=context,
                    pm_model=pm_model,
                    language=language,
                    wm_index=wm_index,
                    todo_status=todo_status,
                )
                if recovered is None:
                    return
                handle, plan, agent_run_cursor, todo_status = recovered
                await self.runner.wait(handle)
                if self._consume_stop_after_reply(session_id):
                    self._mark_session_unless_terminal(session_id, "running")
                    return

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
            workspace=plan.workspace,
            kind=plan.kind,
            reply=plan.reply,
            summary=plan.summary,
            todo=plan.todo,
            deliberation=plan.deliberation,
            ready=plan.ready,
            planning_rounds=plan.planning_rounds,
        )

    async def _recover_from_fatal_agent_exit(
        self,
        *,
        session_id: str,
        task_id: str,
        goal: str,
        workspace: str,
        plan: PMPlan,
        fatal_rows: list[Any],
        fatal_msg: str,
        failed_agents: set[str],
        enabled_agents: list[dict[str, Any]],
        context: str,
        pm_model: str,
        language: str,
        wm_index: list[dict[str, Any]],
        todo_status: list[dict[str, str]],
    ) -> tuple[Any, PMPlan, str, list[dict[str, str]]] | None:
        failed_agents.add(plan.agent)
        candidates = [
            row for row in enabled_agents
            if str(row.get("name") or "").strip() not in failed_agents
        ]
        if not candidates:
            await self._emit_pm_error(
                session_id,
                task_id,
                _all_agents_unavailable_text(language, failed_agents, fatal_msg),
            )
            return None
        if not hasattr(self.pm_agent, "recover"):
            await self._emit_pm_error(session_id, task_id, fatal_msg)
            return None
        failure_timeline = events_to_text(fatal_rows, max_chars=8000)
        recover_kwargs: dict[str, Any] = {
            "failed_agent": plan.agent,
            "available_agents": candidates,
            "context": context,
            "pm_model": pm_model,
        }
        if _accepts_keyword(self.pm_agent.recover, "on_stream"):
            recover_kwargs["on_stream"] = self._pm_stream_sink(
                session_id, task_id, f"recover-{len(failed_agents)}"
            )
        if _accepts_keyword(self.pm_agent.recover, "state_key"):
            recover_kwargs["state_key"] = f"{session_id}:{task_id}:pm-recover"
        with trace_context(
            session_id=session_id, task_id=task_id, phase=f"recover-{len(failed_agents)}"
        ):
            recovery = await self.pm_agent.recover(goal, plan, failure_timeline, **recover_kwargs)
        candidate_names = {str(row.get("name") or "").strip() for row in candidates}
        recovery_agent = str(getattr(recovery, "agent", "") or "").strip()
        if recovery_agent not in candidate_names:
            recovery_agent = str(candidates[0].get("name") or "").strip()
        recovery_plan = PMPlan(
            agent=recovery_agent,
            model=str(getattr(recovery, "model", "") or "").strip(),
            effort=str(getattr(recovery, "effort", "") or "").strip(),
            instruction=str(getattr(recovery, "instruction", "") or "").strip()
            or _recovery_fallback_instruction(goal, plan, fatal_msg, language),
            summary=str(getattr(recovery, "summary", "") or "").strip()
            or _recovery_summary(language, plan.agent, recovery_agent),
            todo=list(getattr(recovery, "todo", []) or []),
            deliberation=[str(getattr(recovery, "reason", "") or "").strip()]
            if str(getattr(recovery, "reason", "") or "").strip() else [],
        )
        recovery_plan = self._sanitize_pm_plan(recovery_plan, pm_model)
        if recovery_plan.todo:
            todo_status = _initial_todo_status(recovery_plan.todo)
        await self._emit_pm_plan(session_id, task_id, recovery_plan, todo_status=todo_status)
        await self._emit_pm_status(
            session_id,
            task_id,
            "recover",
            _pm_status_text(language, "recover", recovery_plan.agent),
        )
        self._inject_work_modes_for_plan(workspace, task_id, recovery_plan, wm_index)
        agent_run_cursor = _last_event_id(self.store.get_events(session_id)) if self.store else ""
        handle = await self.runner.launch(
            recovery_plan.agent,
            recovery_plan.instruction,
            Path(workspace),
            session_id,
            model=recovery_plan.model,
            effort=recovery_plan.effort,
        )
        self._mark_session_unless_terminal(session_id, "running")
        return handle, recovery_plan, agent_run_cursor, todo_status

    def _inject_work_modes_for_plan(
        self, workspace: str, task_id: str, plan: PMPlan, wm_index: list[dict[str, Any]]
    ) -> None:
        if self.injector is None:
            return
        material = self._build_work_mode_material(plan.instruction, wm_index)
        if not material["skills"] and not material["standards"]:
            return
        try:
            self.injector.inject(workspace, material, agents=plan.agent, task_id=task_id)
        except Exception:  # noqa: BLE001 — injection is best-effort
            pass

    def _build_work_mode_material(
        self, instruction: str, wm_index: list[dict[str, Any]]
    ) -> dict:
        """Turn the selected L0 index into injection material (§7): skills + code_standards with their
        bodies (pulled from the same resolver as work_mode_get). Skills go to the coding-agent file
        channel; standards go full-text into the managed block (D1). qa_rubric/workflow are not file-
        injected here (rubric is a review-side concern; workflow is P5)."""
        skills: list[dict] = []
        standards: list[dict] = []
        get = getattr(self.store, "get_active_definition", None) if self.store is not None else None
        for entry in wm_index:
            kind = entry.get("kind")
            name = entry.get("name")
            if kind not in ("skill", "code_standard") or not name or get is None:
                continue
            # FULL body for the file-injection channel (D1: standards go full-text into the managed
            # block). NOT resolver.body() — that applies the 6000-char PM-pull cap, which is for the
            # PM context window, not the workspace files the coding agent reads.
            row = get(kind, name)
            body = (getattr(row, "body", "") or "") if row is not None else ""
            if not body:
                continue
            item = {"name": name, "body": body, "description": entry.get("description", "")}
            (skills if kind == "skill" else standards).append(item)
        return {"instruction": instruction, "skills": skills, "standards": standards}

    def _build_review_guidance(self, wm_index: list[dict[str, Any]]) -> str:
        """Soft-constraint review text (P4 §9, D2): selected qa_rubric bodies + code_standard check
        fields, capped. Fed to PMAgent.review as the acceptance standard so it shapes done/follow_up.
        The check command is shown as a JUDGMENT REFERENCE only — it is NOT executed (the hard check
        gate is deferred to V2). Returns "" when no rubric/standard was selected (no-op, back-compat)."""
        get = getattr(self.store, "get_active_definition", None) if self.store is not None else None
        if get is None:
            return ""
        parts: list[str] = []
        for entry in wm_index:
            kind, name = entry.get("kind"), entry.get("name")
            if not name:
                continue
            if kind == "qa_rubric":
                row = get("qa_rubric", name)
                body = (getattr(row, "body", "") or "").strip() if row is not None else ""
                if body:
                    parts.append(f"## QA rubric: {name}\n{body}")
            elif kind == "code_standard":
                cmd = self._standard_check_cmd(get, name)
                if cmd:
                    parts.append(
                        f"## Code standard check ({name})\n"
                        f"The change should pass `{cmd}` (use as a judgment reference; not executed)."
                    )
        return "\n\n".join(parts)[:WORKMODE_BODY_MAX_CHARS]

    @staticmethod
    def _standard_check_cmd(get_active, name: str) -> str:
        """The `metadata.check.cmd` of an active code_standard (P4), or "" if none / non-command."""
        row = get_active("code_standard", name)
        if row is None:
            return ""
        try:
            meta = json.loads(getattr(row, "metadata_json", "{}") or "{}")
        except (TypeError, ValueError):
            return ""
        chk = meta.get("check") if isinstance(meta, dict) else None
        if isinstance(chk, dict) and chk.get("type") == "command":
            return str(chk.get("cmd") or "").strip()
        return ""

    async def _emit_work_mode(
        self,
        session_id: str,
        task_id: str,
        selected: list[dict[str, Any]],
        dropped: list[dict[str, Any]],
        resolver: WorkModeResolver,
        *,
        session_memory_tokens: int = 0,
    ) -> None:
        """Emit one ``work_mode`` telemetry event per dispatch (§8/§16): what was selected/dropped
        plus the L0 index & pulled-body token accounting and per-lane tokens (§8B.8). Tokens are ~4
        char/token approximations (P1b swaps in a real tokenizer). Metadata only — no bodies."""
        index_tokens = approx_tokens(render_l0_index(selected))
        body_tokens = (resolver.body_chars + 3) // 4
        await self._persist_then_publish(
            make_event(
                "work_mode",
                "pm-agent",
                session_id,
                task_id=task_id,
                payload={
                    "selected": [
                        {"kind": e.get("kind", ""), "name": e.get("name", ""),
                         "est_tokens": e.get("est_tokens", 0)}
                        for e in selected
                    ],
                    "dropped": [
                        {"kind": e.get("kind", ""), "name": e.get("name", "")} for e in dropped
                    ],
                    "index_tokens": index_tokens,
                    "pulls": resolver.pulls,
                    "body_tokens": body_tokens,
                    "kinds": sorted({e.get("kind", "") for e in selected}),
                    "per_lane_tokens": {
                        "l0_index": index_tokens,
                        "l1_bodies": body_tokens,
                        "session_memory": session_memory_tokens,
                    },
                    "scorer": getattr(resolver, "last_scorer", "lexical"),
                    "embed_calls": getattr(resolver, "embed_calls", 0),
                },
            )
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
                "kind": plan.kind,
                "reply": plan.reply,
                "todo": plan.todo,
                "todo_status": todo_status or [],
                "deliberation": plan.deliberation,
                "ready": plan.ready,
                "planning_rounds": plan.planning_rounds,
            },
        )
        await self._persist_then_publish(event)

    async def _emit_pm_reply(self, session_id: str, task_id: str, plan: PMPlan) -> None:
        reply = (plan.reply or "").strip()
        await self._persist_then_publish(
            make_event(
                "pm_reply",
                "pm-agent",
                session_id,
                task_id=task_id,
                payload={
                    "text": reply,
                    "summary": plan.summary,
                    "todo": plan.todo,
                    "kind": plan.kind,
                },
            )
        )

    async def _emit_pm_review(
        self, session_id: str, task_id: str, review, run_count: int, *, rubric_active: bool = False
    ) -> str:
        # P4 (§16): rubric_active = a qa_rubric/standard was fed this review; a rubric-triggered
        # follow-up is then (rubric_active and not done) — lets us compute the rubric follow-up rate.
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
                "rubric_active": rubric_active,
                "rubric_followup": bool(rubric_active and not review.done and review.follow_up),
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
            if event_type not in {"tool_pre", "tool_post", "tool_stream", "pm_validation_error"}:
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

    async def _build_pm_active_context(
        self,
        session_id: str,
        *,
        purpose: str,
        window_tokens: int,
    ) -> ActiveContext | None:
        if self.context_manager is None:
            return None
        try:
            return self.context_manager.build_active_context(
                session_id,
                purpose=purpose,
                window_tokens=window_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - active-context restore is a soft dependency.
            await self._persist_then_publish(
                make_event(
                    "notification",
                    "pm-agent",
                    session_id,
                    payload={
                        "kind": "context_restore_failed",
                        "purpose": purpose,
                        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                        "fallback": "legacy_session_context",
                    },
                )
            )
            return None

    async def _pm_context_text(
        self,
        session_id: str,
        *,
        purpose: str,
        window_tokens: int,
    ) -> tuple[str, ActiveContext | None]:
        active_context = await self._build_pm_active_context(
            session_id,
            purpose=purpose,
            window_tokens=window_tokens,
        )
        if active_context is not None and (active_context.rendered_text or "").strip():
            return active_context.rendered_text, active_context
        return self._session_context(session_id), None

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

    async def _interrupt_active_work(self, session_id: str) -> int:
        """Best-effort guided follow-up: stop the in-flight PM task and current CLI turn.

        The new prompt remains in the same Foreman session, so `_session_context()` gives the next
        PM plan the compacted prior context plus the just-appended user message, while the old
        thinking/run is no longer allowed to continue writing concurrent state.
        """
        aborted = self._cancel_session_tasks(session_id)
        runner = self.runner
        if runner is not None and hasattr(runner, "handle_for_session"):
            handle = runner.handle_for_session(session_id)
            if handle is not None and hasattr(runner, "interrupt"):
                try:
                    await runner.interrupt(handle)
                except Exception:  # noqa: BLE001 - interrupt is best-effort; new turn still proceeds
                    pass
        await self._persist_then_publish(
            make_event(
                "notification",
                "dispatch",
                session_id,
                payload={
                    "kind": "interrupted",
                    "msg": "用户选择引导：已中止当前思考/执行，开始处理新的提示词。",
                    "aborted_tasks": aborted,
                },
            )
        )
        return aborted

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
            context_compacted = any(getattr(e, "type", "") == "context_compact" for e in events)
            context = (s.plan or "") if context_compacted else ""
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
                    "context_chars": len(context),
                    "context_tokens": _ctx_approx_tokens(context),
                    "context_compacted": bool(context_compacted and context.strip()),
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
        if phase == "recover":
            return f"PM is switching to {agent} after the previous agent failed..."
        if phase == "launch":
            return f"PM selected {agent}; launching the coding agent..."
        return "PM is planning agent choice, todo list, and launch instruction..."
    if phase == "recover":
        return f"PM 检测到当前 agent 失败，正在改派给 {agent}..."
    if phase == "launch":
        return f"PM 已选择 {agent}，正在启动执行 agent..."
    return "PM 正在规划 agent 选择、任务清单和执行指令..."


def _run_limit_text(language: str) -> str:
    if normalize_lang(language) == "en":
        return "PM review still thinks the task is incomplete, but the run limit was reached."
    return "PM 复查仍认为任务未完成，但已达到运行次数上限。"


def _empty_direct_reply_text(language: str) -> str:
    if normalize_lang(language) == "en":
        return "PM selected a direct reply, but the reply text was empty."
    return "PM 选择了直接回复，但回复内容为空。"


def _terminal_plan_text(plan: PMPlan, language: str) -> str:
    msg = (plan.reply or plan.summary or plan.instruction or "").strip()
    if msg:
        return msg[:2000]
    if normalize_lang(language) == "en":
        return "PM could not produce a runnable agent task."
    return "PM 未能生成可执行的 agent 任务。"


def _fatal_agent_exit_text(rows: list[Any], *, language: str, agent: str) -> str:
    if not rows:
        return ""
    timeline = events_to_text(rows, max_chars=8000)
    if classify_tail(timeline) != ERRORED:
        return ""
    low = timeline.lower()
    is_auth_status = re.search(r"\b(?:401|403)\b", low) is not None
    is_auth_marker = any(marker in low for marker in FATAL_AGENT_MARKERS if marker not in {"401", "403"})
    if not (is_auth_status or is_auth_marker):
        return ""
    if normalize_lang(language) == "en":
        return (
            f"{agent} authentication failed. Fix the local CLI login or API credentials, then retry."
        )
    return f"{agent} 认证失败。请修复本地 CLI 登录状态或 API 凭据后重试。"


def _all_agents_unavailable_text(language: str, failed_agents: set[str], fatal_msg: str) -> str:
    agents = ", ".join(sorted(failed_agents))
    if normalize_lang(language) == "en":
        return (
            "All enabled local coding agents are unavailable or have failed. "
            f"Failed agents: {agents}. Last failure: {fatal_msg}"
        )
    return f"所有已启用的本地 coding agent 都不可用或已失败。失败 agent：{agents}。最后失败：{fatal_msg}"


def _recovery_summary(language: str, failed_agent: str, next_agent: str) -> str:
    if normalize_lang(language) == "en":
        return f"{failed_agent} failed locally; PM is switching to {next_agent}."
    return f"{failed_agent} 本地失败；PM 改派给 {next_agent}。"


def _recovery_fallback_instruction(
    goal: str, plan: PMPlan, fatal_msg: str, language: str
) -> str:
    if normalize_lang(language) == "en":
        return (
            "Continue the original user task with this available agent. "
            "Do not invoke the failed coding agent yourself.\n\n"
            f"Original user task:\n{goal}\n\n"
            f"Previous failed instruction:\n{plan.instruction}\n\n"
            f"Failure evidence:\n{fatal_msg}"
        )
    return (
        "使用当前可用 agent 继续完成原始用户任务。不要自行调用已经失败的 coding agent。\n\n"
        f"原始用户任务：\n{goal}\n\n"
        f"上一个失败指令：\n{plan.instruction}\n\n"
        f"失败证据：\n{fatal_msg}"
    )


def _empty_followup_text(language: str) -> str:
    if normalize_lang(language) == "en":
        return "PM review asked to continue but did not provide a follow-up prompt."
    return "PM 复查要求继续，但没有给出后续指令。"


def _workflow_step_instruction(step: dict, *, language: str = "") -> str:
    """One coding instruction for a workflow step (P5 §10): step name + instruction + the L0 INDEX of
    its skills/standards (names only — bodies are injected as workspace files for progressive
    disclosure, NEVER inlined here). Keeps the per-step prompt small (no ×bodies blowup)."""
    en = normalize_lang(language) == "en"
    name = str(step.get("name") or "")
    instr = str(step.get("instruction") or "")
    parts: list[str] = []
    head = f"# Workflow step: {name}" if name else "# Workflow step"
    parts.append(f"{head}\n{instr}" if instr else head)
    skills = [str(s.get("name")) for s in (step.get("skills") or []) if s.get("name")]
    standards = [str(s.get("name")) for s in (step.get("standards") or []) if s.get("name")]
    if skills or standards:
        refs = []
        if skills:
            refs.append(("skills: " if en else "技能：") + ", ".join(skills))
        if standards:
            refs.append(("code standards: " if en else "代码规范：") + ", ".join(standards))
        note = (
            "Applicable work modes for this step (full text is injected into the workspace files — "
            "read them as needed; do NOT expect the bodies inline here):\n"
            if en else
            "本步可用工作方式（正文已注入工作区文件，需要时查阅；此处不内联正文）：\n"
        )
        parts.append(note + "\n".join(refs))
    guard = (
        "Do not push, merge, or deploy unless the user explicitly requested it."
        if en else "除非用户明确要求，不要推送、合并或部署。"
    )
    parts.append(guard)
    return "\n\n".join(p for p in parts if p)


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


_REVIEW_TIMELINE_FRAME_TYPES = {
    "command_result",
    "tool_result",
    "test_result",
    "file_change",
    "agent_output",
    "agent_stop",
    "previous_validation_error",
    "context_compaction",
}


def _review_timeline_from_active_context(
    active_context: ActiveContext | None,
    rows: list[Any],
    reviewed_event_id: str,
) -> str:
    if active_context is None:
        return events_to_text(_events_after(rows, reviewed_event_id))
    order = {_event_id(row): idx for idx, row in enumerate(rows) if _event_id(row)}
    reviewed_idx = order.get((reviewed_event_id or "").strip(), -1)
    lines: list[str] = []
    for frame in active_context.frames_after_checkpoint or []:
        if not isinstance(frame, dict):
            continue
        frame_type = str(frame.get("type") or "").strip()
        if frame_type not in _REVIEW_TIMELINE_FRAME_TYPES:
            continue
        if int(frame.get("lane") or 0) == 7:
            continue
        event_id = _frame_event_id(frame)
        if reviewed_event_id and not event_id:
            continue
        if event_id and event_id in order and order[event_id] <= reviewed_idx:
            continue
        if event_id and event_id not in order and reviewed_event_id:
            continue
        line = _render_review_timeline_frame(frame, event_id)
        if line:
            lines.append(line)
    return "\n".join(lines) if lines else "(no new agent output captured)"


def _frame_event_id(frame: dict[str, Any]) -> str:
    event_id = str(frame.get("event_id") or "").strip()
    if event_id:
        return event_id
    for ref in frame.get("source_refs") or []:
        text = str(ref or "")
        if text.startswith("event:"):
            return text.split(":", 1)[1].strip()
    return ""


def _render_review_timeline_frame(frame: dict[str, Any], event_id: str) -> str:
    frame_type = str(frame.get("type") or "").strip()
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    bits = [frame_type]
    if event_id:
        bits.append(f"event:{event_id}")
    agent_id = str(frame.get("agent_id") or payload.get("agent_id") or "").strip()
    if agent_id:
        bits.append(f"agent:{agent_id}")
    call_id = str(payload.get("call_id") or payload.get("tool_call_id") or "").strip()
    if call_id:
        bits.append(f"call:{call_id}")
    summary = _review_payload_summary(frame_type, payload)
    return f"- {' '.join(bits)}: {summary}" if summary else f"- {' '.join(bits)}"


def _review_payload_summary(frame_type: str, payload: dict[str, Any]) -> str:
    keys_by_type = {
        "command_result": ["command", "exit_code", "cwd", "important_lines", "stdout_summary", "stderr_summary"],
        "tool_result": ["tool", "name", "status", "result", "summary", "important_lines"],
        "test_result": ["command", "status", "passed", "failed", "exit_code", "failures", "important_lines"],
        "file_change": ["changed_files", "files", "paths", "diff_stat", "truncated"],
        "agent_output": ["summary", "text", "message", "important_lines"],
        "agent_stop": ["status", "summary", "result", "payload", "next_actions", "next_steps"],
        "previous_validation_error": ["error", "round", "arguments"],
        "context_compaction": ["summary", "checkpoint_id", "event_id"],
    }
    picked = {
        key: payload.get(key)
        for key in keys_by_type.get(frame_type, [])
        if payload.get(key) not in (None, "", [], {})
    }
    if not picked:
        picked = {key: value for key, value in payload.items() if value not in (None, "", [], {})}
    try:
        text = json.dumps(picked, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(picked)
    text = " ".join(text.split())
    return text[:900] + "...[truncated]" if len(text) > 900 else text


def _advance_reviewed_event_id_from_active_context(
    rows: list[Any],
    reviewed_event_id: str,
    active_context: ActiveContext | None,
) -> str:
    if active_context is None:
        return reviewed_event_id
    cursor = active_context.source_cursor or {}
    end = cursor.get("end") if isinstance(cursor.get("end"), dict) else cursor
    checkpoint_event_id = str(
        end.get("event_id") or end.get("id") or cursor.get("end_event_id") or ""
    ).strip()
    if not checkpoint_event_id:
        return reviewed_event_id
    order = {_event_id(row): idx for idx, row in enumerate(rows) if _event_id(row)}
    checkpoint_idx = order.get(checkpoint_event_id)
    if checkpoint_idx is None:
        return reviewed_event_id
    reviewed_idx = order.get((reviewed_event_id or "").strip())
    if reviewed_idx is None:
        return checkpoint_event_id
    return checkpoint_event_id if checkpoint_idx > reviewed_idx else reviewed_event_id


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
