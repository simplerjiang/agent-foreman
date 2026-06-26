"""Tests for Runner.launch — persist-then-publish, concurrent per session (TASKS T1.7)."""

from __future__ import annotations

import asyncio

import pytest
from _fakes import FakeProc, fake_adapter

from foreman.client.agents.claude_code import ClaudeCodeAdapter
from foreman.client.agents.copilot_cli import CopilotCliAdapter
from foreman.client.agents.runner import Runner
from foreman.client.store import Store
from foreman.shared.config import AgentCfg, Config
from foreman.shared.events import EventBus


def _store(tmp_path) -> Store:
    st = Store(str(tmp_path / "t.db"))
    st.init()
    return st


async def test_launch_persists_and_publishes(tmp_path):
    store = _store(tmp_path)
    bus = EventBus()
    runner = Runner(Config(), bus, store)
    proc = FakeProc(stdout_lines=[
        b'{"type":"assistant","message":{"content":"hi"}}\n',
        b'{"type":"result","result":"done"}\n',
    ])
    runner.adapters["claude-code"] = fake_adapter(
        ClaudeCodeAdapter, AgentCfg(command="claude"), proc
    )

    received = []

    async def collect():
        async for ev in bus.subscribe():
            received.append(ev)
            if ev.type == "stop":
                break

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0.02)  # let the collector register its subscription before we publish

    handle = await runner.launch("claude-code", "do x", tmp_path, "s1")
    await runner.wait(handle)
    await asyncio.wait_for(collector, timeout=2)

    # persisted (order-independent: same-microsecond ts isn't a stable sort key)
    persisted = store.get_events("s1")
    assert {e.type for e in persisted} == {"agent_start", "agent_output", "stop"}
    assert len(persisted) == 3
    # published in stream order
    assert [e.type for e in received] == ["agent_start", "agent_output", "stop"]


async def test_launch_unknown_agent_raises(tmp_path):
    runner = Runner(Config(), EventBus(), _store(tmp_path))
    with pytest.raises(ValueError, match="agent not enabled"):
        await runner.launch("nope", "x", tmp_path, "s")


def test_sync_config_refreshes_enabled_adapters(tmp_path):
    cfg = Config()
    cfg.agents = {"claude-code": AgentCfg(command="claude", enabled=True)}
    runner = Runner(cfg, EventBus(), _store(tmp_path))
    assert sorted(runner.adapters) == ["claude-code"]

    cfg.agents = {
        "claude-code": AgentCfg(command="claude", enabled=False),
        "codex": AgentCfg(command="codex", enabled=True),
    }
    runner.sync_config()

    assert sorted(runner.adapters) == ["codex"]


def test_sync_config_registers_enabled_copilot_cli(tmp_path):
    cfg = Config()
    cfg.agents = {
        "claude-code": AgentCfg(command="claude", enabled=False),
        "codex": AgentCfg(command="codex", enabled=False),
        "copilot-cli": AgentCfg(command="copilot", enabled=True),
    }
    runner = Runner(cfg, EventBus(), _store(tmp_path))

    assert sorted(runner.adapters) == ["copilot-cli"]
    assert isinstance(runner.adapters["copilot-cli"], CopilotCliAdapter)


# ── two-way control: send (resume) + interrupt (P4 / DESIGN §4.2) ───────────────────────────────────
def _multi_spawn_adapter(adapter_cls, cfg, procs):
    """Adapter whose _spawn returns the next proc each call and records every spawned command."""
    a = adapter_cls(cfg)
    a.spawned_cmds = []
    queue = list(procs)

    async def _spawn(cmd, workspace, env=None):
        a.spawned_cmds.append(cmd)
        return queue.pop(0)

    a._spawn = _spawn
    return a


async def test_send_resumes_session_and_repumps(tmp_path):
    store = _store(tmp_path)
    bus = EventBus()
    runner = Runner(Config(), bus, store)
    first = FakeProc(pid=1, stdout_lines=[
        b'{"type":"system","session_id":"sess-abc"}\n',  # native session id captured during stream
        b'{"type":"result","result":"done"}\n',
    ])
    resumed = FakeProc(pid=2, stdout_lines=[b'{"type":"result","result":"resumed"}\n'])
    adapter = _multi_spawn_adapter(ClaudeCodeAdapter, AgentCfg(command="claude"), [first, resumed])
    runner.adapters["claude-code"] = adapter

    handle = await runner.launch("claude-code", "do x", tmp_path, "s1")
    await runner.wait(handle)
    assert handle.native_session_id == "sess-abc"
    assert runner.handle_for_session("s1") is handle  # addressable by session id

    await runner.send(handle, "now do y")
    await runner.wait(handle)

    # the resume command carried --resume <captured id> and the follow-up text
    resume_cmd = adapter.spawned_cmds[1]
    assert "--resume" in resume_cmd and "sess-abc" in resume_cmd and "now do y" in resume_cmd
    # the resumed output streamed to the store too (re-pumped)
    payloads = [e.payload_json for e in store.get_events("s1")]
    assert any("resumed" in p for p in payloads)


async def test_interrupt_terminates_the_process(tmp_path):
    runner = Runner(Config(), EventBus(), _store(tmp_path))
    proc = FakeProc(pid=7, stdout_lines=[b'{"type":"result","result":"x"}\n'])
    runner.adapters["claude-code"] = fake_adapter(
        ClaudeCodeAdapter, AgentCfg(command="claude"), proc
    )
    handle = await runner.launch("claude-code", "do x", tmp_path, "s1")
    await runner.interrupt(handle)
    assert proc.terminated is True
