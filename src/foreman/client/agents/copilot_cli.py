"""GitHub Copilot CLI adapter — conservative headless MVP.

The Copilot CLI argument surface is still being validated, so this adapter keeps parsing lenient:
JSON result lines map to stop, JSON/text lines map to agent_output/reasoning where possible, and a
successful process exit emits a synthetic stop if the CLI did not produce one itself. Permission
flags are intentionally workspace-scoped: full_access never implies --allow-all-paths, and Foreman's
internal session id is not passed as a Copilot resume/session selector for fresh prompts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from foreman.shared.events import AgentEvent, make_event

from ._subprocess import SubprocessCliAdapter, _event_returncode, _handle_event_payload, _process_error_message, _read_pipe_text
from .base import AgentHandle, detect_git_refs


class CopilotCliAdapter(SubprocessCliAdapter):
    name = "copilot-cli"

    def _env_overrides(self, model: str = "", effort: str = "") -> dict[str, str]:
        if model.strip().lower().startswith("gpt-5"):
            return {"COPILOT_PROVIDER_WIRE_API": "responses"}
        return {}

    def _effort_args(self, effort: str) -> list[str]:
        return ["--effort", effort] if effort else []

    def _access_args(self, workspace: Path | None = None) -> list[str]:
        # Headless/non-interactive runs need tool permission pre-approved to avoid hanging on a
        # prompt. Path/URL broadening stays guarded by full_access and never includes
        # --allow-all-paths in this MVP.
        args = ["--allow-all-tools"]
        if not self._full_access():
            return args
        args.append("--allow-all-urls")
        if workspace is not None:
            args.extend(["--add-dir", str(workspace)])
        return args

    def _build_cmd(self, instruction: str, model: str = "", effort: str = "") -> list[str]:
        """Base command shape without session/workspace context.

        SubprocessCliAdapter calls this method in its generic start/send path, but Copilot needs the
        workspace for safe --add-dir authorization. start()/send() below use _build_workspace_cmd()
        instead; keeping this method makes the command skeleton testable without changing the shared
        base signature used by Claude/Codex.
        """
        return [
            self.cfg.command,
            "-p", instruction,
            "--no-auto-update",
            "--no-color",
            "--stream", "off",
            "--no-remote",
            "--no-custom-instructions",
            "--output-format", "json",
            *self._access_args(),
            *self._model_args(model),
            *self._effort_args(effort),
        ]

    def _build_workspace_cmd(
        self,
        instruction: str,
        workspace: Path,
        model: str = "",
        effort: str = "",
    ) -> list[str]:
        return [
            self.cfg.command,
            "-p", instruction,
            "--no-auto-update",
            "--no-color",
            "--stream", "off",
            "--no-remote",
            "--no-custom-instructions",
            "--output-format", "json",
            *self._model_args(model),
            *self._effort_args(effort),
            *self._access_args(workspace),
        ]

    def _build_session_cmd(
        self,
        instruction: str,
        session_id: str,
        workspace: Path,
        model: str = "",
        effort: str = "",
    ) -> list[str]:
        """Compatibility shim: Foreman's session id is intentionally ignored for Copilot CLI.

        Copilot CLI 1.0.63 treats session/connect flags as selectors for existing Copilot sessions
        or tasks. Foreman session ids are internal correlation ids, not Copilot UUIDs, so fresh
        prompts must run with `-p/--prompt` and no restore selector.
        """
        return self._build_workspace_cmd(instruction, workspace, model, effort)

    async def start(
        self,
        instruction: str,
        workspace: Path,
        session_id: str,
        model: str = "",
        effort: str = "",
    ) -> AgentHandle:
        effective_model = self._effective_model(model)
        effective_effort = self._effective_effort(effort)
        workspace = Path(workspace)
        cmd = self._build_workspace_cmd(instruction, workspace, effective_model, effective_effort)
        proc = await self._spawn(
            cmd,
            workspace,
            self._env_overrides(effective_model, effective_effort),
        )
        git_refs = detect_git_refs(workspace)
        handle = AgentHandle(
            id=f"{session_id}:{proc.pid}",
            session_id=session_id,
            pid=proc.pid,
            model=effective_model,
            command=cmd,
            cwd=str(workspace),
            worktree=str(workspace),
            effort=effective_effort,
            branch=git_refs.get("branch", ""),
            base_ref=git_refs.get("base_ref", ""),
            head_sha=git_refs.get("head_sha", ""),
            agent_type=self.name,
            source=self.name,
        )
        self._procs[handle.id] = proc
        self._workspaces[handle.id] = workspace
        return handle

    async def send(self, handle: AgentHandle, text: str) -> None:
        workspace = self._workspaces.get(handle.id, Path(handle.cwd or "."))
        # Do not pass Foreman's internal session id (or an unverified Copilot id) through Copilot
        # resume/session selector flags. Spawn a new non-interactive prompt for the follow-up text.
        cmd = self._build_workspace_cmd(text, workspace, handle.model, handle.effort)
        proc = await self._spawn(
            cmd,
            workspace,
            self._env_overrides(handle.model, handle.effort),
        )
        self._procs[handle.id] = proc
        handle.pid = proc.pid
        handle.command = cmd
        handle.cwd = str(workspace)
        handle.worktree = str(workspace)
        handle.status = "running"

    async def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        proc = self._procs.get(handle.id)
        if proc is None:
            return
        yield make_event(
            "agent_start",
            self.name,
            handle.session_id,
            payload=_handle_event_payload(handle, self.name, status="running"),
        )
        stderr_task = (
            asyncio.create_task(_read_pipe_text(proc.stderr))
            if getattr(proc, "stderr", None) is not None
            else None
        )
        emitted_stop = False
        if proc.stdout is not None:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                event = self._line_to_event(line, handle.session_id)
                if not handle.native_session_id:
                    sid = event.payload.get("session_id")
                    if sid:
                        handle.native_session_id = sid
                if event.type in {"agent_output", "agent_reasoning", "stop"}:
                    event.payload = {
                        **_handle_event_payload(handle, self.name),
                        **event.payload,
                    }
                if event.type == "stop":
                    returncode = _event_returncode(event.payload)
                    explicit_status = str(event.payload.get("status") or "").strip().lower()
                    if returncode not in (None, 0) and explicit_status not in {"cancelled", "interrupted"}:
                        event.payload["status"] = "failed"
                    elif not event.payload.get("status") or event.payload.get("status") == "running":
                        event.payload["status"] = "completed"
                    handle.status = str(event.payload.get("status") or handle.status or "")
                    event.payload.setdefault("returncode", 0)
                if event.type == "stop":
                    emitted_stop = True
                yield event

        returncode = await proc.wait()
        stderr_text = await stderr_task if stderr_task is not None else ""
        if returncode:
            yield make_event(
                "error",
                self.name,
                handle.session_id,
                payload={
                    **_handle_event_payload(handle, self.name, status="failed"),
                    "msg": _process_error_message(self.name, returncode, stderr_text),
                    "returncode": returncode,
                    "stderr": stderr_text[-4000:],
                },
            )
            return
        if not emitted_stop:
            yield make_event(
                "stop",
                self.name,
                handle.session_id,
                payload={**_handle_event_payload(handle, self.name, status="completed"), "result": "", "returncode": 0},
            )
        else:
            if handle.status not in {"failed", "cancelled", "interrupted"}:
                handle.status = "completed"
