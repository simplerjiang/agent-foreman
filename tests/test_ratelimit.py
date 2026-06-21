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


def test_key_map_is_bounded_against_a_flood():
    """A flood of distinct keys (e.g. spoofed IPs) can't grow the map without bound: at capacity it
    sweeps drained buckets, and if still full it fails closed instead of allocating more (issue #10)."""
    t = {"v": 0.0}
    rl = SlidingWindowLimiter(5, 10, now=lambda: t["v"], max_keys=3)
    assert rl.allow("a") and rl.allow("b") and rl.allow("c")  # 3 live keys → at capacity
    # a 4th distinct key while a/b/c are still in-window: nothing to reclaim → fail closed
    assert rl.allow("d") is False
    assert len(rl._hits) == 3
    # once the window passes, a/b/c are drained; admitting a new key sweeps them away
    t["v"] += 11
    assert rl.allow("d") is True
    assert len(rl._hits) <= 3
