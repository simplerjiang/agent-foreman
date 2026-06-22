"""Tests for the provider-agnostic LLM client (TASKS T1.2).

Uses httpx.MockTransport so request construction + response parsing are verified
deterministically — no real network calls, no tokens spent. (pyproject sets
asyncio_mode=auto, so plain `async def` tests are collected.)
"""

from __future__ import annotations

import json

import httpx
import pytest

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


async def test_settings_resolver_overrides_model_and_base_url():
    # A runtime settings page (config_kv) can switch model/base_url WITHOUT restarting — the resolver
    # is consulted per request (DESIGN §15 PM brain settings).
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://config.test/v1"
    cfg.llm.model = "config-model"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    override = {"model": "runtime-model", "base_url": "https://runtime.test/v2"}
    c = LLMClient(
        cfg, transport=httpx.MockTransport(handler), settings_resolver=lambda: override
    )
    await c.complete([Message("user", "hi")])
    await c.aclose()
    assert cap["url"] == "https://runtime.test/v2/chat/completions"
    assert cap["json"]["model"] == "runtime-model"


async def test_settings_resolver_falls_back_to_config_on_empty():
    # An empty/blank override leaves the config default in place (never blanks the model/url).
    cfg = Config()
    cfg.llm.base_url = "https://config.test/v1"
    cfg.llm.model = "config-model"
    cfg.secrets.llm_api_key = "k"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = LLMClient(
        cfg, transport=httpx.MockTransport(handler),
        settings_resolver=lambda: {"model": "", "base_url": ""},
    )
    await c.complete([Message("user", "hi")])
    await c.aclose()
    assert cap["json"]["model"] == "config-model"


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


# ── ws transport: Responses API over WebSocket ───────────────────────────────────────────────────
class _FakeWS:
    """A scripted WebSocket: records what was sent, replays `frames` on each recv()."""

    def __init__(self, frames, sent):
        self._frames = list(frames)
        self._sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data):
        self._sent.append(data)

    async def recv(self):
        if not self._frames:
            raise AssertionError("recv past end of script")
        return self._frames.pop(0)


def _ws_client(frames, sent):
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "gpt-5.5"
    cfg.llm.transport = "ws"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def connect(url, headers, timeout):
        cap.update(url=url, headers=headers, timeout=timeout)
        return _FakeWS(frames, sent)

    return LLMClient(cfg, ws_connect=connect), cap


async def test_ws_responses_accumulates_deltas_and_builds_request():
    sent: list = []
    frames = [
        json.dumps({"type": "codex.rate_limits"}),                       # info frame, ignored
        json.dumps({"type": "response.created"}),
        json.dumps({"type": "response.output_text.delta", "delta": "He"}),
        json.dumps({"type": "response.output_text.delta", "delta": "llo"}),
        json.dumps({"type": "response.completed"}),
    ]
    c, cap = _ws_client(frames, sent)
    out = await c.complete([Message("system", "be terse"), Message("user", "hi")])
    await c.aclose()
    assert out == "Hello"                                                # deltas accumulated
    assert cap["url"] == "wss://example.test/v1/responses"              # http base -> wss /responses
    assert cap["headers"]["Authorization"] == "Bearer secret-key"
    req = json.loads(sent[0])                                            # the response.create frame
    assert req["type"] == "response.create" and req["model"] == "gpt-5.5" and req["stream"] is True
    assert req["instructions"] == "be terse"                            # system -> instructions
    assert req["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


async def test_ws_responses_raises_on_error_frame():
    c, _ = _ws_client([json.dumps({"type": "error", "error": {"message": "bad"}})], [])
    with pytest.raises(RuntimeError):
        await c.complete([Message("user", "x")])
    await c.aclose()


async def test_ws_responses_ignores_non_json_marker():
    frames = [
        "[DONE]",                                                       # non-JSON keepalive/marker
        json.dumps({"type": "response.output_text.delta", "delta": "ok"}),
        json.dumps({"type": "response.completed"}),
    ]
    c, _ = _ws_client(frames, [])
    out = await c.complete([Message("user", "x")])
    await c.aclose()
    assert out == "ok"
