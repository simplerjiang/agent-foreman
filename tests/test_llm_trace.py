"""P1b-trace — LLM request/response debug trace (DESIGN §8C).

Unit: the tracer's off=no-op / one-record / ws-no-double-record / key-redaction / contextvar
isolation / rotation / seq-monotonic, plus the env→config glue. Integration: the REAL PM tool-loop
path (DispatchService → PMAgent(tool_runtime_factory) → PMToolLoop → real LLMClient w/ MockTransport)
asserting the recorded phases/ids and that the trace captures the actual LLM input (§14).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from foreman.client.core.dispatch_service import DispatchService
from foreman.client.core.pm_agent import PMAgent
from foreman.client.store import Store
from foreman.client.tools import PMToolRuntime
from foreman.shared.config import AgentCfg, Config, WorkspaceCfg, load_config
from foreman.shared.events import EventBus, make_event
from foreman.shared.llm import LLMClient, Message
from foreman.shared.llm.trace import LLMTracer, trace_context


def _tracer(tmp_path, **kw):
    opts = dict(log_dir=tmp_path / "dbg", max_bytes=10_000_000, keep=20, keep_days=14)
    opts.update(kw)
    return LLMTracer(**opts)


def _read_lines(tmp_path, session_id=""):
    safe = session_id or "_no-session"
    p = tmp_path / "dbg" / f"llm-trace-{safe}.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _http_client(*, reply="hi", tracer=None, transport_mode="http", api_key="sk-REALKEY12345"):
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://api.openai.test"
    cfg.llm.transport = transport_mode
    cfg.secrets.llm_api_key = api_key

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return LLMClient(cfg, transport=httpx.MockTransport(handler), tracer=tracer)


# ── U-1: off = no-op ──────────────────────────────────────────────────────────────────────────────
async def test_tracer_off_writes_nothing(tmp_path):
    client = _http_client(reply="hello", tracer=None)
    out = await client.complete([Message("user", "hi")])
    assert out == "hello"
    assert not (tmp_path / "dbg").exists()


# ── U-2: one record per call, fields + ids from contextvar ────────────────────────────────────────
async def test_one_record_with_ids_and_fields(tmp_path):
    tr = _tracer(tmp_path)
    client = _http_client(reply="planned", tracer=tr)
    with trace_context(session_id="s1", task_id="t1", phase="plan"):
        await client.complete([Message("system", "sys"), Message("user", "do it")])
    lines = _read_lines(tmp_path, "s1")
    assert len(lines) == 1
    rec = lines[0]
    assert rec["seq"] == 1 and rec["session_id"] == "s1" and rec["task_id"] == "t1"
    assert rec["phase"] == "plan" and rec["kind"] == "complete"
    assert rec["response"]["text"] == "planned"
    assert [m["role"] for m in rec["request"]["messages"]] == ["system", "user"]
    assert rec["metrics"]["resp_chars"] == len("planned")


# ── U-3: ws tool_complete records ONCE (no reentry double) ────────────────────────────────────────
async def test_ws_tool_complete_records_once(tmp_path, monkeypatch):
    tr = _tracer(tmp_path)
    client = _http_client(tracer=tr, transport_mode="ws")

    async def fake_impl(messages, **kw):
        return "ws-text"

    monkeypatch.setattr(client, "_complete_impl", fake_impl)
    with trace_context(session_id="ws1", phase="tool-round-1"):
        resp = await client.tool_complete([Message("user", "x")], tools=[{"name": "t"}])
    assert resp.text == "ws-text"
    lines = _read_lines(tmp_path, "ws1")
    assert len(lines) == 1 and lines[0]["kind"] == "tool_complete"


# ── U-4: api key never lands in the trace ─────────────────────────────────────────────────────────
async def test_key_not_in_trace_even_if_in_message(tmp_path):
    tr = _tracer(tmp_path)
    # settings_resolver injects a key (lives only in headers); also stuff a key-shaped token into a
    # message to prove the belt-and-suspenders redactor scrubs it.
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://api.openai.test"

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = LLMClient(
        cfg, transport=httpx.MockTransport(handler), tracer=tr,
        settings_resolver=lambda: {"api_key": "sk-SECRETKEY99999"},
    )
    with trace_context(session_id="k1"):
        await client.complete([Message("user", "my token is sk-LEAKED12345678 ok")])
    raw = (tmp_path / "dbg" / "llm-trace-k1.jsonl").read_text(encoding="utf-8")
    assert "sk-SECRETKEY99999" not in raw
    assert "sk-LEAKED12345678" not in raw
    assert "[REDACTED]" in raw


# ── U-7: contextvar isolation across concurrent tasks ─────────────────────────────────────────────
async def test_contextvar_isolation(tmp_path):
    tr = _tracer(tmp_path)
    client = _http_client(reply="r", tracer=tr)

    async def run(sid):
        with trace_context(session_id=sid, phase="plan"):
            await client.complete([Message("user", sid)])

    await asyncio.gather(run("A"), run("B"))
    a = _read_lines(tmp_path, "A")
    b = _read_lines(tmp_path, "B")
    assert len(a) == 1 and a[0]["session_id"] == "A"
    assert len(b) == 1 and b[0]["session_id"] == "B"


# ── U-8: rotation + count retention ───────────────────────────────────────────────────────────────
async def test_rotation_and_retention(tmp_path):
    tr = _tracer(tmp_path, max_bytes=400, keep=3)
    client = _http_client(reply="x" * 200, tracer=tr)
    for _ in range(8):
        with trace_context(session_id="rot"):
            await client.complete([Message("user", "y" * 200)])
    files = sorted((tmp_path / "dbg").glob("llm-trace-rot.jsonl*"))
    # current + at most keep-1 rotated
    assert (tmp_path / "dbg" / "llm-trace-rot.jsonl").exists()
    assert len(files) <= 3


# ── U-9: seq monotonic across calls ───────────────────────────────────────────────────────────────
async def test_seq_monotonic(tmp_path):
    tr = _tracer(tmp_path)
    client = _http_client(reply="r", tracer=tr)
    for _ in range(3):
        with trace_context(session_id="seq"):
            await client.complete([Message("user", "z")])
    seqs = [r["seq"] for r in _read_lines(tmp_path, "seq")]
    assert seqs == [1, 2, 3]


# ── U-10: env→config glue ─────────────────────────────────────────────────────────────────────────
def test_config_env_glue(monkeypatch, tmp_path):
    monkeypatch.setenv("FOREMAN_DEBUG_LLM_TRACE", "1")
    assert load_config(tmp_path / "nope.yaml").debug.llm_trace is True
    monkeypatch.setenv("FOREMAN_DEBUG_LLM_TRACE", "0")
    assert load_config(tmp_path / "nope.yaml").debug.llm_trace is False
    monkeypatch.delenv("FOREMAN_DEBUG_LLM_TRACE", raising=False)
    assert load_config(tmp_path / "nope.yaml").debug.llm_trace is False  # falls back to yaml default


# ── integration: real PM tool-loop path records correct phases + ids ──────────────────────────────
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


async def test_tool_loop_trace_phases_and_ids(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    cfg.agents = {"codex": AgentCfg(command="codex", enabled=True)}
    cfg.workspaces = [WorkspaceCfg(path=str(tmp_path))]
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://api.openai.test"
    cfg.secrets.llm_api_key = "sk-test123456"
    tr = _tracer(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        sys = body.get("messages", [{}])[0].get("content", "")
        if "reviewing a coding CLI" in sys:
            content = json.dumps({"done": True, "summary": "ok", "reason": "", "follow_up": ""})
        else:  # plan tool-loop: return a final_plan immediately (no tools needed)
            content = json.dumps({"type": "final_plan", "summary": "go", "agent": "codex",
                                  "model": "", "effort": "high", "instruction": "do it",
                                  "todo": [], "ready": True})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    llm = LLMClient(cfg, transport=httpx.MockTransport(handler), tracer=tr)
    pm = PMAgent(llm, tool_runtime_factory=lambda ws: PMToolRuntime.from_config(cfg, ws))
    runner = _FakeRunner()
    runner._store = store
    svc = DispatchService(cfg, store, bus=EventBus(), runner=runner, pm_agent=pm)

    res = await svc.create("a task")
    await asyncio.gather(*list(svc._tasks))
    sid = res["session_id"]
    lines = _read_lines(tmp_path, sid)
    assert lines, "expected trace lines for the dispatch"
    phases = {r["phase"] for r in lines}
    # plan ran through the tool-loop (tool-round-1) and at least one review.
    assert "tool-round-1" in phases
    assert any(p.startswith("review-") for p in phases)
    # every record correlates to the dispatch session+task and records the real LLM messages.
    assert all(r["session_id"] == sid for r in lines)
    assert all(r["task_id"] == res["task_id"] for r in lines if r["phase"] == "tool-round-1")
    plan_rec = next(r for r in lines if r["phase"] == "tool-round-1")
    assert any(m["role"] == "system" for m in plan_rec["request"]["messages"])

    # I-2: trace ids join with the work_mode telemetry event for the same dispatch.
    rows = store.get_events(sid)
    wm = [json.loads(e.payload_json) for e in rows if e.type == "work_mode"]
    assert wm and all(e.session_id == sid for e in rows if e.type == "work_mode")
