"""P1b-context — token-aware budget, deterministic pack, protect-core, auto-compact (DESIGN §8B).

Unit: the budgeter (approx/char_budget/window-resolve/should_auto_compact), deterministic _dump,
main-path core protection, and the work-mode-aware COMPACT_SYSTEM. Integration: a REAL PM dispatch
that crosses the window threshold auto-compacts before planning and emits before/after token telemetry.
"""

from __future__ import annotations

import asyncio
import json

from foreman.client.core.context_budget import (
    DEFAULT_CTX_WINDOW_TOKENS,
    OUTPUT_RESERVE_TOKENS,
    approx_tokens,
    char_budget,
    resolve_window_tokens,
    should_auto_compact,
)
from foreman.client.core.context_compression import context_pack_to_text
from foreman.client.core.dispatch_service import DispatchService
from foreman.client.core.pm_agent import COMPACT_SYSTEM, PMAgent
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.client.tools import PMToolRuntime
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import EventBus, make_event


# ── budgeter units ────────────────────────────────────────────────────────────────────────────────
def test_approx_and_char_budget():
    assert approx_tokens("") == 0
    assert approx_tokens("a" * 8) == 2
    assert char_budget(1000, 0.25) == 1000  # 250 tokens × 4 chars
    assert char_budget(1000, 0.0) == 0


async def test_resolve_window_uses_context_length_else_default():
    class WithCtx:
        async def list_model_infos(self):
            return [{"id": "m", "context_length": 100_000}]

    class NoCtx:
        async def list_model_infos(self):
            return [{"id": "m"}]  # proxy omitted context_length

    class Broken:
        async def list_model_infos(self):
            raise RuntimeError("no /models")

    class OtherModel:
        async def list_model_infos(self):
            return [{"id": "some-other-model", "context_length": 100_000}]

    assert await resolve_window_tokens(WithCtx(), "m") == 100_000 - OUTPUT_RESERVE_TOKENS
    assert await resolve_window_tokens(NoCtx(), "m") == DEFAULT_CTX_WINDOW_TOKENS - OUTPUT_RESERVE_TOKENS
    assert await resolve_window_tokens(Broken(), "m") == DEFAULT_CTX_WINDOW_TOKENS - OUTPUT_RESERVE_TOKENS
    assert await resolve_window_tokens(object(), "m") == DEFAULT_CTX_WINDOW_TOKENS - OUTPUT_RESERVE_TOKENS
    # a SPECIFIC model that matches NO listed entry must fall back to DEFAULT — never borrow an
    # unrelated model's window (regression: _context_length_for previously returned the first model).
    assert (await resolve_window_tokens(OtherModel(), "gpt-5.5")
            == DEFAULT_CTX_WINDOW_TOKENS - OUTPUT_RESERVE_TOKENS)


def test_should_auto_compact_threshold_and_every_n():
    # threshold: 700+0+0 >= 0.70 * 1000
    assert should_auto_compact(700, 0, 0, window_tokens=1000, run_count=1) is True
    assert should_auto_compact(600, 0, 0, window_tokens=1000, run_count=1) is False
    # every-N: run 8 triggers regardless of tokens
    assert should_auto_compact(0, 0, 0, window_tokens=1000, run_count=8) is True
    assert should_auto_compact(0, 0, 0, window_tokens=1000, run_count=7) is False


# ── deterministic pack (KV-cache stable prefix) ───────────────────────────────────────────────────
def test_pack_render_is_deterministic_and_key_order_independent():
    pack_a = {"session_state": {"goal_quote": "g", "summary": "s"},
              "working_memory": {"constraints": [{"text": "c1"}]}}
    pack_b = {"working_memory": {"constraints": [{"text": "c1"}]},
              "session_state": {"summary": "s", "goal_quote": "g"}}
    a1 = context_pack_to_text(pack_a)
    a2 = context_pack_to_text(pack_a)
    assert a1 == a2  # same input → same bytes
    assert context_pack_to_text(pack_a) == context_pack_to_text(pack_b)  # key order irrelevant


# ── protect-core on the main path ─────────────────────────────────────────────────────────────────
def test_constraints_and_verified_facts_survive_eviction():
    pack = {
        "session_state": {"goal_quote": "g", "summary": "x" * 200},
        "working_memory": {
            "constraints": [{"text": f"C{i}", "importance": 90} for i in range(3)],
            "verified_facts": [{"text": f"F{i}", "importance": 90} for i in range(3)],
            "claims": [{"text": "claim " * 50, "importance": 10} for _ in range(40)],
        },
    }
    rendered = context_pack_to_text(pack, max_chars=900)
    data = json.loads(rendered)
    wm = data.get("working_memory", {})
    assert len(wm.get("constraints", [])) >= 3
    assert len(wm.get("verified_facts", [])) >= 3
    texts = {c["text"] for c in wm["constraints"]}
    assert {"C0", "C1", "C2"} <= texts


# ── COMPACT_SYSTEM is work-mode aware ─────────────────────────────────────────────────────────────
def test_compact_system_mentions_workmode_reference_rule():
    assert "workmode:<kind>:<name>@v<ver>" in COMPACT_SYSTEM
    assert "do NOT copy their verbatim bodies" in COMPACT_SYSTEM
    assert "decisions and constraints" in COMPACT_SYSTEM


# ── integration: auto-compact fires before planning on a bloated session ──────────────────────────
class _FakeHandle:
    session_id = "s"


class _FakeRunner:
    async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
        h = _FakeHandle()
        h.session_id = session_id
        self._store.add_event(make_event("stop", agent, session_id, payload={"result": "done"}))
        return h

    async def wait(self, handle):
        return None


class _SmallWindowLLM:
    """No tool_complete (loop uses the JSON path). Tiny context window forces auto-compact.
    Returns a compact pack / final_plan / review-done by inspecting the system prompt."""

    model = "tiny"

    async def list_model_infos(self):
        return [{"id": "tiny", "context_length": 200}]  # → clamped tiny window

    async def complete(self, messages, *, json_mode=False, model="", on_stream=None, state_key=""):
        sys = messages[0].content
        if "compacting a Foreman coding session" in sys:
            return json.dumps({"version": 1, "session_state": {"goal_quote": "g", "summary": "compacted"},
                               "working_memory": {}, "retrieved_evidence": [], "dynamic_tail": [],
                               "omitted": []})
        if "reviewing a coding CLI" in sys:
            return json.dumps({"done": True, "summary": "ok", "reason": "", "follow_up": ""})
        return json.dumps({"type": "final_plan", "summary": "go", "agent": "codex", "model": "",
                           "effort": "high", "instruction": "do it", "todo": [], "ready": True})


async def test_auto_compact_fires_and_emits_token_telemetry(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    # a continued session carrying a big plan (lane-5) + some history so compact has a timeline.
    store.add_session(Session(id="sess", goal="g", workspace=str(tmp_path),
                              status="running", plan="X" * 6000))
    # a large timeline so compaction genuinely shrinks the token count (before > after).
    for i in range(25):
        store.add_event(make_event("agent_output", "codex", "sess",
                                   payload={"t": f"prior work chunk {i} " + "y" * 300}))
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    pm = PMAgent(_SmallWindowLLM(), tool_runtime_factory=lambda ws: PMToolRuntime.from_config(cfg, ws))
    runner = _FakeRunner()
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=pm)

    res = await svc.create("continue the work", session_id="sess")
    await asyncio.gather(*list(svc._tasks))
    assert res["ok"] is True

    rows = store.get_events("sess")
    compacts = [json.loads(e.payload_json) for e in rows if e.type == "context_compact"]
    assert compacts, "expected an auto-compact before planning a bloated session"
    ev = compacts[0]
    assert "before_tokens" in ev and "after_tokens" in ev
    assert ev["before_tokens"] >= ev["after_tokens"]  # compaction shrank the window
    # work_mode telemetry carries per-lane tokens
    wm = [json.loads(e.payload_json) for e in rows if e.type == "work_mode"][0]
    assert "per_lane_tokens" in wm and "session_memory" in wm["per_lane_tokens"]
