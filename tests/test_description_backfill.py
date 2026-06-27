"""Tests for the legacy-definition LLM description backfill (P0 task 4b / D3).

Uses a FAKE LLM (no network / credentials) so the batch logic — dry-run vs apply, skip-already-has,
per-row error isolation — is exercised deterministically.
"""

from __future__ import annotations

import json
import uuid

from foreman.client.core.definition_service import DefinitionService
from foreman.client.core.description_backfill import (
    backfill_descriptions,
    summarize_to_description,
)
from foreman.client.store import Store
from foreman.client.store.models import Definition


class _FakeLLM:
    def __init__(self, text="自动生成：做某事；何时用：相关任务时。"):
        self.text = text
        self.calls = 0

    async def complete(self, messages, **kw):
        self.calls += 1
        return self.text


class _BoomLLM:
    async def complete(self, messages, **kw):
        raise RuntimeError("llm down")


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def _seed(store, name, *, metadata_json="{}", body="some body") -> str:
    """Insert a definition directly (bypassing the gate, like seed_examples) so we can stage legacy
    rows with/without descriptions."""
    row = Definition(
        id=uuid.uuid4().hex, kind="skill", name=name, version=1, status="draft",
        scope_json="{}", body=body, metadata_json=metadata_json,
    )
    store.add_definition(row)
    store.set_definition_active(row.id)
    return row.id


async def test_summarize_trims_and_caps():
    llm = _FakeLLM(text="  " + "x" * 2000 + "  ")
    out = await summarize_to_description(llm, "body", kind="skill", name="s")
    assert len(out) == 1024
    assert out == "x" * 1024


async def test_dry_run_proposes_but_writes_nothing(tmp_path):
    store = _store(tmp_path)
    svc = DefinitionService(store)
    rid = _seed(store, "needs-desc")
    res = await backfill_descriptions(store, svc, _FakeLLM(), apply=False)
    assert res["apply"] is False
    assert res["written"] == 0
    assert [p["name"] for p in res["proposals"]] == ["needs-desc"]
    # Nothing persisted — still no description on the row.
    meta = json.loads(store.get_definition(rid).metadata_json)
    assert "description" not in meta


async def test_apply_writes_descriptions(tmp_path):
    store = _store(tmp_path)
    svc = DefinitionService(store)
    rid = _seed(store, "needs-desc", metadata_json=json.dumps({"example": True}))
    res = await backfill_descriptions(store, svc, _FakeLLM(), apply=True)
    assert res["apply"] is True
    assert res["written"] == 1
    meta = json.loads(store.get_definition(rid).metadata_json)
    assert meta["description"]  # now present
    assert meta["example"] is True  # existing keys preserved
    assert meta["schema"] == "foreman.workmode.meta/1"


async def test_existing_description_is_skipped(tmp_path):
    store = _store(tmp_path)
    svc = DefinitionService(store)
    _seed(store, "has-desc", metadata_json=json.dumps({"description": "already here"}))
    llm = _FakeLLM()
    res = await backfill_descriptions(store, svc, llm, apply=True)
    assert res["proposals"] == []
    assert llm.calls == 0  # never asked the LLM about a row that already has a description


async def test_llm_error_is_isolated_per_row(tmp_path):
    store = _store(tmp_path)
    svc = DefinitionService(store)
    _seed(store, "a")
    _seed(store, "b")
    res = await backfill_descriptions(store, svc, _BoomLLM(), apply=True)
    assert res["written"] == 0
    assert len(res["errors"]) == 2  # both failed, but the batch didn't crash
    assert all(e["error"] == "llm down" for e in res["errors"])


async def test_no_candidates_when_all_have_descriptions(tmp_path):
    store = _store(tmp_path)
    svc = DefinitionService(store)
    _seed(store, "x", metadata_json=json.dumps({"description": "d"}))
    res = await backfill_descriptions(store, svc, _FakeLLM(), apply=True)
    assert res["proposals"] == [] and res["written"] == 0 and res["errors"] == []
