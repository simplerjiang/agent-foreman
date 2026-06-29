from __future__ import annotations

from _fakes import FakeProc, fake_adapter

from foreman.client.agents.copilot_cli import CopilotCliAdapter
from foreman.shared.config import AgentCfg


def _cfg(**kwargs) -> AgentCfg:
    return AgentCfg(command=kwargs.pop("command", "copilot"), **kwargs)


def _assert_no_resume_selector(cmd: list[str], *session_ids: str) -> None:
    assert "--session-id" not in cmd
    assert "--connect" not in cmd
    assert "--continue" not in cmd
    assert "--resume" not in cmd
    for session_id in session_ids:
        assert session_id not in cmd


def test_build_cmd_base_shape_without_workspace_context():
    adapter = CopilotCliAdapter(_cfg())

    assert adapter._build_cmd("do Z") == [
        "copilot",
        "-p", "do Z",
        "--no-auto-update",
        "--no-color",
        "--stream", "off",
        "--no-remote",
        "--no-custom-instructions",
        "--output-format", "json",
        "--allow-all-tools",
        "--allow-all-urls",
    ]


def test_build_session_cmd_ignores_foreman_session_and_includes_workspace(tmp_path):
    adapter = CopilotCliAdapter(_cfg(model="cfg-model", effort="high"))
    cmd = adapter._build_session_cmd("do Z", "sess-1", tmp_path, "gpt-5.5", "medium")

    assert cmd == [
        "copilot",
        "-p", "do Z",
        "--no-auto-update",
        "--no-color",
        "--stream", "off",
        "--no-remote",
        "--no-custom-instructions",
        "--output-format", "json",
        "--model", "gpt-5.5",
        "--effort", "medium",
        "--allow-all-tools",
        "--allow-all-urls",
        "--add-dir", str(tmp_path),
    ]
    _assert_no_resume_selector(cmd, "sess-1")
    assert "--allow-all-paths" not in cmd


def test_full_access_false_keeps_headless_tools_but_omits_broad_path_args(tmp_path):
    adapter = CopilotCliAdapter(_cfg(full_access=False))
    cmd = adapter._build_session_cmd("do Z", "sess-1", tmp_path, "", "")

    assert "--allow-all-tools" in cmd
    assert "--allow-all-urls" not in cmd
    assert "--add-dir" not in cmd
    assert "--allow-all-paths" not in cmd
    _assert_no_resume_selector(cmd, "sess-1")


async def test_start_uses_config_model_effort_and_session_id(tmp_path):
    proc = FakeProc(pid=123)
    adapter = fake_adapter(
        CopilotCliAdapter,
        _cfg(model="cfg-model", effort="high"),
        proc,
    )

    handle = await adapter.start("do Z", tmp_path, "foreman-session")

    assert handle.pid == 123
    assert handle.session_id == "foreman-session"
    assert handle.native_session_id is None
    assert handle.model == "cfg-model"
    assert handle.effort == "high"
    assert adapter.spawned_cwd == tmp_path
    assert adapter.spawned_cmd == [
        "copilot",
        "-p", "do Z",
        "--no-auto-update",
        "--no-color",
        "--stream", "off",
        "--no-remote",
        "--no-custom-instructions",
        "--output-format", "json",
        "--model", "cfg-model",
        "--effort", "high",
        "--allow-all-tools",
        "--allow-all-urls",
        "--add-dir", str(tmp_path),
    ]
    _assert_no_resume_selector(adapter.spawned_cmd, "foreman-session")


async def test_start_model_and_effort_override_config(tmp_path):
    proc = FakeProc(pid=123)
    adapter = fake_adapter(
        CopilotCliAdapter,
        _cfg(model="cfg-model", effort="high"),
        proc,
    )

    handle = await adapter.start(
        "do Z", tmp_path, "foreman-session", model="run-model", effort="low"
    )

    assert handle.model == "run-model"
    assert handle.effort == "low"
    assert "run-model" in adapter.spawned_cmd
    assert "low" in adapter.spawned_cmd
    assert "cfg-model" not in adapter.spawned_cmd


async def test_start_sets_responses_wire_api_for_gpt5_byok(tmp_path):
    proc = FakeProc(pid=123)
    adapter = fake_adapter(CopilotCliAdapter, _cfg(model="gpt-5.5"), proc)

    await adapter.start("do Z", tmp_path, "foreman-session")

    assert adapter.spawned_env == {"COPILOT_PROVIDER_WIRE_API": "responses"}


async def test_start_leaves_wire_api_unset_for_non_gpt5_models(tmp_path):
    proc = FakeProc(pid=123)
    adapter = fake_adapter(CopilotCliAdapter, _cfg(model="gpt-4"), proc)

    await adapter.start("do Z", tmp_path, "foreman-session")

    assert adapter.spawned_env == {}


class _MultiSpawnCopilot(CopilotCliAdapter):
    def __init__(self, cfg, procs):
        super().__init__(cfg)
        self._queue = list(procs)
        self.spawned_cmds = []
        self.spawned_cwds = []
        self.spawned_envs = []

    async def _spawn(self, cmd, workspace, env=None):
        self.spawned_cmds.append(cmd)
        self.spawned_cwds.append(workspace)
        self.spawned_envs.append(env)
        return self._queue.pop(0)


async def test_send_uses_fresh_prompt_without_foreman_session_resume(tmp_path):
    first = FakeProc(pid=1, stdout_lines=[b'{"type":"result","result":"done"}\n'])
    second = FakeProc(pid=2, stdout_lines=[b'{"type":"result","result":"done again"}\n'])
    adapter = _MultiSpawnCopilot(_cfg(), [first, second])

    handle = await adapter.start("first", tmp_path, "foreman-session")
    await adapter.send(handle, "follow up")

    resume_cmd = adapter.spawned_cmds[1]
    _assert_no_resume_selector(resume_cmd, "foreman-session")
    assert "follow up" in resume_cmd


async def test_send_preserves_responses_wire_api_for_gpt5_byok(tmp_path):
    first = FakeProc(pid=1, stdout_lines=[b'{"type":"result","result":"done"}\n'])
    second = FakeProc(pid=2, stdout_lines=[b'{"type":"result","result":"done again"}\n'])
    adapter = _MultiSpawnCopilot(_cfg(model="gpt-5.5"), [first, second])

    handle = await adapter.start("first", tmp_path, "foreman-session")
    await adapter.send(handle, "follow up")

    assert adapter.spawned_envs == [
        {"COPILOT_PROVIDER_WIRE_API": "responses"},
        {"COPILOT_PROVIDER_WIRE_API": "responses"},
    ]



async def test_send_does_not_use_captured_native_session_id_as_resume_selector(tmp_path):
    first = FakeProc(
        pid=1,
        stdout_lines=[
            b'{"type":"system","session_id":"native-copilot-session"}\n',
            b'{"type":"result","result":"done"}\n',
        ],
    )
    second = FakeProc(pid=2, stdout_lines=[b'{"type":"result","result":"done again"}\n'])
    adapter = _MultiSpawnCopilot(_cfg(), [first, second])

    handle = await adapter.start("first", tmp_path, "foreman-session")
    _ = [event async for event in adapter.stream(handle)]
    await adapter.send(handle, "follow up")

    resume_cmd = adapter.spawned_cmds[1]
    assert handle.native_session_id == "native-copilot-session"
    _assert_no_resume_selector(resume_cmd, "foreman-session", "native-copilot-session")
    assert "follow up" in resume_cmd


async def test_stream_text_and_json_result(tmp_path):
    lines = [
        b"plain copilot text\n",
        b'{"type":"result","result":"ok"}\n',
    ]
    adapter = fake_adapter(CopilotCliAdapter, _cfg(), FakeProc(stdout_lines=lines))
    handle = await adapter.start("x", tmp_path, "s")

    events = [event async for event in adapter.stream(handle)]

    assert [event.type for event in events] == ["agent_start", "agent_output", "stop"]
    assert events[1].source == "copilot-cli"
    assert events[1].payload == {"text": "plain copilot text"}
    assert events[2].payload["result"] == "ok"


async def test_success_without_json_result_emits_stop(tmp_path):
    adapter = fake_adapter(
        CopilotCliAdapter,
        _cfg(),
        FakeProc(stdout_lines=[b"done as plain text\n"], returncode=0),
    )
    handle = await adapter.start("x", tmp_path, "s")

    events = [event async for event in adapter.stream(handle)]

    assert [event.type for event in events] == ["agent_start", "agent_output", "stop"]
    assert events[-1].payload == {"result": "", "returncode": 0}


async def test_nonzero_exit_emits_error_without_synthetic_stop(tmp_path):
    adapter = fake_adapter(
        CopilotCliAdapter,
        _cfg(),
        FakeProc(
            stdout_lines=[b"partial text\n"],
            stderr_lines=[b"copilot failed\n"],
            returncode=2,
        ),
    )
    handle = await adapter.start("x", tmp_path, "s")

    events = [event async for event in adapter.stream(handle)]

    assert [event.type for event in events] == ["agent_start", "agent_output", "error"]
    assert events[-1].payload["returncode"] == 2
    assert "copilot failed" in events[-1].payload["msg"]
