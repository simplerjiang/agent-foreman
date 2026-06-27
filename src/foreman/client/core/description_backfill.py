"""One-off LLM backfill of ``metadata.description`` for legacy definitions (P0 task 4b / D3).

Before the description-required gate (``definition_service._description_error``) can be relied on, any
pre-existing definitions in a user's local DB need a ``metadata.description`` — otherwise the resolver
(``work_mode_context``) silently excludes them from auto-selection (§4.3). The shipped seed examples
get descriptions deterministically from ``manifest.yaml``; **this module handles the legacy rows** by
asking the PM's own LLM to summarize each body into a ≤1024-char "what + when" description.

Safety / human-in-the-loop (§4.3 "人工抽检后写回"):
  - :func:`backfill_descriptions` defaults to **dry-run**: it returns the proposed descriptions and
    writes NOTHING, so a human can eyeball them first. Pass ``apply=True`` to persist.
  - Writes go through :class:`DefinitionService.update_definition`, so they pass the same gate, emit
    the same audit event, and preserve existing metadata keys (e.g. ``example``).
  - Stays client-side: the 秘方 body is decrypted locally and summarized via the user's own LLM; it
    never leaves the local process (§8.3/§14).
"""

from __future__ import annotations

import json
from typing import Any

from foreman.shared.llm import Message

from .definition_service import MAX_DESCRIPTION

# Bound the body we feed the summarizer so a giant paste can't blow up token cost — the opening of a
# definition is plenty to describe "what + when".
_MAX_BODY_INPUT_CHARS = 6000

_SUMMARIZE_SYSTEM = (
    "You write a single concise capability description for a 'work mode' (a skill, code standard, QA "
    "rubric, or workflow) that a PM agent uses to decide WHEN it is relevant to a task. "
    "Output ONE description of at most 1024 characters that states both WHAT it does AND WHEN to use "
    "it. Write in the SAME language as the body (Chinese if the body is Chinese). "
    "Return only the description text — no preamble, no markdown headings, no quotes."
)


async def summarize_to_description(
    llm: Any, body: str, *, kind: str = "", name: str = ""
) -> str:
    """Ask the LLM to summarize ``body`` into a ≤:data:`MAX_DESCRIPTION`-char "what + when"
    description. Returns the trimmed, length-capped text (possibly "" if the model returns nothing)."""
    snippet = (body or "")[:_MAX_BODY_INPUT_CHARS]
    header = f"kind: {kind}\nname: {name}\n\n" if (kind or name) else ""
    user = (
        f"{header}Summarize this work-mode definition into one description "
        f"(what it does + when to use it):\n\n{snippet}"
    )
    text = await llm.complete(
        [Message("system", _SUMMARIZE_SYSTEM), Message("user", user)]
    )
    return (text or "").strip()[:MAX_DESCRIPTION]


def _missing_description(metadata_json: str) -> bool:
    """True iff this row has no non-empty ``metadata.description`` (a backfill candidate)."""
    try:
        meta = json.loads(metadata_json or "{}")
    except (ValueError, TypeError):
        return True
    desc = meta.get("description") if isinstance(meta, dict) else None
    return not (isinstance(desc, str) and desc.strip())


def _merge_description(metadata_json: str, description: str) -> str:
    """Merge ``description`` into existing metadata, preserving other keys and stamping the schema."""
    try:
        meta = json.loads(metadata_json or "{}")
    except (ValueError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["schema"] = "foreman.workmode.meta/1"
    meta["description"] = description
    return json.dumps(meta, ensure_ascii=False)


async def backfill_descriptions(
    store: Any, service: Any, llm: Any, *, apply: bool = False
) -> dict:
    """Backfill ``metadata.description`` for every definition that lacks one.

    Args:
        store: the local Store (read the definition rows + their decrypted bodies).
        service: a :class:`DefinitionService` (writes go through its gated ``update_definition``).
        llm: anything with ``async complete(messages) -> str`` (the PM brain's LLMClient).
        apply: when False (default) **dry-run** — propose descriptions, write nothing (human review);
            when True, persist each proposal.

    Returns:
        ``{"apply": bool, "candidates": int, "proposals": [{id, kind, name, description}],
           "written": int, "errors": [{id, error}]}``.
    """
    rows = store.get_definitions() if hasattr(store, "get_definitions") else []
    proposals: list[dict] = []
    errors: list[dict] = []
    written = 0
    for row in rows:
        meta_json = getattr(row, "metadata_json", "{}")
        if not _missing_description(meta_json):
            continue
        try:
            description = await summarize_to_description(
                llm, getattr(row, "body", "") or "",
                kind=getattr(row, "kind", ""), name=getattr(row, "name", ""),
            )
        except Exception as e:  # an LLM failure on one row shouldn't abort the whole batch
            errors.append({"id": getattr(row, "id", ""), "error": str(e)})
            continue
        if not description:
            errors.append({"id": getattr(row, "id", ""), "error": "empty_summary"})
            continue
        proposals.append({
            "id": getattr(row, "id", ""), "kind": getattr(row, "kind", ""),
            "name": getattr(row, "name", ""), "description": description,
        })
        if apply:
            res = await service.update_definition(
                getattr(row, "id", ""),
                metadata_json=_merge_description(meta_json, description),
            )
            if res.get("ok"):
                written += 1
            else:
                errors.append({"id": getattr(row, "id", ""), "error": res.get("error", "write_failed")})
    return {
        "apply": apply,
        "candidates": len(proposals) + len([e for e in errors if e.get("error") != "write_failed"]),
        "proposals": proposals,
        "written": written,
        "errors": errors,
    }


__all__ = ["summarize_to_description", "backfill_descriptions"]
