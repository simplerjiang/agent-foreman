"""Tests for ProgressTracker — the live home of each agent's last_progress_at (TASKS T2.5).

The clock is injected so idle math is deterministic (no real time). Covers touch/last/keys/drop and
the idle_seconds / is_idle helpers the Supervisor watchdog (T2.6) reads.
"""

from __future__ import annotations

from foreman.client.monitor.progress import ProgressTracker


def _clock(seq):
    """A clock() that yields the given ISO timestamps in order, repeating the last forever."""
    it = iter(seq)
    last = {"v": seq[-1]}

    def tick():
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]

    return tick


def test_touch_records_and_returns_timestamp():
    t = ProgressTracker(clock=lambda: "2026-06-20T00:00:00+00:00")
    assert t.last("a") is None
    ts = t.touch("a")
    assert ts == "2026-06-20T00:00:00+00:00"
    assert t.last("a") == ts


def test_keys_and_drop():
    t = ProgressTracker(clock=lambda: "2026-06-20T00:00:00+00:00")
    t.touch("a")
    t.touch("b")
    assert set(t.keys()) == {"a", "b"}
    t.drop("a")
    assert t.keys() == ["b"]
    t.drop("missing")  # no-op, no raise


def test_idle_seconds_none_when_never_tracked():
    t = ProgressTracker()
    assert t.idle_seconds("ghost") is None
    assert t.is_idle("ghost", 1.0) is False  # untracked is not "idle"


def test_idle_seconds_measures_gap_against_now():
    t = ProgressTracker(clock=_clock(["2026-06-20T00:00:00+00:00"]))
    t.touch("a")  # last = 00:00:00
    gap = t.idle_seconds("a", now="2026-06-20T00:02:00+00:00")
    assert gap == 120.0


def test_is_idle_threshold_boundary():
    t = ProgressTracker(clock=_clock(["2026-06-20T00:00:00+00:00"]))
    t.touch("a")
    now = "2026-06-20T00:00:30+00:00"  # 30s later
    assert t.is_idle("a", 30.0, now=now) is True   # >= threshold
    assert t.is_idle("a", 31.0, now=now) is False  # not yet


def test_touch_refreshes_timestamp():
    t = ProgressTracker(clock=_clock(
        ["2026-06-20T00:00:00+00:00", "2026-06-20T00:05:00+00:00"]
    ))
    t.touch("a")  # 00:00:00
    t.touch("a")  # 00:05:00 — newer progress
    assert t.last("a") == "2026-06-20T00:05:00+00:00"
    assert t.idle_seconds("a", now="2026-06-20T00:05:10+00:00") == 10.0
