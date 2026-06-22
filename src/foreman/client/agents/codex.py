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

    def _build_cmd(self, instruction: str, model: str = "") -> list[str]:
        return [self.cfg.command, "exec", *self._model_args(model), instruction]

    def _build_resume_cmd(
        self, instruction: str, native_session_id: str, model: str = ""
    ) -> list[str]:
        """Resume the prior session with a follow-up (two-way control, DESIGN §4.2)."""
        return [
            self.cfg.command, "exec", "resume",
            *self._model_args(model),
            native_session_id, instruction,
        ]
