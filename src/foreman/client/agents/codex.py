"""Codex CLI adapter — non-interactive `codex exec "<instruction>"`.

Codex has no hook mechanism today, so observation leans on output parsing + the git watcher
(DESIGN §4.2/§4.3). Its output isn't stream-json by default; the base maps JSON lines when present
and falls back to raw agent_output text otherwise.

Lifecycle (spawn / stream / stop) lives in SubprocessCliAdapter; only the launch command differs.
"""

from __future__ import annotations

from ._subprocess import SubprocessCliAdapter


class CodexAdapter(SubprocessCliAdapter):
    name = "codex"

    def _access_args(self) -> list[str]:
        if not self._full_access():
            return []
        return ["--dangerously-bypass-approvals-and-sandbox"]

    def _effort_args(self, effort: str) -> list[str]:
        """Codex carries reasoning level as a config override: `-c model_reasoning_effort=<level>`
        (low|medium|high). Empty → omit, so the model/profile default applies (DESIGN §4.2)."""
        return ["-c", f"model_reasoning_effort={effort}"] if effort else []

    def _build_cmd(self, instruction: str, model: str = "", effort: str = "") -> list[str]:
        return [
            self.cfg.command, "exec", "--json",
            *self._model_args(model), *self._effort_args(effort), *self._access_args(),
            instruction,
        ]

    def _build_resume_cmd(
        self, instruction: str, native_session_id: str, model: str = "", effort: str = ""
    ) -> list[str]:
        """Resume the prior session with a follow-up (two-way control, DESIGN §4.2)."""
        return [
            self.cfg.command, "exec", "--json", "resume",
            *self._model_args(model), *self._effort_args(effort), *self._access_args(),
            native_session_id, instruction,
        ]
