"""Git watcher — observe diffs/commits in a workspace, emit events, refresh last_progress_at.

One of the three observation sources (DESIGN §4.3): watch the workspace's git state and turn
worktree changes into ``git_diff`` events and new commits into ``git_commit`` events (fed to the
Reviewer, §5.3), while every change also refreshes the agent's ``last_progress_at`` (§4.1) so the
Supervisor watchdog can tell "still making changes" from "stalled".

The testable core is ``poll()`` — a single deterministic comparison against the last seen state;
``watch()`` is just a loop that calls it on an interval. Git is invoked via an injectable ``runner``
seam (argv list, no shell — same discipline as CheckpointManager) so tests need no real repo. The
first poll of a key establishes a baseline and emits nothing (we only report *changes*).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from foreman.shared.events import AgentEvent, make_event

# Default poll cadence for the watch() loop; DESIGN §4.1 cheap deterministic poll is 10–30s, but
# git changes are worth catching a touch faster. Callers may override per workspace.
DEFAULT_INTERVAL_S = 5.0

GitRunner = Callable[[list[str], str], subprocess.CompletedProcess]


def _default_git_runner(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run ``git <args>`` in ``cwd`` with an argv list (no shell), UTF-8, never raising on nonzero."""
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )


@dataclass
class _GitState:
    head: str | None  # HEAD commit SHA, or None on an empty repo / not-a-repo
    dirty: str        # `git status --porcelain` output (tracked + untracked changes)


class GitWatcher:
    def __init__(
        self,
        bus,
        tracker=None,
        store=None,
        *,
        runner: GitRunner = _default_git_runner,
    ) -> None:
        self.bus = bus
        self.tracker = tracker          # optional ProgressTracker: change → touch(key)
        self.store = store              # optional client Store: persist events (timeline/replay)
        self._git = runner
        self._seen: dict[str, _GitState] = {}  # per key: last observed git state

    def _read_state(self, workspace: str) -> _GitState:
        head = self._git(["rev-parse", "HEAD"], workspace)
        status = self._git(["status", "--porcelain"], workspace)
        return _GitState(
            head=head.stdout.strip() if head.returncode == 0 else None,
            dirty=status.stdout if status.returncode == 0 else "",
        )

    @staticmethod
    def _dirty_summary(porcelain: str) -> dict:
        """Counts only (not file contents) from `git status --porcelain` — small + privacy-safe."""
        lines = [ln for ln in porcelain.splitlines() if ln.strip()]
        return {"changed_files": len(lines), "untracked": sum(ln.startswith("??") for ln in lines)}

    async def poll(
        self,
        workspace: str | Path,
        session_id: str,
        *,
        key: str | None = None,
        task_id: str | None = None,
    ) -> list[AgentEvent]:
        """Compare current git state to the last seen; emit git_commit/git_diff on change.

        Returns the events emitted this poll (possibly empty). The first poll of a ``key`` just
        records a baseline. ``key`` defaults to ``session_id`` (one agent per session is the common
        case); pass the agent handle id when several agents share a workspace.
        """
        workspace = str(workspace)
        key = key or session_id
        state = self._read_state(workspace)
        prev = self._seen.get(key)
        self._seen[key] = state

        if prev is None:  # baseline — nothing to compare against yet
            return []

        events: list[AgentEvent] = []
        if state.head != prev.head and state.head is not None:
            events.append(make_event(
                "git_commit", "git", session_id, task_id=task_id,
                payload={"head": state.head, "prev": prev.head},
            ))
        if state.dirty != prev.dirty:
            events.append(make_event(
                "git_diff", "git", session_id, task_id=task_id,
                payload=self._dirty_summary(state.dirty),
            ))

        if events and self.tracker is not None:
            self.tracker.touch(key)  # any git change counts as progress (§4.1)
        for ev in events:
            await self._record(ev)
        return events

    async def watch(
        self,
        workspace: str | Path,
        session_id: str,
        *,
        key: str | None = None,
        task_id: str | None = None,
        interval: float = DEFAULT_INTERVAL_S,
    ) -> None:
        """Poll the workspace forever on ``interval`` seconds (cancel the task to stop)."""
        while True:
            await self.poll(workspace, session_id, key=key, task_id=task_id)
            await asyncio.sleep(interval)

    def drop(self, key: str) -> None:
        """Forget a workspace's last-seen state (e.g. once its agent fully stops). No-op if unknown."""
        self._seen.pop(key, None)

    async def _record(self, event: AgentEvent) -> None:
        """Persist THEN publish — mirrors Runner/HookReceiver so a late UI can backfill."""
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        await self.bus.publish(event)
