"""P4 phase-acceptance integration test (ROADMAP P4 "Done when…", docs/TASKS.md P4 验收).

验收: 一步走完 Operator→Auditor→卡→你点→检查点→执行；全程手机可操作.

The P4 unit suites cover each block alone (Operator T4.1, Auditor T4.2, cards T4.3, dial T4.4,
Toolbelt T4.5, dispatch/briefing T4.6, decision loop). This test ties them into the one end-to-end
flow the milestone describes, over *real* infrastructure — a real client Store (SQLite), a real
EventBus, a **real git workspace**, the real FastAPI app via TestClient (the surface the phone hits),
the real Toolbelt (only its OS shell is faked), and the real CheckpointManager — to prove the phase
coheres, not just that each part passes in isolation:

  1. 一步走完 + 全程手机可操作 — an Operator proposal is audited (pass), the autonomy dial (level 1
     "凡事都问") surfaces a Decision Card, the phone reads it off `GET /api/cards`, taps Approve through
     the real `POST /api/cards/{id}/choose` route → a checkpoint is taken, the command actually runs
     (worktree changes), the action is recorded executed, and `GET /api/actions/{id}/detail` shows the
     per-line diff the execution produced (the "检查点→执行" bracket, inspectable from the phone).
  2. 两向控制 (the P2–P4 deferred "执行层") now real — an `agent_instruction` action drives the live
     agent through `Runner.send` (resume), and the Auditor's `revise` verdict sends notes back to it.
  3. 不可逆红线 — a `git push` proposal at the boldest dial (level 3) still cards, never auto-runs.

LLM roles (Operator/Auditor) are faked (their live path needs the user's own key; T4.1/T4.2 mock-test
the real LLM wiring). No network, no tokens, no live CLI/desktop: the genuinely-live hookups (a real
claude/codex resume, a real desktop shell) are the only deferred bits, consistent with P2–P4.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from foreman.client.computer_use.toolbelt import Toolbelt
from foreman.client.core.cards import CardService
from foreman.client.core.decision_loop import DecisionLoop
from foreman.client.core.gate import Gate
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.server.app import create_app
from foreman.shared.config import GatesCfg, load_config
from foreman.shared.events import EventBus


# ── fakes for the two LLM roles (their live path is mock-tested in T4.1/T4.2) ──────────────────────
@dataclass
class _Proposal:
    command: str
    kind: str = "shell"
    rationale: str = ""
    expected_effect: str = ""
    reversible: bool = True


@dataclass
class _OpResult:
    summary: str = ""
    state: str = "running"
    proposals: list = field(default_factory=list)


class _FakeOperator:
    def __init__(self, result):
        self._result = result

    async def observe(self, goal, agent_output, *, context="", recent_actions=""):
        return self._result


@dataclass
class _AuditOut:
    verdict: str
    goal_quality: str = "on-track"
    risk_severity: str = "none"
    reasons: list = field(default_factory=lambda: ["advances the step"])
    suggestions: list = field(default_factory=list)
    model: str = "fake-model"


class _FakeAuditor:
    def __init__(self, verdict="pass", **kw):
        self._out = _AuditOut(verdict=verdict, **kw)

    async def audit(self, command, **kw):
        return self._out


class _FileWritingShell:
    """Real Toolbelt, faked OS layer: runs the command by writing a file into the git workspace."""

    def __init__(self, workspace):
        self.ws = workspace
        self.ran: list = []

    def run(self, command, *, admin=False):
        self.ran.append(command)
        (self.ws / "feature.py").write_text(f"# {command}\n", encoding="utf-8")
        return {"returncode": 0, "stdout": "ok", "stderr": ""}


class _RecordingRunner:
    """Stands in for the live Runner — records send/interrupt instead of driving a real CLI."""

    def __init__(self):
        self.sent: list = []
        self._handle = object()

    def handle_for_session(self, session_id):
        return self._handle

    async def send(self, handle, text):
        self.sent.append(text)

    async def interrupt(self, handle):
        pass


def _git_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init"], cwd=str(ws), capture_output=True, text=True, check=True)
    (ws / "seed.py").write_text("print('seed')\n", encoding="utf-8")
    return ws


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "foreman.db"))
    s.init()
    return s


def _wire(store, ws, *, operator, auditor, runner=None):
    """The local decision loop + its CardService executor + the FastAPI app the phone hits."""
    bus = EventBus()
    gate = Gate(GatesCfg(requires_approval=["git push", "rm -rf"], needs_strategy=["pip install"]))
    cards = CardService(store, bus=bus)
    toolbelt = Toolbelt(shell=_FileWritingShell(ws), gate=gate)
    loop = DecisionLoop(
        store=store, gate=gate, cards=cards, operator=operator, auditor=auditor,
        bus=bus, runner=runner, toolbelt=toolbelt,
    )
    cards.executor = loop.on_card_decision
    app = create_app(load_config(), store, bus, gate=gate, cards=cards)
    return loop, cards, bus, TestClient(app)


# ── 1: one step Operator→Auditor→card→tap→checkpoint→execute, all phone-operable ───────────────────
def test_one_step_walks_the_whole_pipeline_phone_operable(tmp_path):
    import asyncio

    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="ship the feature", workspace=str(ws)))
    op = _FakeOperator(_OpResult(
        summary="run the formatter", proposals=[_Proposal(command="black .", reversible=True)]
    ))
    loop, cards, bus, client = _wire(store, ws, operator=op, auditor=_FakeAuditor("pass"))

    # ① Operator → Auditor → Gate/dial(level 1) → a Decision Card (nothing has run yet).
    res = asyncio.run(loop.observe("s1", "ship the feature", "agent finished editing", level=1))
    r0 = res["results"][0]
    assert r0["outcome"] == "card"
    action_id = r0["action_id"]
    assert store.get_action(action_id).status == "carded"
    assert not (ws / "feature.py").exists()  # not executed before approval

    # ② 手机看卡: the phone reads its card feed through the real REST route.
    feed = client.get("/api/cards").json()
    assert len(feed) == 1 and feed[0]["action_id"] == action_id
    card_id = feed[0]["id"]
    assert {o["action"] for o in feed[0]["options"]} == {"approve", "revise", "undo", "manual"}

    # ③ 你点 → 检查点 → 执行: tap Approve through the REAL route the PWA uses.
    r = client.post(f"/api/cards/{card_id}/choose", json={"option": "approve"})
    assert r.status_code == 200 and r.json()["chosen"] == "approve"
    assert r.json()["execution"]["executed"] is True

    # the command really ran (worktree changed) AFTER a checkpoint was taken (so it's reversible).
    assert (ws / "feature.py").read_text(encoding="utf-8") == "# black .\n"
    action = store.get_action(action_id)
    assert action.status == "executed" and action.executed_at and action.checkpoint_id
    assert store.get_checkpoint(action.checkpoint_id).vcs_ref  # a real git snapshot

    # ④ 手机可下钻: the step detail shows the per-line diff the execution produced (checkpoint→worktree).
    detail = client.get(f"/api/actions/{action_id}/detail").json()
    assert detail["diff"]["summary"]["files"] == 1
    assert detail["diff"]["files"][0]["path"] == "feature.py"

    # the decision + execution were recorded as events (persist-first), no longer deferred.
    decided = next(e for e in store.get_events("s1") if e.type == "card_decided")
    assert json.loads(decided.payload_json)["execution_deferred"] is False
    assert any(e.type == "action_executed" for e in store.get_events("s1"))


# ── 2: two-way control (the P2–P4 deferred 执行层) is now real ───────────────────────────────────────
def test_two_way_control_drives_the_live_agent(tmp_path):
    import asyncio

    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(ws)))
    runner = _RecordingRunner()

    # an agent_instruction action, approved, drives the live agent via Runner.send (resume).
    op = _FakeOperator(_OpResult(proposals=[
        _Proposal(command="add a test for the edge case", kind="agent_instruction")
    ]))
    loop, cards, _, client = _wire(store, ws, operator=op, auditor=_FakeAuditor("pass"), runner=runner)
    carded = asyncio.run(loop.observe("s1", "g", "out", level=1))["results"][0]
    client.post(f"/api/cards/{carded['card']['id']}/choose", json={"option": "approve"})
    assert runner.sent == ["add a test for the edge case"]  # sent back to the agent (two-way)

    # the Auditor's `revise` verdict bounces notes back to the agent and surfaces no card.
    op2 = _FakeOperator(_OpResult(proposals=[_Proposal(command="rewrite everything")]))
    aud = _FakeAuditor("revise", goal_quality="weak", suggestions=["narrow the scope"])
    loop2 = DecisionLoop(
        store=store, gate=loop.gate, cards=cards, operator=op2, auditor=aud, runner=runner,
        toolbelt=loop.toolbelt,
    )
    res = asyncio.run(loop2.observe("s1", "g", "out"))
    assert res["results"][0]["outcome"] == "revise"
    assert runner.sent[-1] == "narrow the scope"  # the revise notes went back to the agent


# ── 3: the irreversible red line holds through the loop (git push never auto, even at level 3) ─────
def test_irreversible_push_still_cards_at_boldest_dial(tmp_path):
    import asyncio

    store = _store(tmp_path)
    ws = _git_workspace(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace=str(ws)))
    op = _FakeOperator(_OpResult(proposals=[
        _Proposal(command="git push origin main", reversible=False)
    ]))
    loop, cards, _, client = _wire(store, ws, operator=op, auditor=_FakeAuditor("pass"))
    res = asyncio.run(loop.observe("s1", "g", "out", level=3))  # boldest autonomy
    assert res["results"][0]["outcome"] == "card"  # asked, never auto
    # nothing executed: no executed action, the worktree is untouched
    assert store.get_action(res["results"][0]["action_id"]).status == "carded"
    assert not any(e.type == "action_executed" for e in store.get_events("s1"))
