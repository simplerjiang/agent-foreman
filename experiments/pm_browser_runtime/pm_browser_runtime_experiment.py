from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


SAFE = "safe"
NEEDS_STRATEGY = "needs-strategy"
REQUIRES_APPROVAL = "requires-approval"
EXTERNAL_WEB = "external_web_content"


@dataclass
class ToolResult:
    id: str
    name: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    risk: str = NEEDS_STRATEGY
    taint: list[str] = field(default_factory=lambda: [EXTERNAL_WEB])
    artifact_paths: list[str] = field(default_factory=list)
    truncated: bool = False


@dataclass
class BrowserRuntimeConfig:
    artifacts_dir: Path
    allowed_origins: tuple[str, ...]
    headless: bool = True
    browser_channel: str = field(
        default_factory=lambda: os.getenv("FOREMAN_PLAYWRIGHT_CHANNEL", "").strip()
    )
    browser_executable_path: str = field(
        default_factory=lambda: os.getenv("FOREMAN_PLAYWRIGHT_EXECUTABLE_PATH", "").strip()
    )
    width: int = 1280
    height: int = 800
    max_text_chars: int = 3000
    timeout_ms: int = 5000


class MiniPMBrowserRuntime:
    """Standalone Playwright browser runtime experiment; not production Foreman code."""

    def __init__(self, cfg: BrowserRuntimeConfig) -> None:
        self.cfg = cfg
        self.artifacts_dir = cfg.artifacts_dir.resolve()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._refs: dict[str, str] = {}

    def __enter__(self) -> MiniPMBrowserRuntime:
        self._ensure_page()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def call(self, call: dict[str, Any]) -> ToolResult:
        name = str(call.get("name") or "")
        args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        cid = str(call.get("id") or f"call_{name}")
        try:
            if name == "browser_open":
                return self.browser_open(cid, args)
            if name == "browser_snapshot":
                return self.browser_snapshot(cid)
            if name == "browser_click":
                return self.browser_click(cid, args)
            if name == "browser_type":
                return self.browser_type(cid, args)
            if name == "browser_extract_text":
                return self.browser_extract_text(cid)
            if name == "browser_screenshot":
                return self.browser_screenshot(cid, args)
            if name == "browser_close":
                return self.browser_close(cid)
        except Exception as exc:  # noqa: BLE001 - experiment returns structured errors
            return ToolResult(cid, name, False, error=f"{type(exc).__name__}: {exc}")
        return ToolResult(cid, name, False, error="unknown_tool", risk=REQUIRES_APPROVAL)

    def browser_open(self, cid: str, args: dict[str, Any]) -> ToolResult:
        url = str(args.get("url") or "")
        if not self._origin_allowed(url):
            return ToolResult(cid, "browser_open", False, error="origin_not_allowed")
        page = self._ensure_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.timeout_ms)
        page.wait_for_timeout(100)
        return ToolResult(
            cid,
            "browser_open",
            True,
            data={
                "url": page.url,
                "title": page.title(),
                "status": response.status if response else None,
            },
        )

    def browser_snapshot(self, cid: str) -> ToolResult:
        return ToolResult(cid, "browser_snapshot", True, data=self._snapshot())

    def browser_click(self, cid: str, args: dict[str, Any]) -> ToolResult:
        ref = self._resolve_ref(args)
        if not ref:
            return ToolResult(cid, "browser_click", False, error="missing_or_unknown_ref")
        page = self._ensure_page()
        page.locator(f'[data-foreman-ref="{ref}"]').click(timeout=self.cfg.timeout_ms)
        page.wait_for_timeout(150)
        return ToolResult(cid, "browser_click", True, data={"ref": ref, **self._page_state()})

    def browser_type(self, cid: str, args: dict[str, Any]) -> ToolResult:
        ref = self._resolve_ref(args)
        text = str(args.get("text") or "")
        if not ref:
            return ToolResult(cid, "browser_type", False, error="missing_or_unknown_ref")
        page = self._ensure_page()
        page.locator(f'[data-foreman-ref="{ref}"]').fill(text, timeout=self.cfg.timeout_ms)
        if bool(args.get("submit")):
            page.locator(f'[data-foreman-ref="{ref}"]').press("Enter", timeout=self.cfg.timeout_ms)
        page.wait_for_timeout(100)
        return ToolResult(cid, "browser_type", True, data={"ref": ref, "typed_chars": len(text)})

    def browser_extract_text(self, cid: str) -> ToolResult:
        data = self._page_state()
        data["text_excerpt"] = self._visible_text()
        return ToolResult(cid, "browser_extract_text", True, data=data)

    def browser_screenshot(self, cid: str, args: dict[str, Any]) -> ToolResult:
        page = self._ensure_page()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / f"pm-browser-{int(time.time() * 1000)}.png"
        page.screenshot(path=str(path), full_page=bool(args.get("full_page")))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        data = {
            **self._page_state(),
            "path": str(path),
            "sha256": digest,
            "bytes": path.stat().st_size,
        }
        return ToolResult(
            cid,
            "browser_screenshot",
            True,
            data=data,
            artifact_paths=[str(path)],
        )

    def browser_close(self, cid: str) -> ToolResult:
        self.close()
        return ToolResult(cid, "browser_close", True, data={}, risk=SAFE, taint=[])

    def close(self) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._refs = {}

    def _ensure_page(self) -> Page:
        if self._page:
            return self._page
        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": self.cfg.headless}
        if self.cfg.browser_executable_path:
            launch_kwargs["executable_path"] = self.cfg.browser_executable_path
        elif self.cfg.browser_channel:
            launch_kwargs["channel"] = self.cfg.browser_channel
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            viewport={"width": self.cfg.width, "height": self.cfg.height}
        )
        self._page = self._context.new_page()
        return self._page

    def _snapshot(self) -> dict[str, Any]:
        page = self._ensure_page()
        self._refs = {}
        elements: list[dict[str, Any]] = []
        locator = page.locator(
            "button, input, textarea, select, a[href], [role=button], [role=link], [tabindex]"
        )
        count = locator.count()
        for index in range(count):
            item = locator.nth(index)
            if not item.is_visible(timeout=500):
                continue
            ref = f"ref_{len(elements) + 1}"
            info = item.evaluate(
                """(el, ref) => {
                    el.setAttribute('data-foreman-ref', ref);
                    const tag = el.tagName.toLowerCase();
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const labels = el.labels ? Array.from(el.labels).map(l => l.innerText.trim()).join(' ') : '';
                    const role = el.getAttribute('role')
                        || (tag === 'a' ? 'link'
                        : tag === 'button' || type === 'button' || type === 'submit' ? 'button'
                        : tag === 'select' ? 'combobox'
                        : tag === 'textarea' || tag === 'input' ? 'textbox'
                        : 'generic');
                    const name = el.getAttribute('aria-label')
                        || labels
                        || el.getAttribute('placeholder')
                        || el.innerText
                        || el.getAttribute('value')
                        || '';
                    return {
                        role,
                        name: name.trim().replace(/\\s+/g, ' '),
                        tag,
                        type,
                        value: type === 'password' ? '<redacted>' : (el.value || '')
                    };
                }""",
                ref,
            )
            self._refs[ref] = str(info.get("name") or "")
            elements.append({"ref": ref, **info})
        return {
            **self._page_state(),
            "text_excerpt": self._visible_text(),
            "elements": elements,
        }

    def _resolve_ref(self, args: dict[str, Any]) -> str:
        if not self._refs:
            self._snapshot()
        ref = str(args.get("ref") or "")
        if ref in self._refs:
            return ref
        name = str(args.get("name") or args.get("text") or "").casefold()
        if not name:
            return ""
        for candidate, label in self._refs.items():
            if name in label.casefold():
                return candidate
        return ""

    def _page_state(self) -> dict[str, Any]:
        page = self._ensure_page()
        return {"url": page.url, "title": page.title()}

    def _visible_text(self) -> str:
        page = self._ensure_page()
        text = page.locator("body").inner_text(timeout=self.cfg.timeout_ms)
        text = " ".join(text.split())
        if len(text) <= self.cfg.max_text_chars:
            return text
        return text[: self.cfg.max_text_chars]

    def _origin_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        port = f":{parsed.port}" if parsed.port else ""
        origin = f"{parsed.scheme}://{parsed.hostname}{port}"
        return origin in self.cfg.allowed_origins
