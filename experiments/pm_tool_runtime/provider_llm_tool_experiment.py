from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from foreman.shared.config import load_config
from foreman.shared.llm import LLMClient, Message

from pm_tool_runtime_experiment import MiniPMToolRuntime, RuntimeConfig, ToolResult


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(r"E:\AutoWorkAgent\config.yaml")
RESULTS_DIR = Path(__file__).resolve().parent / "results"
WORKSPACE_DIR = Path(__file__).resolve().parent / "provider_workspace"


TOOL_PURPOSES = {
    "list_files": "列出 workspace 内文件, 供 PM 派发前了解项目结构。",
    "read_file": "读取 workspace 内文件内容或行范围。",
    "search_repo": "在 workspace 内搜索文本证据。",
    "write_file": "在 workspace 内创建或覆盖文件, 默认生产配置关闭。",
    "replace_in_file": "对 workspace 内文件做唯一匹配替换, 多匹配失败。",
    "run_command": "运行命令, 证明 PM 可做本地验证; shell 生产默认关闭。",
    "fetch_url": "抓取 URL 文本并标记 external_web_content taint。",
    "web_search": "执行 web search; 实验中为 synthetic provider, 结果标记 external_web_content。",
}


class _FetchHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib handler method
        body = b"provider experiment local fetch payload"
        self.send_response(200)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002, N802 - stdlib signature
        return


def _start_fetch_server() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _FetchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _prepare_workspace() -> None:
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (WORKSPACE_DIR / "nested").mkdir(exist_ok=True)
    (WORKSPACE_DIR / "nested" / "info.txt").write_text("alpha nested\n", encoding="utf-8")
    generated = WORKSPACE_DIR / "generated.txt"
    if generated.exists():
        generated.unlink()


def _tool_call_prompt(tool: str, arguments: dict[str, Any]) -> str:
    return (
        "You are GPT-5.5 acting as the Foreman PM planner in a tool-call protocol experiment. "
        "Do not use real external tools. Your only job is to emit the requested JSON tool call. "
        "Return exactly one JSON object and no markdown. "
        "Schema: {\"type\":\"tool_calls\",\"tool_calls\":[{\"id\":\"call_<tool>\","
        "\"name\":\"<tool>\",\"arguments\":{...}}],\"decision_notes\":[\"...\"]}. "
        f"Required tool name: {tool}. "
        f"Use exactly these arguments: {json.dumps(arguments, ensure_ascii=False)}."
    )


async def _complete(client: LLMClient, model: str, prompt: str) -> str:
    return await client.complete(
        [Message("system", "Return only valid JSON."), Message("user", prompt)],
        json_mode=True,
        model=model,
    )


def _json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", stripped, flags=re.S)
    if not match:
        return {}
    obj = json.loads(match.group(0))
    return obj if isinstance(obj, dict) else {}


def _result_summary(result: ToolResult) -> str:
    if result.error:
        return result.error
    if result.name == "list_files":
        return f"{len(result.data.get('files', []))} files"
    if result.name == "read_file":
        return f"{len(result.data.get('text', ''))} chars"
    if result.name == "search_repo":
        return f"{len(result.data.get('matches', []))} matches"
    if result.name == "write_file":
        return f"{result.data.get('bytes', 0)} bytes written"
    if result.name == "replace_in_file":
        return f"match_count={result.data.get('match_count')}"
    if result.name == "run_command":
        stdout = str(result.data.get("stdout") or "").strip().splitlines()
        stdout_note = f" stdout={stdout[0]!r}" if stdout else ""
        return f"exit {result.data.get('returncode')}{stdout_note}"
    if result.name == "fetch_url":
        return f"{len(result.data.get('text', ''))} chars fetched"
    if result.name == "web_search":
        return f"{len(result.data.get('results', []))} synthetic results"
    return "ok"


def _one_line(value: object, *, limit: int = 180) -> str:
    text = str(value or "").replace("\r", "\\r").replace("\n", "\\n")
    return text[:limit] + "..." if len(text) > limit else text


def _result_detail(result: ToolResult) -> str:
    if result.name == "list_files":
        return ", ".join(result.data.get("files", [])[:5])
    if result.name == "read_file":
        return f"path={result.data.get('path')}; text={_one_line(result.data.get('text'))}"
    if result.name == "search_repo":
        matches = result.data.get("matches", [])
        return "; ".join(f"{m.get('path')}:{m.get('line')}" for m in matches[:5])
    if result.name == "write_file":
        return f"path={result.data.get('path')}; bytes={result.data.get('bytes')}"
    if result.name == "replace_in_file":
        return f"match_count={result.data.get('match_count')}"
    if result.name == "run_command":
        return (
            f"returncode={result.data.get('returncode')}; "
            f"stdout={_one_line(result.data.get('stdout'))}; "
            f"stderr={_one_line(result.data.get('stderr'))}"
        )
    if result.name == "fetch_url":
        return (
            f"url={result.data.get('url')}; "
            f"content_type={result.data.get('content_type')}; "
            f"text={_one_line(result.data.get('text'))}"
        )
    if result.name == "web_search":
        results = result.data.get("results", [])
        titles = "; ".join(str(item.get("title") or "") for item in results[:5])
        return f"query={result.data.get('query')}; titles={titles}"
    return json.dumps(result.data, ensure_ascii=False)


def _markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# PM Tool Runtime GPT-5.5 Provider Experiment",
        "",
        f"Date: {payload['date']}",
        "",
        "## Provider",
        "",
        f"- provider: `{payload['provider']['provider']}`",
        f"- base_url: `{payload['provider']['base_url']}`",
        f"- model: `{payload['provider']['model']}`",
        f"- transport: `{payload['provider']['transport']}`",
        f"- api_key_set: `{payload['provider']['api_key_set']}`",
        "",
        "## Results",
        "",
        (
            "| Tool | Purpose | Code implemented | LLM emitted expected call | "
            "Runtime call had result | Runtime ok | Result summary | Result detail | Risk | Taint |"
        ),
        "|---|---|---:|---:|---:|---:|---|---|---|---|",
    ]
    for row in payload["rows"]:
        lines.append(
            "| {tool} | {purpose} | {code_implemented} | {llm_expected_call} | "
            "{runtime_had_result} | {runtime_ok} | {summary} | {detail} | {risk} | {taint} |".format(
                tool=row["tool"],
                purpose=row["purpose"],
                code_implemented="yes" if row["code_implemented"] else "no",
                llm_expected_call="yes" if row["llm_expected_call"] else "no",
                runtime_had_result="yes" if row["runtime_had_result"] else "no",
                runtime_ok="yes" if row["runtime_ok"] else "no",
                summary=str(row["summary"]).replace("|", "\\|"),
                detail=str(row["detail"]).replace("|", "\\|"),
                risk=row["risk"],
                taint=", ".join(row["taint"]) or "-",
            )
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This is an isolated experiment for PM-agent tools, not production Foreman runtime.",
            "- Secrets are not written to the report; only `api_key_set` is recorded.",
            "- `web_search` uses the synthetic experiment provider, so the LLM-provider part validates tool-call understanding, not public search quality.",
        ]
    )
    return "\n".join(lines) + "\n"


async def run_provider_experiment() -> dict[str, Any]:
    _prepare_workspace()
    cfg = load_config(DEFAULT_CONFIG)
    model = cfg.llm.model or "gpt-5.5"
    runtime = MiniPMToolRuntime(
        RuntimeConfig(
            workspace=WORKSPACE_DIR,
            file_write=True,
            shell=True,
            web_fetch=True,
            web_search=True,
            max_output_chars=8000,
        )
    )
    client = LLMClient(cfg)
    server = _start_fetch_server()
    fetch_url = f"http://127.0.0.1:{server.server_port}/payload.txt"
    tool_args: dict[str, dict[str, Any]] = {
        "list_files": {"path": ".", "max_items": 20},
        "read_file": {"path": "notes.txt", "start_line": 1, "end_line": 2},
        "search_repo": {"query": "alpha", "max_matches": 10},
        "write_file": {"path": "generated.txt", "text": "created by gpt-5.5 tool experiment\n"},
        "replace_in_file": {"path": "notes.txt", "old": "beta", "new": "gamma"},
        "run_command": {"command": "python --version"},
        "fetch_url": {"url": fetch_url},
        "web_search": {"query": "Foreman PM tool runtime", "max_results": 5},
    }
    rows: list[dict[str, Any]] = []
    raw_outputs: dict[str, str] = {}
    try:
        for tool, arguments in tool_args.items():
            raw = await _complete(client, model, _tool_call_prompt(tool, arguments))
            raw_outputs[tool] = raw
            obj = _json_object(raw)
            calls = obj.get("tool_calls") if isinstance(obj.get("tool_calls"), list) else []
            first = calls[0] if calls and isinstance(calls[0], dict) else {}
            expected = first.get("name") == tool and isinstance(first.get("arguments"), dict)
            result = runtime.call(first) if expected else ToolResult("", tool, False, error="bad_llm_call")
            rows.append(
                {
                    "tool": tool,
                    "purpose": TOOL_PURPOSES[tool],
                    "code_implemented": hasattr(runtime, tool),
                    "llm_expected_call": expected,
                    "runtime_had_result": result.id != "" or expected,
                    "runtime_ok": result.ok,
                    "summary": _result_summary(result),
                    "detail": _result_detail(result),
                    "risk": result.risk,
                    "taint": result.taint,
                    "error": result.error,
                    "tool_result": asdict(result),
                }
            )
    finally:
        server.shutdown()
        await client.aclose()

    payload = {
        "date": datetime.now(timezone.utc).isoformat(),
        "provider": {
            "provider": cfg.llm.provider,
            "base_url": cfg.llm.base_url,
            "model": model,
            "transport": cfg.llm.transport,
            "api_key_set": bool(cfg.secrets.llm_api_key),
        },
        "rows": rows,
        "raw_outputs": raw_outputs,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / "2026-06-23-gpt-5.5-all-tools-provider-experiment.json"
    md_path = RESULTS_DIR / "2026-06-23-gpt-5.5-all-tools-provider-experiment.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(_markdown_report(payload), encoding="utf-8", newline="\n")
    return {"json_path": str(json_path), "md_path": str(md_path), "payload": payload}


def main() -> None:
    result = asyncio.run(run_provider_experiment())
    print(json.dumps({"json_path": result["json_path"], "md_path": result["md_path"]}, indent=2))


if __name__ == "__main__":
    main()
