from __future__ import annotations

import json
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from foreman.client.core.gate import Gate
from foreman.client.tools import EXTERNAL_WEB, PMToolLoop, PMToolRuntime, ToolCall
from foreman.client.tools.loop import (
    SUBMIT_PLAN_TOOL,
    _calls_from_json,
    submit_plan_tool_spec,
    validate_final_plan,
)
from foreman.client.tools.models import ToolRuntimeConfig
from foreman.shared.config import Config, GatesCfg
from foreman.shared.llm import LLMToolCall, LLMToolResponse, Message


class _TextHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"hello from local pm tools server"
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _serve_text() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _TextHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/x"


def _runtime(tmp_path: Path, *, cards=None, **kwargs) -> PMToolRuntime:
    cfg = ToolRuntimeConfig(workspace=tmp_path, allowed_roots=[tmp_path], **kwargs)
    return PMToolRuntime(cfg, gate=Gate(Config().gates), cards=cards)


async def test_pm_tool_loop_forwards_llm_stream_chunks(tmp_path: Path):
    chunks: list[dict] = []

    async def on_stream(chunk: dict) -> None:
        chunks.append(chunk)

    class FakeLLM:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            assert on_stream is not None
            await on_stream({"kind": "output", "delta": "planning", "event_type": "chunk"})
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "streamed",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "do the work",
                }
            )

    outcome = await PMToolLoop(
        FakeLLM(),
        _runtime(tmp_path),
        on_stream=on_stream,
    ).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.final_plan["summary"] == "streamed"
    assert chunks == [{"kind": "output", "delta": "planning", "event_type": "chunk"}]


async def test_read_search_write_replace_and_path_guard(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    rt = _runtime(tmp_path, file_write=True)
    listed = await rt.call(ToolCall("list", "list_files", {"path": "."}))
    assert listed.ok and "src/a.txt" in listed.data["files"]
    read = await rt.call(ToolCall("read", "read_file", {"path": "src/a.txt", "end_line": 1}))
    assert read.data["text"] == "alpha"
    matches = await rt.call(ToolCall("search", "search_repo", {"query": "alpha"}))
    assert [m["line"] for m in matches.data["matches"]] == [1, 3]
    escaped = await rt.call(ToolCall("escape", "read_file", {"path": str(outside)}))
    assert escaped.ok is False and escaped.error == "path_outside_workspace"

    written = await rt.call(ToolCall("write", "write_file", {"path": "new.txt", "text": "one"}))
    assert written.ok and (tmp_path / "new.txt").read_text(encoding="utf-8") == "one"
    duplicate = await rt.call(
        ToolCall("replace", "replace_in_file", {"path": "src/a.txt", "old": "alpha", "new": "x"})
    )
    assert duplicate.ok is False and duplicate.data["match_count"] == 2
    unique = await rt.call(
        ToolCall("replace2", "replace_in_file", {"path": "src/a.txt", "old": "beta", "new": "B"})
    )
    assert unique.ok and "B" in (tmp_path / "src" / "a.txt").read_text(encoding="utf-8")


async def test_disabled_write_run_command_allowlist_and_web_taint(tmp_path: Path):
    disabled = await _runtime(tmp_path).call(
        ToolCall("w", "write_file", {"path": "x.txt", "text": "x"})
    )
    assert disabled.error == "tool_disabled"

    rt = _runtime(tmp_path, shell=True, allowed_commands=["python --version", "git push"])
    cmd = await rt.call(ToolCall("cmd", "run_command", {"command": "python --version"}))
    assert cmd.ok and cmd.data["returncode"] == 0
    assert "Python" in (cmd.data["stdout"] + cmd.data["stderr"])
    denied = await rt.call(ToolCall("deny", "run_command", {"command": "git push"}))
    assert denied.error == "requires_approval"
    blocked = await rt.call(ToolCall("no", "run_command", {"command": "python -V"}))
    assert blocked.error == "command_not_allowlisted"
    tainted = await rt.call(
        ToolCall("taint", "run_command", {"command": "python --version"}),
        context_taint=[EXTERNAL_WEB],
    )
    assert tainted.error == "shell_after_web_requires_approval"


async def test_run_command_uses_auditor_only_for_gate_gray_area(tmp_path: Path):
    class FakeAuditor:
        def __init__(self) -> None:
            self.calls = 0

        async def audit(self, command, **kwargs):
            self.calls += 1
            return SimpleNamespace(
                verdict="revise",
                goal_quality="weak",
                risk_severity="mild",
                reasons=["too broad"],
                suggestions=["narrow it"],
            )

    auditor = FakeAuditor()
    gate = Gate(GatesCfg(requires_approval=[], needs_strategy=["python --version"]))
    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=tmp_path,
            allowed_roots=[tmp_path],
            shell=True,
            allowed_commands=["python --version"],
        ),
        gate=gate,
        auditor=auditor,
    )

    result = await rt.call(ToolCall("gray", "run_command", {"command": "python --version"}))

    assert auditor.calls == 1
    assert result.ok is False
    assert result.error == "auditor_revise"
    assert result.data["reasons"] == ["too broad"]


async def test_fetch_url_marks_external_web_content(tmp_path: Path):
    server, url = _serve_text()
    try:
        rt = _runtime(tmp_path, web_fetch=True)
        result = await rt.call(ToolCall("fetch", "fetch_url", {"url": url}))
        assert result.ok and "hello from local" in result.data["text"]
        assert result.taint == [EXTERNAL_WEB]
    finally:
        server.shutdown()


async def test_pm_loop_propagates_external_web_taint_to_next_tool(tmp_path: Path):
    server, url = _serve_text()

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {"id": "fetch", "name": "fetch_url", "arguments": {"url": url}}
                        ],
                    }
                )
            if self.calls == 2:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "cmd",
                                "name": "run_command",
                                "arguments": {"command": "python --version"},
                            }
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "taint verified",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "report taint behavior",
                }
            )

    events: list[tuple[str, dict]] = []
    rt = _runtime(
        tmp_path,
        shell=True,
        web_fetch=True,
        allowed_commands=["python --version"],
    )
    try:
        outcome = await PMToolLoop(
            FakeLLM(),
            rt,
            max_rounds=3,
            on_tool_event=lambda t, p: events.append((t, p)),
        ).run(
            [Message("user", "fetch then command")],
            fallback_plan={
                "agent": "codex",
                "model": "",
                "effort": "high",
                "instruction": "fallback",
            },
            enabled_agents=["codex"],
        )
    finally:
        server.shutdown()

    post_outputs = [json.loads(p["output"]) for t, p in events if t == "tool_post"]
    assert outcome.final_plan["summary"] == "taint verified"
    assert post_outputs[0]["taint"] == [EXTERNAL_WEB]
    assert post_outputs[1]["error"] == "shell_after_web_requires_approval"


async def test_pm_loop_rejects_final_plan_after_unverified_web_search(tmp_path: Path):
    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "search",
                                "name": "web_search",
                                "arguments": {"query": "pm tools", "max_results": 1},
                            }
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "search says it is true",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "act on unverified search",
                }
            )

    rt = _runtime(tmp_path, web_search=True)
    outcome = await PMToolLoop(FakeLLM(), rt, max_rounds=2).run(
        [Message("user", "search then finish")],
        fallback_plan={
            "agent": "codex",
            "model": "",
            "effort": "high",
            "instruction": "fallback",
        },
        enabled_agents=["codex"],
    )

    assert outcome.incomplete is True
    assert outcome.rounds[-1]["error"] == "web_search_leads_unverified"


async def test_pm_loop_accepts_final_plan_after_web_search_fetch_verification(tmp_path: Path):
    server, url = _serve_text()

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {
                                "id": "search",
                                "name": "web_search",
                                "arguments": {"query": "pm tools", "max_results": 1},
                            }
                        ],
                    }
                )
            if self.calls == 2:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {"id": "fetch", "name": "fetch_url", "arguments": {"url": url}}
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "source fetched",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "report fetched source",
                }
            )

    rt = _runtime(tmp_path, web_search=True, web_fetch=True)
    try:
        outcome = await PMToolLoop(FakeLLM(), rt, max_rounds=3).run(
            [Message("user", "search fetch finish")],
            fallback_plan={
                "agent": "codex",
                "model": "",
                "effort": "high",
                "instruction": "fallback",
            },
            enabled_agents=["codex"],
        )
    finally:
        server.shutdown()

    assert outcome.incomplete is False
    assert outcome.final_plan["summary"] == "source fetched"


async def test_invalid_tool_args_max_rounds_and_final_validator(tmp_path: Path):
    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [
                            {"id": "bad", "name": "read_file", "arguments": "not-object"},
                            {"id": "unknown", "name": "no_such_tool", "arguments": {}},
                        ],
                    }
                )
            return json.dumps(
                {
                    "type": "final_plan",
                    "summary": "done",
                    "agent": "codex",
                    "model": "",
                    "effort": "high",
                    "instruction": "run after evidence",
                    "todo": ["verify"],
                    "ready": True,
                }
            )

    events: list[tuple[str, dict]] = []
    rt = _runtime(tmp_path)
    loop = PMToolLoop(FakeLLM(), rt, max_rounds=3, on_tool_event=lambda t, p: events.append((t, p)))
    outcome = await loop.run(
        [Message("system", "sys"), Message("user", "tool_schema runtime_context policy_context")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )
    assert outcome.final_plan["instruction"] == "run after evidence"
    post_outputs = [json.loads(p["output"]) for t, p in events if t == "tool_post"]
    assert {item["error"] for item in post_outputs} == {"invalid_args", "unknown_tool"}

    class NeverFinal:
        async def complete(self, messages, *, json_mode=False, model="", on_stream=None):
            return json.dumps({"type": "tool_calls", "tool_calls": []})

    outcome = await PMToolLoop(NeverFinal(), rt, max_rounds=1).run(
        [Message("user", "x")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )
    assert outcome.incomplete is True
    assert outcome.final_plan["tool_loop_incomplete"] is True

    bad = {
        "type": "final_plan",
        "agent": "bad",
        "instruction": "x",
        "effort": "high",
    }
    try:
        validate_final_plan(bad, enabled_agents=["codex"], fallback_plan={"agent": "codex"})
    except ValueError as exc:
        assert "bad_agent" in str(exc)
    else:
        raise AssertionError("validator should reject unknown agents")


def test_validate_final_plan_clamps_schema_bounds():
    # Medium-2 hardening: the ws backend may not enforce the submit_plan input_schema, so the
    # validator clamps the §5 structural bounds (maxLength/maxItems) itself rather than trust
    # whatever the upstream sends through as tool arguments.
    obj = {
        "type": "final_plan",
        "agent": "codex",
        "effort": "high",
        "instruction": "i" * 7000,
        "summary": "s" * 800,
        "model": "m" * 120,
        "todo": ["t" * 400 for _ in range(20)],
        "deliberation": ["d" * 400 for _ in range(20)],
        "ready": True,
    }
    plan = validate_final_plan(
        obj,
        enabled_agents=["codex"],
        fallback_plan={"agent": "codex"},
        max_plan_items=15,
    )
    assert len(plan["summary"]) == 600
    assert len(plan["model"]) == 80
    assert len(plan["instruction"]) == 6000
    assert len(plan["todo"]) == 15 and all(len(x) <= 200 for x in plan["todo"])
    assert len(plan["deliberation"]) == 15 and all(len(x) <= 300 for x in plan["deliberation"])


def test_json_fallback_accepts_flat_tool_arguments():
    calls = _calls_from_json(
        {
            "type": "tool_calls",
            "tool_calls": [
                {
                    "id": "click-1",
                    "name": "browser_click",
                    "ref": "ref-1",
                },
                {
                    "id": "type-1",
                    "tool": "browser_type",
                    "ref": "ref-2",
                    "text": "hello",
                },
                {
                    "id": "click-2",
                    "name": "browser_click",
                    "input": {"ref": "ref-3"},
                },
            ],
        }
    )

    assert [(call.name, call.arguments) for call in calls] == [
        ("browser_click", {"ref": "ref-1"}),
        ("browser_type", {"ref": "ref-2", "text": "hello"}),
        ("browser_click", {"ref": "ref-3"}),
    ]


def test_submit_plan_tool_spec_constrains_agent_enum():
    spec = submit_plan_tool_spec(["codex"], max_plan_items=17)
    assert spec["name"] == SUBMIT_PLAN_TOOL
    schema = spec["input_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["agent"]["enum"] == ["codex"]
    assert schema["properties"]["todo"]["maxItems"] == 17
    assert schema["properties"]["deliberation"]["maxItems"] == 17
    # Empty/None enabled set falls back to all supported planning agents.
    assert submit_plan_tool_spec([])["input_schema"]["properties"]["agent"]["enum"] == [
        "claude-code",
        "codex",
        "copilot-cli",
    ]
    clamped = submit_plan_tool_spec(["codex"], max_plan_items=9999999)["input_schema"]
    assert clamped["properties"]["todo"]["maxItems"] == 999
    assert clamped["properties"]["deliberation"]["maxItems"] == 999


class _ScriptedToolLLM:
    """A ws-style LLM whose ``tool_complete`` returns pre-scripted tool calls per round and records
    the ``tool_choice`` each round received (so the test can assert auto vs forced submit)."""

    def __init__(self, scripted: list[LLMToolResponse]) -> None:
        self.scripted = scripted
        self.tool_choices: list[object] = []
        self.tools_seen: list[list[str]] = []
        self.raw_tools_seen: list[list[dict]] = []
        self._round = 0

    async def tool_complete(
        self, messages, *, tools, model="", json_mode=False, tool_choice="auto", on_stream=None
    ) -> LLMToolResponse:
        self.tool_choices.append(tool_choice)
        self.tools_seen.append([t["name"] for t in tools])
        self.raw_tools_seen.append(tools)
        resp = self.scripted[min(self._round, len(self.scripted) - 1)]
        self._round += 1
        return resp


def _submit_call(**overrides) -> LLMToolCall:
    args = {
        "summary": "ship it",
        "agent": "codex",
        "model": "",
        "effort": "high",
        "instruction": "do the work",
        "todo": ["inspect"],
        "deliberation": ["evidence read"],
        "ready": True,
    }
    args.update(overrides)
    return LLMToolCall(id="submit-1", name=SUBMIT_PLAN_TOOL, arguments=args)


async def test_pm_loop_native_path_ignores_text_final_plan(tmp_path: Path):
    # §0.5-1 / §11.1-B: on the native (tool_complete) transport the plan must terminate via a
    # submit_plan tool CALL. A model that emits a final_plan as free TEXT (the repetition-prone
    # shape that hung #39) must NOT terminate the loop — it falls through to the conservative
    # fallback instead of letting repeatable text drive the control flow.
    text_plan = json.dumps(
        {
            "type": "final_plan", "agent": "codex", "effort": "high",
            "instruction": "smuggled via text", "summary": "should be ignored",
        }
    )
    llm = _ScriptedToolLLM([LLMToolResponse(text=text_plan, tool_calls=[])])
    outcome = await PMToolLoop(llm, _runtime(tmp_path), max_rounds=1).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )
    assert outcome.incomplete is True
    assert outcome.final_plan["instruction"] == "fallback"
    assert outcome.final_plan["summary"] != "should be ignored"


async def test_pm_loop_submit_plan_tool_terminates_on_auto_round(tmp_path: Path):
    # T1.4: an evidence (auto) round can read a file, then the model calls submit_plan natively to
    # terminate — the plan arrives as validated tool arguments, no regex over free text.
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    scripted = [
        LLMToolResponse(
            text="",
            tool_calls=[LLMToolCall(id="c1", name="read_file", arguments={"path": "README.md"})],
        ),
        LLMToolResponse(text="", tool_calls=[_submit_call()]),
    ]
    llm = _ScriptedToolLLM(scripted)
    events: list[tuple[str, dict]] = []
    outcome = await PMToolLoop(
        llm, _runtime(tmp_path), max_rounds=6, on_tool_event=lambda t, p: events.append((t, p))
    ).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.incomplete is False
    assert outcome.final_plan["summary"] == "ship it"
    assert outcome.final_plan["instruction"] == "do the work"
    assert outcome.final_plan["todo"] == ["inspect"]
    # The evidence round really ran read_file, and submit_plan was offered as a tool on auto.
    assert "read_file" in [p["tool"] for t, p in events if t == "tool_pre"]
    assert SUBMIT_PLAN_TOOL in llm.tools_seen[0]
    assert llm.tool_choices[0] == "auto"
    submit_spec = next(t for t in llm.raw_tools_seen[0] if t["name"] == SUBMIT_PLAN_TOOL)
    assert submit_spec["input_schema"]["properties"]["todo"]["maxItems"] == 6
    assert submit_spec["input_schema"]["properties"]["deliberation"]["maxItems"] == 6


async def test_pm_loop_can_ask_question_before_submit_plan(tmp_path: Path):
    class _FakeCards:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def ask_question(self, **kwargs):
            self.calls.append(kwargs)
            return {"ok": True, "card_id": "q1", "choice": "B", "label": "Observe demo"}

    scripted = [
        LLMToolResponse(
            text="",
            tool_calls=[
                LLMToolCall(
                    id="q",
                    name="ask_question",
                    arguments={
                        "question": "Pick a path",
                        "options": [
                            {"label": "Read docs", "value": "A"},
                            {"label": "Observe demo", "value": "B"},
                        ],
                    },
                )
            ],
        ),
        LLMToolResponse(text="", tool_calls=[_submit_call(summary="user picked B")]),
    ]
    cards = _FakeCards()
    runtime = _runtime(tmp_path, cards=cards)
    runtime.set_decision_context("s1", "t1")
    events: list[tuple[str, dict]] = []

    outcome = await PMToolLoop(
        _ScriptedToolLLM(scripted),
        runtime,
        max_rounds=6,
        on_tool_event=lambda t, p: events.append((t, p)),
    ).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.incomplete is False
    assert outcome.final_plan["summary"] == "user picked B"
    assert cards.calls[0]["session_id"] == "s1"
    assert cards.calls[0]["question"] == "Pick a path"
    post = [p for t, p in events if t == "tool_post" and p["tool"] == "ask_question"][0]
    result = json.loads(post["output"])["data"]
    assert result["choice"] == "B"


async def test_pm_loop_forces_submit_plan_on_final_round_no_fallback(tmp_path: Path):
    # T1.4 root fix for #39: a model that would otherwise loop forever (always asking for more
    # evidence — the repetition/stall shape) is FORCED to submit_plan on the final round, so the
    # loop ends with a REAL plan instead of degrading to the conservative fallback.
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    evidence = LLMToolResponse(
        text="", tool_calls=[LLMToolCall(id="e", name="read_file", arguments={"path": "a.txt"})]
    )
    submit = LLMToolResponse(text="", tool_calls=[_submit_call(instruction="forced plan")])

    class _ForcedLLM:
        def __init__(self) -> None:
            self.tool_choices: list[object] = []

        async def tool_complete(
            self, messages, *, tools, model="", json_mode=False, tool_choice="auto", on_stream=None
        ) -> LLMToolResponse:
            self.tool_choices.append(tool_choice)
            # Only submit when the loop forces the submit_plan tool_choice (final round).
            if tool_choice == {"type": "function", "name": SUBMIT_PLAN_TOOL}:
                return submit
            return evidence

    llm = _ForcedLLM()
    outcome = await PMToolLoop(llm, _runtime(tmp_path), max_rounds=3).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    assert outcome.incomplete is False  # did NOT degrade to fallback
    assert outcome.final_plan["instruction"] == "forced plan"
    assert llm.tool_choices[:2] == ["auto", "auto"]  # evidence rounds were auto
    assert llm.tool_choices[2] == {"type": "function", "name": SUBMIT_PLAN_TOOL}  # final forced


async def test_pm_loop_rejects_submit_plan_until_web_search_verified(tmp_path: Path):
    # The web_search → verify guard must apply to the native submit_plan path too, not just the
    # legacy final_plan text path: a submit_plan straight after web_search is rejected; once a
    # local read verifies the leads, the next submit_plan is accepted.
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    scripted = [
        LLMToolResponse(
            text="",
            tool_calls=[LLMToolCall(id="s", name="web_search", arguments={"query": "foreman"})],
        ),
        LLMToolResponse(text="", tool_calls=[_submit_call(summary="too early")]),
        LLMToolResponse(
            text="",
            tool_calls=[LLMToolCall(id="r", name="read_file", arguments={"path": "README.md"})],
        ),
        LLMToolResponse(text="", tool_calls=[_submit_call(summary="verified")]),
    ]
    llm = _ScriptedToolLLM(scripted)
    rt = _runtime(tmp_path, web_search=True)
    outcome = await PMToolLoop(llm, rt, max_rounds=6).run(
        [Message("user", "plan")],
        fallback_plan={"agent": "codex", "model": "", "effort": "high", "instruction": "fallback"},
        enabled_agents=["codex"],
    )

    # The first submit (round 2) is rejected as unverified; the verified submit (round 4) lands.
    assert [r for r in outcome.rounds if r.get("error") == "web_search_leads_unverified"]
    assert outcome.incomplete is False
    assert outcome.final_plan["summary"] == "verified"
