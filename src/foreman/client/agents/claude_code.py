"""Claude Code adapter — headless stream-json driving.

    claude -p "<instruction>" --output-format stream-json --verbose
    claude -p "<follow-up>" --resume <session_id>   (P4; native_session_id captured during stream)

Lifecycle (spawn / stream / stop, native-session-id capture) lives in SubprocessCliAdapter;
only the launch command differs here. See docs/DESIGN.zh-CN.md §4.2 / §10.
"""

from __future__ import annotations

from ._subprocess import SubprocessCliAdapter


class ClaudeCodeAdapter(SubprocessCliAdapter):
    name = "claude-code"

    def _access_args(self) -> list[str]:
        if not self._full_access():
            return []
        return ["--permission-mode", "bypassPermissions", "--tools", "default"]

    def _build_cmd(self, instruction: str, model: str = "", effort: str = "") -> list[str]:
        return [
            self.cfg.command, "-p", instruction,
            *self._model_args(model),
            *self._access_args(),
            "--output-format", "stream-json", "--verbose",
        ]

    def _build_resume_cmd(
        self, instruction: str, native_session_id: str, model: str = "", effort: str = ""
    ) -> list[str]:
        """Resume the captured session with a follow-up (two-way control, DESIGN §4.2)."""
        return [
            self.cfg.command, "-p", instruction, "--resume", native_session_id,
            *self._model_args(model),
            *self._access_args(),
            "--output-format", "stream-json", "--verbose",
        ]

    def _env_overrides(self, model: str = "", effort: str = "") -> dict[str, str]:
        """Claude Code has no headless flag for reasoning level — it reads the effort from the
        ``CLAUDE_CODE_EFFORT_LEVEL`` env var (low|medium|high|xhigh|max). Empty → CLI default."""
        return {"CLAUDE_CODE_EFFORT_LEVEL": effort} if effort else {}
