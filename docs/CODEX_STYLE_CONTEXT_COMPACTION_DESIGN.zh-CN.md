# Foreman 上下文压缩与多 Agent 记忆设计书（参考 openai/codex）

> 日期：2026-07-01  
> 目标：把 Foreman 当前的 PM 上下文压缩，从“事件日志截断 + 一段摘要”升级为“结构化 active history + 可恢复 checkpoint + subagent 运行态感知”。  
> 参考：`openai/codex` 源码 commit `db887d03e1f907467e33271572dffb73bceecd6b`，重点参考其 `ContextManager`、`compact`、`compact_remote`、`replacement_history`、subagent hook metadata、context usage UI。

---

## 1. 背景与问题

Foreman 当前已具备基础的 `ContextPack` / `MemoryItem` / `context_compact` 机制，但实际长会话里暴露出三个核心问题：

1. **压缩输入不可靠**  
   现有 PM 侧逻辑容易从最近事件或有限字符里拼出压缩输入。PM 自己的流式输出、reasoning、重复日志可能挤掉更重要的 subagent 启动、命令输出、worktree、测试结果等证据。

2. **压缩产物不可作为下一轮模型历史直接恢复**  
   `Session.plan` 更像一段摘要文本，不是完整的“模型可见 active history”。压缩后 PM 可能知道“有个任务”，但不知道自己在哪个 worktree、哪些 agent 做过什么、哪些 tool call/result 已经发生。

3. **subagent 缺少结构化运行态**  
   PM 对 subagent 的输入/输出、身份、父子关系、cwd/worktree/branch、transcript 路径缺少统一模型。最终导致 PM review 或下一轮 plan 只能从自然语言里猜。

本设计的目标不是止血，而是把上下文系统一次性升级到可长期运行的形态。

---

## 2. Codex 的关键做法

以下是从 `openai/codex` 源码确认到的机制，不是仅来自文档或讨论。

### 2.1 active history 是模型协议项，不是字符串日志

Codex 的 `ContextManager` 保存 `Vec<ResponseItem>`，也就是模型协议级历史。它记录 token usage、history version、reference context、world state baseline。发送给模型前调用 `for_prompt()` 做规范化。

对 Foreman 的启发：PM 不应该每次从 `events_to_text()` 临时拼字符串，而应该维护一份可直接喂模型的结构化 active context。

### 2.2 tool call 与 tool output 成对维护

Codex normalize 阶段会补缺失 output、删除孤儿 output，并在模型不支持图片时剥离图片内容。也就是说，工具调用不是普通日志，而是模型历史中的成对协议项。

对 Foreman 的启发：subagent 输入、命令调用、命令结果、测试结果要作为结构化 frame 保留，不能混进 `pm_output` 文本流后再靠摘要恢复。

### 2.3 compact 生成 checkpoint，并替换 active history

Codex compact 后会构造 `CompactedItem`，其中包含：

- `message`: 人可读 summary；
- `replacement_history`: compact 后新的模型可见历史；
- `window_number` / `window_id`: compact 窗口标识；
- `first_window_id` / `previous_window_id`: checkpoint 链路。

安装 checkpoint 时，Codex 调用 `replace_compacted_history()`，把 live history 原子替换为 `replacement_history`，并把同一份 replacement history 持久化。resume 时如果看到 `replacement_history`，直接用它恢复，而不是重新从全量日志猜。

对 Foreman 的启发：必须新增“可恢复的 active context checkpoint”层，不能只依赖 `ContextSnapshot` 或 `Session.plan` 里的摘要文本。

### 2.4 compact 有 local 与 remote 两类

Codex local compact 使用 compact prompt 让模型生成 handoff summary，再构造 replacement history。remote compact 调 `/responses/compact`，由接口直接返回新的 `ResponseItem[]`。

Foreman 当前线上 PM provider 已实测支持 OpenAI/Codex 形态的 remote compact：

- 时间：2026-07-01；
- 配置：`provider=openai`、`base_url=https://api.kongsites.com/v1`、`model=gpt-5.5`；
- 请求：`POST /v1/responses/compact`；
- 结果：HTTP 200，返回顶层 `object="response.compaction"`，并包含 `output: [ResponseItem...]`；
- 最小返回 item 类型包含 `message` 与 `compaction_summary`。

因此 Foreman 第一版不应只做本地 summary compact。正确接口形态应是：

```text
input active_history -> output replacement_history
```

而不是：

```text
input text timeline -> output summary string
```

实现策略：优先使用 provider `/responses/compact`，能力探测失败或 provider 不支持时 fallback 到 local compact。local compact 也必须输出同一套 `replacement_history_json`，不能退回只写一段摘要。

关键分层：Foreman schema 仍然由 Foreman 自己定义，`/responses/compact` 只作为语义压缩引擎。

```text
Foreman ContextFrame[] / runtime_state
  -> render provider active history input
  -> POST /responses/compact
  -> provider output ResponseItem[]
  -> write context_checkpoints:
       replacement_history_json.provider_payload = output
       summary_json = Foreman-readable summary/index
       runtime_state_json = Foreman runtime facts
       source_cursor_json = Foreman cursor
       token_usage_json = response.usage
```

不能把 provider 返回的 `compaction_summary.encrypted_content` 当作 Foreman 的人类可读 summary。Foreman 仍要自己保存 worktree、branch、active agents、changed files、tests、next steps、review cursor 等结构化语义。

### 2.5 compact 是可见 turn item

Codex 提供 `thread/compact/start` 手动触发 compact，compact 过程中会发 `contextCompaction` item started/completed。UI 能看到“context compacted”，不是静默后台魔法。

对 Foreman 的启发：UI 应该有 Context 面板、手动 Compact 按钮、checkpoint 列表与 token usage，而不是只在后端改 `session.plan`。

### 2.6 subagent 运行态显式进入 hook 输入

Codex 的 compact hook、tool hook、user prompt hook 输入包含：

- `session_id`
- `turn_id`
- `agent_id`
- `agent_type`
- `transcript_path`
- `cwd`
- `model`
- `trigger`

root agent 没有 `agent_id/agent_type`，subagent 内 hook 会带这两个字段。

对 Foreman 的启发：PM agent、Dev agent、Test agent、UI agent 的身份和工作目录必须是结构化上下文，不允许只藏在 prompt 文本里。

---

## 3. Foreman 目标架构

### 3.1 新增核心概念：ContextFrame

把所有会进入模型上下文的东西规范成 frame。事件日志仍是事实来源，但 active context 不再直接等于最后 N 条事件。

第一版 frame 是 Foreman 自己的 provider-neutral schema，不强行复制 Codex `ResponseItem`。Foreman 主要通过 hooks、stdout、事件表观察外部 CLI/subagent，不能假装拥有完整 provider tool protocol。未来如果需要，可以把 Foreman active history 再映射成 OpenAI Responses item、Chat Completions messages 或其它 provider 消息格式。

建议 frame 类型：

```text
user_message
pm_plan
pm_review
agent_start
agent_input
agent_output
tool_call
tool_result
command_call
command_result
file_change
test_result
worktree_state
decision
constraint
verified_fact
open_question
context_compaction
```

每个 frame 的基础字段：

```json
{
  "id": "frame_...",
  "session_id": "...",
  "event_id": 123,
  "turn_id": "turn_...",
  "type": "command_result",
  "role": "tool",
  "agent_id": "agent-dev-1",
  "agent_role": "dev",
  "parent_agent_id": "pm",
  "created_at": "...",
  "source_refs": ["event:123"],
  "payload": {}
}
```

subagent / worktree 相关 frame 必须额外带：

```json
{
  "cwd": "E:\\AutoWorkAgent-fix-scroll-context-worktree-20260701",
  "worktree": "E:\\AutoWorkAgent-fix-scroll-context-worktree-20260701",
  "branch": "codex/fix-scroll-context-worktree-20260701",
  "base_ref": "origin/main",
  "head_sha": "...",
  "transcript_path": "...",
  "agent_type": "dev"
}
```

### 3.2 新增核心组件：ContextManager

职责：

1. 从 raw events 增量 materialize `ContextFrame[]`。
2. 维护 session 的 active history。
3. 估算 token usage。
4. 决定是否 compact。
5. 生成并安装 checkpoint。
6. 为不同用途渲染上下文：
   - `purpose="pm_plan"`
   - `purpose="pm_review"`
   - `purpose="subagent_launch"`
   - `purpose="compact"`
   - `purpose="ui_preview"`

幂等要求：

- materializer 必须可重复运行。`ContextFrame.id` 不用随机值，使用 `session_id + event_id + frame_type + payload_hash` 稳定生成；同一个 raw event replay 多次不能重复插入 frame。
- 现有 `Event.id` 是字符串，事件读取按 `ts` 排序；checkpoint cursor 不得假设整数自增。cursor 必须保存为 JSON，例如 `{"event_ts":"...","event_id":"..."}`，比较时按 `(ts, id)`。
- compact 后继续 review 时，只处理 checkpoint `source_cursor_json` 之后的新事件；checkpoint 覆盖过的事件不能再次进入 incremental review。
- 重建旧 session 时，先找最新可恢复 `context_checkpoints` v2，replay 其后的 raw events。没有 v2 时才走 legacy `Session.plan` / `ContextPack v1`。

建议接口：

```python
class ContextManager:
    async def record_event(self, session_id: str, event: Event) -> list[ContextFrame]: ...

    async def build_active_context(
        self,
        session_id: str,
        *,
        purpose: str,
        window_tokens: int,
    ) -> ActiveContext: ...

    async def maybe_compact(
        self,
        session_id: str,
        *,
        reason: str,
        purpose: str,
        window_tokens: int,
    ) -> ContextCheckpoint | None: ...

    async def compact_now(
        self,
        session_id: str,
        *,
        trigger: str,
        window_tokens: int,
    ) -> ContextCheckpoint: ...

    async def restore_from_latest_checkpoint(self, session_id: str) -> ActiveContext: ...
```

### 3.3 新增持久化对象：ContextCheckpoint

替代“只靠 `Session.plan` 恢复”的做法。

**命名约束**：本文里的 ContextCheckpoint 是“模型上下文 checkpoint”，不是现有的 git 工作区 `Checkpoint`。Foreman 现有 `Checkpoint` 表用于操作前工作区快照和 undo，不允许混用。实现时禁止新增同名 `Checkpoint` 类或 API；DB 表名用 `context_checkpoints`，代码类型用 `ContextCheckpoint`。

建议 schema：

```json
{
  "version": 2,
  "checkpoint_id": "ctxcp_...",
  "session_id": "...",
  "trigger": "auto|manual|pre_turn|mid_turn|resume_rebuild",
  "reason": "context_limit|user_requested|model_switch|run_count|explicit_review",
  "source_cursor": {
    "start": {"event_ts": "...", "event_id": "..."},
    "end": {"event_ts": "...", "event_id": "..."}
  },
  "input_frame_ids": ["frame_1", "frame_2"],
  "summary": {
    "current_progress": [],
    "key_decisions": [],
    "constraints": [],
    "verified_facts": [],
    "open_questions": [],
    "next_steps": []
  },
  "replacement_history": [
    {
      "id": "rh_...",
      "schema": "foreman.active_history.item.v1",
      "type": "message|command_call|command_result|agent_status|runtime_state|context_summary|file_change|test_result",
      "role": "system|developer|user|assistant|tool",
      "kind": "original_goal|checkpoint_summary|runtime_state|command_result|agent_status|test_result",
      "content": "",
      "frame_ids": ["frame_..."],
      "source_refs": ["event:123"],
      "agent_id": "dev-1",
      "tool_call_id": "call_...",
      "model_visible": true,
      "payload": {}
    }
  ],
  "runtime_state": {
    "cwd": "...",
    "worktree": "...",
    "branch": "...",
    "active_agents": []
  },
  "token_usage": {
    "before_tokens": 120000,
    "after_tokens": 18000,
    "window_tokens": 272000
  },
  "created_at": "..."
}
```

关键规则：

- `replacement_history` 是恢复源，不是展示用附件。
- resume / PM plan / PM review 必须优先加载最新 checkpoint 的 `replacement_history`。
- raw events 仍保留为事实来源，用于审计和重建，但不作为每轮 prompt 的直接拼接来源。

`replacement_history` 的最低要求：

1. **可直接渲染到 PM prompt**：每条 item 必须有 `type`、`role`、`content` 或可渲染 `payload`。
2. **可追溯**：每条 item 必须保留 `frame_ids` 或 `source_refs`，不能只留下无来源结论。
3. **可观测工具成对**：Foreman 自己发起的 command/internal tool/PM action 必须通过同一个 `tool_call_id` 成对；对 Codex/Claude CLI 内部不可观测的 provider tool protocol，不强行还原，只保留 Foreman 可观测层面的 command/tool frame。
4. **runtime state 必须保留**：当前 `cwd/worktree/branch/active_agents` 不允许只存在 summary 文本里，必须是 replacement history 或 runtime_state 的结构化字段。
5. **摘要不是 history**：summary 只给人读；PM 下一轮恢复以 `replacement_history` 为准。

---

## 4. 上下文分层策略

参考 Codex “稳定前缀 + 增量历史 + compact 边界重写”的思想，Foreman 分为 7 层。

| Lane | 内容 | 是否可压缩 | 规则 |
|---|---|---:|---|
| 1 | 系统指令、工具 schema、安全约束 | 否 | 永远不进 checkpoint 摘要器逐出队列 |
| 2 | 用户目标、AGENTS.md、项目硬约束 | 否 | 进入稳定前缀，任务内不改写 |
| 3 | 当前 workspace/worktree/runtime state | 否 | 每轮可 diff 更新，但必须结构化 |
| 4 | 当前计划、当前步骤、active agents | 否 | PM 必须完整看到 |
| 5 | 长期 session memory / ContextPack | 可压缩 | 保留 top constraints / verified_facts |
| 6 | subagent/tool 详细输出 | 可压缩 | 优先保留结论、命令、exit code、测试结果 |
| 7 | streaming delta、低价值日志、重复输出 | 最先压缩 | 不应挤掉 lane 3-6 |

这解决当前问题中的关键错误：不能让 `pm_output` / `pm_reasoning` 这种 lane 7 噪声挤掉 `git worktree add`、agent_start、command_result。

### 4.1 PM active context envelope

PM plan/review 的输入必须从“长 prompt 拼接”升级为固定顶层 envelope。目标是让 PM 清楚区分用户意图、运行环境、可用 agent、工具、输出契约和校验规则，减少把纯咨询误判成代码任务，也减少 `final_plan_missing_instruction` 这类 validator 循环。

建议顶层结构：

```json
{
  "task": {
    "user_intent_type": "direct_answer|code_change|repo_inspection|browser_task|planning_only",
    "original_user_request": "...",
    "current_goal": "...",
    "task_constraints": []
  },
  "environment": {
    "cwd": "...",
    "workspace": "...",
    "worktree": "...",
    "branch": "...",
    "base_ref": "origin/main",
    "runtime_policy": {}
  },
  "agents": {
    "available": [],
    "active": []
  },
  "context": {
    "stable_prefix": [],
    "checkpoint_replacement_history": [],
    "frames_after_checkpoint": [],
    "runtime_state": {}
  },
  "tools": {
    "available": [],
    "schemas": []
  },
  "output_contract": {
    "protocol": "submit_plan_required",
    "allowed_plan_types": ["direct_reply", "dispatch", "ask_user", "inspect_only"],
    "direct_reply_instruction_required": true
  },
  "validator_rules": {
    "required_fields": [],
    "non_empty_fields": [],
    "allowed_values": {}
  }
}
```

关键规则：

1. `user_intent_type` 必须由 dispatcher/PM 前置分类产生，并进入 PM 输入。取值至少包含 `direct_answer`、`code_change`、`repo_inspection`、`browser_task`、`planning_only`。
2. `output_contract` 和 `validator_rules` 必须放在靠前、短小、稳定的位置，不允许被长工具 schema 淹没。
3. “必须调用 `submit_plan`”这类协议要求放在 envelope 前部，同时在 tool schema 里保留完整定义。
4. `direct_reply` 的规则必须显式：如果 validator 要求 `instruction` 非空，则 schema 里必须写 `direct_reply.instruction` 也要填，例如填最终回复摘要或“direct reply only”。
5. 用户原始任务、系统调度元数据、工具列表必须分区，不再混在同一段自然语言里。

### 4.2 PM tool surface 调整

现有 PM tools 不应该只靠自然语言描述。工具 schema 要表达默认行为、可选参数和安全边界。

需要补齐的现有工具：

- `read_file`: 明确 `start_line` / `end_line` 可选；不传时的默认行为必须写清楚，例如读全文但有最大行数/字节数上限。
- `search_repo`: 增加 `include_globs` / `exclude_globs`，支持限定 `*.py`、排除 `node_modules` / `dist` / `.git`。
- `run_command`: 增加 `timeout_ms`、`cwd`、`env`、`risk_hint`。`risk_hint` 用于表达 `read_only|writes_files|network|destructive_candidate`，帮助 Gate 和审计判断。
- `write_file` / `replace_in_file`: 支持 `dry_run` 或 `diff_preview`，让 PM 能先生成可审计 diff，再决定是否执行。
- browser tools: 增加等待/断言类能力，例如 `browser_wait_for_text`、`browser_assert_visible`，避免 PM 只靠截图或即时 DOM 猜测。

建议新增工具：

- `get_repo_status`: 返回 git status、当前分支、未提交变更摘要。
- `get_file_tree`: 支持递归、忽略规则、文件大小、修改时间。
- `apply_patch`: 用 patch 表达代码修改，比整文件写入更适合审计。
- `git_diff` / `read_diff`: 快速确认改动范围。
- `run_tests`: 结构化执行测试，返回 passed/failed、耗时、失败摘要。
- `detect_project`: 识别语言、包管理器、测试命令、lint 命令。
- `artifact_create`: 保存报告、截图、日志、补丁等交付物。
- `ask_user_freeform`: 用于复杂澄清；现有选择题式 `ask_question` 不覆盖所有场景。

### 4.3 输出契约与 validator 前置

PM 的最终输出契约必须和 validator 使用同一份规则源。禁止 prompt 里说“可以 direct reply”，但 validator 又暗中要求 `instruction` 非空导致二次返工。

最低要求：

1. `validation_constraints` 或 `validator_rules` 必须进入 PM 输入。
2. 每个 plan type 的必填字段、允许值、非空要求必须显式列出。
3. validator error 必须反向进入下一轮 PM 输入，作为 `previous_validation_error` frame，而不是只在日志里出现。
4. direct answer 场景应优先走 `direct_reply`，不得因为有可用 coding agent 就启动 worktree/dispatch。
5. PM 输出如果要启动 agent，必须引用 `get_repo_status` 或等价 runtime state，证明自己知道当前 cwd/worktree/branch。

---

## 5. 压缩流程设计

### 5.1 触发条件

支持三类触发：

1. **手动触发**：UI `Compact Now`。
2. **pre-turn 自动触发**：PM plan/review 前发现 usage 超阈值。
3. **run-count 自动触发**：每 N 次 PM loop 后触发，避免长会话线性膨胀。

建议默认：

```text
soft threshold: 70% window
hard threshold: 90% window
run-count threshold: 8 PM review loops
```

第一版实现策略：**全部 compact 都是 PM turn 边界上的阻塞式 compact**。soft threshold 只表示“下一轮 plan/review 前先 compact”，不做后台并发 compact。后台 compact 需要 active history version + compare-and-swap 后才能引入，否则会和 PM review/plan 同时读写 active history，造成 cursor 错位。

hard threshold 必须阻塞下一轮 PM 请求，先 compact 再继续。

### 5.2 compact 输入

compact 输入不是最后 N 条 event，而是：

1. 最新 active history。
2. 当前 runtime state。
3. active agents 列表。
4. 最近未压缩 frame。
5. 关键 anchors：
   - 用户原始目标；
   - worktree / branch / cwd；
   - agent_start / agent_stop；
   - command_call / command_result；
   - file_change；
   - test_result；
   - open questions / risks。

### 5.3 compact 输出

compact 输出必须同时有：

1. 人可读 summary。
2. 机器可恢复 replacement history。
3. source refs。
4. token before/after。
5. checkpoint metadata。

### 5.4 安装 checkpoint

安装是一个原子边界：

1. 写入 `ContextCheckpoint`。
2. 更新 session active history 指针。
3. 更新 `Session.plan` 为 checkpoint summary 的展示/兼容字段。
4. emit `context_compact` event。
5. UI 收到 `contextCompaction` started/completed。

如果 compact 失败：

- 不替换 active history；
- emit failed event；
- 保留原始上下文；
- 如果 hard threshold 已到，PM 停止并报告“上下文压缩失败，不能诚实继续”。

---

## 6. Subagent 输入输出处理

### 6.1 subagent 启动

启动 subagent 时必须写 `agent_start` frame：

```json
{
  "type": "agent_start",
  "agent_id": "dev-1",
  "agent_role": "dev",
  "parent_agent_id": "pm",
  "task": "fix scroll and context compression",
  "cwd": "E:\\AutoWorkAgent-fix-scroll-context-worktree-20260701",
  "worktree": "E:\\AutoWorkAgent-fix-scroll-context-worktree-20260701",
  "branch": "codex/fix-scroll-context-worktree-20260701",
  "base_ref": "origin/main"
}
```

PM prompt 里不用靠自然语言猜，而是渲染成明确运行态：

```text
Active agents:
- dev-1 (dev): cwd=..., branch=..., status=running
- test-1 (test): cwd=..., status=completed
```

### 6.2 subagent 输入

每次 PM 给 subagent 的任务写 `agent_input` frame：

```json
{
  "type": "agent_input",
  "agent_id": "dev-1",
  "message": "...",
  "expected_output": "patch + tests",
  "source_refs": ["event:123"]
}
```

### 6.3 subagent 输出

subagent 输出不直接作为大段文本塞进 PM prompt，而是拆成：

- `agent_output`: 高层结果；
- `command_call`: 命令；
- `command_result`: exit code、stdout/stderr 摘要、关键行；
- `file_change`: 文件、diff stat；
- `test_result`: 测试命令、pass/fail、失败摘要。

长 stdout/stderr 的策略：

1. 保留命令、exit code、cwd。
2. 保留首尾摘要。
3. 提取错误行、测试失败行、文件路径、commit/worktree/branch 相关行。
4. 原文进 raw event，不全量进 active history。

### 6.4 subagent runtime state 与完成

完成时写 `agent_output` + `agent_stop`：

```json
{
  "type": "agent_stop",
  "agent_id": "dev-1",
  "status": "completed|failed|interrupted",
  "summary": "...",
  "changed_files": [],
  "tests": [],
  "next_actions": []
}
```

压缩时必须保留每个 agent 的最后状态，但 active agents 不能只从 `agent_stop` 推导。PM 需要的是“现在还能否指挥这个 agent、它在哪个 worktree、任务是否结束”，所以 runtime_state 至少合并：

- runner/session handle：当前是否还有可交互 session；
- task status：queued/running/completed/failed/cancelled；
- heartbeat/transcript 最新时间：判断卡死、断连或长时间无输出；
- `agent_start` / `agent_stop`：结构化的起止事件；
- cwd/worktree/branch：每个 agent 当前工作目录和分支；
- last meaningful output：最后一次可摘要的 file_change / test_result / command_result。

如果 `agent_stop` 缺失，但 runner handle 或 task status 仍显示 running，PM 必须把它视为 active agent；如果 `agent_stop` 显示 completed，但 heartbeat 或 task status 后续又变化，runtime_state 要以最新可验证状态为准。

---

## 7. 与现有 Foreman 模块的映射

### 7.1 `context_compression.py`

保留 `ContextPack`，但降级为 checkpoint summary 的一个组成部分。

需要演进：

- `ContextPack v1` 继续兼容；
- 新增 `ContextCheckpoint v2`；
- `context_pack_to_text()` 只负责 summary 渲染；
- 新增 `compact_active_history()`：优先调用 provider `/responses/compact`，失败时 fallback local compact；
- 新增 `frames_to_replacement_history()`；
- 新增 `checkpoint_to_prompt_items()`。

### 7.2 `context_budget.py`

继续作为 token 预算入口。

需要补齐：

- active context token estimate；
- per-lane token usage；
- `tokens_until_compaction`；
- hard/soft threshold 判断；
- UI 用 context usage payload。

### 7.3 `pm_agent.py`

当前 `events_to_text()` 只能作为兼容 fallback。主路径应改为：

```text
ContextManager.build_active_context(session_id, purpose="pm_plan")
```

PM prompt 生成必须改为固定 envelope：

- `task`: 原始用户任务、`user_intent_type`、当前目标；
- `environment`: cwd/workspace/worktree/branch/runtime policy；
- `agents`: available/active agents；
- `context`: stable prefix、checkpoint replacement history、checkpoint 后 frames、runtime_state；
- `tools`: 可用工具与 schema；
- `output_contract`: `submit_plan` 协议、plan type、direct_reply 规则；
- `validator_rules`: required / allowed / non-empty 字段。

短期过渡规则：

- 不再先 `rows[-120:]` 再过滤；
- timeline anchor 必须保留；
- `pm_output/pm_reasoning` 属低优先级 lane 7；
- `command_execution.aggregated_output` 必须解析成 command_result frame。
- `submit_plan` 和 validator 规则必须放在 PM 输入前部，不能埋在长工具 schema 后。
- validator error 必须进入下一轮 PM 输入。

### 7.4 `dispatch_service.py`

PM loop 的上下文入口改为：

1. resolve window tokens；
2. context usage estimate；
3. maybe compact；
4. build active context；
5. PM plan/review。

compact 后必须同步：

- 重读 active context；
- 推进 reviewed cursor 到 checkpoint 的 `source_cursor_json.end`；
- 避免同一事件既在 checkpoint 又在 incremental review 里重复出现。

### 7.5 DB

第一版应新增两张 v2 表：`context_frames` 和 `context_checkpoints`。这不是现有 git `Checkpoint`，也不是直接替换 `ContextSnapshot`。

兼容策略：

1. `context_frames` / `context_checkpoints` 是 PM 主上下文恢复的新来源。
2. 继续写现有 `ContextSnapshot` / `MemoryItem` / `Session.plan` 派生物，保证旧 UI、旧 API 和旧 DB 不断。
3. `ContextSnapshot` 第一阶段只作为兼容/展示层，不承载 active history restore 的唯一真相。
4. 后续确认 v2 稳定后，再把 `ContextSnapshot` 降级为纯派生缓存或逐步迁移。

建议 schema：

```sql
CREATE TABLE context_frames (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  event_id TEXT,
  event_ts TEXT,
  turn_id TEXT,
  type TEXT NOT NULL,
  agent_id TEXT,
  agent_role TEXT,
  payload_json TEXT NOT NULL,
  source_refs_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE context_checkpoints (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  trigger TEXT NOT NULL,
  reason TEXT NOT NULL,
  source_cursor_json TEXT NOT NULL,
  input_frame_ids_json TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  replacement_history_json TEXT NOT NULL,
  runtime_state_json TEXT NOT NULL,
  token_usage_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

`Session.plan` 只保留：

- 最新 summary；
- latest checkpoint id；
- 兼容旧 UI 的简短文本。

与现有表的边界：

- `context_checkpoints`: 模型上下文 checkpoint / active history restore。
- `context_frames`: raw event 到模型上下文语义项的确定性物化层。
- `ContextSnapshot`: 兼容展示、旧 summary、旧 memory pipeline 派生缓存。
- `MemoryItem`: 从 ContextSnapshot 派生的事实、约束、决策、风险和 todo。
- `Checkpoint`: git 工作区快照，用于 action undo，不参与 PM 上下文恢复命名。

---

## 8. UI 设计

新增 `Context / 上下文` 面板：

1. context usage：
   - used tokens；
   - window tokens；
   - percent；
   - tokens until compact；
   - lane usage。
2. checkpoint 列表：
   - 时间；
   - trigger；
   - reason；
   - before/after tokens；
   - source event range。
3. checkpoint 详情：
   - summary；
   - active agents；
   - worktree；
   - key decisions；
   - next steps；
   - omitted count。
4. 操作：
   - `Compact Now`；
   - `View Raw Events`；
   - `Copy Checkpoint Summary`。

并行 UI bug：发送新消息滚动到底部属于独立修复，不是上下文架构前置。可以同 PR 验证，但不得把它作为 Context 面板或 checkpoint 的依赖条件。验收时单独断言：新消息提交后 conversation view 必须滚动到底部，不得跳顶部。

---

## 9. 实施阶段

### Phase 0：锁定兼容模型与迁移边界

交付：

- 明确 `context_frames` / `context_checkpoints` migration 名称和字段；
- 明确 event cursor 使用 `{"event_ts": "...", "event_id": "..."}`，排序按 `(ts, id)`；
- 明确 `Checkpoint` 只表示 git undo 快照，文档和代码命名避开冲突；
- 写旧 `ContextSnapshot` / `Session.plan` 的读取兼容规则；
- 定义 `ReplacementHistoryItem` schema 和渲染函数签名。

验收：

- 不新增混淆性 `Checkpoint` 表或类名；
- 旧 DB 可以启动；
- v2 frame/checkpoint schema 能被独立 round-trip。

### Phase 1：context_frames / context_checkpoints 骨架

交付：

- `context_frames` / `context_checkpoints` 表；
- `ContextFrame.id = hash(session_id, event_id, frame_type, payload_hash)`；
- raw event -> frame materializer；
- checkpoint 写入/读取；
- replacement_history / runtime_state / token_usage 独立 JSON 字段；
- 继续写旧 `ContextSnapshot` / `Session.plan` 兼容派生物。

验收：

- latest session 能从 DB 生成 deterministic frames；
- 重跑 materializer 不产生重复 frames；
- 能生成 `context_checkpoints` v2；
- 旧 UI/API 仍能读到兼容 summary。

### Phase 2：ContextManager restore 与 PM 主路径接入

交付：

- `ContextManager.build_active_context(session_id, purpose)`；
- PM active context envelope：`task/environment/agents/context/tools/output_contract/validator_rules`；
- `user_intent_type` 分类字段；
- `submit_plan` 协议与 validator 规则前置；
- direct_reply 的 `instruction` 非空规则写入 schema 和 prompt；
- stable prefix；
- latest checkpoint `replacement_history_json`；
- checkpoint cursor 之后的 raw event frames；
- 当前 runtime_state；
- tool schema 补默认行为与安全边界：`read_file` 范围、`search_repo` globs、`run_command` timeout/cwd/env/risk_hint、write dry-run/diff preview；
- `pm_agent.events_to_text()` 降级为 fallback；
- PM plan/review 使用 ContextManager。

验收：

- direct answer 请求不会误启动 coding agent；
- `final_plan_missing_instruction` 这类 validator error 不再因 schema/prompt 不一致出现；
- validator error 会进入下一轮 PM 输入；
- resume 使用 `replacement_history_json`，不是重新拼最后 N 条 event；
- PM 能看到当前 worktree/branch/cwd；
- PM 能看到每个 subagent 的 last status；
- 压缩后 PM 仍知道已改文件、已跑测试、下一步；
- compact 后下一轮 review 不重复消费 checkpoint 覆盖过的 events。

### Phase 3：阻塞式 compact 安装与失败语义

交付：

- provider `/responses/compact` adapter：输入 active history，输出 provider `output`；
- compact capability probe：缓存当前 provider/base_url/model 是否支持 remote compact；
- local compact fallback：输出同一套 `summary_json` 和 `replacement_history_json`；
- checkpoint 安装原子化：frame cursor、replacement_history、runtime_state、token_usage 同事务落库；
- soft threshold：PM turn 边界先尝试 compact；失败时记录 `context_compact_failed`，可继续但必须把失败事件放入 active context；
- hard threshold：必须阻塞 PM 请求；compact 失败则停止并把 UI 状态置为需要人工处理；
- corrupted checkpoint fallback。

验收：

- soft compact 失败不会伪装成成功；
- hard compact 失败不会继续让 PM 生成 plan/review；
- remote compact 可用时走 `/responses/compact`，不可用时明确 fallback local；
- checkpoint 安装后 active context token 数下降；
- corrupted checkpoint 可被跳过并回退到 raw events / legacy fallback。

### Phase 4：自动 compact 与 token usage

交付：

- soft/hard threshold；
- run-count compact；
- context usage API；
- `context_compact` event 增加 before/after tokens；
- per-lane token telemetry。

验收：

- 超阈值自动 compact；
- compact 后 token 显著下降；
- lane 7 噪声不会挤掉 lane 3-6；
- 第一版无后台 compact 竞态；compact 发生在 PM turn 边界。

### Phase 5：Subagent runtime 与 UI Context 面板

交付：

- active agents state 合并 runner/session handle、任务状态、heartbeat/transcript 最新时间和 `agent_stop`；
- Context 面板；
- checkpoint 列表与详情；
- Compact Now；
- compact progress item。

验收：

- 用户能看到 checkpoint；
- 用户能手动 compact；
- compact 过程有可见状态。

### Phase 6：回放、恢复、兼容

交付：

- 旧 session 兼容；
- 没有 checkpoint 的 session 用 legacy rebuild；
- 有 checkpoint 的 session 直接 restore；
- checkpoint corruption fallback。

验收：

- 旧 DB 不崩；
- 新 session 恢复精确；
- checkpoint 缺失/损坏时诚实降级并提示。

---

## 10. 测试计划

### 单元测试

- frame materializer：command_execution nested payload 能提取 command/output/exit_code。
- frame materializer idempotency：同一 event 重跑生成同一 `ContextFrame.id`，不重复插入。
- cursor ordering：event id 是 TEXT，checkpoint cursor 按 `(event_ts, event_id)` 比较。
- PM envelope：必须包含 `task/environment/agents/context/tools/output_contract/validator_rules`。
- intent classification：`direct_answer` 不生成 dispatch plan。
- output contract：`direct_reply` 的 `instruction` 非空规则和 validator 保持一致。
- tool schema：`read_file/search_repo/run_command/write_file/replace_in_file` 的可选字段、默认行为和安全字段能被序列化。
- subagent frame：agent_start 必带 cwd/worktree/branch。
- subagent runtime：active agent 状态合并 runner/session handle、task status、heartbeat/transcript 最新时间和 `agent_stop`。
- checkpoint serialization：replacement_history round-trip 不丢字段。
- token budget：soft/hard threshold 正确。
- context restore：latest checkpoint 优先于 raw event scan。
- tool pairing：Foreman 可观测 command/internal tool/PM action 的 call/result 成对保留或成对压缩。

### 集成测试

- 构造长会话，PM 输出大量 reasoning，确认重要 command_result 不被挤掉。
- 构造 subagent 多 worktree，会话压缩后 PM 仍知道当前 worktree。
- 纯咨询请求生成 `direct_reply`，不创建 worktree、不 dispatch agent。
- validator 返回 `final_plan_missing_instruction` 时，下一轮 PM 输入包含 `previous_validation_error` 并能修正。
- PM 启动 agent 前 active context 包含 repo status / cwd / branch。
- compact 后继续 review，不重复消费 checkpoint 覆盖过的 events。
- soft compact 失败：记录失败事件，PM 可继续，但 active context 必须包含失败事实。
- hard compact 失败：PM 请求停止，UI 显示需要人工处理，不生成新的 plan/review。
- 手动 compact 生成 checkpoint 并能在 UI 看到。
- legacy DB：无 v2 checkpoint 时仍能启动并 fallback。
- corrupted checkpoint：跳过坏 checkpoint，诚实提示并回退到 raw events / legacy fallback。

### 回归测试

- `tests/test_context_compression.py`
- `tests/test_context_p1b.py`
- `tests/test_dispatch_service.py`
- `tests/test_web_page.py`

### 真实 DB 验证

用本地最新 DB session 验证：

1. 找 latest session。
2. materialize frames。
3. compact。
4. restore。
5. 检查 restored active context 包含：
   - 用户目标；
   - worktree；
   - active agent；
   - command result；
   - changed files；
   - tests；
   - next steps。
6. 再跑一次 materializer，确认 frames 和 checkpoint cursor 没有重复或倒退。

---

## 11. 关键不变量

1. raw events 永远是事实来源。
2. active history 是模型可见状态，不等于 UI transcript。
3. compact 只能在 checkpoint 边界重写 active history。
4. replacement history 必须可直接恢复。
5. subagent 身份与 worktree 是结构化 runtime state，不是自然语言备注。
6. Foreman 可观测 tool call/result 必须成对保留或成对压缩。
7. lane 7 噪声不能驱逐 lane 3-6。
8. 压缩失败不能假装成功继续。
9. UI 必须能解释当前上下文使用情况。
10. 旧 session 必须可降级恢复。
11. PM 输出契约必须和 validator 规则同源或同步生成。
12. direct answer 场景不得默认升级成代码 dispatch。

---

## 12. 禁止事项与验收门槛

这些规则用于防止实现退回“方向对但做法错”的状态。

### 12.1 禁止事项

1. 禁止再用“最后 N 条 event + 一段 summary”作为 PM 主上下文恢复机制。
2. 禁止让 `Session.plan` 成为 PM 恢复源；它只能是展示/兼容字段。
3. 禁止新增或复用名为 `Checkpoint` 的模型表示上下文 checkpoint；`Checkpoint` 已用于 git undo。
4. 禁止把 subagent 的 cwd/worktree/branch 只写在 prompt 文本里，必须结构化保存。
5. 禁止 compact 成功但没有 `replacement_history`。
6. 禁止 compact 后不推进 reviewed cursor。
7. 禁止第一版后台 compact 与 PM review/plan 并发写 active history。
8. 禁止吞掉 compact 失败后继续声称上下文已压缩。
9. 禁止尝试恢复不可观测的外部 CLI provider 内部协议；只恢复 Foreman 自己能观测和校验的 call/result。
10. 禁止把 `submit_plan`、`direct_reply` 字段要求、validator 非空规则埋在长工具 schema 之后。
11. 禁止工具 schema 只写必填字段而不说明可选字段默认行为和安全边界。

### 12.2 交付验收门槛

一个实现 PR 必须同时满足：

1. DB 中存在最新 `context_checkpoints` row，`replacement_history_json` 能独立 round-trip。
2. `replacement_history_json` round-trip 后能渲染出 PM plan/review 输入。
3. 对本地长会话样本，压缩后 PM 输入仍包含 worktree、branch、active agents、关键 command_result、changed files、tests、next steps。
4. compact 后下一轮 review 不重复消费已覆盖 events。
5. 旧 session 没有 v2 checkpoint 时能 fallback 到旧 `Session.plan` / `ContextPack v1`，并明确标记 legacy。
6. PM 输入 envelope 明确包含 task type、output contract、validator rules，并能处理 direct_reply。
7. 测试覆盖 materializer 幂等、restore 优先级、可观测 tool call/result 成对、subagent runtime state、cursor 对齐、soft/hard compact failure、checkpoint corruption、direct answer 不 dispatch、validator error recovery。

---

## 13. 与 Codex 做法的差异

Foreman 不能完全照搬 Codex，有三点差异：

1. Codex 使用 `ResponseItem` Rust 类型；Foreman 是 Python/SQLite，需要自定义 `ContextFrame` JSON schema。
2. Codex 有 `/responses/compact` remote endpoint；Foreman 的当前线上 provider 已实测可用，因此应优先 remote compact，同时保留 local compact fallback。
3. Foreman 有 PM/runtime/subagent 分派系统，checkpoint 必须额外保存 worktree、agent role、issue/PR/release 状态。

真正要照抄的是机制：

- active history 不从最后 N 条日志拼；
- compact 产物是 replacement history；
- resume 直接用 replacement history；
- tool/subagent/runtime state 结构化；
- context usage 可见；
- compact 是显式 checkpoint 事件。

---

## 14. 先后顺序建议

优先级从高到低：

1. `context_frames` / `context_checkpoints` schema + deterministic materializer。
2. replacement_history restore + ContextManager active context。
3. PM active context envelope + output_contract + validator_rules。
4. PM plan/review 主路径改用 ContextManager。
5. `/responses/compact` remote adapter + local fallback。
6. 阻塞式 compact 安装、soft/hard 失败语义和 cursor 对齐。
7. subagent/worktree runtime state。
8. auto compact 和 token usage API。
9. UI Context 面板。

如果只先做 UI 或只修 `events_to_text()`，会继续停留在止血层。真正一步到位的关键是第 1-6 项：先让 PM 主路径有可恢复、可校验、可压缩的 active context，并优先复用 provider 已有 compact 能力。
