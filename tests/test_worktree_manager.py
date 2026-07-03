from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from foreman.client.core.worktree_manager import WorktreeManager


def _git(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        pytest.skip("git executable is required for worktree manager tests")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "foreman@example.test")
    _git(repo, "config", "user.name", "Foreman Test")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _commit(path: Path, filename: str, text: str, message: str) -> str:
    (path / filename).write_text(text, encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def test_list_reports_main_locked_and_deleted_real_worktrees(tmp_path: Path):
    repo = _repo(tmp_path)
    locked = tmp_path / "locked"
    deleted = tmp_path / "deleted"
    _git(repo, "worktree", "add", "-b", "locked-branch", str(locked))
    _git(repo, "worktree", "add", "-b", "deleted-branch", str(deleted))
    _git(repo, "worktree", "lock", "--reason", "keep for test", str(locked))
    shutil.rmtree(deleted)

    result = WorktreeManager().list(repo)

    assert result["ok"] is True
    by_branch = {item["branch"]: item for item in result["worktrees"]}
    assert by_branch["main"]["exists"] is True
    assert by_branch["main"]["locked"] is False
    assert by_branch["locked-branch"]["exists"] is True
    assert by_branch["locked-branch"]["locked"] is True
    assert by_branch["locked-branch"]["locked_reason"] == "keep for test"
    assert by_branch["deleted-branch"]["exists"] is False
    assert by_branch["deleted-branch"]["resolved_path"]


def test_status_reports_clean_dirty_and_ahead_behind(tmp_path: Path):
    repo = _repo(tmp_path)
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree))
    feature_head = _commit(worktree, "feature.txt", "feature\n", "feature")
    _commit(repo, "main.txt", "main\n", "main")

    clean = WorktreeManager().status(worktree, "main")

    assert clean["ok"] is True
    assert clean["dirty"] is False
    assert clean["changed_files"] == []
    assert clean["ahead"] == 1
    assert clean["behind"] == 1
    assert clean["base_ref"] == "main"
    assert clean["head_sha"] == feature_head

    (worktree / "feature.txt").write_text("dirty\n", encoding="utf-8")
    dirty = WorktreeManager().status(worktree, "main")

    assert dirty["ok"] is True
    assert dirty["dirty"] is True
    assert dirty["changed_files"] == ["feature.txt"]
    assert dirty["ahead"] == 1
    assert dirty["behind"] == 1


def test_status_accepts_directory_symlink_when_available(tmp_path: Path):
    repo = _repo(tmp_path)
    worktree = tmp_path / "feature"
    link = tmp_path / "feature-link"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree))
    try:
        link.symlink_to(worktree, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    result = WorktreeManager().status(link, "main")

    assert result["ok"] is True
    assert result["is_symlink"] is True
    assert result["resolved_path"] == str(worktree.resolve(strict=False))


def test_errors_are_structured_for_missing_and_non_git_paths(tmp_path: Path, monkeypatch):
    manager = WorktreeManager()
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    not_repo = manager.list(plain)
    missing = manager.status(tmp_path / "missing", "main")

    assert not_repo["ok"] is False
    assert not_repo["error"] == "not_git_repo"
    assert not_repo["exists"] is True
    assert missing["ok"] is False
    assert missing["error"] == "missing_path"
    assert missing["exists"] is False


def test_status_returns_structured_error_for_bad_compare_ref(tmp_path: Path):
    repo = _repo(tmp_path)

    result = WorktreeManager().status(repo, "missing-ref")

    assert result["ok"] is False
    assert result["error"] == "compare_ref_not_found"
    assert result["detail"]
