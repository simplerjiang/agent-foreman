"""P5 — workflow control flow: API + lightweight step dispatch + step-boundary compression (§10/§8B.6).

The WorkflowEngine state machine itself is covered by test_workflow_engine; here we test the NEW P5
wiring: the /api/workflows/* routes (status codes + 503 fallback), the dispatcher's lightweight
launch_workflow_step (inject + ONE launch, no PM review loop, names-not-bodies instruction), and the
scope=workflow MemoryItem written at each step boundary.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.core.cards import CardService
from foreman.client.core.context_budget import MEMORY_SCOPE_WORKFLOW
from foreman.client.core.dispatch_service import DispatchService, _workflow_step_instruction
from foreman.client.core.workflow_engine import WorkflowEngine
from foreman.client.store import Store
from foreman.client.store.models import Definition, Session
from foreman.server.app import create_app
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import EventBus

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

SIMPLE_WF = """
name: two-step
steps:
  - name: step-a
    instruction: do A
    skills: [how-to-test]
    standards: [our-style]
  - name: step-b
    instruction: do B
"""


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _seed(st, *, body=WF_YAML, name="add-feature", workspace=""):
    st.add_session(Session(id="s1", goal="g", workspace=workspace))
    wf = st.add_definition(Definition(id="wf1", kind="workflow", name=name, version=1, body=body))
    st.set_definition_active(wf.id)
    for kind, nm, b in [
        ("skill", "how-to-test", "# SKILL_BODY_TESTS"),
        ("code_standard", "test-naming", "# STD_BODY_NAMING"),
        ("code_standard", "our-style", "# STD_BODY_STYLE"),
        ("qa_rubric", "covers-happy-path", "# does it cover the happy path?"),
    ]:
        d = st.add_definition(Definition(id=f"{kind}:{nm}", kind=kind, name=nm, body=b))
        st.set_definition_active(d.id)
    return name


# ── instruction helper: names (L0 index), NOT bodies ──────────────────────────────────────────────
def test_step_instruction_has_names_not_bodies():
    step = {"name": "step-a", "instruction": "do A",
            "skills": [{"name": "how-to-test", "body": "# SKILL_BODY_TESTS"}],
            "standards": [{"name": "our-style", "body": "# STD_BODY_STYLE"}]}
    instr = _workflow_step_instruction(step, language="en")
    assert "step-a" in instr and "do A" in instr
    assert "how-to-test" in instr and "our-style" in instr  # L0 index (names)
    assert "SKILL_BODY_TESTS" not in instr and "STD_BODY_STYLE" not in instr  # bodies stay in files
    assert "push, merge, or deploy" in instr  # guardrail


# ── API routes ────────────────────────────────────────────────────────────────────────────────────
def _app(tmp_path, *, with_engine=True, **seed):
    st = _store(tmp_path)
    _seed(st, **seed)
    eng = WorkflowEngine(st, cards=CardService(st), bus=EventBus()) if with_engine else None
    app = create_app(Config(), st, EventBus(), workflow_engine=eng)
    return app, st, eng


def test_routes_503_without_engine(tmp_path):
    app, _, _ = _app(tmp_path, with_engine=False)
    c = TestClient(app)
    assert c.post("/api/workflows/start",
                  json={"session_id": "s1", "workflow": "add-feature"}).status_code == 503
    assert c.get("/api/workflows/x").status_code == 503


def test_routes_start_view_begin_and_error_codes(tmp_path):
    app, _, _ = _app(tmp_path)
    c = TestClient(app)
    r = c.post("/api/workflows/start", json={"session_id": "s1", "workflow": "add-feature"})
    assert r.status_code == 200 and r.json()["total_steps"] == 3
    rid = r.json()["run_id"]
    assert c.get(f"/api/workflows/{rid}").status_code == 200
    assert c.post("/api/workflows/begin", json={"run_id": rid}).status_code == 200  # inject-only
    # error-code mapping
    assert c.post("/api/workflows/start",
                  json={"session_id": "s1", "workflow": "nope"}).status_code == 404  # no_workflow
    assert c.get("/api/workflows/nope").status_code == 404
    assert c.post("/api/workflows/submit",
                  json={"run_id": "nope"}).status_code == 404  # no_run
    assert c.post("/api/workflows/resume",
                  json={"run_id": rid, "approved": True}).status_code == 409  # not_blocked


def test_routes_full_flow_to_done(tmp_path):
    app, _, _ = _app(tmp_path)
    c = TestClient(app)
    rid = c.post("/api/workflows/start",
                 json={"session_id": "s1", "workflow": "add-feature"}).json()["run_id"]
    # step 0 (write-tests) has a qa gate: plain submit parks it in qa, qa_passed=true advances.
    c.post("/api/workflows/begin", json={"run_id": rid})
    assert c.post("/api/workflows/submit", json={"run_id": rid}).json()["status"] == "qa"
    assert c.post("/api/workflows/submit",
                  json={"run_id": rid, "qa_passed": True}).json()["status"] == "advanced"
    # step 1 (implement) no qa → advances
    c.post("/api/workflows/begin", json={"run_id": rid})
    assert c.post("/api/workflows/submit", json={"run_id": rid}).json()["status"] == "advanced"
    # step 2 (push-gate) approval → blocked, then resume(True) → done
    c.post("/api/workflows/begin", json={"run_id": rid})
    assert c.post("/api/workflows/submit", json={"run_id": rid}).json()["status"] == "blocked"
    assert c.post("/api/workflows/resume",
                  json={"run_id": rid, "approved": True}).json()["status"] == "done"


# ── lightweight dispatch + step-boundary compression ──────────────────────────────────────────────
class _FakeHandle:
    session_id = "s1"


class _CountingRunner:
    def __init__(self):
        self.launches = []
        self.waits = 0

    async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
        self.launches.append(instruction)
        return _FakeHandle()

    async def wait(self, handle):
        self.waits += 1


async def test_launch_workflow_step_injects_and_launches_once(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    st = _store(tmp_path)
    _seed(st, body=SIMPLE_WF, name="two-step", workspace=str(ws))
    from foreman.client.core.injector import WorkspaceInjector
    eng = WorkflowEngine(st, injector=WorkspaceInjector(), bus=EventBus())
    runner = _CountingRunner()
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(ws))]
    svc = DispatchService(cfg, st, runner=runner, workflow_engine=eng)

    run = (await eng.start("s1", "two-step"))
    rid = run["run_id"]
    res = await svc.launch_workflow_step(rid, agent="codex")
    assert res["ok"] is True
    # exactly one launch + wait (no PM review loop — lightweight dispatch §10)
    assert len(runner.launches) == 1 and runner.waits == 1
    # the step material was injected into the workspace (real path, not just the return dict)
    assert (ws / "AGENTS.md").exists() or (ws / "CLAUDE.md").exists()
    # the instruction carries the L0 index, not the skill body
    assert "how-to-test" in runner.launches[0] and "SKILL_BODY_TESTS" not in runner.launches[0]


async def test_step_boundary_writes_workflow_scope_memory(tmp_path):
    st = _store(tmp_path)
    _seed(st, body=SIMPLE_WF, name="two-step")
    eng = WorkflowEngine(st, bus=EventBus())
    run = await eng.start("s1", "two-step")
    rid = run["run_id"]
    # advance step 0 → a scope=workflow MemoryItem is written at the boundary
    eng.begin_step(rid)
    await eng.submit_step(rid)  # step-a has no qa/gate → advance
    mems = st.get_memory_items("s1", scope=MEMORY_SCOPE_WORKFLOW)
    assert mems, "expected a scope=workflow MemoryItem after a step advance"
    assert any("step-a" in m.text for m in mems)


async def test_n_step_run_does_not_trigger_pm_reviews(tmp_path):
    """A 2-step workflow run via lightweight dispatch triggers N launches and ZERO PM reviews."""
    ws = tmp_path / "ws2"
    ws.mkdir()
    st = _store(tmp_path)
    _seed(st, body=SIMPLE_WF, name="two-step", workspace=str(ws))
    eng = WorkflowEngine(st, bus=EventBus())
    runner = _CountingRunner()

    class _NoReviewPM:
        async def review(self, *a, **k):  # must never be called
            raise AssertionError("workflow step dispatch must not run PM review")

    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(ws))]
    svc = DispatchService(cfg, st, runner=runner, pm_agent=_NoReviewPM(), workflow_engine=eng)
    rid = (await eng.start("s1", "two-step"))["run_id"]
    for _ in range(2):
        await svc.launch_workflow_step(rid, agent="codex")
        await eng.submit_step(rid)
    assert len(runner.launches) == 2  # one launch per step, no review loop
