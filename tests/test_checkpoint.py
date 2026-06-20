"""Tests for the checkpoint git-repo bootstrap (TASKS T2.1)."""

from __future__ import annotations

import subprocess

from foreman.client.core.checkpoint import CheckpointManager, ensure_repo


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
