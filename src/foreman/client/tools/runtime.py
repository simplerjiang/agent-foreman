"""Production PM tool runtime."""

from __future__ import annotations

import asyncio
import html
import os
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus, urlparse

import httpx

from foreman.shared.config import Config

from .models import (
    EXTERNAL_WEB,
    NEEDS_STRATEGY,
    REQUIRES_APPROVAL,
    SAFE,
    ToolCall,
    ToolResult,
    ToolRuntimeConfig,
    ToolSpec,
)
from .policy import PathGuard, ToolPolicyError, command_allowed, normalize_command

if TYPE_CHECKING:
    from .browser import BrowserRuntime

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "env", "node_modules", ".pytest_cache"}


class PMToolRuntime:
    def __init__(
        self,
        cfg: ToolRuntimeConfig,
        *,
        gate: Any = None,
        auditor: Any = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.gate = gate
        self.auditor = auditor
        self.guard = PathGuard(cfg.workspace, cfg.allowed_roots)
        self._http = http_client
        self._browser: BrowserRuntime | None = None

    @classmethod
    def from_config(
        cls,
        cfg: Config,
        workspace: str | Path,
        *,
        gate: Any = None,
        auditor: Any = None,
    ) -> "PMToolRuntime":
        roots = [Path(w.path) for w in cfg.workspaces] or [Path(workspace)]
        pm = cfg.pm_tools
        return cls(
            ToolRuntimeConfig(
                workspace=Path(workspace),
                allowed_roots=roots,
                file_read=pm.file_read,
                file_write=pm.file_write,
                shell=pm.shell,
                web_fetch=pm.web_fetch,
                web_search=pm.web_search,
                browser=pm.browser,
                allowed_commands=list(pm.allowed_commands),
                allowed_origins=list(pm.allowed_origins),
                web_search_provider=pm.web_search_provider,
                searxng_url=pm.searxng_url,
                browser_headless=pm.browser_headless,
                max_rounds=pm.max_rounds,
            ),
            gate=gate,
            auditor=auditor,
        )

    @staticmethod
    def specs() -> list[ToolSpec]:
        string = {"type": "string"}
        boolean = {"type": "boolean"}
        integer = {"type": "integer"}
        return [
            ToolSpec(
                "list_files",
                "List files under a workspace path.",
                {
                    "type": "object",
                    "properties": {"path": string, "max_results": integer},
                    "additionalProperties": False,
                },
                SAFE,
            ),
            ToolSpec(
                "read_file",
                "Read UTF-8 text from a workspace file.",
                {
                    "type": "object",
                    "properties": {"path": string, "start_line": integer, "end_line": integer},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                SAFE,
            ),
            ToolSpec(
                "search_repo",
                "Search text in workspace files and return matching lines.",
                {
                    "type": "object",
                    "properties": {"query": string, "path": string, "max_results": integer},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                SAFE,
            ),
            ToolSpec(
                "write_file",
                "Write UTF-8 text to a workspace file.",
                {
                    "type": "object",
                    "properties": {"path": string, "text": string},
                    "required": ["path", "text"],
                    "additionalProperties": False,
                },
                NEEDS_STRATEGY,
            ),
            ToolSpec(
                "replace_in_file",
                "Replace one exact, unique text match in a workspace file.",
                {
                    "type": "object",
                    "properties": {"path": string, "old": string, "new": string},
                    "required": ["path", "old", "new"],
                    "additionalProperties": False,
                },
                NEEDS_STRATEGY,
            ),
            ToolSpec(
                "run_command",
                "Run one settings-allowlisted command in the workspace.",
                {
                    "type": "object",
                    "properties": {"command": string},
                    "required": ["command"],
                    "additionalProperties": False,
                },
                NEEDS_STRATEGY,
            ),
            ToolSpec(
                "fetch_url",
                "Fetch an HTTP(S) URL as untrusted external web content.",
                {
                    "type": "object",
                    "properties": {"url": string},
                    "required": ["url"],
                    "additionalProperties": False,
                },
                NEEDS_STRATEGY,
            ),
            ToolSpec(
                "web_search",
                "Search the web for leads only; fetch sources before treating them as facts.",
                {
                    "type": "object",
                    "properties": {"query": string, "max_results": integer},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                NEEDS_STRATEGY,
            ),
            ToolSpec("browser_open", "Open an allowed browser URL.", {
                "type": "object", "properties": {"url": string}, "required": ["url"],
                "additionalProperties": False,
            }, NEEDS_STRATEGY),
            ToolSpec("browser_snapshot", "Return visible browser elements and text.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, NEEDS_STRATEGY),
            ToolSpec("browser_click", "Click a ref from the latest browser snapshot.", {
                "type": "object", "properties": {"ref": string}, "required": ["ref"],
                "additionalProperties": False,
            }, NEEDS_STRATEGY),
            ToolSpec("browser_type", "Type text into a ref from the latest browser snapshot.", {
                "type": "object",
                "properties": {"ref": string, "text": string, "submit": boolean},
                "required": ["ref", "text"],
                "additionalProperties": False,
            }, NEEDS_STRATEGY),
            ToolSpec("browser_extract_text", "Extract title, URL, and visible text.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, NEEDS_STRATEGY),
            ToolSpec("browser_screenshot", "Save a browser screenshot artifact.", {
                "type": "object", "properties": {"full_page": boolean}, "additionalProperties": False,
            }, NEEDS_STRATEGY),
            ToolSpec("browser_close", "Close the PM browser session.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, SAFE),
        ]

    def tool_schema(self) -> list[dict[str, Any]]:
        return [spec.to_prompt() for spec in self.specs()]

    def runtime_context(self) -> dict[str, Any]:
        return {
            "os": os.name,
            "cwd": str(self.cfg.workspace),
            "path_style": "windows" if os.name == "nt" else "posix",
            "shell": "powershell" if os.name == "nt" else "sh",
        }

    def policy_context(self) -> dict[str, Any]:
        return {
            "tools_enabled": {
                "file_read": self.cfg.file_read,
                "file_write": self.cfg.file_write,
                "shell": self.cfg.shell,
                "web_fetch": self.cfg.web_fetch,
                "web_search": self.cfg.web_search,
                "browser": self.cfg.browser,
            },
            "allowed_roots": [str(p) for p in self.cfg.allowed_roots],
            "allowed_commands": list(self.cfg.allowed_commands),
            "allowed_origins": list(self.cfg.allowed_origins),
            "web_search_rule": (
                "web_search returns leads only; verify facts with fetch_url or local evidence"
            ),
            "auditor_rule": "Gate hard-denies requires-approval; Auditor only reviews gray commands.",
        }

    async def call(
        self, call: ToolCall, *, context_taint: list[str] | None = None
    ) -> ToolResult:
        args = _unwrap_tool_args(call.arguments if isinstance(call.arguments, dict) else {})
        if args.get("__invalid_args__"):
            return ToolResult(call.id, call.name, False, error="invalid_args")
        try:
            if call.name == "list_files":
                return self._list_files(call.id, args)
            if call.name == "read_file":
                return self._read_file(call.id, args)
            if call.name == "search_repo":
                return self._search_repo(call.id, args)
            if call.name == "write_file":
                return self._write_file(call.id, args)
            if call.name == "replace_in_file":
                return self._replace_in_file(call.id, args)
            if call.name == "run_command":
                return await self._run_command(call.id, args, context_taint=context_taint or [])
            if call.name == "fetch_url":
                return await self._fetch_url(call.id, args)
            if call.name == "web_search":
                return await self._web_search(call.id, args)
            if call.name.startswith("browser_"):
                return await self._browser_call(ToolCall(call.id, call.name, args))
            return ToolResult(call.id, call.name, False, error="unknown_tool", risk=REQUIRES_APPROVAL)
        except ToolPolicyError as exc:
            return ToolResult(call.id, call.name, False, error=exc.code, risk=REQUIRES_APPROVAL)
        except Exception as exc:  # noqa: BLE001 - tool failures must be returned to the PM loop
            return ToolResult(
                call.id, call.name, False, error=f"{type(exc).__name__}: {str(exc)[:200]}"
            )

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.aclose()
            self._browser = None
        if self._http is not None:
            await self._http.aclose()

    def _list_files(self, cid: str, args: dict[str, Any]) -> ToolResult:
        if not self.cfg.file_read:
            return ToolResult(cid, "list_files", False, error="tool_disabled", risk=NEEDS_STRATEGY)
        root = self.guard.resolve(str(args.get("path") or "."))
        max_results = _positive_int(args.get("max_results"), self.cfg.max_results)
        files: list[str] = []
        if root.is_file():
            files.append(self.guard.relative(root))
        else:
            for current, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for name in sorted([*dirs, *names]):
                    files.append(self.guard.relative(Path(current) / name))
                    if len(files) >= max_results:
                        return ToolResult(cid, "list_files", True, {"files": files}, truncated=True)
        return ToolResult(cid, "list_files", True, {"files": files})

    def _read_file(self, cid: str, args: dict[str, Any]) -> ToolResult:
        if not self.cfg.file_read:
            return ToolResult(cid, "read_file", False, error="tool_disabled", risk=NEEDS_STRATEGY)
        path = self.guard.resolve(str(args.get("path") or ""))
        if not path.is_file():
            return ToolResult(cid, "read_file", False, error="not_file")
        text = _read_text(path)
        start = _positive_int(args.get("start_line"), 1)
        end = _positive_int(args.get("end_line"), 0)
        if start > 1 or end > 0:
            lines = text.splitlines()
            hi = end if end > 0 else len(lines)
            text = "\n".join(lines[start - 1:hi])
        text, truncated = _truncate(text, self.cfg.max_chars)
        return ToolResult(
            cid,
            "read_file",
            True,
            {"path": self.guard.relative(path), "text": text},
            truncated=truncated,
        )

    def _search_repo(self, cid: str, args: dict[str, Any]) -> ToolResult:
        if not self.cfg.file_read:
            return ToolResult(cid, "search_repo", False, error="tool_disabled", risk=NEEDS_STRATEGY)
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(cid, "search_repo", False, error="missing_query")
        root = self.guard.resolve(str(args.get("path") or "."))
        max_results = _positive_int(args.get("max_results"), 50)
        matches: list[dict[str, Any]] = []
        paths = [root] if root.is_file() else _walk_files(root)
        for path in paths:
            if len(matches) >= max_results:
                break
            try:
                text = _read_text(path)
            except UnicodeDecodeError:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if query.casefold() in line.casefold():
                    matches.append(
                        {
                            "path": self.guard.relative(path),
                            "line": idx,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= max_results:
                        break
        return ToolResult(
            cid,
            "search_repo",
            True,
            {"matches": matches},
            truncated=len(matches) >= max_results,
        )

    def _write_file(self, cid: str, args: dict[str, Any]) -> ToolResult:
        if not self.cfg.file_write:
            return ToolResult(cid, "write_file", False, error="tool_disabled", risk=NEEDS_STRATEGY)
        path = self.guard.resolve(str(args.get("path") or ""), for_write=True)
        text = str(args.get("text") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return ToolResult(
            cid,
            "write_file",
            True,
            {"path": self.guard.relative(path), "bytes": len(text.encode("utf-8"))},
            risk=NEEDS_STRATEGY,
        )

    def _replace_in_file(self, cid: str, args: dict[str, Any]) -> ToolResult:
        if not self.cfg.file_write:
            return ToolResult(
                cid, "replace_in_file", False, error="tool_disabled", risk=NEEDS_STRATEGY
            )
        old = str(args.get("old") or "")
        if not old:
            return ToolResult(cid, "replace_in_file", False, error="missing_old")
        path = self.guard.resolve(str(args.get("path") or ""), for_write=True)
        text = _read_text(path)
        count = text.count(old)
        if count != 1:
            return ToolResult(
                cid,
                "replace_in_file",
                False,
                data={"match_count": count},
                error="old_not_unique",
                risk=NEEDS_STRATEGY,
            )
        new_text = text.replace(old, str(args.get("new") or ""), 1)
        path.write_text(new_text, encoding="utf-8")
        return ToolResult(
            cid,
            "replace_in_file",
            True,
            {"path": self.guard.relative(path), "match_count": 1},
            risk=NEEDS_STRATEGY,
        )

    async def _run_command(
        self, cid: str, args: dict[str, Any], *, context_taint: list[str]
    ) -> ToolResult:
        if not self.cfg.shell:
            return ToolResult(cid, "run_command", False, error="tool_disabled", risk=NEEDS_STRATEGY)
        command = normalize_command(str(args.get("command") or ""))
        if not command:
            return ToolResult(cid, "run_command", False, error="missing_command")
        if EXTERNAL_WEB in context_taint:
            return ToolResult(
                cid,
                "run_command",
                False,
                error="shell_after_web_requires_approval",
                risk=REQUIRES_APPROVAL,
            )
        if self.gate is not None and getattr(self.gate, "classify", None):
            risk = self.gate.classify(command)
            if risk == REQUIRES_APPROVAL:
                return ToolResult(
                    cid, "run_command", False, error="requires_approval", risk=REQUIRES_APPROVAL
                )
            if risk == NEEDS_STRATEGY:
                audit_block = await self._audit_gray_command(command)
                if audit_block is not None:
                    return ToolResult(
                        cid,
                        "run_command",
                        False,
                        data=audit_block,
                        error=f"auditor_{audit_block['verdict']}",
                        risk=(
                            REQUIRES_APPROVAL
                            if audit_block["verdict"] == "escalate"
                            else NEEDS_STRATEGY
                        ),
                    )
        if not command_allowed(command, self.cfg.allowed_commands):
            return ToolResult(
                cid,
                "run_command",
                False,
                error="command_not_allowlisted",
                risk=NEEDS_STRATEGY,
            )

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=str(self.cfg.workspace),
                capture_output=True,
                text=True,
                shell=True,
                timeout=self.cfg.timeout_s,
            )
            stdout, out_trunc = _truncate(proc.stdout or "", self.cfg.max_chars)
            stderr, err_trunc = _truncate(proc.stderr or "", self.cfg.max_chars)
            return ToolResult(
                cid,
                "run_command",
                True,
                {
                    "command": command,
                    "returncode": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "truncated": out_trunc or err_trunc,
                },
                truncated=out_trunc or err_trunc,
                risk=NEEDS_STRATEGY,
            )
        except subprocess.TimeoutExpired as exc:
            stdout, out_trunc = _truncate(str(exc.stdout or ""), self.cfg.max_chars)
            stderr, err_trunc = _truncate(str(exc.stderr or ""), self.cfg.max_chars)
            return ToolResult(
                cid,
                "run_command",
                False,
                {
                    "command": command,
                    "returncode": -1,
                    "stdout": stdout,
                    "stderr": stderr,
                    "truncated": out_trunc or err_trunc,
                },
                error="timeout",
                truncated=True,
                risk=NEEDS_STRATEGY,
            )

    async def _audit_gray_command(self, command: str) -> dict[str, Any] | None:
        if self.auditor is None or not getattr(self.auditor, "audit", None):
            return None
        audit = await self.auditor.audit(
            command,
            current_step="PM tool runtime run_command",
            writable_paths=", ".join(str(path) for path in self.cfg.allowed_roots),
            autonomy="PM run_command requires settings allowlist and Gate screening",
        )
        verdict = str(getattr(audit, "verdict", "") or "").strip().lower()
        if verdict == "pass":
            return None
        return {
            "verdict": verdict or "blocked",
            "goal_quality": str(getattr(audit, "goal_quality", "") or ""),
            "risk_severity": str(getattr(audit, "risk_severity", "") or ""),
            "reasons": list(getattr(audit, "reasons", []) or []),
            "suggestions": list(getattr(audit, "suggestions", []) or []),
        }

    async def _fetch_url(self, cid: str, args: dict[str, Any]) -> ToolResult:
        if not self.cfg.web_fetch:
            return ToolResult(cid, "fetch_url", False, error="tool_disabled", risk=NEEDS_STRATEGY)
        url = str(args.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ToolResult(cid, "fetch_url", False, error="unsupported_scheme", risk=NEEDS_STRATEGY)
        client = self._client()
        response = await client.get(url, follow_redirects=True)
        text = response.text
        text, truncated = _truncate(text, self.cfg.max_chars)
        return ToolResult(
            cid,
            "fetch_url",
            True,
            {"url": str(response.url), "status_code": response.status_code, "text": text},
            truncated=truncated,
            risk=NEEDS_STRATEGY,
            taint=[EXTERNAL_WEB],
        )

    async def _web_search(self, cid: str, args: dict[str, Any]) -> ToolResult:
        if not self.cfg.web_search:
            return ToolResult(cid, "web_search", False, error="tool_disabled", risk=NEEDS_STRATEGY)
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(cid, "web_search", False, error="missing_query")
        max_results = min(max(_positive_int(args.get("max_results"), 5), 1), 10)
        provider = (self.cfg.web_search_provider or "duckduckgo").strip().lower()
        warnings: list[str] = []
        try:
            if provider == "searxng" and self.cfg.searxng_url:
                results = await self._searxng_search(query, max_results)
            else:
                results = await self._duckduckgo_search(query, max_results)
        except Exception as exc:  # noqa: BLE001 - search is a best-effort lead source
            results = []
            warnings.append(f"{type(exc).__name__}: {str(exc)[:160]}")
        return ToolResult(
            cid,
            "web_search",
            True,
            {
                "query": query,
                "provider": provider,
                "results": results,
                "warnings": warnings,
                "fact_rule": "Search results are leads only; fetch_url or local evidence is required.",
            },
            risk=NEEDS_STRATEGY,
            taint=[EXTERNAL_WEB],
        )

    async def _browser_call(self, call: ToolCall) -> ToolResult:
        if not self.cfg.browser:
            return ToolResult(call.id, call.name, False, error="tool_disabled", risk=NEEDS_STRATEGY)
        if self._browser is None:
            from .browser import BrowserRuntime

            self._browser = BrowserRuntime(
                workspace=self.cfg.workspace,
                allowed_origins=self.cfg.allowed_origins,
                headless=self.cfg.browser_headless,
                max_chars=self.cfg.max_chars,
            )
        return await self._browser.call(call)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.cfg.timeout_s, trust_env=False)
        return self._http

    async def _searxng_search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        base = self.cfg.searxng_url.rstrip("/")
        response = await self._client().get(
            f"{base}/search", params={"q": query, "format": "json"}, follow_redirects=True
        )
        response.raise_for_status()
        data = response.json()
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(data.get("results", [])[:max_results], start=1):
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "rank": idx,
                    "title": str(item.get("title") or "")[:300],
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("content") or "")[:800],
                    "source": "searxng",
                }
            )
        return out

    async def _duckduckgo_search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        response = await self._client().get(
            url,
            headers={"User-Agent": "Foreman PM tools/0.1"},
            follow_redirects=True,
        )
        response.raise_for_status()
        parser = _DDGParser(max_results)
        parser.feed(response.text)
        return parser.results


def _walk_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            out.append(Path(current) / name)
    return out


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw[:2048]:
        raise UnicodeDecodeError("utf-8", raw, 0, 1, "binary file")
    return raw.decode("utf-8", errors="replace")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]...", True


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if not isinstance(value, (str, bytes, bytearray)):
        return default
    try:
        out = int(value)
    except ValueError:
        return default
    return out if out > 0 else default


def _unwrap_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    for key in ("arguments", "args", "input"):
        nested = args.get(key)
        if isinstance(nested, dict):
            extras = {
                str(k): v
                for k, v in args.items()
                if k not in {"arguments", "args", "input", "context_taint", "source", "tool"}
            }
            return {**nested, **extras}
    return args


class _DDGParser(HTMLParser):
    def __init__(self, max_results: int) -> None:
        super().__init__()
        self.max_results = max_results
        self.results: list[dict[str, Any]] = []
        self._capture: str = ""
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        classes = set(attr.get("class", "").split())
        if tag == "a" and "result__a" in classes and len(self.results) < self.max_results:
            self._capture = "title"
            self._href = attr.get("href", "")
            self._buf = []
        elif "result__snippet" in classes and self.results:
            self._capture = "snippet"
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture == "title" and tag == "a":
            title = html.unescape(" ".join("".join(self._buf).split()))
            if title and self._href:
                self.results.append(
                    {
                        "rank": len(self.results) + 1,
                        "title": title[:300],
                        "url": self._href,
                        "snippet": "",
                        "source": "duckduckgo",
                    }
                )
            self._capture = ""
        elif self._capture == "snippet" and tag in {"a", "div"}:
            snippet = html.unescape(" ".join("".join(self._buf).split()))
            if snippet:
                self.results[-1]["snippet"] = snippet[:800]
            self._capture = ""
