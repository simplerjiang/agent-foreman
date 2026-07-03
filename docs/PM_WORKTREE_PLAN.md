**worktree 管理确实应该排第一**，但不要把它做成“PM Agent 通过 `run_command` 拼 `git worktree ...`”。它应该是 Foreman 里的一个**一级 PM Tool family**：负责“发现 / 创建 / 绑定 session / 隔离执行 / review diff / promote / cleanup”的完整生命周期。

Foreman 当前定位本来就是一个本地 PM 层：负责规划、任务分发、进度跟踪、review、approval gates、手机端控制等。([GitHub][1]) 现有架构也很适合加这类工具：事件先持久化，再通过 Event Bus 分发，PM Brain、Reviewer、Gate、WebSocket 都围绕这条 timeline 运作。([GitHub][2]) 代码里已经有 `client/tools` 目录和 `ToolRuntime`，目前 PM tool 已包括文件读写、repo search、shell、web、browser、ask_question、work_mode 等能力。([GitHub][3])([GitHub][4])

## 1. 现状判断：已经有 worktree 的雏形，但还不是“管理能力”

这个项目已经在处理 worktree：#190 里提到 PM final plan 可以携带已验证的目标 workspace/worktree，Foreman 会校验路径是否在 allowlist 或是 `git worktree list --porcelain` 里注册的 worktree，然后写回 `session.workspace` 并从该 worktree 启动 coding agent。([GitHub][5]) #185 里也已经区分了 `main_workspace` 和当前 `session.workspace`，并要求分支切换时必须是已知 workspace、本地分支、clean tree。([GitHub][6])

所以现在的缺口不是“能不能用 worktree”，而是：

**PM Agent 还不能主动、安全、结构化地管理 worktree 生命周期。**

现在更像是：PM 可以“选择一个已存在的 worktree”。下一步应该变成：PM 可以“为任务分配一个隔离 workspace，并在整个任务生命周期里维护它”。

---

# 2. Worktree Tools 设计

## 设计原则

我建议把这组能力叫做 **Workspace/Worktree Manager**，不要只叫 Git tool。因为 PM Agent 关心的是“这个任务在哪个工作区执行、是否隔离、是否干净、是否可以交付”，而不是单纯执行 Git 命令。

核心原则：

1. **PM 不能随意 cwd 到任意路径。**
   继续复用现在的 allowlist / registered worktree 校验逻辑。安全说明里也强调 workspace allowlist 应该收窄，deploy/push/secrets/destructive 动作需要 approval gate。([GitHub][1])

2. **默认每个复杂任务一个独立 worktree。**
   主 checkout 作为 `main_workspace` 保持稳定，coding agent 在 `session.workspace` 对应的 worktree 里跑。

3. **创建、删除、切分支、promote 都要结构化输出。**
   不让 PM Agent 只看 shell stdout，而是返回 `{path, branch, base_sha, dirty, owner_session_id, risk}` 这种可审计结果。

4. **所有 mutation 进 timeline。**
   现有 tool loop 已经会把 tool call/result 作为事件记录，新的 worktree tool 应该沿用这套机制，而不是旁路执行。

---

## MVP Tool Set

### 2.1 `worktree_list`

**Risk:** `safe`
**作用:** 列出当前 repo 的所有 worktree，并标注 Foreman session 占用情况。

输入：

```json
{
  "main_workspace": "optional path",
  "include_status": true,
  "include_sessions": true
}
```

输出：

```json
{
  "main_workspace": "/repo/app",
  "worktrees": [
    {
      "path": "/repo/.foreman/worktrees/session-abc123",
      "branch": "foreman/abc123/add-login",
      "head_sha": "abc...",
      "is_main": false,
      "dirty": false,
      "locked": false,
      "owner_session_id": "abc123",
      "owner_task_id": "task-1",
      "exists": true
    }
  ]
}
```

PM Agent 用法：
在规划前先看有哪些可复用 workspace，避免两个 agent 写同一个目录。

---

### 2.2 `worktree_status`

**Risk:** `safe`
**作用:** 检查某个 worktree 是否干净、当前分支、ahead/behind、changed files、base ref。

输入：

```json
{
  "worktree_path": "/repo/.foreman/worktrees/session-abc123",
  "compare_to": "main"
}
```

输出：

```json
{
  "path": "/repo/.foreman/worktrees/session-abc123",
  "branch": "foreman/abc123/add-login",
  "dirty": true,
  "changed_files": [
    "src/auth/login.ts",
    "tests/auth.test.ts"
  ],
  "ahead": 2,
  "behind": 0,
  "base_ref": "main",
  "head_sha": "def..."
}
```

PM Agent 用法：
review 前、cleanup 前、继续任务前都应该调用。

---

### 2.3 `worktree_plan`

**Risk:** `safe` 或 `needs-strategy`
**作用:** 只做 dry-run 决策，不修改文件系统。它告诉 PM：应该复用现有 worktree、创建新 worktree、还是拒绝。

输入：

```json
{
  "goal": "add login flow",
  "main_workspace": "/repo/app",
  "base_ref": "main",
  "preferred_branch": "foreman/add-login",
  "reuse_policy": "reuse-clean-owned-only"
}
```

输出：

```json
{
  "decision": "create",
  "reason": "No clean owned worktree exists for this task.",
  "proposed_path": "/repo/.foreman/worktrees/abc123-add-login",
  "proposed_branch": "foreman/abc123/add-login",
  "base_ref": "main",
  "requires_approval": false,
  "risks": []
}
```

PM Agent 用法：
这是最重要的 planner tool。PM 不应该一上来就创建 worktree，而是先拿一个结构化 plan。

---

### 2.4 `worktree_create`

**Risk:** `needs-strategy`
**作用:** 创建新的 worktree 和任务分支，可选地绑定当前 session。

输入：

```json
{
  "main_workspace": "/repo/app",
  "base_ref": "main",
  "branch": "foreman/abc123/add-login",
  "path": "/repo/.foreman/worktrees/abc123-add-login",
  "bind_session": true,
  "session_id": "abc123",
  "task_id": "task-1",
  "dry_run": false
}
```

输出：

```json
{
  "created": true,
  "path": "/repo/.foreman/worktrees/abc123-add-login",
  "branch": "foreman/abc123/add-login",
  "base_ref": "main",
  "base_sha": "abc...",
  "head_sha": "abc...",
  "session_bound": true,
  "workspace_switched": true,
  "workspace": "/repo/.foreman/worktrees/abc123-add-login",
  "lease_id": "lease-xyz"
}
```

`bind_session=true` 的语义必须是：创建成功并通过验证后，立即走 `worktree_bind_session` 同一套校验与写库路径，把该 session 的 effective `workspace` 切到新 worktree。也就是说，后续 `submit_plan`、coding agent cwd、PM review 的默认 workspace 都应该是这个 worktree，而不是 `main_workspace`。如果 `bind_session=false`，`worktree_create` 只分配 worktree/lease，不切换 session workspace。

实现上不要通过通用 shell tool 暴露，而是由 WorktreeManager 内部执行：

```bash
git -C <main_workspace> worktree add -b <branch> <path> <base_ref>
```

但是 tool 层需要负责校验：

* `main_workspace` 必须在 allowlist 内；
* `path` 必须在允许的 worktree root 内；
* `branch` 必须符合 Foreman 命名策略；
* 目标路径不能已存在，除非明确 reuse；
* branch 已存在时只能复用 clean 且 owned 的 worktree；
* 创建后必须再次用 `git worktree list --porcelain` 验证。

---

### 2.5 `worktree_bind_session`

**Risk:** `safe` 或 `needs-strategy`
**作用:** 把 session 的 effective workspace 切到某个已验证 worktree。

输入：

```json
{
  "session_id": "abc123",
  "worktree_path": "/repo/.foreman/worktrees/abc123-add-login",
  "reason": "Run coding agent in isolated workspace."
}
```

输出：

```json
{
  "bound": true,
  "session_id": "abc123",
  "main_workspace": "/repo/app",
  "workspace": "/repo/.foreman/worktrees/abc123-add-login",
  "previous_workspace": "/repo/app"
}
```

这个 tool 是切换 effective workspace 的唯一权威入口：它必须更新 `session.workspace`，保留 `session.main_workspace` 作为原始 checkout/fallback，并让本 session 之后的 PM tools、`submit_plan(workspace=...)`、coding agent 启动 cwd 都默认指向绑定后的 worktree。不能只返回一个推荐路径，也不能只让 PM 在自然语言计划里记住它。

这个能力现在已经被 final plan 间接触发了；我建议把它显式 tool 化。这样 PM Agent 的 planning transcript 会更清楚：它不是“神秘地把 workspace 写进 plan”，而是先经过验证、绑定、再 submit plan。#190 现在的验收项也正好覆盖了 session workspace 更新、agent 从 worktree cwd 运行、拒绝任意 cwd 等行为。([GitHub][5])

---

### 2.6 `worktree_diff`

**Risk:** `safe`
**作用:** 给 PM review 用，结构化返回相对 base 的 diff summary。

输入：

```json
{
  "worktree_path": "/repo/.foreman/worktrees/abc123-add-login",
  "base_ref": "main",
  "include_patch": false
}
```

输出：

```json
{
  "base_ref": "main",
  "changed_files": [
    {
      "path": "src/auth/login.ts",
      "status": "modified",
      "additions": 42,
      "deletions": 8
    }
  ],
  "summary": "Auth login flow and tests changed.",
  "patch_artifact_path": ".foreman/tool-logs/diff-abc123.patch"
}
```

PM Agent 用法：
review coding agent 输出时，不能只看 agent 的自然语言总结，要看真实 diff。

---

### 2.7 `worktree_cleanup`

**Risk:**

* clean 且已 released：`needs-strategy`
* dirty / unmerged / unknown owner：`requires-approval`

作用：删除已完成或废弃 worktree，或者只做 dry-run。

输入：

```json
{
  "worktree_path": "/repo/.foreman/worktrees/abc123-add-login",
  "mode": "remove-if-clean",
  "dry_run": true
}
```

输出：

```json
{
  "would_remove": true,
  "safe": true,
  "dirty": false,
  "branch_merged": true,
  "requires_approval": false
}
```

真实删除时使用：

```bash
git -C <main_workspace> worktree remove <path>
git -C <main_workspace> worktree prune
```

但有几条硬规则：

* dirty worktree 不自动删；
* branch 未合并不自动删；
* 不是 Foreman 创建 / lease 的 worktree 不自动删；
* 删除前生成 diff/checkpoint artifact；
* destructive cleanup 一律可走 approval gate。

---

### 2.8 `worktree_promote`

**Risk:** `requires-approval`
**作用:** 把完成的 worktree 变成交付物：commit、patch、PR draft、merge request、或者只生成 handoff summary。

输入：

```json
{
  "worktree_path": "/repo/.foreman/worktrees/abc123-add-login",
  "mode": "prepare-pr",
  "base_ref": "main",
  "title": "Add login flow",
  "dry_run": true
}
```

输出：

```json
{
  "mode": "prepare-pr",
  "branch": "foreman/abc123/add-login",
  "commits": [],
  "diff_artifact_path": ".foreman/artifacts/abc123.patch",
  "handoff_summary": "..."
}
```

这里要非常保守：README 也明确说 push/deploy/destructive 操作要经过 approval gate。([GitHub][1]) 所以 `worktree_promote` 的默认行为应该是 **prepare**，不是自动 push/merge。

---

# 3. 推荐的 Worktree 生命周期

PM Agent 的理想调用链：

```text
用户给任务
  ↓
worktree_list
  ↓
worktree_plan
  ↓
worktree_create(bind_session=true)
  ↓
session.workspace = created_worktree
  ↓
worktree_status
  ↓
submit_plan(workspace=<created_worktree>)
  ↓
coding agent 在 worktree 中执行
  ↓
PM review:
  worktree_status
  worktree_diff
  test_run / quality_gate
  ↓
worktree_promote(dry_run=true)
  ↓
用户 approval
  ↓
promote / cleanup
```

这样 Foreman 的 session 有一个清晰状态：

```text
main_workspace = 原始 repo checkout
workspace      = 当前任务 worktree
branch         = foreman/<session>/<slug>
owner          = session_id / task_id
status         = active | reviewing | released | stale | removed
```

---

# 4. 数据模型建议

目前 store model 里已经有 Session、Task、Event、Action、Audit、DecisionCard、Checkpoint、Definition、WorkflowRun、ConfigKV 等概念。([GitHub][7]) 对 worktree 来说，建议新增一张表，而不是只塞进 `Session.workspace`。

建议表：`WorktreeLease`

```text
WorktreeLease
- id
- repo_root
- main_workspace
- worktree_path
- branch
- base_ref
- base_sha
- head_sha
- session_id
- task_id
- owner_agent
- status: active | released | stale | removed
- dirty: bool
- locked: bool
- created_at
- updated_at
- last_seen_at
- metadata_json
```

为什么要 lease：

1. PM 要知道哪个 worktree 被哪个 session 占用；
2. cleanup 需要知道“这是 Foreman 创建的，还是用户自己的”；
3. 多 agent 并发时需要避免撞 workspace；
4. UI 可以展示 Worktrees panel；
5. 未来做 “traffic control for concurrent agents writing same workspace” 时可以直接复用。Roadmap 里也已经提到多机 routing、MCP integration、policy learning，以及并发 agent 写同一 workspace 的 conflict/traffic control。([GitHub][8])

---

# 5. 代码落点建议

我会这样落：

```text
src/foreman/client/core/worktree_manager.py
src/foreman/client/tools/worktree.py
src/foreman/client/store/models.py
src/foreman/client/store/migrations.py
src/foreman/client/tools/runtime.py
```

### `WorktreeManager`

负责纯业务逻辑：

```python
class WorktreeManager:
    def list(self, main_workspace): ...
    def status(self, worktree_path): ...
    def plan(self, goal, main_workspace, base_ref): ...
    def create(self, main_workspace, base_ref, branch, path): ...
    def bind_session(self, session_id, path): ...
    def diff(self, path, base_ref): ...
    def cleanup(self, path, mode, dry_run): ...
```

内部执行 Git 时不要走 PM 的通用 `run_command`。原因是：

* 通用 shell 结果不结构化；
* 不好做 allowlist / lease / ownership 约束；
* 不好在 UI 展示 worktree 状态；
* 不好区分 safe / needs-strategy / requires-approval；
* 不好写稳定测试。

但可以复用现有 `run_command` 的一些治理模式：Gate、Auditor、approval、tool log、timeout、artifact path。现有 runtime 已经有 shell 风险分类、approval、审计和 `.foreman/tool-logs` 输出机制。([GitHub][4])

### `PMToolRuntime.specs()`

新增 specs：

```text
worktree_list
worktree_status
worktree_plan
worktree_create
worktree_bind_session
worktree_diff
worktree_cleanup
worktree_promote
```

### `ToolRuntimeConfig`

新增配置：

```python
git_worktree: bool = True
worktree_roots: list[str] = []
worktree_branch_prefix: str = "foreman/"
default_base_ref: str = "HEAD"
require_clean_for_branch_switch: bool = True
require_approval_for_cleanup: bool = True
```

### 默认 worktree 路径策略

推荐默认：

```text
<main_workspace_parent>/.foreman-worktrees/<repo-name>/<session-id>-<slug>
```

或者 repo 内：

```text
<main_workspace>/.foreman/worktrees/<session-id>-<slug>
```

我更倾向于 **repo 外 sibling root**，避免 tools/search/test 意外扫描 worktree 目录。但本地实现上要注意 allowlist 覆盖这个 root。

---

# 6. PM Agent Prompt 规则

给 PM Agent 的 system/developer prompt 应该补几条硬规则：

```text
When a coding task may modify files, prefer an isolated worktree.
Before selecting a workspace, call worktree_list or worktree_plan.
Never use run_command to create/remove/switch worktrees unless the dedicated worktree tool is unavailable.
Never bind a session to an arbitrary path.
After worktree_create(bind_session=true) or worktree_bind_session succeeds, treat the bound worktree as the session workspace for all later PM tools, submit_plan, and coding agent cwd.
Do not remove dirty or unmerged worktrees without explicit approval.
Do not push, merge, deploy, or delete branches unless the user explicitly approved it.
Before final review, inspect worktree_status and worktree_diff.
```

这个很重要。否则 PM Agent 即使有 tools，也可能继续用 `run_command` 偷懒。

---

# 7. 验收测试建议

至少加这些测试：

1. **create inside allowed root succeeds**
   创建 worktree 后，`git worktree list --porcelain` 能看到它。

2. **arbitrary path rejected**
   PM 传 `/tmp/random` 或不在 allowlist 的路径，必须拒绝。

3. **session workspace bound**
   `session.workspace` 更新到 worktree，`main_workspace` 保持原值。

4. **coding agent cwd is worktree**
   dispatch 后 agent runner 的 cwd 是 worktree path。#190 已经把这个作为验收点。([GitHub][5])

5. **dirty cleanup requires approval**
   有未提交变更时，`worktree_cleanup` 不得直接删除。

6. **same worktree cannot be leased by two write sessions**
   除非显式 read-only，否则拒绝复用。

7. **deleted worktree fallback is explicit**
   worktree 消失时可以 fallback 到 `main_workspace`，但不要静默让 coding agent 继续在 main checkout 里改代码。#185 已经提到 worktree 不存在时 fallback 逻辑。([GitHub][6])

---

# 8. 除了 worktree，还值得给 PM Agent 做什么 tools？

我会按优先级这样排。

## P0：Repo Intelligence Tools

PM Agent 的核心是规划和 review，所以它需要比普通 coding agent 更强的 repo 理解能力。

### `repo_map`

生成项目结构、主要模块、入口点、测试目录、配置文件。

```json
{
  "depth": 3,
  "include_languages": true,
  "include_test_dirs": true
}
```

### `symbol_search`

按函数、类、接口、路由、组件查找。

```json
{
  "query": "LoginController",
  "kind": "class|function|route|component"
}
```

### `impact_analysis`

给一个目标变更，返回可能影响的文件、测试、风险点。

```json
{
  "goal": "add login flow",
  "changed_files_hint": []
}
```

为什么优先：PM Agent 要写好的 coding prompt，就必须知道该让 coding agent 改哪里、测哪里、别碰哪里。

---

## P0：Test / Quality Tools

现在 PM review 如果只看 agent 自述，会很脆。应该让 PM 有结构化质量工具。

### `test_discover`

识别项目测试命令：

```json
{
  "workspace": "...",
  "scope": "unit|integration|all"
}
```

输出：

```json
{
  "commands": [
    "npm test",
    "pytest",
    "go test ./..."
  ],
  "confidence": 0.82,
  "evidence_files": ["package.json", "pyproject.toml"]
}
```

### `test_run`

结构化执行测试，返回失败摘要和 log artifact。

```json
{
  "command": "npm test -- auth",
  "timeout_s": 120
}
```

### `quality_gate`

一键跑 lint/typecheck/test/build：

```json
{
  "profile": "default",
  "scope": "changed-files"
}
```

输出应包括：

```json
{
  "passed": false,
  "failed_steps": ["typecheck"],
  "summary": "...",
  "artifacts": ["..."]
}
```

Roadmap 里已经有 QA rubrics、code standards、workflow/skills/definition engine 的方向，这组 tool 可以直接成为那些定义的执行层。([GitHub][8])

---

## P0：Git Review / Checkpoint Tools

Roadmap 早期就提到 Checkpoint Manager：每步前自动 git snapshot。([GitHub][8]) 既然项目里已有 checkpoint 概念，建议把它暴露成 PM Agent 的结构化工具。

### `git_status`

比 shell `git status` 更结构化。

### `git_diff_summary`

按文件、风险、测试覆盖总结 diff。

### `checkpoint_create`

PM 在 dispatch coding agent 前创建 checkpoint。

### `checkpoint_restore`

高风险，需要 approval。用于 agent 改坏时回滚。

### `patch_export`

把 worktree diff 导出为 artifact，方便 handoff 或 PR。

---

## P1：Session / Agent Orchestration Tools

PM Agent 不应该让 coding agent 自己“再启动一个 agent”。这个 repo 的 PM prompt 里也有不要让 coding agent 启动其他 agent 的倾向。更好的做法是：PM 自己有 agent orchestration tools。

### `agent_start`

启动一个 coding agent 子任务。

```json
{
  "workspace": "...",
  "agent": "claude|codex|...",
  "prompt": "...",
  "model": "...",
  "effort": "medium"
}
```

### `agent_resume`

继续某个 agent session。

### `agent_stop`

停止失控或卡住的 agent。

### `agent_compare`

同一任务让两个 agent 给方案，但不同时写同一个 workspace。可以配合 worktree lease。

### `task_split`

把用户目标拆成多个子任务，每个子任务绑定独立 worktree。

这会让 Foreman 从“单 agent PM”走向真正的 multi-agent foreman。

---

## P1：Conflict / Lock Tools

这个和 worktree 强相关。

### `workspace_lock`

锁定 workspace，避免两个 coding agent 同时写。

### `file_conflict_check`

PM 派发任务前检查目标文件是否被其他 session 修改。

### `merge_risk_check`

判断两个 worktree 的 diff 是否可能冲突。

输出示例：

```json
{
  "conflict_risk": "high",
  "overlapping_files": [
    "src/auth/session.ts"
  ],
  "recommendation": "Run tasks sequentially or split by module."
}
```

这正好对应 roadmap 里未来的并发 agent traffic control。([GitHub][8])

---

## P1：Issue / PR Tools

这些工具不一定一开始就要能写远程系统。先做 read / prepare，再做 approval-gated write。

### `issue_read`

读取 GitHub issue / Linear ticket / Jira ticket。

### `issue_extract_requirements`

把 issue 转成 acceptance criteria。

### `pr_prepare`

生成 PR title/body/checklist，但不自动 push。

### `pr_review`

读取 diff + CI 状态 + review comments，给 PM 决策。

### `release_notes_draft`

根据 merged changes 生成 release note。

任何外部写操作，比如 comment、create PR、push branch，都应该 approval-gated。

---

## P1：Evidence / Timeline Query Tools

Foreman 的架构是事件 timeline 驱动。PM Agent 应该能问自己的历史，而不是只靠当前上下文窗口。

### `event_query`

查询当前 session timeline：

```json
{
  "session_id": "abc123",
  "types": ["tool_result", "agent_output", "approval"],
  "query": "test failure"
}
```

### `session_summary`

生成当前 session 的事实摘要。

### `artifact_read`

读取 tool logs、screenshots、patches、test reports。

### `decision_history`

查看之前为什么 approve/reject 某个动作。

这会显著增强 PM review 和 recovery 能力。架构文档里已有 Action/Audit/DecisionCard/Gate 等概念，适合进一步 tool 化。([GitHub][2])

---

## P2：Environment / Runtime Tools

这些工具帮助 PM 判断任务能不能跑，而不是让 coding agent 盲试。

### `env_probe`

识别语言、包管理器、运行命令、端口、数据库依赖。

### `dependency_install_plan`

只生成安装计划；真实安装走 approval。

### `service_start`

启动 dev server，高风险时 approval。

### `port_check`

检查本地端口占用。

### `log_tail`

读取 dev server/test server 日志。

---

## P2：Security / Safety Tools

PM Agent 要能 review 危险变更。

### `secret_scan`

扫描 diff 是否引入 secret。

### `dangerous_change_scan`

检查是否改了 auth、payment、permissions、deploy、infra 等敏感区域。

### `dependency_vuln_audit`

检查新增依赖风险。

### `license_check`

检查新增依赖 license。

### `migration_risk_check`

检查数据库 migration 是否 destructive。

---

## P2：Workflow / Definition Tools

项目 roadmap 里有 definition engine、workflow、skills、code standards、QA rubrics、plugin discovery 等方向。([GitHub][8]) 这些可以变成 PM Agent 的流程工具。

### `workflow_start`

按 repo 定义启动某个流程，比如 “bugfix workflow”。

### `workflow_next_step`

告诉 PM 下一步该 plan、dispatch、test、review 还是 approval。

### `rubric_evaluate`

按 QA rubric 检查结果。

### `definition_link`

把某个 project rule、coding standard、runbook 链接到 session。

---

# 9. 我建议的开发顺序

## Milestone 1：Worktree 管理最小闭环

先做：

```text
worktree_list
worktree_status
worktree_plan
worktree_create
worktree_bind_session
```

目标：PM 能为新任务创建隔离 worktree，并让 coding agent 从那里启动。

## Milestone 2：Review + Cleanup

再做：

```text
worktree_diff
worktree_cleanup
checkpoint_create
git_diff_summary
test_discover
test_run
```

目标：PM 能 review 真实变更、跑测试、清理 workspace。

## Milestone 3：并发与交付

再做：

```text
workspace_lock
merge_risk_check
worktree_promote
pr_prepare
quality_gate
```

目标：多个 coding agent 并发时不互相踩，完成后能准备 PR/handoff。

## Milestone 4：PM 智能增强

最后做：

```text
repo_map
impact_analysis
event_query
session_summary
workflow_next_step
rubric_evaluate
```

目标：PM 不只是调工具，而是能基于 repo 和历史做更好的计划与复盘。

---

# 10. 连续开发任务拆分

下面的拆分按“每个任务都可以独立 review、独立回滚、独立补测”的粒度设计。每一步都默认遵守本仓库规约：从最新 `origin/main` 新建隔离 worktree，做最小改动，提交前二次阅读 requirement 和 diff；如果进入 PR/merge/release 阶段，再按当时最新 `main` 领取版本号和补版本说明。

## 全局门禁

每个任务完成前必须满足：

1. `git status --short` 只包含本任务预期文件。
2. 新增代码路径有至少一个失败可复现的测试或明确覆盖本任务验收点的测试。
3. 二次阅读新写代码，确认没有把 main checkout、任意路径、dirty cleanup、push/merge/delete 等危险动作绕过 Gate。
4. PM-visible 状态不能只靠自然语言：workspace、worktree、branch、lease、dirty、approval 等关键事实必须结构化保存或结构化事件化。
5. 如果没有真实 E2E 证据，只能留下 `needs-e2e` handoff，不能声称已经端到端完成。

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

如果改了 UI 或用户可见流程，除了 pytest，还要创建 `needs-e2e` review issue，写清入口、验收点、截图/日志证据；没有真实点击测试时不能移除 `needs-e2e`。

---

# 11. 最核心的一句话

**Worktree tool 不应该是 Git 命令包装器，而应该是 Foreman 的“任务隔离与 workspace 生命周期管理器”。**

我会优先实现这个最小 API：

```text
worktree_list
worktree_plan
worktree_create
worktree_bind_session
worktree_status
worktree_diff
worktree_cleanup
```

然后紧接着做：

```text
test_run
git_diff_summary
checkpoint_create
workspace_lock
repo_map
impact_analysis
```

这样 PM Agent 会从“能写计划的 agent”升级成“能安全分配工作区、派发任务、审查真实变更、管理交付风险的本地工程 PM”。

[1]: https://github.com/simplerjiang/agent-foreman "GitHub - simplerjiang/agent-foreman: Self-hosted PM helper for coding-agent workflows on your PC and phone. / 自托管 PM 管家，让电脑和手机一起管理编码 Agent 工作流。 · GitHub"
[2]: https://github.com/simplerjiang/agent-foreman/blob/main/docs/ARCHITECTURE.md "agent-foreman/docs/ARCHITECTURE.md at main · simplerjiang/agent-foreman · GitHub"
[3]: https://github.com/simplerjiang/agent-foreman/tree/main/src/foreman/client/tools "agent-foreman/src/foreman/client/tools at main · simplerjiang/agent-foreman · GitHub"
[4]: https://raw.githubusercontent.com/simplerjiang/agent-foreman/main/src/foreman/client/tools/runtime.py "raw.githubusercontent.com"
[5]: https://github.com/simplerjiang/agent-foreman/issues/190 "[Review] PM 选择 worktree 后同步会话 workspace · Issue #190 · simplerjiang/agent-foreman · GitHub"
[6]: https://github.com/simplerjiang/agent-foreman/issues/185 "[Review] 会话 worktree 状态与分支切换 · Issue #185 · simplerjiang/agent-foreman · GitHub"
[7]: https://raw.githubusercontent.com/simplerjiang/agent-foreman/main/src/foreman/client/store/models.py "raw.githubusercontent.com"
[8]: https://raw.githubusercontent.com/simplerjiang/agent-foreman/main/docs/ROADMAP.md "raw.githubusercontent.com"
