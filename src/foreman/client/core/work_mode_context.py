"""Work-mode selection funnel — the L0 metadata resolver (DESIGN §5, P0).

This is the engine behind progressive disclosure (渐进式披露). Given the active definitions
(workflow / skill / code_standard / qa_rubric), a task ``goal`` and the dispatch ``workspace`` /
``agent``, it produces a **L0 index** — a lightweight ``[{id, kind, name, description, est_tokens}]``
list that **never carries a body**. The full body is only ever pulled later, on demand, by the P1
``work_mode_get`` tool (PM channel) or written to ``.claude/skills`` files (coding-agent channel).

Three-step funnel (§5):
  1. **Hard filter (scope)** — keep only definitions applicable here: ``scope_json`` workspace
     prefixes (via :func:`_within_any`, Windows-safe — never a bare string prefix), agent, optional
     path globs. A definition with **no non-empty ``metadata.description``** is excluded fail-closed
     (§4.3: "无 description 不进自动选择" is implemented here as resolver exclusion, NOT a write-time
     reject — existing/imported rows stay readable). Manually selected ids pass straight through.
  2. **Lexical relevance (V1)** — score the survivors by keyword / name / description overlap with
     the goal; ``metadata.priority`` breaks ties. (P3 swaps in embeddings.)
  3. **Top-K truncation** — keep the best ``limit`` (default :data:`WORKMODE_MAX_SELECTED`) in
     ``selected``; the rest go to ``dropped`` (never silently discarded — the timeline shows "另有 N
     条未选中").

This module is **client-side core** and a **pure function**: it takes already-queried definition
rows and touches no store, no LLM, no files. P1 wires it into the PM tool-loop; P2 reuses its output
for the managed-block index. Keeping it pure makes the L0 contract independently unit-testable.
"""

from __future__ import annotations

import json
import math
import re
from fnmatch import fnmatch
from typing import Any

# Reuse the Windows-safe path-containment helper from the sibling dispatch service (same ``core``
# package — no ``tools → core`` back-dependency). It takes a ``str`` path and resolves with
# ``Path.resolve(strict=False)`` + ``is_relative_to`` so a bare string prefix can't mis-match across
# drives (DESIGN §5 / dispatch_service.py:_within_any).
from .dispatch_service import _within_any

# ── §8 budget constants (selection / index slice; full table in 90-conventions-and-glossary.md) ──
# Only the three selection/index-related constants live here in P0; the L1 pull / pulls-per-plan
# budget (WORKMODE_BODY_MAX_CHARS / WORKMODE_MAX_PULLS) lands with the P1 tool handler.
WORKMODE_MAX_SELECTED = 8         # top-K kept in the L0 index (Tool-RAG truncation)
WORKMODE_INDEX_DESC_CHARS = 200   # description truncation INSIDE the L0 index (< the 1024 storage cap)
WORKMODE_INDEX_MAX_TOKENS = 1500  # hard cap for the whole L0 index block (P1 enforces; P0 records)

# Rough token estimate when metadata carries no measured ``est_tokens`` (~4 chars/token, matching the
# repo's other approximations). The body never leaves this function — only its size becomes a number.
_CHARS_PER_TOKEN = 4

# Word tokenizer for lexical scoring: lowercase alphanumeric runs. CJK has no spaces, so we also fall
# back to per-character overlap for non-ASCII goals (see ``_tokenize``).
_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[一-鿿]")

# Relative weight of a keyword hit vs a name/description hit — keywords are the curated selection
# signal, so they count for more (RAG-MCP: curated metadata drives selection accuracy).
_KEYWORD_WEIGHT = 3
_NAME_WEIGHT = 2
_DESC_WEIGHT = 1


def _field(row: Any, attr: str, default: Any = None) -> Any:
    """Read ``attr`` from a Definition row that may be an object (``store.get_definitions``) OR a
    JSON-friendly dict (``list_definitions``). Keeps the resolver agnostic to its caller."""
    if isinstance(row, dict):
        return row.get(attr, default)
    return getattr(row, attr, default)


def _parse_obj(text: Any) -> dict:
    """Parse a JSON-object string to a dict; anything else → ``{}`` (scope/metadata are objects)."""
    if isinstance(text, dict):
        return text
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _as_list(value: Any) -> list[str]:
    """Coerce a scope/metadata field to a list of strings (accept a bare string or a list)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    return []


def _tokenize(text: str) -> set[str]:
    """Lowercase word set for lexical overlap. Adds single CJK chars so Chinese goals (no spaces)
    still score against Chinese keywords/descriptions."""
    if not text:
        return set()
    low = text.lower()
    tokens = set(_WORD_RE.findall(low))
    tokens.update(_CJK_RE.findall(low))
    return tokens


def _description_of(meta: dict) -> str:
    """The non-empty ``metadata.description`` (L0 selection signal), or "" if absent/blank."""
    desc = meta.get("description")
    return desc.strip() if isinstance(desc, str) and desc.strip() else ""


def _est_tokens(row: Any, meta: dict) -> int:
    """``metadata.est_tokens`` if it's a sane int, else ~ceil(len(body)/4). Only the COUNT is
    emitted — the body itself never enters the L0 index."""
    raw = meta.get("est_tokens")
    if isinstance(raw, bool):  # bool is an int subclass; reject it explicitly
        raw = None
    if isinstance(raw, int) and raw >= 0:
        return raw
    if isinstance(raw, float) and raw >= 0 and not math.isnan(raw):
        return int(raw)
    body = _field(row, "body", "") or ""
    return math.ceil(len(body) / _CHARS_PER_TOKEN) if body else 0


def _l0_entry(row: Any, meta: dict) -> dict:
    """One L0 index entry — EXACTLY ``{id, kind, name, description, est_tokens}``, never a body.
    The description is truncated to :data:`WORKMODE_INDEX_DESC_CHARS` so the index stays cheap."""
    return {
        "id": _field(row, "id", ""),
        "kind": _field(row, "kind", ""),
        "name": _field(row, "name", ""),
        "description": _description_of(meta)[:WORKMODE_INDEX_DESC_CHARS],
        "est_tokens": _est_tokens(row, meta),
    }


def _scope_ok(
    scope: dict, *, workspace: str | None, agent: str | None, path: str | None
) -> bool:
    """Hard applicability check (§5 step 1). A declared dimension that doesn't match excludes the
    definition; an absent dimension is permissive (global). Path/agent dimensions are skipped when
    the caller has no value to test them against (dispatch knows the workspace + agent, not the exact
    target files — those refine later)."""
    # workspace prefix: scope.workspaces / scope.workspace (a list of allowed roots).
    roots = _as_list(scope.get("workspaces")) or _as_list(scope.get("workspace"))
    if roots:
        if not workspace or not _within_any(workspace, roots):
            return False
    # agent allow-list: scope.agents / scope.agent.
    agents = _as_list(scope.get("agents")) or _as_list(scope.get("agent"))
    if agents and agent:
        if not any(agent.strip().lower() == a.strip().lower() for a in agents):
            return False
    # path globs: only enforced when the caller passes a concrete ``path`` to test.
    globs = _as_list(scope.get("paths"))
    if globs and path:
        if not any(fnmatch(path, g) for g in globs):
            return False
    return True


def _relevance(meta: dict, name: str, goal_tokens: set[str]) -> int:
    """Lexical overlap score (§5 step 2): weighted hits of goal tokens against keywords / name /
    description. Higher = more relevant. 0 when nothing overlaps (still selectable if it survives to
    a free top-K slot — relevance only orders the survivors)."""
    if not goal_tokens:
        return 0
    keyword_tokens: set[str] = set()
    for kw in _as_list(meta.get("keywords")):
        keyword_tokens |= _tokenize(kw)
    score = _KEYWORD_WEIGHT * len(goal_tokens & keyword_tokens)
    score += _NAME_WEIGHT * len(goal_tokens & _tokenize(name))
    score += _DESC_WEIGHT * len(goal_tokens & _tokenize(_description_of(meta)))
    return score


def _priority(meta: dict) -> int:
    """``metadata.priority`` as an int (tie-break; higher wins), 0 if absent/non-numeric."""
    raw = meta.get("priority")
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)) and not (isinstance(raw, float) and math.isnan(raw)):
        return int(raw)
    return 0


def resolve_work_mode_context(
    definitions: list,
    *,
    goal: str,
    workspace: str | None = None,
    agent: str | None = None,
    selected_ids: list[str] | None = None,
    kind: str | None = None,
    path: str | None = None,
    limit: int = WORKMODE_MAX_SELECTED,
) -> dict:
    """Run the three-step funnel and return the L0 index.

    Args:
        definitions: already-active Definition rows (the caller passes
            ``store.get_definitions(active_only=True)``). Objects or JSON-dicts both work.
        goal: the task goal — drives lexical relevance (§5 step 2).
        workspace / agent: dispatch context for the scope hard-filter (§5 step 1).
        selected_ids: ids the user **manually** picked in the composer — these bypass ranking and
            truncation and the no-description exclusion (an explicit pick is honored as-is, §5).
        kind: optional single-kind filter (e.g. only ``code_standard``).
        path: optional concrete path to test ``scope.paths`` globs against (dispatch has none).
        limit: top-K cap for AUTO candidates (default :data:`WORKMODE_MAX_SELECTED`).

    Returns:
        ``{"selected": [...], "dropped": [...]}`` where every entry is EXACTLY
        ``{id, kind, name, description, est_tokens}`` — **never a body**. ``dropped`` holds candidates
        that passed scope + had a description but lost the top-K cut (so the timeline can surface
        "另有 N 条未选中"); scope-rejected and description-less rows are simply absent.
    """
    selected_set = {s for s in (selected_ids or []) if s}
    manual: list[dict] = []
    manual_ids_seen: set[str] = set()
    auto_candidates: list[tuple[int, int, int, dict]] = []  # (score, priority, -order, entry)
    goal_tokens = _tokenize(goal or "")

    for order, row in enumerate(definitions or []):
        rid = _field(row, "id", "")
        row_kind = _field(row, "kind", "")
        if kind and row_kind != kind:
            # A manual pick of a filtered-out kind is still honored (explicit user intent).
            if rid not in selected_set:
                continue
        meta = _parse_obj(_field(row, "metadata_json", "{}"))

        # Manual pick: straight through — bypass scope, description gate, ranking AND truncation.
        if rid and rid in selected_set:
            if rid not in manual_ids_seen:
                manual.append(_l0_entry(row, meta))
                manual_ids_seen.add(rid)
            continue

        # Step 1 — hard scope filter.
        scope = _parse_obj(_field(row, "scope_json", "{}"))
        if not _scope_ok(scope, workspace=workspace, agent=agent, path=path):
            continue
        # Fail-closed: no non-empty description → excluded from auto-selection (§4.3).
        if not _description_of(meta):
            continue

        # Step 2 — lexical relevance (priority + stable order as tie-breakers).
        score = _relevance(meta, _field(row, "name", ""), goal_tokens)
        auto_candidates.append((score, _priority(meta), -order, _l0_entry(row, meta)))

    # Highest score first, then priority, then original order (stable).
    auto_candidates.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)

    cap = max(0, int(limit))
    auto_selected = [t[3] for t in auto_candidates[:cap]]
    dropped = [t[3] for t in auto_candidates[cap:]]

    # Manual picks lead (explicit user intent), then the auto top-K. De-dupe defensively so a manual
    # id never also appears via auto.
    selected = manual + [e for e in auto_selected if e["id"] not in manual_ids_seen]
    return {"selected": selected, "dropped": dropped}


__all__ = [
    "resolve_work_mode_context",
    "WORKMODE_MAX_SELECTED",
    "WORKMODE_INDEX_DESC_CHARS",
    "WORKMODE_INDEX_MAX_TOKENS",
]
