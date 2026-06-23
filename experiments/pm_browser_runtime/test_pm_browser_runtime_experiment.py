from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from pm_browser_runtime_experiment import BrowserRuntimeConfig, MiniPMBrowserRuntime


HTML = """<!doctype html>
<html>
  <head><title>PM Browser Runtime Lab</title></head>
  <body>
    <h1>PM Browser Runtime Lab</h1>
    <p id="counter">Counter: 0</p>
    <button id="increment" onclick="document.getElementById('counter').textContent='Counter: 1'">
      Increment counter
    </button>
    <label for="note">Task note</label>
    <input id="note" placeholder="enter note" />
    <button id="save" onclick="document.getElementById('saved').textContent='Saved note: ' + document.getElementById('note').value">
      Save note
    </button>
    <p id="saved">Saved note: none</p>
  </body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def _serve() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_browser_runtime_reads_clicks_types_and_screenshots(tmp_path: Path) -> None:
    server = _serve()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with MiniPMBrowserRuntime(
            BrowserRuntimeConfig(
                artifacts_dir=tmp_path / "artifacts",
                allowed_origins=(origin,),
                headless=True,
            )
        ) as runtime:
            opened = runtime.call(
                {"id": "open", "name": "browser_open", "arguments": {"url": origin}}
            )
            snapshot = runtime.call({"id": "snapshot", "name": "browser_snapshot", "arguments": {}})
            increment_ref = next(
                item["ref"]
                for item in snapshot.data["elements"]
                if item["name"] == "Increment counter"
            )
            note_ref = next(
                item["ref"] for item in snapshot.data["elements"] if item["name"] == "Task note"
            )
            save_ref = next(
                item["ref"] for item in snapshot.data["elements"] if item["name"] == "Save note"
            )
            clicked = runtime.call(
                {"id": "click", "name": "browser_click", "arguments": {"ref": increment_ref}}
            )
            typed = runtime.call(
                {
                    "id": "type",
                    "name": "browser_type",
                    "arguments": {"ref": note_ref, "text": "PM browser runtime works"},
                }
            )
            saved = runtime.call(
                {"id": "save", "name": "browser_click", "arguments": {"ref": save_ref}}
            )
            text = runtime.call({"id": "read", "name": "browser_extract_text", "arguments": {}})
            shot = runtime.call(
                {
                    "id": "shot",
                    "name": "browser_screenshot",
                    "arguments": {"full_page": True},
                }
            )
    finally:
        server.shutdown()

    assert opened.ok is True
    assert opened.data["title"] == "PM Browser Runtime Lab"
    assert snapshot.ok is True
    assert len(snapshot.data["elements"]) == 3
    assert clicked.ok is True
    assert typed.ok is True
    assert saved.ok is True
    assert "Counter: 1" in text.data["text_excerpt"]
    assert "Saved note: PM browser runtime works" in text.data["text_excerpt"]
    assert shot.ok is True
    assert Path(shot.data["path"]).exists()
    assert shot.data["bytes"] > 1000
    assert len(shot.data["sha256"]) == 64


def test_browser_runtime_blocks_unlisted_origin(tmp_path: Path) -> None:
    with MiniPMBrowserRuntime(
        BrowserRuntimeConfig(
            artifacts_dir=tmp_path / "artifacts",
            allowed_origins=("http://127.0.0.1:1",),
            headless=True,
        )
    ) as runtime:
        result = runtime.call(
            {"id": "open", "name": "browser_open", "arguments": {"url": "http://127.0.0.1:2"}}
        )

    assert result.ok is False
    assert result.error == "origin_not_allowed"
