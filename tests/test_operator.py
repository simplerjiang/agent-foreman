"""Tests for the Operator (T4.1, DESIGN §6.1/§4.1).

Two concerns: (1) parsing an LLM reply into a validated result — robustly and *conservatively*
(garbage → no proposals + blocked state, never an invented command; unknown reversibility → False so
the Gate forces approval); (2) the live `observe()` path wired through a mock LLMClient
(httpx.MockTransport — no network, no tokens), checking the §15 language directive and prompt
assembly (goal / context / recent actions / agent output, with the tail kept when truncated).
"""

from __future__ import annotations

import json

import httpx

from foreman.client.core.operator import (
    BLOCKED,
    DONE,
    RUNNING,
    WAITING_APPROVAL,
    Operator,
    ProposedAction,
    build_operator_prompt,
    parse_operator,
)
from foreman.shared.config import Config
from foreman.shared.llm import LLMClient


# ── parse_operator: happy paths ─────────────────────────────────────────────────────────────────


def test_parse_full_with_proposals():
    raw = json.dumps({
        "summary": "refactored login into a hook",
        "state": "running",
        "proposals": [
            {
                "kind": "shell",
                "command": "pytest -q",
                "rationale": "verify the refactor",
                "expected_effect": "tests pass",
                "reversible": True,
            },
            {
                "kind": "agent_instruction",
                "command": "add an error-path test",
                "reversible": True,
            },
        ],
    })
    res = parse_operator(raw)
    assert res.summary == "refactored login into a hook"
    assert res.state == RUNNING
    assert len(res.proposals) == 2
    first = res.proposals[0]
    assert first.command == "pytest -q"
    assert first.kind == "shell"
    assert first.rationale == "verify the refactor"
    assert first.expected_effect == "tests pass"
    assert first.reversible is True
    # missing fields default cleanly
    assert res.proposals[1].rationale == "" and res.proposals[1].expected_effect == ""


def test_parse_no_proposals_is_valid():
    res = parse_operator('{"summary": "all done", "state": "done", "proposals": []}')
    assert res.state == DONE
    assert res.proposals == []


def test_parse_state_is_case_insensitive():
    assert parse_operator('{"state": "WAITING_APPROVAL"}').state == WAITING_APPROVAL


# ── parse_operator: conservative fallbacks (从严默认) ───────────────────────────────────────────


def test_parse_non_json_blocks_with_no_proposals():
    res = parse_operator("I think we should just run git push, looks fine")
    assert res.state == BLOCKED
    assert res.proposals == []  # never invent a command out of prose


def test_parse_empty_blocks():
    res = parse_operator("")
    assert res.state == BLOCKED
    assert res.proposals == []


def test_parse_unknown_state_falls_back_to_blocked():
    res = parse_operator('{"state": "vibing", "proposals": []}')
    assert res.state == BLOCKED


def test_reversible_defaults_to_false_when_omitted():
    # Unknown reversibility must be gated, not auto-run (DESIGN §6.6).
    res = parse_operator('{"state": "running", "proposals": [{"command": "git push"}]}')
    assert res.proposals[0].reversible is False


def test_reversible_only_true_on_explicit_yes():
    res = parse_operator(json.dumps({
        "state": "running",
        "proposals": [
            {"command": "a", "reversible": "maybe"},
            {"command": "b", "reversible": "true"},
            {"command": "c", "reversible": True},
            {"command": "d", "reversible": 0},
        ],
    }))
    flags = [p.reversible for p in res.proposals]
    assert flags == [False, True, True, False]


def test_proposal_without_command_is_dropped():
    res = parse_operator(json.dumps({
        "state": "running",
        "proposals": [
            {"kind": "shell", "rationale": "no command here"},
            {"command": "pytest"},
        ],
    }))
    assert len(res.proposals) == 1
    assert res.proposals[0].command == "pytest"


def test_proposals_not_a_list_yields_none():
    res = parse_operator('{"state": "running", "proposals": "git push"}')
    assert res.proposals == []


def test_top_level_array_blocks():
    # A JSON array (not an object) is not a valid result → fail closed.
    res = parse_operator('[{"command": "git push"}]')
    assert res.state == BLOCKED
    assert res.proposals == []


# ── parse_operator: tolerant extraction ─────────────────────────────────────────────────────────


def test_parse_strips_code_fence():
    raw = '```json\n{"summary": "ok", "state": "idle", "proposals": []}\n```'
    res = parse_operator(raw)
    assert res.summary == "ok" and res.state == "idle"


def test_parse_extracts_object_from_prose():
    raw = 'Sure:\n{"summary": "fine", "state": "running", "proposals": []} — done.'
    assert parse_operator(raw).state == RUNNING


def test_kind_defaults_to_shell():
    res = parse_operator('{"state": "running", "proposals": [{"command": "ls"}]}')
    assert res.proposals[0].kind == "shell"


# ── build_operator_prompt ───────────────────────────────────────────────────────────────────────


def test_prompt_includes_goal_and_output():
    p = build_operator_prompt("ship feature X", "agent said hi")
    assert "# Goal" in p and "ship feature X" in p
    assert "# Agent output" in p and "agent said hi" in p


def test_prompt_omits_empty_optional_sections():
    p = build_operator_prompt("g", "o")
    assert "# Context" not in p
    assert "# Recent actions" not in p


def test_prompt_includes_optional_sections():
    p = build_operator_prompt("g", "o", context="cwd=/proj", recent_actions="ran tests")
    assert "# Context" in p and "cwd=/proj" in p
    assert "# Recent actions" in p and "ran tests" in p


def test_prompt_truncates_keeping_tail():
    head = "HEAD" + "x" * 5000
    tail = "TAILMARKER"
    p = build_operator_prompt("g", head + tail, max_output_chars=100)
    assert "[output truncated]" in p
    assert tail in p          # the most recent part is kept
    assert "HEAD" not in p    # the head is dropped


# ── Operator.observe() through a mock LLM (no network) ──────────────────────────────────────────


def _operator(reply_text: str, captured: dict, *, language: str = "zh") -> Operator:
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "test-model"
    cfg.secrets.llm_api_key = "secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})

    llm = LLMClient(cfg, transport=httpx.MockTransport(handler))
    return Operator(llm, language=language)


async def test_observe_parses_llm_reply():
    cap: dict = {}
    reply = json.dumps({
        "summary": "added a test",
        "state": "running",
        "proposals": [{"command": "pytest -q", "reversible": True}],
    })
    op = _operator(reply, cap)
    res = await op.observe("add feature", "the agent's stdout", context="cwd=/proj")
    await op.llm.aclose()

    assert res.summary == "added a test"
    assert res.state == RUNNING
    assert res.proposals[0].command == "pytest -q"
    # json_mode nudged + prompt carried goal/output/context.
    assert cap["json"]["response_format"] == {"type": "json_object"}
    user = cap["json"]["messages"][-1]["content"]
    assert "add feature" in user and "the agent's stdout" in user and "cwd=/proj" in user


async def test_observe_appends_language_directive_zh():
    cap: dict = {}
    op = _operator('{"state": "running", "proposals": []}', cap, language="zh")
    await op.observe("g", "o")
    await op.llm.aclose()
    system = cap["json"]["messages"][0]["content"]
    assert "请始终用简体中文回答" in system


async def test_observe_appends_language_directive_en():
    cap: dict = {}
    op = _operator('{"state": "running", "proposals": []}', cap, language="en")
    await op.observe("g", "o")
    await op.llm.aclose()
    system = cap["json"]["messages"][0]["content"]
    assert "Always respond in English." in system


async def test_observe_blocks_on_garbage_reply():
    cap: dict = {}
    op = _operator("not json at all", cap)
    res = await op.observe("g", "o")
    await op.llm.aclose()
    assert res.state == BLOCKED and res.proposals == []


def test_proposed_action_defaults():
    # Direct dataclass construction: command required, the rest default conservatively.
    a = ProposedAction(command="ls")
    assert a.kind == "shell" and a.reversible is False
    assert a.rationale == "" and a.expected_effect == ""
