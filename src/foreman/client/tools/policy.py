"""Deterministic PM tool policy helpers."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

LOCAL_BROWSER_HOSTS = {"localhost", "127.0.0.1", "::1"}
BLOCKED_BROWSER_SCHEMES = {"file", "chrome", "edge", "devtools", "about"}


class ToolPolicyError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PathGuard:
    def __init__(self, workspace: Path, allowed_roots: list[Path]) -> None:
        self.workspace = workspace.resolve(strict=False)
        roots = allowed_roots or [workspace]
        self.allowed_roots = [Path(root).resolve(strict=False) for root in roots]

    def resolve(self, path: str, *, for_write: bool = False) -> Path:
        raw = str(path or ".").strip() or "."
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, ValueError) as exc:
            raise ToolPolicyError("invalid_path") from exc
        probe = resolved.parent if for_write else resolved
        try:
            probe = probe.resolve(strict=False)
        except (OSError, ValueError) as exc:
            raise ToolPolicyError("invalid_path") from exc
        if not any(_is_relative_or_same(probe, root) for root in self.allowed_roots):
            raise ToolPolicyError("path_outside_workspace")
        return resolved

    def relative(self, path: Path) -> str:
        try:
            return str(path.resolve(strict=False).relative_to(self.workspace)).replace("\\", "/")
        except ValueError:
            return str(path)


def _is_relative_or_same(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except ValueError:
        return False


def normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", str(command or "").strip())


def command_allowed(command: str, allowed: list[str]) -> bool:
    norm = normalize_command(command)
    return bool(norm) and norm in {normalize_command(item) for item in allowed if item}


def origin_for(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{host.lower()}{port}"


def browser_origin_allowed(url: str, allowed_origins: list[str]) -> bool:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme in BLOCKED_BROWSER_SCHEMES or scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if host in LOCAL_BROWSER_HOSTS:
        return True
    origin = origin_for(url)
    return origin in {origin_for(item) for item in allowed_origins if origin_for(item)}
