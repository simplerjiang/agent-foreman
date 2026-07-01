# Context v2 基线调查

日期：2026-07-01

本文件记录 `docs/CONTEXT_V2_IMPLEMENTATION_TASKS.zh-CN.md` Commit 0 的基线结果。此轮不改业务逻辑，只确认现有实现边界、替换点和基线测试状态。

## 当前实现确认

- `Event.id` 当前是 `str` 主键，定义在 `src/foreman/client/store/models.py`。
- `Store.get_events(session_id)` 当前按 `Event.ts` 排序，尚未按 `(ts, id)` 做稳定复合排序。
- `ContextSnapshot` 和 `MemoryItem` 是 v1 派生缓存；raw `Event` 仍是事实来源。
- `Checkpoint` 当前是 git 工作区 undo 快照，不能复用为上下文 checkpoint。
- `DispatchService.compact()` 当前从 `events_to_text(rows)` 生成 timeline，写入 `Session.plan`，并追加 `ContextSnapshot` / `MemoryItem` / `context_compact` event。
- PM plan/review 当前主路径仍通过 `_safe_pm_launch()` 收集 `Session.plan` 和 `events_to_text()` 文本上下文，再调用 `PMAgent.plan()` / `PMAgent.review()`。
- `pm_agent.events_to_text()` 已跳过 `pm_output` / `pm_reasoning`，但仍是 legacy 文本拼接路径。
- `submit_plan_tool_spec()` 当前手写 required 字段，包含 `instruction`。
- `validate_final_plan()` 当前独立手写校验，空 `instruction` 无条件返回 `final_plan_missing_instruction`。
- direct reply 已存在简单短路和 `kind="direct_reply"` 字段，但 output contract、schema、validator 仍不是同一规则源。
- `PMToolRuntime` 当前已有 `list_files/read_file/search_repo/write_file/replace_in_file/run_command/fetch_url/web_search/browser_* / ask_question / work_mode_*`。
- PM tools 当前缺少 Context v2 第一批要求的 `include_globs/exclude_globs`、`run_command.cwd/timeout_ms/env/risk_hint`、`write_file/replace_in_file dry_run/diff_preview`、`get_repo_status/get_file_tree/git_diff/detect_project`。
- `LLMClient` 当前没有 `/responses/compact` adapter。
- `Runner` 当前只保存 live handle 基本字段；`agent_start` 事件由 subprocess adapter 发送，payload 包含 `pid/command/cwd/model/effort`，但未结构化保存 `worktree/branch/base_ref/head_sha/status/native_session_id`。
- `HookReceiver` 当前可把 Claude `PreToolUse/PostToolUse/Stop/SubagentStop/Notification` 映射为 `tool_pre/tool_post/stop/notification` events。
- Server 当前只有旧 `POST /api/sessions/{session_id}/compact`，内部调用 `dispatcher.compact()`；没有 Context v2 usage/checkpoint API。

## 后续替换点

1. 在 store 层新增 `ContextFrame`、`ContextCheckpoint`、`Session.latest_context_checkpoint_id`、v3 migration 和原子安装 API。
2. 新增 Context v2 materializer，把 raw Event 幂等物化为稳定 frame，cursor 比较必须使用 `(event_ts, event_id)`。
3. 把 `submit_plan` schema 和 `validate_final_plan()` 收敛到同一个 `PlanContract`。
4. 新增 `ContextManager.build_active_context()`，PM plan/review 主路径改用固定 envelope；`events_to_text()` 只保留为 degraded fallback。
5. compact 从 `Session.plan + ContextSnapshot` 升级为 `ContextCheckpoint.replacement_history_json`，优先 remote `/responses/compact`，失败 fallback local compact。
6. 后续再补 PM tool surface、runtime state、Context API/UI 和真实 DB 验证。

## Commit 1 addendum

- 当前 `CLIENT_MIGRATIONS` 最高版本是 v2，所以 `Session.latest_context_checkpoint_id` 必须作为 v3 migration 添加。
- 当前 `context_budget` 已有 70% auto compact 和 every-8-run compact，但还没有 hard threshold，也没有 `ContextUsage` / per-lane telemetry。

## 基线测试

命令：

```powershell
$env:PYTHONPATH='src'
python -m pytest tests/test_context_compression.py tests/test_context_p1b.py tests/test_dispatch_service.py tests/test_web_page.py
```

结果：

- `128 passed`
- `1 warning`: FastAPI/TestClient 的既有 `StarletteDeprecationWarning`
