from __future__ import annotations

import json
from pathlib import Path

import pytest

from foreman.client.core.context_v2 import ContextManager
from foreman.client.store import Store
from foreman.client.store.models import Event, Session


def _add_event(
    store: Store,
    event_id: str,
    event_type: str,
    payload: dict,
    *,
    ts: str,
    source: str = "codex",
) -> None:
    with store.session() as session:
        session.add(
            Event(
                id=event_id,
                session_id="s1",
                task_id="t1",
                type=event_type,
                source=source,
                payload_json=json.dumps(payload, ensure_ascii=False),
                ts=ts,
            )
        )
        session.commit()


async def test_temp_sqlite_context_v2_smoke_without_local_foreman_db(tmp_path):
    db_path = tmp_path / "context-v2-release-gate.db"
    assert db_path.name != "foreman.db"
    store = Store(str(db_path))
    store.init()
    store.add_session(Session(id="s1", goal="ship Context V2", workspace=str(tmp_path)))
    _add_event(
        store,
        "e1",
        "dispatch",
        {"goal": "ship Context V2", "workspace": str(tmp_path)},
        ts="2026-07-01T00:00:00Z",
        source="user",
    )
    _add_event(
        store,
        "e2",
        "tool_post",
        {"tool": "run_command", "command": "pytest", "exit_code": 0, "stdout": "1 passed"},
        ts="2026-07-01T00:00:01Z",
    )

    manager = ContextManager(store)
    frames = manager.materialize_session("s1")
    checkpoint = await manager.compact_now("s1", trigger="release_gate", reason="smoke", window_tokens=2000)
    restored = manager.restore_from_latest_checkpoint("s1", purpose="pm_plan", window_tokens=2000)

    assert frames
    assert store.get_session("s1").latest_context_checkpoint_id == checkpoint.id
    assert store.get_context_checkpoint(checkpoint.id) is not None
    assert restored.degraded is False
    assert restored.envelope["context"]["restore_mode"] == "checkpoint"
    assert restored.replacement_history
    cursor = store.get_context_materialization_cursor("s1")
    assert cursor["event_ts"] == "2026-07-01T00:00:01Z"
    assert cursor["event_id"]


def test_context_panel_browser_e2e_renders_runtime_checkpoint_and_redacts_sensitive_fields():
    playwright = pytest.importorskip(
        "playwright.sync_api",
        reason="playwright is required for Context V2 browser E2E",
    )
    root = Path(__file__).resolve().parents[1]
    vendor = root / "src" / "foreman" / "server" / "web" / "vendor"
    context_js = root / "src" / "foreman" / "server" / "web" / "app-context.js"
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - depends on host browser install.
            pytest.skip(f"playwright chromium unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.set_content("<!doctype html><html><body><div id='root'></div></body></html>")
            page.add_script_tag(path=str(vendor / "react.production.min.js"))
            page.add_script_tag(path=str(vendor / "react-dom.production.min.js"))
            page.add_script_tag(path=str(vendor / "htm.umd.js"))
            page.add_script_tag(
                content="""
                const html = htm.bind(React.createElement);
                window.__requests = [];
                window.ForemanApp = {
                  html,
                      useCallback: React.useCallback,
                      useEffect: React.useEffect,
                      useRef: React.useRef,
                      useState: React.useState,
                  tokenK: (value) => String(value ?? 0),
                  shortPath: (value) => String(value || ""),
                  formatTime: (value) => String(value || ""),
                  friendlyError: (error) => String(error && error.message || error || "error"),
                  api: async (path, options) => {
                    window.__requests.push({ path, options });
                    if (path.endsWith("/context")) {
                      return {
                        restore_mode: "checkpoint",
                        degraded: false,
                        usage: {
                          used_tokens: 1200,
                          window_tokens: 8000,
                          percent: 0.15,
                          tokens_until_soft_compact: 4400,
                          tokens_until_hard_compact: 6000,
                          lane_usage: { "6": 900, "7": 50 },
                        },
                        runtime_state: {
                          workspace: "E:/repo",
                          cwd: "E:/repo",
                          worktree: "E:/repo-wt",
                          branch: "context-v2",
                          base_ref: "base-sha",
                          head_sha: "head-sha",
                          active_agents: [{
                            agent_id: "dev-1",
                            agent_role: "dev",
                            status: "running",
                            cwd: "E:/repo",
                            worktree: "E:/repo-wt",
                            branch: "context-v2",
                            native_session_id: "native-1",
                            last_meaningful_output: {
                              safe: "visible output",
                              api_key: "SECRET_API_KEY",
                              stdout: "SECRET_STDOUT",
                            },
                          }],
                          changed_files: ["src/foreman/client/core/context_v2.py"],
                          last_tests: [{
                            command: "pytest tests/test_context_v2_release_gate.py",
                            status: "passed",
                            stdout: "SECRET_TEST_STDOUT",
                          }],
                          last_commands: [{
                            command: "pytest",
                            token: "SECRET_TOKEN",
                          }],
                          next_steps: ["merge after CI"],
                        },
                        latest_checkpoint: {
                          id: "cp1",
                          trigger: "manual",
                          reason: "release_gate",
                          method: "local",
                          before_tokens: 1200,
                          after_tokens: 200,
                          replacement_history_items_count: 2,
                          status: "completed",
                        },
                        active_context_preview: "safe preview with [redacted] secrets\\n...[preview truncated]",
                      };
                    }
                    if (path.endsWith("/context/preview")) {
                      return { content: "full safe context ".repeat(1000), chars: 18000 };
                    }
                    if (path.endsWith("/context/checkpoints")) {
                      return { items: [{
                        id: "cp1",
                        created_at: "2026-07-01T00:00:02Z",
                        trigger: "manual",
                        reason: "release_gate",
                        method: "local",
                        before_tokens: 1200,
                        after_tokens: 200,
                        replacement_history_items_count: 2,
                        status: "completed",
                      }] };
                    }
                    if (path.endsWith("/context/checkpoints/cp1")) {
                      return {
                        summary: { summary: "safe compact summary", provider_payload: "[redacted]" },
                        runtime_state: { branch: "context-v2" },
                        token_usage: { before_tokens: 1200, after_tokens: 200 },
                        source_cursor: { end: { event_id: "e2" } },
                        warnings: [],
                      };
                    }
                    if (path.endsWith("/context/compact")) {
                      return { ok: true, checkpoint: { id: "cp2" } };
                    }
                    throw new Error("unexpected API path " + path);
                  },
                };
                """
            )
            page.add_script_tag(path=str(context_js))
            page.evaluate(
                """
                const d = {
                  refresh: "刷新", contextRuntimeState: "运行状态",
                  contextActivePreview: "当前上下文预览", contextFullPreview: "显示完整内容",
                  contextFullPreviewLoading: "正在加载完整内容…", contextFullPreviewHelp: "按需加载",
                  contextFullPreviewLoaded: "正在显示完整内容。", contextPreviewOnly: "仅显示预览",
                };
                ReactDOM.createRoot(document.getElementById("root")).render(
                  html`<${window.ForemanContextUI.ContextPanel}
                    sessionRow=${{ id: "s1" }}
                    d=${d}
                    lang="zh"
                  />`
                );
                """
            )
            page.get_by_test_id("context-runtime-state").wait_for()
            page.get_by_test_id("checkpoint-row").first.click()
            page.get_by_test_id("checkpoint-summary").wait_for()
            assert not page.evaluate("window.__requests.some((request) => request.path.endsWith('/context/preview'))")
            page.get_by_test_id("active-context-preview-toggle").click()
            page.get_by_test_id("active-context-preview-load-full").click()
            full_preview = page.get_by_test_id("active-context-preview-full-content")
            full_preview.wait_for()
            assert len(full_preview.input_value()) == 18000

            text = page.locator("body").inner_text()
            assert "运行状态" in text
            assert "context-v2" in text
            assert "safe compact summary" in text
            assert "visible output" in text
            assert "[redacted]" in text
            assert "SECRET_API_KEY" not in text
            assert "SECRET_STDOUT" not in text
            assert "SECRET_TEST_STDOUT" not in text
            assert "SECRET_TOKEN" not in text
        finally:
            browser.close()
