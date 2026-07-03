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

5. **mutation tool 的 session/task 只能来自服务端上下文。**
   `worktree_create`、`worktree_bind_session`、`worktree_cleanup`、`worktree_promote` 等会改变状态的工具不接受 PM 传入 `session_id` / `task_id`，只能使用 PM tool runtime 注入的当前 session/task。PM 不能跨 session bind、cleanup 或 promote 其它 worktree。

6. **worktree path 默认由 Foreman 生成。**
   PM 默认只能给 goal/base/branch intent，不能自由指定 filesystem path。自定义 path 只有在配置显式允许时才可用，并且必须同时通过 worktree root、symlink、normalized path、registered git worktree 校验。

7. **比较基准固定为 lease 的 `base_sha`。**
   diff、review、cleanup 的默认比较基准是 `WorktreeLease.base_sha`；`base_ref` 只作为人类可读标签和创建时输入。不得默认把会移动的 `main` / `origin/main` 当成唯一判断依据。

8. **worktree tools 不做远端 Git 操作。**
   这组工具不自动 `git fetch` / `pull` / `push` / `merge`，也不删除 remote branch。涉及远端或 destructive 操作时，必须拆成单独 approval-gated 动作。

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
  "base_sha": "abc...",
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
  "bind_session": true,
  "dry_run": false
}
```

`session_id` / `task_id` 不在输入里，由 PM tool runtime 从当前请求上下文注入。`path` 默认也不在输入里，由 Foreman 根据当前 session/task 和配置生成；只有配置显式开启自定义 path 时才允许传 `custom_path`，且必须经过 worktree root、symlink、normalized path、registered git worktree 校验。

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
* 生成的 `path` 必须在允许的 worktree root 内；
* 自定义 path 默认禁用；启用后必须通过 symlink、normalized path、worktree root 和 registered git worktree 校验；
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
  "lease_id": "lease-xyz",
  "reason": "Run coding agent in isolated workspace."
}
```

`lease_id` 只能指向当前 runtime session 允许绑定的 lease；不能绑定其它 session 的 lease，也不能通过传 path 绕过 ownership 校验。当前 session/task 仍由服务端上下文注入，不接受 PM 输入。

输出：

```json
{
  "bound": true,
  "lease_id": "lease-xyz",
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
  "lease_id": "lease-xyz",
  "include_patch": false
}
```

输出：

```json
{
  "base_ref": "main",
  "base_sha": "abc...",
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

默认比较基准必须来自 `WorktreeLease.base_sha`；`base_ref` 只是人类可读标签。除非用户明确发起 rebase/refresh 类动作并经过单独审批，diff/review 不应因为 `main` 或 `origin/main` 前进而改变历史判断。

---

### 2.7 `worktree_cleanup`

**Risk:**

* clean 且已 released：`needs-strategy`
* dirty / unmerged / unknown owner：`requires-approval`

作用：删除已完成或废弃 worktree，或者只做 dry-run。

输入：

```json
{
  "mode": "remove-if-clean",
  "dry_run": true
}
```

cleanup 默认只作用于当前 session 的 active/released lease，不接受 PM 传入任意 `worktree_path`。如果需要清理其它 session 或 unknown owner 的 worktree，必须走单独的用户确认流程，不能通过 PM tool 跨 session 删除。

输出：

```json
{
  "would_remove": true,
  "safe": true,
  "dirty": false,
  "branch_merged": true,
  "base_sha": "abc...",
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
* 删除前基于 `WorktreeLease.base_sha` 生成 diff/checkpoint artifact；
* destructive cleanup 一律可走 approval gate。
* cleanup 不自动 fetch/pull/push/merge，也不删除 remote branch。

---

### 2.8 `worktree_promote`

**Risk:** `requires-approval`
**作用:** 把完成的 worktree 变成交付物：commit、patch、PR draft、merge request、或者只生成 handoff summary。

输入：

```json
{
  "mode": "prepare-pr",
  "title": "Add login flow",
  "dry_run": true
}
```

输出：

```json
{
  "mode": "prepare-pr",
  "branch": "foreman/abc123/add-login",
  "base_ref": "main",
  "base_sha": "abc...",
  "commits": [],
  "diff_artifact_path": ".foreman/artifacts/abc123.patch",
  "handoff_summary": "..."
}
```

这里要非常保守：README 也明确说 push/deploy/destructive 操作要经过 approval gate。([GitHub][1]) 所以 `worktree_promote` 的默认行为应该是 **prepare**，不是自动 push/merge。它不接受任意 `worktree_path`，默认只准备当前 session lease 的 handoff；也不自动 `git fetch/pull/push/merge` 或删除 remote branch。

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
    def create(self, context, main_workspace, base_ref, branch): ...
    def bind_session(self, context, lease_id): ...
    def diff(self, context, lease_id=None): ...
    def cleanup(self, context, mode, dry_run): ...
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
allow_custom_worktree_path: bool = False
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
Never pass session_id or task_id to worktree mutation tools; the server runtime injects the current session/task.
Do not invent worktree paths; use Foreman-generated paths unless custom paths are explicitly enabled and validated.
After worktree_create(bind_session=true) or worktree_bind_session succeeds, treat the bound worktree as the session workspace for all later PM tools, submit_plan, and coding agent cwd.
Use the lease base_sha as the default diff/review/cleanup base; treat base_ref only as a label.
Do not remove dirty or unmerged worktrees without explicit approval.
Do not fetch, pull, push, merge, deploy, or delete local/remote branches through worktree tools; remote or destructive Git actions require separate approval-gated tools.
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

任务拆分已单独维护在 [`PM_WORKTREE_TASK_BREAKDOWN.md`](PM_WORKTREE_TASK_BREAKDOWN.md)，包括 T0-T14、验收标准、测试标准、E2E 标准和合并前总验证。

---

# 10. 最核心的一句话

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
