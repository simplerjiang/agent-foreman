"""Async Playwright runtime for PM browser tools."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any

from .models import EXTERNAL_WEB, NEEDS_STRATEGY, SAFE, ToolCall, ToolResult
from .policy import browser_origin_allowed


class BrowserRuntime:
    def __init__(
        self,
        *,
        workspace: Path,
        allowed_origins: list[str],
        headless: bool,
        max_chars: int,
    ) -> None:
        self.workspace = Path(workspace)
        self.allowed_origins = allowed_origins
        self.headless = headless
        self.max_chars = max_chars
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._refs: dict[str, str] = {}

    async def call(self, call: ToolCall) -> ToolResult:
        if call.name == "browser_open":
            return await self.open(call.id, str(call.arguments.get("url") or ""))
        if call.name == "browser_snapshot":
            return await self.snapshot(call.id)
        if call.name == "browser_click":
            return await self.click(call.id, _ref_arg(call.arguments))
        if call.name == "browser_type":
            return await self.type_text(
                call.id,
                _ref_arg(call.arguments),
                str(call.arguments.get("text") or call.arguments.get("value") or ""),
                bool(call.arguments.get("submit", False)),
            )
        if call.name == "browser_extract_text":
            return await self.extract_text(call.id)
        if call.name == "browser_screenshot":
            return await self.screenshot(call.id, bool(call.arguments.get("full_page", False)))
        if call.name == "browser_close":
            await self.aclose()
            return ToolResult(call.id, "browser_close", True, risk=SAFE)
        return ToolResult(call.id, call.name, False, error="unknown_tool")

    async def open(self, cid: str, url: str) -> ToolResult:
        if not browser_origin_allowed(url, self.allowed_origins):
            return self._result(cid, "browser_open", False, error="origin_not_allowed")
        page = await self._ensure_page()
        await page.goto(url, wait_until="domcontentloaded")
        return self._result(cid, "browser_open", True, data=await self._page_state())

    async def snapshot(self, cid: str) -> ToolResult:
        return self._result(cid, "browser_snapshot", True, data=await self._snapshot())

    async def click(self, cid: str, ref: str) -> ToolResult:
        locator = await self._locator_for(ref)
        if locator is None:
            return self._result(cid, "browser_click", False, error="missing_or_unknown_ref")
        await locator.click()
        return self._result(cid, "browser_click", True, data={"ref": ref, **await self._page_state()})

    async def type_text(self, cid: str, ref: str, text: str, submit: bool) -> ToolResult:
        locator = await self._locator_for(ref, for_type=True)
        if locator is None:
            return self._result(cid, "browser_type", False, error="missing_or_unknown_ref")
        if submit:
            return self._result(cid, "browser_type", False, error="submit_requires_approval")
        await locator.fill(text)
        return self._result(
            cid, "browser_type", True, data={"ref": ref, "typed_chars": len(text)}
        )

    async def extract_text(self, cid: str) -> ToolResult:
        return self._result(cid, "browser_extract_text", True, data=await self._page_state())

    async def screenshot(self, cid: str, full_page: bool) -> ToolResult:
        page = await self._ensure_page()
        artifact_dir = self.workspace / "artifacts" / "pm_browser"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / f"pm-browser-{int(time.time() * 1000)}.png"
        data = await page.screenshot(path=str(path), full_page=full_page)
        digest = hashlib.sha256(data).hexdigest()
        state = await self._page_state(include_text=False)
        state.update(
            {
                "path": str(path),
                "bytes": len(data),
                "sha256": digest,
                "full_page": full_page,
            }
        )
        return self._result(
            cid,
            "browser_screenshot",
            True,
            data=state,
            artifact_paths=[str(path)],
        )

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._refs = {}

    async def _ensure_page(self):
        if self._page is not None:
            return self._page
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": self.headless}
        executable_path = os.getenv("FOREMAN_PLAYWRIGHT_EXECUTABLE_PATH", "").strip()
        channel = os.getenv("FOREMAN_PLAYWRIGHT_CHANNEL", "").strip()
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        elif channel:
            launch_kwargs["channel"] = channel
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        return self._page

    async def _snapshot(self) -> dict[str, Any]:
        page = await self._ensure_page()
        await self._mark_refs(page)
        elements = await page.locator("[data-foreman-ref]").evaluate_all(
            """els => els.map(el => ({
                ref: el.getAttribute('data-foreman-ref'),
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '',
                name: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('value') || '').trim(),
                enabled: !el.disabled
            })).slice(0, 80)"""
        )
        state = await self._page_state()
        state["elements"] = elements
        return state

    async def _mark_refs(self, page) -> None:
        script = """() => {
            const selectors = 'a,button,input,textarea,select,[role="button"],[contenteditable="true"]';
            return Array.from(document.querySelectorAll(selectors)).slice(0, 80).map((el, idx) => {
                const ref = `ref-${idx + 1}`;
                el.setAttribute('data-foreman-ref', ref);
                return ref;
            });
        }"""
        refs = await page.evaluate(script)
        self._refs = {ref: f'[data-foreman-ref="{ref}"]' for ref in refs}

    async def _locator_for(self, ref: str, *, for_type: bool = False):
        page = await self._ensure_page()
        selector = self._refs.get(ref)
        if selector:
            return page.locator(selector)
        text = str(ref or "").strip()
        if not text:
            return None
        if for_type:
            label = page.get_by_label(text, exact=True)
            if await label.count():
                return label.nth(0)
        by_text = page.get_by_text(text, exact=True)
        if await by_text.count():
            return by_text.nth(0)
        return None

    async def _page_state(self, *, include_text: bool = True) -> dict[str, Any]:
        page = await self._ensure_page()
        title = await page.title()
        out: dict[str, Any] = {"url": page.url, "title": title}
        if include_text:
            text = await page.locator("body").inner_text(timeout=3000)
            if len(text) > self.max_chars:
                text = text[: self.max_chars] + "\n...[truncated]..."
                out["truncated"] = True
            out["text"] = text
        return out

    def _result(
        self,
        cid: str,
        name: str,
        ok: bool,
        *,
        data: dict[str, Any] | None = None,
        error: str = "",
        artifact_paths: list[str] | None = None,
    ) -> ToolResult:
        return ToolResult(
            cid,
            name,
            ok,
            data=data or {},
            error=error,
            risk=NEEDS_STRATEGY if name != "browser_close" else SAFE,
            taint=[EXTERNAL_WEB] if name != "browser_close" else [],
            artifact_paths=artifact_paths or [],
        )


def _ref_arg(arguments: dict[str, Any]) -> str:
    for key in ("ref", "name", "label", "text", "target"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return value
    return ""
