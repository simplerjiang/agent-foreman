"""Tests for the Supervisor watchdog — pool health sweep + cheap poll + LLM-on-suspicion (T2.6).

The two side-effecting seams (``liveness`` = is the process alive, ``tail_provider`` = stdout tail)
and the ``judge`` (LLM escalation) are all injected, so these tests drive scripted signals with no
real process and no tokens spent. The clock / ``now`` is injected so idle math is deterministic.
DESIGN §4.1 / §5.6.
"""

from __future__ import annotations

import json

import httpx

from foreman.client.core.supervisor import (
    DEAD,
    ERRORED,
    IDLE,
    RUNNING,
    STALLED,
    STARTING,
    WAITING_INPUT,
    LLMJudge,
    Supervisor,
    Thresholds,
    classify_tail,
    parse_judge_state,
    plan_recovery,
    redact_secrets,
)
from foreman.client.monitor.progress import ProgressTracker
from foreman.shared.config import Config
from foreman.shared.events import EventBus
from foreman.shared.llm import LLMClient

T0 = "2026-06-20T00:00:00+00:00"


def _bus_capture(bus):
    captured = []
    orig = bus.publish

    async def publish(ev):
        captured.append(ev)
        await orig(ev)

    bus.publish = publish
    return captured


def _at(seconds: float) -> str:
    """An ISO timestamp ``seconds`` after T0 (same minute window keeps it simple)."""
    mm, ss = divmod(int(seconds), 60)
    return f"2026-06-20T00:{mm:02d}:{ss:02d}+00:00"


# —— pure helpers ————————————————————————————————————————————————————————————————————————————
def test_classify_tail_detects_error_waiting_and_none():
    assert classify_tail("hit a Rate Limit, retry") == ERRORED
    assert classify_tail("HTTP 429 Too Many Requests") == ERRORED
    assert classify_tail("Do you want to continue? [y/n]") == WAITING_INPUT
    assert classify_tail("compiling module foo...") is None
    assert classify_tail("") is None
    assert classify_tail(None) is None


def test_classify_tail_error_beats_waiting():
    # A crashed run may still show an old prompt; error takes precedence.
    assert classify_tail("Continue? [y/n]\nUnauthorized: invalid api key") == ERRORED


def test_plan_recovery_maps_states():
    assert plan_recovery(DEAD) == "restart_from_checkpoint"
    assert plan_recovery(DEAD, restarts_left=False) == "escalate_card"
    assert plan_recovery(STALLED) == "nudge"
    assert plan_recovery(WAITING_INPUT) == "answer_or_card"
    assert plan_recovery(ERRORED) == "backoff_or_card"
    assert plan_recovery(RUNNING) == "none"
    assert plan_recovery(IDLE) == "none"


# —— cheap deterministic classification ————————————————————————————————————————————————————————
def _sup(**kw) -> Supervisor:
    return Supervisor(clock=lambda: T0, **kw)


def test_classify_dead_process_is_suspicious():
    sup = _sup(liveness=lambda key, pid: False)
    rec = sup.register("a", session_id="s1", agent_type="claude-code", pid=100)
    state, suspicious, _ = sup.classify(rec, now=T0)
    assert state == DEAD and suspicious is True


def test_classify_recent_progress_is_running_not_suspicious():
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")  # progressed at T0
    sup = _sup(tracker=tracker)
    rec = sup.register("a", session_id="s1", agent_type="claude-code")
    state, suspicious, _ = sup.classify(rec, now=_at(10))
    assert state == RUNNING and suspicious is False


def test_classify_yellow_idle_then_stalled():
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = _sup(tracker=tracker)  # claude-code: idle 120s, stall 300s
    rec = sup.register("a", session_id="s1", agent_type="claude-code")
    assert sup.classify(rec, now=_at(150))[0] == IDLE      # past 120, under 300
    assert sup.classify(rec, now=_at(301))[0] == STALLED   # past 300


def test_codex_thresholds_are_tighter_than_claude():
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("c")
    sup = _sup(tracker=tracker)
    codex = sup.register("c", session_id="s1", agent_type="codex")
    # At 90s codex (idle 60) is already IDLE; a claude agent (idle 120) would still be RUNNING.
    assert sup.classify(codex, now=_at(90))[0] == IDLE
    tracker.touch("k")
    claude = sup.register("k", session_id="s1", agent_type="claude-code")
    assert sup.classify(claude, now=_at(90))[0] == RUNNING


def test_classify_tail_overrides_idle():
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = _sup(tracker=tracker, tail_provider=lambda key: "Continue? [y/n]")
    rec = sup.register("a", session_id="s1", agent_type="claude-code")
    # Even though it's been idle a while, the prompt-like tail says WAITING_INPUT.
    assert sup.classify(rec, now=_at(400))[0] == WAITING_INPUT


def test_classify_no_progress_yet_is_starting():
    tracker = ProgressTracker(clock=lambda: T0)
    sup = _sup(tracker=tracker)
    rec = sup.register("a", session_id="s1", agent_type="claude-code")
    state, suspicious, _ = sup.classify(rec, now=_at(999))
    assert state == STARTING and suspicious is False


def test_custom_thresholds_respected():
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = _sup(tracker=tracker, thresholds={"claude-code": Thresholds(idle_s=5, stall_s=10)})
    rec = sup.register("a", session_id="s1", agent_type="claude-code")
    assert sup.classify(rec, now=_at(6))[0] == IDLE
    assert sup.classify(rec, now=_at(11))[0] == STALLED


# —— sweep: events, escalation, robustness ————————————————————————————————————————————————————
async def test_sweep_emits_health_stall_recover_on_bad_transition():
    bus = EventBus()
    cap = _bus_capture(bus)
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = Supervisor(bus=bus, tracker=tracker, clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="claude-code")

    verdicts = await sup.poll_once(now=_at(400))  # stalled
    assert verdicts[0].state == STALLED
    types = [e.type for e in cap]
    assert types == ["health", "stall", "recover"]
    assert cap[2].payload["action"] == "nudge"
    assert cap[2].payload["execution_deferred"] is True
    assert all(e.source == "supervisor" and e.session_id == "s1" for e in cap)


async def test_no_event_while_state_persists():
    bus = EventBus()
    cap = _bus_capture(bus)
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = Supervisor(bus=bus, tracker=tracker, clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="claude-code")

    await sup.poll_once(now=_at(400))   # → STALLED (emits)
    cap.clear()
    await sup.poll_once(now=_at(420))   # still STALLED → quiet
    assert cap == []


async def test_running_transition_emits_health_only_no_recover():
    bus = EventBus()
    cap = _bus_capture(bus)
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = Supervisor(bus=bus, tracker=tracker, clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="claude-code")
    # STARTING (default) → RUNNING is a benign transition: health only, no stall/recover.
    await sup.poll_once(now=_at(5))
    assert [e.type for e in cap] == ["health"]


async def test_judge_only_called_on_suspicion_and_can_override():
    calls = []

    async def judge(rec, tail):
        calls.append((rec.key, tail))
        return RUNNING  # the LLM says it's actually fine, downgrade

    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = Supervisor(tracker=tracker, judge=judge, tail_provider=lambda k: "tail-x", clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="claude-code")

    # Not suspicious → judge untouched.
    v = (await sup.poll_once(now=_at(10)))[0]
    assert v.escalated is False and calls == []

    # Suspicious (stalled) → judge consulted, and its RUNNING verdict wins.
    v = (await sup.poll_once(now=_at(400)))[0]
    assert v.escalated is True
    assert calls == [("a", "tail-x")]
    assert v.state == RUNNING


async def test_judge_cannot_push_to_dead_or_done():
    # A misbehaving judge must not be able to fake a crash / retire an agent: only the 4 allowed
    # refinement states win; anything else leaves the deterministic verdict intact.
    async def rogue_judge(rec, tail):
        return DEAD  # not in _JUDGE_ALLOWED

    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = Supervisor(tracker=tracker, judge=rogue_judge, clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="claude-code")
    v = (await sup.poll_once(now=_at(400)))[0]  # cheap poll says STALLED
    assert v.escalated is True
    assert v.state == STALLED          # rogue DEAD ignored
    assert sup.pool["a"].fail_count == 0  # no crash counted


async def test_one_agent_error_does_not_abort_sweep():
    def liveness(key, pid):
        if key == "boom":
            raise RuntimeError("psutil blew up")
        return True

    bus = EventBus()
    cap = _bus_capture(bus)
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("ok")
    sup = Supervisor(bus=bus, tracker=tracker, liveness=liveness, clock=lambda: T0)
    sup.register("boom", session_id="s1", agent_type="codex", pid=1)
    sup.register("ok", session_id="s2", agent_type="claude-code", pid=2)

    verdicts = await sup.poll_once(now=_at(10))
    # The healthy agent still got a verdict; the broken one logged an error event.
    assert any(v.key == "ok" for v in verdicts)
    err = [e for e in cap if e.type == "error"]
    assert len(err) == 1 and err[0].payload["key"] == "boom"
    assert "psutil blew up" in err[0].payload["error"]


async def test_repeated_crashes_escalate_card_after_max_restarts():
    sup = Supervisor(liveness=lambda k, p: False, max_restarts=2, clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="codex", pid=1)
    actions = []
    for _ in range(4):
        v = (await sup.poll_once(now=_at(1)))[0]
        actions.append(v.action)
        sup.pool["a"].state = RUNNING  # force a fresh transition so each tick re-emits/decides
    # First two deaths restart; once fail_count exceeds max_restarts (2) it escalates a card.
    assert actions[0] == "restart_from_checkpoint"
    assert actions[-1] == "escalate_card"


async def test_persist_first_then_publish(tmp_path):
    from foreman.client.store.db import Store  # client store

    db = tmp_path / "f.db"
    store = Store(str(db))
    store.init()
    bus = EventBus()
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = Supervisor(bus=bus, store=store, tracker=tracker, clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="claude-code")

    await sup.poll_once(now=_at(400))
    rows = store.get_events("s1")
    assert any(e.type == "stall" for e in rows)  # written to the store, not just the bus


async def test_mark_done_and_unregister_skip_agent():
    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = Supervisor(tracker=tracker, clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="claude-code")
    sup.mark_done("a")
    assert (await sup.poll_once(now=_at(400))) == []  # done → skipped
    sup.unregister("a")
    assert "a" not in sup.pool


async def test_watch_loops_until_cancelled():
    import asyncio

    tracker = ProgressTracker(clock=lambda: T0)
    tracker.touch("a")
    sup = Supervisor(tracker=tracker, clock=lambda: T0)
    sup.register("a", session_id="s1", agent_type="claude-code")
    task = asyncio.create_task(sup.watch(interval=0.001))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert sup.pool["a"].state in {STARTING, RUNNING, IDLE, STALLED}


# —— LLM judge (mock transport; live key hookup deferred) ————————————————————————————————————
def test_parse_judge_state_json_and_fallback():
    assert parse_judge_state('{"state": "stalled"}') == STALLED
    assert parse_judge_state('  {"state":"WAITING_INPUT"}  ') == WAITING_INPUT
    assert parse_judge_state("I think it is errored honestly") == ERRORED
    assert parse_judge_state("waiting_input vs stalled") == WAITING_INPUT  # specific wins
    assert parse_judge_state("nonsense") is None
    assert parse_judge_state("") is None


def test_redact_secrets_masks_common_credential_shapes():
    assert "[REDACTED]" in redact_secrets("OPENAI_API_KEY=sk-abcd1234efgh5678")
    assert "[REDACTED]" in redact_secrets("Authorization: Bearer aBcD1234EfGh")
    assert "[REDACTED]" in redact_secrets("token ghp_0123456789ABCDEFabcdef")
    assert "[REDACTED]" in redact_secrets("password: hunter2pass")
    # leaves ordinary output untouched
    assert redact_secrets("compiling module foo") == "compiling module foo"
    assert redact_secrets(None) == ""


async def test_llm_judge_redacts_tail_before_egress():
    cap = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"state":"running"}'}}]})

    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.secrets.llm_api_key = "k"
    llm = LLMClient(cfg, transport=httpx.MockTransport(handler))
    sup = Supervisor(clock=lambda: T0)
    rec = sup.register("a", session_id="s1", agent_type="codex")
    await LLMJudge(llm=llm, language="en")(rec, "leaked sk-deadbeef12345678 in logs")
    await llm.aclose()

    user = next(m["content"] for m in cap["json"]["messages"] if m["role"] == "user")
    assert "sk-deadbeef12345678" not in user and "[REDACTED]" in user


async def test_llm_judge_builds_prompt_with_language_directive_and_parses():
    cap = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"state":"stalled"}'}}]})

    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.secrets.llm_api_key = "k"
    llm = LLMClient(cfg, transport=httpx.MockTransport(handler))
    judge = LLMJudge(llm=llm, language="zh")

    sup = Supervisor(clock=lambda: T0)
    rec = sup.register("a", session_id="s1", agent_type="codex")
    out = await judge(rec, "some output tail")
    await llm.aclose()

    assert out == STALLED
    msgs = cap["json"]["messages"]
    system = next(m["content"] for m in msgs if m["role"] == "system")
    assert "中文" in system  # DESIGN §15 language_directive appended
    user = next(m["content"] for m in msgs if m["role"] == "user")
    assert "codex" in user and "some output tail" in user
    assert cap["json"]["response_format"] == {"type": "json_object"}
