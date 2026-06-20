"""Tests for ProcessWatcher — liveness + CPU activity → last_progress_at (TASKS T2.5).

psutil is reached only through the injectable ``sampler`` seam, so these tests drive scripted
ProcSample sequences with no real process. The default psutil sampler is exercised separately
against this very test process (a guaranteed-alive PID).
"""

from __future__ import annotations

import os

import pytest

from foreman.client.monitor.process import (
    ProcessWatcher,
    ProcSample,
    _default_sampler,
)
from foreman.client.monitor.progress import ProgressTracker


def _sampler(samples):
    """A sampler() that returns the given ProcSample/None values in order, repeating the last."""
    seq = list(samples)

    def sample(_pid):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return sample


def test_first_poll_is_baseline_not_active():
    w = ProcessWatcher(sampler=_sampler([ProcSample(alive=True, cpu_seconds=1.0)]))
    st = w.poll("a", 100)
    assert st.alive is True
    assert st.active is False  # baseline never reports activity


def test_cpu_growth_marks_active_and_touches_tracker():
    tracker = ProgressTracker(clock=lambda: "2026-06-20T00:00:00+00:00")
    w = ProcessWatcher(
        tracker=tracker,
        sampler=_sampler([
            ProcSample(alive=True, cpu_seconds=1.0),
            ProcSample(alive=True, cpu_seconds=1.5),  # burned 0.5s CPU
        ]),
    )
    w.poll("a", 100)             # baseline
    st = w.poll("a", 100)
    assert st.active is True
    assert tracker.last("a") == "2026-06-20T00:00:00+00:00"  # progress recorded


def test_flat_cpu_is_not_active_no_touch():
    tracker = ProgressTracker(clock=lambda: "2026-06-20T00:00:00+00:00")
    w = ProcessWatcher(
        tracker=tracker,
        sampler=_sampler([
            ProcSample(alive=True, cpu_seconds=2.0),
            ProcSample(alive=True, cpu_seconds=2.0),  # no CPU burned → likely hung
        ]),
    )
    w.poll("a", 100)
    st = w.poll("a", 100)
    assert st.active is False
    assert tracker.last("a") is None  # no progress → watchdog can flag idle


def test_tiny_jitter_below_threshold_is_not_active():
    w = ProcessWatcher(
        active_threshold_s=0.01,
        sampler=_sampler([
            ProcSample(alive=True, cpu_seconds=1.0),
            ProcSample(alive=True, cpu_seconds=1.005),  # 0.005s < 0.01 epsilon
        ]),
    )
    w.poll("a", 100)
    assert w.poll("a", 100).active is False


def test_dead_process_reported_and_baseline_dropped():
    w = ProcessWatcher(
        sampler=_sampler([
            ProcSample(alive=True, cpu_seconds=1.0),
            ProcSample(alive=False, cpu_seconds=0.0),
        ]),
    )
    w.poll("a", 100)               # alive baseline
    st = w.poll("a", 100)
    assert st.alive is False and st.active is False
    assert "a" not in w._seen     # baseline cleared so a restart re-baselines


def test_unreadable_tick_reports_unknown_without_disturbing_baseline():
    samples = [
        ProcSample(alive=True, cpu_seconds=1.0),
        None,                                       # transient AccessDenied / read failure
        ProcSample(alive=True, cpu_seconds=1.5),
    ]
    seq = list(samples)
    w = ProcessWatcher(sampler=lambda _pid: seq.pop(0))

    w.poll("a", 100)                 # baseline at 1.0
    st = w.poll("a", 100)            # unreadable
    assert st.alive is None and st.active is False
    st = w.poll("a", 100)            # 1.5 — compared against the 1.0 baseline, not lost
    assert st.active is True


def test_drop_forgets_baseline():
    w = ProcessWatcher(sampler=_sampler([ProcSample(alive=True, cpu_seconds=1.0)]))
    w.poll("a", 100)
    assert "a" in w._seen
    w.drop("a")
    assert "a" not in w._seen
    w.drop("missing")  # no-op


def test_dead_then_alive_rebaselines_without_false_active():
    samples = [
        ProcSample(alive=True, cpu_seconds=5.0),   # baseline (old run)
        ProcSample(alive=False, cpu_seconds=0.0),  # process died → baseline dropped
        ProcSample(alive=True, cpu_seconds=9.0),   # restart: high CPU but a fresh baseline
        ProcSample(alive=True, cpu_seconds=9.2),   # now real progress on the new run
    ]
    seq = list(samples)
    tracker = ProgressTracker(clock=lambda: "2026-06-20T00:00:00+00:00")
    w = ProcessWatcher(tracker=tracker, sampler=lambda _pid: seq.pop(0))

    w.poll("a", 100)                       # baseline
    w.poll("a", 100)                       # dead
    assert w.poll("a", 100).active is False  # restart re-baselines, no false progress
    assert tracker.last("a") is None
    assert w.poll("a", 100).active is True   # genuine CPU growth on the new run


def test_pid_change_under_same_key_rebaselines():
    # Same key, new pid with higher cumulative CPU must NOT be read as activity.
    seq = [
        ProcSample(alive=True, cpu_seconds=3.0),
        ProcSample(alive=True, cpu_seconds=100.0),  # different process, big number
    ]
    pids = [100, 200]
    tracker = ProgressTracker(clock=lambda: "2026-06-20T00:00:00+00:00")
    w = ProcessWatcher(tracker=tracker, sampler=lambda _pid: seq.pop(0))

    w.poll("a", pids[0])
    assert w.poll("a", pids[1]).active is False  # pid changed → re-baseline, no touch
    assert tracker.last("a") is None


def test_default_sampler_reads_a_live_process():
    """The real psutil sampler should see this very test process as alive with CPU time."""
    pytest.importorskip("psutil")
    s = _default_sampler(os.getpid())
    assert s is not None
    assert s.alive is True
    assert s.cpu_seconds > 0.0


def test_default_sampler_dead_for_bogus_pid():
    # A PID that does not exist → NoSuchProcess → alive False (not None).
    pytest.importorskip("psutil")
    s = _default_sampler(2_000_000_000)
    assert s is not None
    assert s.alive is False
