"""Tests for the work-mode selection funnel — ``resolve_work_mode_context`` (P0, DESIGN §5).

The resolver is a PURE function (no store / LLM / files), so it's unit-tested directly. These tests
pin the **L0 contract** that P1's tool-loop integration and P2's managed-block writer both depend on:
every selected/dropped entry is EXACTLY ``{id, kind, name, description, est_tokens}`` — never a body.
"""

from __future__ import annotations

import json

from foreman.client.core.work_mode_context import (
    WORKMODE_INDEX_DESC_CHARS,
    WORKMODE_MAX_SELECTED,
    resolve_work_mode_context,
)
from foreman.client.store.models import Definition

_L0_KEYS = {"id", "kind", "name", "description", "est_tokens"}


def _defn(
    name: str,
    *,
    kind: str = "skill",
    description: str | None = "does a thing; use when relevant",
    keywords: list[str] | None = None,
    priority: int | None = None,
    est_tokens: int | None = None,
    scope: dict | None = None,
    body: str = "BODY-SHOULD-NEVER-LEAK",
) -> Definition:
    meta: dict = {}
    if description is not None:
        meta["description"] = description
    if keywords is not None:
        meta["keywords"] = keywords
    if priority is not None:
        meta["priority"] = priority
    if est_tokens is not None:
        meta["est_tokens"] = est_tokens
    return Definition(
        id=f"id-{name}",
        kind=kind,
        name=name,
        version=1,
        status="active",
        is_active=True,
        scope_json=json.dumps(scope or {}),
        body=body,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )


def _names(entries: list[dict]) -> list[str]:
    return [e["name"] for e in entries]


# ── L0 contract: no body ever leaks ───────────────────────────────────────────────────────────────
def test_l0_entries_have_exactly_the_five_keys_no_body():
    defs = [_defn("a"), _defn("b", description=None), _defn("c")]
    out = resolve_work_mode_context(defs, goal="anything")
    for bucket in (out["selected"], out["dropped"]):
        for entry in bucket:
            assert set(entry.keys()) == _L0_KEYS
            assert "body" not in entry
            assert "BODY-SHOULD-NEVER-LEAK" not in json.dumps(entry)


# ── scope hard filter: workspace prefix via _within_any (real paths, OS-correct) ──────────────────
def test_scope_workspace_prefix_hit_and_miss(tmp_path):
    proj = tmp_path / "proj"
    sub = proj / "pkg"
    sub.mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()

    scoped = _defn("scoped", scope={"workspaces": [str(proj)]})

    # A workspace nested under the declared root → kept.
    inside = resolve_work_mode_context([scoped], goal="x", workspace=str(sub))
    assert "scoped" in _names(inside["selected"])

    # A sibling workspace that merely shares a string prefix is NOT under the root → dropped out.
    outside = resolve_work_mode_context([scoped], goal="x", workspace=str(other))
    assert "scoped" not in _names(outside["selected"])
    assert "scoped" not in _names(outside["dropped"])  # scope-rejected, not a top-K casualty


def test_scope_absent_is_global():
    glob = _defn("global", scope={})
    out = resolve_work_mode_context([glob], goal="x", workspace="E:/anywhere")
    assert "global" in _names(out["selected"])


# ── scope hard filter: agent allow-list ───────────────────────────────────────────────────────────
def test_scope_agent_allowlist():
    only_codex = _defn("codex-only", scope={"agents": ["codex"]})
    kept = resolve_work_mode_context([only_codex], goal="x", agent="codex")
    assert "codex-only" in _names(kept["selected"])
    dropped = resolve_work_mode_context([only_codex], goal="x", agent="claude-code")
    assert "codex-only" not in _names(dropped["selected"])


# ── no description → excluded from auto-selection (fail-closed, §4.3) ──────────────────────────────
def test_blank_description_excluded_from_auto():
    has = _defn("has-desc", description="real description here")
    blank = _defn("blank", description="   ")
    missing = _defn("missing", description=None)
    out = resolve_work_mode_context([has, blank, missing], goal="x")
    names = _names(out["selected"]) + _names(out["dropped"])
    assert "has-desc" in names
    assert "blank" not in names
    assert "missing" not in names


# ── ranking + top-K + priority tie-break ──────────────────────────────────────────────────────────
def test_relevance_orders_and_top_k_truncates():
    relevant = _defn("migrate-db", keywords=["migration", "database"],
                     description="run database migrations")
    irrelevant = [_defn(f"noise-{i}", keywords=["unrelated"], description="something else")
                  for i in range(WORKMODE_MAX_SELECTED + 2)]
    out = resolve_work_mode_context([*irrelevant, relevant], goal="please run a database migration")
    # Best match leads.
    assert out["selected"][0]["name"] == "migrate-db"
    # Exactly top-K selected, the rest dropped (never silently lost).
    # 11 candidates (WORKMODE_MAX_SELECTED + 2 noise + 1 relevant) → 8 selected, 3 dropped.
    assert len(out["selected"]) == WORKMODE_MAX_SELECTED
    assert len(out["dropped"]) == 3
    assert len(out["selected"]) + len(out["dropped"]) == WORKMODE_MAX_SELECTED + 3


def test_priority_breaks_ties():
    low = _defn("low", keywords=["build"], description="d", priority=0)
    high = _defn("high", keywords=["build"], description="d", priority=10)
    out = resolve_work_mode_context([low, high], goal="build it", limit=2)
    assert out["selected"][0]["name"] == "high"


# ── manual selection passes straight through (bypass scope / description / ranking / top-K) ────────
def test_manual_selected_ids_bypass_everything():
    # Out of scope, no description, and we ask for a tiny limit — manual pick still gets in.
    picked = _defn("picked", description=None, scope={"agents": ["nobody"]})
    fillers = [_defn(f"f-{i}", keywords=["fill"], description="filler") for i in range(10)]
    out = resolve_work_mode_context(
        [picked, *fillers], goal="fill", agent="claude-code",
        selected_ids=["id-picked"], limit=1,
    )
    assert "picked" in _names(out["selected"])
    # Manual lead does not consume the auto top-K budget.
    assert len(out["selected"]) == 2  # the manual pick + 1 auto (limit=1)


# ── description truncated inside the index (storage cap 1024 not carried in) ───────────────────────
def test_index_description_truncated():
    long_desc = "x" * 900
    out = resolve_work_mode_context([_defn("long", description=long_desc)], goal="x")
    entry = out["selected"][0]
    assert len(entry["description"]) == WORKMODE_INDEX_DESC_CHARS
    assert entry["description"] == "x" * WORKMODE_INDEX_DESC_CHARS


# ── est_tokens: measured metadata wins, else ~len(body)/4 ─────────────────────────────────────────
def test_est_tokens_from_metadata_or_body():
    measured = _defn("measured", est_tokens=1234)
    computed = _defn("computed", est_tokens=None, body="a" * 400)
    out = resolve_work_mode_context([measured, computed], goal="x")
    by_name = {e["name"]: e for e in out["selected"]}
    assert by_name["measured"]["est_tokens"] == 1234
    assert by_name["computed"]["est_tokens"] == 100  # 400 chars / 4


# ── kind filter ───────────────────────────────────────────────────────────────────────────────────
def test_kind_filter():
    skill = _defn("a-skill", kind="skill")
    std = _defn("a-standard", kind="code_standard")
    out = resolve_work_mode_context([skill, std], goal="x", kind="code_standard")
    names = _names(out["selected"])
    assert "a-standard" in names and "a-skill" not in names


# ── accepts JSON-dict rows (list_definitions shape), not just objects ──────────────────────────────
def test_accepts_dict_rows():
    row = {
        "id": "id-dict", "kind": "skill", "name": "dicty",
        "scope_json": "{}", "metadata_json": json.dumps({"description": "from a dict"}),
        "body": "ignored",
    }
    out = resolve_work_mode_context([row], goal="x")
    assert _names(out["selected"]) == ["dicty"]
    assert out["selected"][0]["description"] == "from a dict"
