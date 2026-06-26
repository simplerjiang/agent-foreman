"""GitHub Copilot CLI adapter — conservative headless MVP.

The Copilot CLI argument surface is still being validated, so this adapter keeps parsing lenient:
JSON result lines map to stop, JSON/text lines map to agent_output/reasoning where possible, and a
successful process exit emits a synthetic stop if the CLI did not produce one itself. Permission
flags are intentionally workspace-scoped: full_access never implies --allow-all-paths.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from foreman.shared.events import AgentEvent, make_event

from ._subprocess import SubprocessCliAdapter, _process_error_message, _read_pipe_text
from .base import AgentHandle


class CopilotCliAdapter(SubprocessCliAdapter):
    name = "copilot-cli"

    def _effort_args(self, effort: str) -> list[str]:
        return ["--effort", effort] if effort else []

    def _access_args(self, workspace: Path | None = None) -> list[str]:
        if not self._full_access():
            return []
        args = ["--allow-all-tools", "--allow-all-urls"]
        if workspace is not None:
            args.extend(["--add-dir", str(workspace)])
        return args

    def _build_cmd(self, instruction: str, model: str = "", effort: str = "") -> list[str]:
        """Base command shape without session/workspace context.

        SubprocessCliAdapter calls this method in its generic start/send path, but Copilot needs the
        Foreman session id and workspace for safe --add-dir authorization. start()/send() below use
        _build_session_cmd() instead; keeping this method makes the command skeleton testable without
        changing the shared base signature used by Claude/Codex.
        """
        return [
            self.cfg.command,
            "-p", instruction,
            "--no-auto-update",
            "--output-format", "json",
            *self._model_args(model),
            *self._effort_args(effort),
        ]

    def _build_session_cmd(
        self,
        instruction: str,
        session_id: str,
        workspace: Path,
        model: str = "",
        effort: str = "",
    ) -> list[str]:
        copilot_session_id = _uuid_session_id(session_id)
        return [
            self.cfg.command,
            "-p", instruction,
            "--session-id", copilot_session_id,
            "--no-auto-update",
            "--output-format", "json",
            *self._model_args(model),
            *self._effort_args(effort),
            *self._access_args(workspace),
        ]

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
        cmd = self._build_session_cmd(
            instruction, session_id, workspace, effective_model, effective_effort
        )
        proc = await self._spawn(
            cmd,
            workspace,
            self._env_overrides(effective_model, effective_effort),
        )
        handle = AgentHandle(
            id=f"{session_id}:{proc.pid}",
            session_id=session_id,
            pid=proc.pid,
            model=effective_model,
            command=cmd,
            cwd=str(workspace),
            effort=effective_effort,
        )
        self._procs[handle.id] = proc
        self._workspaces[handle.id] = workspace
        return handle

    async def send(self, handle: AgentHandle, text: str) -> None:
        workspace = self._workspaces.get(handle.id, Path(handle.cwd or "."))
        session_id = handle.native_session_id or handle.session_id
        cmd = self._build_session_cmd(text, session_id, workspace, handle.model, handle.effort)
        proc = await self._spawn(
            cmd,
            workspace,
            self._env_overrides(handle.model, handle.effort),
        )
        self._procs[handle.id] = proc
        handle.pid = proc.pid
        handle.command = cmd
        handle.cwd = str(workspace)
        handle.native_session_id = session_id

    async def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        proc = self._procs.get(handle.id)
        if proc is None:
            return
        yield make_event(
            "agent_start",
            self.name,
            handle.session_id,
            payload={
                "pid": handle.pid,
                "command": handle.command,
                "cwd": handle.cwd,
                "model": handle.model,
                "effort": handle.effort,
            },
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
                payload={"result": "", "returncode": 0},
            )


def _uuid_session_id(value: str) -> str:
    """Copilot CLI accepts --session-id only as a UUID.

    Foreman session ids are 32 hex chars. They are valid UUID bits, but Copilot rejects the raw
    compact form with "The value is not a valid UUID". Canonicalize when possible so a new Copilot
    session can be created deterministically from the Foreman session id; leave already-native
    non-UUID values untouched so future Copilot resume identifiers still pass through.
    """
    text = str(value or "").strip()
    if not text:
        return text
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text
