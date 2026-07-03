"""Read-only git worktree discovery helpers for PM worktree tools."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


class WorktreeManager:
    def __init__(self, *, git: str = "git", timeout_s: int = 10) -> None:
        self.git = git
        self.timeout_s = timeout_s

    def list(self, main_workspace: str | Path) -> dict[str, Any]:
        workspace = _normalize_path(main_workspace)
        if not workspace.exists() or not workspace.is_dir():
            return _error("missing_path", path=workspace)
        root = self._git(workspace, "rev-parse", "--show-toplevel")
        if not root["ok"]:
            return _error("not_git_repo", path=workspace, detail=root["stderr"])
        repo_root = _normalize_path(root["stdout"].strip() or workspace)
        rows = self._git(repo_root, "worktree", "list", "--porcelain")
        if not rows["ok"]:
            return _error("worktree_list_failed", path=repo_root, detail=rows["stderr"])
        return {
            "ok": True,
            "main_workspace": str(workspace),
            "repo_root": str(repo_root),
            "worktrees": [_entry_dict(item) for item in _parse_worktree_porcelain(rows["stdout"])],
        }

    def status(self, worktree_path: str | Path, compare_to: str = "") -> dict[str, Any]:
        worktree = _normalize_path(worktree_path)
        if not worktree.exists() or not worktree.is_dir():
            return _error("missing_path", path=worktree)
        inside = self._git(worktree, "rev-parse", "--is-inside-work-tree")
        if not inside["ok"] or inside["stdout"].strip().lower() != "true":
            return _error("not_git_repo", path=worktree, detail=inside["stderr"])
        head = self._git(worktree, "rev-parse", "HEAD")
        if not head["ok"]:
            return _error("head_not_found", path=worktree, detail=head["stderr"])
        porcelain = self._git(worktree, "status", "--porcelain=v1")
        if not porcelain["ok"]:
            return _error("git_status_failed", path=worktree, detail=porcelain["stderr"])
        changed_files = _changed_files(porcelain["stdout"])
        ahead = behind = 0
        base_ref = str(compare_to or "").strip()
        if base_ref:
            counts = self._git(worktree, "rev-list", "--left-right", "--count", f"{base_ref}...HEAD")
            if not counts["ok"]:
                return _error("compare_ref_not_found", path=worktree, detail=counts["stderr"])
            behind, ahead = _ahead_behind(counts["stdout"])
        return {
            "ok": True,
            "path": str(worktree_path),
            "resolved_path": str(worktree),
            "exists": True,
            "is_symlink": Path(worktree_path).expanduser().is_symlink(),
            "dirty": bool(changed_files),
            "changed_files": changed_files,
            "ahead": ahead,
            "behind": behind,
            "base_ref": base_ref,
            "head_sha": head["stdout"].strip(),
        }

    def _git(self, cwd: Path, *args: str) -> dict[str, Any]:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        try:
            proc = subprocess.run(
                [self.git, "-C", str(cwd), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=env,
                **_subprocess_no_window_kwargs(),
            )
        except FileNotFoundError:
            return {"ok": False, "stdout": "", "stderr": "git executable not found"}
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "stdout": str(exc.stdout or ""),
                "stderr": "git command timed out",
            }
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr.strip(),
            "returncode": proc.returncode,
        }


def _parse_worktree_porcelain(raw: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:
                rows.append(current)
            current = {"path": value.strip()}
            continue
        if key == "branch":
            current["branch"] = _short_branch(value.strip())
        elif key == "HEAD":
            current["head_sha"] = value.strip()
        elif key == "locked":
            current["locked"] = "true"
            current["locked_reason"] = value.strip()
        elif key:
            current[key] = value.strip()
    if current:
        rows.append(current)
    return rows


def _entry_dict(item: dict[str, str]) -> dict[str, Any]:
    raw_path = item.get("path", "")
    path = Path(raw_path).expanduser()
    resolved = path.resolve(strict=False)
    return {
        "path": raw_path,
        "resolved_path": str(resolved),
        "branch": item.get("branch", ""),
        "head_sha": item.get("head_sha", ""),
        "locked": item.get("locked") == "true",
        "locked_reason": item.get("locked_reason", ""),
        "exists": path.exists(),
        "is_symlink": path.is_symlink(),
    }


def _changed_files(raw: str) -> list[str]:
    files: list[str] = []
    for line in raw.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip()
        if path and path not in files:
            files.append(path)
    return files


def _ahead_behind(raw: str) -> tuple[int, int]:
    parts = raw.split()
    if len(parts) < 2:
        return 0, 0
    return int(parts[0]), int(parts[1])


def _short_branch(ref: str) -> str:
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


def _normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _error(code: str, *, path: Path, detail: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "error": code,
        "path": str(path),
        "resolved_path": str(path.resolve(strict=False)),
        "exists": path.exists(),
        "is_symlink": path.is_symlink(),
        "detail": detail.strip(),
    }


def _subprocess_no_window_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
