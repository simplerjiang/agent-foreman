"""P1 — L1 retrieval tools + L0-into-the-live-loop + budget + telemetry (DESIGN §6/§8/§16).

Two layers:
  * unit — the work_mode_search / work_mode_get handlers, deterministic L0 rendering, and the
    token-budget fit, exercised directly on a PMToolRuntime + WorkModeResolver.
  * integration — the REAL PM tool-loop path (DispatchService → PMAgent(tool_runtime_factory) →
    PMToolLoop): a FakeLLM drives work_mode_search → work_mode_get → final_plan, and we assert the L0
    index and the pulled body reach the ACTUAL messages sent to the LLM (NOT build_plan_prompt), and
    that one work_mode telemetry event is emitted. (§14 hard requirement.)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from foreman.client.core.dispatch_service import DispatchService
from foreman.client.core.pm_agent import PMAgent
from foreman.client.core.work_mode_context import (
    WORKMODE_BODY_MAX_CHARS,
    WORKMODE_MAX_PULLS,
    WorkModeResolver,
    approx_tokens,
    fit_l0_index,
    render_l0_index,
    work_mode_prompt_block,
)
from foreman.client.store import Store
from foreman.client.store.models import Definition
from foreman.client.tools import PMToolRuntime, ToolCall
from foreman.client.tools.models import ToolRuntimeConfig
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import EventBus, make_event


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _seed(store, *, kind="code_standard", name="std", body="BODY", description="d", keywords=None):
    meta = {"description": description}
    if keywords:
        meta["keywords"] = keywords
    row = Definition(
        id=uuid.uuid4().hex, kind=kind, name=name, version=1, status="active",
        is_active=True, scope_json="{}", body=body, metadata_json=json.dumps(meta),
    )
    store.add_definition(row)
    store.set_definition_active(row.id)
    return row.id


def _runtime(tmp_path, resolver=None) -> PMToolRuntime:
    rt = PMToolRuntime(ToolRuntimeConfig(workspace=Path(tmp_path), allowed_roots=[Path(tmp_path)]))
    if resolver is not None:
        rt.set_work_mode_resolver(resolver)
    return rt


# ── unit: work_mode_search handler ────────────────────────────────────────────────────────────────
async def test_search_returns_metadata_only_no_body(tmp_path):
    store = _store(tmp_path)
    _seed(store, name="s1", body="SECRET-BODY", description="does s1", keywords=["alpha"])
    rt = _runtime(tmp_path, WorkModeResolver(store, goal="alpha"))
    res = await rt.call(ToolCall("c", "work_mode_search", {"query": "alpha"}))
    assert res.ok and "modes" in res.data
    assert res.data["modes"], "expected at least one applicable mode"
    for m in res.data["modes"]:
        assert set(m.keys()) == {"id", "kind", "name", "description", "est_tokens"}
        assert "SECRET-BODY" not in json.dumps(m)


async def test_search_without_resolver_is_unavailable_not_crash(tmp_path):
    rt = _runtime(tmp_path)  # no resolver attached
    res = await rt.call(ToolCall("c", "work_mode_search", {"query": "x"}))
    assert res.ok is False and res.error == "work_mode_unavailable"


# ── unit: work_mode_get handler ───────────────────────────────────────────────────────────────────
async def test_get_returns_full_body(tmp_path):
    store = _store(tmp_path)
    _seed(store, name="g1", body="THE-FULL-BODY", description="d")
    resolver = WorkModeResolver(store, goal="x")
    rt = _runtime(tmp_path, resolver)
    res = await rt.call(ToolCall("c", "work_mode_get", {"name": "g1", "kind": "code_standard"}))
    assert res.ok and res.data["body"] == "THE-FULL-BODY"
    assert res.truncated is False and resolver.pulls == 1


async def test_get_truncates_oversize_body(tmp_path):
    store = _store(tmp_path)
    _seed(store, name="big", body="x" * (WORKMODE_BODY_MAX_CHARS + 500), description="d")
    rt = _runtime(tmp_path, WorkModeResolver(store, goal="x"))
    res = await rt.call(ToolCall("c", "work_mode_get", {"name": "big"}))
    assert res.ok and res.truncated is True
    assert len(res.data["body"]) == WORKMODE_BODY_MAX_CHARS


async def test_get_missing_is_not_found(tmp_path):
    store = _store(tmp_path)
    rt = _runtime(tmp_path, WorkModeResolver(store, goal="x"))
    res = await rt.call(ToolCall("c", "work_mode_get", {"name": "nope"}))
    assert res.ok is False and res.error == "not_found"


async def test_get_rate_limited_after_max_pulls(tmp_path):
    store = _store(tmp_path)
    _seed(store, name="g", body="b", description="d")
    resolver = WorkModeResolver(store, goal="x")
    rt = _runtime(tmp_path, resolver)
    for _ in range(WORKMODE_MAX_PULLS):
        ok = await rt.call(ToolCall("c", "work_mode_get", {"name": "g"}))
        assert ok.ok
    over = await rt.call(ToolCall("c", "work_mode_get", {"name": "g"}))
    assert over.ok is False and over.error == "max_pulls_exceeded"


async def test_get_without_resolver_is_unavailable(tmp_path):
    rt = _runtime(tmp_path)
    res = await rt.call(ToolCall("c", "work_mode_get", {"name": "x"}))
    assert res.ok is False and res.error == "work_mode_unavailable"


# ── unit: deterministic L0 render + budget fit + prompt block framing ─────────────────────────────
def test_render_l0_index_is_deterministic_and_body_free():
    entries = [
        {"id": "1", "kind": "skill", "name": "a", "description": "do a", "est_tokens": 10},
        {"id": "2", "kind": "code_standard", "name": "b", "description": "do b", "est_tokens": 20},
    ]
    a = render_l0_index(entries)
    b = render_l0_index(entries)
    assert a == b  # same input → same bytes (KV-cache stable prefix)
    assert "body" not in a and "do a" in a and "do b" in a


def test_fit_l0_index_drops_k_then_shrinks_description():
    entries = [
        {"id": str(i), "kind": "skill", "name": f"n{i}", "description": "x" * 100, "est_tokens": i}
        for i in range(8)
    ]
    big = approx_tokens(render_l0_index(entries))
    fitted = fit_l0_index(entries, max_tokens=max(1, big // 4))
    assert len(fitted) < len(entries)  # cut K to fit
    assert approx_tokens(render_l0_index(fitted)) <= max(1, big // 4)
    # under-budget input is returned intact
    assert fit_l0_index(entries, max_tokens=big + 100) == entries


def test_prompt_block_frames_untrusted_and_carries_index():
    entries = [{"id": "1", "kind": "code_standard", "name": "std", "description": "no shortcuts",
                "est_tokens": 5}]
    block = work_mode_prompt_block(entries)
    assert "push/merge/deploy" in block  # untrusted framing (§11)
    assert "work_mode_get" in block       # tells the PM how to pull bodies
    assert "std" in block and "no shortcuts" in block


# ── integration: the REAL tool-loop path (§14) ────────────────────────────────────────────────────
class _FakeHandle:
    session_id = "s"


class _FakeRunner:
    async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
        h = _FakeHandle()
        h.session_id = session_id
        # a terminal event so the review loop can read a timeline and stop
        self._store.add_event(make_event("stop", agent, session_id, payload={"result": "done"}))
        return h

    async def wait(self, handle):
        return None


class _ToolLoopFakeLLM:
    """Drives the JSON tool path: round 1 → work_mode_search, round 2 → work_mode_get, round 3 →
    final_plan. Review → done. Records every final user message actually sent to the LLM."""

    def __init__(self):
        self.sent: list[str] = []

    async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
        if "reviewing a coding CLI" in messages[0].content:
            return json.dumps({"done": True, "summary": "ok", "reason": "", "follow_up": ""})
        last = messages[-1].content
        self.sent.append(last)
        joined = "\n".join(m.content for m in messages)
        if "Runtime-generated tool_results" not in last:
            return json.dumps({"type": "tool_calls", "tool_calls": [
                {"id": "s", "name": "work_mode_search", "arguments": {"query": "shortcuts"}}]})
        if "FORTYTWO-RULE" in joined:  # the body has been pulled → finalize
            return json.dumps({"type": "final_plan", "summary": "apply standard", "agent": "codex",
                               "model": "", "effort": "high",
                               "instruction": "Follow the no-shortcuts standard (FORTYTWO-RULE).",
                               "todo": ["x"], "ready": True})
        return json.dumps({"type": "tool_calls", "tool_calls": [
            {"id": "g", "name": "work_mode_get",
             "arguments": {"name": "no-shortcuts", "kind": "code_standard"}}]})


def _pm_cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    return cfg


async def test_l0_and_pulled_body_reach_actual_llm_input_and_telemetry(tmp_path):
    store = _store(tmp_path)
    _seed(store, kind="code_standard", name="no-shortcuts",
          body="FORTYTWO-RULE: never take shortcuts in the implementation.",
          description="代码规范：不要走捷径。何时用：写实现时。", keywords=["shortcuts", "standard"])
    cfg = _pm_cfg(tmp_path)
    fake = _ToolLoopFakeLLM()
    pm = PMAgent(fake, tool_runtime_factory=lambda ws: PMToolRuntime.from_config(cfg, ws))
    bus = EventBus()
    q = bus.subscribe_queue()
    runner = _FakeRunner()
    runner._store = store
    svc = DispatchService(cfg, store, bus=bus, runner=runner, pm_agent=pm)

    res = await svc.create("实现功能并遵守规范")
    await asyncio.gather(*list(svc._tasks))
    assert res["ok"] is True

    # L0 index block reached the FIRST message actually sent to the LLM (not build_plan_prompt).
    assert fake.sent and "# Work modes (L0 index)" in fake.sent[0]
    assert "no-shortcuts" in fake.sent[0]
    # the work_mode_get body reached a LATER round's actual LLM input.
    assert any("FORTYTWO-RULE" in s for s in fake.sent[1:])
    # the final plan instruction reflects the pulled standard.
    rows = store.get_events(res["session_id"])
    plan_rows = [json.loads(e.payload_json) for e in rows if e.type == "pm_plan"]
    assert plan_rows and "FORTYTWO-RULE" in plan_rows[-1]["instruction"]

    # exactly one work_mode telemetry event, fields complete.
    wm = [json.loads(e.payload_json) for e in rows if e.type == "work_mode"]
    assert len(wm) == 1
    ev = wm[0]
    assert {"selected", "dropped", "index_tokens", "pulls", "body_tokens", "kinds"} <= set(ev)
    assert any(s["name"] == "no-shortcuts" for s in ev["selected"])
    assert ev["pulls"] >= 1 and ev["body_tokens"] >= 1
    assert "code_standard" in ev["kinds"]
    # the event was also published on the bus
    published = []
    while not q.empty():
        published.append(q.get_nowait().type)
    assert "work_mode" in published


async def test_manual_work_mode_ids_pass_through_even_if_irrelevant(tmp_path):
    store = _store(tmp_path)
    picked = _seed(store, kind="skill", name="hand-picked",
                   body="manual body", description="unrelated to the goal", keywords=["zzz"])
    cfg = _pm_cfg(tmp_path)

    class FinalOnlyLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            if "reviewing a coding CLI" in messages[0].content:
                return json.dumps({"done": True, "summary": "ok", "reason": "", "follow_up": ""})
            return json.dumps({"type": "final_plan", "summary": "go", "agent": "codex",
                               "model": "", "effort": "high", "instruction": "do it",
                               "todo": [], "ready": True})

    pm = PMAgent(FinalOnlyLLM(), tool_runtime_factory=lambda ws: PMToolRuntime.from_config(cfg, ws))
    runner = _FakeRunner()
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=pm)

    res = await svc.create("totally different goal", work_mode_ids=[picked])
    await asyncio.gather(*list(svc._tasks))
    rows = store.get_events(res["session_id"])
    wm = [json.loads(e.payload_json) for e in rows if e.type == "work_mode"][0]
    # the hand-picked skill is selected despite zero lexical overlap with the goal.
    assert any(s["name"] == "hand-picked" for s in wm["selected"])


async def test_backward_compatible_no_definitions_emits_empty_event(tmp_path):
    store = _store(tmp_path)  # no definitions at all
    cfg = _pm_cfg(tmp_path)

    class FinalOnlyLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            if "reviewing a coding CLI" in messages[0].content:
                return json.dumps({"done": True, "summary": "ok", "reason": "", "follow_up": ""})
            return json.dumps({"type": "final_plan", "summary": "go", "agent": "codex",
                               "model": "", "effort": "high", "instruction": "do it",
                               "todo": [], "ready": True})

    pm = PMAgent(FinalOnlyLLM(), tool_runtime_factory=lambda ws: PMToolRuntime.from_config(cfg, ws))
    runner = _FakeRunner()
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=pm)
    res = await svc.create("a plain task")
    await asyncio.gather(*list(svc._tasks))
    rows = store.get_events(res["session_id"])
    wm = [json.loads(e.payload_json) for e in rows if e.type == "work_mode"][0]
    assert wm["selected"] == [] and wm["pulls"] == 0 and wm["index_tokens"] == 0
