"""Tolerant extraction of the first JSON object from an LLM reply.

LLMs wrap JSON in prose or ```json fences, and — when they fall into a
repetition loop — *concatenate* many copies of the same object back-to-back.
The naive "first ``{`` … last ``}``" slice then spans every copy at once,
fails to parse, and silently degrades callers to a fallback (this is exactly
what hung a PM planning session: 47 copies parsed as one → empty plan).

`first_json_object` instead scans for the *first* balanced, parseable
top-level object, honouring string literals and backslash escapes so braces
inside string values never miscount. With ``validate`` it skips objects that
don't yet satisfy a predicate — used by the streaming "early-cut" to stop a
stream the moment a structurally complete *and* field-valid object arrives.
"""

from __future__ import annotations

import json
from typing import Callable, Iterator


def _strip_fence(text: str) -> str:
    """Drop a leading ```/```json fence line and any trailing fence."""
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if "```" in text:
            text = text[: text.rfind("```")]
        text = text.strip()
    return text


def _iter_objects(text: str) -> Iterator[dict]:
    """Yield each balanced, JSON-parseable top-level object, in order.

    Scanning respects double-quoted strings and backslash escapes, so braces
    inside string values are ignored. A region that closes but doesn't parse
    as an object (e.g. stray prose braces) is skipped; the first ``{`` that
    never closes stops the scan (an incomplete tail — e.g. mid-stream).
    """
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        escaped = False
        end = -1
        for j in range(i, n):
            c = text[j]
            if in_str:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            return  # unbalanced — incomplete tail, nothing more to harvest
        try:
            obj = json.loads(text[i : end + 1])
        except (TypeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            yield obj
            i = end + 1
        else:
            i += 1  # balanced but not an object (prose braces) — keep scanning


def first_json_object(
    text: str | None,
    *,
    validate: Callable[[dict], bool] | None = None,
) -> dict | None:
    """Return the first balanced top-level JSON object in ``text``, or ``None``.

    Without ``validate``, a clean single JSON value is honoured as-is: a
    top-level object is returned; a top-level non-object (list/str/number)
    yields ``None`` — matching the historical ``_extract_json_object``
    contract. Concatenated or prose-wrapped objects fall through to a
    string-aware scan that returns the *first* parseable object.

    With ``validate``, the whole-text fast path is skipped and the scan
    returns the first object for which ``validate(obj)`` is truthy (others are
    skipped), so a streaming caller can cut as soon as a complete, field-valid
    object appears.
    """
    s = (text or "").strip()
    if not s:
        return None
    if validate is None:
        try:
            obj = json.loads(_strip_fence(s))
        except (TypeError, ValueError):
            pass
        else:
            return obj if isinstance(obj, dict) else None
    for obj in _iter_objects(s):
        if validate is None or validate(obj):
            return obj
    return None
