"""Decision Loop — the串联 that ties Operator → Auditor → Gate → Card → you → checkpoint → execute.

This is the P4 acceptance spine (docs/TASKS.md P4 验收, DESIGN §6.2): one Operator-proposed step
walks the full pipeline, and once you tap **approve** the action is checkpointed and actually run —
the "执行层" the earlier P2–P4 tasks deferred to here.

Flow per the §6.2 pipeline diagram:

    Operator.observe(agent output) → proposals
      └─ for each proposal:
           ① persist an ``actions`` row (proposed)
           ② Auditor.audit (independent, adversarial) → persist an ``audits`` row
                 reject  → drop (garbage / off-track), no card
                 revise  → send back to the operator (best-effort), no card
                 escalate→ always a card (a human must decide)
                 pass    → Gate.disposition (classify × autonomy dial, §6.4/§6.6):
                              auto   → ③ checkpoint → ④ execute → ⑤ record  (no card)
                              card   → build a decision card, wait for your tap
                              report → record only (you drive manually)

When a card is later decided (``on_card_decision``, wired into ``CardService`` as its executor):

    approve → ③ checkpoint → ④ execute → ⑤ record   (the "你点 → 检查点 → 执行" half)
    undo    → restore the worktree to the action's checkpoint (一键回退, §6.5②)
    revise  → send notes back to the agent (two-way control, Runner.send)
    manual  → no auto-execution (you type your own next instruction)

🔑 The checkpoint is taken **right before** execution (after approval), so any step is reversible
(§6.2 ⑤). Irreversible actions never reach ``auto`` — the Gate's red line holds at every level (§6.6).

This is client-side core: it reaches the local Store + CheckpointManager + Runner + Toolbelt, and is
INJECTED into ``server.app`` only indirectly (via the CardService executor) so app.py stays
shared-only (DESIGN §14). Execution backends (Runner for ``agent_instruction``, Toolbelt for
``shell``/``mcp_tool``) are injected/duck-typed, so the whole loop is unit-testable without a live
CLI or desktop; the genuinely-live hookups (a real claude/codex resume, a real desktop click) are
the only deferred bits, consistent with P2–P4.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from foreman.shared.autonomy import AUTO, CARD, REPORT, level_label, normalize_level
from foreman.shared.events import make_event, utc_now_iso

from ..store.models import Action, Audit, Checkpoint
from .auditor import ESCALATE, PASS, REJECT, REVISE


def _default_checkpoint_factory(workspace: str):
    from .checkpoint import CheckpointManager

    return CheckpointManager(Path(workspace))


class DecisionLoop:
    """Drives one Operator step through audit → gate → card → checkpoint → execution (§6.2)."""

    def __init__(
        self,
        *,
        store: Any,
        gate: Any,
        cards: Any,
        operator: Any = None,
        auditor: Any = None,
        bus: Any = None,
        runner: Any = None,
        toolbelt: Any = None,
        checkpoint_factory=None,
        language: str = "zh",
        autonomy_level: int | None = None,
        clock=None,
    ) -> None:
        self.store = store
        self.gate = gate
        self.cards = cards
        self.operator = operator
        self.auditor = auditor
        self.bus = bus
        self.runner = runner
        self.toolbelt = toolbelt
        self._ckpt_factory = checkpoint_factory or _default_checkpoint_factory
        self.language = language
        # Config baseline (cfg.autonomy.level): the effective level when no DB override exists, so a
        # config of level 2/3 actually drives the loop instead of being normalized to 1 (issue #1 P1).
        self._baseline = normalize_level(autonomy_level) if autonomy_level is not None else None
        self._clock = clock or utc_now_iso

    # ── the level (autonomy dial) ───────────────────────────────────────────────────────────────
    def _level(self, override=None) -> int:
        """Effective autonomy level: explicit override, else the stored config_kv override, else the
        config baseline passed at construction, else the module default (1).

        Mirrors the server settings endpoint (GET /api/settings/autonomy), which also falls back to
        cfg.autonomy.level — so the dial the UI shows and the dial the loop enforces agree (P1)."""
        if override is not None:
            return normalize_level(override)
        current = None
        if hasattr(self.store, "get_setting"):
            current = self.store.get_setting("autonomy.level")
        if current is not None:
            return normalize_level(current)
        if self._baseline is not None:
            return self._baseline
        return normalize_level(None)

    # ── observe one agent step → propose / audit / gate / card / (auto-)execute ─────────────────
    async def observe(
        self,
        session_id: str,
        goal: str,
        agent_output: str,
        *,
        context: str = "",
        recent_actions: str = "",
        level=None,
    ) -> dict:
        """Run the Operator on the latest agent output and process each proposal through the chain.

        Returns ``{summary, state, results: [per-proposal outcome…]}``. Nothing executes here unless
        the autonomy dial maps a passed, reversible action to ``auto`` — everything else surfaces a
        card or is reported (DESIGN §6.2/§6.4)."""
        if self.operator is None:
            return {"summary": "", "state": "blocked", "results": []}
        op = await self.operator.observe(
            goal, agent_output, context=context, recent_actions=recent_actions
        )
        lvl = self._level(level)
        results = []
        for proposal in op.proposals:
            results.append(await self._process(session_id, goal, op, proposal, lvl))
        return {"summary": op.summary, "state": op.state, "results": results}

    async def _process(self, session_id: str, goal: str, op, proposal, level: int) -> dict:
        """Persist + audit + gate one proposal; auto-execute, card, or report it (§6.2)."""
        action = Action(
            id=uuid.uuid4().hex,
            session_id=session_id,
            kind=proposal.kind,
            command=proposal.command,
            rationale=proposal.rationale,
            expected_effect=proposal.expected_effect,
            reversible=proposal.reversible,
            status="proposed",
            created_at=self._clock(),
        )
        self.store.add_action(action)

        verdict = PASS
        audit = None
        if self.auditor is not None:
            audit = await self.auditor.audit(
                proposal.command,
                rationale=proposal.rationale,
                expected_effect=proposal.expected_effect,
                goal=goal,
                autonomy=level_label(level, self.language),
                recent_actions="",
            )
            self.store.add_audit(
                Audit(
                    id=uuid.uuid4().hex,
                    action_id=action.id,
                    verdict=audit.verdict,
                    risk_severity=audit.risk_severity,
                    goal_quality=audit.goal_quality,
                    reasons_json=_json(audit.reasons),
                    suggestions_json=_json(audit.suggestions),
                    model=audit.model,
                    ts=self._clock(),
                )
            )
            verdict = audit.verdict
        self.store.update_action(action.id, status="audited")

        # The Auditor's verdict gates whether we even consider the action (§6.2 ①).
        if verdict == REJECT:
            self.store.update_action(action.id, status="rejected")
            return {"action_id": action.id, "verdict": verdict, "outcome": "rejected"}
        if verdict == REVISE:
            # Send the notes back to the operator/agent (best-effort, two-way control). The proposal
            # does not run; the agent re-proposes (you never see this noise, §6.2 ①).
            await self._revise(action, audit)
            self.store.update_action(action.id, status="revising")
            return {"action_id": action.id, "verdict": verdict, "outcome": "revise"}

        # passed audit → Gate disposition (classify × dial). Escalate always cards (§6.7).
        # The Operator's own ``reversible=False`` hint can only TIGHTEN, never loosen (§6.7 从严默认):
        # an action it flags irreversible always cards even if the deterministic Gate didn't catch
        # it — a defence-in-depth backstop layered on top of the Gate's text classification.
        if verdict == ESCALATE or not getattr(proposal, "reversible", True):
            disposition = CARD
        else:
            disposition = self.gate.disposition(proposal.command, level)

        if disposition == AUTO:
            exec_res = await self.execute_action(action.id)
            return {"action_id": action.id, "verdict": verdict, "outcome": "auto", "exec": exec_res}
        if disposition == REPORT:
            return {"action_id": action.id, "verdict": verdict, "outcome": "report"}

        # card: surface a folded decision card and wait for the human's tap (§6.3).
        note = "; ".join(audit.reasons) if (audit and audit.reasons) else (
            audit.verdict if audit else ""
        )
        card = self.cards.build_card(
            action_id=action.id,
            session_id=session_id,
            summary=op.summary or proposal.command,
            audit_note=note,
            diff_stat="",  # pre-execution: no diff yet (it appears after the checkpoint→run)
        )
        self.store.update_action(action.id, status="carded")
        return {"action_id": action.id, "verdict": verdict, "outcome": "card", "card": card}

    # ── execution: checkpoint → run → record (§6.2 ③④⑤) ─────────────────────────────────────────
    async def execute_action(self, action_id: str, *, label: str = "") -> dict:
        """Checkpoint the workspace, run the action, and record it. The "检查点 → 执行" half (§6.2).

        Returns ``{ok, action_id, checkpoint_id, result, executed}``. A snapshot is taken FIRST so the
        step is reversible (one-click undo, §6.5); then the action runs through the matching backend;
        then the row is stamped executed."""
        action = self.store.get_action(action_id)
        if action is None:
            return {"ok": False, "error": "not_found", "executed": False}

        ckpt_id = await self._checkpoint(action, label=label or f"before {action.kind or 'step'}")
        if ckpt_id:
            self.store.update_action(action_id, checkpoint_id=ckpt_id)

        result = await self._run(action)
        ran = bool(result.get("ok"))
        self.store.update_action(
            action_id,
            status="executed" if ran else "audited",
            executed_at=self._clock() if ran else None,
        )
        await self._emit(
            "action_executed",
            action.session_id,
            {
                "action_id": action_id,
                "kind": action.kind,
                "checkpoint_id": ckpt_id,
                "ok": ran,
                "backend": result.get("backend", ""),
                "execution_deferred": result.get("execution_deferred", False),
            },
        )
        return {
            "ok": ran,
            "action_id": action_id,
            "checkpoint_id": ckpt_id,
            "result": result,
            "executed": ran,
        }

    async def _checkpoint(self, action, *, label: str) -> str | None:
        """Snapshot the session's workspace before a step; record a ``checkpoints`` row (§6.5).

        Returns the checkpoint row id (anchors the step-detail diff, §6.3), or None if the session has
        no workspace / git isn't available."""
        session = self.store.get_session(action.session_id) if hasattr(
            self.store, "get_session"
        ) else None
        workspace = getattr(session, "workspace", "") if session else ""
        if not workspace:
            return None
        try:
            mgr = self._ckpt_factory(workspace)
            # Step index comes from the git shadow refs (the single source of truth), NOT the DB —
            # so it stays consistent with the redo snapshot `undo_to` chains off the same refs, and
            # a later checkpoint can never overwrite an undo's redo ref (§6.5).
            step_index = mgr.next_step(action.session_id)
            sha = await mgr.snapshot(
                action.session_id, step_index, label=label, task_id=action.task_id
            )
        except Exception:  # not-a-repo / git failure shouldn't crash the loop — execute uncheckpointed.
            return None
        ckpt = Checkpoint(
            id=uuid.uuid4().hex,
            session_id=action.session_id,
            task_id=action.task_id,
            step_index=step_index,
            vcs_ref=sha,
            label=label,
            created_at=self._clock(),
        )
        self.store.add_checkpoint(ckpt)
        return ckpt.id

    async def _run(self, action) -> dict:
        """Dispatch an action to its execution backend (Runner / Toolbelt). Injected → testable.

        ``agent_instruction`` → Runner.send (resume the CLI with the instruction); ``shell`` →
        Toolbelt.run_shell (the card/auto path already cleared the Gate, so ``approved=True``). Other
        kinds (``file_edit`` — applied by the agent itself; ``mcp_tool`` GUI capabilities, which must
        go through the Toolbelt's per-capability click/type methods with their OWN risk classification,
        not be re-parsed as a shell string) are deferred to their proper backend, not mis-run here. A
        missing backend defers execution (noted), mirroring the P2–P4 live-deferral pattern."""
        kind = action.kind or "shell"
        if kind == "agent_instruction":
            if self.runner is None:
                return {"backend": "runner", "ok": False, "execution_deferred": True}
            handle = self.runner.handle_for_session(action.session_id)
            if handle is None:
                return {"backend": "runner", "ok": False, "error": "no_live_agent"}
            try:
                await self.runner.send(handle, action.command)
            except Exception as exc:
                return {"backend": "runner", "ok": False, "error": _emsg(exc)}
            return {"backend": "runner", "ok": True}
        if kind != "shell":
            # file_edit / mcp_tool / unknown — not a shell command; route through their own backend
            # (the GUI capabilities keep their distinct risk classification, §4.7) before they run.
            return {"backend": "none", "ok": False, "execution_deferred": True, "kind": kind}
        if self.toolbelt is None:
            return {"backend": "toolbelt", "ok": False, "execution_deferred": True}
        try:
            res = self.toolbelt.run_shell(action.command, approved=True)
        except Exception as exc:
            return {"backend": "toolbelt", "ok": False, "error": _emsg(exc)}
        return {
            "backend": "toolbelt",
            "ok": bool(getattr(res, "ok", False)),
            "detail": getattr(res, "detail", ""),
            "error": getattr(res, "error", ""),
        }

    # ── card decision executor (wired into CardService) ─────────────────────────────────────────
    async def on_card_decision(self, card, option: str) -> dict:
        """Execute the human's one-tap card decision (the "你点 → 执行" half, §6.3).

        Injected into ``CardService`` as its executor, so ``POST /api/cards/{id}/choose`` actually
        runs the chosen path. ``card`` is the persisted DecisionCard row."""
        action_id = getattr(card, "action_id", "")
        if option == "approve":
            return await self.execute_action(action_id)
        if option == "undo":
            return await self._undo(action_id)
        if option == "revise":
            action = self.store.get_action(action_id)
            res = await self._revise(action, None) if action else {"ok": False}
            return {"ok": bool(res.get("ok")), "executed": False, "outcome": "revise"}
        # manual: the human will type their own instruction — nothing to auto-run.
        return {"ok": True, "executed": False, "outcome": "manual"}

    async def _undo(self, action_id: str) -> dict:
        """Restore the worktree to the action's checkpoint — one-click undo (§6.5②)."""
        action = self.store.get_action(action_id) if action_id else None
        if action is None:
            return {"ok": False, "executed": False, "error": "no_checkpoint"}
        ckpt_id = getattr(action, "checkpoint_id", None)
        if not ckpt_id or not hasattr(self.store, "get_checkpoint"):
            return {"ok": False, "executed": False, "error": "no_checkpoint"}
        ckpt = self.store.get_checkpoint(ckpt_id)
        session = self.store.get_session(action.session_id) if hasattr(
            self.store, "get_session"
        ) else None
        workspace = getattr(session, "workspace", "") if session else ""
        if ckpt is None or not ckpt.vcs_ref or not workspace:
            return {"ok": False, "executed": False, "error": "no_checkpoint"}
        try:
            mgr = self._ckpt_factory(workspace)
            redo = await mgr.undo_to(ckpt.vcs_ref, session_id=action.session_id)
        except Exception as exc:
            return {"ok": False, "executed": False, "error": _emsg(exc)}
        self.store.update_action(action_id, status="undone")
        await self._emit(
            "action_undone", action.session_id, {"action_id": action_id, "redo_ref": redo}
        )
        return {"ok": True, "executed": True, "outcome": "undo", "redo_ref": redo}

    async def _revise(self, action, audit) -> dict:
        """Send the Auditor's revise notes back to the live agent (two-way control, best-effort)."""
        if action is None or self.runner is None:
            return {"ok": False, "execution_deferred": True}
        handle = self.runner.handle_for_session(action.session_id)
        if handle is None:
            return {"ok": False, "error": "no_live_agent"}
        notes = "; ".join(audit.suggestions) if (audit and audit.suggestions) else (
            "Please revise the previous step."
        )
        try:
            await self.runner.send(handle, notes)
        except Exception as exc:
            return {"ok": False, "error": _emsg(exc)}
        return {"ok": True}

    async def _emit(self, etype: str, session_id: str, payload: dict) -> None:
        """Persist-then-publish an event (mirrors Runner / Gate / CardService)."""
        event = make_event(etype, "decision-loop", session_id, payload=payload)
        if hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)


def _json(value) -> str:
    import json

    return json.dumps(value or [])


def _emsg(exc: Exception) -> str:
    """Type + short message — never the full repr (an exc could carry a path / secret)."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


__all__ = ["DecisionLoop"]
