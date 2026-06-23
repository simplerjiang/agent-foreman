"""Tests for CodexAdapter lifecycle + stream parsing (TASKS T1.6).

Shares the SubprocessCliAdapter base with the Claude adapter; only _build_cmd differs.
"""

from __future__ import annotations

from _fakes import FakeProc, fake_adapter

from foreman.client.agents.codex import CodexAdapter
from foreman.shared.config import AgentCfg


def _cfg(model: str = "") -> AgentCfg:
    return AgentCfg(command="codex", model=model)


def test_build_cmd():
    a = CodexAdapter(_cfg())
    assert a._build_cmd("do Y") == [
        "codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "do Y",
    ]
    assert a._build_cmd("do Y", "gpt-5") == [
        "codex", "exec", "--json", "--model", "gpt-5",
        "--dangerously-bypass-approvals-and-sandbox", "do Y",
    ]


def test_full_access_can_be_disabled():
    a = CodexAdapter(AgentCfg(command="codex", full_access=False))
    assert a._build_cmd("do Y") == ["codex", "exec", "--json", "do Y"]


def test_build_cmd_with_effort():
    # Codex carries reasoning level as a `-c model_reasoning_effort=` config override.
    a = CodexAdapter(_cfg())
    assert a._build_cmd("do Y", "gpt-5", "high") == [
        "codex", "exec", "--json", "--model", "gpt-5", "-c", "model_reasoning_effort=high",
        "--dangerously-bypass-approvals-and-sandbox", "do Y",
    ]
    assert a._build_cmd("do Y", "", "low") == [
        "codex", "exec", "--json", "-c", "model_reasoning_effort=low",
        "--dangerously-bypass-approvals-and-sandbox", "do Y",
    ]


def test_build_resume_cmd():
    a = CodexAdapter(_cfg())
    assert a._build_resume_cmd("more", "sess-9") == [
        "codex", "exec", "--json", "resume",
        "--dangerously-bypass-approvals-and-sandbox", "sess-9", "more",
    ]
    assert a._build_resume_cmd("more", "sess-9", "gpt-5") == [
        "codex", "exec", "--json", "resume", "--model", "gpt-5",
        "--dangerously-bypass-approvals-and-sandbox", "sess-9", "more",
    ]


async def test_start_registers_and_returns_handle(tmp_path):
    proc = FakeProc(pid=999)
    a = fake_adapter(CodexAdapter, _cfg(), proc)
    h = await a.start("do Y", tmp_path, "sx")
    assert h.pid == 999 and h.session_id == "sx" and h.id == "sx:999"
    assert a._procs[h.id] is proc
    assert a.spawned_cmd == [
        "codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "do Y",
    ]
    assert a.spawned_cwd == tmp_path


async def test_start_model_override_wins(tmp_path):
    proc = FakeProc(pid=999)
    a = fake_adapter(CodexAdapter, _cfg("cfg-model"), proc)
    h = await a.start("do Y", tmp_path, "sx", model="run-model")
    assert h.model == "run-model"
    assert a.spawned_cmd == [
        "codex", "exec", "--json", "--model", "run-model",
        "--dangerously-bypass-approvals-and-sandbox", "do Y",
    ]


async def test_stream_parses_lines(tmp_path):
    lines = [
        b"plain codex output line\n",
        b'{"type":"result","result":"ok"}\n',
    ]
    a = fake_adapter(CodexAdapter, _cfg(), FakeProc(stdout_lines=lines))
    h = await a.start("x", tmp_path, "s")
    events = [e async for e in a.stream(h)]

    assert [e.type for e in events] == ["agent_output", "stop"]
    assert events[0].source == "codex"
    assert events[0].payload == {"text": "plain codex output line"}
    assert events[1].payload["result"] == "ok"


async def test_stream_classifies_reasoning_json_lines(tmp_path):
    lines = [
        b'{"type":"reasoning","delta":"thinking"}\n',
        b'{"type":"result","result":"ok"}\n',
    ]
    a = fake_adapter(CodexAdapter, _cfg(), FakeProc(stdout_lines=lines))
    h = await a.start("x", tmp_path, "s")
    events = [e async for e in a.stream(h)]

    assert [e.type for e in events] == ["agent_reasoning", "stop"]
    assert events[0].source == "codex"
    assert events[0].payload["delta"] == "thinking"


async def test_stream_classifies_nested_reasoning_items(tmp_path):
    lines = [
        b'{"type":"item.completed","item":{"content":[{"type":"reasoning","summary":"plan"}]}}\n',
        b'{"type":"result","result":"ok"}\n',
    ]
    a = fake_adapter(CodexAdapter, _cfg(), FakeProc(stdout_lines=lines))
    h = await a.start("x", tmp_path, "s")
    events = [e async for e in a.stream(h)]

    assert [e.type for e in events] == ["agent_reasoning", "stop"]
    assert events[0].payload["item"]["content"][0]["summary"] == "plan"


async def test_stream_reports_nonzero_exit_with_stderr(tmp_path):
    a = fake_adapter(
        CodexAdapter,
        _cfg(),
        FakeProc(stdout_lines=[], stderr_lines=[b"codex failed\n"], returncode=2),
    )
    h = await a.start("x", tmp_path, "s")
    events = [e async for e in a.stream(h)]

    assert [e.type for e in events] == ["error"]
    assert events[0].source == "codex"
    assert events[0].payload["returncode"] == 2
    assert "codex failed" in events[0].payload["msg"]


async def test_stop_terminates(tmp_path):
    proc = FakeProc()
    a = fake_adapter(CodexAdapter, _cfg(), proc)
    h = await a.start("x", tmp_path, "s")
    await a.stop(h)
    assert proc.terminated is True
    assert h.id not in a._procs
