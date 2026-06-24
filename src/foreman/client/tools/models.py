"""Shared PM tool models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SAFE = "safe"
NEEDS_STRATEGY = "needs-strategy"
REQUIRES_APPROVAL = "requires-approval"
EXTERNAL_WEB = "external_web_content"
Risk = Literal["safe", "needs-strategy", "requires-approval"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: Risk = SAFE

    def to_prompt(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk": self.risk,
        }

    def to_native(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_obj(cls, obj: object, *, fallback_id: str) -> "ToolCall | None":
        if not isinstance(obj, dict):
            return None
        name = str(obj.get("name") or obj.get("tool") or "").strip()
        args = obj.get("arguments", obj.get("args", obj.get("input")))
        if args is None:
            args = {
                str(key): value
                for key, value in obj.items()
                if key
                not in {"type", "id", "call_id", "name", "tool", "arguments", "args", "input"}
            }
        if not name or not isinstance(args, dict):
            return None
        cid = str(obj.get("id") or obj.get("call_id") or fallback_id).strip() or fallback_id
        return cls(id=cid, name=name, arguments=args)


@dataclass
class ToolResult:
    id: str
    name: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    truncated: bool = False
    risk: Risk = SAFE
    taint: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "truncated": self.truncated,
            "risk": self.risk,
            "taint": list(self.taint),
            "artifact_paths": list(self.artifact_paths),
        }


@dataclass
class ToolRuntimeConfig:
    workspace: Path
    allowed_roots: list[Path]
    file_read: bool = True
    file_write: bool = False
    shell: bool = False
    web_fetch: bool = False
    web_search: bool = False
    browser: bool = False
    allowed_commands: list[str] = field(default_factory=lambda: ["python --version"])
    allowed_origins: list[str] = field(default_factory=list)
    web_search_provider: str = "duckduckgo"
    searxng_url: str = ""
    browser_headless: bool = False
    max_rounds: int = 6
    timeout_s: int = 30
    max_chars: int = 12000
    max_results: int = 200
