"""Tests for the provider-agnostic LLM client (TASKS T1.2).

Uses httpx.MockTransport so request construction + response parsing are verified
deterministically — no real network calls, no tokens spent. (pyproject sets
asyncio_mode=auto, so plain `async def` tests are collected.)
"""

from __future__ import annotations

import json

import httpx

from foreman.shared.config import Config
from foreman.shared.llm import LLMClient, Message


def _client(provider: str, captured: dict) -> LLMClient:
    cfg = Config()
    cfg.llm.provider = provider
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "test-model"
    cfg.secrets.llm_api_key = "secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["json"] = json.loads(request.content.decode())
        if provider == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "hi-anthropic"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi-openai"}}]})

    return LLMClient(cfg, transport=httpx.MockTransport(handler))


async def test_openai_request_and_parse():
    cap: dict = {}
    c = _client("openai", cap)
    out = await c.complete([Message("system", "sys"), Message("user", "hello")])
    await c.aclose()
    assert out == "hi-openai"
    assert str(cap["request"].url) == "https://example.test/v1/chat/completions"
    assert cap["request"].headers["authorization"] == "Bearer secret-key"
    assert cap["json"]["model"] == "test-model"
    assert cap["json"]["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


async def test_openai_json_mode_sets_response_format():
    cap: dict = {}
    c = _client("openai", cap)
    await c.complete([Message("user", "x")], json_mode=True)
    await c.aclose()
    assert cap["json"]["response_format"] == {"type": "json_object"}


async def test_anthropic_request_and_parse():
    cap: dict = {}
    c = _client("anthropic", cap)
    out = await c.complete([Message("system", "sys"), Message("user", "hello")])
    await c.aclose()
    assert out == "hi-anthropic"
    assert str(cap["request"].url) == "https://example.test/v1/messages"
    assert cap["request"].headers["x-api-key"] == "secret-key"
    assert cap["request"].headers["anthropic-version"] == "2023-06-01"
    # system is split out of messages; turns exclude system
    assert cap["json"]["system"] == "sys"
    assert cap["json"]["messages"] == [{"role": "user", "content": "hello"}]
