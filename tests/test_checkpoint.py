"""Tests for the checkpoint git-repo bootstrap (T2.1) and snapshotting (T2.2, §6.5)."""

from __future__ import annotations

import re
import subprocess

from foreman.client.core.checkpoint import CKPT_REF_PREFIX, CheckpointManager, ensure_repo
from foreman.client.store import Store

_SHA = re.compile(r"[0-9a-f]{40}")


def _git(ws, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(ws), capture_output=True, text=True, encoding="utf-8", check=check
    )


def _write(ws, name, content):
    (ws / name).write_text(content, encoding="utf-8")


def test_ensure_repo_inits_fresh_dir(tmp_path):
    ws = tmp_path / "proj"
    assert ensure_repo(ws) is True       # not a repo → inited
    assert (ws / ".git").exists()
    assert ensure_repo(ws) is False      # already a repo → no-op


def test_ensure_repo_leaves_existing_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    assert ensure_repo(tmp_path) is False


def test_manager_ensure_repo(tmp_path):
    ws = tmp_path / "w"
    assert CheckpointManager(ws).ensure_repo() is True
    assert (ws / ".git").exists()


# ── T2.2 snapshot ─────────────────────────────────────────────────────────────────────────────


async def test_snapshot_returns_commit_with_worktree_tree(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, "hi.txt", "hello")

    sha = await mgr.snapshot("s1", 0)

    assert _SHA.fullmatch(sha)
    # The shadow ref points at the snapshot commit...
    assert _git(ws, "rev-parse", f"{CKPT_REF_PREFIX}/s1/0").stdout.strip() == sha
    # ...and the commit's tree captures the (untracked) worktree file byte-for-byte.
    assert _git(ws, "cat-file", "-p", f"{sha}:hi.txt").stdout == "hello"


async def test_snapshot_does_not_touch_branch_or_index(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, "hi.txt", "hello")

    await mgr.snapshot("s1", 0)

    # No commit lands on any branch (HEAD is unborn) and the real index stays empty.
    assert _git(ws, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    assert _git(ws, "diff", "--cached", "--name-only").stdout.strip() == ""
    # The file is still just untracked in the working tree.
    assert _git(ws, "status", "--porcelain").stdout.strip() == "?? hi.txt"
    # Shadow ref is invisible to ordinary branch listing.
    assert _git(ws, "branch", "--list").stdout.strip() == ""


async def test_snapshot_chains_parent_into_a_timeline(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, "f.txt", "v1")
    c0 = await mgr.snapshot("s1", 0)
    _write(ws, "f.txt", "v2")
    c1 = await mgr.snapshot("s1", 1)

    assert c0 != c1
    assert _git(ws, "rev-parse", f"{c1}^").stdout.strip() == c0   # c1's parent is c0
    assert _git(ws, "cat-file", "-p", f"{c1}:f.txt").stdout == "v2"


async def test_snapshot_isolates_sessions(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, "f.txt", "x")
    await mgr.snapshot("sA", 0)
    cb = await mgr.snapshot("sB", 0)
    # sB's first checkpoint has no parent — it is not chained onto sA's timeline.
    assert _git(ws, "rev-parse", "--verify", f"{cb}^", check=False).returncode != 0


async def test_snapshot_honours_gitignore(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, ".gitignore", "ignored.txt\n")
    _write(ws, "ignored.txt", "secret")
    _write(ws, "kept.txt", "keep")

    sha = await mgr.snapshot("s1", 0)

    files = _git(ws, "ls-tree", "-r", "--name-only", sha).stdout.split()
    assert "kept.txt" in files and ".gitignore" in files
    assert "ignored.txt" not in files


async def test_snapshot_records_checkpoint_row(tmp_path):
    ws = tmp_path / "proj"
    store = Store(str(tmp_path / "t.db"))
    store.init()
    mgr = CheckpointManager(ws, store=store)
    mgr.ensure_repo()
    _write(ws, "hi.txt", "hello")

    sha = await mgr.snapshot("s1", 2, label="before edit", task_id="t9")

    rows = store.get_checkpoints("s1")
    assert len(rows) == 1
    row = rows[0]
    assert row.vcs_ref == sha
    assert row.step_index == 2
    assert row.label == "before edit"
    assert row.task_id == "t9"
    assert row.created_at
