from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from foreman.client.core.gate import Gate
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.client.tools import EXTERNAL_WEB, PMToolLoop, PMToolRuntime, ToolCall
from foreman.client.tools.loop import (
    SUBMIT_PLAN_TOOL,
    _calls_from_json,
    submit_plan_tool_spec,
    validate_final_plan,
)
from foreman.client.tools.models import ToolRuntimeConfig
from foreman.shared.config import Config, GatesCfg
from foreman.shared.llm import LLMToolCall, LLMToolResponse, Message


class _TextHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"hello from local pm tools server"
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _serve_text() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _TextHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/x"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _runtime(tmp_path: Path, *, cards=None, **kwargs) -> PMToolRuntime:
    cfg = ToolRuntimeConfig(workspace=tmp_path, allowed_roots=[tmp_path], **kwargs)
    return PMToolRuntime(cfg, gate=Gate(Config().gates), cards=cards)


def test_pm_tool_schemas_allow_public_activity_note():
    spec = next(item for item in PMToolRuntime.specs() if item.name == "read_file")
    native_props = spec.to_native()["input_schema"]["properties"]
    prompt_props = spec.to_prompt()["input_schema"]["properties"]
    assert native_props["public_note"]["type"] == "string"
    assert native_props["purpose"]["type"] == "string"
    assert prompt_props["public_note"]["maxLength"] == 200
    assert spec.input_schema["additionalProperties"] is False


def test_worktree_bind_schema_rejects_pm_owned_context_fields():
    spec = next(item for item in PMToolRuntime.specs() if item.name == "worktree_bind_session")
    schema = spec.to_prompt()["input_schema"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"lease_id"}
    assert "lease_id" in schema["properties"]
    assert "session_id" not in schema["properties"]
    assert "task_id" not in schema["properties"]
    assert "path" not in schema["properties"]
    assert "worktree_path" not in schema["properties"]


def test_worktree_readonly_tool_schemas_are_safe_and_do_not_accept_pm_context_fields():
    by_name = {item.name: item for item in PMToolRuntime.specs()}

    plan_spec = by_name["worktree_plan"]
    create_spec = by_name["worktree_create"]
    list_spec = by_name["worktree_list"]
    status_spec = by_name["worktree_status"]
    diff_spec = by_name["worktree_diff"]
    cleanup_spec = by_name["worktree_cleanup"]

    assert plan_spec.risk == "safe"
    assert create_spec.risk == "needs-strategy"
    assert list_spec.risk == "safe"
    assert status_spec.risk == "safe"
    assert diff_spec.risk == "safe"
    assert cleanup_spec.risk == "needs-strategy"
    assert plan_spec.input_schema["additionalProperties"] is False
    assert create_spec.input_schema["additionalProperties"] is False
    assert list_spec.input_schema["additionalProperties"] is False
    assert status_spec.input_schema["additionalProperties"] is False
    assert diff_spec.input_schema["additionalProperties"] is False
    assert cleanup_spec.input_schema["additionalProperties"] is False
    assert "custom_path" in plan_spec.input_schema["properties"]
    assert "custom_path" in create_spec.input_schema["properties"]
    assert "dry_run" in create_spec.input_schema["properties"]
    assert "bind_session" in create_spec.input_schema["properties"]
    assert set(cleanup_spec.input_schema["properties"]) == {"dry_run", "reason"}
    for spec in (plan_spec, create_spec, list_spec, status_spec, diff_spec, cleanup_spec):
        assert "session_id" not in spec.input_schema["properties"]
        assert "task_id" not in spec.input_schema["properties"]
        assert "path" not in spec.input_schema["properties"] or spec.name == "worktree_status"
        assert "worktree_path" not in spec.input_schema["properties"]
        assert "base_sha" not in spec.input_schema["properties"]


def test_checkpoint_diff_and_test_tool_schemas_do_not_accept_pm_context_fields():
    by_name = {item.name: item for item in PMToolRuntime.specs()}
    specs = [
        by_name["checkpoint_create"],
        by_name["checkpoint_undo"],
        by_name["git_diff_summary"],
        by_name["test_run"],
    ]

    assert by_name["checkpoint_create"].risk == "safe"
    assert by_name["git_diff_summary"].risk == "safe"
    assert by_name["checkpoint_undo"].risk == "needs-strategy"
    assert by_name["test_run"].risk == "needs-strategy"
    assert set(by_name["checkpoint_create"].input_schema["properties"]) == {"label"}
    assert set(by_name["checkpoint_undo"].input_schema["required"]) == {"checkpoint_id"}
    assert set(by_name["git_diff_summary"].input_schema["required"]) == {"checkpoint_id"}
    assert set(by_name["test_run"].input_schema["required"]) == {"command"}
    for spec in specs:
        assert spec.input_schema["additionalProperties"] is False
        assert "session_id" not in spec.input_schema["properties"]
        assert "task_id" not in spec.input_schema["properties"]
        assert "path" not in spec.input_schema["properties"]
        assert "workspace" not in spec.input_schema["properties"]
        assert "worktree_path" not in spec.input_schema["properties"]


async def test_worktree_tool_disabled_returns_tool_disabled(tmp_path: Path):
    cases = [
        ToolCall("plan", "worktree_plan", {"goal": "x"}),
        ToolCall("create", "worktree_create", {"goal": "x"}),
        ToolCall("bind", "worktree_bind_session", {"lease_id": "lease-1"}),
        ToolCall("list", "worktree_list", {}),
        ToolCall("status", "worktree_status", {}),
        ToolCall("diff", "worktree_diff", {}),
        ToolCall("cleanup", "worktree_cleanup", {}),
    ]

    for call in cases:
        result = await _runtime(tmp_path, git_worktree=False).call(call)
        assert result.ok is False
        assert result.error == "tool_disabled"


def test_runtime_from_config_injects_worktree_dependencies(tmp_path: Path):
    cfg = Config()
    cfg.pm_tools.git_worktree = True
    cfg.pm_tools.worktree_roots = [str(tmp_path / "worktrees")]
    cfg.pm_tools.allow_custom_worktree_path = True
    store = object()
    manager = object()

    rt = PMToolRuntime.from_config(
        cfg,
        tmp_path,
        store=store,
        session_id="s1",
        task_id="t1",
        main_workspace=tmp_path,
        worktree_manager=manager,
    )

    assert rt.cfg.store is store
    assert rt.cfg.session_id == "s1"
    assert rt.cfg.task_id == "t1"
    assert rt.cfg.main_workspace == tmp_path
    assert rt.cfg.worktree_manager is manager
    assert rt.cfg.git_worktree is True
    assert rt.cfg.worktree_roots == [tmp_path / "worktrees"]
    assert rt.cfg.allow_custom_worktree_path is True
    assert "session_id" not in rt.runtime_context()
    assert "task_id" not in rt.runtime_context()
    assert rt.worktree_context()["session_id"] == "s1"
    assert rt.worktree_context()["task_id"] == "t1"


async def test_worktree_bind_injects_context_and_switches_runtime_guard(tmp_path: Path):
    main = tmp_path / "repo"
    worktree_root = tmp_path / ".foreman-worktrees" / "repo"
    worktree = worktree_root / "s1-task"
    main.mkdir()
    worktree.mkdir(parents=True)
    (main / "main.txt").write_text("main", encoding="utf-8")
    (worktree / "wt.txt").write_text("worktree", encoding="utf-8")
    store = object()
    seen: dict[str, object] = {}

    class FakeWorktreeManager:
        def bind_session(self, context, *, lease_id: str, reason: str = ""):
            seen["context"] = context
            seen["lease_id"] = lease_id
            seen["reason"] = reason
            return {
                "ok": True,
                "bound": True,
                "lease_id": lease_id,
                "main_workspace": str(main),
                "workspace": str(worktree),
            }

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            store=store,
            session_id="s1",
            task_id="t1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            worktree_roots=[worktree_root],
        )
    )

    bound = await rt.call(
        ToolCall(
            "bind",
            "worktree_bind_session",
            {"lease_id": "lease-1", "reason": "use isolated workspace"},
        )
    )
    read_worktree = await rt.call(ToolCall("read", "read_file", {"path": "wt.txt"}))
    read_main = await rt.call(ToolCall("main", "read_file", {"path": str(main / "main.txt")}))

    assert bound.ok is True
    assert bound.data["cwd"] == str(worktree.resolve(strict=False))
    assert rt.runtime_context()["cwd"] == str(worktree.resolve(strict=False))
    assert read_worktree.ok is True and read_worktree.data["text"] == "worktree"
    assert read_main.ok is False and read_main.error == "path_outside_workspace"
    context = seen["context"]
    assert context["store"] is store
    assert context["session_id"] == "s1"
    assert context["task_id"] == "t1"
    assert context["main_workspace"] == str(main)
    assert context["worktree_roots"] == [str(worktree_root)]


async def test_worktree_bind_rejects_pm_supplied_session_or_path(tmp_path: Path):
    class FakeWorktreeManager:
        def bind_session(self, context, *, lease_id: str, reason: str = ""):
            raise AssertionError("manager must not be called for forbidden PM context fields")

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=tmp_path,
            allowed_roots=[tmp_path],
            main_workspace=tmp_path,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            worktree_roots=[tmp_path],
        )
    )

    result = await rt.call(
        ToolCall(
            "bind",
            "worktree_bind_session",
            {"lease_id": "lease-1", "session_id": "other", "worktree_path": str(tmp_path)},
        )
    )

    assert result.ok is False
    assert result.error == "invalid_args"


async def test_worktree_plan_injects_runtime_context_and_rejects_pm_context_fields(tmp_path: Path):
    main = tmp_path / "repo"
    worktree_root = tmp_path / ".foreman-worktrees" / "repo"
    main.mkdir()
    store = object()
    seen: dict[str, object] = {"calls": 0}

    class FakeWorktreeManager:
        def plan(self, context, **kwargs):
            seen["calls"] = int(seen["calls"]) + 1
            seen["context"] = context
            seen["kwargs"] = kwargs
            return {
                "ok": True,
                "decision": "create",
                "main_workspace": str(main),
                "repo_root": str(main),
                "proposed_path": str(worktree_root / "s1-task"),
                "proposed_branch": "foreman/s1/task",
                "base_ref": "main",
                "base_sha": "base",
                "head_sha": "",
                "requires_approval": False,
                "risks": [],
            }

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            store=store,
            session_id="s1",
            task_id="t1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            worktree_roots=[worktree_root],
            worktree_branch_prefix="foreman/",
            default_base_ref="main",
        )
    )

    result = await rt.call(
        ToolCall(
            "plan",
            "worktree_plan",
            {
                "goal": "Build task",
                "slug": "task",
                "base_ref": "main",
                "reuse_policy": "reuse_clean_owned",
                "custom_path": str(worktree_root / "s1-task"),
            },
        )
    )
    invalid = await rt.call(
        ToolCall("bad", "worktree_plan", {"goal": "x", "session_id": "other"})
    )

    assert result.ok is True
    assert result.data["decision"] == "create"
    assert invalid.ok is False
    assert invalid.error == "invalid_args"
    assert seen["calls"] == 1
    context = seen["context"]
    assert context["store"] is store
    assert context["session_id"] == "s1"
    assert context["task_id"] == "t1"
    assert context["main_workspace"] == str(main)
    assert context["worktree_roots"] == [str(worktree_root)]
    assert context["allow_custom_worktree_path"] is False
    assert seen["kwargs"] == {
        "goal": "Build task",
        "slug": "task",
        "base_ref": "main",
        "reuse_policy": "reuse_clean_owned",
        "custom_path": str(worktree_root / "s1-task"),
    }


async def test_worktree_create_injects_runtime_context_and_rejects_pm_context_fields(tmp_path: Path):
    main = tmp_path / "repo"
    worktree_root = tmp_path / ".foreman-worktrees" / "repo"
    main.mkdir()
    store = object()
    seen: dict[str, object] = {"calls": 0}

    class FakeWorktreeManager:
        def create(self, context, **kwargs):
            seen["calls"] = int(seen["calls"]) + 1
            seen["context"] = context
            seen["kwargs"] = kwargs
            return {
                "ok": True,
                "decision": "create",
                "created": not kwargs["dry_run"],
                "main_workspace": str(main),
                "repo_root": str(main),
                "proposed_path": str(worktree_root / "s1-task"),
                "proposed_branch": "foreman/s1/task",
                "base_ref": "main",
                "base_sha": "base",
                "head_sha": "base",
                "lease_id": "lease-1",
                "requires_approval": False,
                "risks": [],
            }

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            store=store,
            session_id="s1",
            task_id="t1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            worktree_roots=[worktree_root],
            worktree_branch_prefix="foreman/",
            default_base_ref="main",
            allow_custom_worktree_path=True,
        )
    )

    result = await rt.call(
        ToolCall(
            "create",
            "worktree_create",
            {
                "goal": "Build task",
                "slug": "task",
                "base_ref": "main",
                "reuse_policy": "reuse_clean_owned",
                "custom_path": str(worktree_root / "s1-task"),
                "dry_run": False,
                "bind_session": True,
            },
        )
    )
    invalid = await rt.call(
        ToolCall("bad", "worktree_create", {"goal": "x", "task_id": "other"})
    )

    assert result.ok is True
    assert result.data["created"] is True
    assert invalid.ok is False
    assert invalid.error == "invalid_args"
    assert seen["calls"] == 1
    context = seen["context"]
    assert context["store"] is store
    assert context["session_id"] == "s1"
    assert context["task_id"] == "t1"
    assert context["allow_custom_worktree_path"] is True
    assert seen["kwargs"] == {
        "goal": "Build task",
        "slug": "task",
        "base_ref": "main",
        "reuse_policy": "reuse_clean_owned",
        "custom_path": str(worktree_root / "s1-task"),
        "dry_run": False,
        "bind_session": True,
    }


async def test_worktree_create_bind_session_switches_runtime_guard(tmp_path: Path):
    main = tmp_path / "repo"
    worktree_root = tmp_path / ".foreman-worktrees" / "repo"
    worktree = worktree_root / "s1-task"
    main.mkdir()
    worktree.mkdir(parents=True)
    (main / "main.txt").write_text("main", encoding="utf-8")
    (worktree / "wt.txt").write_text("worktree", encoding="utf-8")

    class FakeWorktreeManager:
        def create(self, context, **kwargs):
            assert kwargs["bind_session"] is True
            return {
                "ok": True,
                "decision": "create",
                "created": True,
                "session_bound": True,
                "workspace_switched": True,
                "lease_id": "lease-1",
                "main_workspace": str(main),
                "workspace": str(worktree),
                "path": str(worktree),
            }

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            session_id="s1",
            task_id="t1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            worktree_roots=[worktree_root],
        )
    )

    created = await rt.call(
        ToolCall("create", "worktree_create", {"goal": "x", "bind_session": True})
    )
    read_worktree = await rt.call(ToolCall("read", "read_file", {"path": "wt.txt"}))
    read_main = await rt.call(ToolCall("main", "read_file", {"path": str(main / "main.txt")}))

    assert created.ok is True
    assert created.data["cwd"] == str(worktree.resolve(strict=False))
    assert rt.runtime_context()["cwd"] == str(worktree.resolve(strict=False))
    assert read_worktree.ok is True and read_worktree.data["text"] == "worktree"
    assert read_main.ok is False and read_main.error == "path_outside_workspace"


async def test_worktree_read_only_bind_disables_write_and_command_tools(tmp_path: Path):
    main = tmp_path / "repo"
    worktree_root = tmp_path / ".foreman-worktrees" / "repo"
    worktree = worktree_root / "s1-task"
    main.mkdir()
    worktree.mkdir(parents=True)
    (worktree / "wt.txt").write_text("worktree", encoding="utf-8")

    class FakeWorktreeManager:
        def bind_session(self, context, *, lease_id: str, reason: str = ""):
            return {
                "ok": True,
                "bound": True,
                "lease_id": lease_id,
                "main_workspace": str(main),
                "workspace": str(worktree),
                "read_only": True,
                "write_lock": False,
            }

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            session_id="s1",
            task_id="t1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            file_write=True,
            shell=True,
            worktree_roots=[worktree_root],
        )
    )

    bound = await rt.call(ToolCall("bind", "worktree_bind_session", {"lease_id": "lease-1"}))
    read_worktree = await rt.call(ToolCall("read", "read_file", {"path": "wt.txt"}))
    write = await rt.call(ToolCall("write", "write_file", {"path": "x.txt", "text": "x"}))
    command = await rt.call(ToolCall("cmd", "run_command", {"command": "python --version"}))
    create = await rt.call(ToolCall("create", "worktree_create", {"goal": "new"}))
    cleanup = await rt.call(ToolCall("cleanup", "worktree_cleanup", {}))
    checkpoint = await rt.call(ToolCall("checkpoint", "checkpoint_create", {}))
    test_run = await rt.call(ToolCall("test", "test_run", {"command": "python --version"}))
    undo = await rt.call(ToolCall("undo", "checkpoint_undo", {"checkpoint_id": "c1"}))

    assert bound.ok is True
    assert bound.data["read_only"] is True
    assert read_worktree.ok is True and read_worktree.data["text"] == "worktree"
    assert write.error == "tool_disabled"
    assert command.error == "tool_disabled"
    assert create.error == "tool_disabled"
    assert cleanup.error == "tool_disabled"
    assert checkpoint.error == "tool_disabled"
    assert test_run.error == "tool_disabled"
    assert undo.error == "tool_disabled"


async def test_worktree_list_and_status_add_lease_ownership(tmp_path: Path):
    main = tmp_path / "repo"
    worktree_root = tmp_path / ".foreman-worktrees" / "repo"
    worktree = worktree_root / "s1-task"
    main.mkdir()
    worktree.mkdir(parents=True)
    lease = SimpleNamespace(
        id="lease-1",
        worktree_path=str(worktree),
        session_id="s1",
        task_id="t1",
        status="active",
    )

    class FakeStore:
        def get_active_worktree_lease(self, *, worktree_path: str, session_id: str | None = None):
            if Path(worktree_path).resolve(strict=False) == worktree.resolve(strict=False):
                return lease
            return None

        def get_worktree_leases(self, *, status: str | None = None):
            return [lease] if status in {None, "active"} else []

    class FakeWorktreeManager:
        def __init__(self):
            self.status_paths: list[Path] = []

        def list(self, path):
            return {
                "ok": True,
                "repo_root": str(main),
                "worktrees": [
                    {
                        "path": str(main),
                        "resolved_path": str(main.resolve(strict=False)),
                        "branch": "main",
                        "head_sha": "base",
                        "locked": False,
                        "exists": True,
                    },
                    {
                        "path": str(worktree),
                        "resolved_path": str(worktree.resolve(strict=False)),
                        "branch": "feature",
                        "head_sha": "head",
                        "locked": False,
                        "exists": True,
                    },
                ],
            }

        def status(self, path, compare_to: str = ""):
            self.status_paths.append(Path(path))
            return {
                "ok": True,
                "resolved_path": str(Path(path).resolve(strict=False)),
                "dirty": False,
                "changed_files": [],
                "ahead": 0,
                "behind": 0,
                "base_ref": compare_to,
                "head_sha": "head",
            }

    manager = FakeWorktreeManager()
    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            store=FakeStore(),
            session_id="s1",
            task_id="t1",
            main_workspace=main,
            worktree_manager=manager,
            git_worktree=True,
            worktree_roots=[worktree_root],
            default_base_ref="main",
        )
    )

    listed = await rt.call(ToolCall("list", "worktree_list", {}))
    status = await rt.call(ToolCall("status", "worktree_status", {"path": str(worktree)}))

    assert listed.ok is True
    by_path = {row["resolved_path"]: row for row in listed.data["worktrees"]}
    assert by_path[str(main.resolve(strict=False))]["owner_session_id"] == ""
    assert by_path[str(worktree.resolve(strict=False))]["owner_session_id"] == "s1"
    assert by_path[str(worktree.resolve(strict=False))]["owner_task_id"] == "t1"
    assert by_path[str(worktree.resolve(strict=False))]["lease_id"] == "lease-1"
    assert status.ok is True
    assert status.data["owner_session_id"] == "s1"
    assert status.data["base_ref"] == "main"
    assert manager.status_paths == [worktree.resolve(strict=False)]


async def test_worktree_status_rejects_arbitrary_path_before_manager_call(tmp_path: Path):
    main = tmp_path / "repo"
    outside = tmp_path / "outside"
    main.mkdir()
    outside.mkdir()

    class FakeWorktreeManager:
        def status(self, path, compare_to: str = ""):
            raise AssertionError("arbitrary paths must not reach the manager")

    class FakeStore:
        def get_active_worktree_lease(self, *, worktree_path: str, session_id: str | None = None):
            if Path(worktree_path).resolve(strict=False) == outside.resolve(strict=False):
                return SimpleNamespace(
                    id="lease-other",
                    worktree_path=str(outside),
                    session_id="other-session",
                    task_id="t2",
                    status="active",
                )
            return None

        def get_worktree_leases(self, *, status: str | None = None):
            return []

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            store=FakeStore(),
            session_id="s1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            worktree_roots=[tmp_path / ".foreman-worktrees" / "repo"],
        )
    )

    result = await rt.call(ToolCall("status", "worktree_status", {"path": str(outside)}))

    assert result.ok is False
    assert result.error == "path_outside_workspace"


async def test_worktree_diff_injects_runtime_context_and_rejects_pm_context_fields(tmp_path: Path):
    main = tmp_path / "repo"
    main.mkdir()
    patch = main / ".foreman" / "tool-logs" / "diff.patch"

    class FakeWorktreeManager:
        def __init__(self):
            self.contexts: list[dict] = []

        def diff(self, context, *, max_patch_chars: int = 0, include_patch: bool = True):
            self.contexts.append(context)
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_text("diff", encoding="utf-8")
            return {
                "ok": True,
                "clean": False,
                "base_sha": "base",
                "base_ref": "main",
                "compare_to": "base",
                "changed_files": [{"path": "a.txt", "status": "modified"}],
                "files_changed": 1,
                "additions": 1,
                "deletions": 0,
                "patch_artifact": str(patch),
                "artifact_paths": [str(patch)],
                "patch_truncated": False,
                "max_patch_chars": max_patch_chars,
                "include_patch": include_patch,
            }

    manager = FakeWorktreeManager()
    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            store=SimpleNamespace(),
            session_id="s1",
            task_id="t1",
            main_workspace=main,
            worktree_manager=manager,
            git_worktree=True,
            worktree_roots=[tmp_path / ".foreman-worktrees" / "repo"],
            max_chars=123,
        )
    )

    result = await rt.call(ToolCall("diff", "worktree_diff", {}))
    rejected = await rt.call(ToolCall("bad", "worktree_diff", {"path": str(main)}))
    rejected_base = await rt.call(ToolCall("bad-base", "worktree_diff", {"base_sha": "other"}))

    assert result.ok is True
    assert result.data["compare_to"] == "base"
    assert result.artifact_paths == [str(patch)]
    assert manager.contexts[0]["session_id"] == "s1"
    assert manager.contexts[0]["task_id"] == "t1"
    assert manager.contexts[0]["main_workspace"] == str(main)
    assert rejected.ok is False and rejected.error == "invalid_args"
    assert rejected_base.ok is False and rejected_base.error == "invalid_args"


async def test_worktree_cleanup_injects_current_context_and_resets_deleted_cwd(tmp_path: Path):
    main = tmp_path / "repo"
    worktree_root = tmp_path / ".foreman-worktrees" / "repo"
    worktree = worktree_root / "s1-task"
    main.mkdir()
    worktree.mkdir(parents=True)
    artifact = main / ".foreman" / "tool-logs" / "cleanup.json"
    seen: dict[str, object] = {}

    class FakeWorktreeManager:
        def cleanup(self, context, *, dry_run: bool = True, reason: str = ""):
            seen["context"] = context
            seen["dry_run"] = dry_run
            seen["reason"] = reason
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}", encoding="utf-8")
            return {
                "ok": True,
                "safe": True,
                "removed": not dry_run,
                "requires_approval": False,
                "workspace": str(worktree),
                "path": str(worktree),
                "main_workspace": str(main),
                "cleanup_artifact": str(artifact),
                "artifact_paths": [str(artifact)],
            }

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=worktree,
            allowed_roots=[worktree],
            store=SimpleNamespace(),
            session_id="s1",
            task_id="t1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            worktree_roots=[worktree_root],
        )
    )

    result = await rt.call(
        ToolCall("cleanup", "worktree_cleanup", {"dry_run": False, "reason": "done"})
    )
    rejected = await rt.call(
        ToolCall("bad", "worktree_cleanup", {"worktree_path": str(worktree)})
    )
    rejected_base = await rt.call(
        ToolCall("bad-base", "worktree_cleanup", {"base_ref": "origin/main"})
    )

    assert result.ok is True
    assert result.risk == "needs-strategy"
    assert result.artifact_paths == [str(artifact)]
    assert result.data["cwd"] == str(main.resolve(strict=False))
    assert rt.runtime_context()["cwd"] == str(main.resolve(strict=False))
    context = seen["context"]
    assert context["session_id"] == "s1"
    assert context["task_id"] == "t1"
    assert context["workspace"] == str(worktree)
    assert context["main_workspace"] == str(main)
    assert context["worktree_roots"] == [str(worktree_root)]
    assert seen["dry_run"] is False
    assert seen["reason"] == "done"
    assert rejected.ok is False and rejected.error == "invalid_args"
    assert rejected_base.ok is False and rejected_base.error == "invalid_args"


async def test_worktree_cleanup_requires_approval_risk_without_deleting(tmp_path: Path):
    main = tmp_path / "repo"
    main.mkdir()

    class FakeWorktreeManager:
        def cleanup(self, context, *, dry_run: bool = True, reason: str = ""):
            return {
                "ok": True,
                "safe": False,
                "removed": False,
                "requires_approval": True,
                "error": "dirty_worktree",
                "workspace": str(main),
                "main_workspace": str(main),
            }

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            session_id="s1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            worktree_roots=[tmp_path / ".foreman-worktrees" / "repo"],
        )
    )

    result = await rt.call(ToolCall("cleanup", "worktree_cleanup", {"dry_run": False}))

    assert result.ok is True
    assert result.risk == "requires-approval"
    assert result.data["requires_approval"] is True
    assert result.data["removed"] is False


async def test_checkpoint_diff_and_undo_tools_use_current_session_worktree(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "foreman@example.test")
    _git(repo, "config", "user.name", "Foreman Test")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-m", "base")
    worktree = tmp_path / "feature"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree), "HEAD")
    store = Store(str(tmp_path / "foreman.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g", workspace=str(worktree), main_workspace=str(repo)))
    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=worktree,
            allowed_roots=[worktree],
            store=store,
            session_id="s1",
            task_id="t1",
            main_workspace=repo,
            shell=True,
        ),
        gate=Gate(Config().gates),
    )

    checkpoint = await rt.call(
        ToolCall("checkpoint", "checkpoint_create", {"label": "before change"})
    )
    (worktree / "app.txt").write_text("changed\n", encoding="utf-8")
    (worktree / "new.txt").write_text("new\n", encoding="utf-8")
    diff = await rt.call(
        ToolCall(
            "diff",
            "git_diff_summary",
            {"checkpoint_id": checkpoint.data["checkpoint_id"], "max_patch_chars": 80},
        )
    )
    bad_diff = await rt.call(
        ToolCall(
            "bad-diff",
            "git_diff_summary",
            {"checkpoint_id": checkpoint.data["checkpoint_id"], "session_id": "other"},
        )
    )
    undo = await rt.call(
        ToolCall("undo", "checkpoint_undo", {"checkpoint_id": checkpoint.data["checkpoint_id"]})
    )

    assert checkpoint.ok is True
    assert checkpoint.data["checkpoint_id"]
    assert store.get_checkpoint(checkpoint.data["checkpoint_id"]).session_id == "s1"
    assert diff.ok is True
    assert diff.data["summary"]["files"] == 2
    assert diff.data["patch_truncated"] is True
    assert diff.artifact_paths
    assert all(repo.resolve(strict=False) in Path(path).resolve(strict=False).parents for path in diff.artifact_paths)
    assert bad_diff.ok is False and bad_diff.error == "invalid_args"
    assert undo.ok is True
    assert undo.data["redo_ref"]
    assert (worktree / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert not (worktree / "new.txt").exists()
    assert all(Path(path).is_file() for path in diff.artifact_paths)


async def test_checkpoint_undo_rejects_cross_session_checkpoint(tmp_path: Path):
    store = Store(str(tmp_path / "foreman.db"))
    store.init()
    store.add_session(Session(id="s1", goal="g", workspace=str(tmp_path)))
    store.add_session(Session(id="other", goal="g", workspace=str(tmp_path)))
    rt_other = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=tmp_path,
            allowed_roots=[tmp_path],
            store=store,
            session_id="other",
            task_id="t2",
        )
    )
    checkpoint = await rt_other.call(ToolCall("checkpoint", "checkpoint_create", {}))
    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=tmp_path,
            allowed_roots=[tmp_path],
            store=store,
            session_id="s1",
            task_id="t1",
        )
    )

    result = await rt.call(
        ToolCall("undo", "checkpoint_undo", {"checkpoint_id": checkpoint.data["checkpoint_id"]})
    )

    assert result.ok is False
    assert result.error == "checkpoint_session_mismatch"


async def test_test_run_reports_failures_timeout_and_artifacts(tmp_path: Path):
    command = f'"{sys.executable}" -c "import sys; print(\'bad test\'); sys.exit(2)"'
    timeout_command = f'"{sys.executable}" -c "import time; time.sleep(3)"'
    rt = _runtime(tmp_path, shell=True, timeout_s=5)

    failed = await rt.call(ToolCall("test", "test_run", {"command": command}))
    timed_out = await rt.call(
        ToolCall("timeout", "test_run", {"command": timeout_command, "timeout_s": 1})
    )

    assert failed.ok is True
    assert failed.data["passed"] is False
    assert failed.data["returncode"] == 2
    assert "Tests failed with exit code 2" in failed.data["summary"]
    assert "bad test" in failed.data["summary"]
    assert all(Path(path).is_file() for path in failed.artifact_paths)
    assert timed_out.ok is True
    assert timed_out.data["passed"] is False
    assert timed_out.data["timed_out"] is True
    assert "timed out" in timed_out.data["summary"]
    assert all(Path(path).is_file() for path in timed_out.artifact_paths)


async def test_test_run_rejects_requires_approval_command(tmp_path: Path):
    rt = _runtime(tmp_path, shell=True)

    result = await rt.call(ToolCall("test", "test_run", {"command": "git push origin main"}))

    assert result.ok is False
    assert result.error == "requires_approval"


async def test_worktree_status_uses_existing_tool_events(tmp_path: Path):
    main = tmp_path / "repo"
    main.mkdir()
    events: list[tuple[str, dict]] = []

    class FakeWorktreeManager:
        def status(self, path, compare_to: str = ""):
            return {
                "ok": True,
                "resolved_path": str(Path(path).resolve(strict=False)),
                "dirty": False,
                "changed_files": [],
                "ahead": 0,
                "behind": 0,
                "base_ref": compare_to,
                "head_sha": "head",
            }

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        async def tool_complete(self, messages, *, tools, model="", json_mode=False, tool_choice=None):
            self.calls += 1
            if self.calls == 1:
                return LLMToolResponse(
                    text="",
                    tool_calls=[
                        LLMToolCall(
                            id="status-1",
                            name="worktree_status",
                            arguments={"path": str(main), "compare_to": "HEAD"},
                        )
                    ],
                )
            return LLMToolResponse(text="", tool_calls=[_submit_call(summary="done")])

    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=main,
            allowed_roots=[main],
            session_id="s1",
            main_workspace=main,
            worktree_manager=FakeWorktreeManager(),
            git_worktree=True,
            default_base_ref="HEAD",
        )
    )

    outcome = await PMToolLoop(
        FakeLLM(),
        rt,
        max_rounds=3,
        on_tool_event=lambda event_type, payload: events.append((event_type, payload)),
    ).run(
        [Message("user", "inspect worktree")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.final_plan["summary"] == "done"
    tool_events = [(event_type, payload["tool"]) for event_type, payload in events]
    assert tool_events == [("tool_pre", "worktree_status"), ("tool_post", "worktree_status")]


async def test_pm_tool_loop_forwards_llm_stream_chunks(tmp_path: Path):
    chunks: list[dict] = []

    async def on_stream(chunk: dict) -> None:
        chunks.append(chunk)

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            assert on_stream is not None
            await on_stream({"kind": "output", "delta": "planning", "event_type": "chunk"})
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "streamed",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "do the work",
                }
            )

    outcome = await PMToolLoop(
        FakeLLM(),
        _runtime(tmp_path),
        on_stream=on_stream,
    ).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.final_plan["summary"] == "streamed"
    assert chunks == [{"kind": "output", "delta": "planning", "event_type": "chunk"}]


async def test_read_search_write_replace_and_path_guard(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    rt = _runtime(tmp_path, file_write=True)
    listed = await rt.call(ToolCall("list", "list_files", {"path": "."}))
    assert listed.ok and "src/a.txt" in listed.data["files"]
    read = await rt.call(ToolCall("read", "read_file", {"path": "src/a.txt", "end_line": 1}))
    assert read.data["text"] == "alpha"
    matches = await rt.call(ToolCall("search", "search_repo", {"query": "alpha"}))
    assert [m["line"] for m in matches.data["matches"]] == [1, 3]
    escaped = await rt.call(ToolCall("escape", "read_file", {"path": str(outside)}))
    assert escaped.ok is False and escaped.error == "path_outside_workspace"

    written = await rt.call(ToolCall("write", "write_file", {"path": "new.txt", "text": "one"}))
    assert written.ok and (tmp_path / "new.txt").read_text(encoding="utf-8") == "one"
    duplicate = await rt.call(
        ToolCall("replace", "replace_in_file", {"path": "src/a.txt", "old": "alpha", "new": "x"})
    )
    assert duplicate.ok is False and duplicate.data["match_count"] == 2
    unique = await rt.call(
        ToolCall("replace2", "replace_in_file", {"path": "src/a.txt", "old": "beta", "new": "B"})
    )
    assert unique.ok and "B" in (tmp_path / "src" / "a.txt").read_text(encoding="utf-8")


async def test_disabled_write_run_command_gate_and_web_taint(tmp_path: Path):
    disabled = await _runtime(tmp_path).call(
        ToolCall("w", "write_file", {"path": "x.txt", "text": "x"})
    )
    assert disabled.error == "tool_disabled"
    disabled_test = await _runtime(tmp_path).call(
        ToolCall("test", "test_run", {"command": "python --version"})
    )
    assert disabled_test.error == "tool_disabled"

    rt = _runtime(tmp_path, shell=True)
    cmd = await rt.call(ToolCall("cmd", "run_command", {"command": "python --version"}))
    assert cmd.ok and cmd.data["returncode"] == 0
    assert "Python" in (cmd.data["stdout"] + cmd.data["stderr"])
    denied = await rt.call(ToolCall("deny", "run_command", {"command": "git push"}))
    assert denied.error == "requires_approval"
    open_command = await rt.call(ToolCall("open", "run_command", {"command": "python -V"}))
    assert open_command.ok and open_command.data["returncode"] == 0
    tainted = await rt.call(
        ToolCall("taint", "run_command", {"command": "python --version"}),
        context_taint=[EXTERNAL_WEB],
    )
    assert tainted.error == "shell_after_web_requires_approval"


async def test_run_command_streams_to_events_and_ignores_shell_timeout(tmp_path: Path):
    command = (
        f'"{sys.executable}" -c "import time; '
        "print('start', flush=True); time.sleep(1.2); print('done', flush=True)\""
    )
    events: list[tuple[str, dict]] = []
    rt = _runtime(tmp_path, shell=True, timeout_s=1)

    result = await rt.call(
        ToolCall("cmd", "run_command", {"command": command}),
        event_sink=lambda t, p: events.append((t, p)),
    )

    assert result.ok
    assert result.data["returncode"] == 0
    assert "start" in result.data["stdout"] and "done" in result.data["stdout"]
    assert result.data["log_path"].endswith(".log")
    assert Path(result.data["log_path"]).read_text(encoding="utf-8").startswith("$ ")
    stream = [p for t, p in events if t == "tool_stream"]
    assert stream and {p["stream"] for p in stream} == {"stdout"}
    assert all(p["log_path"] == result.data["log_path"] for p in stream)


async def test_run_command_requires_approval_can_continue_after_question(tmp_path: Path):
    class FakeCards:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def ask_question(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "choice": "approve"}

    cards = FakeCards()
    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=tmp_path,
            allowed_roots=[tmp_path],
            shell=True,
        ),
        gate=Gate(GatesCfg(requires_approval=["python --version"], needs_strategy=[])),
        cards=cards,
    )
    rt.set_decision_context("s1", "t1")

    result = await rt.call(ToolCall("cmd", "run_command", {"command": "python --version"}))

    assert result.ok
    assert cards.calls and cards.calls[0]["session_id"] == "s1"
    assert cards.calls[0]["options"][0]["action"] == "approve"


async def test_run_command_cancel_stops_child_process_tree(tmp_path: Path):
    flag = tmp_path / "child-still-ran.txt"
    child = f"import time, pathlib; time.sleep(2); pathlib.Path(r'{flag}').write_text('alive')"
    command = f'"{sys.executable}" -c "{child}"'
    rt = _runtime(tmp_path, shell=True)

    task = asyncio.create_task(rt.call(ToolCall("cmd", "run_command", {"command": command})))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(2.5)

    assert not flag.exists()


async def test_run_command_uses_auditor_only_for_gate_gray_area(tmp_path: Path):
    class FakeAuditor:
        def __init__(self) -> None:
            self.calls = 0

        async def audit(self, command, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                verdict="revise",
                goal_quality="weak",
                risk_severity="mild",
                reasons=["too broad"],
                suggestions=["narrow it"],
            )

    auditor = FakeAuditor()
    gate = Gate(GatesCfg(requires_approval=[], needs_strategy=["python --version"]))
    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=tmp_path,
            allowed_roots=[tmp_path],
            shell=True,
        ),
        gate=gate,
        auditor=auditor,
    )

    result = await rt.call(ToolCall("gray", "run_command", {"command": "python --version"}))

    assert auditor.calls == 1
    assert result.ok is False
    assert result.error == "auditor_revise"
    assert result.data["reasons"] == ["too broad"]


async def test_run_command_event_sink_failure_does_not_stop_process(tmp_path: Path):
    command = f'"{sys.executable}" -c "print(\'event sink should not stop me\')"'
    rt = _runtime(tmp_path, shell=True)

    def broken_sink(_event_type: str, _payload: dict) -> None:
        raise RuntimeError("ui stream failed")

    result = await rt.call(
        ToolCall("cmd", "run_command", {"command": command}),
        event_sink=broken_sink,
    )

    assert result.ok
    assert "event sink should not stop me" in result.data["stdout"]


async def test_fetch_url_marks_external_web_content(tmp_path: Path):
    server, url = _serve_text()
    try:
        rt = _runtime(tmp_path, web_fetch=True)
        result = await rt.call(ToolCall("fetch", "fetch_url", {"url": url}))
        assert result.ok and "hello from local" in result.data["text"]
        assert result.taint == [EXTERNAL_WEB]
    finally:
        server.shutdown()


async def test_pm_loop_propagates_external_web_taint_to_next_tool(tmp_path: Path):
    server, url = _serve_text()

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {"id": "fetch", "name": "fetch_url", "arguments": {"url": url}}
                        ],
                    }
                )
            if self.calls == 2:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "cmd",
                                "name": "run_command",
                                "arguments": {"command": "python --version"},
                            }
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "taint verified",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "report taint behavior",
                }
            )

    events: list[tuple[str, dict]] = []
    rt = _runtime(
        tmp_path,
        shell=True,
        web_fetch=True,
    )
    try:
        outcome = await PMToolLoop(
            FakeLLM(),
            rt,
            max_rounds=3,
            on_tool_event=lambda t, p: events.append((t, p)),
        ).run(
            [Message("user", "fetch then command")],
            fallback_plan={
                "agent": "codex",
                "model": "",
                "effort": "high",
                "instruction": "fallback",
            },
            enabled_agents=["codex"],
        )
    finally:
        server.shutdown()

    post_outputs = [json.loads(p["output"]) for t, p in events if t == "tool_post"]
    assert outcome.final_plan["summary"] == "taint verified"
    assert post_outputs[0]["taint"] == [EXTERNAL_WEB]
    assert post_outputs[1]["error"] == "shell_after_web_requires_approval"


async def test_pm_loop_rejects_final_plan_after_unverified_web_search(tmp_path: Path):
    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "search",
                                "name": "web_search",
                                "arguments": {"query": "pm tools", "max_results": 1},
                            }
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "search says it is true",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "act on unverified search",
                }
            )

    rt = _runtime(tmp_path, web_search=True)
    outcome = await PMToolLoop(FakeLLM(), rt, max_rounds=2).run(
        [Message("user", "search then finish")],
        fallback_plan={
            "agent": "codex",
            "model": "",
            "effort": "high",
            "instruction": "fallback",
        },
        enabled_agents=["codex"],
    )

    assert outcome.incomplete is True
    assert outcome.rounds[-1]["error"] == "web_search_leads_unverified"


async def test_pm_loop_accepts_final_plan_after_web_search_fetch_verification(tmp_path: Path):
    server, url = _serve_text()

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "search",
                                "name": "web_search",
                                "arguments": {"query": "pm tools", "max_results": 1},
                            }
                        ],
                    }
                )
            if self.calls == 2:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {"id": "fetch", "name": "fetch_url", "arguments": {"url": url}}
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "source fetched",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "report fetched source",
                }
            )

    rt = _runtime(tmp_path, web_search=True, web_fetch=True)
    try:
        outcome = await PMToolLoop(FakeLLM(), rt, max_rounds=3).run(
            [Message("user", "search fetch finish")],
            fallback_plan={
                "agent": "codex",
                "model": "",
                "effort": "high",
                "instruction": "fallback",
            },
            enabled_agents=["codex"],
        )
    finally:
        server.shutdown()

    assert outcome.incomplete is False
    assert outcome.final_plan["summary"] == "source fetched"


async def test_invalid_tool_args_max_rounds_and_final_validator(tmp_path: Path):
    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {"id": "bad", "name": "read_file", "arguments": "not-object"},
                            {"id": "unknown", "name": "no_such_tool", "arguments": {}},
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "done",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "run after evidence",
                    "todo": ["verify"],
                    "ready": True,
                }
            )

    events: list[tuple[str, dict]] = []
    rt = _runtime(tmp_path)
    loop = PMToolLoop(FakeLLM(), rt, max_rounds=3, on_tool_event=lambda t, p: events.append((t, p)))
    outcome = await loop.run(
        [Message("system", "sys"), Message("user", "tool_schema runtime_context policy_context")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )
    assert outcome.final_plan["instruction"] == "run after evidence"
    post_outputs = [json.loads(p["output"]) for t, p in events if t == "tool_post"]
    assert {item["error"] for item in post_outputs} == {"invalid_args", "unknown_tool"}

    class NeverFinal:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            return json.dumps({"type": "tool_calls", "tool_calls": []})

    outcome = await PMToolLoop(NeverFinal(), rt, max_rounds=1).run(
        [Message("user", "x")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )
    assert outcome.incomplete is True
    assert outcome.final_plan["tool_loop_incomplete"] is True

    bad = {
        "type": "final_plan",
        "agent": "bad",
        "instruction": "x",
        "effort": "high",
    }
    try:
        validate_final_plan(bad, enabled_agents=["codex"], fallback_plan={"agent": "codex"})
    except ValueError as exc:
        assert "bad_agent" in str(exc)
    else:
        raise AssertionError("validator should reject unknown agents")


def test_validate_final_plan_clamps_schema_bounds():
    # Medium-2 hardening: the ws backend may not enforce the submit_plan input_schema, so the
    # validator clamps the §5 structural bounds (maxLength/maxItems) itself rather than trust
    # whatever the upstream sends through as tool arguments.
    obj = {
        "type": "final_plan",
        "agent": "codex",
        "effort": "high",
        "instruction": "i" * 7000,
        "summary": "s" * 800,
        "model": "m" * 120,
        "workspace": "w" * 700,
        "todo": ["t" * 400 for _ in range(20)],
        "deliberation": ["d" * 400 for _ in range(20)],
        "ready": True,
    }
    plan = validate_final_plan(
        obj,
        enabled_agents=["codex"],
        fallback_plan={"agent": "codex"},
        max_plan_items=15,
    )
    assert len(plan["summary"]) == 600
    assert len(plan["model"]) == 80
    assert len(plan["workspace"]) == 500
    assert len(plan["instruction"]) == 6000
    assert len(plan["todo"]) == 15 and all(len(x) <= 200 for x in plan["todo"])
    assert len(plan["deliberation"]) == 15 and all(len(x) <= 300 for x in plan["deliberation"])


def test_json_fallback_accepts_flat_tool_arguments():
    calls = _calls_from_json(
        {
            "type": "tool_calls",
            "tool_calls": [
                {
                    "id": "click-1",
                    "name": "browser_click",
                    "ref": "ref-1",
                },
                {
                    "id": "type-1",
                    "tool": "browser_type",
                    "ref": "ref-2",
                    "text": "hello",
                },
                {
                    "id": "click-2",
                    "name": "browser_click",
                    "input": {"ref": "ref-3"},
                },
            ],
        }
    )

    assert [(call.name, call.arguments) for call in calls] == [
        ("browser_click", {"ref": "ref-1"}),
        ("browser_type", {"ref": "ref-2", "text": "hello"}),
        ("browser_click", {"ref": "ref-3"}),
    ]


def test_submit_plan_tool_spec_constrains_agent_enum():
    spec = submit_plan_tool_spec(["codex"], max_plan_items=17)
    assert spec["name"] == SUBMIT_PLAN_TOOL
    schema = spec["input_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["agent"]["enum"] == ["codex"]
    assert schema["properties"]["workspace"]["maxLength"] == 500
    assert schema["properties"]["todo"]["maxItems"] == 17
    assert schema["properties"]["deliberation"]["maxItems"] == 17
    # Empty/None enabled set falls back to all supported planning agents.
    assert submit_plan_tool_spec([])["input_schema"]["properties"]["agent"]["enum"] == [
        "claude-code",
        "codex",
        "copilot-cli",
    ]
    clamped = submit_plan_tool_spec(["codex"], max_plan_items=9999999)["input_schema"]
    assert clamped["properties"]["todo"]["maxItems"] == 999
    assert clamped["properties"]["deliberation"]["maxItems"] == 999


class _ScriptedToolLLM:
    """A ws-style LLM whose ``tool_complete`` returns pre-scripted tool calls per round and records
    the ``tool_choice`` each round received (so the test can assert auto vs forced submit)."""

    def __init__(self, scripted: list[LLMToolResponse]) -> None:
        self.scripted = scripted
        self.tool_choices: list[object] = []
        self.tools_seen: list[list[str]] = []
        self.raw_tools_seen: list[list[dict]] = []
        self._round = 0

    async def tool_complete(
        self, messages, *, tools, model="", json_mode=False, tool_choice="auto", on_stream=None
    ) -> LLMToolResponse:
        self.tool_choices.append(tool_choice)
        self.tools_seen.append([t["name"] for t in tools])
        self.raw_tools_seen.append(tools)
        resp = self.scripted[min(self._round, len(self.scripted) - 1)]
        self._round += 1
        return resp


def _submit_call(**overrides) -> LLMToolCall:
    args = {
        "summary": "ship it",
        "agent": "codex",
        "model": "",
        "effort": "high",
        "instruction": "do the work",
        "todo": ["inspect"],
        "deliberation": ["evidence read"],
        "ready": True,
    }
    args.update(overrides)
    return LLMToolCall(id="submit-1", name=SUBMIT_PLAN_TOOL, arguments=args)


async def test_pm_loop_native_path_ignores_text_final_plan(tmp_path: Path):
    # §0.5-1 / §11.1-B: on the native (tool_complete) transport the plan must terminate via a
    # submit_plan tool CALL. A model that emits a final_plan as free TEXT (the repetition-prone
    # shape that hung #39) must NOT terminate the loop — it falls through to the conservative
    # fallback instead of letting repeatable text drive the control flow.
    text_plan = json.dumps(
        {
            "type": "final_plan", "agent": "codex", "effort": "high",
            "instruction": "smuggled via text", "summary": "should be ignored",
        }
    )
    llm = _ScriptedToolLLM([LLMToolResponse(text=text_plan, tool_calls=[])])
    outcome = await PMToolLoop(llm, _runtime(tmp_path), max_rounds=1).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )
    assert outcome.incomplete is True
    assert outcome.final_plan["instruction"] == "fallback"
    assert outcome.final_plan["summary"] != "should be ignored"


async def test_pm_loop_submit_plan_tool_terminates_on_auto_round(tmp_path: Path):
    # T1.4: an evidence (auto) round can read a file, then the model calls submit_plan natively to
    # terminate — the plan arrives as validated tool arguments, no regex over free text.
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    scripted = [
        LLMToolResponse(
            text="",
            tool_calls=[LLMToolCall(id="c1", name="read_file", arguments={"path": "README.md"})],
        ),
        LLMToolResponse(text="", tool_calls=[_submit_call()]),
    ]
    llm = _ScriptedToolLLM(scripted)
    events: list[tuple[str, dict]] = []
    outcome = await PMToolLoop(
        llm, _runtime(tmp_path), max_rounds=6, on_tool_event=lambda t, p: events.append((t, p))
    ).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.incomplete is False
    assert outcome.final_plan["summary"] == "ship it"
    assert outcome.final_plan["instruction"] == "do the work"
    assert outcome.final_plan["todo"] == ["inspect"]
    # The evidence round really ran read_file, and submit_plan was offered as a tool on auto.
    assert "read_file" in [p["tool"] for t, p in events if t == "tool_pre"]
    assert SUBMIT_PLAN_TOOL in llm.tools_seen[0]
    assert llm.tool_choices[0] == "auto"
    submit_spec = next(t for t in llm.raw_tools_seen[0] if t["name"] == SUBMIT_PLAN_TOOL)
    assert submit_spec["input_schema"]["properties"]["todo"]["maxItems"] == 6
    assert submit_spec["input_schema"]["properties"]["deliberation"]["maxItems"] == 6


async def test_pm_loop_can_ask_question_before_submit_plan(tmp_path: Path):
    class _FakeCards:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def ask_question(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "card_id": "q1", "choice": "B", "label": "Observe demo"}

    scripted = [
        LLMToolResponse(
            text="",
            tool_calls=[
                LLMToolCall(
                    id="q",
                    name="ask_question",
                    arguments={
                        "question": "Pick a path",
                        "options": [
                            {"label": "Read docs", "value": "A"},
                            {"label": "Observe demo", "value": "B"},
                        ],
                    },
                )
            ],
        ),
        LLMToolResponse(text="", tool_calls=[_submit_call(summary="user picked B")]),
    ]
    cards = _FakeCards()
    runtime = _runtime(tmp_path, cards=cards)
    runtime.set_decision_context("s1", "t1")
    events: list[tuple[str, dict]] = []

    outcome = await PMToolLoop(
        _ScriptedToolLLM(scripted),
        runtime,
        max_rounds=6,
        on_tool_event=lambda t, p: events.append((t, p)),
    ).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.incomplete is False
    assert outcome.final_plan["summary"] == "user picked B"
    assert cards.calls[0]["session_id"] == "s1"
    assert cards.calls[0]["question"] == "Pick a path"
    post = [p for t, p in events if t == "tool_post" and p["tool"] == "ask_question"][0]
    result = json.loads(post["output"])["data"]
    assert result["choice"] == "B"


async def test_pm_loop_forces_submit_plan_on_final_round_no_fallback(tmp_path: Path):
    # T1.4 root fix for #39: a model that would otherwise loop forever (always asking for more
    # evidence — the repetition/stall shape) is FORCED to submit_plan on the final round, so the
    # loop ends with a REAL plan instead of degrading to the conservative fallback.
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    evidence = LLMToolResponse(
        text="", tool_calls=[LLMToolCall(id="e", name="read_file", arguments={"path": "a.txt"})]
    )
    submit = LLMToolResponse(text="", tool_calls=[_submit_call(instruction="forced plan")])

    class _ForcedLLM:
        def __init__(self) -> None:
            self.tool_choices: list[object] = []

        async def tool_complete(
            self, messages, *, tools, model="", json_mode=False, tool_choice="auto", on_stream=None
        ) -> LLMToolResponse:
            self.tool_choices.append(tool_choice)
            # Only submit when the loop forces the submit_plan tool_choice (final round).
            if tool_choice == {"type": "function", "name": SUBMIT_PLAN_TOOL}:
                return submit
            return evidence

    llm = _ForcedLLM()
    outcome = await PMToolLoop(llm, _runtime(tmp_path), max_rounds=3).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.incomplete is False  # did NOT degrade to fallback
    assert outcome.final_plan["instruction"] == "forced plan"
    assert llm.tool_choices[:2] == ["auto", "auto"]  # evidence rounds were auto
    assert llm.tool_choices[2] == {"type": "function", "name": SUBMIT_PLAN_TOOL}  # final forced


async def test_pm_loop_rejects_submit_plan_until_web_search_verified(tmp_path: Path):
    # The web_search → verify guard must apply to the native submit_plan path too, not just the
    # legacy final_plan text path: a submit_plan straight after web_search is rejected; once a
    # local read verifies the leads, the next submit_plan is accepted.
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    scripted = [
        LLMToolResponse(
            text="",
            tool_calls=[LLMToolCall(id="s", name="web_search", arguments={"query": "foreman"})],
        ),
        LLMToolResponse(text="", tool_calls=[_submit_call(summary="too early")]),
        LLMToolResponse(
            text="",
            tool_calls=[LLMToolCall(id="r", name="read_file", arguments={"path": "README.md"})],
        ),
        LLMToolResponse(text="", tool_calls=[_submit_call(summary="verified")]),
    ]
    llm = _ScriptedToolLLM(scripted)
    rt = _runtime(tmp_path, web_search=True)
    outcome = await PMToolLoop(llm, rt, max_rounds=6).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    # The first submit (round 2) is rejected as unverified; the verified submit (round 4) lands.
    assert [r for r in outcome.rounds if r.get("error") == "web_search_leads_unverified"]
    assert outcome.incomplete is False
    assert outcome.final_plan["summary"] == "verified"
