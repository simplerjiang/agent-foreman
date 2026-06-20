"""Checkpoint Manager — per-step git snapshots that enable one-click undo (see §6.5).

Before each workflow step runs, snapshot the workspace so any step can be reverted from PC/phone
("better than Copilot's undo": a timeline you can roll back to). Only workspace files are covered;
irreversible side-effects (network/DB/deploy) are blocked up front by the Gate, not undone here.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

# Shadow-ref namespace for checkpoints: refs/foreman/ckpt/<session>/<step> (§6.5). Living outside
# refs/heads keeps them invisible to `git branch`/`git log` and out of a default `git push`.
CKPT_REF_PREFIX = "refs/foreman/ckpt"

# Stable identity for the snapshot commits so commit-tree never fails on a repo with no user.name
# configured (e.g. a fresh CI checkout). These commits never reach your real history unless squashed.
_CKPT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Foreman",
    "GIT_AUTHOR_EMAIL": "foreman@localhost",
    "GIT_COMMITTER_NAME": "Foreman",
    "GIT_COMMITTER_EMAIL": "foreman@localhost",
}


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
    def __init__(self, workspace: Path, store=None) -> None:
        self.workspace = Path(workspace)
        # Optional client Store; when given, snapshot() records a `checkpoints` row (§7.1).
        self.store = store

    def ensure_repo(self) -> bool:
        """Make sure the workspace is a git repo (git init if needed). See module `ensure_repo`."""
        return ensure_repo(self.workspace)

    def _git(self, *args: str, env: dict | None = None, check: bool = True):
        """Run git in the workspace with an argv list (no shell), UTF-8 in/out."""
        return subprocess.run(
            ["git", *args],
            cwd=str(self.workspace),
            capture_output=True, text=True, encoding="utf-8",
            env=env, check=check,
        )

    async def snapshot(
        self, session_id: str, step_index: int, label: str = "", task_id: str | None = None
    ) -> str:
        """Snapshot the whole workspace before a step; return the checkpoint commit SHA (§6.5).

        Uses a throwaway index (``GIT_INDEX_FILE``) so the real staging area, current branch, and
        history are never touched: ``git add -A`` (a full worktree snapshot, honouring .gitignore) →
        ``write-tree`` → ``commit-tree`` (parent = this session's previous checkpoint, chaining a
        timeline) → ``update-ref`` onto the shadow ref ``refs/foreman/ckpt/<session>/<step>``.
        ``core.autocrlf=false`` keeps line endings byte-identical so undo restores exactly (§6.5 note).
        If a Store was given, also records a `checkpoints` row with ``vcs_ref`` = the commit SHA.
        """
        self.ensure_repo()
        base_env = {**os.environ, **_CKPT_IDENTITY}

        with tempfile.TemporaryDirectory() as td:
            env = {**base_env, "GIT_INDEX_FILE": os.path.join(td, "index")}
            # Fresh empty index + add -A → tree of the entire current worktree (incl. agent's
            # not-yet-staged new files), minus .gitignore'd paths like node_modules.
            self._git("-c", "core.autocrlf=false", "add", "-A", env=env)
            tree = self._git("write-tree", env=env).stdout.strip()

        parent = self._latest_commit(session_id)
        msg = label or f"foreman ckpt {session_id}#{step_index}"
        commit_args = ["commit-tree", tree, "-m", msg]
        if parent:
            commit_args += ["-p", parent]
        commit = self._git(*commit_args, env=base_env).stdout.strip()

        ref = f"{CKPT_REF_PREFIX}/{session_id}/{step_index}"
        self._git("update-ref", ref, commit, env=base_env)

        if self.store is not None:
            from foreman.shared.events import utc_now_iso

            from ..store.models import Checkpoint
            self.store.add_checkpoint(Checkpoint(
                id=uuid.uuid4().hex,
                session_id=session_id, task_id=task_id, step_index=step_index,
                vcs_ref=commit, label=label, created_at=utc_now_iso(),
            ))
        return commit

    def _latest_commit(self, session_id: str) -> str | None:
        """Resolve this session's most recent checkpoint commit (highest step) to chain onto, or None."""
        res = self._git(
            "for-each-ref", "--format=%(objectname) %(refname)",
            f"{CKPT_REF_PREFIX}/{session_id}/", check=False,
        )
        best_step, best_sha = -1, None
        for line in res.stdout.splitlines():
            sha, _, refname = line.partition(" ")
            try:
                step = int(refname.rsplit("/", 1)[-1])
            except ValueError:
                continue
            if step > best_step:
                best_step, best_sha = step, sha
        return best_sha

    async def undo_to(self, vcs_ref: str) -> None:
        """Revert the workspace to a checkpoint (git reset --hard <ref>) and reset agent state. T2.3."""
        raise NotImplementedError("CheckpointManager.undo_to — roadmap T2.3")
