"""Hybrid workflow engine — fixed skeleton + per-step skill/standard injection + approval-gate cards.

DESIGN §11.2 ("混合式工作流怎么跑"), one line: **the skeleton is fixed; how each step is done is
live.** A workflow is a 秘方 ``definitions`` row (``kind='workflow'``) whose ``body`` is a YAML/JSON
list of steps. This engine drives one session through that skeleton:

1. **固定骨架 (fixed skeleton):** the ordered steps are parsed once and walked deterministically —
   controllable and reproducible. Progress is tracked in the ``workflow_runs`` table (T5.1).
2. **每步 LLM/skill 驱动 (per-step, live):** for each step the engine resolves the matching **skill +
   code standard** building blocks (their active versions, by name *and* via the ``definition_links``
   relation table) and assembles the **injection material** — the Markdown that teaches the agent how
   to do *this* step. (Actually *writing* that material into the workspace — ``CLAUDE.md`` / ``AGENTS.md``
   / a skill file — is T5.3; this engine produces the material and exposes it via ``injected_md``.)
3. **卡审批点 (approval gates):** a step flagged ``approval: true`` builds a decision card and blocks
   the run, so a human taps to proceed (resumed via ``resume_after_gate``).

QA ("过了才走下一步", §11.2 step 3) is represented here but **driven** by T5.4: ``submit_step`` takes
an explicit ``qa_passed`` the QA reviewer provides, and the engine only uses it for the state
transition (pass → advance, fail → ``failed``). The QA *standard text* is resolved alongside the
skills so T5.4 has it to review against.

This is client-side core: it reaches the local Store (definitions / links / workflow_runs all live
ONLY in the local process, never the shared server — DESIGN §8.3 / §14) and an injected ``cards``
(CardService) for the gate cards, so app.py stays shared-only. Everything is unit-testable without a
live agent: the engine orchestrates state + material; the live hookups (writing the workspace files,
running the QA review, resuming a real agent) are the seams T5.3/T5.4 plug into.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from foreman.shared.events import make_event, utc_now_iso

from ..store.models import Action, WorkflowRun

# step_status values on a WorkflowRun row (DESIGN §7.1 / T5.1 vocabulary).
PENDING = "pending"
RUNNING = "running"
QA = "qa"
PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"

# definition_links relations the engine reads to resolve a step's blocks (DESIGN §11.2).
USES_SKILL = "uses_skill"
USES_STANDARD = "uses_standard"
JUDGED_BY = "judged_by"


@dataclass
class WorkflowStep:
    """One step of the fixed skeleton (DESIGN §11.2). Blocks may be named inline here OR wired via
    the ``definition_links`` table; the engine merges both when resolving material."""

    name: str
    instruction: str = ""
    skills: list[str] = field(default_factory=list)      # skill names to inject for this step
    standards: list[str] = field(default_factory=list)   # code_standard names to inject
    qa: str = ""                                          # qa_rubric name to judge this step
    approval: bool = False                               # this step is an approval gate (卡审批点)
    summary: str = ""                                    # human-facing one-liner (gate card summary)


@dataclass
class WorkflowSpec:
    """A parsed workflow skeleton. ``error`` non-empty ⇒ the body was unusable (fail-closed)."""

    name: str
    steps: list[WorkflowStep] = field(default_factory=list)
    error: str = ""


# ── parsing the skeleton (固定骨架) ──────────────────────────────────────────────────────────────
def _as_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def _as_str_list(value: object) -> list[str]:
    """Coerce to a clean list[str]: a list of scalars, or a lone string, dropping blanks."""
    if isinstance(value, list):
        return [s for s in (_as_str(v) for v in value) if s]
    s = _as_str(value)
    return [s] if s else []


def _as_bool(value: object) -> bool:
    """Conservative truthiness: only an explicit yes is True (so a typo never opens a gate-less run)."""
    if isinstance(value, bool):
        return value
    return _as_str(value).lower() in {"true", "yes", "1"}


def _load_body(body: str) -> object | None:
    """Parse a workflow body as YAML (a superset of JSON, so both formats work). None on failure."""
    if not (body or "").strip():
        return None
    try:
        import yaml

        return yaml.safe_load(body)
    except Exception:  # malformed YAML/JSON — fail closed, never guess a skeleton from garbage.
        return None


def _parse_step(raw: dict, index: int) -> WorkflowStep:
    return WorkflowStep(
        name=_as_str(raw.get("name")) or f"step-{index + 1}",
        instruction=_as_str(raw.get("instruction") or raw.get("goal")),
        skills=_as_str_list(raw.get("skills") or raw.get("skill")),
        standards=_as_str_list(raw.get("standards") or raw.get("standard")),
        qa=_as_str(raw.get("qa") or raw.get("qa_rubric")),
        approval=_as_bool(raw.get("approval") or raw.get("gate")),
        summary=_as_str(raw.get("summary")),
    )


def parse_workflow(body: str, *, name: str = "") -> WorkflowSpec:
    """Parse a workflow ``body`` (YAML/JSON) into a validated ``WorkflowSpec`` (DESIGN §11.2).

    Fail-closed (§6.7 从严默认): an unparseable body, a non-mapping/list shape, an empty step list, or
    a malformed step yields a spec with ``error`` set and **no steps** — the engine refuses to start
    rather than invent a skeleton. Accepts either a top-level list of steps or a ``{name, steps:[…]}``
    mapping.
    """
    data = _load_body(body)
    if data is None:
        return WorkflowSpec(name=name, error="workflow body is empty or not valid YAML/JSON")
    steps_raw: Any
    if isinstance(data, list):
        steps_raw, wf_name = data, name
    elif isinstance(data, dict):
        steps_raw, wf_name = data.get("steps"), (_as_str(data.get("name")) or name)
    else:
        return WorkflowSpec(name=name, error="workflow body must be a mapping or a list of steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        return WorkflowSpec(name=wf_name, error="workflow has no steps")
    steps: list[WorkflowStep] = []
    for i, raw in enumerate(steps_raw):
        if not isinstance(raw, dict):
            return WorkflowSpec(name=wf_name, error=f"step {i} is not a mapping")
        steps.append(_parse_step(raw, i))
    return WorkflowSpec(name=wf_name, steps=steps, error="")


def _run_to_dict(run: WorkflowRun) -> dict:
    return {
        "id": run.id,
        "session_id": run.session_id,
        "workflow_id": run.workflow_id,
        "step_index": run.step_index,
        "step_status": run.step_status,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
    }


class WorkflowEngine:
    """Drives a session through a workflow skeleton (§11.2): start → per-step material → gate/advance.

    ``store`` is the local client Store. ``cards`` (optional) is a CardService used to surface the
    approval-gate card; without it the run still blocks at gates (the card is just not pushed). ``bus``
    (optional) receives ``workflow`` events (persist-first, mirrors the rest of the loop).
    """

    def __init__(
        self,
        store: Any,
        *,
        cards: Any = None,
        bus: Any = None,
        injector: Any = None,
        clock=None,
    ) -> None:
        self.store = store
        self.cards = cards
        self.bus = bus
        # injector (T5.3, optional) writes this step's skills/standards into the workspace before the
        # agent runs (事前注入, §11.2 D). Without it the engine still produces the material via
        # ``injected_md``; the live workspace write is just skipped.
        self.injector = injector
        self._clock = clock or utc_now_iso

    # ── load + start ───────────────────────────────────────────────────────────────────────────
    def load(self, workflow_name: str, *, version: int | None = None) -> tuple[Any, WorkflowSpec]:
        """Resolve a workflow definition (active version, or a specific one) and parse its skeleton.

        Returns ``(definition_row_or_None, spec)``. The spec carries ``error`` when the row is missing
        or its body is unusable, so callers can fail-closed on one check.
        """
        row = self._workflow_row(workflow_name, version)
        if row is None:
            return None, WorkflowSpec(name=workflow_name, error="no such active workflow")
        return row, parse_workflow(row.body, name=row.name)

    def _workflow_row(self, workflow_name: str, version: int | None):
        if version is None:
            if hasattr(self.store, "get_active_definition"):
                return self.store.get_active_definition("workflow", workflow_name)
            return None
        for d in self.store.get_definitions(kind="workflow", name=workflow_name):
            if d.version == version:
                return d
        return None

    async def start(
        self, session_id: str, workflow_name: str, *, version: int | None = None
    ) -> dict:
        """Begin a workflow run for a session: resolve + parse + create a ``workflow_runs`` row.

        Returns ``{ok, run_id, workflow, total_steps, step}`` on success, or ``{ok: False, error}``
        with error ∈ {no_workflow, bad_workflow} (fail-closed on a missing/garbled skeleton).
        """
        row, spec = self.load(workflow_name, version=version)
        if row is None:
            return {"ok": False, "error": "no_workflow"}
        if spec.error or not spec.steps:
            return {"ok": False, "error": "bad_workflow", "detail": spec.error}
        run = self.store.add_workflow_run(
            WorkflowRun(
                id=uuid.uuid4().hex,
                session_id=session_id,
                workflow_id=row.id,
                step_index=0,
                step_status=PENDING,
                started_at=self._clock(),
            )
        )
        await self._emit(session_id, run, "started", {"workflow": spec.name, "steps": len(spec.steps)})
        return {
            "ok": True,
            "run_id": run.id,
            "workflow": spec.name,
            "total_steps": len(spec.steps),
            "step": self._step_view(run, spec),
        }

    # ── per-step material (每步 LLM/skill 驱动) ──────────────────────────────────────────────────
    def _resolve_material(self, workflow_id: str, spec: WorkflowSpec, step_index: int) -> dict:
        """Resolve this step's skill/standard/qa blocks → injection material (§11.2 step 2).

        Block names come from the step body (inline) AND the ``definition_links`` table (the canonical
        wiring, §11.2). Each name resolves to its **active** version's body; an unresolved name is
        recorded in ``missing`` (never silently dropped) so the operator can see a broken wiring.
        """
        step = spec.steps[step_index]
        links = self._links_for_step(workflow_id, step_index)
        skill_names = _dedup(step.skills + links[USES_SKILL])
        standard_names = _dedup(step.standards + links[USES_STANDARD])
        qa_name = step.qa or (links[JUDGED_BY][0] if links[JUDGED_BY] else "")

        missing: list[str] = []
        skills = self._resolve_blocks("skill", skill_names, missing)
        standards = self._resolve_blocks("code_standard", standard_names, missing)
        qa = None
        if qa_name:
            body = self._block_body("qa_rubric", qa_name)
            if body is None:
                missing.append(f"qa_rubric:{qa_name}")
            else:
                qa = {"name": qa_name, "body": body}

        return {
            "step_index": step_index,
            "name": step.name,
            "instruction": step.instruction,
            "approval": step.approval,
            "summary": step.summary,
            "skills": skills,
            "standards": standards,
            "qa": qa,
            "missing": missing,
            "injected_md": _build_injected_md(step, skills, standards),
        }

    def _links_for_step(self, workflow_id: str, step_index: int) -> dict[str, list[str]]:
        """The block NAMES wired to this workflow step, grouped by relation (§11.2 link table)."""
        out: dict[str, list[str]] = {USES_SKILL: [], USES_STANDARD: [], JUDGED_BY: []}
        if not hasattr(self.store, "get_definition_links"):
            return out
        for link in self.store.get_definition_links(workflow_id, step_index=step_index):
            if link.relation not in out:
                continue
            target = self.store.get_definition(link.to_id) if hasattr(
                self.store, "get_definition"
            ) else None
            if target is not None and target.name:
                out[link.relation].append(target.name)
        return out

    def _resolve_blocks(self, kind: str, names: list[str], missing: list[str]) -> list[dict]:
        out: list[dict] = []
        for nm in names:
            body = self._block_body(kind, nm)
            if body is None:
                missing.append(f"{kind}:{nm}")
            else:
                out.append({"name": nm, "body": body})
        return out

    def _block_body(self, kind: str, name: str) -> str | None:
        if not hasattr(self.store, "get_active_definition"):
            return None
        row = self.store.get_active_definition(kind, name)
        return row.body if row is not None else None

    def _step_view(self, run: WorkflowRun, spec: WorkflowSpec) -> dict:
        """The current step's material + run state — what a UI/agent needs to do this step."""
        material = self._resolve_material(run.workflow_id, spec, run.step_index)
        return {**material, "run": _run_to_dict(run), "total_steps": len(spec.steps)}

    def step_view(self, run_id: str) -> dict | None:
        """Public: the current step view for a run (None if the run / its workflow is gone)."""
        run = self.store.get_workflow_run(run_id)
        if run is None:
            return None
        spec = self._spec_for_run(run)
        if spec is None or run.step_index >= len(spec.steps):
            return None
        return self._step_view(run, spec)

    def begin_step(self, run_id: str) -> dict:
        """Mark the current step ``running``, inject its material into the workspace, return the view.

        The injection (T5.3, 事前注入 §11.2 D) writes this step's skills/standards into the session's
        workspace (CLAUDE.md / AGENTS.md / skill files) so the agent reads them before it starts. It is
        best-effort: a write failure or missing workspace is reported in ``injection`` but never aborts
        the step (the material is also available in the returned step view).
        """
        run = self.store.get_workflow_run(run_id)
        if run is None:
            return {"ok": False, "error": "no_run"}
        if run.step_status in (PASSED, FAILED):
            return {"ok": False, "error": "run_finished"}
        spec = self._spec_for_run(run)
        if spec is None or run.step_index >= len(spec.steps):
            return {"ok": False, "error": "bad_workflow"}
        run = self.store.update_workflow_run(run_id, step_status=RUNNING)
        step_view = self._step_view(run, spec)
        injection = self._inject(run, step_view)
        return {"ok": True, "step": step_view, "injection": injection}

    def _inject(self, run: WorkflowRun, material: dict) -> dict | None:
        """Write this step's material into the session's workspace (事前注入, T5.3). None if no injector."""
        if self.injector is None:
            return None
        workspace = self._workspace_for(run.session_id)
        if not workspace:
            return {"ok": False, "error": "no_workspace"}
        try:
            return self.injector.inject(workspace, material, agents=self._agent_for(run.session_id))
        except Exception as exc:  # noqa: BLE001 — injection must never crash the step.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    def _workspace_for(self, session_id: str) -> str:
        if not hasattr(self.store, "get_session"):
            return ""
        session = self.store.get_session(session_id)
        return getattr(session, "workspace", "") or "" if session is not None else ""

    def _agent_for(self, session_id: str) -> str | None:
        if not hasattr(self.store, "get_session"):
            return None
        session = self.store.get_session(session_id)
        return getattr(session, "agent_type", None) if session is not None else None

    # ── advance / gate / finish ─────────────────────────────────────────────────────────────────
    async def submit_step(self, run_id: str, *, qa_passed: bool | None = None) -> dict:
        """Finish the current step: open an approval gate, await/apply QA, or advance (§11.2).

        - An **approval-gate** step (``approval: true``) builds a decision card and blocks; resume with
          ``resume_after_gate``.
        - A step with a **QA standard** and no ``qa_passed`` yet is parked in ``qa`` (T5.4 runs the
          review, then calls back with the result). ``qa_passed=False`` → ``failed``; ``True`` →
          advance.
        - Otherwise → advance to the next step (or finish the run).
        """
        run = self.store.get_workflow_run(run_id)
        if run is None:
            return {"ok": False, "error": "no_run"}
        if run.step_status in (PASSED, FAILED):
            return {"ok": False, "error": "run_finished"}
        if run.step_status == BLOCKED:
            # Already parked at a gate — the only way forward is resume_after_gate, so a stray submit
            # must not open (and re-card) the gate a second time.
            return {"ok": False, "error": "blocked_on_gate"}
        spec = self._spec_for_run(run)
        if spec is None or run.step_index >= len(spec.steps):
            return {"ok": False, "error": "bad_workflow"}
        step = spec.steps[run.step_index]

        if step.approval:
            return await self._open_gate(run, spec, step)

        if step.qa:
            if qa_passed is None:
                run = self.store.update_workflow_run(run_id, step_status=QA)
                material = self._resolve_material(run.workflow_id, spec, run.step_index)
                return {"ok": True, "status": QA, "qa": material["qa"], "step": self._step_view(run, spec)}
            if not qa_passed:
                run = self.store.update_workflow_run(run_id, step_status=FAILED)
                self._clear(run)  # run failed — revert the injected workspace scaffolding (T5.3).
                await self._emit(run.session_id, run, "qa_failed", {"step": step.name})
                return {"ok": True, "status": FAILED, "step": self._step_view(run, spec)}

        return await self._advance(run, spec)

    async def _open_gate(self, run: WorkflowRun, spec: WorkflowSpec, step: WorkflowStep) -> dict:
        """Approval gate (卡审批点): build a decision card and block the run until a human taps."""
        card = None
        action_id = self._gate_action(run, step)
        if self.cards is not None and action_id:
            summary = step.summary or f"工作流「{spec.name}」第 {run.step_index + 1} 步：{step.name} — 准不准继续？"
            card = self.cards.build_card(
                action_id=action_id,
                session_id=run.session_id,
                summary=summary,
                audit_note="workflow approval gate",
                diff_stat="",
            )
        run = self.store.update_workflow_run(run.id, step_status=BLOCKED)
        await self._emit(run.session_id, run, "gate", {"step": step.name, "card_id": card["id"] if card else ""})
        return {"ok": True, "status": BLOCKED, "card": card, "step": self._step_view(run, spec)}

    def _gate_action(self, run: WorkflowRun, step: WorkflowStep) -> str:
        """Persist a synthetic ``actions`` row backing the gate card (kind ``workflow_gate``).

        A no-op kind: if it ever reaches the decision-loop executor it defers (never mis-runs as a
        shell command), so the only way past the gate is the explicit ``resume_after_gate``.
        """
        if not hasattr(self.store, "add_action"):
            return ""
        action = Action(
            id=uuid.uuid4().hex,
            session_id=run.session_id,
            kind="workflow_gate",
            command="",
            rationale=f"workflow gate at step {run.step_index + 1} ({step.name})",
            expected_effect="advance the workflow past this approval gate",
            reversible=True,
            status="carded",
            created_at=self._clock(),
        )
        self.store.add_action(action)
        return action.id

    async def resume_after_gate(self, run_id: str, approved: bool) -> dict:
        """Resume a run blocked at an approval gate: ``approved`` advances it, else it ``fails``."""
        run = self.store.get_workflow_run(run_id)
        if run is None:
            return {"ok": False, "error": "no_run"}
        if run.step_status != BLOCKED:
            return {"ok": False, "error": "not_blocked"}
        spec = self._spec_for_run(run)
        if spec is None:
            return {"ok": False, "error": "bad_workflow"}
        if not approved:
            run = self.store.update_workflow_run(run_id, step_status=FAILED)
            self._clear(run)  # gate rejected — revert the injected workspace scaffolding (T5.3).
            await self._emit(run.session_id, run, "gate_rejected", {})
            return {"ok": True, "status": FAILED, "step": self._step_view(run, spec)}
        return await self._advance(run, spec)

    async def _advance(self, run: WorkflowRun, spec: WorkflowSpec) -> dict:
        """Move to the next step, or finish the run if this was the last (§11.2 sequencing)."""
        nxt = run.step_index + 1
        if nxt >= len(spec.steps):
            run = self.store.update_workflow_run(
                run.id, step_status=PASSED, ended_at=self._clock()
            )
            self._clear(run)  # run done — revert the injected workspace scaffolding (T5.3).
            await self._emit(run.session_id, run, "done", {"workflow": spec.name})
            return {"ok": True, "status": "done", "run": _run_to_dict(run)}
        run = self.store.update_workflow_run(run.id, step_index=nxt, step_status=PENDING)
        await self._emit(run.session_id, run, "advanced", {"to_step": nxt})
        return {"ok": True, "status": "advanced", "step": self._step_view(run, spec)}

    def _clear(self, run: WorkflowRun) -> None:
        """Best-effort revert of the injected workspace scaffolding when a run ends (T5.3)."""
        if self.injector is None:
            return
        workspace = self._workspace_for(run.session_id)
        if not workspace:
            return
        try:
            self.injector.clear(workspace, agents=self._agent_for(run.session_id))
        except Exception:  # noqa: BLE001 — cleanup is best-effort, never crash the finish.
            pass

    # ── helpers ─────────────────────────────────────────────────────────────────────────────────
    def _spec_for_run(self, run: WorkflowRun) -> WorkflowSpec | None:
        defn = self.store.get_definition(run.workflow_id) if hasattr(
            self.store, "get_definition"
        ) else None
        if defn is None:
            return None
        spec = parse_workflow(defn.body, name=defn.name)
        return spec if not spec.error and spec.steps else None

    async def _emit(self, session_id: str, run: WorkflowRun, phase: str, extra: dict) -> None:
        """Persist-then-publish a ``workflow`` event (mirrors Runner / Gate / DecisionLoop)."""
        payload = {"run_id": run.id, "phase": phase, "step_index": run.step_index, **extra}
        event = make_event("workflow", "workflow-engine", session_id, payload=payload)
        if hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)


def _dedup(names: list[str]) -> list[str]:
    """Order-preserving de-dup (a name can appear inline and via a link)."""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _build_injected_md(step: WorkflowStep, skills: list[dict], standards: list[dict]) -> str:
    """Assemble the per-step injection material (the Markdown T5.3 will write to the workspace).

    "前面教" (§11.2 D): the agent reads this before doing the step — the instruction plus every
    resolved skill and code standard. Pure text assembly; no files are touched here (that is T5.3).
    """
    parts: list[str] = []
    if step.instruction:
        parts.append(f"## 本步任务\n{step.instruction}")
    for s in skills:
        parts.append(f"## 技能：{s['name']}\n{s['body']}")
    for st in standards:
        parts.append(f"## 代码规范：{st['name']}\n{st['body']}")
    return "\n\n".join(parts)


__all__ = [
    "WorkflowEngine",
    "WorkflowSpec",
    "WorkflowStep",
    "parse_workflow",
    "PENDING",
    "RUNNING",
    "QA",
    "PASSED",
    "FAILED",
    "BLOCKED",
    "USES_SKILL",
    "USES_STANDARD",
    "JUDGED_BY",
]
