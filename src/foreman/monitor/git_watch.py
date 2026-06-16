"""Git watcher — observe diffs/commits in a workspace and emit events for the Reviewer.

Uses `watchfiles` on the workspace and `git diff` / `git log` to extract changes.
See docs/DESIGN.zh-CN.md §4.3.
"""

from __future__ import annotations

from pathlib import Path

from ..core.events import EventBus


class GitWatcher:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus

    async def watch(self, workspace: Path, session_id: str) -> None:
        """Watch the workspace; emit git_diff / git_commit events (P2)."""
        raise NotImplementedError("GitWatcher.watch — roadmap P2")
