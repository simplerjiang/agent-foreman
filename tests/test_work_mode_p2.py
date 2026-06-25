"""P2 — coding-agent channel lifecycle on the REAL dispatch path (DESIGN §7/§7.3).

Asserts the workspace is injected BEFORE launch, the injection survives the follow-up loop, and it's
cleared once the task truly ends — exercised through DispatchService._pm_launch with a real
WorkspaceInjector (not the injector unit functions alone, §5).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from foreman.client.core.dispatch_service import DispatchService
from foreman.client.core.injector import NATIVE_SKILLS_DIR, WorkspaceInjector
from foreman.client.core.pm_agent import PMAgent
from foreman.client.store import Store
from foreman.client.store.models import Definition
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import EventBus, make_event


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _seed(store, kind, name, body, desc):
    row = Definition(id=uuid.uuid4().hex, kind=kind, name=name, version=1, status="active",
                     is_active=True, scope_json="{}", body=body,
                     metadata_json=json.dumps({"description": desc}))
    store.add_definition(row)
    store.set_definition_active(row.id)


class _FakeHandle:
    session_id = "s"
    cwd = ""


class _FakeRunner:
    def __init__(self, ws):
        self.ws = Path(ws)
        self.injected_at_launch = None
        self.injected_at_waits = []
        self._store = None
        self._follow = 0

    async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
        self.injected_at_launch = (self.ws / "CLAUDE.md").exists()
        h = _FakeHandle()
        h.session_id = session_id
        h.cwd = str(self.ws)
        self._store.add_event(make_event("stop", agent, session_id, payload={"result": "done"}))
        return h

    async def wait(self, handle):
        self.injected_at_waits.append((self.ws / "CLAUDE.md").exists())

    async def send(self, handle, text):
        self._store.add_event(make_event("agent_output", "codex", handle.session_id,
                                         payload={"t": text}))


def _pm(reviews):
    """A PMAgent whose FakeLLM returns final_plan (agent=claude-code) then the given review verdicts."""
    state = {"reviews": list(reviews)}

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None,
                           state_key=""):
            if "reviewing a coding CLI" in messages[0].content:
                r = state["reviews"].pop(0) if state["reviews"] else {"done": True, "summary": "ok",
                                                                       "reason": "", "follow_up": ""}
                return json.dumps(r)
            return json.dumps({"summary": "go", "agent": "claude-code", "model": "", "effort": "high",
                               "instruction": "do the work", "todo": [], "ready": True})

    return PMAgent(FakeLLM())


def _cfg(tmp_path):
    cfg = Config()
    cfg.agents = {"claude-code": AgentCfg(command="claude", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    return cfg


async def test_inject_before_launch_and_clear_after(tmp_path):
    store = _store(tmp_path)
    _seed(store, "skill", "write-tests", "SKILL BODY", "write tests; use before impl")
    _seed(store, "code_standard", "py-style", "STANDARD BODY", "python style")
    cfg = _cfg(tmp_path)
    runner = _FakeRunner(tmp_path)
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=_pm([]),
                          injector=WorkspaceInjector(allowed_roots=[str(tmp_path)]))
    res = await svc.create("do work", workspace=str(tmp_path))
    await asyncio.gather(*list(svc._tasks))
    assert res["ok"] is True
    # injected BEFORE launch (the runner saw CLAUDE.md present)
    assert runner.injected_at_launch is True
    # cleared after the task ended: managed block gone, native skills gone
    if (tmp_path / "CLAUDE.md").exists():
        assert "FOREMAN:BEGIN" not in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    skills_dir = tmp_path / NATIVE_SKILLS_DIR
    assert not skills_dir.exists() or not list(skills_dir.glob("foreman-*"))


async def test_followup_keeps_injection_until_done(tmp_path):
    store = _store(tmp_path)
    _seed(store, "skill", "write-tests", "SKILL BODY", "write tests")
    cfg = _cfg(tmp_path)
    runner = _FakeRunner(tmp_path)
    runner._store = store
    # first review asks for a follow-up, second is done → two waits, injection must persist for both.
    pm = _pm([{"done": False, "summary": "more", "reason": "", "follow_up": "keep going"},
              {"done": True, "summary": "ok", "reason": "", "follow_up": ""}])
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=pm,
                          injector=WorkspaceInjector(allowed_roots=[str(tmp_path)]))
    await svc.create("do work", workspace=str(tmp_path))
    await asyncio.gather(*list(svc._tasks))
    assert len(runner.injected_at_waits) >= 2
    assert all(runner.injected_at_waits)  # injection present at every wait (clear only in finally)


async def test_no_work_modes_no_injection(tmp_path):
    """P2 §4 back-compat: an injector IS wired but NO work modes are selected → zero injection, zero
    residue (the workspace is never touched; the plan instruction goes straight to the CLI)."""
    store = _store(tmp_path)  # no definitions seeded
    cfg = _cfg(tmp_path)
    runner = _FakeRunner(tmp_path)
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=_pm([]),
                          injector=WorkspaceInjector(allowed_roots=[str(tmp_path)]))
    res = await svc.create("do work", workspace=str(tmp_path))
    await asyncio.gather(*list(svc._tasks))
    assert res["ok"] is True
    assert runner.injected_at_launch is False  # workspace untouched at launch
    assert not (tmp_path / "CLAUDE.md").exists() and not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / NATIVE_SKILLS_DIR).exists()
    assert not (tmp_path / ".foreman").exists()


async def test_no_injector_no_residue(tmp_path):
    store = _store(tmp_path)
    _seed(store, "skill", "write-tests", "SKILL BODY", "write tests")
    cfg = _cfg(tmp_path)
    runner = _FakeRunner(tmp_path)
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=_pm([]))  # no injector
    res = await svc.create("do work", workspace=str(tmp_path))
    await asyncio.gather(*list(svc._tasks))
    assert res["ok"] is True
    assert not (tmp_path / NATIVE_SKILLS_DIR).exists()
    assert not (tmp_path / "CLAUDE.md").exists()
