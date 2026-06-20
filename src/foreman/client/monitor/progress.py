"""ProgressTracker — the live home of each agent's ``last_progress_at`` (DESIGN §4.1 / §4.3).

The Supervisor watchdog (T2.6) decides "is this agent stalled?" by asking how long it has been
since the agent last made progress. *Progress* is any fresh signal — a stdout line from the Runner,
a Claude hook, a git diff/commit, or observed CPU/IO activity — and **any** of those simply calls
``touch(key)`` here. Keeping it in-memory (not a DB write per signal) matches §4.1's note that the
Runner holds the handles and their ``last_progress_at``; the cheap 10–30s poll reads it without
spending tokens or hitting SQLite every tick. The clock is injected so tests stay deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from foreman.shared.events import utc_now_iso


class ProgressTracker:
    """Per-agent ``last_progress_at`` registry. Keyed by a caller-chosen agent key (e.g. handle id)."""

    def __init__(self, clock: Callable[[], str] = utc_now_iso) -> None:
        # clock() returns a UTC ISO8601 timestamp (same format as events) so values are comparable.
        self._clock = clock
        self._last: dict[str, str] = {}

    def touch(self, key: str) -> str:
        """Record that agent ``key`` just made progress; return the new timestamp."""
        ts = self._clock()
        self._last[key] = ts
        return ts

    def last(self, key: str) -> str | None:
        """The last-progress timestamp for ``key`` (ISO8601), or None if never touched."""
        return self._last.get(key)

    def keys(self) -> list[str]:
        """All tracked agent keys (so the Supervisor can sweep the whole pool)."""
        return list(self._last)

    def drop(self, key: str) -> None:
        """Forget an agent (e.g. once it has fully stopped). No-op if unknown."""
        self._last.pop(key, None)

    def idle_seconds(self, key: str, now: str | None = None) -> float | None:
        """Seconds since ``key`` last made progress, or None if it was never tracked.

        ``now`` defaults to the tracker's clock; pass an explicit ISO timestamp in tests.
        """
        last = self._last.get(key)
        if last is None:
            return None
        ref = now or self._clock()
        return (datetime.fromisoformat(ref) - datetime.fromisoformat(last)).total_seconds()

    def is_idle(self, key: str, threshold_seconds: float, now: str | None = None) -> bool:
        """True if ``key`` has gone ``threshold_seconds`` without progress (untracked → not idle)."""
        idle = self.idle_seconds(key, now)
        return idle is not None and idle >= threshold_seconds
