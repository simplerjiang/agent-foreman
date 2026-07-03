from __future__ import annotations

import json

import httpx

from foreman.shared.config import Config
from foreman.shared.llm.client import LLMClient, LLMCompactUnsupported


def _cfg() -> Config:
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://llm.test/v1"
    cfg.llm.model = "gpt-test"
    cfg.secrets.llm_api_key = "test-key"
    return cfg


async def test_responses_compact_posts_payload():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"summary_json": {"summary": "remote summary"}})

    client = LLMClient(_cfg(), transport=httpx.MockTransport(handler))

    result = await client.responses_compact(
        [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ctx"}]}],
        instructions="compact",
        metadata={"session_id": "s1"},
    )

    assert seen["url"] == "https://llm.test/v1/responses/compact"
    assert seen["auth"] == "Bearer test-key"
    assert seen["payload"]["model"] == "gpt-test"
    assert seen["payload"]["instructions"] == "compact"
    assert seen["payload"]["metadata"] == {"session_id": "s1"}
    assert result["summary_json"]["summary"] == "remote summary"


async def test_responses_compact_unsupported_raises():
    client = LLMClient(
        _cfg(),
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json={"error": "nope"})),
    )

    try:
        await client.responses_compact([], instructions="compact")
    except LLMCompactUnsupported as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("expected LLMCompactUnsupported")
