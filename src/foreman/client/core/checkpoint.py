"""Checkpoint Manager — per-step git snapshots that enable one-click undo (see §6.5).

Before each workflow step runs, snapshot the workspace so any step can be reverted from PC/phone
("better than Copilot's undo": a timeline you can roll back to). Only workspace files are covered;
irreversible side-effects (network/DB/deploy) are blocked up front by the Gate, not undone here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def ensure_repo(workspace: Path) -> bool:
    """Ensure `workspace` is inside a git work tree; `git init` it if not.

    Returns True if it ran `git init` (workspace was not a repo), False if already one. Checkpoints
    (§6.5) are built on git, so every workspace Foreman drives must be a repo. A workspace that is a
    subdir of an existing repo is left as-is (already inside a work tree). Uses argv lists (no shell).
    """
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(workspace), capture_output=True, text=True,
    )
    if inside.returncode == 0 and inside.stdout.strip() == "true":
        return False
    subprocess.run(["git", "init"], cwd=str(workspace), capture_output=True, text=True, check=True)
    return True


class CheckpointManager:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    def ensure_repo(self) -> bool:
        """Make sure the workspace is a git repo (git init if needed). See module `ensure_repo`."""
        return ensure_repo(self.workspace)

    async def snapshot(self, session_id: str, step_index: int, label: str = "") -> str:
        """Create a checkpoint before a step; return a vcs_ref (git commit/stash/tag). Roadmap P2."""
        # P2: git add -A && commit on a foreman ref (or stash create), record ref in `checkpoints`.
        raise NotImplementedError("CheckpointManager.snapshot — roadmap P2")

    async def undo_to(self, vcs_ref: str) -> None:
        """Revert the workspace to a checkpoint (git reset --hard <ref>) and reset agent state. P2/P4."""
        raise NotImplementedError("CheckpointManager.undo_to — roadmap P2/P4")
