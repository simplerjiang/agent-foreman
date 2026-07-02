"""Shared base for subprocess-driven CLI agent adapters (Claude Code, Codex).

Both adapters spawn a CLI in the workspace, stream its stdout line-by-line into AgentEvents,
and stop it. Only the launch command (`_build_cmd`) and `name` differ — those are overridden by
subclasses. See docs/DESIGN.zh-CN.md §4.2.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import locale
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from foreman.shared.config import AgentCfg
from foreman.shared.events import AgentEvent, make_event

from .base import AgentHandle


_STDOUT_READ_CHUNK_BYTES = 64 * 1024


class SubprocessCliAdapter:
    """Spawn → stream stdout → stop. Subclasses set `name` and `_build_cmd`."""

    name = "subprocess"

    def __init__(self, cfg: AgentCfg) -> None:
        self.cfg = cfg  # command, mode, model
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # Remember each handle's workspace so a resume (`send`) re-spawns in the same cwd.
        self._workspaces: dict[str, Path] = {}

    def _model_args(self, model: str) -> list[str]:
        return ["--model", model] if model else []

    def _effective_model(self, model: str = "") -> str:
        return (model or self.cfg.model or "").strip()

    def _effective_effort(self, effort: str = "") -> str:
        """Reasoning level for this run: explicit arg, else the agent's config default ("")."""
        return (effort or getattr(self.cfg, "effort", "") or "").strip()

    def _full_access(self) -> bool:
        return bool(getattr(self.cfg, "full_access", True))

    def _build_cmd(self, instruction: str, model: str = "", effort: str = "") -> list[str]:
        raise NotImplementedError

    def _build_resume_cmd(
        self, instruction: str, native_session_id: str, model: str = "", effort: str = ""
    ) -> list[str]:
        """Command that resumes a prior session with a follow-up instruction (two-way control).

        Subclasses override to add their CLI's resume flag (claude `--resume`, codex `exec resume`).
        Default: a plain re-run (no session continuity) so `send` still works on an adapter that
        has no resume concept. See docs/DESIGN.zh-CN.md §4.2 ("会话续接用 --resume / --continue")."""
        return self._build_cmd(instruction, model, effort)

    def _env_overrides(self, model: str = "", effort: str = "") -> dict[str, str]:
        """Extra environment for the child process. Default none; claude maps effort → an env var
        (it has no CLI flag for it), while codex carries effort in the command instead (§4.2)."""
        return {}

    def _resolve_argv(self, cmd: list[str]) -> list[str]:
        """Resolve argv[0] via PATHEXT so a Windows shim is found (the issue-#3 launch failure).

        ``asyncio.create_subprocess_exec`` → Windows ``CreateProcess``, which does NOT search PATHEXT
        for ``.cmd``/``.bat`` (it only auto-appends ``.exe``). So a bare ``"claude"`` raises
        ``FileNotFoundError [WinError 2]`` even when ``claude.CMD`` is installed and works in the
        shell (npm installs CLIs as ``.CMD`` shims). ``shutil.which`` respects PATHEXT — we spawn the
        resolved absolute path. If it can't be resolved we keep the original name so it still errors
        as genuinely-not-installed. POSIX is unaffected (which finds the same executable)."""
        if not cmd:
            return cmd
        resolved = _which_spawnable(cmd[0])
        return [resolved, *cmd[1:]] if resolved else cmd

    async def _spawn(
        self, cmd: list[str], workspace: Path, env: dict[str, str] | None = None
    ) -> asyncio.subprocess.Process:
        """Spawn the agent process. Overridable seam so tests can inject a fake process.

        ``env`` (when given) is merged onto the parent environment — never replaces it, so the CLI
        still finds PATH / its own credentials."""
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # hide the child console window
        if env:
            kwargs["env"] = {**os.environ, **env}
        return await asyncio.create_subprocess_exec(
            *self._resolve_argv(cmd),
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )

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
        cmd = self._build_cmd(instruction, effective_model, effective_effort)
        proc = await self._spawn(
            cmd,
            workspace,
            self._env_overrides(effective_model, effective_effort),
        )
        git_refs = _detect_git_refs(workspace)
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
        self._workspaces[handle.id] = Path(workspace)
        return handle

    async def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        """Yield stdout events and surface a non-zero process exit as an error event."""
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
        try:
            if proc.stdout is not None:
                async for raw in _iter_pipe_lines(proc.stdout):
                    line = _decode_cli_text(raw).strip()
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
                        if not event.payload.get("status") or event.payload.get("status") == "running":
                            event.payload["status"] = "completed"
                        event.payload.setdefault("returncode", 0)
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
            else:
                handle.status = "completed"
        finally:
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task

    def _line_to_event(self, line: str, session_id: str) -> AgentEvent:
        """Map one output line to an AgentEvent; non-JSON / non-object → raw agent_output.

        Conservative (CLI schemas drift, DESIGN §13.1): keep the full object in payload so detail
        views can extract tool calls later (§6.3); only a `result` line maps to `stop`.
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return make_event("agent_output", self.name, session_id, payload={"text": line})
        if not isinstance(obj, dict):
            return make_event("agent_output", self.name, session_id, payload={"text": line})
        if obj.get("type") == "result":
            etype = "stop"
        elif _is_reasoning_payload(obj):
            etype = "agent_reasoning"
        else:
            etype = "agent_output"
        return make_event(etype, self.name, session_id, payload=obj)

    async def send(self, handle: AgentHandle, text: str) -> None:
        """Append a follow-up instruction by resuming the session (two-way control, DESIGN §4.2).

        These CLIs run one-shot (`-p` / `exec`), so a follow-up means spawning a *resume* process
        rather than writing to a long-lived stdin. We build the resume command (carrying the native
        session id captured during the first stream, when present), spawn it, and re-register it
        under the same handle id so the Runner can re-pump its output to store+bus.
        """
        if handle.native_session_id:
            cmd = self._build_resume_cmd(
                text, handle.native_session_id, handle.model, handle.effort
            )
        else:
            # No captured session id yet → fall back to a fresh run with the follow-up text.
            cmd = self._build_cmd(text, handle.model, handle.effort)
        workspace = self._workspaces.get(handle.id, Path("."))
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

    async def interrupt(self, handle: AgentHandle) -> None:
        """Pause/interrupt the running process (the first rung of the stall ladder, DESIGN §5.6).

        Terminate the live process gracefully; resuming afterwards goes through `send` (`--resume`).
        A process that has already exited is a no-op."""
        proc = self._procs.get(handle.id)
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            pass  # already gone

    async def stop(self, handle: AgentHandle) -> None:
        """Terminate the agent process (graceful → kill) and deregister it."""
        proc = self._procs.pop(handle.id, None)
        self._workspaces.pop(handle.id, None)
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
        except ProcessLookupError:
            pass  # already gone


def _which_spawnable(name: str) -> str | None:
    """Resolve a command to a path that ``create_subprocess_exec`` can launch directly."""
    if not _is_windows():
        return shutil.which(name)
    found = shutil.which(name)
    if found and Path(found).suffix.lower() in {".exe", ".cmd", ".bat", ".com"}:
        return found
    path = Path(name)
    if path.suffix.lower() in {".exe", ".cmd", ".bat", ".com"}:
        return str(path) if path.exists() or path.parent == Path(".") else found
    if path.suffix:
        return None
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        base = Path(directory) / name
        for ext in (".cmd", ".exe", ".bat", ".com"):
            candidate = base.with_suffix(ext)
            if candidate.is_file():
                return str(candidate)
    return None


def _is_windows() -> bool:
    return os.name == "nt"


def _detect_git_refs(workspace: Path) -> dict[str, str]:
    def run_git(*args: str) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(workspace), *args],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=2,
            ).strip()
        except Exception:
            return ""

    branch = run_git("branch", "--show-current")
    head_sha = run_git("rev-parse", "HEAD")
    base_ref = run_git("merge-base", "HEAD", "origin/main")
    return {"branch": branch, "head_sha": head_sha, "base_ref": base_ref}


def _handle_event_payload(handle: AgentHandle, source: str, *, status: str = "") -> dict:
    if status:
        handle.status = status
    return {
        "handle_id": handle.id,
        "agent_id": handle.id,
        "pid": handle.pid,
        "command": handle.command,
        "cwd": handle.cwd,
        "worktree": handle.worktree or handle.cwd,
        "branch": handle.branch,
        "base_ref": handle.base_ref,
        "head_sha": handle.head_sha,
        "model": handle.model,
        "effort": handle.effort,
        "native_session_id": handle.native_session_id or "",
        "agent_type": handle.agent_type or source,
        "source": source,
        "status": status or handle.status or "",
    }


def _is_reasoning_payload(obj: dict) -> bool:
    markers = ("reasoning", "thinking", "thought")
    text = " ".join(
        str(obj.get(key) or "").lower()
        for key in ("type", "subtype", "event", "channel", "role", "name")
    )
    if any(marker in text for marker in markers):
        return True
    for key in ("message", "item"):
        message = obj.get(key)
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").lower()
            if any(marker in block_type for marker in markers):
                return True
            if any(marker in block for marker in markers):
                return True
    return False


async def _read_pipe_text(pipe) -> str:
    """Drain a subprocess pipe and decode text without lossy replacement."""
    try:
        data = await pipe.read()
    except AttributeError:
        chunks: list[bytes] = []
        async for raw in pipe:
            chunks.append(raw)
        data = b"".join(chunks)
    if isinstance(data, str):
        return data.strip()
    return _decode_cli_text(data or b"").strip()


def _decode_cli_text(data: bytes) -> str:
    """Decode CLI output bytes, accepting UTF-8 plus common Windows console encodings."""
    candidates = ["utf-8-sig", locale.getpreferredencoding(False), "gb18030"]
    if _is_windows():
        candidates.append("cp1252")
    seen: set[str] = set()
    for enc in candidates:
        key = (enc or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


async def _iter_pipe_lines(
    pipe,
    *,
    chunk_size: int = _STDOUT_READ_CHUNK_BYTES,
) -> AsyncIterator[bytes]:
    """Yield newline-delimited records without StreamReader's readline limit."""
    pending = bytearray()
    while True:
        chunk = await pipe.read(chunk_size)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", "replace")
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            yield line
    if pending:
        yield bytes(pending)


def _process_error_message(name: str, returncode: int, stderr_text: str) -> str:
    base = f"{name} exited with code {returncode}"
    if stderr_text:
        return f"{base}: {stderr_text[:500]}"
    return base
