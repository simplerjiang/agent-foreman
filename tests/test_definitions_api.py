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


# Description is required on create now (P0). These endpoint tests check OTHER behavior, so inject a
# default metadata_json (with a description) unless the call sets its own. Negative cases trip an
# earlier check (bad_kind / no service → 503), which still fires because the gate runs last.
_DEFAULT_META = '{"description": "test fixture: does X; use when Y"}'


def _newdef(c, **fields):
    fields.setdefault("metadata_json", _DEFAULT_META)
    return c.post("/api/definitions", json=fields)


def test_create_list_get(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    r = _newdef(c, **{"kind": "workflow", "name": "add-feature", "body": "steps: []"})
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
    r = _newdef(c, **{"kind": "nope", "name": "x", "body": "y"})
    assert r.status_code == 400


def test_create_duplicate_version_409(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    _newdef(c, **{"kind": "skill", "name": "s", "version": 1, "body": "a"})
    r = _newdef(c, **{"kind": "skill", "name": "s", "version": 1, "body": "b"})
    assert r.status_code == 409


def test_update_in_place(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    d = _newdef(c, **{"kind": "skill", "name": "s", "body": "old"}).json()["definition"]
    r = c.patch(f"/api/definitions/{d['id']}", json={"body": "new"})
    assert r.status_code == 200 and r.json()["definition"]["body"] == "new"


def test_update_unknown_404(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    assert c.patch("/api/definitions/nope", json={"body": "x"}).status_code == 404


def test_activate_rolls_back(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    v1 = _newdef(c, **{"kind": "qa_rubric", "name": "r", "body": "v1"}).json()["definition"]
    _newdef(c, **{"kind": "qa_rubric", "name": "r", "body": "v2"})  # v2 live
    r = c.post(f"/api/definitions/{v1['id']}/activate")
    assert r.status_code == 200 and r.json()["definition"]["is_active"] is True
    live = {d["version"]: d["is_active"] for d in c.get("/api/definitions?name=r").json()}
    assert live == {1: True, 2: False}


def test_delete(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    d = _newdef(c, **{"kind": "skill", "name": "s", "body": "x"}).json()["definition"]
    assert c.delete(f"/api/definitions/{d['id']}").status_code == 200
    assert c.get(f"/api/definitions/{d['id']}").status_code == 404
    assert c.delete(f"/api/definitions/{d['id']}").status_code == 404  # already gone


def test_no_service_returns_503(tmp_path):
    """Team-cache server (no DefinitionService injected) → 503: 秘方 are local-only (§8.3)."""
    app, _ = _app(tmp_path, with_service=False)
    c = TestClient(app)
    assert c.get("/api/definitions").status_code == 503
    assert _newdef(c, **{"kind": "skill", "name": "s"}).status_code == 503


# ── export / import endpoints (T6.2) ─────────────────────────────────────────────────────────────
def test_export_import_round_trip(tmp_path):
    """GET /api/definitions/export → POST /api/definitions/import restores everything (T6.2)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    app, _ = _app(tmp_path / "a")
    c = TestClient(app)
    _newdef(c, **{"kind": "workflow", "name": "wf", "body": "steps: []"})
    _newdef(c, **{"kind": "skill", "name": "sk", "body": "# skill"})
    bundle = c.get("/api/definitions/export").json()
    assert bundle["format"] == "foreman-definitions"
    assert len(bundle["definitions"]) == 2

    app2, _ = _app(tmp_path / "b")
    c2 = TestClient(app2)
    r = c2.post("/api/definitions/import", json={"bundle": bundle})
    assert r.status_code == 200 and r.json()["imported"] == 2
    assert {d["name"] for d in c2.get("/api/definitions").json()} == {"wf", "sk"}


def test_export_route_not_shadowed_by_param(tmp_path):
    """/api/definitions/export must not be captured as definition_id='export' (route order)."""
    app, _ = _app(tmp_path)
    c = TestClient(app)
    r = c.get("/api/definitions/export")
    assert r.status_code == 200 and "definitions" in r.json()


def test_import_bad_bundle_400(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    assert c.post("/api/definitions/import", json={"bundle": {}}).status_code == 400


def test_export_import_no_service_503(tmp_path):
    app, _ = _app(tmp_path, with_service=False)
    c = TestClient(app)
    assert c.get("/api/definitions/export").status_code == 503
    assert c.post("/api/definitions/import", json={"bundle": {}}).status_code == 503
