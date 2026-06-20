"""Tests for the Auditor (T4.2, DESIGN §6.7).

Three concerns: (1) parsing an LLM reply into a validated two-axis result — robustly and
*conservatively* (garbage / unknown verdict → escalate; unknown axes → worst case; the verdict is
only ever tightened by the axes, never loosened — severe risk forces escalate, garbage quality can
never pass); (2) the GuardAgent checklist is assembled into the prompt with the Operator's rationale
flagged as an unverified claim; (3) the live `audit()` path wired through a mock LLMClient
(httpx.MockTransport — no network, no tokens), checking the §15 language directive and that the
model id is recorded on the result (for the `audits` row).
"""

from __future__ import annotations

import json

import httpx

from foreman.client.core.auditor import (
    ESCALATE,
    GARBAGE,
    ON_TRACK,
    PASS,
    REJECT,
    REVISE,
    SEVERE,
    Auditor,
    AuditResult,
    build_audit_prompt,
    parse_audit,
)
from foreman.shared.config import Config
from foreman.shared.llm import LLMClient


# ── parse_audit: happy paths ────────────────────────────────────────────────────────────────────


def test_parse_full_pass():
    raw = json.dumps({
        "verdict": "pass",
        "goal_quality": "on-track",
        "risk_severity": "none",
        "reasons": ["advances the step", "no destructive ops"],
        "suggestions": [],
    })
    res = parse_audit(raw, model="test-model")
    assert res.verdict == PASS
    assert res.goal_quality == ON_TRACK
    assert res.risk_severity == "none"
    assert res.reasons == ["advances the step", "no destructive ops"]
    assert res.suggestions == []
    assert res.model == "test-model"
    assert res.needs_human is False


def test_parse_revise():
    res = parse_audit(json.dumps({
        "verdict": "revise",
        "goal_quality": "weak",
        "risk_severity": "mild",
        "suggestions": ["narrow the scope"],
    }))
    assert res.verdict == REVISE
    assert res.suggestions == ["narrow the scope"]
    assert res.needs_human is False


def test_parse_verdict_case_insensitive():
    res = parse_audit('{"verdict": "PASS", "goal_quality": "ON-TRACK", "risk_severity": "NONE"}')
    assert res.verdict == PASS and res.goal_quality == ON_TRACK


# ── parse_audit: conservative synthesis (从严默认), code-enforced ────────────────────────────────


def test_severe_risk_forces_escalate_even_if_model_says_pass():
    # The model may go soft; severe risk always goes to a human (DESIGN §6.7).
    res = parse_audit(json.dumps({
        "verdict": "pass",
        "goal_quality": "on-track",
        "risk_severity": "severe",
    }))
    assert res.verdict == ESCALATE
    assert res.risk_severity == SEVERE
    assert res.needs_human is True


def test_garbage_quality_never_passes():
    res = parse_audit(json.dumps({
        "verdict": "pass",
        "goal_quality": "garbage",
        "risk_severity": "none",
    }))
    assert res.verdict == REJECT  # downgraded, not passed


def test_garbage_quality_keeps_stricter_verdict():
    # A reject stays reject (we tighten, never loosen).
    res = parse_audit(json.dumps({
        "verdict": "reject",
        "goal_quality": "garbage",
        "risk_severity": "none",
    }))
    assert res.verdict == REJECT


def test_unknown_verdict_escalates():
    res = parse_audit('{"verdict": "vibes", "goal_quality": "on-track", "risk_severity": "none"}')
    assert res.verdict == ESCALATE
    assert res.needs_human is True


def test_non_json_escalates_worst_case():
    res = parse_audit("looks fine to me, just run it")
    assert res.verdict == ESCALATE
    assert res.goal_quality == GARBAGE
    assert res.risk_severity == SEVERE
    assert res.reasons == ["auditor output was not valid JSON"]


def test_empty_escalates():
    res = parse_audit("")
    assert res.verdict == ESCALATE
    assert res.goal_quality == GARBAGE
    assert res.risk_severity == SEVERE


def test_unknown_axes_fall_back_to_worst_case_and_escalate():
    # Unknown risk → severe → forces escalate; unknown quality → garbage.
    res = parse_audit('{"verdict": "pass", "goal_quality": "meh", "risk_severity": "spicy"}')
    assert res.goal_quality == GARBAGE
    assert res.risk_severity == SEVERE
    assert res.verdict == ESCALATE


def test_top_level_array_escalates():
    # A JSON array (not an object) is not a valid result → fail closed.
    res = parse_audit('[{"verdict": "pass"}]')
    assert res.verdict == ESCALATE


def test_reasons_coerce_lone_string_to_list():
    res = parse_audit(json.dumps({
        "verdict": "reject",
        "goal_quality": "garbage",
        "risk_severity": "none",
        "reasons": "off-target",
    }))
    assert res.reasons == ["off-target"]


# ── parse_audit: tolerant extraction ────────────────────────────────────────────────────────────


def test_parse_strips_code_fence():
    raw = '```json\n{"verdict": "pass", "goal_quality": "on-track", "risk_severity": "none"}\n```'
    assert parse_audit(raw).verdict == PASS


def test_parse_extracts_object_from_prose():
    raw = 'Verdict:\n{"verdict": "reject", "goal_quality": "garbage", "risk_severity": "none"} done.'
    assert parse_audit(raw).verdict == REJECT


# ── build_audit_prompt ──────────────────────────────────────────────────────────────────────────


def test_prompt_includes_command():
    p = build_audit_prompt("git push origin main")
    assert "# Proposed command (audit this)" in p
    assert "git push origin main" in p


def test_prompt_includes_guard_checklist():
    p = build_audit_prompt(
        "pytest -q",
        goal="ship feature X",
        current_step="run the tests",
        code_standard="no bare except",
        qa_standard="all tests green",
        writable_paths="D:/proj/src",
        autonomy="1",
        recent_actions="edited login.py",
    )
    for marker in (
        "# Session goal", "ship feature X",
        "# Current step", "run the tests",
        "# Code standard", "no bare except",
        "# QA standard", "all tests green",
        "# Writable whitelist", "D:/proj/src",
        "# Autonomy dial",
        "# Recent actions", "edited login.py",
    ):
        assert marker in p


def test_prompt_flags_rationale_as_unverified():
    p = build_audit_prompt("rm -rf build", rationale="clean stale artifacts", expected_effect="fresh build")
    assert "UNVERIFIED" in p
    assert "clean stale artifacts" in p
    assert "fresh build" in p


def test_prompt_omits_empty_optional_sections():
    p = build_audit_prompt("ls")
    assert "# Session goal" not in p
    assert "# Operator's claim" not in p


# ── Auditor.audit() through a mock LLM (no network) ─────────────────────────────────────────────


def _auditor(reply_text: str, captured: dict, *, language: str = "zh") -> Auditor:
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "test-model"
    cfg.secrets.llm_api_key = "secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})

    llm = LLMClient(cfg, transport=httpx.MockTransport(handler))
    return Auditor(llm, language=language)


async def test_audit_parses_llm_reply_and_records_model():
    cap: dict = {}
    reply = json.dumps({
        "verdict": "pass",
        "goal_quality": "on-track",
        "risk_severity": "none",
        "reasons": ["fine"],
    })
    aud = _auditor(reply, cap)
    res = await aud.audit("pytest -q", rationale="verify", goal="add feature")
    await aud.llm.aclose()

    assert res.verdict == PASS
    assert res.model == "test-model"  # recorded for the audits row
    # json_mode nudged + prompt carried the command, goal, and the unverified-claim banner.
    assert cap["json"]["response_format"] == {"type": "json_object"}
    user = cap["json"]["messages"][-1]["content"]
    assert "pytest -q" in user and "add feature" in user and "UNVERIFIED" in user


async def test_audit_appends_language_directive_zh():
    cap: dict = {}
    aud = _auditor('{"verdict": "pass", "goal_quality": "on-track", "risk_severity": "none"}', cap,
                   language="zh")
    await aud.audit("ls")
    await aud.llm.aclose()
    assert "请始终用简体中文回答" in cap["json"]["messages"][0]["content"]


async def test_audit_appends_language_directive_en():
    cap: dict = {}
    aud = _auditor('{"verdict": "pass", "goal_quality": "on-track", "risk_severity": "none"}', cap,
                   language="en")
    await aud.audit("ls")
    await aud.llm.aclose()
    assert "Always respond in English." in cap["json"]["messages"][0]["content"]


async def test_audit_escalates_on_garbage_reply():
    cap: dict = {}
    aud = _auditor("not json at all", cap)
    res = await aud.audit("git push")
    await aud.llm.aclose()
    assert res.verdict == ESCALATE and res.needs_human is True


def test_audit_result_defaults_conservative():
    # Direct dataclass construction: verdict required, the axes default to worst case.
    a = AuditResult(verdict=ESCALATE)
    assert a.goal_quality == GARBAGE and a.risk_severity == SEVERE
    assert a.reasons == [] and a.suggestions == [] and a.model == ""
