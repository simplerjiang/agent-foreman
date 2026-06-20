"""Tests for the 秘方 store helpers — definitions / definition_links / workflow_runs (TASKS T5.1).

DESIGN §11.2 / §7.1: one `definitions` table holds all four kinds (workflow|skill|code_standard|
qa_rubric); links wire a workflow step to the blocks it uses; workflow_runs track per-session
progress. These live ONLY in the local store. Uses a tmp_path sqlite FILE (not :memory:).
"""

from __future__ import annotations

import pytest

from foreman.client.store import Store
from foreman.client.store.models import Definition, DefinitionLink, WorkflowRun


def _store(tmp_path) -> Store:
    st = Store(str(tmp_path / "t.db"))
    st.init()
    return st


def _defn(id_: str, *, kind="skill", name="how-to-test", version=1, **kw) -> Definition:
    return Definition(id=id_, kind=kind, name=name, version=version, **kw)


# ── definitions ──────────────────────────────────────────────────────────────────────────────
def test_definition_roundtrip_stamps_timestamps(tmp_path):
    st = _store(tmp_path)
    row = st.add_definition(_defn("d1", body="# write tests first", scope_json='{"lang":"py"}'))
    assert row.created_at and row.updated_at == row.created_at
    got = st.get_definition("d1")
    assert got is not None
    assert got.kind == "skill" and got.name == "how-to-test"
    assert got.body == "# write tests first"


def test_add_definition_rejects_duplicate_kind_name_version(tmp_path):
    st = _store(tmp_path)
    st.add_definition(_defn("d1", version=1))
    with pytest.raises(ValueError, match="exists"):
        st.add_definition(_defn("d2", version=1))  # same kind/name/version
    # bumping the version is allowed
    st.add_definition(_defn("d2", version=2))
    # a different kind with the same name/version is allowed (uniqueness is per-kind)
    st.add_definition(_defn("d3", kind="qa_rubric", version=1))
    assert len(st.get_definitions(name="how-to-test")) == 3


def test_get_definitions_filters_and_orders(tmp_path):
    st = _store(tmp_path)
    st.add_definition(_defn("w1", kind="workflow", name="add-feature"))
    st.add_definition(_defn("s2", kind="skill", name="how-to-test", version=2))
    st.add_definition(_defn("s1", kind="skill", name="how-to-test", version=1))

    skills = st.get_definitions(kind="skill")
    assert [d.id for d in skills] == ["s1", "s2"]  # ordered by version within (kind, name)
    assert [d.id for d in st.get_definitions(kind="workflow")] == ["w1"]
    assert len(st.get_definitions()) == 3


def test_set_definition_active_makes_exactly_one_live(tmp_path):
    st = _store(tmp_path)
    st.add_definition(_defn("s1", version=1))
    st.add_definition(_defn("s2", version=2))
    st.add_definition(_defn("s3", version=3))

    assert st.get_active_definition("skill", "how-to-test") is None  # none active yet

    activated = st.set_definition_active("s2")
    assert activated is not None and activated.is_active and activated.status == "active"
    assert st.get_active_definition("skill", "how-to-test").id == "s2"
    assert [d.id for d in st.get_definitions(kind="skill", active_only=True)] == ["s2"]

    # switching the active version clears the previous one — only ever one live
    st.set_definition_active("s3")
    assert st.get_active_definition("skill", "how-to-test").id == "s3"
    assert st.get_definition("s2").is_active is False
    assert [d.id for d in st.get_definitions(active_only=True)] == ["s3"]


def test_set_definition_active_scoped_per_name(tmp_path):
    st = _store(tmp_path)
    st.add_definition(_defn("a1", kind="skill", name="alpha", version=1))
    st.add_definition(_defn("b1", kind="skill", name="beta", version=1))
    st.set_definition_active("a1")
    st.set_definition_active("b1")
    # activating beta must NOT deactivate alpha — they're different building blocks
    assert st.get_active_definition("skill", "alpha").id == "a1"
    assert st.get_active_definition("skill", "beta").id == "b1"


def test_set_definition_active_unknown_id(tmp_path):
    st = _store(tmp_path)
    assert st.set_definition_active("nope") is None


def test_update_definition_partial_and_bumps_updated_at(tmp_path):
    st = _store(tmp_path)
    row = st.add_definition(_defn("d1", body="old", status="draft"))
    created = row.created_at

    updated = st.update_definition("d1", body="new body", status="active")
    assert updated is not None
    assert updated.body == "new body" and updated.status == "active"
    assert updated.scope_json == "{}"  # untouched field preserved
    assert updated.created_at == created  # identity preserved
    assert updated.updated_at >= created


def test_update_definition_unknown_id(tmp_path):
    st = _store(tmp_path)
    assert st.update_definition("nope", body="x") is None


# ── definition_links ─────────────────────────────────────────────────────────────────────────
def test_definition_links_roundtrip_filter_and_order(tmp_path):
    st = _store(tmp_path)
    st.add_definition(_defn("wf", kind="workflow", name="add-feature"))
    st.add_definition_link(
        DefinitionLink(id="l2", from_id="wf", to_id="s1", relation="uses_skill", step_index=1)
    )
    st.add_definition_link(
        DefinitionLink(id="l1", from_id="wf", to_id="std1", relation="uses_standard", step_index=0)
    )
    st.add_definition_link(
        DefinitionLink(id="l3", from_id="wf", to_id="qa1", relation="judged_by", step_index=1)
    )
    # a link belonging to another workflow must not leak in
    st.add_definition_link(
        DefinitionLink(id="lx", from_id="other", to_id="s1", relation="uses_skill", step_index=0)
    )

    links = st.get_definition_links("wf")
    assert [link.id for link in links] == ["l1", "l3", "l2"]  # by step_index, then relation
    assert [link.id for link in st.get_definition_links("wf", step_index=1)] == ["l3", "l2"]
    assert [link.id for link in st.get_definition_links("wf", relation="uses_skill")] == ["l2"]


def test_delete_definition_link(tmp_path):
    st = _store(tmp_path)
    st.add_definition_link(
        DefinitionLink(id="l1", from_id="wf", to_id="s1", relation="uses_skill", step_index=0)
    )
    st.delete_definition_link("l1")
    assert st.get_definition_links("wf") == []
    st.delete_definition_link("l1")  # idempotent no-op


# ── workflow_runs ────────────────────────────────────────────────────────────────────────────
def test_workflow_run_roundtrip_stamps_started_at(tmp_path):
    st = _store(tmp_path)
    run = st.add_workflow_run(WorkflowRun(id="r1", session_id="s1", workflow_id="wf"))
    assert run.started_at and run.step_status == "pending"
    assert st.get_workflow_run("r1").workflow_id == "wf"


def test_get_workflow_runs_filters_by_session_ordered_by_step(tmp_path):
    st = _store(tmp_path)
    st.add_workflow_run(WorkflowRun(id="r2", session_id="s1", workflow_id="wf", step_index=1))
    st.add_workflow_run(WorkflowRun(id="r1", session_id="s1", workflow_id="wf", step_index=0))
    st.add_workflow_run(WorkflowRun(id="rx", session_id="s2", workflow_id="wf", step_index=0))

    runs = st.get_workflow_runs("s1")
    assert [r.id for r in runs] == ["r1", "r2"]  # ordered by step_index
    assert len(st.get_workflow_runs("s2")) == 1


def test_update_workflow_run_partial(tmp_path):
    st = _store(tmp_path)
    st.add_workflow_run(WorkflowRun(id="r1", session_id="s1", workflow_id="wf"))
    updated = st.update_workflow_run("r1", step_status="running")
    assert updated is not None and updated.step_status == "running"
    assert updated.step_index == 0  # untouched

    done = st.update_workflow_run("r1", step_index=1, step_status="passed", ended_at="2026-01-01T00:00:00Z")
    assert done.step_index == 1 and done.step_status == "passed" and done.ended_at


def test_update_workflow_run_unknown_id(tmp_path):
    st = _store(tmp_path)
    assert st.update_workflow_run("nope", step_status="running") is None
