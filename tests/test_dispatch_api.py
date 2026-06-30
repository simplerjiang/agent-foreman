"""Tests for the T4.6 REST endpoints (DESIGN §5.1/§5.5): /api/tasks, /api/overview, /api/reports.

create_app stays shared-only — the DispatchService + BriefingService are INJECTED (client-side core),
exactly like the Gate/CardService. A real client Store backs them; the briefing LLM is mocked
(httpx.MockTransport — no network/tokens) and the dispatcher uses no launcher (no real agent spawn).
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from foreman.client.core.briefing import BriefingService
from foreman.client.core.dispatch_service import DispatchService
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.server.app import create_app
from foreman.shared.config import Config, WorkspaceCfg
from foreman.shared.events import EventBus, make_event
from foreman.shared.llm import LLMClient


def _llm(reply_text: str) -> LLMClient:
    cfg = Config()
    cfg.secrets.llm_api_key = "k"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})

    return LLMClient(cfg, transport=httpx.MockTransport(handler))


def _app(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    bus = EventBus()
    cfg = Config()
    cfg.workspaces = [WorkspaceCfg(path="D:/proj")]
    dispatcher = DispatchService(cfg, store, bus=bus)  # no launcher → no real spawn
    briefings = BriefingService(
        _llm('{"title": "Briefing", "body_md": "all good"}'), store, bus=bus
    )
    app = create_app(cfg, store, bus, dispatcher=dispatcher, briefings=briefings)
    return app, store


def test_dispatch_task_creates_session(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    r = c.post("/api/tasks", json={"goal": "refactor auth"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["workspace"] == "D:/proj"
    # the new session shows up in both the plain list and the multi-session overview.
    assert any(s["id"] == body["session_id"] for s in c.get("/api/sessions").json())
    ov = c.get("/api/overview").json()
    row = next(d for d in ov if d["id"] == body["session_id"])
    assert row["goal"] == "refactor auth" and row["events"] >= 1  # the dispatch event
    assert row["workspace"] == "D:/proj"


def test_dispatch_task_accepts_model(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    r = c.post("/api/tasks", json={"goal": "refactor auth", "model": "gpt-5"})
    assert r.status_code == 200
    assert r.json()["model"] == "gpt-5"


def test_dispatch_task_accepts_effort(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    r = c.post("/api/tasks", json={"goal": "refactor auth", "effort": "high"})
    assert r.status_code == 200
    assert r.json()["effort"] == "high"


def test_dispatch_empty_goal_400(tmp_path):
    app, _ = _app(tmp_path)
    assert TestClient(app).post("/api/tasks", json={"goal": "  "}).status_code == 400


def test_dispatch_accepts_work_mode_ids(tmp_path):
    """The composer sends work_mode_ids; the backend accepts them (no 422/400) and threads them to
    the resolver as manual picks (P1). A request with no such field stays fully auto (backward-
    compat). Here the dispatcher has no PM agent, so the ids are simply accepted and the dispatch
    succeeds — the consumption path is asserted end-to-end in test_work_mode_p1."""
    app, _ = _app(tmp_path)
    c = TestClient(app)
    with_ids = c.post("/api/tasks", json={"goal": "do it", "work_mode_ids": ["id-a", "id-b"]})
    assert with_ids.status_code == 200 and with_ids.json()["ok"] is True
    # the legacy shape (no work_mode_ids) is unaffected
    legacy = c.post("/api/tasks", json={"goal": "do it"})
    assert legacy.status_code == 200 and legacy.json()["ok"] is True


def test_dispatch_503_without_dispatcher():
    c = TestClient(create_app(Config()))
    assert c.post("/api/tasks", json={"goal": "x"}).status_code == 503
    assert c.get("/api/overview").status_code == 503


def test_reports_generate_and_list(tmp_path):
    app, store = _app(tmp_path)
    store.add_session(Session(id="s1", goal="g"))
    store.add_event(make_event("agent_output", "claude-code", "s1", payload={"t": "x"}))
    c = TestClient(app)

    assert c.get("/api/reports").json() == []  # none yet
    gen = c.post("/api/reports/generate", json={"session_id": "s1"})
    assert gen.status_code == 200
    assert gen.json()["report"]["title"] == "Briefing"

    listed = c.get("/api/reports").json()
    assert len(listed) == 1 and listed[0]["body_md"] == "all good"
    assert c.get("/api/reports?session_id=s1").json()[0]["session_id"] == "s1"


def test_reports_503_without_service():
    c = TestClient(create_app(Config()))
    assert c.get("/api/reports").status_code == 503
    assert c.post("/api/reports/generate", json={}).status_code == 503
