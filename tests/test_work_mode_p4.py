"""P4 — soft constraints: qa_rubric / code_standard.check shape review done/follow_up (DESIGN §9, D2).

Hard execution (actually running the check command) is deferred to V2; P4 feeds the rubric body and
the standard's check field into PMAgent.review as the acceptance standard. Integration asserts they
reach the ACTUAL review LLM input on the live _pm_launch path and drive a follow-up.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from foreman.client.core.dispatch_service import DispatchService
from foreman.client.core.pm_agent import REVIEW_SYSTEM, PMAgent, PMPlan, build_review_prompt
from foreman.client.store import Store
from foreman.client.store.models import Definition
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg
from foreman.shared.events import EventBus, make_event


# ── unit: build_review_prompt + REVIEW_SYSTEM ─────────────────────────────────────────────────────
def _plan():
    return PMPlan(agent="codex", model="", effort="high", instruction="do it")


def test_review_prompt_includes_rubric_with_untrusted_framing():
    p = build_review_prompt("goal", _plan(), "timeline", run_count=1, max_runs=3,
                            qa_rubric="MUST_HAVE_TESTS")
    assert "# QA rubric (acceptance standard)" in p
    assert "MUST_HAVE_TESTS" in p
    assert "NOT a new command" in p  # §11 untrusted framing


def test_review_prompt_omits_rubric_section_when_empty():
    p = build_review_prompt("goal", _plan(), "timeline", run_count=1, max_runs=3)
    assert "# QA rubric" not in p  # back-compat: no rubric → no section


def test_review_system_mentions_rubric_acceptance():
    assert "acceptance standard" in REVIEW_SYSTEM
    assert "done=false" in REVIEW_SYSTEM


async def test_review_threads_qa_rubric_to_prompt():
    captured = {}

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None,
                           state_key=""):
            captured["prompt"] = messages[-1].content
            return json.dumps({"done": True, "summary": "ok", "reason": "", "follow_up": ""})

    pm = PMAgent(FakeLLM())
    await pm.review("g", _plan(), "tl", run_count=1, qa_rubric="RUBRIC_X")
    assert "RUBRIC_X" in captured["prompt"]


# ── integration: live _pm_launch feeds rubric + drives follow-up ──────────────────────────────────
def _store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _seed(store, kind, name, *, body, description, check=None):
    meta = {"description": description}
    if check:
        meta["check"] = check
    row = Definition(id=uuid.uuid4().hex, kind=kind, name=name, version=1, status="active",
                     is_active=True, scope_json="{}", body=body, metadata_json=json.dumps(meta))
    store.add_definition(row)
    store.set_definition_active(row.id)


class _FakeHandle:
    session_id = "s"


class _FakeRunner:
    def __init__(self):
        self.sends = []
        self._store = None

    async def launch(self, agent, instruction, workspace, session_id, model="", effort=""):
        h = _FakeHandle()
        h.session_id = session_id
        self._store.add_event(make_event("stop", agent, session_id, payload={"result": "v1"}))
        return h

    async def wait(self, handle):
        return None

    async def send(self, handle, text):
        self.sends.append(text)
        self._store.add_event(make_event("agent_output", "codex", handle.session_id,
                                         payload={"t": "v2"}))


async def test_rubric_reaches_review_input_and_drives_followup(tmp_path):
    store = _store(tmp_path)
    _seed(store, "qa_rubric", "must-test", body="MUST_HAVE_TESTS criteria here",
          description="acceptance: tests required")
    _seed(store, "code_standard", "lint", body="lint rules", description="lint",
          check={"type": "command", "cmd": "ruff check ."})
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]

    review_prompts = []
    reviews = [{"done": False, "summary": "missing tests", "reason": "", "follow_up": "add tests"},
               {"done": True, "summary": "ok", "reason": "", "follow_up": ""}]

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None,
                           state_key=""):
            if "reviewing a coding CLI" in messages[0].content:
                review_prompts.append(messages[-1].content)
                return json.dumps(reviews.pop(0) if reviews else {"done": True, "summary": "ok",
                                                                   "reason": "", "follow_up": ""})
            return json.dumps({"summary": "go", "agent": "codex", "model": "", "effort": "high",
                               "instruction": "do it", "todo": [], "ready": True})

    runner = _FakeRunner()
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=PMAgent(FakeLLM()))
    res = await svc.create("build a feature", workspace=str(tmp_path))
    await asyncio.gather(*list(svc._tasks))

    # rubric body + standard check reached the ACTUAL review LLM input (not just build_review_prompt).
    assert review_prompts and "MUST_HAVE_TESTS" in review_prompts[0]
    assert "ruff check ." in review_prompts[0]
    assert "NOT a new command" in review_prompts[0]  # untrusted framing
    # the not-done review drove a follow-up on the same handle.
    assert runner.sends == ["add tests"]
    # telemetry: first review flagged a rubric-triggered follow-up.
    rows = store.get_events(res["session_id"])
    pm_reviews = [json.loads(e.payload_json) for e in rows if e.type == "pm_review"]
    assert pm_reviews[0]["rubric_active"] is True and pm_reviews[0]["rubric_followup"] is True
    assert pm_reviews[-1]["done"] is True


async def test_no_rubric_is_noop(tmp_path):
    store = _store(tmp_path)  # no definitions at all
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    seen = {}

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None,
                           state_key=""):
            if "reviewing a coding CLI" in messages[0].content:
                seen["review"] = messages[-1].content
                return json.dumps({"done": True, "summary": "ok", "reason": "", "follow_up": ""})
            return json.dumps({"summary": "go", "agent": "codex", "model": "", "effort": "high",
                               "instruction": "do it", "todo": [], "ready": True})

    runner = _FakeRunner()
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=PMAgent(FakeLLM()))
    res = await svc.create("do work", workspace=str(tmp_path))
    await asyncio.gather(*list(svc._tasks))
    assert "# QA rubric" not in seen["review"]  # no rubric section when none selected
    rows = store.get_events(res["session_id"])
    pm_reviews = [json.loads(e.payload_json) for e in rows if e.type == "pm_review"]
    assert pm_reviews[0]["rubric_active"] is False
