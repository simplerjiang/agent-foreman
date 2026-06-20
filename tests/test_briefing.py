"""Tests for the BriefingService (T4.6, DESIGN §4.1 Briefing / §5.5).

Two concerns: (1) parsing an LLM reply into a briefing — robustly, degrading to the raw text rather
than an empty card; (2) the live `generate()` path wired through a mock LLMClient (httpx.MockTransport
— no network, no tokens): it gathers a session's activity, persists a report, emits a `briefing`
event, best-effort pushes, and carries the §15 language directive.
"""

from __future__ import annotations

import json

import httpx

from foreman.client.core.briefing import (
    BriefingService,
    build_brief_prompt,
    parse_brief,
)
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.shared.config import Config
from foreman.shared.events import EventBus, make_event
from foreman.shared.llm import LLMClient


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _llm(reply_text: str, captured: dict) -> LLMClient:
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "test-model"
    cfg.secrets.llm_api_key = "secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})

    return LLMClient(cfg, transport=httpx.MockTransport(handler))


# ── parse_brief ──────────────────────────────────────────────────────────────────────────────────


def test_parse_brief_full():
    res = parse_brief('{"title": "Login refactor", "body_md": "- did x\\n- stuck on y"}')
    assert res.title == "Login refactor"
    assert "did x" in res.body_md


def test_parse_brief_fenced():
    res = parse_brief('```json\n{"title": "T", "body_md": "B"}\n```')
    assert res.title == "T" and res.body_md == "B"


def test_parse_brief_prose_embedded():
    res = parse_brief('Here you go:\n{"title": "T", "body_md": "B"}\nhope that helps')
    assert res.title == "T" and res.body_md == "B"


def test_parse_brief_garbage_degrades_to_raw():
    res = parse_brief("just some plain prose, not json")
    assert res.title == "简报"
    assert "plain prose" in res.body_md  # surfaces the raw text rather than an empty card


def test_parse_brief_empty():
    res = parse_brief("")
    assert res.title == "简报" and res.body_md == "（简报输出为空）"


def test_build_brief_prompt_carries_goal_and_activity():
    p = build_brief_prompt("refactor auth", "[ts] agent_output: did x", kind="daily")
    assert "refactor auth" in p and "did x" in p and "daily" in p


# ── generate(): live path through a mock LLM ─────────────────────────────────────────────────────


async def test_generate_persists_report_and_event(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="refactor auth"))
    store.add_event(make_event("agent_output", "claude-code", "s1", payload={"text": "did x"}))
    cap: dict = {}
    svc = BriefingService(_llm('{"title": "T", "body_md": "B"}', cap), store, bus=EventBus())

    res = await svc.generate("s1")
    await svc.llm.aclose()

    assert res["ok"] is True
    assert res["report"]["title"] == "T" and res["report"]["body_md"] == "B"
    reports = store.get_reports("s1")
    assert len(reports) == 1 and reports[0].kind == "active-briefing"
    assert any(e.type == "briefing" for e in store.get_events("s1"))
    # prompt carried the session goal + activity; system carried the §15 language directive.
    user = cap["json"]["messages"][-1]["content"]
    assert "refactor auth" in user and "did x" in user
    assert "请始终用简体中文回答" in cap["json"]["messages"][0]["content"]


async def test_generate_language_directive_en(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    cap: dict = {}
    svc = BriefingService(_llm('{"title": "T", "body_md": "B"}', cap), store, language="en")
    await svc.generate("s1")
    await svc.llm.aclose()
    assert "Always respond in English." in cap["json"]["messages"][0]["content"]


async def test_generate_no_store():
    svc = BriefingService(_llm("{}", {}), object())  # no add_report on a bare object
    res = await svc.generate("s1")
    await svc.llm.aclose()
    assert res["error"] == "no_store"


async def test_generate_no_llm(tmp_path):
    svc = BriefingService(None, _store(tmp_path))
    assert (await svc.generate("s1"))["error"] == "no_llm"


async def test_generate_unknown_kind_normalized(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    svc = BriefingService(_llm('{"title": "T", "body_md": "B"}', {}), store)
    res = await svc.generate("s1", kind="bogus")
    await svc.llm.aclose()
    assert res["report"]["kind"] == "active-briefing"


async def test_generate_global_roster(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="alpha", status="running"))
    store.add_session(Session(id="s2", goal="beta", status="idle"))
    cap: dict = {}
    svc = BriefingService(_llm('{"title": "Daily", "body_md": "B"}', cap), store)
    res = await svc.generate(None, kind="daily")
    await svc.llm.aclose()
    assert res["report"]["session_id"] is None
    user = cap["json"]["messages"][-1]["content"]
    assert "alpha" in user and "beta" in user  # roster of all sessions


# ── list_reports ─────────────────────────────────────────────────────────────────────────────────


def test_list_reports_newest_first_and_filtered(tmp_path):
    from foreman.client.store.models import Report

    store = _store(tmp_path)
    store.add_report(Report(id="r1", session_id="s1", title="old", ts="2026-01-01T00:00:00Z"))
    store.add_report(Report(id="r2", session_id="s1", title="new", ts="2026-02-01T00:00:00Z"))
    store.add_report(Report(id="r3", session_id="s2", title="other", ts="2026-03-01T00:00:00Z"))
    svc = BriefingService(None, store)
    titles = [r["title"] for r in svc.list_reports("s1")]
    assert titles == ["new", "old"]  # newest first, scoped to s1
    assert [r["title"] for r in svc.list_reports()][0] == "other"  # global newest first


# ── best-effort push ─────────────────────────────────────────────────────────────────────────────


class _FakePusher:
    def __init__(self, gone=None):
        self.gone = gone or []
        self.calls = []

    async def send_to_all(self, subs, title, body, data):
        self.calls.append((title, body))
        return self.gone


async def test_generate_pushes_and_sets_sent(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    store.add_push_subscription(endpoint="https://push.example/1", p256dh="k", auth="a")
    pusher = _FakePusher(gone=[])
    svc = BriefingService(_llm('{"title": "T", "body_md": "B"}', {}), store, pusher=pusher)
    res = await svc.generate("s1")
    await svc.llm.aclose()
    assert pusher.calls and res["report"]["sent"] is True


async def test_generate_prunes_gone_subscription(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    store.add_push_subscription(endpoint="https://push.example/dead", p256dh="k", auth="a")
    pusher = _FakePusher(gone=["https://push.example/dead"])
    svc = BriefingService(_llm('{"title": "T", "body_md": "B"}', {}), store, pusher=pusher)
    res = await svc.generate("s1")
    await svc.llm.aclose()
    assert res["report"]["sent"] is False  # the only sub was gone
    assert store.get_push_subscriptions() == []  # pruned
