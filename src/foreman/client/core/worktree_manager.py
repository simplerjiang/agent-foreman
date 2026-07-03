"""Read-only git worktree discovery helpers for PM worktree tools."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foreman.client.store.models import WorktreeLease
from foreman.shared.config import default_worktree_root
from foreman.shared.events import utc_now_iso


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
        bind_session: bool = False,
    ) -> dict[str, Any]:
        planned = self.plan(
            context,
            goal=goal,
            slug=slug,
            base_ref=base_ref,
            reuse_policy=reuse_policy,
            custom_path=custom_path,
        )
        if dry_run:
            return {**planned, "dry_run": bool(dry_run), "created": False}
        if planned.get("decision") == "reuse":
            out = {**planned, "dry_run": False, "created": False}
            if bind_session:
                return _with_bind_result(out, self.bind_session(context, lease_id=str(out.get("lease_id") or "")))
            return out
        if planned.get("decision") != "create":
            return {**planned, "dry_run": False, "created": False}
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
                locked=True,
            )
        )
        out = {
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
            "read_only": False,
            "write_lock": True,
        }
        if bind_session:
            return _with_bind_result(out, self.bind_session(context, lease_id=str(getattr(lease, "id", "") or "")))
        out["session_bound"] = False
        out["workspace_switched"] = False
        return out

    def bind_session(
        self,
        context: dict[str, Any],
        *,
        lease_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        session_id = str(context.get("session_id") or "").strip()
        if not session_id:
            return _bind_error("missing_session", lease_id=lease_id)
        store = context.get("store")
        get_lease = getattr(store, "get_worktree_lease", None)
        get_session = getattr(store, "get_session", None)
        update_session = getattr(store, "update_session", None)
        if store is None or not callable(get_lease) or not callable(get_session) or not callable(update_session):
            return _bind_error("worktree_store_unavailable", lease_id=lease_id)
        lease = get_lease(str(lease_id or "").strip())
        if lease is None:
            return _bind_error("lease_not_found", lease_id=lease_id)
        if str(getattr(lease, "status", "") or "") != "active":
            return _bind_error("lease_not_active", lease=lease)
        if str(getattr(lease, "session_id", "") or "") != session_id:
            return _bind_error("lease_session_mismatch", lease=lease)
        session = get_session(session_id)
        if session is None:
            return _bind_error("session_not_found", lease=lease)
        worktree_path = _normalize_path(getattr(lease, "worktree_path", "") or "")
        if not worktree_path.exists() or not worktree_path.is_dir():
            return _bind_error("worktree_missing", lease=lease)
        main_workspace = str(
            getattr(session, "main_workspace", "")
            or getattr(lease, "main_workspace", "")
            or context.get("main_workspace")
            or context.get("workspace")
            or ""
        ).strip()
        if not main_workspace:
            return _bind_error("missing_main_workspace", lease=lease)
        root_error = _path_root_error(worktree_path, _worktree_roots(Path(main_workspace), context.get("worktree_roots")))
        if root_error:
            return _bind_error(root_error, lease=lease)
        listed = self.list(main_workspace)
        if not listed.get("ok"):
            return _bind_error(str(listed.get("error") or "worktree_list_failed"), lease=lease, detail=str(listed.get("detail") or ""))
        rows = [row for row in listed.get("worktrees", []) if isinstance(row, dict)]
        registered = _find_worktree(rows, worktree_path, str(getattr(lease, "branch", "") or ""))
        if registered is None:
            return _bind_error("worktree_not_registered", lease=lease)
        write_lock = bool(getattr(lease, "locked", False))
        read_only = not write_lock
        if write_lock:
            conflict = _active_write_lease_for_path(
                store,
                worktree_path,
                exclude_id=str(getattr(lease, "id", "") or ""),
            )
            if conflict is not None:
                return _bind_error(
                    "workspace_write_locked",
                    lease=lease,
                    detail=str(getattr(conflict, "id", "") or ""),
                )
        status = self.status(worktree_path, str(getattr(lease, "base_sha", "") or getattr(lease, "base_ref", "") or ""))
        if not status.get("ok"):
            return _bind_error(str(status.get("error") or "worktree_status_failed"), lease=lease, detail=str(status.get("detail") or ""))
        if write_lock and (bool(status.get("dirty")) or bool(getattr(lease, "dirty", False))):
            return _bind_error("dirty_worktree", lease=lease)
        updated = update_session(
            session_id,
            workspace=str(worktree_path),
            main_workspace=main_workspace,
        )
        if updated is None:
            return _bind_error("session_update_failed", lease=lease)
        update_lease = getattr(store, "update_worktree_lease", None)
        if callable(update_lease):
            update_lease(
                str(getattr(lease, "id", "") or ""),
                head_sha=str(status.get("head_sha") or ""),
                dirty=bool(status.get("dirty")),
                locked=write_lock,
                last_seen_at=utc_now_iso(),
            )
        return {
            "ok": True,
            "bound": True,
            "session_bound": True,
            "workspace_switched": True,
            "lease_id": str(getattr(lease, "id", "") or ""),
            "workspace": str(worktree_path),
            "path": str(worktree_path),
            "main_workspace": main_workspace,
            "branch": str(getattr(lease, "branch", "") or registered.get("branch") or ""),
            "base_ref": str(getattr(lease, "base_ref", "") or ""),
            "base_sha": str(getattr(lease, "base_sha", "") or ""),
            "head_sha": str(status.get("head_sha") or ""),
            "owner_session_id": session_id,
            "owner_task_id": str(getattr(lease, "task_id", "") or ""),
            "lease_status": str(getattr(lease, "status", "") or ""),
            "dirty": bool(status.get("dirty")),
            "read_only": read_only,
            "write_lock": write_lock,
            "reason": str(reason or ""),
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

    def diff(
        self,
        context: dict[str, Any],
        *,
        max_patch_chars: int = 20000,
        include_patch: bool = True,
    ) -> dict[str, Any]:
        session_id = str(context.get("session_id") or "").strip()
        store = context.get("store")
        lease = _active_lease_for_session(store, session_id)
        if lease is None:
            return _clean_diff_result(error="no_active_worktree_lease")
        if str(getattr(lease, "status", "") or "") != "active":
            return _clean_diff_result(lease=lease, error="lease_not_active")
        return self._diff_for_lease(
            context,
            lease,
            max_patch_chars=max_patch_chars,
            include_patch=include_patch,
        )

    def _diff_for_lease(
        self,
        context: dict[str, Any],
        lease: Any,
        *,
        max_patch_chars: int = 20000,
        include_patch: bool = True,
        write_patch_artifact: bool = True,
    ) -> dict[str, Any]:
        worktree = _normalize_path(getattr(lease, "worktree_path", "") or "")
        main_workspace = _normalize_path(
            getattr(lease, "main_workspace", "")
            or context.get("main_workspace")
            or context.get("workspace")
            or "."
        )
        root_error = _path_root_error(
            worktree,
            _worktree_roots(main_workspace, context.get("worktree_roots")),
        )
        if root_error:
            return _error(root_error, path=worktree)
        if not worktree.exists() or not worktree.is_dir():
            return _error("missing_path", path=worktree)
        listed = self.list(main_workspace)
        if not listed.get("ok"):
            return _error(
                str(listed.get("error") or "worktree_list_failed"),
                path=main_workspace,
                detail=str(listed.get("detail") or ""),
            )
        rows = [row for row in listed.get("worktrees", []) if isinstance(row, dict)]
        registered = _find_worktree(rows, worktree, str(getattr(lease, "branch", "") or ""))
        if registered is None:
            return _error("worktree_not_registered", path=worktree)
        base_sha = str(getattr(lease, "base_sha", "") or "").strip()
        if not base_sha:
            return _error("missing_base_sha", path=worktree)
        base = self._git(worktree, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
        if not base["ok"]:
            return _error("base_sha_not_found", path=worktree, detail=base["stderr"])
        head = self._git(worktree, "rev-parse", "HEAD")
        if not head["ok"]:
            return _error("head_not_found", path=worktree, detail=head["stderr"])
        name_status = self._git(worktree, "diff", "--name-status", "-M", base_sha, "--")
        if not name_status["ok"]:
            return _error("git_diff_failed", path=worktree, detail=name_status["stderr"])
        numstat = self._git(worktree, "diff", "--numstat", "-M", base_sha, "--")
        if not numstat["ok"]:
            return _error("git_diff_failed", path=worktree, detail=numstat["stderr"])
        patch_data = self._git(worktree, "diff", "--binary", "-M", base_sha, "--")
        if not patch_data["ok"]:
            return _error("git_diff_failed", path=worktree, detail=patch_data["stderr"])

        files = _parse_diff_files(name_status["stdout"], numstat["stdout"])
        patch = patch_data["stdout"]
        for rel_path in _untracked_paths(self, worktree):
            if _skip_artifact_path(rel_path):
                continue
            entry, entry_patch = _untracked_diff_entry(worktree, rel_path)
            files.append(entry)
            patch += entry_patch

        additions = sum(int(item.get("additions") or 0) for item in files)
        deletions = sum(int(item.get("deletions") or 0) for item in files)
        patch_artifact = ""
        artifact_paths: list[str] = []
        if files and include_patch and patch and write_patch_artifact:
            patch_artifact = _write_diff_artifact(worktree, patch)
            artifact_paths.append(patch_artifact)
        inline_patch, patch_truncated = _truncate_patch(patch if include_patch else "", max_patch_chars)
        return {
            "ok": True,
            "clean": not files,
            "path": str(worktree),
            "resolved_path": str(worktree.resolve(strict=False)),
            "base_ref": str(getattr(lease, "base_ref", "") or ""),
            "base_sha": base_sha,
            "compare_to": base_sha,
            "head_sha": head["stdout"].strip(),
            "branch": str(getattr(lease, "branch", "") or registered.get("branch") or ""),
            "lease_id": str(getattr(lease, "id", "") or ""),
            "lease_status": str(getattr(lease, "status", "") or ""),
            "changed_files": files,
            "files_changed": len(files),
            "additions": additions,
            "deletions": deletions,
            "patch": inline_patch,
            "patch_truncated": patch_truncated,
            "patch_artifact": patch_artifact,
            "artifact_paths": artifact_paths,
        }

    def merge_risk_check(
        self,
        context: dict[str, Any],
        *,
        other_lease_id: str = "",
        other_session_id: str = "",
    ) -> dict[str, Any]:
        session_id = str(context.get("session_id") or "").strip()
        store = context.get("store")
        current = _active_lease_for_session(store, session_id)
        if current is None:
            return _merge_risk_result(error="no_active_worktree_lease")
        other = _other_active_lease(
            store,
            current,
            other_lease_id=other_lease_id,
            other_session_id=other_session_id,
        )
        if other is None:
            return _merge_risk_result(current=current, error="other_active_lease_not_found")

        current_diff = self._diff_for_lease(
            context,
            current,
            max_patch_chars=0,
            include_patch=False,
            write_patch_artifact=False,
        )
        if not current_diff.get("ok", True):
            return _merge_risk_result(
                current=current,
                other=other,
                error=str(current_diff.get("error") or "current_diff_failed"),
                detail=str(current_diff.get("detail") or ""),
            )
        other_diff = self._diff_for_lease(
            context,
            other,
            max_patch_chars=0,
            include_patch=False,
            write_patch_artifact=False,
        )
        if not other_diff.get("ok", True):
            return _merge_risk_result(
                current=current,
                other=other,
                error=str(other_diff.get("error") or "other_diff_failed"),
                detail=str(other_diff.get("detail") or ""),
            )

        current_files = _diff_file_paths(current_diff.get("changed_files") or [])
        other_files = _diff_file_paths(other_diff.get("changed_files") or [])
        overlapping = sorted(current_files & other_files)
        if overlapping:
            risk_level = "high"
        elif current_files or other_files:
            risk_level = "low"
        else:
            risk_level = "none"
        return _merge_risk_result(
            current=current,
            other=other,
            risk_level=risk_level,
            overlapping_files=overlapping,
            current_files=sorted(current_files),
            other_files=sorted(other_files),
        )

    def find_stale_leases(
        self,
        context: dict[str, Any],
        *,
        stale_after_seconds: int = 3600,
    ) -> dict[str, Any]:
        store = context.get("store")
        get_many = getattr(store, "get_worktree_leases", None)
        if store is None or not callable(get_many):
            return {"ok": False, "error": "worktree_store_unavailable", "leases": []}
        try:
            leases = get_many(status="active")
        except TypeError:
            leases = get_many()
        now = datetime.now(timezone.utc)
        threshold = max(0, int(stale_after_seconds))
        stale_rows: list[dict[str, Any]] = []
        for lease in leases or []:
            age_seconds = _lease_age_seconds(lease, now)
            if age_seconds < threshold:
                continue
            worktree = _normalize_path(getattr(lease, "worktree_path", "") or "")
            status = self.status(
                worktree,
                str(getattr(lease, "base_sha", "") or getattr(lease, "base_ref", "") or ""),
            )
            dirty = bool(getattr(lease, "dirty", False))
            if status.get("ok"):
                dirty = dirty or bool(status.get("dirty"))
            stale_rows.append(
                {
                    "lease_id": str(getattr(lease, "id", "") or ""),
                    "session_id": str(getattr(lease, "session_id", "") or ""),
                    "task_id": str(getattr(lease, "task_id", "") or ""),
                    "workspace": str(worktree),
                    "path": str(worktree),
                    "branch": str(getattr(lease, "branch", "") or ""),
                    "status": str(getattr(lease, "status", "") or ""),
                    "locked": bool(getattr(lease, "locked", False)),
                    "dirty": dirty,
                    "age_seconds": age_seconds,
                    "takeover_allowed": not dirty,
                    "auto_takeover_allowed": False,
                    "reason": "dirty_worktree" if dirty else "manual_review_required",
                }
            )
        return {"ok": True, "leases": stale_rows, "stale_count": len(stale_rows)}

    def cleanup(
        self,
        context: dict[str, Any],
        *,
        dry_run: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        session_id = str(context.get("session_id") or "").strip()
        store = context.get("store")
        lease = _cleanup_lease_for_session(store, session_id)
        if lease is None:
            return _cleanup_reject("no_current_session_worktree", requires_approval=True)
        if str(getattr(lease, "session_id", "") or "") != session_id:
            return _cleanup_reject("lease_session_mismatch", lease=lease, requires_approval=True)
        if str(getattr(lease, "status", "") or "") == "removed":
            return _cleanup_result(lease=lease, safe=True, already_removed=True, reason=reason)
        worktree = _normalize_path(getattr(lease, "worktree_path", "") or "")
        main_workspace = _normalize_path(
            getattr(lease, "main_workspace", "")
            or context.get("main_workspace")
            or context.get("workspace")
            or "."
        )
        root_error = _path_root_error(
            worktree,
            _worktree_roots(main_workspace, context.get("worktree_roots")),
        )
        if root_error:
            return _cleanup_reject(root_error, lease=lease, requires_approval=True)
        diff_data = self._diff_for_lease(
            context,
            lease,
            max_patch_chars=0,
            include_patch=True,
            write_patch_artifact=False,
        )
        cleanup_artifact = _write_cleanup_artifact(main_workspace, lease, diff_data)
        if not worktree.exists():
            if not dry_run:
                _mark_lease_removed(store, lease)
            return _cleanup_result(
                lease=lease,
                safe=True,
                would_remove=False,
                removed=not dry_run,
                already_removed=True,
                cleanup_artifact=cleanup_artifact,
                reason=reason,
            )
        listed = self.list(main_workspace)
        if not listed.get("ok"):
            return _cleanup_reject(str(listed.get("error") or "worktree_list_failed"), lease=lease)
        rows = [row for row in listed.get("worktrees", []) if isinstance(row, dict)]
        registered = _find_worktree(rows, worktree, str(getattr(lease, "branch", "") or ""))
        if registered is None:
            return _cleanup_reject("worktree_not_registered", lease=lease, requires_approval=True)
        status = self.status(worktree, str(getattr(lease, "base_sha", "") or ""))
        if not status.get("ok"):
            return _cleanup_reject(str(status.get("error") or "worktree_status_failed"), lease=lease)
        dirty = bool(status.get("dirty")) or bool(getattr(lease, "dirty", False))
        lease_status = str(getattr(lease, "status", "") or "")
        branch_merged = int(status.get("ahead") or 0) == 0 or lease_status == "released"
        if dirty:
            return _cleanup_result(
                lease=lease,
                safe=False,
                dirty=True,
                branch_merged=branch_merged,
                requires_approval=True,
                cleanup_artifact=cleanup_artifact,
                reason=reason,
            )
        if not branch_merged:
            return _cleanup_result(
                lease=lease,
                safe=False,
                dirty=False,
                branch_merged=False,
                requires_approval=True,
                cleanup_artifact=cleanup_artifact,
                error="branch_unmerged",
                reason=reason,
            )
        if dry_run:
            return _cleanup_result(
                lease=lease,
                safe=True,
                would_remove=True,
                dirty=False,
                branch_merged=True,
                cleanup_artifact=cleanup_artifact,
                reason=reason,
            )
        removed = self._git(main_workspace, "worktree", "remove", str(worktree))
        if not removed["ok"]:
            return _cleanup_reject("git_worktree_remove_failed", lease=lease, detail=removed["stderr"])
        self._git(main_workspace, "worktree", "prune")
        updated = _mark_lease_removed(store, lease)
        return _cleanup_result(
            lease=updated or lease,
            safe=True,
            removed=True,
            would_remove=True,
            dirty=False,
            branch_merged=True,
            cleanup_artifact=cleanup_artifact,
            reason=reason,
        )

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
        lease = _active_lease_for_path(store, path, session_id=session_id, task_id=task_id)
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


def _bind_error(code: str, *, lease: Any = None, lease_id: str = "", detail: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "bound": False,
        "session_bound": False,
        "workspace_switched": False,
        "error": code,
        "lease_id": str(getattr(lease, "id", "") or lease_id or ""),
        "workspace": str(getattr(lease, "worktree_path", "") or ""),
        "owner_session_id": str(getattr(lease, "session_id", "") or ""),
        "owner_task_id": str(getattr(lease, "task_id", "") or ""),
        "lease_status": str(getattr(lease, "status", "") or ""),
        "detail": detail.strip(),
    }


def _with_bind_result(out: dict[str, Any], bound: dict[str, Any]) -> dict[str, Any]:
    if not bound.get("ok"):
        result = dict(out)
        result["ok"] = False
        result["session_bound"] = False
        result["workspace_switched"] = False
        result["bind_error"] = str(bound.get("error") or "bind_failed")
        result["error"] = str(bound.get("error") or "bind_failed")
        result["bind_result"] = bound
        return result
    return {
        **out,
        **bound,
        "ok": True,
        "bind_result": bound,
    }


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


def _active_lease_for_path(
    store: Any,
    path: Path,
    *,
    session_id: str = "",
    task_id: str = "",
) -> Any | None:
    leases = _active_leases_for_path(store, path)
    if session_id:
        for lease in leases:
            if _lease_owned_by(lease, session_id, task_id):
                return lease
    return leases[0] if leases else None


def _active_leases_for_path(store: Any, path: Path) -> list[Any]:
    if store is None:
        return []
    get_many = getattr(store, "get_worktree_leases", None)
    if callable(get_many):
        raw_leases: list[Any] = []
        for candidate in {str(path), str(path.resolve(strict=False))}:
            try:
                raw_leases.extend(get_many(worktree_path=candidate, status="active"))
            except TypeError:
                raw_leases = []
                break
        if not raw_leases:
            try:
                raw_leases = list(get_many(status="active") or [])
            except TypeError:
                raw_leases = list(get_many() or [])
        target = path.resolve(strict=False)
        leases: list[Any] = []
        seen: set[str] = set()
        for lease in raw_leases:
            lease_id = str(getattr(lease, "id", "") or id(lease))
            if lease_id in seen:
                continue
            try:
                lease_path = Path(str(getattr(lease, "worktree_path", "") or "")).resolve(
                    strict=False
                )
            except (OSError, ValueError):
                continue
            if lease_path == target and str(getattr(lease, "status", "") or "") == "active":
                seen.add(lease_id)
                leases.append(lease)
        return leases
    get_active = getattr(store, "get_active_worktree_lease", None)
    if callable(get_active):
        for candidate in {str(path), str(path.resolve(strict=False))}:
            lease = get_active(worktree_path=candidate)
            if lease is not None:
                return [lease]
    return []


def _active_write_lease_for_path(store: Any, path: Path, *, exclude_id: str = "") -> Any | None:
    for lease in _active_leases_for_path(store, path):
        if str(getattr(lease, "id", "") or "") == exclude_id:
            continue
        if bool(getattr(lease, "locked", False)):
            return lease
    return None


def _other_active_lease(
    store: Any,
    current: Any,
    *,
    other_lease_id: str = "",
    other_session_id: str = "",
) -> Any | None:
    get_lease = getattr(store, "get_worktree_lease", None)
    lease_id = str(other_lease_id or "").strip()
    if lease_id and callable(get_lease):
        lease = get_lease(lease_id)
        if lease is not None and str(getattr(lease, "status", "") or "") == "active":
            return lease
    get_many = getattr(store, "get_worktree_leases", None)
    if not callable(get_many):
        return None
    try:
        leases = get_many(status="active")
    except TypeError:
        leases = get_many()
    current_id = str(getattr(current, "id", "") or "")
    for lease in leases or []:
        if str(getattr(lease, "id", "") or "") == current_id:
            continue
        if other_session_id and str(getattr(lease, "session_id", "") or "") != other_session_id:
            continue
        return lease
    return None


def _diff_file_paths(files: list[Any]) -> set[str]:
    paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        old_path = str(item.get("old_path") or "").strip()
        if path:
            paths.add(path)
        if old_path:
            paths.add(old_path)
    return paths


def _merge_risk_result(
    *,
    current: Any = None,
    other: Any = None,
    risk_level: str = "none",
    overlapping_files: list[str] | None = None,
    current_files: list[str] | None = None,
    other_files: list[str] | None = None,
    error: str = "",
    detail: str = "",
) -> dict[str, Any]:
    return {
        "ok": not bool(error),
        "error": error,
        "detail": detail.strip(),
        "risk_level": risk_level,
        "overlapping_files": overlapping_files or [],
        "current_files": current_files or [],
        "other_files": other_files or [],
        "current_lease_id": str(getattr(current, "id", "") or ""),
        "other_lease_id": str(getattr(other, "id", "") or ""),
        "current_session_id": str(getattr(current, "session_id", "") or ""),
        "other_session_id": str(getattr(other, "session_id", "") or ""),
    }


def _lease_age_seconds(lease: Any, now: datetime) -> int:
    timestamp = (
        str(getattr(lease, "last_seen_at", "") or "")
        or str(getattr(lease, "updated_at", "") or "")
        or str(getattr(lease, "created_at", "") or "")
    )
    seen = _parse_iso_datetime(timestamp)
    if seen is None:
        return 0
    return max(0, int((now - seen).total_seconds()))


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _active_lease_for_session(store: Any, session_id: str) -> Any | None:
    if store is None or not session_id:
        return None
    get_active = getattr(store, "get_active_worktree_lease", None)
    if callable(get_active):
        try:
            return get_active(session_id=session_id)
        except TypeError:
            pass
    get_many = getattr(store, "get_worktree_leases", None)
    if not callable(get_many):
        return None
    try:
        leases = get_many(session_id=session_id, status="active")
    except TypeError:
        leases = get_many(status="active")
    for lease in leases or []:
        if str(getattr(lease, "session_id", "") or "") == session_id:
            return lease
    return None


def _cleanup_lease_for_session(store: Any, session_id: str) -> Any | None:
    if store is None or not session_id:
        return None
    active = _active_lease_for_session(store, session_id)
    if active is not None:
        return active
    get_many = getattr(store, "get_worktree_leases", None)
    if not callable(get_many):
        return None
    try:
        leases = get_many(session_id=session_id)
    except TypeError:
        leases = get_many()
    owned = [
        lease
        for lease in (leases or [])
        if str(getattr(lease, "session_id", "") or "") == session_id
    ]
    for status in ("released", "stale", "removed"):
        for lease in owned:
            if str(getattr(lease, "status", "") or "") == status:
                return lease
    return None


def _lease_owned_by(lease: Any, session_id: str, task_id: str) -> bool:
    if lease is None:
        return False
    if str(getattr(lease, "session_id", "") or "") != session_id:
        return False
    lease_task_id = str(getattr(lease, "task_id", "") or "")
    return not task_id or lease_task_id == task_id


def _clean_diff_result(*, lease: Any = None, error: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "clean": True,
        "error": error,
        "path": str(getattr(lease, "worktree_path", "") or ""),
        "resolved_path": str(getattr(lease, "worktree_path", "") or ""),
        "base_ref": str(getattr(lease, "base_ref", "") or ""),
        "base_sha": str(getattr(lease, "base_sha", "") or ""),
        "compare_to": str(getattr(lease, "base_sha", "") or ""),
        "head_sha": str(getattr(lease, "head_sha", "") or ""),
        "branch": str(getattr(lease, "branch", "") or ""),
        "lease_id": str(getattr(lease, "id", "") or ""),
        "lease_status": str(getattr(lease, "status", "") or ""),
        "changed_files": [],
        "files_changed": 0,
        "additions": 0,
        "deletions": 0,
        "patch": "",
        "patch_truncated": False,
        "patch_artifact": "",
        "artifact_paths": [],
    }


def _cleanup_reject(
    code: str,
    *,
    lease: Any = None,
    requires_approval: bool = False,
    detail: str = "",
) -> dict[str, Any]:
    return _cleanup_result(
        lease=lease,
        safe=False,
        requires_approval=requires_approval,
        error=code,
        detail=detail,
    )


def _cleanup_result(
    *,
    lease: Any = None,
    safe: bool,
    would_remove: bool = False,
    removed: bool = False,
    already_removed: bool = False,
    dirty: bool = False,
    branch_merged: bool = False,
    requires_approval: bool = False,
    cleanup_artifact: str = "",
    error: str = "",
    detail: str = "",
    reason: str = "",
) -> dict[str, Any]:
    artifact_paths = [cleanup_artifact] if cleanup_artifact else []
    return {
        "ok": True,
        "decision": "cleanup",
        "safe": safe,
        "would_remove": would_remove,
        "removed": removed,
        "already_removed": already_removed,
        "dirty": dirty,
        "branch_merged": branch_merged,
        "requires_approval": requires_approval,
        "error": error,
        "detail": detail.strip(),
        "reason": reason,
        "lease_id": str(getattr(lease, "id", "") or ""),
        "lease_status": str(getattr(lease, "status", "") or ""),
        "owner_session_id": str(getattr(lease, "session_id", "") or ""),
        "owner_task_id": str(getattr(lease, "task_id", "") or ""),
        "workspace": str(getattr(lease, "worktree_path", "") or ""),
        "path": str(getattr(lease, "worktree_path", "") or ""),
        "main_workspace": str(getattr(lease, "main_workspace", "") or ""),
        "repo_root": str(getattr(lease, "repo_root", "") or ""),
        "branch": str(getattr(lease, "branch", "") or ""),
        "base_ref": str(getattr(lease, "base_ref", "") or ""),
        "base_sha": str(getattr(lease, "base_sha", "") or ""),
        "head_sha": str(getattr(lease, "head_sha", "") or ""),
        "cleanup_artifact": cleanup_artifact,
        "artifact_paths": artifact_paths,
        "risks": [error] if error else [],
    }


def _mark_lease_removed(store: Any, lease: Any) -> Any | None:
    if store is None or lease is None:
        return lease
    update = getattr(store, "update_worktree_lease", None)
    lease_id = str(getattr(lease, "id", "") or "")
    if callable(update) and lease_id:
        try:
            return update(lease_id, status="removed", dirty=False, locked=False)
        except TypeError:
            return update(lease_id, status="removed")
    try:
        setattr(lease, "status", "removed")
        setattr(lease, "dirty", False)
        setattr(lease, "locked", False)
    except Exception:  # noqa: BLE001 - best-effort fallback for lightweight fakes
        return lease
    return lease


def _write_cleanup_artifact(main_workspace: Path, lease: Any, diff_data: dict[str, Any]) -> str:
    try:
        root = main_workspace.resolve(strict=False)
        log_dir = (root / ".foreman" / "tool-logs").resolve(strict=False)
        if not (log_dir == root or root in log_dir.parents):
            return ""
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"worktree-cleanup-{uuid.uuid4().hex[:12]}.json"
        payload = {
            "kind": "worktree_cleanup_checkpoint",
            "lease": {
                "id": str(getattr(lease, "id", "") or ""),
                "repo_root": str(getattr(lease, "repo_root", "") or ""),
                "main_workspace": str(getattr(lease, "main_workspace", "") or ""),
                "worktree_path": str(getattr(lease, "worktree_path", "") or ""),
                "branch": str(getattr(lease, "branch", "") or ""),
                "base_ref": str(getattr(lease, "base_ref", "") or ""),
                "base_sha": str(getattr(lease, "base_sha", "") or ""),
                "head_sha": str(getattr(lease, "head_sha", "") or ""),
                "session_id": str(getattr(lease, "session_id", "") or ""),
                "task_id": str(getattr(lease, "task_id", "") or ""),
                "status": str(getattr(lease, "status", "") or ""),
            },
            "diff": {
                "ok": bool(diff_data.get("ok", True)),
                "error": str(diff_data.get("error") or ""),
                "base_ref": str(diff_data.get("base_ref") or ""),
                "base_sha": str(diff_data.get("base_sha") or ""),
                "compare_to": str(diff_data.get("compare_to") or ""),
                "head_sha": str(diff_data.get("head_sha") or ""),
                "clean": bool(diff_data.get("clean", False)),
                "changed_files": diff_data.get("changed_files") or [],
                "files_changed": int(diff_data.get("files_changed") or 0),
                "additions": int(diff_data.get("additions") or 0),
                "deletions": int(diff_data.get("deletions") or 0),
                "patch": str(diff_data.get("patch") or ""),
                "patch_truncated": bool(diff_data.get("patch_truncated", False)),
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except (OSError, ValueError, TypeError):
        return ""


def _parse_diff_files(name_status: str, numstat: str) -> list[dict[str, Any]]:
    nums = _parse_numstat_rows(numstat)
    files: list[dict[str, Any]] = []
    for idx, line in enumerate(name_status.splitlines()):
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        code = fields[0]
        status = _diff_status(code)
        old_path = ""
        path = fields[-1].strip()
        if code.startswith("R") and len(fields) >= 3:
            old_path = fields[1].strip()
            path = fields[2].strip()
        additions, deletions, binary = nums[idx] if idx < len(nums) else (0, 0, False)
        files.append(
            {
                "path": path,
                "old_path": old_path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "binary": binary,
            }
        )
    return files


def _parse_numstat_rows(raw: str) -> list[tuple[int, int, bool]]:
    rows: list[tuple[int, int, bool]] = []
    for line in raw.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        binary = fields[0] == "-" or fields[1] == "-"
        additions = 0 if binary else _safe_int(fields[0])
        deletions = 0 if binary else _safe_int(fields[1])
        rows.append((additions, deletions, binary))
    return rows


def _diff_status(code: str) -> str:
    marker = (code or "")[:1]
    return {
        "A": "added",
        "M": "modified",
        "D": "deleted",
        "R": "renamed",
        "C": "copied",
        "T": "type_changed",
        "U": "unmerged",
    }.get(marker, "modified")


def _safe_int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _untracked_paths(manager: WorktreeManager, worktree: Path) -> list[str]:
    out = manager._git(worktree, "ls-files", "--others", "--exclude-standard")
    if not out["ok"]:
        return []
    return [line.strip() for line in out["stdout"].splitlines() if line.strip()]


def _skip_artifact_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/")
    return normalized.startswith(".foreman/tool-logs/")


def _untracked_diff_entry(worktree: Path, rel_path: str) -> tuple[dict[str, Any], str]:
    target = worktree / rel_path
    try:
        raw = target.read_bytes()
    except OSError:
        raw = b""
    binary = b"\0" in raw
    text = ""
    if not binary:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            binary = True
    additions = 0 if binary or not text else len(text.splitlines())
    entry = {
        "path": rel_path,
        "old_path": "",
        "status": "untracked",
        "additions": additions,
        "deletions": 0,
        "binary": binary,
    }
    if binary:
        return entry, f"diff --git a/{rel_path} b/{rel_path}\nnew file mode 100644\nBinary files /dev/null and b/{rel_path} differ\n"
    lines = text.splitlines()
    patch_lines = [
        f"diff --git a/{rel_path} b/{rel_path}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{rel_path}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    patch_lines.extend(f"+{line}" for line in lines)
    return entry, "\n".join(patch_lines) + "\n"


def _truncate_patch(patch: str, max_chars: int) -> tuple[str, bool]:
    if not patch:
        return "", False
    try:
        limit = max(0, int(max_chars))
    except (TypeError, ValueError):
        limit = 0
    if not limit or len(patch) <= limit:
        return patch, False
    marker = "\n...[worktree diff truncated; see patch_artifact for full patch]...\n"
    head = max(200, (limit - len(marker)) // 2)
    tail = max(200, limit - len(marker) - head)
    return patch[:head].rstrip() + marker + patch[-tail:].lstrip(), True


def _write_diff_artifact(worktree: Path, patch: str) -> str:
    log_dir = (worktree / ".foreman" / "tool-logs").resolve(strict=False)
    worktree_resolved = worktree.resolve(strict=False)
    if not (log_dir == worktree_resolved or worktree_resolved in log_dir.parents):
        raise ValueError("artifact_path_outside_worktree")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"worktree-diff-{uuid.uuid4().hex[:12]}.patch"
    path.write_text(patch, encoding="utf-8", newline="")
    return str(path)


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
