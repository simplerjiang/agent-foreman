"""P2 phase-acceptance integration test (ROADMAP P2 "Done when…", docs/TASKS.md P2 验收).

验收: 任务完成自动产出评审；任一步可一键回退；卡死/崩溃能被发现并恢复或升级。

The P2 unit suites cover each block in isolation. This test ties them into ONE end-to-end flow over
*real* infrastructure — a real git workspace, a real client Store (SQLite), a real EventBus, a real
ProgressTracker — to prove the phase milestone *coheres*, not just that each part passes alone:

  1. 任务完成自动产出评审  — at task completion, CheckpointManager.diff(pre-step ckpt) → Reviewer.review
                            → a structured verdict, persisted + published as a `review` event.
  2. 任一步可一键回退      — the same checkpoint is one-click revertible: undo_to() restores the worktree
                            byte-for-byte (modified reverted, new deleted, deleted recreated) and leaves
                            a redo point, so the Reviewer's escalate→[⛔撤掉重来] path actually works.
  3. 卡死/崩溃能被发现并恢复或升级 — the single global Supervisor's cheap poll detects a stalled and a
                            crashed agent, plans recovery, and (on repeated crashes) escalates a card —
                            consulting the LLM only on suspicion, never on a healthy agent.

No network, no tokens: the Reviewer's LLM is an httpx.MockTransport; the watchdog runs its cheap
deterministic poll with the judge wired but only consulted on suspicious agents.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx

from foreman.client.core.checkpoint import CheckpointManager
from foreman.client.core.reviewer import ESCALATE, Reviewer
from foreman.client.core.supervisor import Supervisor
from foreman.client.monitor.progress import ProgressTracker
from foreman.client.store.db import Store
from foreman.client.store.models import Session as DBSession
from foreman.shared.config import Config
from foreman.shared.events import EventBus, make_event
from foreman.shared.llm import LLMClient

# ── deterministic clock helpers ─────────────────────────────────────────────────────────────────

_BASE = datetime.fromisoformat("2026-06-20T00:00:00+00:00")


def _at(seconds: float) -> str:
    return (_BASE + timedelta(seconds=seconds)).isoformat()


class _Clock:
    """A mutable clock so different agents can be 'touched' at different instants in one test."""

    def __init__(self) -> None:
        self.now = _at(0)

    def __call__(self) -> str:
        return self.now


def _store(tmp_path) -> Store:
    st = Store(db_path=str(tmp_path / "foreman.db"))
    st.init()
    return st


def _drain(q: asyncio.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


def _mock_reviewer(reply: dict, *, language: str = "zh") -> Reviewer:
    """A Reviewer whose LLM returns `reply` (a verdict object) — no network, no tokens."""
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "test-model"
    cfg.secrets.llm_api_key = "secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(reply)}}]}
        )

    llm = LLMClient(cfg, transport=httpx.MockTransport(handler))
    return Reviewer(llm, language=language)


# ── 1+2: completion → auto-review → one-click undo (the escalate → 撤掉重来 path) ────────────────


async def test_completion_produces_review_then_one_click_undo(tmp_path):
    sid, tid = "sess-p2", "task-1"
    ws = tmp_path / "proj"
    store = _store(tmp_path)
    store.add_session(DBSession(id=sid, goal="add a greeting", workspace=str(ws)))

    ckpt = CheckpointManager(ws, store=store)
    ckpt.ensure_repo()

    # Original workspace, then the pre-step checkpoint (step 0) the agent will run on top of.
    (ws / "greet.py").write_text("def hi():\n    return 'hi'\n", encoding="utf-8")
    (ws / "keep.txt").write_text("important\n", encoding="utf-8")
    base = await ckpt.snapshot(sid, 0, label="before step", task_id=tid)

    # The agent does its thing: edits a file, adds a new one, deletes another.
    (ws / "greet.py").write_text("def hi():\n    return 'HALLO oops'\n", encoding="utf-8")
    (ws / "junk.py").write_text("print('debug leftover')\n", encoding="utf-8")
    (ws / "keep.txt").unlink()

    # ① 任务完成 → 自动产出评审: diff the checkpoint → live worktree, feed to the Reviewer.
    diff = ckpt.diff(base)
    assert "greet.py" in diff and "junk.py" in diff and "keep.txt" in diff

    reviewer = _mock_reviewer(
        {
            "verdict": "escalate",
            "summary": "改动偏离目标，且删除了 keep.txt",
            "risks": ["误删 keep.txt", "返回值是占位垃圾"],
            "suggestions": ["撤掉重来"],
        }
    )
    result = await reviewer.review("add a greeting", diff)
    await reviewer.llm.aclose()

    assert result.verdict == ESCALATE
    assert result.needs_human is True          # escalate always pulls a human in (§6.7 从严默认)
    assert result.risks and result.suggestions  # structured, not free text

    # Persist + publish the review like the completion hook would (persist-first, then bus).
    # Subscribe before we publish so the bus delivery is observable.
    bus = EventBus()
    bus_q_seed = bus.subscribe_queue()
    review_event = make_event(
        "review", "reviewer", sid, task_id=tid,
        payload={"verdict": result.verdict, "needs_human": result.needs_human,
                 "risks": result.risks, "summary": result.summary},
    )
    store.add_event(review_event)
    await bus.publish(review_event)

    persisted = store.get_events(sid)
    assert any(e.type == "review" for e in persisted)
    assert [e.type for e in _drain(bus_q_seed)] == ["review"]

    # ② escalate → 一键回退 (撤掉重来): undo back to the pre-step checkpoint.
    redo = await ckpt.undo_to(base, session_id=sid, task_id=tid, redo_label="撤掉重来 redo")

    # Byte-for-byte restore: edit reverted, new file gone, deleted file recreated.
    assert (ws / "greet.py").read_text(encoding="utf-8") == "def hi():\n    return 'hi'\n"
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "important\n"
    assert not (ws / "junk.py").exists()

    # The undo is itself reversible: a redo checkpoint was taken first, and the timeline now has both.
    assert redo and redo != base
    cps = store.get_checkpoints(sid)
    assert [c.step_index for c in cps] == [0, 1]   # step 0 = pre-step, step 1 = redo point


# ── 3: the single global watchdog detects stall + crash, plans recovery, escalates ───────────────


async def test_watchdog_detects_recovers_and_escalates(tmp_path):
    store = _store(tmp_path)
    bus = EventBus()
    bus_q = bus.subscribe_queue()

    clock = _Clock()
    tracker = ProgressTracker(clock=clock)

    # The ② LLM escalation seam: count calls so we can prove it fires ONLY on suspicion, never on a
    # healthy agent and never every tick. Returns None → keep the deterministic verdict (no tokens).
    judged: list[str] = []

    async def judge(rec, tail):
        judged.append(rec.key)
        return None

    # liveness seam: claude-2's process has crashed; the others are alive.
    alive = {"codex-1": True, "claude-1": True, "claude-2": False}

    def liveness(key, pid):
        return alive.get(key)

    sup = Supervisor(
        bus=bus, store=store, tracker=tracker,
        judge=judge, liveness=liveness, clock=clock, max_restarts=3,
    )
    sup.register("codex-1", session_id="s-codex", agent_type="codex", pid=11)
    sup.register("claude-1", session_id="s-claude", agent_type="claude-code", pid=22)
    sup.register("claude-2", session_id="s-claude", agent_type="claude-code", pid=33)

    # codex-1 last progressed long ago (200s ≥ codex stall 150s) → STALLED.
    clock.now = _at(0)
    tracker.touch("codex-1")
    # claude-1 just progressed (10s < idle 120s) → RUNNING (healthy).
    clock.now = _at(190)
    tracker.touch("claude-1")
    # claude-2 also "progressed" recently, but its process is dead → DEAD wins (liveness first).
    tracker.touch("claude-2")

    verdicts = {v.key: v for v in await sup.poll_once(now=_at(200))}

    # codex-1: stalled, recovery = nudge, and the LLM was consulted (suspicious).
    assert verdicts["codex-1"].state == "stalled"
    assert verdicts["codex-1"].action == "nudge"
    assert verdicts["codex-1"].suspicious and verdicts["codex-1"].escalated

    # claude-2: crashed → DEAD, recovery = restart_from_checkpoint (still has restarts left).
    assert verdicts["claude-2"].state == "dead"
    assert verdicts["claude-2"].action == "restart_from_checkpoint"

    # claude-1: healthy → running, no alarm, and the judge was NOT consulted for it.
    assert verdicts["claude-1"].state == "running"
    assert verdicts["claude-1"].action == "none"
    assert "claude-1" not in judged
    # judge fired exactly on the two suspicious agents — not every tick, not on the healthy one.
    assert sorted(judged) == ["claude-2", "codex-1"]

    # Events were persisted-first then published: health + stall + recover for the bad agents.
    types = [e.type for e in store.get_events("s-codex")]
    assert "health" in types and "stall" in types and "recover" in types
    recover = next(e for e in store.get_events("s-codex") if e.type == "recover")
    recover_payload = json.loads(recover.payload_json)
    assert recover_payload["action"] == "nudge"
    assert recover_payload["execution_deferred"] is True   # execution lands in P4

    # The bus saw the same stream (delivery, not just persistence).
    published = {e.type for e in _drain(bus_q)}
    assert {"health", "stall", "recover"} <= published

    # Repeated crashes: once consecutive deaths exceed max_restarts, recovery escalates a card.
    for _ in range(3):
        v = {x.key: x for x in await sup.poll_once(now=_at(200))}["claude-2"]
    assert v.state == "dead"
    assert v.action == "escalate_card"   # 连崩 > max_restarts → 弹卡 (升级) instead of looping restarts
