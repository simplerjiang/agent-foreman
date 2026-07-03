"""Read-only git worktree discovery helpers for PM worktree tools."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from foreman.client.store.models import WorktreeLease
from foreman.shared.config import default_worktree_root


_SAFE_TOKEN_RE = re.compile(r"[^a-z0-9._-]+")


class WorktreeManager:
    def __init__(self, *, git: str = "git", timeout_s: int = 10) -> None:
        self.git = git
        self.timeout_s = timeout_s

    def plan(
        self,
        context: dict[str, Any],
        *,
        goal: str = "",
        slug: str = "",
        base_ref: str = "",
        reuse_policy: str = "reuse_clean_owned",
        custom_path: str = "",
    ) -> dict[str, Any]:
        main_workspace = _normalize_path(
            context.get("main_workspace") or context.get("workspace") or "."
        )
        branch_prefix = str(context.get("branch_prefix") or "foreman/").strip() or "foreman/"
        base_label = str(base_ref or context.get("default_base_ref") or "HEAD").strip() or "HEAD"
        session_id = str(context.get("session_id") or "").strip()
        task_id = str(context.get("task_id") or "").strip()
        work_slug = _slug(slug or goal or "worktree")
        session_slug = _slug(session_id or "session")
        proposed_branch = f"{branch_prefix}{session_slug}/{work_slug}"
        out = _plan_base(
            main_workspace=main_workspace,
            proposed_path=Path(str(custom_path or "")) if custom_path else Path(),
            proposed_branch=proposed_branch,
            base_ref=base_label,
        )
        if reuse_policy not in {"reuse_clean_owned", "never"}:
            return _reject(out, "invalid_reuse_policy")
        if not main_workspace.exists() or not main_workspace.is_dir():
            return _reject(out, "missing_main_workspace")
        repo_root_data = self._git(main_workspace, "rev-parse", "--show-toplevel")
        if not repo_root_data["ok"]:
            return _reject(out, "not_git_repo", detail=repo_root_data["stderr"])
        repo_root = _normalize_path(repo_root_data["stdout"].strip() or main_workspace)
        out["repo_root"] = str(repo_root)
        roots = _worktree_roots(repo_root, context.get("worktree_roots"))
        if not roots:
            return _reject(out, "missing_worktree_roots")
        custom_raw = str(custom_path or "").strip()
        if custom_raw:
            if not bool(context.get("allow_custom_worktree_path")):
                out["proposed_path"] = str(Path(custom_raw).expanduser())
                return _reject(out, "custom_path_disabled")
            proposed_path, path_error = _validate_candidate_path(custom_raw, roots)
            out["proposed_path"] = str(proposed_path)
            if path_error:
                return _reject(out, path_error)
        else:
            proposed_path = roots[0] / f"{session_slug}-{work_slug}"
            out["proposed_path"] = str(proposed_path)
            path_error = _path_root_error(proposed_path, roots)
            if path_error:
                return _reject(out, path_error)
        base_sha_data = self._git(repo_root, "rev-parse", "--verify", f"{base_label}^{{commit}}")
        if not base_sha_data["ok"]:
            return _reject(out, "base_ref_not_found", detail=base_sha_data["stderr"])
        out["base_sha"] = base_sha_data["stdout"].strip()
        branch_check = self._git(repo_root, "check-ref-format", "--branch", proposed_branch)
        if not branch_check["ok"]:
            return _reject(out, "invalid_branch", detail=branch_check["stderr"])
        list_data = self.list(repo_root)
        if not list_data.get("ok"):
            return _reject(
                out,
                str(list_data.get("error") or "worktree_list_failed"),
                detail=str(list_data.get("detail") or ""),
            )
        rows = [row for row in list_data.get("worktrees", []) if isinstance(row, dict)]
        existing = _find_worktree(rows, proposed_path, proposed_branch)
        store = context.get("store")
        if existing is not None:
            existing_path = _normalize_path(existing.get("resolved_path") or existing.get("path") or "")
            out["proposed_path"] = str(existing_path)
            return self._plan_existing(
                out,
                store=store,
                path=existing_path,
                session_id=session_id,
                task_id=task_id,
                compare_to=str(out["base_sha"]),
                reuse_policy=reuse_policy,
            )
        if proposed_path.exists():
            return _reject(out, "path_exists_unregistered")
        branch_ref = self._git(repo_root, "rev-parse", "--verify", f"refs/heads/{proposed_branch}")
        if branch_ref["ok"]:
            return _reject(out, "branch_exists")
        out["decision"] = "create"
        out["ok"] = True
        return out

    def create(
        self,
        context: dict[str, Any],
        *,
        goal: str = "",
        slug: str = "",
        base_ref: str = "",
        reuse_policy: str = "reuse_clean_owned",
        custom_path: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        planned = self.plan(
            context,
            goal=goal,
            slug=slug,
            base_ref=base_ref,
            reuse_policy=reuse_policy,
            custom_path=custom_path,
        )
        if dry_run or planned.get("decision") != "create":
            return {**planned, "dry_run": bool(dry_run), "created": False}
        session_id = str(context.get("session_id") or "").strip()
        task_id = str(context.get("task_id") or "").strip()
        if not session_id:
            return _create_error(planned, "missing_session")
        if not task_id:
            return _create_error(planned, "missing_task")
        store = context.get("store")
        add_lease = getattr(store, "add_worktree_lease", None)
        if store is None or not callable(add_lease):
            return _create_error(planned, "worktree_store_unavailable")
        repo_root = _normalize_path(planned.get("repo_root") or planned.get("main_workspace") or ".")
        worktree_path = _normalize_path(planned.get("proposed_path") or "")
        branch = str(planned.get("proposed_branch") or "").strip()
        base_label = str(planned.get("base_ref") or "").strip()
        if not branch.startswith(str(context.get("branch_prefix") or "")):
            return _create_error(planned, "invalid_branch_prefix")
        parent = worktree_path.parent
        parent_existed = parent.exists()
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _create_error(planned, "worktree_parent_create_failed", detail=str(exc))
        added = self._git(repo_root, "worktree", "add", "-b", branch, str(worktree_path), base_label)
        if not added["ok"]:
            _rollback_failed_create_path(worktree_path, parent, parent_existed)
            return _create_error(planned, "git_worktree_add_failed", detail=added["stderr"])
        verified = self.list(repo_root)
        if not verified.get("ok"):
            return _create_error(
                planned,
                str(verified.get("error") or "worktree_verify_failed"),
                detail=str(verified.get("detail") or ""),
            )
        rows = [row for row in verified.get("worktrees", []) if isinstance(row, dict)]
        registered = _find_worktree(rows, worktree_path, branch)
        if registered is None:
            return _create_error(planned, "worktree_not_registered")
        status = self.status(worktree_path, str(planned.get("base_sha") or ""))
        if not status.get("ok"):
            return _create_error(
                planned,
                str(status.get("error") or "worktree_status_failed"),
                detail=str(status.get("detail") or ""),
            )
        lease = add_lease(
            WorktreeLease(
                id="",
                repo_root=str(repo_root),
                main_workspace=str(planned.get("main_workspace") or repo_root),
                worktree_path=str(worktree_path),
                branch=branch,
                base_ref=base_label,
                base_sha=str(planned.get("base_sha") or ""),
                head_sha=str(status.get("head_sha") or ""),
                session_id=session_id,
                task_id=task_id,
                dirty=bool(status.get("dirty")),
                locked=bool(registered.get("locked")),
            )
        )
        return {
            **planned,
            "ok": True,
            "decision": "create",
            "created": True,
            "dry_run": False,
            "path": str(worktree_path),
            "workspace": str(worktree_path),
            "head_sha": str(status.get("head_sha") or ""),
            "lease_id": str(getattr(lease, "id", "") or ""),
            "owner_session_id": session_id,
            "owner_task_id": task_id,
            "lease_status": str(getattr(lease, "status", "") or ""),
        }

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

    def _plan_existing(
        self,
        out: dict[str, Any],
        *,
        store: Any,
        path: Path,
        session_id: str,
        task_id: str,
        compare_to: str,
        reuse_policy: str,
    ) -> dict[str, Any]:
        lease = _active_lease_for_path(store, path)
        if not _lease_owned_by(lease, session_id, task_id):
            return _reject(out, "unowned_worktree")
        status = self.status(path, compare_to)
        if not status.get("ok"):
            return _reject(
                out,
                str(status.get("error") or "worktree_status_failed"),
                detail=str(status.get("detail") or ""),
            )
        if bool(status.get("dirty")) or bool(getattr(lease, "dirty", False)):
            return _reject(out, "dirty_worktree")
        if reuse_policy != "reuse_clean_owned":
            return _reject(out, "worktree_exists")
        out.update(
            {
                "ok": True,
                "decision": "reuse",
                "head_sha": str(status.get("head_sha") or ""),
                "lease_id": str(getattr(lease, "id", "") or ""),
                "owner_session_id": str(getattr(lease, "session_id", "") or ""),
                "owner_task_id": str(getattr(lease, "task_id", "") or ""),
                "lease_status": str(getattr(lease, "status", "") or ""),
            }
        )
        return out


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


def _plan_base(
    *,
    main_workspace: Path,
    proposed_path: Path,
    proposed_branch: str,
    base_ref: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "decision": "reject",
        "main_workspace": str(main_workspace),
        "repo_root": "",
        "proposed_path": str(proposed_path) if str(proposed_path) != "." else "",
        "proposed_branch": proposed_branch,
        "base_ref": base_ref,
        "base_sha": "",
        "head_sha": "",
        "requires_approval": False,
        "risks": [],
        "error": "",
        "detail": "",
    }


def _reject(out: dict[str, Any], code: str, *, detail: str = "") -> dict[str, Any]:
    result = dict(out)
    result["ok"] = True
    result["decision"] = "reject"
    result["error"] = code
    result["risks"] = [code]
    result["detail"] = detail.strip()
    return result


def _create_error(out: dict[str, Any], code: str, *, detail: str = "") -> dict[str, Any]:
    result = _reject(out, code, detail=detail)
    result["ok"] = False
    result["created"] = False
    return result


def _slug(value: str, *, max_len: int = 48) -> str:
    lowered = str(value or "").strip().lower()
    normalized = _SAFE_TOKEN_RE.sub("-", lowered).strip(".-_")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return (normalized[:max_len].strip(".-_") or "worktree")


def _worktree_roots(repo_root: Path, raw_roots: object) -> list[Path]:
    roots: list[Path] = []
    if isinstance(raw_roots, (list, tuple, set)):
        roots = [Path(str(root)).expanduser().resolve(strict=False) for root in raw_roots if str(root).strip()]
    return roots or [default_worktree_root(repo_root).resolve(strict=False)]


def _validate_candidate_path(raw: str, roots: list[Path]) -> tuple[Path, str]:
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            return candidate, "custom_path_must_be_absolute"
        resolved = candidate.resolve(strict=False)
    except (OSError, ValueError):
        return Path(raw), "invalid_path"
    return resolved, _path_root_error(resolved, roots)


def _path_root_error(path: Path, roots: list[Path]) -> str:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, ValueError):
        return "invalid_path"
    for root in roots:
        root_resolved = root.resolve(strict=False)
        if resolved == root_resolved or root_resolved in resolved.parents:
            return ""
    return "path_outside_worktree_roots"


def _find_worktree(
    rows: list[dict[str, Any]], proposed_path: Path, proposed_branch: str
) -> dict[str, Any] | None:
    resolved = proposed_path.resolve(strict=False)
    branch_match: dict[str, Any] | None = None
    for row in rows:
        try:
            row_path = Path(str(row.get("resolved_path") or row.get("path") or "")).resolve(
                strict=False
            )
        except (OSError, ValueError):
            continue
        if row_path == resolved:
            return row
        if row.get("branch") == proposed_branch:
            branch_match = row
    return branch_match


def _active_lease_for_path(store: Any, path: Path) -> Any | None:
    if store is None:
        return None
    get_active = getattr(store, "get_active_worktree_lease", None)
    if callable(get_active):
        for candidate in {str(path), str(path.resolve(strict=False))}:
            lease = get_active(worktree_path=candidate)
            if lease is not None:
                return lease
    get_many = getattr(store, "get_worktree_leases", None)
    if not callable(get_many):
        return None
    try:
        leases = get_many(status="active")
    except TypeError:
        leases = get_many()
    for lease in leases or []:
        try:
            lease_path = Path(str(getattr(lease, "worktree_path", "") or "")).resolve(strict=False)
        except (OSError, ValueError):
            continue
        if lease_path == path.resolve(strict=False):
            return lease
    return None


def _lease_owned_by(lease: Any, session_id: str, task_id: str) -> bool:
    if lease is None:
        return False
    if str(getattr(lease, "session_id", "") or "") != session_id:
        return False
    lease_task_id = str(getattr(lease, "task_id", "") or "")
    return not task_id or lease_task_id == task_id


def _rollback_failed_create_path(worktree_path: Path, parent: Path, parent_existed: bool) -> None:
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    if not parent_existed and parent.exists():
        shutil.rmtree(parent, ignore_errors=True)


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
