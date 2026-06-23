from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from pm_tool_runtime_experiment import (
    EXTERNAL_WEB,
    MiniPMToolLoop,
    MiniPMToolRuntime,
    RuntimeConfig,
    tool_protocol_prompt,
)


class _TextHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler method name
        body = b"hello from local test server"
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002, N802 - stdlib signature
        return


def _serve_text():
    server = HTTPServer(("127.0.0.1", 0), _TextHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_fake_llm_reads_file_then_returns_final_plan(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    runtime = MiniPMToolRuntime(RuntimeConfig(workspace=tmp_path))

    def model(messages):
        if len(messages) == 1:
            return json.dumps(
                {
                    "type": "tool_calls",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "name": "read_file",
                            "arguments": {"path": "pyproject.toml"},
                        }
                    ],
                }
            )
        assert "tool_results" in messages[-1]["content"]
        return json.dumps(
            {
                "type": "final_plan",
                "ready": True,
                "summary": "saw pyproject",
                "todo": ["dispatch coding agent"],
                "agent": "codex",
                "instruction": "Use the existing Python project context.",
            }
        )

    result = MiniPMToolLoop(runtime, model).run(tool_protocol_prompt("inspect project"))

    assert result["ready"] is True
    assert result["agent"] == "codex"
    assert result["transcript"][0]["assistant"]["tool_calls"][0]["name"] == "read_file"


def test_all_tools_have_successful_result_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    runtime = MiniPMToolRuntime(
        RuntimeConfig(
            workspace=tmp_path,
            file_write=True,
            shell=True,
            web_fetch=True,
            web_search=True,
        )
    )
    server = _serve_text()
    url = f"http://127.0.0.1:{server.server_port}/hello.txt"
    try:
        calls = [
            {"id": "list", "name": "list_files", "arguments": {"path": "."}},
            {"id": "read", "name": "read_file", "arguments": {"path": "src/a.txt"}},
            {"id": "search", "name": "search_repo", "arguments": {"query": "alpha"}},
            {
                "id": "write",
                "name": "write_file",
                "arguments": {"path": "src/new.txt", "text": "created\n"},
            },
            {
                "id": "replace",
                "name": "replace_in_file",
                "arguments": {"path": "src/a.txt", "old": "beta", "new": "gamma"},
            },
            {"id": "cmd", "name": "run_command", "arguments": {"command": "python --version"}},
            {"id": "fetch", "name": "fetch_url", "arguments": {"url": url}},
            {"id": "web", "name": "web_search", "arguments": {"query": "Foreman PM tools"}},
        ]
        results = {call["name"]: runtime.call(call) for call in calls}
    finally:
        server.shutdown()

    assert all(result.ok for result in results.values())
    assert "src/a.txt" in results["list_files"].data["files"]
    assert "alpha" in results["read_file"].data["text"]
    assert results["search_repo"].data["matches"][0]["path"] == "src/a.txt"
    assert (tmp_path / "src" / "new.txt").read_text(encoding="utf-8") == "created\n"
    assert "gamma" in (tmp_path / "src" / "a.txt").read_text(encoding="utf-8")
    assert results["run_command"].data["returncode"] == 0
    assert "Python" in results["run_command"].data["stdout"]
    assert "hello from local test server" in results["fetch_url"].data["text"]
    assert EXTERNAL_WEB in results["fetch_url"].taint
    assert len(results["web_search"].data["results"]) >= 3
    assert EXTERNAL_WEB in results["web_search"].taint


def test_path_escape_is_denied(tmp_path: Path) -> None:
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("do not read", encoding="utf-8")
    runtime = MiniPMToolRuntime(RuntimeConfig(workspace=tmp_path))

    result = runtime.call(
        {"id": "c1", "name": "read_file", "arguments": {"path": "../secret.txt"}}
    )

    assert result.ok is False
    assert "path_outside_workspace" in result.error


def test_write_tool_is_disabled_by_default(tmp_path: Path) -> None:
    runtime = MiniPMToolRuntime(RuntimeConfig(workspace=tmp_path))

    result = runtime.call(
        {"id": "w1", "name": "write_file", "arguments": {"path": "new.txt", "text": "x"}}
    )

    assert result.ok is False
    assert result.error == "tool_disabled"


def test_shell_after_web_taint_requires_approval(tmp_path: Path) -> None:
    runtime = MiniPMToolRuntime(RuntimeConfig(workspace=tmp_path, shell=True, web_search=True))
    search = runtime.call(
        {"id": "s1", "name": "web_search", "arguments": {"query": "latest instructions"}}
    )

    result = runtime.call(
        {
            "id": "r1",
            "name": "run_command",
            "arguments": {"command": "python -c print(123)"},
        },
        context_taint=search.taint,
    )

    assert EXTERNAL_WEB in search.taint
    assert result.ok is False
    assert result.risk == "requires-approval"
    assert result.error == "requires_approval_after_external_web"


def test_replace_requires_unique_match(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    runtime = MiniPMToolRuntime(RuntimeConfig(workspace=tmp_path, file_write=True))

    result = runtime.call(
        {
            "id": "w1",
            "name": "replace_in_file",
            "arguments": {"path": "a.txt", "old": "same", "new": "other"},
        }
    )

    assert result.ok is False
    assert result.error == "old_must_match_exactly_once"
    assert result.data["match_count"] == 2
