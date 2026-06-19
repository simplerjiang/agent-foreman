"""Scheduler — periodic assessment sweeps and briefings.

- Every `assess_every_s`, ask PM Brain to sweep active sessions.
- Optional daily briefing at a configured local time.
- Optional "you're back" detection to trigger an active briefing.
See docs/DESIGN.zh-CN.md §4.1.
"""

from __future__ import annotations

from foreman.shared.config import ScheduleCfg
from .brain import PMBrain


class Scheduler:
    def __init__(self, cfg: ScheduleCfg, brain: PMBrain) -> None:
        self.cfg = cfg
        self.brain = brain

    async def run(self) -> None:
        """Long-running loop driving periodic assessment + briefings (P2/P4)."""
        raise NotImplementedError("Scheduler.run — roadmap P2/P4")
