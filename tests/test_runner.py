"""Tests for Runner.launch — persist-then-publish, concurrent per session (TASKS T1.7)."""

from __future__ import annotations

import asyncio

import pytest
from _fakes import FakeProc, fake_adapter

from foreman.client.agents.claude_code import ClaudeCodeAdapter
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
    assert {e.type for e in persisted} == {"agent_output", "stop"}
    assert len(persisted) == 2
    # published in stream order
    assert [e.type for e in received] == ["agent_output", "stop"]


async def test_launch_unknown_agent_raises(tmp_path):
    runner = Runner(Config(), EventBus(), _store(tmp_path))
    with pytest.raises(ValueError, match="agent not enabled"):
        await runner.launch("nope", "x", tmp_path, "s")
