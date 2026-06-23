import json

from foreman.client.core.context_compression import context_pack_to_text, normalize_context_pack


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
