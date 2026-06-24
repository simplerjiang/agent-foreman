"""Provider-agnostic LLM client.

Used by PM Brain / Reviewer / Briefing — with YOUR API key (config.llm + .env).
NOT used by claude/codex CLIs, which authenticate themselves.

Transports (config.llm.transport):
  - "http" (default):
      provider "openai"    -> POST {base_url}/chat/completions
      provider "anthropic" -> POST {base_url}/messages
  - "ws": the Responses API over a WebSocket (CLIProxyAPI `GET {base_url}/responses` upgrade).
      Send a `response.create` frame with the prompt as Responses `input` items, then accumulate
      `response.output_text.delta` events until `response.completed`. The result is the same plain
      assistant text `complete()` returns on the HTTP path, so callers are transport-agnostic.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from ..config import Config


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMToolResponse:
    text: str
    tool_calls: list[LLMToolCall]


class LLMConfigError(RuntimeError):
    """Raised before a request when the PM brain is not configured enough to call."""


StreamCallback = Callable[[dict], Awaitable[None] | None]


def _messages_to_responses_input(messages: list[Message]) -> tuple[str, list[dict]]:
    """Map chat messages onto the Responses API shape: system text → ``instructions``; each other
    turn → an ``input`` message item (user/system → input_text, assistant → output_text)."""
    instructions: list[str] = []
    items: list[dict] = []
    for m in messages:
        if m.role == "system":
            instructions.append(m.content)
            continue
        ctype = "output_text" if m.role == "assistant" else "input_text"
        items.append(
            {"type": "message", "role": m.role, "content": [{"type": ctype, "text": m.content}]}
        )
    return "\n".join(instructions), items


def _ws_url(base_url: str) -> str:
    """Derive the Responses WebSocket URL from the HTTP base_url (scheme swap + /responses)."""
    u = base_url
    if u.startswith("https://"):
        u = "wss://" + u[len("https://"):]
    elif u.startswith("http://"):
        u = "ws://" + u[len("http://"):]
    return u + "/responses"


def _default_ws_connect(url: str, headers: dict, timeout: float):
    """Open the real WebSocket. Lazy-imports ``websockets`` so the HTTP transport never needs it."""
    import websockets

    return websockets.connect(
        url, additional_headers=headers, open_timeout=timeout, max_size=None
    )


class LLMClient:
    def __init__(
        self,
        cfg: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_connect=None,
        settings_resolver: Callable[[], dict] | None = None,
    ) -> None:
        # Construction-time defaults from config. `settings_resolver` (optional) lets a runtime
        # settings page override provider/model/base_url/key WITHOUT a restart: it's called per
        # request and may return any subset of {"provider", "model", "base_url", "api_key"}.
        self.provider = cfg.llm.provider
        self.base_url = cfg.llm.base_url.rstrip("/")
        self.model = cfg.llm.model
        self.api_key = (cfg.secrets.llm_api_key or "").strip()
        self.max_tokens = cfg.llm.max_tokens
        self.reasoning_effort = (getattr(cfg.llm, "reasoning_effort", "") or "").strip().lower()
        self.timeout = cfg.llm.request_timeout_s
        self.mode = (cfg.llm.transport or "http").strip().lower()
        self._settings_resolver = settings_resolver
        # `transport` lets tests inject httpx.MockTransport (no real network / tokens spent).
        self._client = httpx.AsyncClient(timeout=cfg.llm.request_timeout_s, transport=transport)
        # `ws_connect(url, headers, timeout) -> async-context-manager` lets tests inject a fake socket.
        self._ws_connect = ws_connect or _default_ws_connect
        self._response_state: dict[str, str] = {}

    def _resolve(self, model_override: str = "") -> tuple[str, str, str]:
        """Effective (provider, base_url, model) for this request: a settings-page override (if any)
        wins over the config default. Transport stays config-only wiring."""
        provider, base_url, model = self.provider, self.base_url, self.model
        if self._settings_resolver is not None:
            try:
                ov = self._settings_resolver() or {}
            except Exception:  # noqa: BLE001 — a broken resolver must never break a request
                ov = {}
            provider = (ov.get("provider") or provider or "").strip() or provider
            base_url = ((ov.get("base_url") or base_url or "").strip() or base_url).rstrip("/")
            model = (ov.get("model") or model or "").strip() or model
        model = (model_override or "").strip() or model
        return provider, base_url, model

    def _api_key(self) -> str:
        key = self.api_key
        if self._settings_resolver is not None:
            try:
                ov = self._settings_resolver() or {}
            except Exception:  # noqa: BLE001 — a broken resolver must never leak/break defaults
                ov = {}
            if "api_key" in ov:
                key = (ov.get("api_key") or "").strip()
        if not key:
            raise LLMConfigError("missing FOREMAN_LLM_API_KEY")
        return key

    def _transport_mode(self) -> str:
        mode = self.mode
        if self._settings_resolver is not None:
            try:
                ov = self._settings_resolver() or {}
            except Exception:  # noqa: BLE001 - a broken resolver must never break defaults
                ov = {}
            override = str(ov.get("transport") or "").strip().lower()
            if override:
                mode = override
        return mode

    def _reasoning_effort(self) -> str:
        effort = self.reasoning_effort
        if self._settings_resolver is not None:
            try:
                ov = self._settings_resolver() or {}
            except Exception:  # noqa: BLE001 - a broken resolver must never break defaults
                ov = {}
            override = str(ov.get("reasoning_effort") or "").strip().lower()
            if override:
                effort = override
        return effort if effort in {"low", "medium", "high", "max"} else ""

    async def complete(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        model: str = "",
        on_stream: StreamCallback | None = None,
        state_key: str = "",
    ) -> str:
        """Return the assistant's text. Set json_mode=True to nudge structured JSON output.

        On the ws transport json_mode is a no-op (the Responses path has no response_format; callers
        already instruct the model to emit JSON and parse tolerantly)."""
        provider, base_url, model = self._resolve(model)
        if self._transport_mode() == "ws":
            return await self._responses_ws(
                messages, base_url, model, on_stream=on_stream, state_key=state_key
            )
        if provider == "anthropic":
            return await self._anthropic(messages, json_mode, base_url, model)
        return await self._openai(messages, json_mode, base_url, model, on_stream=on_stream)

    async def tool_complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict],
        json_mode: bool = False,
        model: str = "",
    ) -> LLMToolResponse:
        """Return assistant text plus provider-native tool calls when the transport supports them.

        The Responses WebSocket path currently returns plain text and lets the PM loop use its JSON
        fallback protocol. This keeps the existing WS stream contract small and avoids pretending
        tool results were acknowledged by the provider when Foreman owns the runtime execution.
        """
        provider, base_url, model = self._resolve(model)
        if self._transport_mode() == "ws":
            text = await self.complete(messages, json_mode=json_mode, model=model)
            return LLMToolResponse(text=text, tool_calls=[])
        if provider == "anthropic":
            return await self._anthropic_tools(messages, tools, base_url, model)
        return await self._openai_tools(messages, tools, json_mode, base_url, model)

    async def list_models(self) -> list[str]:
        """Return model ids from the configured PM provider's `/models` endpoint.

        The endpoint is used only to populate UI choices; callers should treat failures as optional
        and fall back to configured defaults. The API key still goes through `_api_key()`, so a
        missing key fails before any network request.
        """
        return [item["id"] for item in await self.list_model_infos()]

    async def list_model_infos(self) -> list[dict]:
        """Return model metadata from `/models`, preserving context-window fields when present."""
        provider, base_url, _model = self._resolve()
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        if provider == "anthropic":
            headers = {
                "x-api-key": self._api_key(),
                "anthropic-version": "2023-06-01",
            }
        r = await self._client.get(f"{base_url}/models", headers=headers)
        r.raise_for_status()
        return _model_infos(r.json())

    async def _openai(
        self,
        messages: list[Message],
        json_mode: bool,
        base_url: str,
        model: str,
        *,
        on_stream: StreamCallback | None = None,
    ) -> str:
        payload: dict = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        effort = self._reasoning_effort()
        if effort:
            payload["reasoning_effort"] = effort
        if on_stream is not None:
            payload["stream"] = True
            return await self._openai_stream(payload, base_url, on_stream)
        r = await self._client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=payload,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _openai_tools(
        self,
        messages: list[Message],
        tools: list[dict],
        json_mode: bool,
        base_url: str,
        model: str,
    ) -> LLMToolResponse:
        payload: dict = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "tools": [_openai_tool_schema(tool) for tool in tools],
            "tool_choice": "auto",
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        effort = self._reasoning_effort()
        if effort:
            payload["reasoning_effort"] = effort
        r = await self._client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=payload,
        )
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        calls: list[LLMToolCall] = []
        for item in msg.get("tool_calls") or []:
            if not isinstance(item, dict):
                continue
            raw_function = item.get("function")
            fn = raw_function if isinstance(raw_function, dict) else {}
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            calls.append(
                LLMToolCall(
                    id=str(item.get("id") or "").strip(),
                    name=name,
                    arguments=_json_args(fn.get("arguments")),
                )
            )
        return LLMToolResponse(text=msg.get("content") or "", tool_calls=calls)

    async def _openai_stream(
        self, payload: dict, base_url: str, on_stream: StreamCallback
    ) -> str:
        buf: list[str] = []
        async with self._client.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=payload,
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except (TypeError, ValueError):
                    continue
                for chunk in _openai_stream_chunks(obj):
                    if chunk["kind"] == "output":
                        buf.append(chunk["delta"])
                    await _call_stream(on_stream, chunk)
        return "".join(buf)

    async def _anthropic(
        self, messages: list[Message], json_mode: bool, base_url: str, model: str
    ) -> str:
        system = "\n".join(m.content for m in messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        payload: dict = {"model": model, "max_tokens": self.max_tokens, "messages": turns}
        if system:
            payload["system"] = system
        r = await self._client.post(
            f"{base_url}/messages",
            headers={"x-api-key": self._api_key(), "anthropic-version": "2023-06-01"},
            json=payload,
        )
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    async def _anthropic_tools(
        self, messages: list[Message], tools: list[dict], base_url: str, model: str
    ) -> LLMToolResponse:
        system = "\n".join(m.content for m in messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        payload: dict = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": turns,
            "tools": [_anthropic_tool_schema(tool) for tool in tools],
        }
        if system:
            payload["system"] = system
        r = await self._client.post(
            f"{base_url}/messages",
            headers={"x-api-key": self._api_key(), "anthropic-version": "2023-06-01"},
            json=payload,
        )
        r.raise_for_status()
        text_parts: list[str] = []
        calls: list[LLMToolCall] = []
        for block in r.json().get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                raw_input = block.get("input")
                arguments = raw_input if isinstance(raw_input, dict) else {}
                calls.append(
                    LLMToolCall(
                        id=str(block.get("id") or "").strip(),
                        name=str(block.get("name") or "").strip(),
                        arguments=arguments,
                    )
                )
        return LLMToolResponse(text="".join(text_parts), tool_calls=calls)

    async def _responses_ws(
        self,
        messages: list[Message],
        base_url: str,
        model: str,
        *,
        on_stream: StreamCallback | None = None,
        state_key: str = "",
    ) -> str:
        """Run one turn over the Responses-API WebSocket and return the accumulated assistant text.

        Sends a `response.create` frame; accumulates `response.output_text.delta` deltas; stops at
        `response.completed`; raises on an `error` frame or if the stream closes early. Each receive
        is bounded by the configured request timeout so a stalled upstream can't hang the loop."""
        previous_id = self._response_state.get(state_key, "") if state_key else ""
        try:
            return await self._responses_ws_once(
                messages, base_url, model, on_stream=on_stream, state_key=state_key,
                previous_response_id=previous_id,
            )
        except RuntimeError as exc:
            if not previous_id or "previous_response" not in str(exc).lower():
                raise
            self._response_state.pop(state_key, None)
            return await self._responses_ws_once(
                messages, base_url, model, on_stream=on_stream, state_key=state_key
            )

    async def _responses_ws_once(
        self,
        messages: list[Message],
        base_url: str,
        model: str,
        *,
        on_stream: StreamCallback | None = None,
        state_key: str = "",
        previous_response_id: str = "",
    ) -> str:
        instructions, items = _messages_to_responses_input(messages)
        request: dict = {
            "type": "response.create",
            "model": model,
            "stream": True,
            "store": False,
            "input": items,
        }
        reasoning = {"summary": "auto"}
        effort = self._reasoning_effort()
        if effort:
            reasoning["effort"] = effort
        request["reasoning"] = reasoning
        if previous_response_id:
            request["previous_response_id"] = previous_response_id
        if instructions:
            request["instructions"] = instructions
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        buf: list[str] = []
        reasoning_streamed = False
        response_id = ""
        async with self._ws_connect(_ws_url(base_url), headers, self.timeout) as ws:
            await ws.send(json.dumps(request))
            while True:
                raw = await asyncio.wait_for(ws.recv(), self.timeout)
                try:
                    obj = json.loads(raw)
                except (TypeError, ValueError):
                    continue  # ignore a non-JSON keepalive/marker frame
                etype = obj.get("type")
                response_id = _response_id(obj) or response_id
                chunk = _stream_chunk(obj)
                if chunk is not None and on_stream is not None:
                    if chunk["kind"] == "reasoning":
                        reasoning_streamed = True
                    await _call_stream(on_stream, chunk)
                if etype == "response.output_text.delta":
                    buf.append(str(obj.get("delta", "")))
                elif etype == "response.completed":
                    if on_stream is not None and not reasoning_streamed:
                        for text in _completed_reasoning_summaries(obj):
                            await _call_stream(
                                on_stream,
                                {
                                    "kind": "reasoning",
                                    "delta": text,
                                    "event_type": str(etype or ""),
                                },
                            )
                    break
                elif etype == "error":
                    raise RuntimeError(f"responses ws error: {obj.get('error')}")
        if state_key and response_id:
            self._response_state[state_key] = response_id
        return "".join(buf)

    async def aclose(self) -> None:
        await self._client.aclose()


async def _call_stream(callback: StreamCallback, chunk: dict) -> None:
    res = callback(chunk)
    if inspect.isawaitable(res):
        await res


def _stream_chunk(obj: dict) -> dict | None:
    etype = str(obj.get("type") or "")
    if etype == "response.output_text.delta":
        return {"kind": "output", "delta": str(obj.get("delta", "")), "event_type": etype}
    if "reasoning" not in etype.lower() or not etype.endswith(".delta"):
        return None
    delta = _string_part(obj.get("delta"))
    if not delta:
        delta = _string_part(obj.get("text") or obj.get("summary"))
    return {"kind": "reasoning", "delta": delta, "event_type": etype} if delta else None


def _response_id(obj: dict) -> str:
    response = obj.get("response")
    if isinstance(response, dict):
        rid = str(response.get("id") or "").strip()
        if rid:
            return rid
    rid = str(obj.get("response_id") or "").strip()
    if rid:
        return rid
    etype = str(obj.get("type") or "")
    if etype in {"response.created", "response.in_progress", "response.completed"}:
        return str(obj.get("id") or "").strip()
    return ""


def _openai_stream_chunks(obj: dict) -> list[dict]:
    out: list[dict] = []
    choices = obj.get("choices", [])
    if not isinstance(choices, list):
        return out
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        raw_delta = choice.get("delta")
        delta = raw_delta if isinstance(raw_delta, dict) else {}
        content = _string_part(delta.get("content"))
        if content:
            out.append({"kind": "output", "delta": content, "event_type": "chat.completion.chunk"})
        reasoning = (
            _string_part(delta.get("reasoning_content"))
            or _string_part(delta.get("reasoning"))
            or _string_part(delta.get("thinking"))
        )
        if reasoning:
            out.append(
                {"kind": "reasoning", "delta": reasoning, "event_type": "chat.completion.chunk"}
            )
        details = delta.get("reasoning_details") or choice.get("reasoning_details")
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                text = _string_part(detail.get("text") or detail.get("summary"))
                if text:
                    out.append(
                        {
                            "kind": "reasoning",
                            "delta": text,
                            "event_type": str(detail.get("type") or "reasoning_details"),
                        }
                    )
    return out


def _completed_reasoning_summaries(obj: dict) -> list[str]:
    response = obj.get("response")
    items = response.get("output", []) if isinstance(response, dict) else obj.get("output", [])
    out: list[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        text = _reasoning_summary_text(item.get("summary"))
        if text:
            out.append(text)
    return out


def _reasoning_summary_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _string_part(value.get("text") or value.get("summary"))
    if isinstance(value, list):
        parts = [_reasoning_summary_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()
    return ""


def _string_part(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _model_ids(data: object) -> list[str]:
    """Extract model ids from common OpenAI/Anthropic-compatible shapes."""
    return [str(item["id"]) for item in _model_infos(data)]


def _model_infos(data: object) -> list[dict[str, object]]:
    """Extract model ids and optional token-window metadata from common provider shapes."""
    if isinstance(data, dict):
        items = data.get("data", data.get("models", []))
    else:
        items = data
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return out
    for item in items:
        if isinstance(item, str):
            mid = item.strip()
            info: dict[str, object] = {"id": mid}
        elif isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or "").strip()
            info = {"id": mid}
            context_length = _positive_int(
                item.get("context_length")
                or item.get("contextLength")
                or item.get("context_window")
                or item.get("input_token_limit")
                or item.get("inputTokenLimit")
                or item.get("maxInputTokens")
            )
            max_tokens = _positive_int(
                item.get("max_completion_tokens")
                or item.get("max_tokens")
                or item.get("output_token_limit")
                or item.get("outputTokenLimit")
                or item.get("maxOutputTokens")
            )
            if context_length is not None:
                info["context_length"] = context_length
            if max_tokens is not None:
                info["max_tokens"] = max_tokens
        else:
            mid = ""
            info = {"id": mid}
        if mid and mid not in seen:
            seen.add(mid)
            out.append(info)
    return out


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        out = value
    elif isinstance(value, float):
        out = int(value)
    elif isinstance(value, str):
        try:
            out = int(value)
        except ValueError:
            return None
    else:
        return None
    return out if out > 0 else None


def _openai_tool_schema(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


def _anthropic_tool_schema(tool: dict) -> dict:
    return {
        "name": str(tool.get("name") or ""),
        "description": str(tool.get("description") or ""),
        "input_schema": tool.get("input_schema") or {"type": "object", "properties": {}},
    }


def _json_args(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            obj = json.loads(value)
            return obj if isinstance(obj, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}
