"""Tests for the provider-agnostic LLM client (TASKS T1.2).

Uses httpx.MockTransport so request construction + response parsing are verified
deterministically — no real network calls, no tokens spent. (pyproject sets
asyncio_mode=auto, so plain `async def` tests are collected.)
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from foreman.shared.config import Config
from foreman.shared.llm import LLMClient, LLMConfigError, LLMStalledError, Message


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


async def test_missing_api_key_fails_before_http_request():
    cfg = Config()
    cfg.llm.base_url = "https://example.test/v1"
    cfg.secrets.llm_api_key = "  "
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(LLMConfigError, match="FOREMAN_LLM_API_KEY"):
        await c.complete([Message("user", "hello")])
    await c.aclose()
    assert called is False


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


async def test_openai_tool_complete_builds_native_tool_request():
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "tool-model"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"README.md"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    out = await c.tool_complete(
        [Message("user", "inspect")],
        tools=[
            {
                "name": "read_file",
                "description": "Read file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ],
    )
    await c.aclose()

    assert cap["json"]["tools"][0]["type"] == "function"
    assert cap["json"]["tool_choice"] == "auto"
    assert out.tool_calls[0].name == "read_file"
    assert out.tool_calls[0].arguments == {"path": "README.md"}


async def test_anthropic_tool_complete_parses_tool_use():
    cfg = Config()
    cfg.llm.provider = "anthropic"
    cfg.llm.base_url = "https://anthropic.test/v1"
    cfg.llm.model = "claude"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "need file"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "list_files",
                        "input": {"path": "."},
                    },
                ]
            },
        )

    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    out = await c.tool_complete(
        [Message("system", "sys"), Message("user", "inspect")],
        tools=[{"name": "list_files", "description": "List", "input_schema": {"type": "object"}}],
    )
    await c.aclose()

    assert cap["json"]["tools"][0]["name"] == "list_files"
    assert out.text == "need file"
    assert out.tool_calls[0].arguments == {"path": "."}


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


async def test_complete_model_override_wins_for_one_request():
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://config.test/v1"
    cfg.llm.model = "config-model"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    await c.complete([Message("user", "hi")], model="dispatch-pm-model")
    await c.aclose()
    assert cap["json"]["model"] == "dispatch-pm-model"


async def test_settings_resolver_overrides_api_key_without_restart():
    cfg = Config()
    cfg.llm.base_url = "https://config.test/v1"
    cfg.llm.model = "config-model"
    cfg.secrets.llm_api_key = ""
    state = {"api_key": "runtime-key"}
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = LLMClient(cfg, transport=httpx.MockTransport(handler), settings_resolver=lambda: state)
    await c.complete([Message("user", "hi")])
    await c.aclose()
    assert cap["auth"] == "Bearer runtime-key"


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


async def test_list_models_openai_compatible():
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["url"] = str(request.url)
        cap["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-5", "context_length": 272000, "max_completion_tokens": 128000},
                    {"id": "gpt-5"},
                    {"id": "gpt-5-mini"},
                ]
            },
        )

    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    models = await c.list_models()
    infos = await c.list_model_infos()
    await c.aclose()
    assert cap == {"url": "https://example.test/v1/models", "auth": "Bearer secret-key"}
    assert models == ["gpt-5", "gpt-5-mini"]
    assert infos[0] == {"id": "gpt-5", "context_length": 272000, "max_tokens": 128000}


async def test_list_models_anthropic_shape():
    cfg = Config()
    cfg.llm.provider = "anthropic"
    cfg.llm.base_url = "https://api.anthropic.test/v1"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["key"] = request.headers["x-api-key"]
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-5"}]})

    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    models = await c.list_models()
    await c.aclose()
    assert cap["key"] == "secret-key"
    assert models == ["claude-sonnet-4-5"]


async def test_openai_json_mode_sets_response_format():
    cap: dict = {}
    c = _client("openai", cap)
    await c.complete([Message("user", "x")], json_mode=True)
    await c.aclose()
    assert cap["json"]["response_format"] == {"type": "json_object"}


async def test_openai_reasoning_effort_is_optional_and_configured():
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "reasoning-model"
    cfg.llm.reasoning_effort = "high"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    await c.complete([Message("user", "x")])
    await c.aclose()

    assert cap["json"]["reasoning_effort"] == "high"


async def test_openai_stream_callback_receives_output_and_reasoning():
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "stream-model"
    cfg.secrets.llm_api_key = "secret-key"
    cap: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["json"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            text="\n\n".join(
                [
                    'data: {"choices":[{"delta":{"reasoning_content":"think"}}]}',
                    'data: {"choices":[{"delta":{"content":"Hi"}}]}',
                    "data: [DONE]",
                ]
            ),
        )

    chunks: list[dict] = []
    c = LLMClient(cfg, transport=httpx.MockTransport(handler))
    out = await c.complete([Message("user", "x")], on_stream=lambda chunk: chunks.append(chunk))
    await c.aclose()

    assert cap["json"]["stream"] is True
    assert out == "Hi"
    assert [(c["kind"], c["delta"]) for c in chunks] == [
        ("reasoning", "think"),
        ("output", "Hi"),
    ]


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
        self.closed = False

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

    async def close(self):
        self.closed = True


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
    assert req["store"] is False
    assert req["reasoning"] == {"summary": "auto"}
    assert req["instructions"] == "be terse"                            # system -> instructions
    assert req["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


async def test_ws_responses_state_key_reuses_previous_response_id():
    sent: list[str] = []
    scripts = [
        [
            json.dumps({"type": "response.created", "response": {"id": "resp_1"}}),
            json.dumps({"type": "response.output_text.delta", "delta": "one"}),
            json.dumps({"type": "response.completed", "response": {"id": "resp_1"}}),
        ],
        [
            json.dumps({"type": "response.created", "response": {"id": "resp_2"}}),
            json.dumps({"type": "response.output_text.delta", "delta": "two"}),
            json.dumps({"type": "response.completed", "response": {"id": "resp_2"}}),
        ],
    ]

    def connect(_url, _headers, _timeout):
        return _FakeWS(scripts.pop(0), sent)

    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "gpt-5.5"
    cfg.llm.transport = "ws"
    cfg.secrets.llm_api_key = "secret-key"
    c = LLMClient(cfg, ws_connect=connect)

    assert await c.complete([Message("user", "first")], state_key="pm-review") == "one"
    assert await c.complete([Message("user", "delta")], state_key="pm-review") == "two"
    await c.aclose()

    first = json.loads(sent[0])
    second = json.loads(sent[1])
    assert "previous_response_id" not in first
    assert second["previous_response_id"] == "resp_1"
    assert second["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "delta"}]}
    ]


async def test_ws_responses_reasoning_effort_is_optional_and_configured():
    sent: list = []
    frames = [
        json.dumps({"type": "response.output_text.delta", "delta": "ok"}),
        json.dumps({"type": "response.completed"}),
    ]
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "gpt-5.5"
    cfg.llm.transport = "ws"
    cfg.llm.reasoning_effort = "max"
    cfg.secrets.llm_api_key = "secret-key"

    c = LLMClient(cfg, ws_connect=lambda _url, _headers, _timeout: _FakeWS(frames, sent))
    await c.complete([Message("user", "x")])
    await c.aclose()

    assert json.loads(sent[0])["reasoning"] == {"summary": "auto", "effort": "max"}


async def test_ws_responses_stream_callback_receives_output_and_reasoning():
    sent: list = []
    frames = [
        json.dumps({"type": "response.reasoning_summary_text.delta", "delta": "think"}),
        json.dumps({"type": "response.output_text.delta", "delta": "Hi"}),
        json.dumps({"type": "response.completed"}),
    ]
    chunks: list[dict] = []
    c, _ = _ws_client(frames, sent)
    out = await c.complete([Message("user", "x")], on_stream=lambda chunk: chunks.append(chunk))
    await c.aclose()

    assert out == "Hi"
    assert [(c["kind"], c["delta"]) for c in chunks] == [
        ("reasoning", "think"),
        ("output", "Hi"),
    ]


async def test_ws_tool_complete_forwards_stream_callback():
    sent: list = []
    frames = [
        json.dumps({"type": "response.output_text.delta", "delta": "plan"}),
        json.dumps({"type": "response.completed"}),
    ]
    chunks: list[dict] = []
    c, _ = _ws_client(frames, sent)
    out = await c.tool_complete(
        [Message("user", "x")],
        tools=[],
        on_stream=lambda chunk: chunks.append(chunk),
    )
    await c.aclose()

    assert out.text == "plan"
    assert out.tool_calls == []
    assert chunks == [
        {"kind": "output", "delta": "plan", "event_type": "response.output_text.delta"}
    ]


async def test_ws_tool_complete_sends_tools_choice_and_parses_function_call():
    sent: list = []
    frames = [
        json.dumps(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "get_weather",
                },
            }
        ),
        json.dumps({"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"'}),
        json.dumps({"type": "response.function_call_arguments.delta", "output_index": 1, "delta": 'city'}),
        json.dumps({"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '":"'}),
        json.dumps({"type": "response.function_call_arguments.delta", "output_index": 1, "delta": 'Paris'}),
        json.dumps({"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '"}'}),
        json.dumps({"type": "response.function_call_arguments.done", "output_index": 1}),
    ]
    c, _ = _ws_client(frames, sent)
    out = await c.tool_complete(
        [Message("user", "weather")],
        tools=[
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
        tool_choice={"type": "function", "name": "get_weather"},
    )
    await c.aclose()

    req = json.loads(sent[0])
    assert req["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    assert req["tool_choice"] == {"type": "function", "name": "get_weather"}
    assert out.text == ""
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].id == "call_1"
    assert out.tool_calls[0].name == "get_weather"
    assert out.tool_calls[0].arguments == {"city": "Paris"}


async def test_ws_tool_complete_waits_for_arguments_after_output_item_done():
    sent: list = []
    frames = [
        json.dumps(
            {
                "type": "response.output_item.done",
                "item_id": "1",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": "",
                },
            }
        ),
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "output_index": 1,
                "arguments": '{"city":"Paris"}',
            }
        ),
    ]
    c, _ = _ws_client(frames, sent)
    out = await c.tool_complete(
        [Message("user", "weather")],
        tools=[{"name": "get_weather", "description": "Get weather"}],
    )
    await c.aclose()

    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].arguments == {"city": "Paris"}


async def test_ws_tool_complete_accepts_arguments_before_output_item_done():
    sent: list = []
    frames = [
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "output_index": 1,
                "arguments": '{"city":"Paris"}',
            }
        ),
        json.dumps(
            {
                "type": "response.output_item.done",
                "item_id": "1",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "get_weather",
                },
            }
        ),
    ]
    c, _ = _ws_client(frames, sent)
    out = await c.tool_complete(
        [Message("user", "weather")],
        tools=[{"name": "get_weather", "description": "Get weather"}],
    )
    await c.aclose()

    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].arguments == {"city": "Paris"}


async def test_ws_tool_complete_ignores_empty_or_invalid_arguments_for_early_break():
    sent: list = []
    frames = [
        json.dumps(
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": "",
                },
            }
        ),
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "output_index": 1,
                "arguments": "{bad json",
            }
        ),
        json.dumps({"type": "response.output_text.delta", "delta": "still waiting"}),
        json.dumps({"type": "response.completed"}),
    ]
    c, _ = _ws_client(frames, sent)
    out = await c.tool_complete(
        [Message("user", "weather")],
        tools=[{"name": "get_weather", "description": "Get weather"}],
    )
    await c.aclose()

    assert out.text == "still waiting"
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].arguments == {}


async def test_ws_json_mode_repetition_watchdog_closes_stream():
    ws = _FakeWS(
        [
            json.dumps({"type": "response.output_text.delta", "delta": '{"ready":true}'}),
            json.dumps({"type": "response.output_text.delta", "delta": '{"ready":true}'}),
        ],
        [],
    )
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "gpt-5.5"
    cfg.llm.transport = "ws"
    cfg.secrets.llm_api_key = "secret-key"
    c = LLMClient(cfg, ws_connect=lambda _url, _headers, _timeout: ws)

    with pytest.raises(LLMStalledError, match="structured_repetition"):
        await c.complete([Message("user", "x")], json_mode=True)
    await c.aclose()

    assert ws.closed is True


async def test_ws_wall_clock_watchdog_aborts_never_completed_delta_stream():
    class SlowDeltaWS(_FakeWS):
        async def recv(self):
            await asyncio.sleep(0.01)
            return json.dumps({"type": "response.output_text.delta", "delta": "x"})

    ws = SlowDeltaWS([], [])
    cfg = Config()
    cfg.llm.provider = "openai"
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "gpt-5.5"
    cfg.llm.transport = "ws"
    cfg.secrets.llm_api_key = "secret-key"
    c = LLMClient(cfg, ws_connect=lambda _url, _headers, _timeout: ws)
    c.timeout = 0.03

    with pytest.raises(LLMStalledError, match="wall_clock_timeout"):
        await c.complete([Message("user", "x")])
    await c.aclose()

    assert ws.closed is True


async def test_settings_resolver_can_switch_transport_to_ws_after_construction():
    cfg = Config()
    cfg.llm.base_url = "https://example.test/v1"
    cfg.llm.model = "gpt-5.5"
    cfg.llm.transport = "http"
    cfg.secrets.llm_api_key = "secret-key"
    sent: list = []
    frames = [
        json.dumps({"type": "response.output_text.delta", "delta": "ok"}),
        json.dumps({"type": "response.completed"}),
    ]
    cap: dict = {}

    def connect(url, headers, timeout):
        cap.update(url=url, headers=headers, timeout=timeout)
        return _FakeWS(frames, sent)

    c = LLMClient(cfg, ws_connect=connect, settings_resolver=lambda: {"transport": "ws"})
    out = await c.complete([Message("user", "x")])
    await c.aclose()

    assert out == "ok"
    assert cap["url"] == "wss://example.test/v1/responses"


async def test_ws_responses_completed_frame_can_surface_reasoning_summary():
    frames = [
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {"type": "reasoning", "summary": [{"text": "summary"}]},
                        {"type": "message", "content": [{"type": "output_text", "text": "Done"}]},
                    ]
                },
            }
        ),
    ]
    chunks: list[dict] = []
    c, _ = _ws_client(frames, [])
    out = await c.complete([Message("user", "x")], on_stream=lambda chunk: chunks.append(chunk))
    await c.aclose()

    assert out == ""
    assert chunks == [
        {"kind": "reasoning", "delta": "summary", "event_type": "response.completed"}
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
