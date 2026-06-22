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
from pathlib import Path
from typing import Any

from foreman.shared.config import Config
from foreman.shared.events import make_event, utc_now_iso

from ..dispatch import build_session_task

# Bound the goal so a multi-megabyte string can't inflate every later briefing's token cost (and
# keeps the argv passed to the agent CLI sane). Truncated, not rejected, to stay friendly.
MAX_GOAL_CHARS = 8000


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
        clock=None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        # launcher(session_id, goal, workspace, agent, model) -> awaitable; None = launch deferred.
        self.launcher = launcher
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
    ) -> dict:
        """Validate + persist a new Root Session/Task; emit ``dispatch``; optionally launch.

        Returns ``{"ok": True, session_id, task_id, goal, workspace, agent, model,
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
        resolved_agent, err = self._resolve_agent(agent)
        if err:
            return {"ok": False, "error": err}
        ws, err = self._resolve_workspace(workspace)
        if err:
            return {"ok": False, "error": err}
        resolved_model = self._resolve_model(resolved_agent, model)

        session, task = build_session_task(self.store, goal, ws, resolved_agent)
        deferred = self.launcher is None
        await self._emit_dispatch(
            session.id, task.id, goal, ws, resolved_agent, resolved_model, deferred
        )
        if self.launcher is not None:
            # Fire-and-forget: a phone dispatch returns immediately; the agent runs in the
            # background (Runner pumps its events to store+bus, T1.7). Failures emit an `error`.
            # Keep a strong ref (discarded on completion) so the task isn't GC'd mid-flight.
            launch_task = asyncio.create_task(
                self._safe_launch(session.id, goal, ws, resolved_agent, resolved_model)
            )
            self._tasks.add(launch_task)
            launch_task.add_done_callback(self._tasks.discard)
        return {
            "ok": True,
            "session_id": session.id,
            "task_id": task.id,
            "goal": goal,
            "workspace": ws,
            "agent": resolved_agent,
            "model": resolved_model,
            "execution_deferred": deferred,
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

    def _resolve_model(self, agent: str, model: str | None) -> str:
        override = (model or "").strip()
        if override:
            return override
        cfg = self.cfg.agents.get(agent)
        return (cfg.model if cfg else "").strip()

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
        deferred: bool,
    ) -> None:
        event = make_event(
            "dispatch",
            "phone",
            session_id,
            task_id=task_id,
            payload={
                "goal": goal,
                "agent": agent,
                "model": model,
                "workspace": workspace,
                # launching the agent is the two-way control layer (P4) when no launcher is wired.
                "execution_deferred": deferred,
            },
        )
        await self._persist_then_publish(event)

    async def _safe_launch(
        self, session_id: str, goal: str, workspace: str, agent: str, model: str
    ) -> None:
        """Run the injected launcher; an agent that can't start records an `error` event, not a crash."""
        try:
            await self.launcher(session_id, goal, workspace, agent, model)
        except Exception as exc:  # noqa: BLE001 — a launch failure must not take down the server loop
            event = make_event(
                "error",
                "dispatch",
                session_id,
                payload={"msg": f"{type(exc).__name__}: {exc}"[:200]},
            )
            await self._persist_then_publish(event)

    async def _persist_then_publish(self, event) -> None:
        """Persist-first (so a late UI can backfill) then publish — mirrors Runner/Gate."""
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)

    # ── multi-session overview (dashboard) ────────────────────────────────────────────────────
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
