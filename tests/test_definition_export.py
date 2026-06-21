"""Tests for definition export/backup + optional at-rest encryption (T6.2, DESIGN §765 / §11.2C).

Three things are exercised end to end against a real client SQLite Store:
  1. The Store transparently encrypts/decrypts definition bodies when a cipher is configured.
  2. DefinitionService.export_bundle dumps every definition + link (plaintext or encrypted bundle).
  3. DefinitionService.import_bundle restores them idempotently and fail-closed.
These 秘方 live ONLY in the local store (§8.3) — nothing here touches the server库 or the network.
"""

from __future__ import annotations

import pytest

from foreman.client.core.definition_service import (
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    DefinitionService,
)
from foreman.client.store import Store
from foreman.client.store.models import Definition, DefinitionLink
from foreman.shared.crypto import BodyCipher

pytest.importorskip("cryptography")


def _key_cipher() -> BodyCipher:
    return BodyCipher.from_key(BodyCipher.generate_key())


def _store(tmp_path, *, cipher=None) -> Store:
    tmp_path.mkdir(parents=True, exist_ok=True)  # tests pass fresh subdirs (a/, b/)
    s = Store(str(tmp_path / "t.db"), cipher=cipher)
    s.init()
    return s


async def _seed(svc: DefinitionService) -> tuple[str, str]:
    """A workflow + a skill, wired together. Returns (workflow_id, skill_id)."""
    wf = (await svc.create_definition(kind="workflow", name="add-feature", body="steps: []"))[
        "definition"
    ]
    sk = (await svc.create_definition(kind="skill", name="write-tests", body="# how to test"))[
        "definition"
    ]
    svc.store.add_definition_link(
        DefinitionLink(id="lnk1", from_id=wf["id"], to_id=sk["id"], relation="uses_skill", step_index=0)
    )
    return wf["id"], sk["id"]


# ── at-rest encryption (Store) ────────────────────────────────────────────────────────────────
def test_store_encrypts_body_at_rest(tmp_path):
    cipher = _key_cipher()
    db = str(tmp_path / "enc.db")
    s = Store(db, cipher=cipher)
    s.init()
    s.add_definition(Definition(id="d1", kind="skill", name="s", version=1, body="my secret recipe"))
    # reader sees plaintext (transparent decryption)
    assert s.get_definition("d1").body == "my secret recipe"
    # but a cipher-less Store reading the same file sees ciphertext, not the recipe
    raw = Store(db)
    raw.init()
    stored = raw.get_definition("d1").body
    assert stored != "my secret recipe"
    assert BodyCipher.is_encrypted(stored)


def test_plaintext_and_encrypted_rows_coexist(tmp_path):
    # a row written without a cipher stays readable after a cipher is turned on (untagged → pass-through)
    db = str(tmp_path / "mix.db")
    plain = Store(db)
    plain.init()
    plain.add_definition(Definition(id="p1", kind="skill", name="old", version=1, body="legacy"))
    enc = Store(db, cipher=_key_cipher())
    enc.init()
    enc.add_definition(Definition(id="e1", kind="skill", name="new", version=1, body="fresh"))
    assert enc.get_definition("p1").body == "legacy"   # old plaintext still readable
    assert enc.get_definition("e1").body == "fresh"     # new encrypted row readable too


def test_update_definition_re_encrypts(tmp_path):
    cipher = _key_cipher()
    db = str(tmp_path / "u.db")
    s = Store(db, cipher=cipher)
    s.init()
    s.add_definition(Definition(id="d1", kind="skill", name="s", version=1, body="v1"))
    s.update_definition("d1", body="v2")
    assert s.get_definition("d1").body == "v2"
    raw = Store(db)
    raw.init()
    assert BodyCipher.is_encrypted(raw.get_definition("d1").body)


# ── export ────────────────────────────────────────────────────────────────────────────────────
async def test_export_plaintext_bundle(tmp_path):
    svc = DefinitionService(_store(tmp_path))
    await _seed(svc)
    res = svc.export_bundle()
    assert res["ok"] is True
    b = res["bundle"]
    assert b["format"] == BUNDLE_FORMAT and b["version"] == BUNDLE_VERSION
    assert b["encrypted"] is False
    assert len(b["definitions"]) == 2
    assert len(b["links"]) == 1
    bodies = {d["name"]: d["body"] for d in b["definitions"]}
    assert bodies["add-feature"] == "steps: []"   # plaintext


async def test_export_encrypted_bundle(tmp_path):
    cipher = _key_cipher()
    svc = DefinitionService(_store(tmp_path), cipher=cipher)
    await _seed(svc)
    res = svc.export_bundle(encrypt=True)
    assert res["ok"] is True
    assert res["bundle"]["encrypted"] is True
    for d in res["bundle"]["definitions"]:
        assert BodyCipher.is_encrypted(d["body"])           # bodies hidden in the file
        assert cipher.decrypt(d["body"]) in ("steps: []", "# how to test")


async def test_export_encrypt_without_cipher_fails(tmp_path):
    svc = DefinitionService(_store(tmp_path))  # no cipher
    await _seed(svc)
    res = svc.export_bundle(encrypt=True)
    assert res == {"ok": False, "error": "no_cipher"}


# ── import (round-trip + merge + fail-closed) ───────────────────────────────────────────────────
async def test_import_round_trip(tmp_path):
    src = DefinitionService(_store(tmp_path / "a"))
    await _seed(src)
    bundle = src.export_bundle()["bundle"]

    dst_store = _store(tmp_path / "b")
    dst = DefinitionService(dst_store)
    res = await dst.import_bundle(bundle)
    assert res["ok"] is True
    assert res["imported"] == 2 and res["links_imported"] == 1
    names = {d["name"] for d in dst.list_definitions()}
    assert names == {"add-feature", "write-tests"}
    assert len(dst_store.get_all_definition_links()) == 1


async def test_import_is_idempotent(tmp_path):
    src = DefinitionService(_store(tmp_path / "a"))
    await _seed(src)
    bundle = src.export_bundle()["bundle"]
    dst = DefinitionService(_store(tmp_path / "b"))
    await dst.import_bundle(bundle)
    res2 = await dst.import_bundle(bundle)  # re-import: everything skipped, nothing clobbered
    assert res2["ok"] is True
    assert res2["imported"] == 0 and res2["skipped"] == 2
    assert res2["links_imported"] == 0
    assert len(dst.list_definitions()) == 2


async def test_import_encrypted_bundle_decrypts(tmp_path):
    cipher = _key_cipher()
    src = DefinitionService(_store(tmp_path / "a"), cipher=cipher)
    await _seed(src)
    bundle = src.export_bundle(encrypt=True)["bundle"]
    # importer has the SAME key → bodies come back as plaintext
    dst = DefinitionService(_store(tmp_path / "b"), cipher=cipher)
    res = await dst.import_bundle(bundle)
    assert res["ok"] is True
    bodies = {d["name"]: d["body"] for d in dst.list_definitions()}
    assert bodies["add-feature"] == "steps: []"


async def test_import_encrypted_bundle_without_key_fails(tmp_path):
    cipher = _key_cipher()
    src = DefinitionService(_store(tmp_path / "a"), cipher=cipher)
    await _seed(src)
    bundle = src.export_bundle(encrypt=True)["bundle"]
    dst = DefinitionService(_store(tmp_path / "b"))  # no key
    assert (await dst.import_bundle(bundle))["error"] == "needs_key"


async def test_import_encrypted_bundle_wrong_key_fails(tmp_path):
    src = DefinitionService(_store(tmp_path / "a"), cipher=_key_cipher())
    await _seed(src)
    bundle = src.export_bundle(encrypt=True)["bundle"]
    dst = DefinitionService(_store(tmp_path / "b"), cipher=_key_cipher())  # different key
    assert (await dst.import_bundle(bundle))["error"] == "bad_key"


async def test_import_fail_closed_validation(tmp_path):
    svc = DefinitionService(_store(tmp_path))
    assert (await svc.import_bundle("nope"))["error"] == "bad_bundle"
    assert (await svc.import_bundle({}))["error"] == "bad_format"
    assert (await svc.import_bundle({"format": BUNDLE_FORMAT, "version": 99}))[
        "error"
    ] == "unsupported_version"
    bad = {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "definitions": "x"}
    assert (await svc.import_bundle(bad))["error"] == "bad_bundle"


async def test_import_skips_bad_rows_and_dangling_links(tmp_path):
    svc = DefinitionService(_store(tmp_path))
    bundle = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "definitions": [
            {"id": "ok1", "kind": "skill", "name": "good", "version": 1, "body": "b"},
            {"id": "bad1", "kind": "not-a-kind", "name": "x", "version": 1, "body": "b"},
            {"kind": "skill", "name": "noid", "version": 1, "body": "b"},  # missing id
        ],
        "links": [
            {"id": "l1", "from_id": "ok1", "to_id": "ghost", "relation": "uses_skill"},  # dangling
        ],
    }
    res = await svc.import_bundle(bundle)
    assert res["imported"] == 1 and res["skipped"] == 2
    assert res["links_imported"] == 0 and res["links_skipped"] == 1


async def test_import_enforces_single_live_version(tmp_path):
    # destination already has v1 live; bundle brings v2 also marked active → exactly one stays live
    dst = DefinitionService(_store(tmp_path))
    await dst.create_definition(kind="skill", name="s", body="v1")  # v1 active
    bundle = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "definitions": [
            {"id": "v2id", "kind": "skill", "name": "s", "version": 2, "body": "v2", "is_active": True},
        ],
        "links": [],
    }
    res = await dst.import_bundle(bundle)
    assert res["imported"] == 1
    live = [d for d in dst.list_definitions(kind="skill", name="s") if d["is_active"]]
    assert len(live) == 1 and live[0]["version"] == 2  # imported active version wins, v1 deactivated


async def test_import_skips_oversize_body(tmp_path):
    svc = DefinitionService(_store(tmp_path))
    bundle = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "definitions": [
            {"id": "big", "kind": "skill", "name": "huge", "version": 1, "body": "x" * 200_001},
        ],
        "links": [],
    }
    res = await svc.import_bundle(bundle)
    assert res["imported"] == 0 and res["skipped"] == 1


async def test_import_sanitizes_bad_scope_and_status(tmp_path):
    svc = DefinitionService(_store(tmp_path))
    bundle = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "definitions": [
            {
                "id": "d1", "kind": "skill", "name": "s", "version": 1, "body": "b",
                "scope_json": "not json", "metadata_json": "[1,2]", "status": "bogus",
            },
        ],
        "links": [],
    }
    res = await svc.import_bundle(bundle)
    assert res["imported"] == 1
    d = svc.get_definition("d1")
    assert d["scope_json"] == "{}" and d["metadata_json"] == "{}" and d["status"] in {"draft", "active"}


async def test_import_no_store():
    svc = DefinitionService(None)
    assert (await svc.import_bundle({"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION}))[
        "error"
    ] == "no_store"
