"""Tests for the hybrid workflow engine — fixed skeleton + per-step injection + approval gates.

TASKS T5.2 / DESIGN §11.2. Covers, over a REAL client Store (tmp_path sqlite) with a real
CardService and a recording bus:
  - parse_workflow(): JSON + YAML skeletons, and fail-closed parsing of garbage;
  - start(): missing / garbled workflow → fail-closed; success creates a workflow_runs row + event;
  - per-step material: inline skills/standards/qa bodies + definition_links wiring + missing/dedup;
  - the step state machine: begin → advance, QA park/pass/fail, approval-gate card + resume, finish.
"""

from __future__ import annotations

from foreman.client.core.cards import CardService
from foreman.client.core.workflow_engine import (
    BLOCKED,
    FAILED,
    PASSED,
    PENDING,
    QA,
    RUNNING,
    WorkflowEngine,
    parse_workflow,
)
from foreman.client.store import Store
from foreman.client.store.models import Definition, DefinitionLink, Session

# A self-contained two-step + gate workflow (YAML, the §585 format for workflow bodies).
WF_YAML = """
name: add-feature
steps:
  - name: write-tests
    instruction: Write failing tests first.
    skills: [how-to-test]
    standards: [test-naming]
    qa: covers-happy-path
  - name: implement
    instruction: Make the tests pass.
    standards: [our-style]
  - name: push-gate
    approval: true
    summary: Ready to push — approve?
"""


class _RecBus:
    def __init__(self):
        self.events = []

    async def publish(self, event):
        self.events.append(event)


def _store(tmp_path) -> Store:
    st = Store(str(tmp_path / "t.db"))
    st.init()
    return st


def _seed(st: Store) -> str:
    """Seed a session + the workflow + the blocks it names; return the active workflow's name."""
    st.add_session(Session(id="s1", goal="add feature", workspace=""))
    wf = st.add_definition(
        Definition(id="wf1", kind="workflow", name="add-feature", version=1, body=WF_YAML)
    )
    st.set_definition_active(wf.id)
    for kind, name, body in [
        ("skill", "how-to-test", "# write a failing test first"),
        ("code_standard", "test-naming", "# name tests test_<unit>_<case>"),
        ("code_standard", "our-style", "# 100-col lines, no bare except"),
        ("qa_rubric", "covers-happy-path", "# does it cover the happy path?"),
    ]:
        d = st.add_definition(Definition(id=f"{kind}:{name}", kind=kind, name=name, body=body))
        st.set_definition_active(d.id)
    return "add-feature"


def _engine(st: Store, bus=None) -> WorkflowEngine:
    return WorkflowEngine(st, cards=CardService(st), bus=bus, clock=lambda: "2026-06-21T00:00:00+00:00")


# ── parse_workflow (固定骨架) ──────────────────────────────────────────────────────────────────
def test_parse_workflow_yaml_skeleton():
    spec = parse_workflow(WF_YAML)
    assert spec.error == "" and spec.name == "add-feature"
    assert [s.name for s in spec.steps] == ["write-tests", "implement", "push-gate"]
    assert spec.steps[0].skills == ["how-to-test"]
    assert spec.steps[0].qa == "covers-happy-path"
    assert spec.steps[2].approval is True


def test_parse_workflow_json_list_and_name_default():
    spec = parse_workflow('[{"name": "a"}, {"instruction": "do b"}]', name="inline")
    assert spec.error == "" and spec.name == "inline"
    assert spec.steps[0].name == "a"
    assert spec.steps[1].name == "step-2"  # default name from index
    assert spec.steps[1].instruction == "do b"


def test_parse_workflow_fail_closed():
    for body in ["", "   ", "{not valid yaml: [", "42", "name: x\nsteps: []",
                 "name: x\nsteps: hello"]:
        spec = parse_workflow(body)
        assert spec.error and spec.steps == []  # never invent a skeleton from garbage


def test_parse_workflow_rejects_non_mapping_step():
    spec = parse_workflow("steps:\n  - name: ok\n  - just a string\n")
    assert spec.error and spec.steps == []


def test_parse_step_bool_is_conservative():
    spec = parse_workflow("steps:\n  - name: g\n    approval: maybe\n")
    assert spec.steps[0].approval is False  # only an explicit yes opens a gate


# ── start (fail-closed + run creation) ─────────────────────────────────────────────────────────
async def test_start_missing_workflow(tmp_path):
    st = _store(tmp_path)
    st.add_session(Session(id="s1", goal="g"))
    res = await _engine(st).start("s1", "nope")
    assert res == {"ok": False, "error": "no_workflow"}


async def test_start_bad_workflow_body(tmp_path):
    st = _store(tmp_path)
    st.add_session(Session(id="s1", goal="g"))
    d = st.add_definition(Definition(id="wf", kind="workflow", name="broken", body="not: [valid"))
    st.set_definition_active(d.id)
    res = await _engine(st).start("s1", "broken")
    assert res["ok"] is False and res["error"] == "bad_workflow"


async def test_start_creates_run_and_emits(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    bus = _RecBus()
    res = await _engine(st, bus).start("s1", name)
    assert res["ok"] and res["total_steps"] == 3
    run = st.get_workflow_run(res["run_id"])
    assert run.step_index == 0 and run.step_status == PENDING and run.started_at
    assert [e.type for e in bus.events] == ["workflow"]
    assert bus.events[0].payload["phase"] == "started"
    assert res["step"]["name"] == "write-tests"


# ── per-step material (每步 LLM/skill 驱动) ──────────────────────────────────────────────────────
async def test_step_material_resolves_inline_blocks(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    res = await _engine(st).start("s1", name)
    step = res["step"]
    assert [s["name"] for s in step["skills"]] == ["how-to-test"]
    assert step["skills"][0]["body"] == "# write a failing test first"
    assert [s["name"] for s in step["standards"]] == ["test-naming"]
    assert step["qa"]["name"] == "covers-happy-path"
    assert step["missing"] == []
    # injected material teaches the agent before the step (T5.3 will write it to the workspace)
    assert "## 本步任务" in step["injected_md"]
    assert "# write a failing test first" in step["injected_md"]
    assert "# name tests" in step["injected_md"]


async def test_step_material_records_missing_block(tmp_path):
    st = _store(tmp_path)
    st.add_session(Session(id="s1", goal="g"))
    d = st.add_definition(
        Definition(
            id="wf",
            kind="workflow",
            name="w",
            body="steps:\n  - name: s\n    skills: [ghost]\n    qa: phantom\n",
        )
    )
    st.set_definition_active(d.id)
    res = await _engine(st).start("s1", "w")
    assert set(res["step"]["missing"]) == {"skill:ghost", "qa_rubric:phantom"}


async def test_step_material_merges_links_and_dedups(tmp_path):
    st = _store(tmp_path)
    st.add_session(Session(id="s1", goal="g"))
    wf = st.add_definition(
        Definition(
            id="wf",
            kind="workflow",
            name="w",
            body="steps:\n  - name: s\n    skills: [how-to-test]\n",
        )
    )
    st.set_definition_active(wf.id)
    for kind, name in [("skill", "how-to-test"), ("skill", "linked-skill")]:
        d = st.add_definition(Definition(id=f"{kind}:{name}", kind=kind, name=name, body=f"body {name}"))
        st.set_definition_active(d.id)
    # wire two skills to step 0 via the link table; one duplicates the inline name.
    st.add_definition_link(
        DefinitionLink(id="l1", from_id="wf", to_id="skill:how-to-test", relation="uses_skill", step_index=0)
    )
    st.add_definition_link(
        DefinitionLink(id="l2", from_id="wf", to_id="skill:linked-skill", relation="uses_skill", step_index=0)
    )
    res = await _engine(st).start("s1", "w")
    # inline + linked, de-duplicated, order-preserving
    assert [s["name"] for s in res["step"]["skills"]] == ["how-to-test", "linked-skill"]


# ── state machine: begin → advance / qa / gate / finish ────────────────────────────────────────
async def test_begin_step_marks_running(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    res = await _engine(st).start("s1", name)
    out = _engine(st).begin_step(res["run_id"])
    assert out["ok"]
    assert st.get_workflow_run(res["run_id"]).step_status == RUNNING


async def test_qa_step_parks_then_advances(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    eng = _engine(st)
    res = await eng.start("s1", name)
    rid = res["run_id"]
    # step 0 has a QA standard → no qa_passed → parked in `qa`, with the rubric returned for T5.4
    parked = await eng.submit_step(rid)
    assert parked["status"] == QA and parked["qa"]["name"] == "covers-happy-path"
    assert st.get_workflow_run(rid).step_status == QA
    # QA passes → advance to step 1
    adv = await eng.submit_step(rid, qa_passed=True)
    assert adv["status"] == "advanced"
    assert st.get_workflow_run(rid).step_index == 1
    assert adv["step"]["name"] == "implement"


async def test_qa_failure_marks_failed(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    eng = _engine(st)
    res = await eng.start("s1", name)
    out = await eng.submit_step(res["run_id"], qa_passed=False)
    assert out["status"] == FAILED
    assert st.get_workflow_run(res["run_id"]).step_status == FAILED
    # a finished run refuses further submits
    again = await eng.submit_step(res["run_id"])
    assert again == {"ok": False, "error": "run_finished"}


async def test_step_without_qa_advances_directly(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    eng = _engine(st)
    res = await eng.start("s1", name)
    rid = res["run_id"]
    await eng.submit_step(rid, qa_passed=True)  # past step 0 (write-tests)
    # step 1 (implement) has no qa → submit advances straight to the gate step
    adv = await eng.submit_step(rid)
    assert adv["status"] == "advanced" and adv["step"]["name"] == "push-gate"


async def test_approval_gate_blocks_and_cards(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    bus = _RecBus()
    eng = _engine(st, bus)
    res = await eng.start("s1", name)
    rid = res["run_id"]
    await eng.submit_step(rid, qa_passed=True)  # step 0 → 1
    await eng.submit_step(rid)                   # step 1 → 2 (gate)
    gate = await eng.submit_step(rid)            # open the gate
    assert gate["status"] == BLOCKED
    assert st.get_workflow_run(rid).step_status == BLOCKED
    # a real decision card was built + persisted, backed by a synthetic gate action
    assert gate["card"] is not None
    cards = st.get_decision_cards("s1")
    assert len(cards) == 1 and cards[0].id == gate["card"]["id"]
    action = st.get_action(cards[0].action_id)
    assert action is not None and action.kind == "workflow_gate" and action.command == ""
    assert any(e.payload.get("phase") == "gate" for e in bus.events)


async def test_submit_on_blocked_gate_does_not_recard(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    eng = _engine(st)
    res = await eng.start("s1", name)
    rid = res["run_id"]
    await eng.submit_step(rid, qa_passed=True)
    await eng.submit_step(rid)
    await eng.submit_step(rid)  # open gate → 1 card
    stray = await eng.submit_step(rid)
    assert stray == {"ok": False, "error": "blocked_on_gate"}
    assert len(st.get_decision_cards("s1")) == 1  # no second card


async def test_resume_after_gate_approved_finishes(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    eng = _engine(st)
    res = await eng.start("s1", name)
    rid = res["run_id"]
    await eng.submit_step(rid, qa_passed=True)
    await eng.submit_step(rid)
    await eng.submit_step(rid)  # open gate (last step)
    done = await eng.resume_after_gate(rid, approved=True)
    assert done["status"] == "done"
    run = st.get_workflow_run(rid)
    assert run.step_status == PASSED and run.ended_at


async def test_resume_after_gate_rejected_fails(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    eng = _engine(st)
    res = await eng.start("s1", name)
    rid = res["run_id"]
    await eng.submit_step(rid, qa_passed=True)
    await eng.submit_step(rid)
    await eng.submit_step(rid)  # open gate
    out = await eng.resume_after_gate(rid, approved=False)
    assert out["status"] == FAILED
    assert st.get_workflow_run(rid).step_status == FAILED


async def test_resume_after_gate_requires_blocked(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)
    eng = _engine(st)
    res = await eng.start("s1", name)
    out = await eng.resume_after_gate(res["run_id"], approved=True)
    assert out == {"ok": False, "error": "not_blocked"}  # run is at step 0, not a gate


# ── T5.3 wiring: begin_step injects into the workspace, finish/fail clears it ───────────────────────
def _seed_ws(st: Store, ws: str) -> str:
    """Like _seed but the session has a real workspace path (for injection)."""
    st.add_session(Session(id="s1", goal="add feature", workspace=ws, agent_type="claude-code"))
    wf = st.add_definition(
        Definition(id="wf1", kind="workflow", name="add-feature", version=1, body=WF_YAML)
    )
    st.set_definition_active(wf.id)
    for kind, name, body in [
        ("skill", "how-to-test", "# write a failing test first"),
        ("code_standard", "test-naming", "# name tests test_<unit>_<case>"),
        ("code_standard", "our-style", "# 100-col lines, no bare except"),
        ("qa_rubric", "covers-happy-path", "# does it cover the happy path?"),
    ]:
        d = st.add_definition(Definition(id=f"{kind}:{name}", kind=kind, name=name, body=body))
        st.set_definition_active(d.id)
    return "add-feature"


async def test_begin_step_injects_into_workspace(tmp_path):
    from foreman.client.core.injector import MARKER_BEGIN, WorkspaceInjector

    ws = tmp_path / "ws"
    ws.mkdir()
    st = _store(tmp_path)
    name = _seed_ws(st, str(ws))
    eng = WorkflowEngine(st, injector=WorkspaceInjector(), clock=lambda: "2026-06-21T00:00:00+00:00")
    res = await eng.start("s1", name)
    out = eng.begin_step(res["run_id"])
    assert out["ok"] and out["injection"]["ok"] is True
    # CLAUDE.md (the session's agent is claude-code) carries the step's standards in a managed block,
    # and the skill landed as a native Claude Code skill (P2 §7: progressive disclosure).
    claude = (ws / "CLAUDE.md").read_text(encoding="utf-8")
    assert MARKER_BEGIN in claude and "test-naming" in claude
    assert (ws / ".claude" / "skills" / "foreman-how-to-test" / "SKILL.md").exists()


async def test_run_finish_clears_injection(tmp_path):
    from foreman.client.core.injector import WorkspaceInjector

    ws = tmp_path / "ws"
    ws.mkdir()
    st = _store(tmp_path)
    name = _seed_ws(st, str(ws))
    eng = WorkflowEngine(st, cards=CardService(st), injector=WorkspaceInjector(),
                         clock=lambda: "2026-06-21T00:00:00+00:00")
    res = await eng.start("s1", name)
    rid = res["run_id"]
    eng.begin_step(rid)
    assert (ws / "CLAUDE.md").exists()
    # walk to the end: qa pass → advance → advance → gate → approve → done
    await eng.submit_step(rid, qa_passed=True)
    await eng.submit_step(rid)
    await eng.submit_step(rid)
    done = await eng.resume_after_gate(rid, approved=True)
    assert done["status"] == "done"
    # the run finished → injected scaffolding reverted (T5.3 clear).
    assert not (ws / "CLAUDE.md").exists()
    assert not (ws / ".foreman").exists()


async def test_begin_step_no_injector_is_noop(tmp_path):
    st = _store(tmp_path)
    name = _seed(st)  # session has no workspace
    eng = _engine(st)  # no injector wired
    res = await eng.start("s1", name)
    out = eng.begin_step(res["run_id"])
    assert out["ok"] and out["injection"] is None
