from __future__ import annotations

from foreman.client.agents.copilot_cli import CopilotCliAdapter
from foreman.shared.config import AgentCfg


def test_windows_multiline_prompt_stays_in_one_argument(tmp_path, monkeypatch):
    monkeypatch.setattr("foreman.client.agents.copilot_cli._is_windows", lambda: True)
    adapter = CopilotCliAdapter(AgentCfg(command="copilot"))

    cmd = adapter._build_session_cmd("first\r\nsecond\nthird", "session", tmp_path, "gpt-5.4")

    prompt = cmd[cmd.index("-p") + 1]
    assert prompt == "first\u2028second\u2028third"
    assert "\r" not in prompt and "\n" not in prompt


def test_non_windows_multiline_prompt_is_unchanged(monkeypatch):
    monkeypatch.setattr("foreman.client.agents.copilot_cli._is_windows", lambda: False)

    assert CopilotCliAdapter._prompt_arg("first\nsecond") == "first\nsecond"
