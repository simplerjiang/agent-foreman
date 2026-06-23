"""Tests for the post-checkpoint Reviewer (T2.7, DESIGN §4.1/§5.3).

Two concerns: (1) parsing an LLM reply into a validated verdict — robustly and *conservatively*
(unknown/garbage → escalate, never a silent approve); (2) the live `review()` path wired through a
mock LLMClient (httpx.MockTransport — no network, no tokens), checking the §15 language directive and
prompt assembly. Plus the CheckpointManager.diff() source that feeds the Reviewer.
"""

from __future__ import annotations

import json
import subprocess

import httpx

from foreman.client.core.checkpoint import CheckpointManager
from foreman.client.core.reviewer import (
    APPROVE,
    ESCALATE,
    REQUEST_CHANGES,
    Reviewer,
    build_review_prompt,
    compact_diff_for_review,
    parse_review,
)
from foreman.shared.config import Config
from foreman.shared.llm import LLMClient


# ── parse_review: happy paths ───────────────────────────────────────────────────────────────────


def test_parse_approve_full():
    raw = json.dumps({
        "verdict": "approve", "summary": "looks good",
        "risks": ["r1"], "suggestions": ["s1", "s2"], "needs_human": False,
    })
    res = parse_review(raw)
    assert res.verdict == APPROVE
    assert res.summary == "looks good"
    assert res.risks == ["r1"]
    assert res.suggestions == ["s1", "s2"]
    assert res.needs_human is False


def test_parse_request_changes():
    res = parse_review('{"verdict": "request_changes", "summary": "redo it"}')
    assert res.verdict == REQUEST_CHANGES
    assert res.needs_human is False  # not escalate → no human unless flagged
    assert res.risks == [] and res.suggestions == []


def test_parse_verdict_is_case_insensitive():
    assert parse_review('{"verdict": "APPROVE"}').verdict == APPROVE


# ── parse_review: conservative fallbacks (从严默认) ─────────────────────────────────────────────


def test_parse_unknown_verdict_escalates():
    res = parse_review('{"verdict": "looks_fine", "summary": "eh"}')
    assert res.verdict == ESCALATE
    assert res.needs_human is True


def test_parse_non_json_escalates():
    res = parse_review("the change looks fine to me, ship it")
    assert res.verdict == ESCALATE
    assert res.needs_human is True


def test_parse_empty_escalates():
    res = parse_review("")
    assert res.verdict == ESCALATE
    assert res.needs_human is True


def test_escalate_verdict_forces_needs_human():
    # Model said escalate but needs_human=False — we still force a human in the loop.
    res = parse_review('{"verdict": "escalate", "needs_human": false}')
    assert res.verdict == ESCALATE
    assert res.needs_human is True


# ── parse_review: tolerant extraction + coercion ────────────────────────────────────────────────


def test_parse_strips_code_fence():
    raw = '```json\n{"verdict": "approve", "summary": "ok"}\n```'
    res = parse_review(raw)
    assert res.verdict == APPROVE and res.summary == "ok"


def test_parse_extracts_object_from_prose():
    raw = 'Sure! Here is my review:\n{"verdict": "approve", "summary": "fine"} — done.'
    assert parse_review(raw).verdict == APPROVE


def test_parse_coerces_scalar_risks_to_list():
    res = parse_review('{"verdict": "approve", "risks": "single risk", "suggestions": null}')
    assert res.risks == ["single risk"]
    assert res.suggestions == []


def test_parse_drops_blank_list_items():
    res = parse_review('{"verdict": "approve", "risks": ["", "  ", "real"]}')
    assert res.risks == ["real"]


# ── build_review_prompt ─────────────────────────────────────────────────────────────────────────


def test_prompt_includes_goal_qa_and_diff():
    p = build_review_prompt("ship feature X", "diff body", qa_standard="must have tests")
    assert "# Goal" in p and "ship feature X" in p
    assert "# QA standard" in p and "must have tests" in p
    assert "```diff" in p and "diff body" in p


def test_prompt_omits_empty_optional_sections():
    p = build_review_prompt("g", "d")
    assert "# QA standard" not in p
    assert "# Context" not in p


def test_prompt_truncates_long_diff():
    big = "x" * 5000
    p = build_review_prompt("g", big, max_diff_chars=100)
    assert "[diff truncated]" in p
    assert big not in p


# ── Reviewer.review() through a mock LLM (no network) ───────────────────────────────────────────


def test_compact_diff_keeps_scope_tail_and_omitted_list():
    parts = []
    for i in range(8):
        body = "\n".join(f"+line {i}-{n}" for n in range(80))
        if i == 7:
            body += "\n+TAIL_FILE_MARKER"
        parts.append(
            f"diff --git a/file{i}.py b/file{i}.py\n"
            f"--- a/file{i}.py\n"
            f"+++ b/file{i}.py\n"
            f"{body}\n"
        )
    compacted = compact_diff_for_review("".join(parts), max_chars=2500)

    assert "# Diff overview" in compacted
    assert "files_changed: 8" in compacted
    assert "- file7.py" in compacted
    assert "TAIL_FILE_MARKER" in compacted
    assert "# Omitted diff evidence" in compacted
    assert "file_not_in_selected_hunks" in compacted


def _reviewer(reply_text: str, captured: dict, *, language: str = "zh") -> Reviewer:
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "test-model"
    cfg.secrets.llm_api_key = "secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})

    llm = LLMClient(cfg, transport=httpx.MockTransport(handler))
    return Reviewer(llm, language=language)


async def test_review_parses_llm_reply():
    cap: dict = {}
    rv = _reviewer('{"verdict": "request_changes", "summary": "add a test"}', cap)
    res = await rv.review("add feature", "some diff", qa_standard="needs a unit test")
    await rv.llm.aclose()

    assert res.verdict == REQUEST_CHANGES
    assert res.summary == "add a test"
    # json_mode nudged + prompt carried goal/qa/diff.
    assert cap["json"]["response_format"] == {"type": "json_object"}
    user = cap["json"]["messages"][-1]["content"]
    assert "add feature" in user and "needs a unit test" in user and "some diff" in user


async def test_review_appends_language_directive_zh():
    cap: dict = {}
    rv = _reviewer('{"verdict": "approve"}', cap, language="zh")
    await rv.review("g", "d")
    await rv.llm.aclose()
    system = cap["json"]["messages"][0]["content"]
    assert "请始终用简体中文回答" in system


async def test_review_appends_language_directive_en():
    cap: dict = {}
    rv = _reviewer('{"verdict": "approve"}', cap, language="en")
    await rv.review("g", "d")
    await rv.llm.aclose()
    system = cap["json"]["messages"][0]["content"]
    assert "Always respond in English." in system


async def test_review_escalates_on_garbage_reply():
    cap: dict = {}
    rv = _reviewer("not json at all", cap)
    res = await rv.review("g", "d")
    await rv.llm.aclose()
    assert res.verdict == ESCALATE and res.needs_human is True


# ── CheckpointManager.diff(): the Reviewer's diff source ────────────────────────────────────────


def _write(ws, name, content):
    (ws / name).write_text(content, encoding="utf-8")


async def test_diff_checkpoint_to_worktree_includes_new_file(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, "a.txt", "one\n")
    base = await mgr.snapshot("s1", 0)

    # Agent modifies a.txt and adds an untracked b.txt after the checkpoint.
    _write(ws, "a.txt", "one\ntwo\n")
    _write(ws, "b.txt", "new file\n")

    out = mgr.diff(base)  # base → current worktree
    assert "a.txt" in out and "+two" in out
    assert "b.txt" in out and "new file" in out  # untracked new file shows up


async def test_diff_between_two_checkpoints(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, "a.txt", "one\n")
    c0 = await mgr.snapshot("s1", 0)
    _write(ws, "a.txt", "one\ntwo\n")
    c1 = await mgr.snapshot("s1", 1)

    out = mgr.diff(c0, c1)
    assert "+two" in out
    # Reverse direction shows the removal.
    assert "-two" in mgr.diff(c1, c0)


async def test_diff_empty_when_unchanged(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, "a.txt", "stable\n")
    base = await mgr.snapshot("s1", 0)
    assert mgr.diff(base) == ""  # nothing changed since the checkpoint


def test_diff_rejects_bad_ref(tmp_path):
    ws = tmp_path / "proj"
    mgr = CheckpointManager(ws)
    mgr.ensure_repo()
    _write(ws, "a.txt", "x\n")
    try:
        mgr.diff("--not-a-ref")
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("expected a bad ref to raise, not be parsed as a flag")
