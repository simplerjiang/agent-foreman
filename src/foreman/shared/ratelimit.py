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
    window are evicted on access, so the check is O(attempts-in-window). Empty buckets are dropped
    so the key map doesn't grow without bound for one-off keys.
    """

    def __init__(self, max_events: int, window_seconds: float, *, now=time.monotonic) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._now = now
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Record an attempt for ``key``; return False if it exceeds the window budget."""
        t = self._now()
        cutoff = t - self.window
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.max_events:
            if not dq:  # never reached (len 0 < max), but keep the map tidy defensively
                self._hits.pop(key, None)
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
