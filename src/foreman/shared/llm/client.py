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
import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from ..config import Config


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMConfigError(RuntimeError):
    """Raised before a request when the PM brain is not configured enough to call."""


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
        self.timeout = cfg.llm.request_timeout_s
        self.mode = (cfg.llm.transport or "http").strip().lower()
        self._settings_resolver = settings_resolver
        # `transport` lets tests inject httpx.MockTransport (no real network / tokens spent).
        self._client = httpx.AsyncClient(timeout=cfg.llm.request_timeout_s, transport=transport)
        # `ws_connect(url, headers, timeout) -> async-context-manager` lets tests inject a fake socket.
        self._ws_connect = ws_connect or _default_ws_connect

    def _resolve(self) -> tuple[str, str, str]:
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

    async def complete(self, messages: list[Message], *, json_mode: bool = False) -> str:
        """Return the assistant's text. Set json_mode=True to nudge structured JSON output.

        On the ws transport json_mode is a no-op (the Responses path has no response_format; callers
        already instruct the model to emit JSON and parse tolerantly)."""
        provider, base_url, model = self._resolve()
        if self.mode == "ws":
            return await self._responses_ws(messages, base_url, model)
        if provider == "anthropic":
            return await self._anthropic(messages, json_mode, base_url, model)
        return await self._openai(messages, json_mode, base_url, model)

    async def _openai(
        self, messages: list[Message], json_mode: bool, base_url: str, model: str
    ) -> str:
        payload: dict = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        r = await self._client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=payload,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

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

    async def _responses_ws(self, messages: list[Message], base_url: str, model: str) -> str:
        """Run one turn over the Responses-API WebSocket and return the accumulated assistant text.

        Sends a `response.create` frame; accumulates `response.output_text.delta` deltas; stops at
        `response.completed`; raises on an `error` frame or if the stream closes early. Each receive
        is bounded by the configured request timeout so a stalled upstream can't hang the loop."""
        instructions, items = _messages_to_responses_input(messages)
        request: dict = {
            "type": "response.create",
            "model": model,
            "stream": True,
            "input": items,
        }
        if instructions:
            request["instructions"] = instructions
        headers = {"Authorization": f"Bearer {self._api_key()}"}
        buf: list[str] = []
        async with self._ws_connect(_ws_url(base_url), headers, self.timeout) as ws:
            await ws.send(json.dumps(request))
            while True:
                raw = await asyncio.wait_for(ws.recv(), self.timeout)
                try:
                    obj = json.loads(raw)
                except (TypeError, ValueError):
                    continue  # ignore a non-JSON keepalive/marker frame
                etype = obj.get("type")
                if etype == "response.output_text.delta":
                    buf.append(str(obj.get("delta", "")))
                elif etype == "response.completed":
                    break
                elif etype == "error":
                    raise RuntimeError(f"responses ws error: {obj.get('error')}")
        return "".join(buf)

    async def aclose(self) -> None:
        await self._client.aclose()
