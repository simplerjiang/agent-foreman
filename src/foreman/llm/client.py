"""Provider-agnostic LLM client.

Used by PM Brain / Reviewer / Briefing — with YOUR API key (config.llm + .env).
NOT used by claude/codex CLIs, which authenticate themselves.

Supports two wire formats:
  - "openai"    -> POST {base_url}/chat/completions
  - "anthropic" -> POST {base_url}/messages
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import Config


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


class LLMClient:
    def __init__(self, cfg: Config) -> None:
        self.provider = cfg.llm.provider
        self.base_url = cfg.llm.base_url.rstrip("/")
        self.model = cfg.llm.model
        self.api_key = cfg.secrets.llm_api_key
        self.max_tokens = cfg.llm.max_tokens
        self._client = httpx.AsyncClient(timeout=cfg.llm.request_timeout_s)

    async def complete(self, messages: list[Message], *, json_mode: bool = False) -> str:
        """Return the assistant's text. Set json_mode=True to nudge structured JSON output."""
        if self.provider == "anthropic":
            return await self._anthropic(messages, json_mode)
        return await self._openai(messages, json_mode)

    async def _openai(self, messages: list[Message], json_mode: bool) -> str:
        payload: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        r = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _anthropic(self, messages: list[Message], json_mode: bool) -> str:
        system = "\n".join(m.content for m in messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        payload: dict = {"model": self.model, "max_tokens": self.max_tokens, "messages": turns}
        if system:
            payload["system"] = system
        r = await self._client.post(
            f"{self.base_url}/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json=payload,
        )
        r.raise_for_status()
        blocks = r.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    async def aclose(self) -> None:
        await self._client.aclose()
