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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Config


_ANTHROPIC_DEFAULT_MAX_TOKENS = 2048


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


class LLMCompactUnsupported(RuntimeError):
    """Raised when the configured provider does not expose /responses/compact."""


class LLMStalledError(RuntimeError):
    """Raised when a streaming LLM turn is aborted by Foreman's call-level watchdog."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        message = reason if not detail else f"{reason}: {detail}"
        super().__init__(message)


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
        tracer: Any = None,
    ) -> None:
        # Construction-time defaults from config. `settings_resolver` (optional) lets a runtime
        # settings page override provider/model/base_url/key WITHOUT a restart: it's called per
        # request and may return any subset of {"provider", "model", "base_url", "api_key"}.
        self.provider = cfg.llm.provider
        self.base_url = cfg.llm.base_url.rstrip("/")
        self.model = cfg.llm.model
        self.api_key = (cfg.secrets.llm_api_key or "").strip()
        self.context_window_tokens = getattr(cfg.llm, "context_window_tokens", 272_000)
        self.max_tokens = _ANTHROPIC_DEFAULT_MAX_TOKENS
        self.reasoning_effort = (getattr(cfg.llm, "reasoning_effort", "") or "").strip().lower()
        self.timeout = cfg.llm.request_timeout_s
        self.mode = (cfg.llm.transport or "http").strip().lower()
        self._settings_resolver = settings_resolver
        # Optional debug tracer (work-mode P1b-trace, §8C). None = off, zero overhead. Records the
        # request/response at the two choke points below — never raises into a real call.
        self._tracer = tracer
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

    def _request_timeout(self) -> float:
        timeout: float = float(self.timeout or 0)
        if self._settings_resolver is not None:
            try:
                ov = self._settings_resolver() or {}
            except Exception:  # noqa: BLE001 - a broken resolver must never break defaults
                ov = {}
            raw = ov.get("request_timeout_s")
            if raw not in (None, ""):
                try:
                    timeout = float(str(raw).strip())
                except (TypeError, ValueError):
                    timeout = float(self.timeout or 0)
        return max(float(timeout or 0), 1.0)

    def runtime_context_window_tokens(self) -> int:
        return self._context_window_tokens()

    def runtime_max_tokens(self) -> int:
        return _positive_setting(self.max_tokens, _ANTHROPIC_DEFAULT_MAX_TOKENS)

    def _context_window_tokens(self) -> int:
        tokens = _positive_setting(self.context_window_tokens, 272_000)
        if self._settings_resolver is not None:
            try:
                ov = self._settings_resolver() or {}
            except Exception:  # noqa: BLE001 - a broken resolver must never break defaults
                ov = {}
            raw = ov.get("context_window_tokens")
            if raw not in (None, ""):
                tokens = _positive_setting(raw, tokens)
        return tokens

    def _max_tokens(self) -> int:
        # Anthropic requires a max_tokens field; OpenAI-compatible providers do not. Keep this as an
        # internal protocol default instead of a user/provider setting.
        return _positive_setting(self.max_tokens, _ANTHROPIC_DEFAULT_MAX_TOKENS)

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

        On the ws transport json_mode still does not request provider-side response_format, but it
        enables Foreman's structured repetition watchdog for JSON-shaped PM streams."""
        if self._tracer is None:
            return await self._complete_impl(
                messages, json_mode=json_mode, model=model, on_stream=on_stream, state_key=state_key
            )
        provider, _base, model_eff = self._resolve(model)
        t0 = time.perf_counter()
        err: str | None = None
        text = ""
        try:
            text = await self._complete_impl(
                messages, json_mode=json_mode, model=model, on_stream=on_stream, state_key=state_key
            )
            return text
        except Exception as exc:  # record failures too, then re-raise
            err = repr(exc)
            raise
        finally:
            self._tracer.record(
                kind="complete", provider=provider, model=model_eff,
                transport=self._transport_mode(), json_mode=json_mode, messages=messages,
                tools=None, response_text=text, tool_calls=None,
                latency_ms=(time.perf_counter() - t0) * 1000, error=err,
            )

    async def _complete_impl(
        self,
        messages: list[Message],
        *,
        json_mode: bool = False,
        model: str = "",
        on_stream: StreamCallback | None = None,
        state_key: str = "",
    ) -> str:
        provider, base_url, model = self._resolve(model)
        if self._transport_mode() == "ws":
            return await self._responses_ws(
                messages,
                base_url,
                model,
                json_mode=json_mode,
                on_stream=on_stream,
                state_key=state_key,
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
        on_stream: StreamCallback | None = None,
        tool_choice: object | None = "auto",
    ) -> LLMToolResponse:
        """Return assistant text plus provider-native tool calls when the transport supports them."""
        if self._tracer is None:
            return await self._tool_complete_impl(
                messages, tools=tools, json_mode=json_mode, model=model, on_stream=on_stream,
                tool_choice=tool_choice,
            )
        provider, _base, model_eff = self._resolve(model)
        t0 = time.perf_counter()
        err: str | None = None
        resp = LLMToolResponse(text="", tool_calls=[])
        try:
            resp = await self._tool_complete_impl(
                messages, tools=tools, json_mode=json_mode, model=model, on_stream=on_stream,
                tool_choice=tool_choice,
            )
            return resp
        except Exception as exc:
            err = repr(exc)
            raise
        finally:
            self._tracer.record(
                kind="tool_complete", provider=provider, model=model_eff,
                transport=self._transport_mode(), json_mode=json_mode, messages=messages,
                tools=tools, response_text=resp.text,
                tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments}
                            for c in resp.tool_calls],
                latency_ms=(time.perf_counter() - t0) * 1000, error=err,
            )

    async def _tool_complete_impl(
        self,
        messages: list[Message],
        *,
        tools: list[dict],
        json_mode: bool = False,
        model: str = "",
        on_stream: StreamCallback | None = None,
        tool_choice: object | None = "auto",
    ) -> LLMToolResponse:
        provider, base_url, model = self._resolve(model)
        if self._transport_mode() == "ws":
            return await self._responses_ws_tool(
                messages,
                base_url,
                model,
                tools=tools,
                tool_choice=tool_choice,
                json_mode=json_mode,
                on_stream=on_stream,
            )
        if provider == "anthropic":
            return await self._anthropic_tools(messages, tools, base_url, model)
        return await self._openai_tools(
            messages, tools, json_mode, base_url, model,
            tool_choice=tool_choice,
            on_stream=on_stream,
        )

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

    async def embed(self, texts: list[str], *, model: str = "") -> list[list[float]]:
        """One embedding vector per input text via the configured provider's /embeddings (work-mode
        P3 §3.2). Reuses _resolve()/_api_key()/self._client like complete(). Only the OpenAI-compatible
        shape is native; anthropic (no embeddings endpoint) raises so the caller can fall back to a
        local embedder. Empty input → []. Always plain HTTP POST (embeddings are transport-agnostic)."""
        if not texts:
            return []
        provider, base_url, _model = self._resolve(model)
        if provider == "anthropic":
            raise LLMConfigError("anthropic provider has no /embeddings; use a local fallback")
        emb_model = (model or "").strip() or self.model
        r = await self._client.post(
            f"{base_url}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json={"model": emb_model, "input": texts},
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        # Order by the per-item `index` (the OpenAI /embeddings contract — some proxies reorder); fall
        # back to response order when absent. Then index-align to the inputs so a vector can never be
        # paired with the wrong text.
        ordered = sorted(
            enumerate(data),
            key=lambda pair: pair[1].get("index", pair[0]) if isinstance(pair[1], dict) else pair[0],
        )
        vecs = [list(item.get("embedding") or []) if isinstance(item, dict) else []
                for _, item in ordered]
        return vecs[: len(texts)] if len(vecs) >= len(texts) else vecs + [[]] * (len(texts) - len(vecs))

    async def responses_compact(
        self,
        input_items,
        *,
        instructions: str = "",
        model: str = "",
        metadata: dict | None = None,
    ) -> dict:
        provider, base_url, model = self._resolve(model)
        if provider == "anthropic":
            raise LLMCompactUnsupported("anthropic provider has no /responses/compact")
        payload = {
            "model": model,
            "input": input_items,
            "instructions": instructions,
            "metadata": metadata or {},
        }
        r = await self._client.post(
            f"{base_url}/responses/compact",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=payload,
            timeout=self._request_timeout(),
        )
        if r.status_code in {404, 405}:
            raise LLMCompactUnsupported(f"/responses/compact unsupported: HTTP {r.status_code}")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise RuntimeError("responses_compact_invalid_response")
        error = data.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "").lower()
            message = str(error.get("message") or "")
            if "unsupported" in code or "not_found" in code or "not supported" in message.lower():
                raise LLMCompactUnsupported(message or "responses_compact_unsupported")
        return data

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
            timeout=self._request_timeout(),
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
        *,
        tool_choice: object | None = "auto",
        on_stream: StreamCallback | None = None,
    ) -> LLMToolResponse:
        payload: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "tools": [_openai_tool_schema(tool) for tool in tools],
        }
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        effort = self._reasoning_effort()
        if effort:
            payload["reasoning_effort"] = effort
        if on_stream is not None:
            payload["stream"] = True
            return await self._openai_tools_stream(payload, base_url, on_stream)
        r = await self._client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=payload,
            timeout=self._request_timeout(),
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

    async def _openai_tools_stream(
        self, payload: dict, base_url: str, on_stream: StreamCallback
    ) -> LLMToolResponse:
        buf: list[str] = []
        tool_items: dict[int, dict[str, Any]] = {}
        async with self._client.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=payload,
            timeout=self._request_timeout(),
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
                for choice in obj.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    raw_delta = choice.get("delta")
                    if not isinstance(raw_delta, dict):
                        continue
                    for raw in raw_delta.get("tool_calls") or []:
                        if not isinstance(raw, dict):
                            continue
                        try:
                            idx = int(raw.get("index") or 0)
                        except (TypeError, ValueError):
                            idx = 0
                        state = tool_items.setdefault(idx, {"id": "", "name": "", "arguments": []})
                        if raw.get("id"):
                            state["id"] = str(raw.get("id") or "")
                        raw_fn = raw.get("function")
                        fn = raw_fn if isinstance(raw_fn, dict) else {}
                        if fn.get("name"):
                            state["name"] = str(fn.get("name") or "")
                        if fn.get("arguments") is not None:
                            state["arguments"].append(str(fn.get("arguments") or ""))
        calls: list[LLMToolCall] = []
        for idx in sorted(tool_items):
            state = tool_items[idx]
            name = str(state.get("name") or "").strip()
            if not name:
                continue
            calls.append(
                LLMToolCall(
                    id=str(state.get("id") or f"call_{idx}"),
                    name=name,
                    arguments=_json_args("".join(state.get("arguments") or [])),
                )
            )
        return LLMToolResponse(text="".join(buf), tool_calls=calls)

    async def _openai_stream(
        self, payload: dict, base_url: str, on_stream: StreamCallback
    ) -> str:
        buf: list[str] = []
        async with self._client.stream(
            "POST",
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=payload,
            timeout=self._request_timeout(),
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
        payload: dict = {"model": model, "max_tokens": self._max_tokens(), "messages": turns}
        if system:
            payload["system"] = system
        r = await self._client.post(
            f"{base_url}/messages",
            headers={"x-api-key": self._api_key(), "anthropic-version": "2023-06-01"},
            json=payload,
            timeout=self._request_timeout(),
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
            "max_tokens": self._max_tokens(),
            "messages": turns,
            "tools": [_anthropic_tool_schema(tool) for tool in tools],
        }
        if system:
            payload["system"] = system
        r = await self._client.post(
            f"{base_url}/messages",
            headers={"x-api-key": self._api_key(), "anthropic-version": "2023-06-01"},
            json=payload,
            timeout=self._request_timeout(),
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
        json_mode: bool = False,
        on_stream: StreamCallback | None = None,
        state_key: str = "",
    ) -> str:
        """Run one turn over the Responses-API WebSocket and return the accumulated assistant text.

        The whole turn is bounded by a wall-clock watchdog. JSON-mode streams also get a small
        structured repetition detector so PM-style repeated objects fail loudly instead of hanging.
        """
        previous_id = self._response_state.get(state_key, "") if state_key else ""
        try:
            out = await self._responses_ws_once(
                messages,
                base_url,
                model,
                json_mode=json_mode,
                on_stream=on_stream,
                state_key=state_key,
                previous_response_id=previous_id,
            )
            return out.text
        except RuntimeError as exc:
            if not previous_id or "previous_response" not in str(exc).lower():
                raise
            self._response_state.pop(state_key, None)
            out = await self._responses_ws_once(
                messages,
                base_url,
                model,
                json_mode=json_mode,
                on_stream=on_stream,
                state_key=state_key,
            )
            return out.text

    async def _responses_ws_tool(
        self,
        messages: list[Message],
        base_url: str,
        model: str,
        *,
        tools: list[dict],
        tool_choice: object | None,
        json_mode: bool = False,
        on_stream: StreamCallback | None = None,
    ) -> LLMToolResponse:
        return await self._responses_ws_once(
            messages,
            base_url,
            model,
            tools=tools,
            tool_choice=tool_choice,
            json_mode=json_mode,
            on_stream=on_stream,
        )

    async def _responses_ws_once(
        self,
        messages: list[Message],
        base_url: str,
        model: str,
        *,
        tools: list[dict] | None = None,
        tool_choice: object | None = None,
        json_mode: bool = False,
        on_stream: StreamCallback | None = None,
        state_key: str = "",
        previous_response_id: str = "",
    ) -> LLMToolResponse:
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
        if tools:
            request["tools"] = [_responses_tool_schema(tool) for tool in tools]
            if tool_choice is not None:
                request["tool_choice"] = _responses_tool_choice(tool_choice)
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        buf: list[str] = []
        tool_items: dict[str, dict[str, Any]] = {}
        last_tool_key = ""
        reasoning_streamed = False
        response_id = ""
        wall_timeout = max(self._request_timeout(), 0.001)
        stall_timeout = min(30.0, max(15.0, wall_timeout / 2))
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_progress_at = started_at
        repeat_watch = _StructuredRepeatWatch() if json_mode else None
        async with self._ws_connect(_ws_url(base_url), headers, wall_timeout) as ws:
            await ws.send(json.dumps(request))
            try:
                while True:
                    timeout = _recv_timeout(
                        loop, started_at, last_progress_at, wall_timeout, stall_timeout
                    )
                    raw = await asyncio.wait_for(ws.recv(), timeout)
                    try:
                        obj = json.loads(raw)
                    except (TypeError, ValueError):
                        _check_watchdog(
                            loop, started_at, last_progress_at, wall_timeout, stall_timeout
                        )
                        continue  # ignore a non-JSON keepalive/marker frame
                    etype = str(obj.get("type") or "")
                    response_id = _response_id(obj) or response_id
                    chunk = _stream_chunk(obj)
                    made_progress = chunk is not None or _is_ws_progress_event(etype)
                    if chunk is not None and on_stream is not None:
                        if chunk["kind"] == "reasoning":
                            reasoning_streamed = True
                        await _call_stream(on_stream, chunk)
                    if etype == "response.output_text.delta":
                        delta = str(obj.get("delta", ""))
                        buf.append(delta)
                        if repeat_watch is not None:
                            reason = repeat_watch.feed(delta)
                            if reason:
                                raise LLMStalledError(
                                    reason, "stream emitted another complete JSON object"
                                )
                    elif etype == "response.output_item.added":
                        key = _merge_ws_tool_item(tool_items, obj, fallback=last_tool_key)
                        last_tool_key = key or last_tool_key
                    elif etype == "response.function_call_arguments.delta":
                        key = _ws_tool_key(obj, fallback=last_tool_key)
                        if key:
                            item = tool_items.setdefault(key, _new_ws_tool_state(key))
                            item["arguments_parts"].append(str(obj.get("delta") or ""))
                            last_tool_key = key
                    elif etype == "response.function_call_arguments.done":
                        key = _ws_tool_key(obj, fallback=last_tool_key)
                        if key:
                            item = tool_items.setdefault(key, _new_ws_tool_state(key))
                            if obj.get("arguments") is not None:
                                item["arguments"] = str(obj.get("arguments") or "")
                            item["done"] = True
                            last_tool_key = key
                        if tools and _ws_tool_calls_ready(tool_items):
                            break
                    elif etype == "response.output_item.done":
                        key = _merge_ws_tool_item(tool_items, obj, fallback=last_tool_key)
                        if key:
                            tool_items.setdefault(key, _new_ws_tool_state(key))["done"] = True
                            last_tool_key = key
                        if tools and _ws_tool_calls_ready(tool_items):
                            break
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
                    if made_progress:
                        last_progress_at = loop.time()
                    else:
                        _check_watchdog(
                            loop, started_at, last_progress_at, wall_timeout, stall_timeout
                        )
            except asyncio.TimeoutError as exc:
                now = loop.time()
                wall_left = wall_timeout - (now - started_at)
                stall_left = stall_timeout - (now - last_progress_at)
                reason = (
                    "wall_clock_timeout"
                    if now - started_at >= wall_timeout or wall_left <= stall_left
                    else "no_progress_timeout"
                )
                detail = f"responses ws did not finish within {wall_timeout:.1f}s"
                if reason == "no_progress_timeout":
                    detail = f"responses ws made no progress for {stall_timeout:.1f}s"
                await _close_ws(ws)
                raise LLMStalledError(reason, detail) from exc
            except LLMStalledError:
                await _close_ws(ws)
                raise
        if state_key and response_id:
            self._response_state[state_key] = response_id
        return LLMToolResponse(text="".join(buf), tool_calls=_ws_tool_calls(tool_items))

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


def _recv_timeout(
    loop: asyncio.AbstractEventLoop,
    started_at: float,
    last_progress_at: float,
    wall_timeout: float,
    stall_timeout: float,
) -> float:
    _check_watchdog(loop, started_at, last_progress_at, wall_timeout, stall_timeout)
    now = loop.time()
    wall_left = wall_timeout - (now - started_at)
    stall_left = stall_timeout - (now - last_progress_at)
    return max(0.001, min(wall_left, stall_left))


def _check_watchdog(
    loop: asyncio.AbstractEventLoop,
    started_at: float,
    last_progress_at: float,
    wall_timeout: float,
    stall_timeout: float,
) -> None:
    now = loop.time()
    if now - started_at >= wall_timeout:
        raise LLMStalledError(
            "wall_clock_timeout", f"responses ws did not finish within {wall_timeout:.1f}s"
        )
    if now - last_progress_at >= stall_timeout:
        raise LLMStalledError(
            "no_progress_timeout", f"responses ws made no progress for {stall_timeout:.1f}s"
        )


def _is_ws_progress_event(etype: str) -> bool:
    return etype in {
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
        "error",
    }


async def _close_ws(ws) -> None:
    close = getattr(ws, "close", None)
    if not callable(close):
        return
    try:
        res = close()
        if inspect.isawaitable(res):
            await res
    except Exception:  # noqa: BLE001 - watchdog close is best-effort cleanup
        return


class _StructuredRepeatWatch:
    """Detect a second complete top-level JSON object in JSON-mode streaming output."""

    def __init__(self) -> None:
        self._buf: list[str] = []
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._completed = False
        self._seen: set[str] = set()

    def feed(self, text: str) -> str:
        for ch in text:
            if self._depth == 0:
                if ch == "{":
                    if self._completed:
                        return "structured_repetition"
                    self._buf = [ch]
                    self._depth = 1
                    self._in_string = False
                    self._escape = False
                continue
            self._buf.append(ch)
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self._in_string = False
                continue
            if ch == '"':
                self._in_string = True
            elif ch == "{":
                self._depth += 1
            elif ch == "}":
                self._depth -= 1
                if self._depth == 0:
                    fingerprint = _json_fingerprint("".join(self._buf))
                    self._buf = []
                    if not fingerprint:
                        continue
                    if fingerprint in self._seen:
                        return "structured_repetition"
                    self._seen.add(fingerprint)
                    self._completed = True
        return ""


def _json_fingerprint(text: str) -> str:
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _responses_tool_schema(tool: dict) -> dict:
    if not isinstance(tool, dict):
        return {"type": "function", "name": "", "parameters": {"type": "object"}}
    if str(tool.get("type") or "") == "function" and str(tool.get("name") or "").strip():
        out = {
            "type": "function",
            "name": str(tool.get("name") or "").strip(),
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("parameters")
            or tool.get("input_schema")
            or {"type": "object", "properties": {}},
        }
        return out
    raw_function = tool.get("function")
    fn = raw_function if isinstance(raw_function, dict) else {}
    name = str(tool.get("name") or fn.get("name") or "").strip()
    return {
        "type": "function",
        "name": name,
        "description": str(tool.get("description") or fn.get("description") or ""),
        "parameters": (
            tool.get("input_schema")
            or tool.get("parameters")
            or fn.get("parameters")
            or {"type": "object", "properties": {}}
        ),
    }


def _responses_tool_choice(choice: object) -> object:
    if not isinstance(choice, dict):
        return choice
    raw_function = choice.get("function")
    fn = raw_function if isinstance(raw_function, dict) else {}
    name = str(choice.get("name") or fn.get("name") or "").strip()
    if str(choice.get("type") or "") == "function" and name:
        return {"type": "function", "name": name}
    return choice


def _new_ws_tool_state(key: str) -> dict[str, Any]:
    return {"id": key, "name": "", "arguments_parts": [], "arguments": "", "done": False}


def _function_call_item(obj: dict) -> dict:
    item = obj.get("item")
    if isinstance(item, dict) and item.get("type") == "function_call":
        return item
    return {}


def _ws_tool_key(obj: dict, item: dict | None = None, *, fallback: str = "") -> str:
    item = item or {}
    values = (
        obj.get("item_id"),
        item.get("id"),
        item.get("call_id"),
        obj.get("call_id"),
        fallback,
        obj.get("output_index"),
    )
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _merge_ws_tool_item(
    tool_items: dict[str, dict[str, Any]], obj: dict, *, fallback: str = ""
) -> str:
    item = _function_call_item(obj)
    if not item:
        return ""
    key = _ws_tool_key(obj, item, fallback=fallback or str(len(tool_items)))
    state = tool_items.setdefault(key, _new_ws_tool_state(key))
    call_id = str(item.get("call_id") or item.get("id") or state["id"] or key).strip()
    name = str(item.get("name") or state["name"] or "").strip()
    if call_id:
        state["id"] = call_id
    if name:
        state["name"] = name
    if item.get("arguments") is not None:
        args = item.get("arguments")
        state["arguments"] = (
            json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args or "")
        )
    return key


def _ws_tool_calls(tool_items: dict[str, dict[str, Any]]) -> list[LLMToolCall]:
    calls: list[LLMToolCall] = []
    for key, state in tool_items.items():
        name = str(state.get("name") or "").strip()
        if not name:
            continue
        raw_args = str(state.get("arguments") or "")
        if not raw_args:
            raw_args = "".join(str(part) for part in state.get("arguments_parts") or [])
        calls.append(
            LLMToolCall(
                id=str(state.get("id") or key),
                name=name,
                arguments=_json_args(raw_args),
            )
        )
    return calls


def _ws_tool_calls_ready(tool_items: dict[str, dict[str, Any]]) -> bool:
    for state in tool_items.values():
        name = str(state.get("name") or "").strip()
        if not name:
            continue
        raw_args = str(state.get("arguments") or "")
        if not raw_args:
            raw_args = "".join(str(part) for part in state.get("arguments_parts") or [])
        if not raw_args.strip():
            continue
        try:
            obj = json.loads(raw_args)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict) and obj:
            return True
    return False


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


def _positive_setting(value: object, default: int) -> int:
    try:
        n = int(value)  # type: ignore[arg-type,call-overload]
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


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
            if context_length is not None:
                info["context_length"] = context_length
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
