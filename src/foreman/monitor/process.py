"""Process / idle watcher — liveness and "looks stuck" detection.

If a session produces no output for `monitor.idle_seconds`, mark it idle/blocked so the
PM Brain can decide whether to ping the phone. Combine with Claude Code's Notification
hook (a stronger "waiting for input" signal) to reduce false positives.
See docs/DESIGN.zh-CN.md §4.3 and §12.
"""

from __future__ import annotations

from ..core.events import EventBus


class ProcessWatcher:
    def __init__(self, bus: EventBus, idle_seconds: int = 120) -> None:
        self.bus = bus
        self.idle_seconds = idle_seconds

    async def watch(self, session_id: str, pid: int | None) -> None:
        """Track liveness + last-output time; emit idle/blocked events (P2)."""
        raise NotImplementedError("ProcessWatcher.watch — roadmap P2")
