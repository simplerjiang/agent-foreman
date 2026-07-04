from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from foreman.client.core.worktree_manager import WorktreeManager
from foreman.client.store.db import Store
from foreman.client.store.models import Session, WorktreeLease


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


def _store(tmp_path: Path) -> Store:
    st = Store(str(tmp_path / "foreman.db"))
    st.init()
    return st


def _commit(path: Path, filename: str, text: str, message: str) -> str:
    (path / filename).write_text(text, encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def _lease_context(
    store: Store,
    *,
    repo: Path,
    worktree: Path,
    base_sha: str,
    branch: str = "feature",
    session_id: str = "s1",
    task_id: str = "t1",
    status: str = "active",
    locked: bool = False,
) -> dict:
    store.add_session(
        Session(id=session_id, goal="goal", workspace=str(worktree), main_workspace=str(repo))
    )
    store.add_worktree_lease(
        WorktreeLease(
            id="lease-1",
            repo_root=str(repo),
            main_workspace=str(repo),
            worktree_path=str(worktree),
            branch=branch,
            base_ref="main",
            base_sha=base_sha,
            head_sha=base_sha,
            session_id=session_id,
            task_id=task_id,
            status=status,
            locked=locked,
        )
    )
    return {
        "store": store,
        "session_id": session_id,
        "task_id": task_id,
        "workspace": str(worktree),
        "main_workspace": str(repo),
        "worktree_roots": [str(worktree.parent)],
    }


def test_store_rejects_second_active_lease_for_path(tmp_path: Path):
    store = _store(tmp_path)
    path = str(tmp_path / "worktree")
    base = {
        "repo_root": str(tmp_path / "repo"),
        "main_workspace": str(tmp_path / "repo"),
        "worktree_path": path,
        "branch": "feature",
        "base_ref": "main",
        "base_sha": "base",
        "head_sha": "base",
        "task_id": "t1",
    }

    store.add_worktree_lease(WorktreeLease(id="write-1", session_id="s1", locked=True, **base))

    with pytest.raises(ValueError, match="active_worktree_lease_exists"):
        store.add_worktree_lease(
            WorktreeLease(id="read-1", session_id="s2", locked=False, task_id="t2", **{k: v for k, v in base.items() if k != "task_id"})
        )

    store.update_worktree_lease("write-1", status="released", locked=False)
    lease = store.add_worktree_lease(
        WorktreeLease(id="read-1", session_id="s2", locked=False, task_id="t2", **{k: v for k, v in base.items() if k != "task_id"})
    )
    assert lease.id == "read-1"


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


def test_diff_reports_clean_worktree_against_lease_base_sha(tmp_path: Path):
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    context = _lease_context(_store(tmp_path), repo=repo, worktree=worktree, base_sha=base_sha)

    result = WorktreeManager().diff(context)

    assert result["ok"] is True
    assert result["clean"] is True
    assert result["changed_files"] == []
    assert result["base_sha"] == base_sha
    assert result["compare_to"] == base_sha
    assert result["base_ref"] == "main"
    assert result["patch_artifact"] == ""


def test_diff_reports_untracked_deleted_renamed_and_stable_base_sha(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "rename_me.txt").write_text("rename\n", encoding="utf-8")
    _git(repo, "add", "rename_me.txt")
    _git(repo, "commit", "-m", "add rename base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    context = _lease_context(_store(tmp_path), repo=repo, worktree=worktree, base_sha=base_sha)

    _git(worktree, "mv", "rename_me.txt", "renamed.txt")
    (worktree / "file.txt").unlink()
    (worktree / "untracked.txt").write_text("new\nlines\n", encoding="utf-8")
    _commit(repo, "main_moved.txt", "main moved\n", "advance main")

    result = WorktreeManager().diff(context)

    by_path = {row["path"]: row for row in result["changed_files"]}
    assert result["clean"] is False
    assert result["compare_to"] == base_sha
    assert result["base_sha"] == base_sha
    assert by_path["file.txt"]["status"] == "deleted"
    assert by_path["renamed.txt"]["status"] == "renamed"
    assert by_path["renamed.txt"]["old_path"] == "rename_me.txt"
    assert by_path["untracked.txt"]["status"] == "untracked"
    assert by_path["untracked.txt"]["additions"] == 2
    assert result["files_changed"] == 3
    artifact = Path(result["patch_artifact"]).resolve(strict=True)
    assert (worktree / ".foreman" / "tool-logs").resolve(strict=False) in artifact.parents
    assert all(not row["path"].startswith(".foreman/") for row in result["changed_files"])


def test_diff_reports_binary_and_truncates_large_patch_artifact(tmp_path: Path):
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    context = _lease_context(_store(tmp_path), repo=repo, worktree=worktree, base_sha=base_sha)
    (worktree / "big.txt").write_text(
        "\n".join(f"line {i}" for i in range(800)),
        encoding="utf-8",
    )
    (worktree / "blob.bin").write_bytes(b"\0binary")

    result = WorktreeManager().diff(context, max_patch_chars=500)

    by_path = {row["path"]: row for row in result["changed_files"]}
    assert by_path["blob.bin"]["binary"] is True
    assert by_path["big.txt"]["additions"] == 800
    assert result["patch_truncated"] is True
    assert "worktree diff truncated" in result["patch"]
    assert Path(result["patch_artifact"]).is_file()


def test_merge_risk_check_reports_overlap_and_low_risk_different_files(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "add other")
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree_a = tmp_path / "feature-a"
    worktree_b = tmp_path / "feature-b"
    worktree_c = tmp_path / "feature-c"
    _git(repo, "worktree", "add", "-b", "feature-a", str(worktree_a), base_sha)
    _git(repo, "worktree", "add", "-b", "feature-b", str(worktree_b), base_sha)
    _git(repo, "worktree", "add", "-b", "feature-c", str(worktree_c), base_sha)
    store = _store(tmp_path)
    for session_id, lease_id, branch, path in (
        ("s1", "lease-a", "feature-a", worktree_a),
        ("s2", "lease-b", "feature-b", worktree_b),
        ("s3", "lease-c", "feature-c", worktree_c),
    ):
        store.add_session(Session(id=session_id, goal="g", workspace=str(path), main_workspace=str(repo)))
        store.add_worktree_lease(
            WorktreeLease(
                id=lease_id,
                repo_root=str(repo),
                main_workspace=str(repo),
                worktree_path=str(path),
                branch=branch,
                base_ref="HEAD",
                base_sha=base_sha,
                head_sha=base_sha,
                session_id=session_id,
                task_id=f"t-{session_id}",
                locked=True,
            )
        )
    (worktree_a / "file.txt").write_text("a\n", encoding="utf-8")
    (worktree_b / "file.txt").write_text("b\n", encoding="utf-8")
    (worktree_c / "other.txt").write_text("c\n", encoding="utf-8")
    manager = WorktreeManager()
    context = {
        "store": store,
        "session_id": "s1",
        "task_id": "t-s1",
        "main_workspace": str(repo),
        "worktree_roots": [str(tmp_path)],
    }

    high = manager.merge_risk_check(context, other_lease_id="lease-b")
    low = manager.merge_risk_check(context, other_lease_id="lease-c")

    assert high["ok"] is True
    assert high["risk_level"] == "high"
    assert high["overlapping_files"] == ["file.txt"]
    assert low["ok"] is True
    assert low["risk_level"] == "low"
    assert low["overlapping_files"] == []
    assert low["current_files"] == ["file.txt"]
    assert low["other_files"] == ["other.txt"]


def test_find_stale_leases_reports_dirty_without_auto_takeover(tmp_path: Path):
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    store = _store(tmp_path)
    context = _lease_context(
        store,
        repo=repo,
        worktree=worktree,
        base_sha=base_sha,
        locked=True,
    )
    store.update_worktree_lease("lease-1", last_seen_at="2000-01-01T00:00:00+00:00")
    (worktree / "file.txt").write_text("dirty\n", encoding="utf-8")

    result = WorktreeManager().find_stale_leases(context, stale_after_seconds=1)

    assert result["ok"] is True
    assert result["stale_count"] == 1
    stale = result["leases"][0]
    assert stale["lease_id"] == "lease-1"
    assert stale["dirty"] is True
    assert stale["takeover_allowed"] is False
    assert stale["auto_takeover_allowed"] is False
    assert stale["reason"] == "dirty_worktree"


def test_cleanup_dry_run_and_delete_clean_merged_owned_worktree(tmp_path: Path):
    repo = _repo(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", "main")
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    _git(worktree, "push", "origin", "feature")
    store = _store(tmp_path)
    context = _lease_context(store, repo=repo, worktree=worktree, base_sha=base_sha, locked=True)
    manager = WorktreeManager()

    dry_run = manager.cleanup(context, dry_run=True, reason="ready")

    assert dry_run["ok"] is True
    assert dry_run["safe"] is True
    assert dry_run["would_remove"] is True
    assert dry_run["removed"] is False
    assert dry_run["dirty"] is False
    assert dry_run["branch_merged"] is True
    assert worktree.exists()
    assert store.get_worktree_lease("lease-1").status == "active"
    assert store.get_worktree_lease("lease-1").locked is True
    dry_artifact = Path(dry_run["cleanup_artifact"]).resolve(strict=True)
    assert repo.resolve(strict=False) in dry_artifact.parents
    assert json.loads(dry_artifact.read_text(encoding="utf-8"))["lease"]["base_sha"] == base_sha

    removed = manager.cleanup(context, dry_run=False, reason="ready")

    assert removed["ok"] is True
    assert removed["safe"] is True
    assert removed["removed"] is True
    assert removed["would_remove"] is True
    assert not worktree.exists()
    assert store.get_worktree_lease("lease-1").status == "removed"
    assert store.get_worktree_lease("lease-1").locked is False
    assert Path(removed["cleanup_artifact"]).is_file()
    assert "feature" in _git(repo, "branch", "--list", "feature")
    assert "refs/heads/feature" in _git(repo, "ls-remote", "--heads", "origin", "feature")


def test_cleanup_allows_released_current_session_lease(tmp_path: Path):
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    _commit(worktree, "feature.txt", "released\n", "released")
    store = _store(tmp_path)
    context = _lease_context(
        store,
        repo=repo,
        worktree=worktree,
        base_sha=base_sha,
        status="released",
    )

    result = WorktreeManager().cleanup(context, dry_run=False)

    assert result["ok"] is True
    assert result["safe"] is True
    assert result["branch_merged"] is True
    assert result["removed"] is True
    assert result["lease_status"] == "removed"
    assert not worktree.exists()
    assert store.get_worktree_lease("lease-1").status == "removed"


def test_cleanup_rejects_dirty_worktree_and_preserves_artifact(tmp_path: Path):
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    context = _lease_context(_store(tmp_path), repo=repo, worktree=worktree, base_sha=base_sha)
    (worktree / "file.txt").write_text("dirty\n", encoding="utf-8")

    result = WorktreeManager().cleanup(context, dry_run=False)

    assert result["ok"] is True
    assert result["safe"] is False
    assert result["dirty"] is True
    assert result["requires_approval"] is True
    assert result["removed"] is False
    assert worktree.exists()
    artifact = Path(result["cleanup_artifact"]).resolve(strict=True)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["diff"]["base_sha"] == base_sha
    assert payload["diff"]["changed_files"][0]["path"] == "file.txt"


def test_cleanup_rejects_unmerged_branch_without_deleting(tmp_path: Path):
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    context = _lease_context(_store(tmp_path), repo=repo, worktree=worktree, base_sha=base_sha)
    _commit(worktree, "feature.txt", "feature\n", "feature")

    result = WorktreeManager().cleanup(context, dry_run=False)

    assert result["ok"] is True
    assert result["safe"] is False
    assert result["dirty"] is False
    assert result["branch_merged"] is False
    assert result["requires_approval"] is True
    assert result["error"] == "branch_unmerged"
    assert result["removed"] is False
    assert worktree.exists()


def test_cleanup_rejects_cross_session_and_outside_root_leases(tmp_path: Path):
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    outside = tmp_path / "outside"
    root = tmp_path / "allowed"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    store = _store(tmp_path)
    _lease_context(
        store,
        repo=repo,
        worktree=worktree,
        base_sha=base_sha,
        session_id="other-session",
        task_id="other-task",
    )

    cross_session = WorktreeManager().cleanup(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "workspace": str(worktree),
            "main_workspace": str(repo),
            "worktree_roots": [str(worktree.parent)],
        },
        dry_run=False,
    )

    assert cross_session["safe"] is False
    assert cross_session["requires_approval"] is True
    assert cross_session["error"] == "no_current_session_worktree"
    assert worktree.exists()

    outside_db = tmp_path / "outside-db"
    outside_db.mkdir()
    store2 = _store(outside_db)
    context = _lease_context(store2, repo=repo, worktree=outside, base_sha=base_sha)
    context["worktree_roots"] = [str(root)]

    outside_root = WorktreeManager().cleanup(context, dry_run=False)

    assert outside_root["safe"] is False
    assert outside_root["requires_approval"] is True
    assert outside_root["error"] == "path_outside_worktree_roots"


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


def test_plan_dry_run_does_not_mutate_git_db_or_filesystem(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    manager = WorktreeManager()
    before_worktrees = _git(repo, "worktree", "list", "--porcelain")
    before_leases = len(store.get_worktree_leases())

    result = manager.plan(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "worktree_roots": [],
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
        },
        goal="Fix Login & Tests",
    )

    assert result["ok"] is True
    assert result["decision"] == "create"
    assert result["proposed_branch"] == "foreman/s1/fix-login-tests"
    assert result["base_sha"] == _git(repo, "rev-parse", "HEAD")
    assert result["requires_approval"] is False
    assert result["risks"] == []
    assert Path(result["proposed_path"]) == tmp_path / ".foreman-worktrees" / "repo" / (
        "s1-fix-login-tests"
    )
    assert not Path(result["proposed_path"]).exists()
    assert _git(repo, "worktree", "list", "--porcelain") == before_worktrees
    assert len(store.get_worktree_leases()) == before_leases


def test_plan_reuses_existing_clean_owned_worktree(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    manager = WorktreeManager()
    context = {
        "store": store,
        "session_id": "s1",
        "task_id": "t1",
        "main_workspace": str(repo),
        "worktree_roots": [],
        "branch_prefix": "foreman/",
        "default_base_ref": "HEAD",
    }
    plan = manager.plan(context, goal="Reuse Me")
    path = Path(plan["proposed_path"])
    path.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", plan["proposed_branch"], str(path), "HEAD")
    head = _git(path, "rev-parse", "HEAD")
    store.add_worktree_lease(
        WorktreeLease(
            id="lease-1",
            repo_root=str(repo),
            main_workspace=str(repo),
            worktree_path=str(path),
            branch=plan["proposed_branch"],
            base_ref="HEAD",
            base_sha=head,
            head_sha=head,
            session_id="s1",
            task_id="t1",
        )
    )

    result = manager.plan(context, goal="Reuse Me")

    assert result["decision"] == "reuse"
    assert result["proposed_path"] == str(path.resolve(strict=False))
    assert result["lease_id"] == "lease-1"
    assert result["owner_session_id"] == "s1"
    assert result["owner_task_id"] == "t1"
    assert result["head_sha"] == head


def test_plan_rejects_unowned_and_dirty_existing_worktrees(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    manager = WorktreeManager()
    context = {
        "store": store,
        "session_id": "s1",
        "task_id": "t1",
        "main_workspace": str(repo),
        "branch_prefix": "foreman/",
        "default_base_ref": "HEAD",
    }
    plan = manager.plan(context, goal="Blocked Existing")
    path = Path(plan["proposed_path"])
    path.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-b", plan["proposed_branch"], str(path), "HEAD")
    head = _git(path, "rev-parse", "HEAD")

    unowned = manager.plan(context, goal="Blocked Existing")
    store.add_worktree_lease(
        WorktreeLease(
            id="lease-1",
            repo_root=str(repo),
            main_workspace=str(repo),
            worktree_path=str(path),
            branch=plan["proposed_branch"],
            base_ref="HEAD",
            base_sha=head,
            head_sha=head,
            session_id="s1",
            task_id="t1",
        )
    )
    (path / "file.txt").write_text("dirty\n", encoding="utf-8")
    dirty = manager.plan(context, goal="Blocked Existing")

    assert unowned["decision"] == "reject"
    assert unowned["error"] == "unowned_worktree"
    assert dirty["decision"] == "reject"
    assert dirty["error"] == "dirty_worktree"


def test_plan_rejects_custom_path_by_default_and_normalized_escape(tmp_path: Path):
    repo = _repo(tmp_path)
    manager = WorktreeManager()
    root = tmp_path / "roots"
    root.mkdir()
    context = {
        "session_id": "s1",
        "task_id": "t1",
        "main_workspace": str(repo),
        "worktree_roots": [root],
        "branch_prefix": "foreman/",
        "default_base_ref": "HEAD",
    }

    disabled = manager.plan(context, goal="Custom", custom_path=str(root / "custom"))
    escaped = manager.plan(
        {**context, "allow_custom_worktree_path": True},
        goal="Custom",
        custom_path=str(root / ".." / "outside"),
    )

    assert disabled["decision"] == "reject"
    assert disabled["error"] == "custom_path_disabled"
    assert escaped["decision"] == "reject"
    assert escaped["error"] == "path_outside_worktree_roots"


def test_plan_rejects_custom_path_symlink_escape_when_available(tmp_path: Path):
    repo = _repo(tmp_path)
    root = tmp_path / "roots"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    result = WorktreeManager().plan(
        {
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "worktree_roots": [root],
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
            "allow_custom_worktree_path": True,
        },
        goal="Custom",
        custom_path=str(link / "task"),
    )

    assert result["decision"] == "reject"
    assert result["error"] == "path_outside_worktree_roots"


def test_create_dry_run_does_not_mutate_git_db_or_filesystem(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    manager = WorktreeManager()
    before_worktrees = _git(repo, "worktree", "list", "--porcelain")

    result = manager.create(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
        },
        goal="Dry Run Create",
        dry_run=True,
    )

    assert result["decision"] == "create"
    assert result["dry_run"] is True
    assert result["created"] is False
    assert not Path(result["proposed_path"]).exists()
    assert store.get_worktree_leases() == []
    assert _git(repo, "worktree", "list", "--porcelain") == before_worktrees


def test_create_success_registers_worktree_and_active_lease(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)

    class RecordingManager(WorktreeManager):
        def __init__(self):
            super().__init__()
            self.commands: list[tuple[str, ...]] = []

        def _git(self, cwd: Path, *args: str) -> dict[str, object]:
            self.commands.append(args)
            return super()._git(cwd, *args)

    manager = RecordingManager()
    result = manager.create(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "worktree_roots": [tmp_path / "roots"],
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
        },
        goal="Create Feature",
    )

    assert result["ok"] is True
    assert result["decision"] == "create"
    assert result["created"] is True
    worktree = Path(result["path"])
    assert worktree.exists()
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "foreman/s1/create-feature"
    listed = WorktreeManager().list(repo)
    assert any(row["resolved_path"] == str(worktree.resolve(strict=False)) for row in listed["worktrees"])
    lease = store.get_active_worktree_lease(session_id="s1")
    assert lease is not None
    assert lease.worktree_path == str(worktree.resolve(strict=False))
    assert lease.branch == "foreman/s1/create-feature"
    assert lease.base_sha == result["base_sha"]
    assert lease.head_sha == result["head_sha"]
    assert lease.session_id == "s1"
    assert lease.task_id == "t1"
    assert lease.locked is True
    forbidden = {"fetch", "pull", "push", "merge"}
    assert not any(args and args[0] in forbidden for args in manager.commands)


def test_create_reuses_existing_clean_owned_worktree(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    manager = WorktreeManager()
    context = {
        "store": store,
        "session_id": "s1",
        "task_id": "t1",
        "main_workspace": str(repo),
        "branch_prefix": "foreman/",
        "default_base_ref": "HEAD",
    }
    created = manager.create(context, goal="Reuse Create")

    reused = manager.create(context, goal="Reuse Create")

    assert created["created"] is True
    assert reused["decision"] == "reuse"
    assert reused["created"] is False
    assert reused["lease_id"] == created["lease_id"]
    assert len(store.get_worktree_leases(status="active")) == 1


def test_create_rejects_custom_path_target_exists_bad_base_and_bad_branch(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    manager = WorktreeManager()
    context = {
        "store": store,
        "session_id": "s1",
        "task_id": "t1",
        "main_workspace": str(repo),
        "branch_prefix": "foreman/",
        "default_base_ref": "HEAD",
    }
    planned = manager.plan(context, goal="Existing Target")
    Path(planned["proposed_path"]).mkdir(parents=True)

    custom = manager.create(context, goal="Custom", custom_path=str(tmp_path / "manual"))
    existing = manager.create(context, goal="Existing Target")
    missing_base = manager.create(context, goal="Missing Base", base_ref="missing-ref")
    bad_branch = manager.create({**context, "branch_prefix": "bad prefix/"}, goal="Bad Branch")

    assert custom["decision"] == "reject" and custom["error"] == "custom_path_disabled"
    assert existing["decision"] == "reject" and existing["error"] == "path_exists_unregistered"
    assert missing_base["decision"] == "reject" and missing_base["error"] == "base_ref_not_found"
    assert bad_branch["decision"] == "reject" and bad_branch["error"] == "invalid_branch"
    assert store.get_worktree_leases() == []


def test_create_git_add_failure_does_not_write_lease_or_leave_path(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)

    class FailingAddManager(WorktreeManager):
        def _git(self, cwd: Path, *args: str) -> dict[str, object]:
            if args[:2] == ("worktree", "add"):
                return {"ok": False, "stdout": "", "stderr": "simulated add failure"}
            return super()._git(cwd, *args)

    result = FailingAddManager().create(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "worktree_roots": [tmp_path / "roots"],
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
        },
        goal="Fail Add",
    )

    assert result["ok"] is False
    assert result["error"] == "git_worktree_add_failed"
    assert not Path(result["proposed_path"]).exists()
    assert not (tmp_path / "roots").exists()
    assert store.get_worktree_leases() == []


def test_create_rejects_custom_path_symlink_escape_when_available(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    root = tmp_path / "roots"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    result = WorktreeManager().create(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "worktree_roots": [root],
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
            "allow_custom_worktree_path": True,
        },
        goal="Custom",
        custom_path=str(link / "task"),
    )

    assert result["decision"] == "reject"
    assert result["error"] == "path_outside_worktree_roots"
    assert store.get_worktree_leases() == []


def test_bind_session_updates_session_workspace_and_preserves_main_workspace(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal", workspace=str(repo), main_workspace=str(repo)))
    manager = WorktreeManager()
    created = manager.create(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
        },
        goal="Bind Me",
    )

    bound = manager.bind_session(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "workspace": str(repo),
            "main_workspace": str(repo),
        },
        lease_id=created["lease_id"],
        reason="use isolated workspace",
    )
    session = store.get_session("s1")

    assert bound["ok"] is True
    assert bound["workspace_switched"] is True
    assert session is not None
    assert session.workspace == created["path"]
    assert session.main_workspace == str(repo)
    assert bound["main_workspace"] == str(repo)
    assert bound["owner_session_id"] == "s1"
    assert bound["owner_task_id"] == "t1"


def test_create_bind_session_uses_same_binding_logic(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal", workspace=str(repo), main_workspace=str(repo)))

    result = WorktreeManager().create(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
        },
        goal="Create Bound",
        bind_session=True,
    )

    assert result["ok"] is True
    assert result["created"] is True
    assert result["session_bound"] is True
    assert result["workspace_switched"] is True
    assert store.get_session("s1").workspace == result["workspace"]


def test_bind_session_blocks_dirty_write_and_allows_read_only_after_release(tmp_path: Path):
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), base_sha)
    store = _store(tmp_path)
    store.add_session(Session(id="writer", goal="g", workspace=str(repo), main_workspace=str(repo)))
    store.add_session(Session(id="reader", goal="g", workspace=str(repo), main_workspace=str(repo)))
    common = {
        "repo_root": str(repo),
        "main_workspace": str(repo),
        "worktree_path": str(worktree.resolve(strict=False)),
        "branch": "feature",
        "base_ref": "HEAD",
        "base_sha": base_sha,
        "head_sha": base_sha,
    }
    store.add_worktree_lease(
        WorktreeLease(
            id="writer-lease",
            session_id="writer",
            task_id="tw",
            locked=True,
            **common,
        )
    )
    (worktree / "file.txt").write_text("dirty\n", encoding="utf-8")
    manager = WorktreeManager()

    writer = manager.bind_session(
        {
            "store": store,
            "session_id": "writer",
            "task_id": "tw",
            "main_workspace": str(repo),
            "worktree_roots": [str(tmp_path)],
        },
        lease_id="writer-lease",
    )
    assert writer["error"] == "dirty_worktree"

    store.update_worktree_lease("writer-lease", status="released", locked=False)
    store.add_worktree_lease(
        WorktreeLease(
            id="reader-lease",
            session_id="reader",
            task_id="tr",
            locked=False,
            **common,
        )
    )
    reader = manager.bind_session(
        {
            "store": store,
            "session_id": "reader",
            "task_id": "tr",
            "main_workspace": str(repo),
            "worktree_roots": [str(tmp_path)],
        },
        lease_id="reader-lease",
    )

    assert reader["ok"] is True
    assert reader["read_only"] is True
    assert reader["write_lock"] is False
    assert reader["dirty"] is True
    assert store.get_session("reader").workspace == str(worktree.resolve(strict=False))


def test_bind_session_rejects_wrong_session_missing_unregistered_and_dirty(tmp_path: Path):
    repo = _repo(tmp_path)
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal", workspace=str(repo), main_workspace=str(repo)))
    store.add_session(Session(id="s2", goal="goal", workspace=str(repo), main_workspace=str(repo)))
    manager = WorktreeManager()
    created = manager.create(
        {
            "store": store,
            "session_id": "s1",
            "task_id": "t1",
            "main_workspace": str(repo),
            "branch_prefix": "foreman/",
            "default_base_ref": "HEAD",
        },
        goal="Reject Bind",
    )
    wrong_session = manager.bind_session(
        {"store": store, "session_id": "s2", "task_id": "t2", "main_workspace": str(repo)},
        lease_id=created["lease_id"],
    )
    missing = store.add_worktree_lease(
        WorktreeLease(
            id="missing",
            repo_root=str(repo),
            main_workspace=str(repo),
            worktree_path=str(tmp_path / "missing"),
            branch="foreman/s1/missing",
            base_ref="HEAD",
            base_sha=_git(repo, "rev-parse", "HEAD"),
            head_sha=_git(repo, "rev-parse", "HEAD"),
            session_id="s1",
            task_id="t1",
        )
    )
    other_repo = tmp_path / ".foreman-worktrees" / "repo" / "other"
    other_repo.mkdir(parents=True)
    _git(other_repo, "init", "-b", "main")
    _git(other_repo, "config", "user.email", "foreman@example.test")
    _git(other_repo, "config", "user.name", "Foreman Test")
    (other_repo / "file.txt").write_text("other\n", encoding="utf-8")
    _git(other_repo, "add", "file.txt")
    _git(other_repo, "commit", "-m", "other")
    unregistered = store.add_worktree_lease(
        WorktreeLease(
            id="unregistered",
            repo_root=str(repo),
            main_workspace=str(repo),
            worktree_path=str(other_repo),
            branch="foreman/s1/unregistered",
            base_ref="HEAD",
            base_sha=_git(repo, "rev-parse", "HEAD"),
            head_sha=_git(other_repo, "rev-parse", "HEAD"),
            session_id="s1",
            task_id="t1",
        )
    )
    (Path(created["path"]) / "file.txt").write_text("dirty\n", encoding="utf-8")
    dirty = manager.bind_session(
        {"store": store, "session_id": "s1", "task_id": "t1", "main_workspace": str(repo)},
        lease_id=created["lease_id"],
    )

    assert wrong_session["error"] == "lease_session_mismatch"
    assert manager.bind_session(
        {"store": store, "session_id": "s1", "task_id": "t1", "main_workspace": str(repo)},
        lease_id=missing.id,
    )["error"] == "worktree_missing"
    assert manager.bind_session(
        {"store": store, "session_id": "s1", "task_id": "t1", "main_workspace": str(repo)},
        lease_id=unregistered.id,
    )["error"] == "worktree_not_registered"
    assert dirty["error"] == "dirty_worktree"
    assert store.get_session("s1").workspace == str(repo)
