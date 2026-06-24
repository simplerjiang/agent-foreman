from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from foreman.client.tools import PMToolRuntime, ToolCall
from foreman.client.tools.models import EXTERNAL_WEB, ToolRuntimeConfig


class _BrowserHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"""<!doctype html>
<html><head><title>PM Browser Runtime Lab</title></head>
<body>
  <h1>PM Browser Runtime Lab</h1>
  <p id="count">Counter: 0</p>
  <button onclick="document.getElementById('count').textContent='Counter: 1'">Increment counter</button>
  <input aria-label="Note" id="note">
  <button onclick="document.getElementById('saved').textContent='Saved note: '+document.getElementById('note').value">Save note</button>
  <p id="saved"></p>
</body></html>"""
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _serve() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _BrowserHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/"


async def test_browser_runtime_open_click_type_extract_screenshot(tmp_path: Path):
    server, url = _serve()
    rt = PMToolRuntime(
        ToolRuntimeConfig(
            workspace=tmp_path,
            allowed_roots=[tmp_path],
            browser=True,
            browser_headless=True,
        )
    )
    try:
        opened = await rt.call(ToolCall("open", "browser_open", {"url": url}))
        assert opened.ok and opened.taint == [EXTERNAL_WEB]
        snapshot = await rt.call(ToolCall("snap", "browser_snapshot", {}))
        assert snapshot.ok
        refs = {item["name"]: item["ref"] for item in snapshot.data["elements"]}
        assert "Increment counter" in refs and "Save note" in refs and "Note" in refs

        clicked = await rt.call(
            ToolCall("click", "browser_click", {"input": {"ref": refs["Increment counter"]}})
        )
        assert clicked.ok
        typed = await rt.call(
            ToolCall("type", "browser_type", {"ref": refs["Note"], "text": "browser works"})
        )
        assert typed.ok
        saved = await rt.call(ToolCall("save", "browser_click", {"ref": refs["Save note"]}))
        assert saved.ok

        text = await rt.call(ToolCall("text", "browser_extract_text", {}))
        assert "Counter: 1" in text.data["text"]
        assert "Saved note: browser works" in text.data["text"]
        shot = await rt.call(ToolCall("shot", "browser_screenshot", {}))
        assert shot.ok and shot.artifact_paths and Path(shot.artifact_paths[0]).exists()
    finally:
        await rt.aclose()
        server.shutdown()


async def test_browser_runtime_blocks_unlisted_remote_origin(tmp_path: Path):
    rt = PMToolRuntime(
        ToolRuntimeConfig(workspace=tmp_path, allowed_roots=[tmp_path], browser=True)
    )
    try:
        result = await rt.call(ToolCall("open", "browser_open", {"url": "https://example.com"}))
        assert result.ok is False and result.error == "origin_not_allowed"
    finally:
        await rt.aclose()
