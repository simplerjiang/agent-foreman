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
        clock=None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        # launcher(session_id, goal, workspace, agent, model, effort) -> awaitable; None = deferred.
        self.launcher = launcher
        self.runner = runner
        self.pm_agent = pm_agent
        self._clock = clock or utc_now_iso
        self._tasks: set[asyncio.Task] = set()  # strong refs so fire-and-forget launches aren't GC'd

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
        resolved_agent, err = self._resolve_agent(
            agent or (existing_session.agent_type if existing_session else "")
        )
        if err:
            return {"ok": False, "error": err}
        ws, err = self._resolve_workspace(
            workspace or (existing_session.workspace if existing_session else "")
        )
        if err:
            return {"ok": False, "error": err}
        pm_enabled = self.pm_agent is not None and self.runner is not None
        enabled_agents = sorted(k for k, a in self.cfg.agents.items() if a.enabled)
        direct_agents = _explicit_agent_targets(goal, enabled_agents) if pm_enabled else []
        resolved_model = (
            ""
            if direct_agents
            else (model or "").strip() if pm_enabled else self._resolve_model(resolved_agent, model)
        )
        resolved_effort = "" if pm_enabled else self._resolve_effort(resolved_agent, effort)

        session_agent = "+".join(direct_agents) if direct_agents else resolved_agent
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
            self._tasks.add(launch_task)
            launch_task.add_done_callback(self._tasks.discard)
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
            self._tasks.add(launch_task)
            launch_task.add_done_callback(self._tasks.discard)
        elif self.launcher is not None:
            # Fire-and-forget: a phone dispatch returns immediately; the agent runs in the
            # background (Runner pumps its events to store+bus, T1.7). Failures emit an `error`.
            # Keep a strong ref (discarded on completion) so the task isn't GC'd mid-flight.
            launch_task = asyncio.create_task(
                self._safe_launch(
                    session.id, goal, ws, resolved_agent, resolved_model, resolved_effort
                )
            )
            self._tasks.add(launch_task)
            launch_task.add_done_callback(self._tasks.discard)
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
            event = make_event(
                "error",
                "pm-agent",
                session_id,
                task_id=task_id,
                payload={"msg": f"{type(exc).__name__}: {str(exc)[:200]}"},
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
        language = getattr(self.pm_agent, "language", "")
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
        enabled_agents = [
            {"name": name, "model": cfg.model, "effort": getattr(cfg, "effort", "")}
            for name, cfg in sorted(self.cfg.agents.items())
            if cfg.enabled
        ] or [{"name": agent, "model": "", "effort": effort}]
        context = self._session_context(session_id)
        plan_kwargs = {
            "workspace": workspace,
            "available_agents": enabled_agents,
            "requested_agent": "",
            "pm_model": pm_model,
            "requested_effort": "high",
            "fallback_instruction": _fallback_instruction(goal, context),
            "context": context,
        }
        if _accepts_keyword(self.pm_agent.plan, "on_stream"):
            plan_kwargs["on_stream"] = self._pm_stream_sink(session_id, task_id, "plan")
        plan = await self.pm_agent.plan(goal, **plan_kwargs)
        plan = self._sanitize_pm_plan(plan, pm_model)
        await self._emit_pm_plan(session_id, task_id, plan)
        handle = await self.runner.launch(
            plan.agent, plan.instruction, Path(workspace), session_id,
            model=plan.model, effort=plan.effort,
        )
        run_count = 1
        await self.runner.wait(handle)
        while True:
            timeline = events_to_text(self.store.get_events(session_id))
            review_kwargs = {
                "run_count": run_count,
                "context": context,
                "pm_model": pm_model,
            }
            if _accepts_keyword(self.pm_agent.review, "on_stream"):
                review_kwargs["on_stream"] = self._pm_stream_sink(
                    session_id, task_id, f"review-{run_count}"
                )
            review = await self.pm_agent.review(goal, plan, timeline, **review_kwargs)
            await self._emit_pm_review(session_id, task_id, review, run_count)
            if review.done:
                return
            if run_count >= self.pm_agent.max_runs:
                await self._emit_pm_error(
                    session_id,
                    task_id,
                    "PM review still thinks the task is incomplete, but the run limit was reached.",
                )
                return
            if not review.follow_up:
                await self._emit_pm_error(
                    session_id,
                    task_id,
                    "PM review asked to continue but did not provide a follow-up prompt.",
                )
                return
            await self.runner.send(handle, review.follow_up)
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
        )

    async def _emit_pm_plan(self, session_id: str, task_id: str, plan: PMPlan) -> None:
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
            },
        )
        await self._persist_then_publish(event)

    async def _emit_pm_review(self, session_id: str, task_id: str, review, run_count: int) -> None:
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
            },
        )
        await self._persist_then_publish(event)

    async def _emit_pm_error(self, session_id: str, task_id: str, msg: str) -> None:
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

    async def _safe_launch(
        self, session_id: str, goal: str, workspace: str, agent: str, model: str, effort: str
    ) -> None:
        """Run the injected launcher; an agent that can't start records an `error` event, not a crash."""
        try:
            await self.launcher(session_id, goal, workspace, agent, model, effort)
        except Exception as exc:  # noqa: BLE001 — a launch failure must not take down the server loop
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


def _fallback_instruction(goal: str, context: str = "") -> str:
    parts = [
        "You are working under Foreman's PM supervision. Complete the user task, verify the result, "
        "and report honestly what changed and what could not be verified.",
    ]
    if context:
        parts.append(f"Existing session context:\n{context}")
    parts.append(f"User task:\n{goal}")
    return "\n\n".join(parts)


def _fallback_compact(timeline: str, existing: str = "") -> str:
    text = "\n".join(part for part in [existing, timeline] if part).strip()
    if len(text) <= MAX_CONTEXT_CHARS:
        return text
    return "...[context compacted to latest events]...\n" + text[-MAX_CONTEXT_CHARS:]


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


def _direct_agent_instruction(
    goal: str, agent: str, *, multi: bool, language: str = ""
) -> str:
    if not multi:
        return goal
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


def _event_id(row: Any) -> str:
    return str(getattr(row, "id", "") or "").strip()


def _as_optional_str(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
