"""Structured context compaction helpers.

The raw event log remains the source of truth. A ContextPack is only a derived,
rebuildable view used to seed the next LLM call with high-signal context.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Iterable, cast


CONTEXT_PACK_VERSION = 1
DEFAULT_CONTEXT_BUDGET_CHARS = 12000

MEMORY_FIELDS = (
    "verified_facts",
    "claims",
    "decisions",
    "constraints",
    "open_questions",
    "risks",
    "next_steps",
    "files",
    "commands",
    "tests",
)

EVICT_MEMORY_FIELDS = (
    "tests",
    "commands",
    "files",
    "next_steps",
    "risks",
    "open_questions",
    "claims",
    "decisions",
)

FIELD_STATUS = {
    "verified_facts": "verified",
    "claims": "claimed",
    "decisions": "verified",
    "constraints": "verified",
    "open_questions": "unknown",
    "risks": "unknown",
    "next_steps": "unknown",
    "files": "unknown",
    "commands": "unknown",
    "tests": "unknown",
}

FIELD_KIND = {
    "verified_facts": "fact",
    "claims": "fact",
    "decisions": "decision",
    "constraints": "constraint",
    "open_questions": "question",
    "risks": "risk",
    "next_steps": "todo",
    "files": "file_note",
    "commands": "command",
    "tests": "test_result",
}


def extract_json_object(raw: str) -> dict | None:
    """Extract a JSON object from a model reply."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if "```" in text:
            text = text[: text.rfind("```")]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (TypeError, ValueError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (TypeError, ValueError):
            return None
    return None


def parse_context_pack(
    raw: str,
    *,
    goal: str,
    timeline: str,
    existing_context: str = "",
) -> dict:
    """Parse model output into a normalized ContextPack, falling back conservatively."""
    obj = extract_json_object(raw)
    if obj is None:
        return fallback_context_pack(goal, timeline, existing_context=existing_context)
    return normalize_context_pack(obj, goal=goal)


def normalize_context_pack(obj: dict, *, goal: str = "") -> dict:
    """Coerce a partial/loose object into the ContextPack v1 shape."""
    session_state = _as_dict(obj.get("session_state"))
    working = _as_dict(obj.get("working_memory"))

    # Also accept the flat schema used in the research note.
    for field in ("summary", "goal_quote"):
        if field in obj and field not in session_state:
            session_state[field] = _as_str(obj.get(field))
    for field in MEMORY_FIELDS:
        if field in obj and field not in working:
            working[field] = obj.get(field)

    session_state = {
        "goal_quote": _as_str(session_state.get("goal_quote")) or goal.strip(),
        "summary": _as_str(session_state.get("summary")),
        "status": _as_str(session_state.get("status")) or "unknown",
        "current_step": _as_str(session_state.get("current_step")),
    }
    normalized_working = {field: _as_items(working.get(field)) for field in MEMORY_FIELDS}
    pack = {
        "version": CONTEXT_PACK_VERSION,
        "stable_prefix": {
            "format": "context_pack_v1",
            "rule": (
                "Use this pack as derived context only. Raw events remain the source of truth."
            ),
        },
        "session_state": session_state,
        "working_memory": normalized_working,
        "retrieved_evidence": _as_items(obj.get("retrieved_evidence")),
        "dynamic_tail": _as_items(obj.get("dynamic_tail")),
        "omitted": _as_items(obj.get("omitted")),
    }
    return pack


def fallback_context_pack(goal: str, timeline: str, *, existing_context: str = "") -> dict:
    """Build a deterministic pack when the compactor model cannot return valid JSON."""
    lines = [line for line in (timeline or "").splitlines() if line.strip()]
    tail = lines[-12:]
    pack = normalize_context_pack(
        {
            "session_state": {
                "goal_quote": goal.strip(),
                "summary": _tail_excerpt(timeline, 800),
                "status": "unknown",
            },
            "working_memory": {
                "open_questions": [
                    {
                        "text": "Compactor fallback was used; verify raw events before relying on this context.",
                        "status": "unknown",
                    }
                ]
            },
            "dynamic_tail": [{"text": line, "source_refs": _refs_from_line(line)} for line in tail],
            "omitted": _fallback_omitted(timeline, existing_context),
        },
        goal=goal,
    )
    if existing_context:
        pack["retrieved_evidence"].append(
            {
                "kind": "prior_context",
                "text": _tail_excerpt(existing_context, 1200),
                "source_refs": [],
            }
        )
    return pack


def context_pack_to_text(pack: dict, *, max_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS) -> str:
    """Render a pack as stable JSON and bound it without silently hiding omissions."""
    data = normalize_context_pack(pack, goal=_pack_goal(pack))
    text = _dump(data)
    if len(text) <= max_chars:
        return text

    data = copy.deepcopy(data)
    protected_memory = {
        "constraints": _top_memory(data, "constraints", 3),
        "verified_facts": _top_memory(data, "verified_facts", 3),
    }
    omitted = data.setdefault("omitted", [])
    for section in ("dynamic_tail", "retrieved_evidence"):
        items = data.get(section)
        while isinstance(items, list) and items and len(_dump(data)) > max_chars:
            removed = items.pop(0)
            omitted.append(
                {
                    "kind": section,
                    "reason": "context_budget",
                    "source_refs": _item_refs(removed),
                    "text": _as_str(_as_dict(removed).get("text"))[:160],
                }
            )
    for field in EVICT_MEMORY_FIELDS:
        items = data.get("working_memory", {}).get(field)
        while isinstance(items, list) and items and len(_dump(data)) > max_chars:
            removed = _pop_lowest_importance(items)
            omitted.append(
                {
                    "kind": field,
                    "reason": "context_budget",
                    "source_refs": _item_refs(removed),
                    "text": _as_str(_as_dict(removed).get("text"))[:160],
                }
            )

    # Protect-core on the MAIN path (§8B.3): the top-3 constraints + verified_facts must survive
    # eviction even if EVICT_MEMORY_FIELDS is later changed to include them. Today they survive only
    # because they're absent from EVICT_MEMORY_FIELDS — this guard makes the guarantee explicit.
    working = data.setdefault("working_memory", {})
    for field, pinned in protected_memory.items():
        current = working.get(field) or []
        present = {_as_str(_as_dict(it).get("text")) for it in current}
        for item in pinned:
            if _as_str(_as_dict(item).get("text")) not in present:
                current.insert(0, item)
        working[field] = current

    text = _dump(data)
    if len(text) <= max_chars:
        return text
    while len(data.get("omitted", [])) > 8 and len(_dump(data)) > max_chars:
        data["omitted"].pop(0)
    text = _dump(data)
    if len(text) <= max_chars:
        return text
    minimal = normalize_context_pack(
        {
            "session_state": {
                "goal_quote": data["session_state"]["goal_quote"][:500],
                "summary": data["session_state"]["summary"][:800],
                "status": data["session_state"]["status"],
                "current_step": data["session_state"]["current_step"],
            },
            "working_memory": {
                "constraints": protected_memory["constraints"],
                "verified_facts": protected_memory["verified_facts"],
            },
            "omitted": data.get("omitted", [])[-20:]
            + [{"kind": "context_pack", "reason": "storage_budget", "source_refs": []}],
        },
        goal=data["session_state"]["goal_quote"],
    )
    text = _dump(minimal)
    if len(text) <= max_chars:
        return text
    minimal["omitted"] = [
        {"kind": "context_pack", "reason": "storage_budget", "source_refs": []}
    ]
    minimal["session_state"]["summary"] = ""
    return _dump(minimal)


def memory_items_from_pack(pack: dict) -> list[dict]:
    """Derive MemoryItem-compatible dicts from a ContextPack."""
    data = normalize_context_pack(pack, goal=_pack_goal(pack))
    out: list[dict] = []
    working = data["working_memory"]
    for field in MEMORY_FIELDS:
        for item in working.get(field, []):
            text = _as_str(item.get("text"))
            if not text:
                continue
            out.append(
                {
                    "kind": FIELD_KIND[field],
                    "text": text,
                    "status": _as_str(item.get("status")) or FIELD_STATUS[field],
                    "importance": _as_int(item.get("importance"), 50),
                    "source_refs": _as_str_list(item.get("source_refs")),
                    "tags": _as_str_list(item.get("tags")),
                    "confidence": _as_int(item.get("confidence"), 50),
                    "valid_from": _as_str(item.get("valid_from")),
                    "valid_until": _as_str(item.get("valid_until")),
                    "supersedes": _as_str(item.get("supersedes")),
                    "superseded_by": _as_str(item.get("superseded_by")),
                }
            )
    return out


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_items(value: object) -> list[dict]:
    if value is None:
        return []
    raw_items: Iterable[object] = value if isinstance(value, list) else [value]
    items: list[dict] = []
    for raw in raw_items:
        if isinstance(raw, dict):
            text = _as_str(raw.get("text"))
            source_refs = _as_str_list(raw.get("source_refs"))
            item = {str(k): v for k, v in raw.items()}
            item["text"] = text or json.dumps(raw, ensure_ascii=False)[:1000]
            item["source_refs"] = source_refs
            items.append(item)
        else:
            text = _as_str(raw)
            if text:
                items.append({"text": text, "source_refs": []})
    return items


def _as_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [text for text in (_as_str(v) for v in value) if text]
    text = _as_str(value)
    return [text] if text else []


def _as_int(value: object, default: int) -> int:
    try:
        return max(0, min(100, int(cast(Any, value))))
    except (TypeError, ValueError):
        return default


def _dump(data: dict) -> str:
    # sort_keys=True → deterministic bytes (same pack → same string), so a ContextPack can sit in the
    # KV-cache stable prefix without a key-order change silently busting the cache (§8B.4).
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _tail_excerpt(text: str, chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= chars:
        return text
    return "[head omitted; latest tail kept]\n" + text[-chars:]


def _refs_from_line(line: str) -> list[str]:
    if line.startswith("[event:") and "]" in line:
        return [line[1 : line.index("]")]]
    return []


def _fallback_omitted(timeline: str, existing_context: str) -> list[dict]:
    omitted: list[dict] = []
    if len(timeline or "") > 4000:
        omitted.append({"kind": "timeline", "reason": "fallback_tail_only", "source_refs": []})
    if len(existing_context or "") > 1200:
        omitted.append(
            {"kind": "prior_context", "reason": "fallback_tail_only", "source_refs": []}
        )
    return omitted


def _pack_goal(pack: dict) -> str:
    return _as_str(_as_dict(pack.get("session_state")).get("goal_quote"))


def _item_refs(item: object) -> list[str]:
    return _as_str_list(_as_dict(item).get("source_refs"))


def _top_memory(data: dict, field: str, limit: int) -> list[dict]:
    items = _as_items(_as_dict(data.get("working_memory")).get(field))
    items.sort(key=lambda item: _as_int(item.get("importance"), 50), reverse=True)
    return items[:limit]


def _pop_lowest_importance(items: list[dict]) -> dict:
    index = min(
        range(len(items)),
        key=lambda i: _as_int(_as_dict(items[i]).get("importance"), 50),
    )
    return items.pop(index)


__all__ = [
    "CONTEXT_PACK_VERSION",
    "DEFAULT_CONTEXT_BUDGET_CHARS",
    "context_pack_to_text",
    "extract_json_object",
    "fallback_context_pack",
    "memory_items_from_pack",
    "normalize_context_pack",
    "parse_context_pack",
]
