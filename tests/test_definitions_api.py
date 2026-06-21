"""Tests for the T6.1 REST endpoints (DESIGN §11.2): /api/definitions CRUD.

create_app stays shared-only — the DefinitionService is INJECTED (client-side core), exactly like
the Gate/CardService. A real client Store backs it; no LLM/network involved. Personal mode without
the service → 503 (definitions are local-only; the shared server never holds 秘方, §8.3 / §14).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.core.definition_service import DefinitionService
from foreman.client.store import Store
from foreman.server.app import create_app
from foreman.shared.config import Config
from foreman.shared.events import EventBus


def _app(tmp_path, *, with_service=True):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    bus = EventBus()
    definitions = DefinitionService(store, bus=bus) if with_service else None
    app = create_app(Config(), store, bus, definitions=definitions)
    return app, store


def test_create_list_get(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    r = c.post("/api/definitions", json={"kind": "workflow", "name": "add-feature", "body": "steps: []"})
    assert r.status_code == 200
    d = r.json()["definition"]
    assert d["is_active"] is True
    # list
    rows = c.get("/api/definitions").json()
    assert len(rows) == 1 and rows[0]["name"] == "add-feature"
    # filter by kind
    assert c.get("/api/definitions?kind=skill").json() == []
    # get one
    got = c.get(f"/api/definitions/{d['id']}").json()
    assert got["body"] == "steps: []"


def test_create_bad_kind_400(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    r = c.post("/api/definitions", json={"kind": "nope", "name": "x", "body": "y"})
    assert r.status_code == 400


def test_create_duplicate_version_409(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    c.post("/api/definitions", json={"kind": "skill", "name": "s", "version": 1, "body": "a"})
    r = c.post("/api/definitions", json={"kind": "skill", "name": "s", "version": 1, "body": "b"})
    assert r.status_code == 409


def test_update_in_place(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    d = c.post("/api/definitions", json={"kind": "skill", "name": "s", "body": "old"}).json()["definition"]
    r = c.patch(f"/api/definitions/{d['id']}", json={"body": "new"})
    assert r.status_code == 200 and r.json()["definition"]["body"] == "new"


def test_update_unknown_404(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    assert c.patch("/api/definitions/nope", json={"body": "x"}).status_code == 404


def test_activate_rolls_back(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    v1 = c.post("/api/definitions", json={"kind": "qa_rubric", "name": "r", "body": "v1"}).json()["definition"]
    c.post("/api/definitions", json={"kind": "qa_rubric", "name": "r", "body": "v2"})  # v2 live
    r = c.post(f"/api/definitions/{v1['id']}/activate")
    assert r.status_code == 200 and r.json()["definition"]["is_active"] is True
    live = {d["version"]: d["is_active"] for d in c.get("/api/definitions?name=r").json()}
    assert live == {1: True, 2: False}


def test_delete(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    d = c.post("/api/definitions", json={"kind": "skill", "name": "s", "body": "x"}).json()["definition"]
    assert c.delete(f"/api/definitions/{d['id']}").status_code == 200
    assert c.get(f"/api/definitions/{d['id']}").status_code == 404
    assert c.delete(f"/api/definitions/{d['id']}").status_code == 404  # already gone


def test_no_service_returns_503(tmp_path):
    """Team-cache server (no DefinitionService injected) → 503: 秘方 are local-only (§8.3)."""
    app, _ = _app(tmp_path, with_service=False)
    c = TestClient(app)
    assert c.get("/api/definitions").status_code == 503
    assert c.post("/api/definitions", json={"kind": "skill", "name": "s"}).status_code == 503
