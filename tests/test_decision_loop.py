"""Unit tests for the Decision Loop串联 (P4 acceptance, DESIGN §6.2).

Covers, over a real client Store, with fake LLM roles + injected execution backends (no live CLI /
desktop):
  - observe(): Operator → Auditor → Gate disposition → card / auto / report / reject / revise;
  - execute_action(): checkpoint (REAL git) → run via the Toolbelt → record + emit;
  - on_card_decision(): approve→execute, undo→restore, manual→noop;
  - the irreversible red line (a requires-approval command never auto-runs, even at level 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from foreman.client.core.cards import CardService
from foreman.client.core.decision_loop import DecisionLoop
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.shared.config import GatesCfg
from foreman.shared.events import EventBus


# ── fakes ────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class _Proposal:
    command: str
    kind: str = "shell"
    rationale: str = ""
    expected_effect: str = ""
    reversible: bool = True


@dataclass
class _OpResult:
    summary: str = "did a thing"
    state: str = "running"
    proposals: list = field(default_factory=list)


class _FakeOperator:
    def __init__(self, result: _OpResult):
        self._result = result
        self.seen: dict = {}

    async def observe(self, goal, agent_output, *, context="", recent_actions=""):
        self.seen = {"goal": goal, "output": agent_output}
        return self._result


@dataclass
class _AuditOut:
    verdict: str
    goal_quality: str = "on-track"
    risk_severity: str = "none"
    reasons: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)
    model: str = "fake-model"


class _FakeAuditor:
    def __init__(self, verdict="pass", **kw):
        self._out = _AuditOut(verdict=verdict, **kw)
        self.calls = 0

    async def audit(self, command, **kw):
        self.calls += 1
        return self._out


class _FakeToolbelt:
    """Records run_shell calls; writes a file into the workspace so the checkpoint→diff is real."""

    def __init__(self, workspace=None):
        self.workspace = workspace
        self.calls: list = []

    def run_shell(self, command, *, admin=False, approved=False):
        self.calls.append({"command": command, "approved": approved})
        if self.workspace is not None:
            (self.workspace / "made.txt").write_text(command, encoding="utf-8")

        @dataclass
        class _R:
            ok: bool = True
            detail: str = "exit 0"
            error: str = ""

        return _R()


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "loop.db"))
    s.init()
    return s


def _git_workspace(tmp_path):
    """A real git repo so checkpoints (snapshot/undo) run for real."""
    import subprocess

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init"], cwd=str(ws), capture_output=True, text=True, check=True)
    (ws / "seed.txt").write_text("seed\n", encoding="utf-8")
    return ws


def _loop(store, *, operator, auditor, toolbelt=None, gate_cfg=None, bus=None):
    from foreman.client.core.gate import Gate

    gate = Gate(gate_cfg or GatesCfg(requires_approval=["git push"], needs_strategy=["pip install"]))
    cards = CardService(store, bus=bus)
    loop = DecisionLoop(
        store=store, gate=gate, cards=cards, operator=operator, auditor=auditor,
        bus=bus, toolbelt=toolbelt,
    )
    cards.executor = loop.on_card_decision
    return loop, cards


# ── observe → card (default level 1: ask everything) ───────────────────────────────────────────────
async def test_observe_passing_proposal_makes_a_card_at_level_1(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=""))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="ruff check .")]))
    aud = _FakeAuditor(verdict="pass")
    loop, cards = _loop(store, operator=op, auditor=aud)

    res = await loop.observe("s1", "g", "agent said stuff", level=1)
    assert res["state"] == "running"
    r0 = res["results"][0]
    assert r0["outcome"] == "card" and r0["verdict"] == "pass"
    # the card is persisted and addresses the audited action
    listed = cards.list_cards("s1")
    assert len(listed) == 1 and listed[0]["action_id"] == r0["action_id"]
    action = store.get_action(r0["action_id"])
    assert action.status == "carded"
    assert aud.calls == 1  # the auditor ran on the proposal


async def test_observe_reject_drops_without_a_card(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="cat /etc/passwd")]))
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="reject",
                                                                 goal_quality="garbage"))
    res = await loop.observe("s1", "g", "out")
    assert res["results"][0]["outcome"] == "rejected"
    assert cards.list_cards("s1") == []
    assert store.get_action(res["results"][0]["action_id"]).status == "rejected"


async def test_observe_escalate_always_cards_even_at_level_3(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="ruff check .")]))
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="escalate"))
    res = await loop.observe("s1", "g", "out", level=3)
    assert res["results"][0]["outcome"] == "card"  # escalate → a human decides, never auto


# ── auto disposition (level 2: safe → auto) executes through the toolbelt ───────────────────────────
async def test_observe_auto_executes_safe_action_at_level_2(tmp_path):
    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(ws)))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="echo hi", kind="shell")]))
    tb = _FakeToolbelt(workspace=ws)
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="pass"), toolbelt=tb)

    res = await loop.observe("s1", "g", "out", level=2)
    r0 = res["results"][0]
    assert r0["outcome"] == "auto" and r0["exec"]["executed"] is True
    # the toolbelt actually ran the command, with approved=True (it cleared the dial)
    assert tb.calls == [{"command": "echo hi", "approved": True}]
    action = store.get_action(r0["action_id"])
    assert action.status == "executed" and action.executed_at
    # a checkpoint was taken BEFORE execution (so the step is reversible)
    assert action.checkpoint_id and store.get_checkpoint(action.checkpoint_id).vcs_ref


# ── the irreversible red line: requires-approval never auto, even at level 3 ────────────────────────
async def test_irreversible_command_never_auto_runs_at_any_level(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="git push origin main",
                                                      reversible=False)]))
    tb = _FakeToolbelt()
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="pass"), toolbelt=tb)
    res = await loop.observe("s1", "g", "out", level=3)  # boldest dial
    assert res["results"][0]["outcome"] == "card"  # still asked, never auto
    assert tb.calls == []  # nothing ran


# ── defence-in-depth: the Operator's reversible=False hint forces a card even if the Gate misses it ─
async def test_operator_irreversible_hint_forces_card_even_when_gate_says_safe(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    # a command the Gate classifies SAFE (no denylist match), but the Operator flagged irreversible.
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="custom-irreversible-tool run",
                                                      reversible=False)]))
    tb = _FakeToolbelt()
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="pass"), toolbelt=tb)
    res = await loop.observe("s1", "g", "out", level=3)  # boldest dial
    assert res["results"][0]["outcome"] == "card"  # reversible=False can only tighten → still asked
    assert tb.calls == []


# ── non-shell kinds are never mis-run as a shell command ────────────────────────────────────────────
async def test_file_edit_kind_is_deferred_not_run_as_shell(tmp_path):
    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(ws)))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="patch auth.py", kind="file_edit")]))
    tb = _FakeToolbelt(workspace=ws)
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="pass"), toolbelt=tb)
    carded = (await loop.observe("s1", "g", "out", level=1))["results"][0]
    out = await loop.execute_action(carded["action_id"])
    assert out["executed"] is False and out["result"]["execution_deferred"] is True
    assert tb.calls == []  # a file_edit was NOT funneled through run_shell


# ── you tap approve on a card → checkpoint → execute (the §6.2 ④⑤ half) ─────────────────────────────
async def test_card_approve_checkpoints_and_executes(tmp_path):
    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(ws)))
    bus = EventBus()
    q = bus.subscribe_queue()
    op = _FakeOperator(_OpResult(summary="reformat", proposals=[_Proposal(command="fmt .")]))
    tb = _FakeToolbelt(workspace=ws)
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="pass"), toolbelt=tb, bus=bus)

    carded = (await loop.observe("s1", "g", "out", level=1))["results"][0]
    card_id = carded["card"]["id"]
    # the human taps Approve through the CardService (the route the phone hits)
    out = await cards.record_choice(card_id, "approve")
    assert out["chosen"] == "approve"
    assert out["execution"]["executed"] is True
    # it really ran + checkpointed
    assert tb.calls and tb.calls[0]["approved"] is True
    action = store.get_action(carded["action_id"])
    assert action.status == "executed" and action.checkpoint_id
    # events: a card_decided (not deferred) and an action_executed on the bus
    seen = []
    while not q.empty():
        seen.append(q.get_nowait().type)
    assert "card_decided" in seen and "action_executed" in seen


async def test_card_decided_event_not_deferred_when_executor_runs(tmp_path):
    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(ws)))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="fmt .")]))
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="pass"),
                        toolbelt=_FakeToolbelt(workspace=ws))
    carded = (await loop.observe("s1", "g", "out"))["results"][0]
    await cards.record_choice(carded["card"]["id"], "approve")
    import json
    decided = next(e for e in store.get_events("s1") if e.type == "card_decided")
    payload = json.loads(decided.payload_json)
    assert payload["execution_deferred"] is False and payload["executed"] is True


# ── undo restores the worktree to the action's pre-execution checkpoint (§6.5②) ─────────────────────
async def test_card_undo_restores_worktree(tmp_path):
    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(ws)))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="write made.txt")]))
    tb = _FakeToolbelt(workspace=ws)
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="pass"), toolbelt=tb)

    carded = (await loop.observe("s1", "g", "out"))["results"][0]
    card_id = carded["card"]["id"]
    await cards.record_choice(card_id, "approve")
    assert (ws / "made.txt").exists()  # execution created the file (after the checkpoint)

    # tap ⛔ undo on the same card → restore the worktree to the action's pre-execution checkpoint
    undo = await cards.record_choice(card_id, "undo")
    assert undo["execution"]["ok"] is True and undo["execution"]["executed"] is True
    assert not (ws / "made.txt").exists()  # undo rolled back to the pre-execution checkpoint
    assert store.get_action(carded["action_id"]).status == "undone"


async def test_undo_redo_ref_not_clobbered_by_next_step(tmp_path):
    """Regression: the redo snapshot undo_to chains off git refs must not be overwritten by a later
    DB-derived step index (DB and git step indices must stay in lockstep)."""
    from foreman.client.core.checkpoint import CheckpointManager

    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(ws)))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="step one")]))
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor("pass"),
                        toolbelt=_FakeToolbelt(workspace=ws))
    carded = (await loop.observe("s1", "g", "out"))["results"][0]
    cid = carded["card"]["id"]
    await cards.record_choice(cid, "approve")  # checkpoint at git step 0, then execute
    undo = await cards.record_choice(cid, "undo")  # undo_to → redo snapshot at git step 1
    redo_ref = undo["execution"]["redo_ref"]
    assert redo_ref

    mgr = CheckpointManager(ws)
    assert mgr.resolve_step("s1", 1) == redo_ref  # redo ref still intact (not overwritten)
    assert mgr.next_step("s1") == 2  # the next checkpoint lands AFTER the redo, no clobber


async def test_manual_option_does_not_auto_execute(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=""))
    op = _FakeOperator(_OpResult(proposals=[_Proposal(command="x")]))
    tb = _FakeToolbelt()
    loop, cards = _loop(store, operator=op, auditor=_FakeAuditor(verdict="pass"), toolbelt=tb)
    carded = (await loop.observe("s1", "g", "out"))["results"][0]
    out = await cards.record_choice(carded["card"]["id"], "manual")
    assert out["execution"] == {"ok": True, "executed": False, "outcome": "manual"}
    assert tb.calls == []
