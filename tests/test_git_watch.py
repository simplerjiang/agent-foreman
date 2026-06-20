"""Tests for GitWatcher — workspace git state → git_diff/git_commit events + progress (TASKS T2.5).

Git is reached through an injectable ``runner`` (argv list, no shell), so no real repo is needed: a
``FakeGit`` serves scripted ``git rev-parse HEAD`` / ``git status --porcelain`` results per call. The
first poll establishes a baseline (no events); subsequent polls report only what changed and touch
the ProgressTracker on any change.
"""

from __future__ import annotations

import subprocess

from foreman.client.monitor.git_watch import GitWatcher
from foreman.client.monitor.progress import ProgressTracker
from foreman.shared.events import EventBus


def _cp(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


class FakeGit:
    """Returns scripted CompletedProcess results for rev-parse / status, recording calls."""

    def __init__(self, head="", dirty="", head_rc=0, status_rc=0):
        self.head = head
        self.dirty = dirty
        self.head_rc = head_rc
        self.status_rc = status_rc
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd):
        self.calls.append(args)
        if args[:1] == ["rev-parse"]:
            return _cp(self.head, self.head_rc)
        if args[:1] == ["status"]:
            return _cp(self.dirty, self.status_rc)
        return _cp("", 0)


def _bus_capture(bus):
    captured = []
    orig = bus.publish

    async def publish(ev):
        captured.append(ev)
        await orig(ev)

    bus.publish = publish
    return captured


async def test_first_poll_is_baseline_no_events():
    git = FakeGit(head="abc", dirty="")
    bus = EventBus()
    captured = _bus_capture(bus)
    w = GitWatcher(bus, runner=git)

    out = await w.poll("/ws", "s1")

    assert out == []
    assert captured == []


async def test_new_commit_emits_git_commit():
    git = FakeGit(head="aaa", dirty="")
    bus = EventBus()
    tracker = ProgressTracker(clock=lambda: "2026-06-20T00:00:00+00:00")
    w = GitWatcher(bus, tracker=tracker, runner=git)

    await w.poll("/ws", "s1")          # baseline at aaa
    git.head = "bbb"                    # a commit happened
    out = await w.poll("/ws", "s1")

    types = [e.type for e in out]
    assert types == ["git_commit"]
    assert out[0].payload == {"head": "bbb", "prev": "aaa"}
    assert out[0].source == "git" and out[0].session_id == "s1"
    assert tracker.last("s1") == "2026-06-20T00:00:00+00:00"  # progress touched


async def test_worktree_change_emits_git_diff_with_counts_only():
    git = FakeGit(head="aaa", dirty="")
    bus = EventBus()
    w = GitWatcher(bus, runner=git)

    await w.poll("/ws", "s1")  # baseline clean
    git.dirty = " M a.py\n M b.py\n?? new.txt\n"
    out = await w.poll("/ws", "s1")

    assert [e.type for e in out] == ["git_diff"]
    # privacy: only counts, never file contents/names
    assert out[0].payload == {"changed_files": 3, "untracked": 1}


async def test_commit_and_diff_together():
    git = FakeGit(head="aaa", dirty="")
    bus = EventBus()
    w = GitWatcher(bus, runner=git)

    await w.poll("/ws", "s1")
    git.head, git.dirty = "bbb", " M a.py\n"
    out = await w.poll("/ws", "s1")

    assert [e.type for e in out] == ["git_commit", "git_diff"]


async def test_no_change_no_events_no_touch():
    git = FakeGit(head="aaa", dirty=" M a.py\n")
    bus = EventBus()
    tracker = ProgressTracker(clock=lambda: "2026-06-20T00:00:00+00:00")
    w = GitWatcher(bus, tracker=tracker, runner=git)

    await w.poll("/ws", "s1")           # baseline
    out = await w.poll("/ws", "s1")     # identical state

    assert out == []
    assert tracker.last("s1") is None   # nothing changed → no progress


async def test_events_persisted_to_store_then_published(tmp_path):
    from foreman.client.store import Store
    from foreman.client.store.models import Session

    store = Store(str(tmp_path / "g.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g"))

    git = FakeGit(head="aaa", dirty="")
    bus = EventBus()
    w = GitWatcher(bus, store=store, runner=git)

    await w.poll("/ws", "s1")
    git.head = "bbb"
    await w.poll("/ws", "s1")

    assert [e.type for e in store.get_events("s1")] == ["git_commit"]


async def test_not_a_repo_stays_baseline():
    # rev-parse fails (rc!=0) and status empty → head None, dirty "" — no spurious events.
    git = FakeGit(head="", dirty="", head_rc=128)
    bus = EventBus()
    w = GitWatcher(bus, runner=git)

    await w.poll("/ws", "s1")
    out = await w.poll("/ws", "s1")
    assert out == []


async def test_drop_forgets_state():
    git = FakeGit(head="aaa", dirty="")
    w = GitWatcher(EventBus(), runner=git)
    await w.poll("/ws", "s1")
    assert "s1" in w._seen
    w.drop("s1")
    assert "s1" not in w._seen
    w.drop("missing")  # no-op, no raise


async def test_separate_keys_track_independently():
    git = FakeGit(head="aaa", dirty="")
    bus = EventBus()
    w = GitWatcher(bus, runner=git)

    await w.poll("/ws", "s1", key="agentA")
    await w.poll("/ws", "s1", key="agentB")
    git.head = "bbb"
    out_a = await w.poll("/ws", "s1", key="agentA")
    assert [e.type for e in out_a] == ["git_commit"]
