"""Tests for the tolerant first-JSON-object early-cut (shared/jsonscan.py)."""

import json

from foreman.shared.jsonscan import first_json_object


def test_empty_and_none_return_none():
    assert first_json_object(None) is None
    assert first_json_object("") is None
    assert first_json_object("   \n  ") is None


def test_clean_single_object():
    assert first_json_object('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_top_level_non_object_yields_none():
    # Preserve historical _extract_json_object contract: a top-level list/str
    # is not an object → None (no digging into list elements on the fast path).
    assert first_json_object("[1, 2, 3]") is None
    assert first_json_object('"just a string"') is None
    assert first_json_object("42") is None


def test_code_fence_is_unwrapped():
    assert first_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert first_json_object('```\n{"a": 1}\n```') == {"a": 1}


def test_prose_around_object():
    assert first_json_object('Here is the plan: {"a": 1} — done!') == {"a": 1}
    assert first_json_object('Sure!\n{"agent": "codex"}\nThanks') == {"agent": "codex"}


def test_concatenated_objects_returns_first_only():
    one = {"summary": "plan", "agent": "codex", "ready": True}
    blob = (json.dumps(one) + "\n") * 47  # the 47x repetition-loop fault
    assert first_json_object(blob) == one


def test_nested_object_kept_whole():
    obj = {"a": 1, "nested": {"b": {"c": 2}}, "d": 3}
    assert first_json_object(json.dumps(obj) + " trailing junk }") == obj


def test_braces_inside_strings_do_not_miscount():
    obj = {"instruction": "use the {placeholder} and close } brace", "n": 1}
    assert first_json_object(json.dumps(obj)) == obj


def test_escaped_quotes_inside_strings():
    obj = {"instruction": 'say \\"hi\\" then {x}', "n": 2}
    raw = json.dumps(obj)
    assert first_json_object(raw) == obj


def test_prose_brace_then_valid_object():
    # A stray non-JSON brace before the real object must be skipped over.
    assert first_json_object('note {todo} then {"a": 1}') == {"a": 1}


def test_incomplete_object_midstream_returns_none():
    # No balanced close yet (still streaming) → nothing to harvest.
    assert first_json_object('{"a": 1, "b": ') is None
    assert first_json_object('{"a": {"b": 1}') is None


def test_validate_skips_non_matching_objects():
    blob = '{"type": "tool_call"} {"type": "final_plan", "agent": "codex"}'
    got = first_json_object(blob, validate=lambda o: o.get("type") == "final_plan")
    assert got == {"type": "final_plan", "agent": "codex"}


def test_validate_returns_none_when_nothing_matches():
    blob = '{"type": "a"} {"type": "b"}'
    assert first_json_object(blob, validate=lambda o: o.get("type") == "z") is None
