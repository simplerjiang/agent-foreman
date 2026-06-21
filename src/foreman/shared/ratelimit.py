"""Tiny in-process sliding-window rate limiter — a brute-force speed bump for auth (DESIGN §8.2).

Deliberately simple: in-memory, per-process, not distributed and not durable. It exists to make
online password / invite brute-forcing impractical (§8.2 login), NOT to be an accounting system.
`now` is injectable (monotonic seconds) so tests are deterministic without sleeping.
"""

from __future__ import annotations

import time
from collections import deque


class SlidingWindowLimiter:
    """Allow at most ``max_events`` per ``window_seconds`` for each key.

    Each key (e.g. a client IP) keeps a deque of recent attempt timestamps; entries older than the
    window are evicted on access, so the check is O(attempts-in-window).

    Memory is bounded against a hostile flood of distinct keys (e.g. spoofed IPs): a key whose bucket
    drains to empty is dropped immediately, and once the map exceeds ``max_keys`` a full sweep evicts
    every empty/expired bucket. If a flood still keeps it over capacity, ``allow`` fails closed
    (returns False) rather than growing without limit — denying the *excess* attempts is the safe
    direction for an auth speed bump.
    """

    def __init__(
        self, max_events: int, window_seconds: float, *, now=time.monotonic, max_keys: int = 100_000
    ) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self.max_keys = max_keys
        self._now = now
        self._hits: dict[str, deque[float]] = {}

    def _sweep(self, cutoff: float) -> None:
        """Drop every key whose attempts have all aged out of the window (bounds the map)."""
        for k in [k for k, dq in self._hits.items() if not dq or dq[-1] <= cutoff]:
            del self._hits[k]

    def allow(self, key: str) -> bool:
        """Record an attempt for ``key``; return False if it exceeds the window budget.

        Convenience for the common "check-and-count in one shot" case. For auth, prefer the
        ``over_limit`` (peek) + ``record`` (count only on failure) pair so a correct credential is
        never pre-blocked and successes don't burn the budget."""
        if self.over_limit(key):
            return False
        return self.record(key)  # False if a key-flood made the record fail closed

    def over_limit(self, key: str) -> bool:
        """True if ``key`` is already at/over budget in the current window. Peeks (evicts aged-out
        entries lazily) but records nothing — so checking can't itself consume the budget."""
        dq = self._hits.get(key)
        if dq is None:
            return False
        cutoff = self._now() - self.window
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if not dq:
            self._hits.pop(key, None)  # drained → drop the key
            return False
        return len(dq) >= self.max_events

    def record(self, key: str) -> bool:
        """Count one attempt (e.g. a failure) for ``key``, bounding the key map against a flood.

        Returns True if it was recorded; False if a distinct-key flood is at capacity even after a
        sweep, in which case the record is dropped rather than growing memory without bound."""
        t = self._now()
        cutoff = t - self.window
        dq = self._hits.get(key)
        if dq is None:
            if len(self._hits) >= self.max_keys:
                self._sweep(cutoff)  # reclaim drained buckets before admitting a new key
                if len(self._hits) >= self.max_keys:
                    return False  # still flooded → drop this record rather than grow unbounded
            dq = deque()
            self._hits[key] = dq
        else:
            while dq and dq[0] <= cutoff:
                dq.popleft()
        dq.append(t)
        return True

    def reset(self, key: str | None = None) -> None:
        """Clear one key's history (or all keys when ``key`` is None) — e.g. after a success."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


__all__ = ["SlidingWindowLimiter"]
