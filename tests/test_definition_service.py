"""Tests for the DefinitionService — CRUD the four 秘方 blocks from the UI (T6.1, DESIGN §11.2).

create / list / read / update / activate / delete with fail-closed validation. A real client
SQLite Store backs it so persistence + the `definition` audit event are exercised end to end; the
EventBus is real (no network). These 秘方 live ONLY in the local store (§8.3).
"""

from __future__ import annotations

from foreman.client.core.definition_service import KNOWN_KINDS, DefinitionService
from foreman.client.store import Store
from foreman.client.store.models import Definition
from foreman.shared.events import EventBus


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _svc(tmp_path, *, bus=None):
    store = _store(tmp_path)
    return DefinitionService(store, bus=bus), store


# Description is now required on create (P0). These tests predate that and exercise OTHER behaviors,
# so inject a default metadata_json (with a description) unless the call sets its own. Negative tests
# that pass metadata_json explicitly, or trip an earlier check (bad_kind/bad_name/...), are unaffected
# because the description gate runs last.
_DEFAULT_META = '{"description": "test fixture: does a thing; use when relevant"}'


async def _mkdef(svc, **kw):
    kw.setdefault("metadata_json", _DEFAULT_META)
    create = svc.create_definition
    return await create(**kw)


# ── create ──────────────────────────────────────────────────────────────────────────────────────
async def test_create_persists_and_activates(tmp_path):
    svc, store = _svc(tmp_path)
    res = await _mkdef(svc, kind="workflow", name="add-feature", body="steps: []")
    assert res["ok"] is True
    d = res["definition"]
    assert d["kind"] == "workflow" and d["name"] == "add-feature" and d["version"] == 1
    assert d["is_active"] is True  # activate defaults True → it's the live version
    assert d["status"] == "active"
    # round-trips through the store
    assert store.get_definition(d["id"]).body == "steps: []"


def _drain(q) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_create_emits_definition_event(tmp_path):
    bus = EventBus()
    q = bus.subscribe_queue()
    svc, store = _svc(tmp_path, bus=bus)
    res = await _mkdef(svc, kind="skill", name="how-to-test", body="x")
    seen = _drain(q)
    assert seen and seen[-1].type == "definition"
    assert seen[-1].payload["action"] == "created"
    assert seen[-1].payload["id"] == res["definition"]["id"]
    # persisted too (audit trail)
    assert any(e.type == "definition" for e in store.get_events(""))


async def test_create_autobumps_version(tmp_path):
    svc, _ = _svc(tmp_path)
    a = await _mkdef(svc, kind="skill", name="s", body="v1")
    b = await _mkdef(svc, kind="skill", name="s", body="v2")
    assert a["definition"]["version"] == 1
    assert b["definition"]["version"] == 2
    # creating the second version (activate default True) makes it the live one
    assert b["definition"]["is_active"] is True


async def test_create_explicit_duplicate_version_conflicts(tmp_path):
    svc, _ = _svc(tmp_path)
    await _mkdef(svc, kind="skill", name="s", version=1, body="a")
    res = await _mkdef(svc, kind="skill", name="s", version=1, body="b")
    assert res == {"ok": False, "error": "version_exists"}


async def test_create_no_activate_leaves_previous_live(tmp_path):
    svc, _ = _svc(tmp_path)
    v1 = await _mkdef(svc, kind="qa_rubric", name="r", body="v1")
    v2 = await _mkdef(svc, kind="qa_rubric", name="r", body="v2", activate=False)
    assert v2["definition"]["is_active"] is False
    # v1 stays the active one
    rows = {d["version"]: d["is_active"] for d in svc.list_definitions(name="r")}
    assert rows == {1: True, 2: False}
    assert v1["definition"]["version"] == 1


async def test_create_rejects_unknown_kind(tmp_path):
    svc, _ = _svc(tmp_path)
    res = await _mkdef(svc, kind="prompt-template", name="x", body="y")
    assert res == {"ok": False, "error": "bad_kind"}


async def test_create_rejects_blank_name(tmp_path):
    svc, _ = _svc(tmp_path)
    assert (await _mkdef(svc, kind="skill", name="   ", body="y"))["error"] == "bad_name"


async def test_create_rejects_oversize_body(tmp_path):
    svc, _ = _svc(tmp_path)
    res = await _mkdef(svc, kind="skill", name="x", body="a" * 200_001)
    assert res == {"ok": False, "error": "body_too_large"}


async def test_create_rejects_non_object_scope_json(tmp_path):
    svc, _ = _svc(tmp_path)
    res = await _mkdef(svc, kind="skill", name="x", body="y", scope_json="[1,2]")
    assert res == {"ok": False, "error": "bad_scope_json"}


async def test_create_rejects_invalid_metadata_json(tmp_path):
    svc, _ = _svc(tmp_path)
    res = await _mkdef(svc, kind="skill", name="x", body="y", metadata_json="{bad")
    assert res == {"ok": False, "error": "bad_metadata_json"}


async def test_create_no_store_errors():
    svc = DefinitionService(None)
    assert (await _mkdef(svc, kind="skill", name="x", body="y"))["error"] == "no_store"


# ── description gate (P0, §4.3) ─────────────────────────────────────────────────────────────────
async def test_create_requires_description(tmp_path):
    svc, _ = _svc(tmp_path)
    # No metadata at all → missing_description (the default "{}" has no description).
    res = await svc.create_definition(kind="skill", name="x", body="y")
    assert res == {"ok": False, "error": "missing_description"}


async def test_create_rejects_blank_description(tmp_path):
    svc, _ = _svc(tmp_path)
    res = await svc.create_definition(
        kind="skill", name="x", body="y", metadata_json='{"description": "   "}'
    )
    assert res == {"ok": False, "error": "missing_description"}


async def test_create_rejects_overlong_description(tmp_path):
    svc, _ = _svc(tmp_path)
    import json as _json
    meta = _json.dumps({"description": "x" * 1025})
    res = await svc.create_definition(kind="skill", name="x", body="y", metadata_json=meta)
    assert res == {"ok": False, "error": "description_too_long"}


async def test_create_accepts_valid_description(tmp_path):
    svc, _ = _svc(tmp_path)
    res = await svc.create_definition(
        kind="skill", name="x", body="y",
        metadata_json='{"description": "does X; use when Y"}',
    )
    assert res["ok"] is True


async def test_update_with_metadata_requires_description(tmp_path):
    svc, _ = _svc(tmp_path)
    d = (await _mkdef(svc, kind="skill", name="s", body="x"))["definition"]
    # A PATCH that DOES carry metadata_json must include a description.
    res = await svc.update_definition(d["id"], metadata_json="{}")
    assert res == {"ok": False, "error": "missing_description"}


async def test_update_body_only_does_not_require_description(tmp_path):
    svc, _ = _svc(tmp_path)
    d = (await _mkdef(svc, kind="skill", name="s", body="x"))["definition"]
    # A body-only PATCH (no metadata_json) is never forced to re-supply a description.
    res = await svc.update_definition(d["id"], body="new")
    assert res["ok"] is True and res["definition"]["body"] == "new"


# ── read / list ──────────────────────────────────────────────────────────────────────────────────
async def test_list_filters_by_kind(tmp_path):
    svc, _ = _svc(tmp_path)
    await _mkdef(svc, kind="workflow", name="w", body="a")
    await _mkdef(svc, kind="skill", name="s", body="b")
    kinds = {d["kind"] for d in svc.list_definitions(kind="skill")}
    assert kinds == {"skill"}


def test_get_unknown_returns_none(tmp_path):
    svc, _ = _svc(tmp_path)
    assert svc.get_definition("nope") is None


# ── update ──────────────────────────────────────────────────────────────────────────────────────
async def test_update_edits_body_in_place(tmp_path):
    svc, store = _svc(tmp_path)
    d = (await _mkdef(svc, kind="skill", name="s", body="old"))["definition"]
    res = await svc.update_definition(d["id"], body="new")
    assert res["ok"] is True and res["definition"]["body"] == "new"
    # identity unchanged
    assert res["definition"]["kind"] == "skill" and res["definition"]["version"] == 1
    assert store.get_definition(d["id"]).body == "new"


async def test_update_rejects_bad_status(tmp_path):
    svc, _ = _svc(tmp_path)
    d = (await _mkdef(svc, kind="skill", name="s", body="x"))["definition"]
    assert (await svc.update_definition(d["id"], status="live"))["error"] == "bad_status"


async def test_update_rejects_bad_scope_json(tmp_path):
    svc, _ = _svc(tmp_path)
    d = (await _mkdef(svc, kind="skill", name="s", body="x"))["definition"]
    assert (await svc.update_definition(d["id"], scope_json="nope"))["error"] == "bad_scope_json"


async def test_update_unknown_id_not_found(tmp_path):
    svc, _ = _svc(tmp_path)
    assert (await svc.update_definition("nope", body="x"))["error"] == "not_found"


# ── activate (enable / rollback) ──────────────────────────────────────────────────────────────────
async def test_activate_makes_exactly_one_live(tmp_path):
    svc, _ = _svc(tmp_path)
    v1 = (await _mkdef(svc, kind="workflow", name="w", body="v1"))["definition"]
    (await _mkdef(svc, kind="workflow", name="w", body="v2"))  # v2 now live
    # roll back to v1
    res = await svc.activate_definition(v1["id"])
    assert res["ok"] is True and res["definition"]["is_active"] is True
    live = {d["version"]: d["is_active"] for d in svc.list_definitions(name="w")}
    assert live == {1: True, 2: False}


async def test_activate_unknown_id_not_found(tmp_path):
    svc, _ = _svc(tmp_path)
    assert (await svc.activate_definition("nope"))["error"] == "not_found"


# ── delete ──────────────────────────────────────────────────────────────────────────────────────
async def test_delete_removes_row(tmp_path):
    svc, store = _svc(tmp_path)
    d = (await _mkdef(svc, kind="skill", name="s", body="x"))["definition"]
    res = await svc.delete_definition(d["id"])
    assert res == {"ok": True, "id": d["id"]}
    assert store.get_definition(d["id"]) is None


async def test_delete_unknown_id_not_found(tmp_path):
    svc, _ = _svc(tmp_path)
    assert (await svc.delete_definition("nope"))["error"] == "not_found"


async def test_delete_emits_event_with_identity(tmp_path):
    bus = EventBus()
    q = bus.subscribe_queue()
    svc, _ = _svc(tmp_path, bus=bus)
    d = (await _mkdef(svc, kind="code_standard", name="cs", body="x"))["definition"]
    await svc.delete_definition(d["id"])
    ev = _drain(q)[-1]
    assert ev.type == "definition" and ev.payload["action"] == "deleted"
    assert ev.payload["kind"] == "code_standard" and ev.payload["name"] == "cs"


# ── boundary / sanity ─────────────────────────────────────────────────────────────────────────────
def test_known_kinds_are_the_four_blocks():
    assert KNOWN_KINDS == {"workflow", "skill", "code_standard", "qa_rubric"}


def test_delete_definition_store_removes_links(tmp_path):
    """The store-level delete also drops links pointing to/from the definition (no dangling wiring)."""
    from foreman.client.store.models import DefinitionLink

    store = _store(tmp_path)
    store.add_definition(Definition(id="wf", kind="workflow", name="w"))
    store.add_definition(Definition(id="sk", kind="skill", name="s"))
    store.add_definition_link(
        DefinitionLink(id="l1", from_id="wf", to_id="sk", relation="uses_skill", step_index=0)
    )
    assert store.delete_definition("wf") is True
    assert store.get_definition_links("wf") == []
    assert store.delete_definition("wf") is False  # idempotent
