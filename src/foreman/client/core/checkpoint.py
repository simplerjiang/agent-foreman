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

    def _scan_steps(self, session_id: str) -> list[tuple[int, str]]:
        """List this session's checkpoints as (step_index, commit_sha), highest step last."""
        res = self._git(
            "for-each-ref", "--format=%(objectname) %(refname)",
            f"{CKPT_REF_PREFIX}/{session_id}/", check=False,
        )
        steps: list[tuple[int, str]] = []
        for line in res.stdout.splitlines():
            sha, _, refname = line.partition(" ")
            try:
                step = int(refname.rsplit("/", 1)[-1])
            except ValueError:
                continue
            steps.append((step, sha))
        steps.sort()
        return steps

    def _latest_commit(self, session_id: str) -> str | None:
        """Resolve this session's most recent checkpoint commit (highest step) to chain onto, or None."""
        steps = self._scan_steps(session_id)
        return steps[-1][1] if steps else None

    def _next_step(self, session_id: str) -> int:
        """The step index a new checkpoint should take to land at the end of this session's timeline."""
        steps = self._scan_steps(session_id)
        return steps[-1][0] + 1 if steps else 0

    def resolve_step(self, session_id: str, step_index: int) -> str:
        """Resolve a session's step checkpoint to its commit SHA (the shadow ref's target)."""
        return self._git("rev-parse", f"{CKPT_REF_PREFIX}/{session_id}/{step_index}").stdout.strip()

    def diff(self, from_ref: str, to_ref: str | None = None) -> str:
        """Unified diff from checkpoint ``from_ref`` to ``to_ref`` (or the current worktree if None).

        This is the Reviewer's input (T2.7): the changes an agent made at a checkpoint. When
        ``to_ref`` is None we snapshot the live worktree through a throwaway index (same trick as
        ``snapshot``) so **new, not-yet-tracked files show up in the diff** — a plain ``git diff
        <commit>`` would miss them. Refs are resolved+verified to commits up front so junk/option-like
        input can't be mis-parsed as a flag. ``core.autocrlf=false`` keeps the diff byte-faithful.
        """
        base_env = {**os.environ, **_CKPT_IDENTITY}
        from_tree = self._git(
            "rev-parse", "--verify", f"{from_ref}^{{commit}}", env=base_env
        ).stdout.strip()
        if to_ref is None:
            with tempfile.TemporaryDirectory() as td:
                env = {**base_env, "GIT_INDEX_FILE": os.path.join(td, "index")}
                self._git("-c", "core.autocrlf=false", "add", "-A", env=env)
                to_tree = self._git("write-tree", env=env).stdout.strip()
        else:
            to_tree = self._git(
                "rev-parse", "--verify", f"{to_ref}^{{commit}}", env=base_env
            ).stdout.strip()
        return self._git(
            "-c", "core.autocrlf=false", "diff", from_tree, to_tree, env=base_env, check=False
        ).stdout

    async def undo_to(
        self,
        vcs_ref: str,
        session_id: str | None = None,
        step_index: int | None = None,
        task_id: str | None = None,
        redo_label: str = "before undo",
    ) -> str | None:
        """Revert the workspace to checkpoint ``vcs_ref``, byte-for-byte (§6.5②). Returns the redo SHA.

        Order matters: **first** snapshot the current state (so the undo is itself reversible — redo,
        "反悔的反悔") when a ``session_id`` is given, **then** restore the worktree to the target tree:
        modified files are overwritten, files deleted since the checkpoint are recreated, and files
        *created after* the checkpoint are deleted (§6.5② "第 N 步之后新建的文件删掉"). .gitignore'd paths
        (node_modules, secrets) are never touched — they were never in the snapshot to begin with.

        Only **workspace files** are reverted here; resetting the agent's session state to that step
        (§6.5② step 3) is wired in at the decision-loop layer (P4), which owns the Runner/adapter.
        Returns the redo checkpoint SHA (call ``undo_to`` on it to redo), or None if no session_id.
        """
        self.ensure_repo()
        # ① Point the current state too — so this undo can itself be undone (redo). Chains onto the
        # session timeline as the next step, so it shows up in the PC/phone history like any other.
        redo: str | None = None
        if session_id is not None:
            if step_index is None:
                step_index = self._next_step(session_id)
            redo = await self.snapshot(session_id, step_index, label=redo_label, task_id=task_id)
        # ② Restore the worktree to the target snapshot byte-for-byte.
        self._restore_worktree(vcs_ref)
        return redo

    def _ls_files_now(self) -> list[str]:
        """Paths git currently sees in the worktree (honouring .gitignore), via a throwaway index."""
        base_env = {**os.environ, **_CKPT_IDENTITY}
        with tempfile.TemporaryDirectory() as td:
            env = {**base_env, "GIT_INDEX_FILE": os.path.join(td, "index")}
            self._git("-c", "core.autocrlf=false", "add", "-A", env=env)
            out = self._git("ls-files", "-z", env=env).stdout
        return [f for f in out.split("\0") if f]

    def _restore_worktree(self, target: str) -> None:
        """Make the worktree match ``target``'s tree exactly: rewrite tracked files, drop new ones."""
        base_env = {**os.environ, **_CKPT_IDENTITY}
        # Resolve+verify the ref to a canonical commit SHA up front: normalises any ref to a commit
        # and rejects junk/option-like input (git errors out rather than mis-parsing it as a flag).
        target = self._git("rev-parse", "--verify", f"{target}^{{commit}}").stdout.strip()
        current = set(self._ls_files_now())
        tree_out = self._git("ls-tree", "-r", "--name-only", "-z", target).stdout
        in_target = {f for f in tree_out.split("\0") if f}

        # Rewrite every target file from the object store (modified → overwritten, deleted →
        # recreated) through a temp index so the real index/branch/HEAD stay untouched.
        with tempfile.TemporaryDirectory() as td:
            env = {**base_env, "GIT_INDEX_FILE": os.path.join(td, "index")}
            self._git("-c", "core.autocrlf=false", "read-tree", target, env=env)
            self._git("-c", "core.autocrlf=false", "checkout-index", "--all", "--force", env=env)

        # Delete files that exist now but not at the checkpoint (created after it). .gitignore'd
        # paths aren't in `current`, so they survive — undo never nukes node_modules/secrets.
        for rel in current - in_target:
            p = self.workspace / rel
            if p.is_file() or p.is_symlink():
                p.unlink()
                self._prune_empty_dirs(p.parent)

    def _prune_empty_dirs(self, start: Path) -> None:
        """Remove now-empty directories left by deletions, walking up but never past the workspace."""
        ws = self.workspace.resolve()
        d = Path(start).resolve()
        while d != ws and ws in d.parents:
            if d.name == ".git" or not d.is_dir() or any(d.iterdir()):
                break
            d.rmdir()
            d = d.parent
