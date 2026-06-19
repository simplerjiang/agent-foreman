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

    def _build_cmd(self, instruction: str) -> list[str]:
        return [self.cfg.command, "exec", instruction]
