"""Tests for the sliding-window rate limiter (auth brute-force speed bump, DESIGN §8.2)."""

from __future__ import annotations

from foreman.shared.ratelimit import SlidingWindowLimiter


def test_allows_up_to_budget_then_blocks():
    t = {"v": 1000.0}
    rl = SlidingWindowLimiter(3, 60, now=lambda: t["v"])
    assert [rl.allow("ip") for _ in range(4)] == [True, True, True, False]


def test_window_slides_and_budget_refills():
    t = {"v": 0.0}
    rl = SlidingWindowLimiter(2, 60, now=lambda: t["v"])
    assert rl.allow("ip") and rl.allow("ip")
    assert rl.allow("ip") is False           # budget spent within the window
    t["v"] += 61                              # advance past the window
    assert rl.allow("ip") is True             # old attempts evicted -> allowed again


def test_keys_are_independent():
    rl = SlidingWindowLimiter(1, 10, now=lambda: 0.0)
    assert rl.allow("a") is True
    assert rl.allow("b") is True              # a different IP has its own budget
    assert rl.allow("a") is False            # but 'a' is now over budget


def test_reset_clears_history():
    rl = SlidingWindowLimiter(1, 10, now=lambda: 0.0)
    assert rl.allow("a") is True and rl.allow("a") is False
    rl.reset("a")
    assert rl.allow("a") is True
