"""Tests for the Auditor (T4.2, DESIGN §6.7)."""

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


def test_parse_audit_cases():
    cases = [
        (
            {
                "verdict": "pass",
                "goal_quality": "on-track",
                "risk_severity": "none",
                "reasons": ["advances the step", "no destructive ops"],
                "suggestions": [],
            },
            "test-model",
            {
                "verdict": PASS,
                "goal_quality": ON_TRACK,
                "risk_severity": "none",
                "reasons": ["advances the step", "no destructive ops"],
                "suggestions": [],
                "model": "test-model",
                "needs_human": False,
            },
        ),
        (
            {
                "verdict": "revise",
                "goal_quality": "weak",
                "risk_severity": "mild",
                "suggestions": ["narrow the scope"],
            },
            "",
            {"verdict": REVISE, "suggestions": ["narrow the scope"], "needs_human": False},
        ),
        (
            {"verdict": "PASS", "goal_quality": "ON-TRACK", "risk_severity": "NONE"},
            "",
            {"verdict": PASS, "goal_quality": ON_TRACK},
        ),
        (
            {"verdict": "pass", "goal_quality": "on-track", "risk_severity": "severe"},
            "",
            {"verdict": ESCALATE, "risk_severity": SEVERE, "needs_human": True},
        ),
        (
            {"verdict": "pass", "goal_quality": "garbage", "risk_severity": "none"},
            "",
            {"verdict": REJECT},
        ),
        (
            {"verdict": "reject", "goal_quality": "garbage", "risk_severity": "none"},
            "",
            {"verdict": REJECT},
        ),
        (
            {"verdict": "vibes", "goal_quality": "on-track", "risk_severity": "none"},
            "",
            {"verdict": ESCALATE, "needs_human": True},
        ),
        (
            "looks fine to me, just run it",
            "",
            {
                "verdict": ESCALATE,
                "goal_quality": GARBAGE,
                "risk_severity": SEVERE,
                "reasons": ["auditor output was not valid JSON"],
            },
        ),
        ("", "", {"verdict": ESCALATE, "goal_quality": GARBAGE, "risk_severity": SEVERE}),
        (
            {"verdict": "pass", "goal_quality": "meh", "risk_severity": "spicy"},
            "",
            {"verdict": ESCALATE, "goal_quality": GARBAGE, "risk_severity": SEVERE},
        ),
        ([{"verdict": "pass"}], "", {"verdict": ESCALATE}),
        (
            {
                "verdict": "reject",
                "goal_quality": "garbage",
                "risk_severity": "none",
                "reasons": "off-target",
            },
            "",
            {"reasons": ["off-target"]},
        ),
        (
            '```json\n{"verdict": "pass", "goal_quality": "on-track", "risk_severity": "none"}\n```',
            "",
            {"verdict": PASS},
        ),
        (
            'Verdict:\n{"verdict": "reject", "goal_quality": "garbage", "risk_severity": "none"} done.',
            "",
            {"verdict": REJECT},
        ),
    ]
    for raw, model, checks in cases:
        raw_text = json.dumps(raw) if isinstance(raw, (dict, list)) else raw
        res = parse_audit(raw_text, model=model)
        for attr, expected in checks.items():
            assert getattr(res, attr) == expected


def test_build_audit_prompt_sections():
    p = build_audit_prompt(
        "pytest -q",
        goal="ship feature X",
        current_step="run the tests",
        code_standard="no bare except",
        qa_standard="all tests green",
        writable_paths="D:/proj/src",
        autonomy="1",
        recent_actions="edited login.py",
        rationale="verify before merge",
        expected_effect="green tests",
    )
    for marker in (
        "# Proposed command (audit this)",
        "pytest -q",
        "# Session goal",
        "ship feature X",
        "# Current step",
        "run the tests",
        "# Code standard",
        "no bare except",
        "# QA standard",
        "all tests green",
        "# Writable whitelist",
        "D:/proj/src",
        "# Autonomy dial",
        "# Recent actions",
        "edited login.py",
        "UNVERIFIED",
        "verify before merge",
        "green tests",
    ):
        assert marker in p

    minimal = build_audit_prompt("ls")
    assert "# Session goal" not in minimal
    assert "# Operator's claim" not in minimal


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
    assert res.model == "test-model"
    assert cap["json"]["response_format"] == {"type": "json_object"}
    user = cap["json"]["messages"][-1]["content"]
    assert "pytest -q" in user and "add feature" in user and "UNVERIFIED" in user


async def test_audit_appends_language_directive():
    for language, expected in [("zh", "请始终用简体中文回答"), ("en", "Always respond in English.")]:
        cap: dict = {}
        aud = _auditor(
            '{"verdict": "pass", "goal_quality": "on-track", "risk_severity": "none"}',
            cap,
            language=language,
        )
        await aud.audit("ls")
        await aud.llm.aclose()
        assert expected in cap["json"]["messages"][0]["content"]


async def test_audit_escalates_on_garbage_reply():
    cap: dict = {}
    aud = _auditor("not json at all", cap)
    res = await aud.audit("git push")
    await aud.llm.aclose()
    assert res.verdict == ESCALATE and res.needs_human is True


def test_audit_result_defaults_conservative():
    a = AuditResult(verdict=ESCALATE)
    assert a.goal_quality == GARBAGE and a.risk_severity == SEVERE
    assert a.reasons == [] and a.suggestions == [] and a.model == ""
