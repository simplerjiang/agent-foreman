"""Supervisor / Watchdog — the single global health sweep over the whole agents pool.

DESIGN §4.1 / §5.6. Long-running CLIs (**especially Codex**, which has no hook signals) stall or
die silently and don't self-recover, so one watchdog must watch them. **There is exactly one
Supervisor** per local PM Core: a single coroutine that sweeps *every* agent in the pool each tick
(not one-per-session, not one-per-agent) — a central loop is simpler, spends fewer tokens, and is the
only vantage point that can spot *systemic* trouble (e.g. "several agents stalled at once").

Two-layer detection, "deterministic first, LLM only as backstop":

  ① **Cheap deterministic poll** (every 10–30s, no tokens): is the process alive? has
     ``last_progress_at`` gone stale past the threshold? does the stdout tail look like it's waiting
     for input or reporting an error? This is ``classify()`` — pure, side-effect-free.

  ② **LLM judgment** (only when ① flags a *suspicious* (yellow) agent): hand the output tail to a
     ``judge`` seam to decide still-working / waiting-input / truly-stalled / errored. **Never called
     every tick** — only on suspicion — to keep token cost down.

On a confirmed bad state the watchdog names a **recovery playbook** step (DESIGN §4.1 table). NOTE:
this task (T2.6) implements *detection + classification + planning + escalation events*; the actual
execution of nudge / interrupt+resume / restart-from-checkpoint is **deferred to P4** (the decision
loop owns the Runner two-way control — Runner.send is a P4 stub, interrupt a P3 stub, and §6 gating
lives there). So the Supervisor emits a ``recover`` event naming the planned step (and, when restarts
are exhausted or input is needed, escalates via a card-style event) but does not itself mutate agents.

Robustness (DESIGN §4.1 "单点要稳"): one agent's check throwing only records one ``error`` event and
never aborts the rest of the sweep. The two side-effecting seams — ``liveness`` (process alive?) and
``tail_provider`` (recent stdout) — are injected so tests need no real process; left unset, the
Supervisor falls back to pure ``last_progress_at`` idle detection. The clock is injected too.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from foreman.shared.events import make_event, utc_now_iso
from foreman.shared.i18n import language_directive

# —— Health states (DESIGN §4.1: agent_state ∈ {...}) ——————————————————————————————————————
STARTING = "starting"
RUNNING = "running"
IDLE = "idle"
WAITING_INPUT = "waiting_input"
STALLED = "stalled"
ERRORED = "errored"
DEAD = "dead"
DONE = "done"

HEALTH_STATES: frozenset[str] = frozenset(
    {STARTING, RUNNING, IDLE, WAITING_INPUT, STALLED, ERRORED, DEAD, DONE}
)

# States that warrant an alarm (a ``stall`` event when newly entered). WAITING_INPUT is expected, not
# bad; DONE/STARTING/RUNNING/IDLE are not alarms (IDLE is only a yellow flag → LLM, not an alarm).
BAD_STATES: frozenset[str] = frozenset({STALLED, ERRORED, DEAD})

# States the cheap poll treats as "suspicious" (yellow) → eligible for the ② LLM escalation.
SUSPICIOUS_STATES: frozenset[str] = frozenset({IDLE, WAITING_INPUT, STALLED, ERRORED, DEAD})

# The only states the ② LLM escalation is allowed to refine an agent into. The judge *clarifies a
# suspicion* — it must never be able to push an agent to DEAD/DONE/STARTING (which would fake a crash,
# silently retire it, or reset it). The Supervisor enforces this even for a custom ``judge`` seam.
_JUDGE_ALLOWED: frozenset[str] = frozenset({RUNNING, WAITING_INPUT, STALLED, ERRORED})


@dataclass
class Thresholds:
    """Idle thresholds (seconds) for the cheap poll. Yellow at ``idle_s``, red (stalled) at ``stall_s``."""

    idle_s: float   # no progress this long → IDLE (suspicious; ask the LLM)
    stall_s: float  # no progress this long → STALLED (confirmed-stuck candidate)


# Per-agent-type thresholds. Codex has no hooks and stalls more on long runs (DESIGN §4.1 "阈值按
# agent 类型分设，Codex 调更紧"), so its lines are tighter than Claude Code's.
DEFAULT_THRESHOLDS: dict[str, Thresholds] = {
    "claude-code": Thresholds(idle_s=120.0, stall_s=300.0),
    "codex": Thresholds(idle_s=60.0, stall_s=150.0),
}
FALLBACK_THRESHOLDS = Thresholds(idle_s=120.0, stall_s=300.0)

# How many consecutive crashes before we stop auto-restarting and escalate a card instead
# (DESIGN §4.1: "连崩 N 次 → 弹卡问你").
DEFAULT_MAX_RESTARTS = 3

# DESIGN §4.1 cheap poll cadence: 10–30s. The slow-changing signals (liveness / idle) tolerate the
# slower end; callers may override per deployment.
DEFAULT_INTERVAL_S = 15.0

# —— Cheap stdout-tail classification (deterministic, no token) ————————————————————————————————
# Substrings that suggest the agent is blocked waiting for the user, or has hit an error / rate
# limit / auth failure. Lowercased substring match — same cheap discipline as Gate.classify.
_WAITING_MARKERS = (
    "waiting for input",
    "press enter",
    "[y/n]",
    "(y/n)",
    "do you want to",
    "continue?",
    "are you sure",
    "?\nhuman:",  # claude-style prompt echo
)
_ERROR_MARKERS = (
    "rate limit",
    "rate_limit",
    "429",
    "too many requests",
    "quota",
    "unauthorized",
    "401",
    "authentication",
    "invalid api key",
    "login expired",
    "traceback (most recent call last)",
    "fatal error",
    "panic:",
)


def classify_tail(tail: str | None) -> str | None:
    """Map a stdout tail to WAITING_INPUT / ERRORED, or None when nothing stands out.

    Errors take precedence over waiting prompts (a crashed run may still show an old prompt).
    """
    if not tail:
        return None
    low = tail.lower()
    if any(m in low for m in _ERROR_MARKERS):
        return ERRORED
    if any(m in low for m in _WAITING_MARKERS):
        return WAITING_INPUT
    return None


# —— Recovery playbook (DESIGN §4.1 table) ————————————————————————————————————————————————————
# Maps a confirmed state to the named recovery step. Execution is deferred to P4 (see module docs);
# the Supervisor only emits the plan. "escalate_card" means push to the phone for a human decision.
def plan_recovery(state: str, *, restarts_left: bool = True) -> str:
    """Name the recovery step for ``state`` (or ``"none"`` when nothing is needed)."""
    if state == DEAD:
        # Crashed → restart from the last checkpoint, unless we've already retried too many times.
        return "restart_from_checkpoint" if restarts_left else "escalate_card"
    if state == STALLED:
        return "nudge"               # nudge → (P4) interrupt+resume → restart-from-checkpoint
    if state == WAITING_INPUT:
        return "answer_or_card"      # auto-answer if we can, else push a decision card
    if state == ERRORED:
        return "backoff_or_card"     # transient → backoff retry; auth/quota → push a card
    return "none"


@dataclass
class AgentRecord:
    """One agent's slot in the pool: identity + per-type thresholds + sticky health bookkeeping."""

    key: str            # pool key (the agent handle id)
    session_id: str
    agent_type: str     # "claude-code" | "codex"
    pid: int | None = None
    state: str = STARTING
    fail_count: int = 0  # consecutive DEAD observations (drives the restart-vs-card decision)
    task_id: str | None = None


@dataclass
class HealthVerdict:
    """The per-agent outcome of one sweep tick — what the Supervisor decided and why."""

    key: str
    session_id: str
    state: str
    prev_state: str
    suspicious: bool        # the cheap poll flagged it yellow (escalation eligible)
    escalated: bool         # the LLM judge was actually consulted this tick
    action: str             # recovery playbook step, or "none"
    detail: str = ""        # short human-readable reason


# A judge takes (record, tail) and returns a refined state from HEALTH_STATES, or None to keep the
# deterministic verdict. Async so a real implementation can call the LLM.
Judge = Callable[[AgentRecord, "str | None"], Awaitable["str | None"]]
# liveness(key, pid) → True alive / False dead / None unknown-this-tick. tail_provider(key) → tail.
Liveness = Callable[[str, "int | None"], "bool | None"]
TailProvider = Callable[[str], "str | None"]


class Supervisor:
    """The one global watchdog. Register agents, then call ``poll_once`` on a timer (or ``watch``)."""

    def __init__(
        self,
        *,
        bus=None,
        store=None,
        tracker=None,
        judge: Judge | None = None,
        liveness: Liveness | None = None,
        tail_provider: TailProvider | None = None,
        thresholds: dict[str, Thresholds] | None = None,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.bus = bus                  # optional EventBus: publish health/stall/recover/error
        self.store = store              # optional client Store: persist events (persist-first)
        self.tracker = tracker          # ProgressTracker: last_progress_at source for idle detection
        self.judge = judge              # ② LLM escalation seam (only called on suspicion)
        self._liveness = liveness       # ① process-alive seam (unset → liveness not checked)
        self._tail = tail_provider      # ① stdout-tail seam (unset → tail not classified)
        self._thresholds = thresholds or DEFAULT_THRESHOLDS
        self._max_restarts = max_restarts
        self._clock = clock
        self.pool: dict[str, AgentRecord] = {}

    # —— pool membership ————————————————————————————————————————————————————————————————————
    def register(
        self,
        key: str,
        *,
        session_id: str,
        agent_type: str,
        pid: int | None = None,
        task_id: str | None = None,
    ) -> AgentRecord:
        """Add (or replace) an agent in the pool. Idempotent on ``key``."""
        rec = AgentRecord(
            key=key, session_id=session_id, agent_type=agent_type, pid=pid, task_id=task_id
        )
        self.pool[key] = rec
        return rec

    def set_pid(self, key: str, pid: int | None) -> None:
        """Update an agent's pid once its process is known (no-op if unregistered)."""
        if (rec := self.pool.get(key)) is not None:
            rec.pid = pid

    def mark_done(self, key: str) -> None:
        """Mark an agent finished so the sweep skips it (no-op if unregistered)."""
        if (rec := self.pool.get(key)) is not None:
            rec.state = DONE

    def unregister(self, key: str) -> None:
        """Drop an agent from the pool (e.g. once fully stopped). No-op if unknown."""
        self.pool.pop(key, None)

    def _thresholds_for(self, agent_type: str) -> Thresholds:
        return self._thresholds.get(agent_type, FALLBACK_THRESHOLDS)

    # —— ① cheap deterministic classification (pure, no token) ————————————————————————————————
    def _read_tail(self, rec: AgentRecord) -> str | None:
        return self._tail(rec.key) if self._tail is not None else None

    def classify(self, rec: AgentRecord, now: str | None = None) -> tuple[str, bool, str]:
        """Decide a provisional ``(state, suspicious, detail)`` from cheap signals only.

        Precedence: dead process > stdout error > stdout waiting-prompt > stalled-idle > yellow-idle
        > running. ``suspicious`` marks states eligible for the ② LLM escalation.
        """
        return self._classify(rec, now, self._read_tail(rec))

    def _classify(
        self, rec: AgentRecord, now: str | None, tail: str | None
    ) -> tuple[str, bool, str]:
        """``classify`` against an already-read ``tail`` (so a tick reads the tail at most once)."""
        if rec.state == DONE:
            return DONE, False, "done"

        # Process liveness first — a dead process is unambiguous and the most urgent.
        if self._liveness is not None:
            alive = self._liveness(rec.key, rec.pid)
            if alive is False:
                return DEAD, True, "process exited"
            # alive True / None (unknown this tick) → fall through to softer signals.

        # Stdout tail signals (errors/prompts) are more specific than a bare idle timeout.
        tail_state = classify_tail(tail)
        if tail_state == ERRORED:
            return ERRORED, True, "output looks like an error/rate-limit/auth failure"
        if tail_state == WAITING_INPUT:
            return WAITING_INPUT, True, "output looks like it is waiting for input"

        # Idle thresholds from last_progress_at.
        if self.tracker is None:
            return RUNNING, False, "no tracker"
        idle = self.tracker.idle_seconds(rec.key, now)
        if idle is None:
            # Never made progress yet — still starting up; give it grace (not suspicious).
            return STARTING, False, "no progress signal yet"
        th = self._thresholds_for(rec.agent_type)
        if idle >= th.stall_s:
            return STALLED, True, f"no progress for {idle:.0f}s (>= {th.stall_s:.0f}s)"
        if idle >= th.idle_s:
            return IDLE, True, f"idle {idle:.0f}s (>= {th.idle_s:.0f}s)"
        return RUNNING, False, f"progressing ({idle:.0f}s since last)"

    # —— one full sweep over the whole pool ————————————————————————————————————————————————————
    async def poll_once(self, now: str | None = None) -> list[HealthVerdict]:
        """Sweep every pooled agent once; return a verdict per agent. Robust to per-agent errors."""
        verdicts: list[HealthVerdict] = []
        for key in list(self.pool):  # snapshot: a verdict's side effects must not break iteration
            rec = self.pool.get(key)
            if rec is None or rec.state == DONE:
                continue
            try:
                verdicts.append(await self._assess(rec, now))
            except Exception as exc:  # noqa: BLE001 — one agent's failure must not abort the sweep
                # Bound + type-tag the error so a future seam's exception can't dump an unbounded
                # message (e.g. a filesystem path) into a persisted/published event.
                await self._emit(
                    "error", rec.session_id, rec.task_id,
                    {
                        "key": key, "where": "supervisor.poll",
                        "error": f"{type(exc).__name__}: {exc}"[:200],
                    },
                )
        return verdicts

    async def _assess(self, rec: AgentRecord, now: str | None) -> HealthVerdict:
        prev = rec.state
        tail = self._read_tail(rec)  # read the tail once; reuse it for both classify and the judge
        state, suspicious, detail = self._classify(rec, now, tail)

        # ② Escalate to the LLM ONLY on a suspicious signal, and only if a judge is wired.
        escalated = False
        if suspicious and self.judge is not None:
            escalated = True
            refined = await self.judge(rec, tail)
            # The judge may only *refine a suspicion* — never push to DEAD/DONE/STARTING.
            if refined in _JUDGE_ALLOWED:
                state, detail = refined, f"{detail}; llm→{refined}"

        # Track consecutive crashes so repeated deaths escalate a card instead of looping restarts.
        # Any non-DEAD observation breaks the streak (the deaths are no longer consecutive).
        if state == DEAD:
            rec.fail_count += 1
        else:
            rec.fail_count = 0
        restarts_left = rec.fail_count <= self._max_restarts
        action = plan_recovery(state, restarts_left=restarts_left)

        rec.state = state
        await self._emit_transition(rec, prev, state, action, detail)
        return HealthVerdict(
            key=rec.key, session_id=rec.session_id, state=state, prev_state=prev,
            suspicious=suspicious, escalated=escalated, action=action, detail=detail,
        )

    async def _emit_transition(
        self, rec: AgentRecord, prev: str, state: str, action: str, detail: str
    ) -> None:
        """Emit health/stall/recover events on a state change (quiet while a state persists)."""
        if state == prev:
            return  # no churn: only report changes so the timeline stays readable
        payload = {
            "key": rec.key, "agent_type": rec.agent_type,
            "state": state, "prev": prev, "detail": detail, "action": action,
            "fail_count": rec.fail_count,
        }
        await self._emit("health", rec.session_id, rec.task_id, payload)
        if state in BAD_STATES:
            await self._emit("stall", rec.session_id, rec.task_id, payload)
        if action != "none":
            # Name the planned recovery; execution is deferred to P4 (decision loop owns the Runner).
            await self._emit(
                "recover", rec.session_id, rec.task_id,
                {**payload, "execution_deferred": True},
            )

    async def _emit(self, type_: str, session_id: str, task_id: str | None, payload: dict) -> None:
        """Persist THEN publish — mirrors Runner/HookReceiver so a late UI can backfill."""
        event = make_event(type_, "supervisor", session_id, task_id=task_id, payload=payload)
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)

    async def watch(self, *, interval: float = DEFAULT_INTERVAL_S) -> None:
        """Sweep the pool forever on ``interval`` seconds (cancel the task to stop)."""
        while True:
            await self.poll_once()
            await asyncio.sleep(interval)


# —— LLM judge (② escalation) — built now, live hookup deferred (needs the user's API key) ————————
_JUDGE_SYSTEM = (
    "You are the watchdog for a coding-agent supervisor. A cheap check flagged an agent as possibly "
    "stuck. Given its type and the tail of its output, decide its true state. Reply with ONLY a JSON "
    'object: {"state": "<one of: running, waiting_input, stalled, errored>"}. '
    "Use 'running' if it is clearly still making progress, 'waiting_input' if it is blocked on a "
    "question for the human, 'stalled' if it is hung with no progress, 'errored' if it failed / hit a "
    "rate limit / lost auth. When unsure, prefer 'stalled' so a human is alerted."
)


# Defense-in-depth: a coding agent's stdout tail can contain secrets (API keys, bearer tokens). The
# tail is the one thing the watchdog sends off-box (to the user's *own* LLM endpoint), so scrub the
# obvious credential shapes before egress. Cheap regex masking — not a guarantee, just a backstop.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),                               # OpenAI-style keys
    re.compile(r"\b(?:Bearer|token)\s+[A-Za-z0-9._-]{8,}", re.I),      # bearer / token <value>
    re.compile(r"(?i)(api[_-]?key|secret|password)\s*[:=]\s*\S+"),     # key=... / secret: ...
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),                         # GitHub tokens
)


def redact_secrets(text: str | None) -> str:
    """Mask common credential shapes in ``text`` before it leaves the machine (best-effort)."""
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def parse_judge_state(text: str) -> str | None:
    """Pull a recognized state out of the judge's reply (JSON preferred, substring fallback)."""
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            cand = str(obj.get("state", "")).strip().lower()
            if cand in _JUDGE_ALLOWED:
                return cand
    except (ValueError, TypeError):
        pass
    low = text.lower()
    for state in (WAITING_INPUT, STALLED, ERRORED, RUNNING):  # specific → generic
        if state in low:
            return state
    return None


@dataclass
class LLMJudge:
    """Wraps an LLMClient as a ``Judge``; appends ``language_directive`` per DESIGN §15.

    Live API hookup is gated on the user's own LLM key (config.llm + .env). The prompt-building and
    parsing are exercised here via a mock transport; wiring to a real key is deferred (TASKS T2.6).
    """

    llm: Any  # foreman.shared.llm.LLMClient
    language: str = "zh"
    tail_chars: int = 2000  # only the tail is sent — bounded token cost

    async def __call__(self, rec: AgentRecord, tail: str | None) -> str | None:
        from foreman.shared.llm.client import Message  # local import: keep client a soft dep
        from foreman.shared.llm.trace import trace_context

        system = _JUDGE_SYSTEM + "\n" + language_directive(self.language)
        safe_tail = redact_secrets(tail)[-self.tail_chars:]  # scrub THEN bound
        user = (
            f"agent_type: {rec.agent_type}\n"
            f"cheap_check_state: {rec.state}\n"
            f"output_tail:\n{safe_tail}"
        )
        with trace_context(phase="supervisor"):
            out = await self.llm.complete(
                [Message("system", system), Message("user", user)], json_mode=True
            )
        return parse_judge_state(out)


__all__ = [
    "Supervisor",
    "AgentRecord",
    "HealthVerdict",
    "Thresholds",
    "LLMJudge",
    "classify_tail",
    "plan_recovery",
    "parse_judge_state",
    "redact_secrets",
    "HEALTH_STATES",
    "BAD_STATES",
    "STARTING",
    "RUNNING",
    "IDLE",
    "WAITING_INPUT",
    "STALLED",
    "ERRORED",
    "DEAD",
    "DONE",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_INTERVAL_S",
]
