# PM Worktree 连续开发任务拆分

本文件从 `PM_WORKTREE_PLAN.md` 拆出，专门维护连续开发任务、验收标准、测试标准和 E2E 标准。

下面的拆分按“每个任务都可以独立 review、独立回滚、独立补测”的粒度设计。每一步都默认遵守本仓库规约：从最新 `origin/main` 新建隔离 worktree，做最小改动，提交前二次阅读 requirement 和 diff；如果进入 PR/merge/release 阶段，再按当时最新 `main` 领取版本号和补版本说明。

## 全局门禁

每个任务完成前必须满足：

1. `git status --short` 只包含本任务预期文件。
2. 新增代码路径有至少一个失败可复现的测试或明确覆盖本任务验收点的测试。
3. 二次阅读新写代码，确认没有把 main checkout、任意路径、dirty cleanup、push/merge/delete 等危险动作绕过 Gate。
4. PM-visible 状态不能只靠自然语言：workspace、worktree、branch、lease、dirty、approval 等关键事实必须结构化保存或结构化事件化。
5. 如果没有真实 E2E 证据，只能留下 `needs-e2e` handoff，不能声称已经端到端完成。

## 全局 E2E 标准

每个任务的“测试标准”都继承本节。E2E 可以只覆盖本任务新增或修改的代码路径，不要求每一步都跑完整产品回归；但必须是从公开入口或相邻真实边界进入，而不是只测一个孤立函数。

1. **代码级 E2E 适用范围:** store/model/tool/dispatch/runner/context 这类底层改动，可以用真实 SQLite Store、真实 git repo/worktree、真实 `PMToolRuntime` 或 `DispatchService`，配 fake PM/fake runner 跑完整链路。证据要覆盖“输入 -> 新代码路径 -> 持久化/事件/runner 参数/结构化输出”。
2. **产品级 E2E 适用范围:** 改了 UI、API、用户可见 session/workspace 状态、approval card、浏览器流程或 exe 行为时，必须用浏览器/Playwright/`foreman-e2e` 或 packaged exe smoke 走真实用户入口。只验证新改路径即可，但必须有截图、事件日志或 API 响应证据。
3. **工作区类 E2E:** 涉及 worktree 创建、绑定、fallback、cleanup、diff、lock 的任务，至少要创建真实临时 git repo，并用 `git worktree add` 生成真实 worktree；普通临时目录不能替代 worktree E2E。
4. **安全类 E2E:** 涉及删除、branch 操作、push/merge/deploy、dirty cleanup、approval 的任务，必须包含拒绝路径 E2E：dirty/unowned/out-of-root/缺失路径等危险输入不能产生副作用。
5. **证据标准:** 最终说明或 review issue 必须列出 E2E 入口、操作步骤、期望行为、实际结果、命令输出或截图/日志 artifact。没有跑 E2E 时，要说明阻塞原因并保留 `needs-e2e`，不能写成通过。

## T0：当前行为基线与防回归

**目标:** 在写新工具前固定现有 session/worktree 行为，防止后续重构把已有能力打坏。

**改动范围:**

```text
tests/test_dispatch_service.py
tests/test_runner.py
tests/test_context_v2_*.py
docs/PM_WORKTREE_PLAN.md 或 docs/WORKTREE_TEST_NOTES.md
```

**验收标准:**

1. 已有 session 的 `workspace` 是仍存在的 worktree 时，follow-up PM dispatch 传给 PM 的 workspace 是该 worktree。
2. PM plan 返回已登记 git worktree 时，`session.workspace` 切到 worktree，`session.main_workspace` 保持原始 checkout。
3. runner/subprocess handle 的 `cwd` 和 `worktree` 都是 worktree 路径，不是 main checkout。
4. worktree 丢失时 fallback 到 `main_workspace` 是显式行为，并且 timeline/UI 能看出 fallback，不静默继续在 main 写代码。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_dispatch_service.py::test_existing_session_allows_recorded_worktree_outside_allowlist `
       tests/test_dispatch_service.py::test_existing_session_rejects_missing_recorded_worktree_outside_allowlist `
       tests/test_runner.py -q
```

新增或补强测试必须用真实 `git worktree add` 覆盖 `_resolve_plan_workspace()` 的登记校验，不只用普通临时目录假装 worktree。

## T1：WorktreeLease 数据模型与迁移

**目标:** 先有可审计的 ownership/lease 基础，后续 create/bind/cleanup 都不能只靠 `Session.workspace`。

**改动范围:**

```text
src/foreman/client/store/models.py
src/foreman/client/store/migrations.py
src/foreman/client/store/db.py
tests/test_store_migrations.py
tests/test_client_store.py
```

**验收标准:**

1. 新增 `WorktreeLease` 表，字段覆盖 `repo_root/main_workspace/worktree_path/branch/base_ref/base_sha/head_sha/session_id/task_id/status/dirty/locked/created_at/updated_at/last_seen_at/metadata_json`。
2. 迁移幂等：旧 DB 升级后既保留 `session.workspace/main_workspace`，又能创建 lease。
3. lease 状态至少支持 `active | released | stale | removed`。
4. DB helper 支持按 `session_id`、`worktree_path`、`status` 查询 active lease。
5. 不改变已有 session 创建、任务派发、context restore 的行为。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_store_migrations.py tests/test_client_store.py -q
```

测试必须覆盖：fresh DB、旧 schema DB、重复运行迁移、同一路径 active lease 唯一性或冲突检测。

## T2：WorktreeManager 只读发现与状态

**目标:** 建立纯业务层，不暴露给 PM tool 之前先把 git/worktree 解析做稳定。

**改动范围:**

```text
src/foreman/client/core/worktree_manager.py
tests/test_worktree_manager.py
```

**验收标准:**

1. `list(main_workspace)` 解析 `git worktree list --porcelain`，返回 main 与所有 worktree 的 `path/branch/head_sha/locked/exists`。
2. `status(worktree_path, compare_to)` 返回 `dirty/changed_files/ahead/behind/base_ref/head_sha`。
3. 路径必须 normalize/resolve，但输出保留可读路径；Windows drive/path separator 不导致误判。
4. 非 git repo、损坏 worktree、缺失路径返回结构化错误，不抛未处理异常。
5. 只读方法不得写 DB、不得修改文件系统。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_worktree_manager.py -q
```

测试必须用真实 git repo 覆盖：clean worktree、dirty worktree、locked worktree、deleted worktree、branch ahead/behind。

## T3：worktree_list / worktree_status PM tools

**目标:** 先把只读能力接进 `PMToolRuntime`，让 PM 可以看见工作区事实。

**改动范围:**

```text
src/foreman/client/tools/runtime.py
src/foreman/client/tools/models.py
src/foreman/client/tools/policy.py
tests/test_pm_tools.py
```

**验收标准:**

1. `PMToolRuntime.specs()` 出现 `worktree_list`、`worktree_status`，risk 都是 `safe`。
2. tool input 只接受 allowed root 或 session-owned worktree，任意路径返回结构化拒绝。
3. tool result 进入已有 `tool_pre/tool_post` timeline，不走旁路日志。
4. 返回值包含 lease/session 占用信息；没有 lease 时明确 `owner_session_id=""`。
5. 外部 web taint、shell approval、write tools 逻辑不受影响。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_pm_tools.py tests/test_dispatch_service.py::test_pm_agent_tool_loop_persists_tool_events_before_launch -q
```

新增测试必须断言 tool schema、risk、拒绝任意路径、tool events 顺序。

## T4：worktree_plan dry-run 决策

**目标:** PM 创建或复用 worktree 前必须先拿结构化 plan，避免直接 mutation。

**改动范围:**

```text
src/foreman/client/core/worktree_manager.py
src/foreman/client/tools/runtime.py
tests/test_worktree_manager.py
tests/test_pm_tools.py
```

**验收标准:**

1. `worktree_plan` 不改 DB、不创建目录、不创建 branch。
2. 输出 `decision/create|reuse|reject`、`proposed_path`、`proposed_branch`、`base_ref`、`requires_approval`、`risks`。
3. 默认路径使用 repo 外 sibling root，且必须在配置的 `worktree_roots` 或可派生 allowlist 内。
4. slug/branch 命名稳定，非法字符被拒绝或规范化。
5. 已存在 clean owned worktree 时按 `reuse_policy` 复用；dirty/unowned worktree 默认 reject。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_worktree_manager.py tests/test_pm_tools.py -q
```

测试必须断言 dry-run 后 `git worktree list --porcelain`、DB lease 数量、文件系统目录都没有变化。

## T5：worktree_create 最小 mutation

**目标:** 允许 PM 创建隔离 worktree，但默认仍不做 push/merge/delete。

**改动范围:**

```text
src/foreman/client/core/worktree_manager.py
src/foreman/client/tools/runtime.py
src/foreman/client/tools/models.py
tests/test_worktree_manager.py
tests/test_pm_tools.py
```

**验收标准:**

1. `worktree_create(dry_run=true)` 与 `worktree_plan` 一样不修改文件系统。
2. `dry_run=false` 时内部执行 `git worktree add -b <branch> <path> <base_ref>`，不经通用 `run_command`。
3. 创建前校验 `main_workspace` allowlist、`path` worktree root、branch prefix、目标路径不存在。
4. 创建后再次用 `git worktree list --porcelain` 验证路径登记成功。
5. 创建成功写入 active `WorktreeLease`，记录 `base_sha/head_sha/session_id/task_id`。
6. branch 已存在时只允许复用 clean 且 owned 的 worktree；否则结构化拒绝。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_worktree_manager.py tests/test_pm_tools.py -q
```

测试必须覆盖：成功创建、路径越界、branch 前缀非法、目标已存在、base ref 不存在、git 命令失败回滚 lease。

## T6：worktree_bind_session 与 workspace 切换

**目标:** 把“worktree 已创建”变成“本 session 后续真的在 worktree 执行”的硬契约。

**改动范围:**

```text
src/foreman/client/core/worktree_manager.py
src/foreman/client/core/dispatch_service.py
src/foreman/client/tools/runtime.py
tests/test_dispatch_service.py
tests/test_pm_tools.py
```

**验收标准:**

1. `worktree_bind_session` 是切换 effective workspace 的唯一权威入口。
2. bind 成功必须更新 `session.workspace=<worktree>`，保留 `session.main_workspace=<original checkout>`。
3. `worktree_create(bind_session=true)` 创建成功后必须调用同一绑定逻辑；`bind_session=false` 不切 workspace。
4. 绑定后 PM follow-up、PM tools、coding agent launch cwd、`agent_input.cwd/worktree` 全部指向 worktree。
5. 绑定任意路径、缺失路径、非登记 worktree、unowned dirty worktree 都拒绝。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_dispatch_service.py tests/test_pm_tools.py tests/test_context_v2_subagents.py -q
```

必须新增两个真实 git worktree 测试：一个验证 PM plan 选择 worktree 后 subagent cwd 是 worktree；一个验证已有 session workspace 已是 worktree 时 follow-up 仍在 worktree。

## T7：PM prompt 与 submit_plan 规则收紧

**目标:** 让 PM Agent 知道什么时候必须用 dedicated worktree tools，而不是继续用自然语言或 shell 偷懒。

**改动范围:**

```text
src/foreman/client/core/pm_agent.py
src/foreman/client/core/pm_contract.py
tests/test_pm_agent.py
tests/test_pm_contract.py
```

**验收标准:**

1. PM system/developer prompt 明确：会改文件的任务优先 worktree；创建/删除/切换 worktree 不得用 `run_command`，除非 dedicated tool 不可用。
2. `submit_plan(workspace=...)` 只能引用已验证/已绑定的 workspace，不允许任意路径。
3. PM direct answer 场景不创建 worktree、不启动 subagent。
4. prompt 中保留 `main_workspace` 与 `workspace` 的区别，避免 PM 把 main 当作当前执行目录。
5. 现有 `direct_reply`、tool loop、planning stream 行为不回退。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_pm_agent.py tests/test_pm_contract.py tests/test_pm_direct_reply_validator.py -q
```

测试必须断言 prompt 包含 worktree tool 规则，且 direct reply 不触发 runner launch。

## T8：UI / API 可见性

**目标:** 用户能看见 session 当前在哪个 worktree、原始 main 在哪、branch 是什么，以及是否处于 fallback。

**改动范围:**

```text
src/foreman/server/app.py
src/foreman/server/web/app.js
src/foreman/server/web/app-context.js
tests/test_local_api.py
tests/test_web_page.py
```

**验收标准:**

1. `/api/sessions` 或 workspace status API 返回 `workspace/main_workspace/worktree/branch/lease_status/fallback_reason`。
2. 前端工作区状态显示当前 worktree 与 branch；main workspace 只作为 fallback/原始 checkout 显示。
3. worktree 缺失时 UI 明确提示，不把 main checkout 伪装成当前 worktree。
4. 新对话没有 worktree 时显示 “worktree: none”，不误报。
5. 文案中英文一致，且不硬编码版本号。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_local_api.py tests/test_web_page.py -q
```

涉及实际浏览器交互时，补 Playwright 或现有 web smoke 截图证据；没有截图不能声称 UI E2E 完成。

## T9：worktree_diff 与 review 证据

**目标:** PM review 不能只看 coding agent 自述，必须看真实 diff。

**改动范围:**

```text
src/foreman/client/core/worktree_manager.py
src/foreman/client/tools/runtime.py
src/foreman/client/core/reviewer.py 或 dispatch_service.py
tests/test_worktree_manager.py
tests/test_pm_tools.py
tests/test_reviewer.py
```

**验收标准:**

1. `worktree_diff` 返回相对 `base_ref` 的 changed files、additions/deletions、可选 patch artifact。
2. patch artifact 写在 `.foreman/tool-logs` 或明确 artifact 目录，不污染源码目录。
3. PM review 前能拿到 diff summary；大型 patch 有截断标记，不能误报完整。
4. 无 diff 时返回 clean 状态，review 不生成虚假变更。
5. 新文件、删除文件、重命名文件都有结构化状态。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_worktree_manager.py tests/test_pm_tools.py tests/test_reviewer.py -q
```

测试必须覆盖 untracked file、deleted tracked file、binary/large patch 截断、artifact 路径不越界。

## T10：worktree_cleanup 安全删除

**目标:** 完成或废弃 worktree 可以清理，但 dirty/unmerged/unknown owner 永远不能静默删除。

**改动范围:**

```text
src/foreman/client/core/worktree_manager.py
src/foreman/client/tools/runtime.py
src/foreman/client/core/gate.py
tests/test_worktree_manager.py
tests/test_pm_tools.py
tests/test_gate.py
```

**验收标准:**

1. `dry_run=true` 返回 `would_remove/safe/dirty/branch_merged/requires_approval`，不删除。
2. clean、merged、released、owned 的 worktree 可走 `needs-strategy` 清理。
3. dirty、unmerged、unknown owner、路径越界必须 `requires-approval` 或直接 reject。
4. 删除前生成 diff/checkpoint artifact。
5. 删除成功后 lease 状态变 `removed`，`git worktree prune` 只在安全路径内执行。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_worktree_manager.py tests/test_pm_tools.py tests/test_gate.py -q
```

测试必须覆盖 dirty 拒删、unmerged 拒删、unknown owner 拒删、clean merged 删除成功、重复 cleanup 幂等。

## T11：checkpoint / git_diff_summary / test_run

**目标:** 让 PM 在 dispatch 前后有可恢复点、可读 diff、可执行测试，而不是依赖 agent 自述。

**改动范围:**

```text
src/foreman/client/core/checkpoint.py
src/foreman/client/tools/runtime.py
src/foreman/client/core/decision_loop.py
tests/test_checkpoint.py
tests/test_pm_tools.py
tests/test_p4_acceptance.py
```

**验收标准:**

1. dispatch coding agent 前可创建 checkpoint，记录 session/task/worktree/branch/head_sha。
2. `git_diff_summary` 输出 changed files、风险提示、测试建议。
3. `test_discover` 从 `pyproject.toml/package.json` 等文件识别候选命令，并带 confidence/evidence。
4. `test_run` 结构化返回 exit code、stdout/stderr 摘要、失败行、log artifact。
5. `run_command` 的 Gate/Auditor 治理不能被 test tools 绕过；高风险命令仍需 approval。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_checkpoint.py tests/test_pm_tools.py tests/test_p4_acceptance.py -q
```

测试必须覆盖测试失败摘要、超时、artifact 写入、checkpoint undo 后 worktree 恢复。

## T12：workspace_lock 与并发冲突

**目标:** 多个 coding agent 不得同时写同一个 workspace，除非明确 read-only。

**改动范围:**

```text
src/foreman/client/core/worktree_manager.py
src/foreman/client/store/models.py
src/foreman/client/store/db.py
tests/test_worktree_manager.py
tests/test_dispatch_service.py
```

**验收标准:**

1. active write lease 阻止第二个 write session 绑定同一 worktree。
2. read-only session 可以共享，但 tool runtime 禁用 write/command mutation。
3. `merge_risk_check` 能列出 overlapping files 和 risk level。
4. session 结束或 cleanup 后释放/更新 lock 状态。
5. 崩溃恢复时 stale lease 可被识别，但不能自动接管 dirty worktree。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_worktree_manager.py tests/test_dispatch_service.py -q
```

测试必须覆盖两个并发 dispatch、同文件冲突、不同文件低风险、stale lease recovery。

## T13：worktree_promote / pr_prepare 只准备不发布

**目标:** 交付前生成 commit/PR/handoff 所需事实，但默认不 push、不 merge、不 deploy。

**改动范围:**

```text
src/foreman/client/core/worktree_manager.py
src/foreman/client/tools/runtime.py
tests/test_pm_tools.py
```

**验收标准:**

1. 默认 `mode=prepare-pr` 只输出 branch、commits、diff artifact、handoff summary。
2. push/merge/deploy/delete branch 必须是 explicit approval gate，且默认不可自动执行。
3. dirty 或测试失败时 handoff summary 明确风险，不生成“ready to merge”结论。
4. commit 只在用户明确选择对应 mode 后执行；不替用户自动 stage unrelated files。
5. PR body 包含 requirement review、code review、验证证据、剩余风险。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_pm_tools.py tests/test_gate.py -q
```

测试必须断言 prepare 模式不产生远端 side effect；如需要调用 `gh`，必须 mock，不打真实 GitHub。

## T14：repo_map / impact_analysis / event_query

**目标:** 最后增强 PM 智能，不阻塞 worktree 安全闭环。

**改动范围:**

```text
src/foreman/client/tools/runtime.py
src/foreman/client/core/context_v2.py
src/foreman/client/core/briefing.py
tests/test_pm_tools.py
tests/test_context_v2_*.py
```

**验收标准:**

1. `repo_map` 有文件数/目录深度/入口点/test dirs 上限，不把整个 repo 塞进上下文。
2. `impact_analysis` 输出候选文件、测试建议、风险，不声称确定性结论。
3. `event_query/session_summary/artifact_read` 只读 timeline/artifacts，不绕过权限读任意文件。
4. Context v2 压缩后仍保留 workspace/worktree/branch/active agents/关键测试结果。
5. PM prompt 使用这些工具产出的结构化事实，而不是重复长文本。

**测试标准:**

```powershell
$env:PYTHONPATH='src'
pytest tests/test_pm_tools.py tests/test_context_v2_dispatch.py tests/test_context_v2_subagents.py tests/test_context_v2_realistic_session.py -q
```

测试必须覆盖大 repo 截断、artifact 路径越界拒绝、context compact 后 worktree evidence 不丢失。

## 合并前总验证

每个阶段至少要有一个覆盖本阶段新增代码路径的 E2E 证据。底层任务可以是代码级 E2E；UI/API/用户路径必须是产品级 E2E。

任一阶段准备开 PR 或合并前，至少跑：

```powershell
$env:PYTHONPATH='src'
pytest tests/test_pm_tools.py tests/test_dispatch_service.py tests/test_local_api.py tests/test_web_page.py tests/test_store_migrations.py -q
```

如果改了 runner/adapters，再加：

```powershell
$env:PYTHONPATH='src'
pytest tests/test_runner.py tests/test_codex_adapter.py tests/test_claude_adapter.py -q
```

如果改了 context/PM review，再加：

```powershell
$env:PYTHONPATH='src'
pytest tests/test_context_v2_dispatch.py tests/test_context_v2_subagents.py tests/test_context_v2_realistic_session.py tests/test_reviewer.py -q
```

如果改了 worktree/session/agent dispatch 链路，补一条真实 git worktree 的代码级 E2E：从任务入口或 tool call 进入，断言 `session.workspace`、lease、timeline event、runner cwd/worktree 或结构化 tool result。

如果改了 UI 或用户可见流程，除了 pytest，还要创建 `needs-e2e` review issue，写清入口、验收点、截图/日志证据；没有真实点击测试时不能移除 `needs-e2e`。

---
