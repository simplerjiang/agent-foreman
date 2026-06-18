"""Checkpoint Manager — per-step git snapshots that enable one-click undo (see §6.5).

Before each workflow step runs, snapshot the workspace so any step can be reverted from PC/phone
("better than Copilot's undo": a timeline you can roll back to). Only workspace files are covered;
irreversible side-effects (network/DB/deploy) are blocked up front by the Gate, not undone here.
"""

from __future__ import annotations

from pathlib import Path


class CheckpointManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    async def snapshot(self, session_id: str, step_index: int, label: str = "") -> str:
        """Create a checkpoint before a step; return a vcs_ref (git commit/stash/tag). Roadmap P2."""
        # P2: git add -A && commit on a foreman ref (or stash create), record ref in `checkpoints`.
        raise NotImplementedError("CheckpointManager.snapshot — roadmap P2")

    async def undo_to(self, vcs_ref: str) -> None:
        """Revert the workspace to a checkpoint (git reset --hard <ref>) and reset agent state. P2/P4."""
        raise NotImplementedError("CheckpointManager.undo_to — roadmap P2/P4")
