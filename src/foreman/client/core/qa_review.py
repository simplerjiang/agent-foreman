"""QA-standard-driven review for a workflow step (T5.4, DESIGN §11.2 step 3 / §5.3 / §88-89).

The hybrid workflow engine (T5.2) parks a step in ``qa`` when it carries a QA rubric and the engine
has no ``qa_passed`` yet. This module closes that loop: it pulls the step's resolved QA rubric (the
*尺子* "怎么算合格", §88-89) plus a diff of what the step changed, hands them to YOUR LLM Reviewer
(T2.7), maps the verdict to a pass/fail, and feeds it back to the engine so **"过了才走下一步"**
(§11.2 step 3: pass → advance, fail → redo).

**Conservative by default (§6.7 从严默认):** only an ``approve`` verdict passes; anything else —
``request_changes``, ``escalate``, or an unparseable reply that the Reviewer already turns into an
``escalate`` — does NOT advance the workflow. A non-passing QA marks the step ``failed`` (the engine
reverts the injected scaffolding, T5.3) and the verdict's ``needs_human`` is surfaced so the decision
loop can offer a card (⛔ 撤掉重来, §5.3). A step parked in ``qa`` with no resolvable rubric body is
itself fail-closed (``no_qa_rubric``) — we never silently advance a step we cannot judge.

Client-side core: it reaches the local Store / CheckpointManager and the injected WorkflowEngine +
Reviewer; no server import, no shared leak (§14). The live LLM needs the user's own key (mocked in
tests, same seam as the Reviewer). The diff comes from the CheckpointManager (which captures new,
not-yet-tracked files too — see ``CheckpointManager.diff``).
"""

from __future__ import annotations

from foreman.shared.events import make_event

from .reviewer import APPROVE, ReviewResult
from .workflow_engine import QA

# Which Reviewer verdicts let the workflow advance. Only an explicit approve passes (§6.7): a
# request_changes / escalate / parse-failure escalate all hold the run back.
_VERDICT_PASSES: frozenset[str] = frozenset({APPROVE})


def _review_to_dict(r: ReviewResult) -> dict:
    return {
        "verdict": r.verdict,
        "summary": r.summary,
        "risks": list(r.risks),
        "suggestions": list(r.suggestions),
        "needs_human": r.needs_human,
    }


class WorkflowQAReviewer:
    """Drives the QA-standard review for a workflow step parked in ``qa`` (T5.4, §11.2 step 3).

    ``engine`` is the WorkflowEngine (T5.2); ``reviewer`` is the LLM Reviewer (T2.7). ``checkpoints``
    (optional) is a CheckpointManager used to compute the step's diff when one is not passed in.
    ``bus`` (optional) receives the ``review`` event (persist-first, mirrors the rest of the loop).
    """

    def __init__(
        self,
        engine,
        reviewer,
        *,
        checkpoints=None,
        store=None,
        bus=None,
    ) -> None:
        self.engine = engine
        self.reviewer = reviewer
        self.checkpoints = checkpoints
        # The engine already owns the local Store; fall back to it so callers need not pass both.
        self.store = store or getattr(engine, "store", None)
        self.bus = bus

    async def review_step(
        self,
        run_id: str,
        *,
        goal: str = "",
        diff: str | None = None,
        context: str = "",
        from_ref: str | None = None,
        to_ref: str | None = None,
    ) -> dict:
        """Review a workflow step parked in ``qa`` against its QA rubric, then resolve the run.

        Resolves the step's QA rubric + a diff of what changed, runs the Reviewer, maps the verdict to
        pass/fail (only ``approve`` passes), feeds it back to ``engine.submit_step`` and emits a
        ``review`` event. Returns ``{ok, verdict, qa_passed, needs_human, review, engine}`` on success,
        or ``{ok: False, error}`` with error ∈ {no_run, not_in_qa, bad_workflow, no_qa_rubric}.
        """
        run = self._get_run(run_id)
        if run is None:
            return {"ok": False, "error": "no_run"}
        if run.step_status != QA:
            # Only a step the engine has parked awaiting QA can be judged here (fail-closed: never
            # review a running / gated / finished step).
            return {"ok": False, "error": "not_in_qa"}
        view = self.engine.step_view(run_id)
        if view is None:
            return {"ok": False, "error": "bad_workflow"}
        qa = view.get("qa")
        if not qa or not qa.get("body"):
            # Parked in QA but the rubric body did not resolve (missing/archived definition). We
            # cannot judge against nothing, so refuse rather than silently advance.
            return {"ok": False, "error": "no_qa_rubric", "missing": view.get("missing", [])}

        resolved_goal = goal or self._session_goal(run.session_id) or view.get("instruction", "")
        resolved_diff = self._resolve_diff(diff, from_ref, to_ref)
        review = await self.reviewer.review(
            resolved_goal, resolved_diff, qa_standard=qa["body"], context=context
        )
        qa_passed = review.verdict in _VERDICT_PASSES

        await self._emit_review(run, view, qa, review, qa_passed)
        engine_result = await self.engine.submit_step(run_id, qa_passed=qa_passed)
        return {
            "ok": True,
            "verdict": review.verdict,
            "qa_passed": qa_passed,
            "needs_human": review.needs_human,
            "review": _review_to_dict(review),
            "engine": engine_result,
        }

    # ── helpers ─────────────────────────────────────────────────────────────────────────────────
    def _get_run(self, run_id: str):
        store = getattr(self.engine, "store", None) or self.store
        if store is None or not hasattr(store, "get_workflow_run"):
            return None
        return store.get_workflow_run(run_id)

    def _session_goal(self, session_id: str) -> str:
        if self.store is None or not hasattr(self.store, "get_session"):
            return ""
        session = self.store.get_session(session_id)
        return getattr(session, "goal", "") or "" if session is not None else ""

    def _resolve_diff(self, diff: str | None, from_ref: str | None, to_ref: str | None) -> str:
        """The changes this step made: an explicit diff wins; else compute from a checkpoint ref."""
        if diff is not None:
            return diff
        if self.checkpoints is not None and from_ref:
            try:
                return self.checkpoints.diff(from_ref, to_ref)
            except Exception:  # noqa: BLE001 — a bad ref must not crash the review; degrade to empty.
                return ""
        return ""

    async def _emit_review(self, run, view: dict, qa: dict, review: ReviewResult, qa_passed: bool) -> None:
        """Persist-then-publish a ``review`` event for this QA outcome (metadata only, no diff/body)."""
        payload = {
            "run_id": run.id,
            "step_index": run.step_index,
            "step": view.get("name", ""),
            "qa_rubric": qa.get("name", ""),
            "verdict": review.verdict,
            "qa_passed": qa_passed,
            "needs_human": review.needs_human,
            "summary": review.summary,
        }
        event = make_event("review", "qa-reviewer", run.session_id, payload=payload)
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)


__all__ = ["WorkflowQAReviewer"]
