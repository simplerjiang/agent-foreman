import json

from foreman.client.core.context_compression import (
    context_pack_to_text,
    extract_json_object,
    normalize_context_pack,
    parse_context_pack,
)


def test_context_pack_budget_fallback_stays_valid_json():
    pack = normalize_context_pack(
        {
            "session_state": {"goal_quote": "ship auth", "summary": "s" * 4000},
            "working_memory": {
                "verified_facts": [
                    {"text": f"fact {i} " + ("x" * 200), "source_refs": [f"event:{i}"]}
                    for i in range(40)
                ]
            },
            "dynamic_tail": [{"text": "tail " + ("y" * 300)} for _ in range(20)],
        },
        goal="ship auth",
    )

    text = context_pack_to_text(pack, max_chars=1200)
    data = json.loads(text)

    assert data["session_state"]["goal_quote"] == "ship auth"
    assert data["omitted"]
    assert data["omitted"][-1]["reason"] == "storage_budget"


def test_context_pack_budget_keeps_constraints_and_verified_facts_first():
    pack = normalize_context_pack(
        {
            "session_state": {"goal_quote": "ship auth", "summary": "compact me"},
            "working_memory": {
                "verified_facts": [
                    {
                        "text": "pytest failed in test_auth.py",
                        "source_refs": ["event:test"],
                        "importance": 95,
                    }
                ],
                "constraints": [
                    {
                        "text": "Do not deploy unless the user explicitly asks.",
                        "source_refs": ["event:user"],
                        "importance": 100,
                    }
                ],
                "tests": [
                    {"text": f"low value test log {i} " + ("x" * 240), "importance": 5}
                    for i in range(30)
                ],
            },
            "dynamic_tail": [{"text": "tail " + ("y" * 240)} for _ in range(20)],
        },
        goal="ship auth",
    )

    text = context_pack_to_text(pack, max_chars=3500)
    data = json.loads(text)

    assert data["working_memory"]["constraints"][0]["text"].startswith("Do not deploy")
    assert data["working_memory"]["verified_facts"][0]["text"] == "pytest failed in test_auth.py"
    assert data["omitted"]


def test_extract_json_object_recovers_from_repetition_loop():
    # Regression for #39 (T0.6): a stalled compactor that repeats the SAME pack object N times
    # used to slice first "{" → last "}", concatenate them into invalid JSON, and drop the whole
    # pack to the conservative fallback. Early-cut now takes the FIRST complete object.
    one = {"session_state": {"goal_quote": "ship auth", "summary": "use codex"}}
    blob = (json.dumps(one, ensure_ascii=False) + "\n") * 47

    obj = extract_json_object(blob)
    assert obj == one

    pack = parse_context_pack(blob, goal="ship auth", timeline="raw events")
    # Not the fallback: the fallback injects a telltale open question.
    questions = [q.get("text", "") for q in pack["working_memory"]["open_questions"]]
    assert not any("fallback" in q.lower() for q in questions)
    assert pack["session_state"]["summary"] == "use codex"


def test_extract_json_object_unwraps_code_fence():
    # T0.6: compactor sometimes wraps the pack in a ```json fence; early-cut still finds it.
    fenced = '```json\n{"session_state": {"summary": "wrapped"}}\n```'
    assert extract_json_object(fenced) == {"session_state": {"summary": "wrapped"}}
