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
        """Record an attempt for ``key``; return False if it exceeds the window budget."""
        t = self._now()
        cutoff = t - self.window
        dq = self._hits.get(key)
        if dq is None:
            if len(self._hits) >= self.max_keys:
                self._sweep(cutoff)  # reclaim drained buckets before admitting a new key
                if len(self._hits) >= self.max_keys:
                    return False  # still flooded → fail closed rather than grow unbounded
            dq = deque()
            self._hits[key] = dq
        else:
            while dq and dq[0] <= cutoff:
                dq.popleft()
        if len(dq) >= self.max_events:
            return False
        dq.append(t)
        return True

    def reset(self, key: str | None = None) -> None:
        """Clear one key's history (or all keys when ``key`` is None) — e.g. after a success."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)


__all__ = ["SlidingWindowLimiter"]
