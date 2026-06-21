"""Tests for the built-in starter 秘方 examples (T6.4, DESIGN §11.2C / §765).

The repo ships a tiny, generic, **redacted** set of example definitions so OSS users can start from
a working, editable library (foreman.db itself never enters git). These tests verify the shipped
files load, are internally consistent (the example workflow's referenced blocks all resolve), seed
idempotently into a real Store, and carry no secrets.
"""

from __future__ import annotations

import json

import pytest

from foreman.client.core.definition_service import KNOWN_KINDS
from foreman.client.core.examples import _safe_rel, load_example_definitions, seed_examples
from foreman.client.core.workflow_engine import parse_workflow
from foreman.client.store import Store


def _store(tmp_path) -> Store:
    st = Store(str(tmp_path / "t.db"))
    st.init()
    return st


# ── load + shape ─────────────────────────────────────────────────────────────────────────────
def test_load_examples_returns_all_four_kinds():
    examples = load_example_definitions()
    kinds = {e.kind for e in examples}
    # The starter set demonstrates every building-block kind so a new user sees one of each.
    assert kinds == set(KNOWN_KINDS)
    assert len(examples) >= 4


def test_every_example_is_well_formed():
    for e in load_example_definitions():
        assert e.kind in KNOWN_KINDS
        assert e.name and e.name.strip() == e.name
        assert e.body.strip(), f"{e.kind}/{e.name} has an empty body"
        # scope/metadata must be JSON objects (the store + readers assume that, §7.1).
        assert isinstance(json.loads(e.scope_json), dict)
        meta = json.loads(e.metadata_json)
        assert isinstance(meta, dict) and meta.get("example") is True


def test_example_names_are_unique_per_kind():
    seen = set()
    for e in load_example_definitions():
        key = (e.kind, e.name)
        assert key not in seen, f"duplicate example {key}"
        seen.add(key)


# ── internal consistency: the workflow's referenced blocks all ship ────────────────────────────
def test_add_feature_workflow_parses_with_a_gate():
    examples = {(e.kind, e.name): e for e in load_example_definitions()}
    wf = examples[("workflow", "add-feature")]
    spec = parse_workflow(wf.body, name=wf.name)
    assert spec.error == "" and spec.steps
    # The DESIGN §11.2 example ends with an approval gate before push.
    assert spec.steps[-1].approval is True
    assert any(not s.approval for s in spec.steps)  # and has real work steps too


def test_workflow_references_only_shipped_blocks():
    examples = load_example_definitions()
    by_kind: dict[str, set[str]] = {}
    for e in examples:
        by_kind.setdefault(e.kind, set()).add(e.name)
    wf = next(e for e in examples if e.kind == "workflow" and e.name == "add-feature")
    spec = parse_workflow(wf.body, name=wf.name)
    for step in spec.steps:
        for skill in step.skills:
            assert skill in by_kind.get("skill", set()), f"missing skill {skill}"
        for std in step.standards:
            assert std in by_kind.get("code_standard", set()), f"missing standard {std}"
        if step.qa:
            assert step.qa in by_kind.get("qa_rubric", set()), f"missing rubric {step.qa}"


# ── seeding into a real Store ──────────────────────────────────────────────────────────────────
def test_seed_examples_inserts_and_activates(tmp_path):
    st = _store(tmp_path)
    result = seed_examples(st)
    assert result["skipped"] == []
    assert len(result["added"]) == len(load_example_definitions())
    # every example is now the active version of its (kind, name)
    for e in load_example_definitions():
        active = st.get_active_definition(e.kind, e.name)
        assert active is not None
        assert active.is_active and active.body == e.body


def test_seed_examples_is_idempotent(tmp_path):
    st = _store(tmp_path)
    first = seed_examples(st)
    second = seed_examples(st)
    assert second["added"] == []
    assert set(second["skipped"]) == set(first["added"])
    # no duplicate rows were created on the second pass
    assert len(st.get_definitions()) == len(first["added"])


def test_seed_examples_no_activate(tmp_path):
    st = _store(tmp_path)
    seed_examples(st, activate=False)
    # rows exist but none is the live version
    assert st.get_definitions()
    for e in load_example_definitions():
        assert st.get_active_definition(e.kind, e.name) is None


def test_seed_examples_idempotent_even_without_activate(tmp_path):
    """The skip guard keys off existence, not activation — so re-running with activate=False (which
    never activates anything) must still seed nothing the second time, not duplicate every row."""
    st = _store(tmp_path)
    seed_examples(st, activate=False)
    n = len(st.get_definitions())
    second = seed_examples(st, activate=False)
    assert second["added"] == []
    assert len(st.get_definitions()) == n  # no v2 duplicates


def test_seed_examples_skips_user_owned_name(tmp_path):
    """If the user already has a definition with an example's (kind, name), seeding leaves it alone."""
    from foreman.client.store.models import Definition

    st = _store(tmp_path)
    mine = Definition(id="mine", kind="skill", name="write-tests", version=1, body="my own version")
    st.add_definition(mine)
    st.set_definition_active("mine")
    seed_examples(st)
    # the user's row is untouched and still the only write-tests skill
    rows = st.get_definitions(kind="skill", name="write-tests")
    assert [r.id for r in rows] == ["mine"]
    assert st.get_active_definition("skill", "write-tests").body == "my own version"


def test_seeded_workflow_resolves_all_material(tmp_path):
    """End-to-end: after seeding, the engine resolves the add-feature workflow's per-step material
    with NO missing blocks — the shipped set is self-contained and runnable."""
    from foreman.client.core.workflow_engine import WorkflowEngine

    st = _store(tmp_path)
    seed_examples(st)
    engine = WorkflowEngine(st)
    row, spec = engine.load("add-feature")
    assert row is not None and not spec.error
    for i in range(len(spec.steps)):
        material = engine._resolve_material(row.id, spec, i)
        assert material["missing"] == [], f"step {i} has unresolved blocks: {material['missing']}"


# ── redaction: no secrets in the shipped bodies ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "rel,ok",
    [
        ("skill/write-tests.md", True),
        ("manifest.yaml", True),
        ("../secret.txt", False),
        ("skill/../../etc/passwd", False),
        ("/etc/passwd", False),
        ("C:/Windows/system32", False),
        ("skill\\evil", False),
        ("", False),
    ],
)
def test_safe_rel_rejects_traversal(rel, ok):
    assert _safe_rel(rel) is ok


@pytest.mark.parametrize(
    "needle",
    [
        "sk-",                 # OpenAI-style key prefix
        "ghp_",                # GitHub token prefix
        "BEGIN PRIVATE KEY",   # PEM material
        "password=",
        "api_key=",
        "AKIA",                # AWS access key id prefix
    ],
)
def test_examples_carry_no_secrets(needle):
    for e in load_example_definitions():
        assert needle not in e.body, f"{e.kind}/{e.name} looks like it leaks a secret ({needle})"
