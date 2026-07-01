你现在在 simplerjiang/agent-foreman 仓库开发“Context v2 + PM Active Context Envelope + Remote/Local Compact + PM Tool Surface”功能。

协作交付要求：
- 每一轮开发完成后，必须把本轮 commit 推送到云端仓库对应分支。
- 每轮回复用户时，必须明确给出：
  - GitHub 仓库地址：`https://github.com/simplerjiang/agent-foreman`
  - 本地 worktree 绝对路径
  - 当前分支名
  - 远端分支地址
- 用户会把这些信息发给 GPT-5.5 pro review；因此每轮回复必须说明本轮完成范围、测试结果、剩余风险，不能只说“完成”。
- 在 GPT-5.5 pro review 反馈前，不要擅自扩大下一轮 scope；如果 review 指出方向偏离，优先修正设计/任务边界。

目标：
把当前 Foreman 的 PM 上下文从：
- events_to_text(rows[-120:])
- Session.plan 摘要
- ContextSnapshot v1 展示缓存
升级为：
- raw Event 仍是事实来源
- ContextFrame 是模型可见 active context 的材料层
- ContextCheckpoint 是可恢复 checkpoint
- replacement_history_json 是 PM resume/plan/review 的恢复源
- PM plan/review 使用固定 envelope：task/environment/agents/context/tools/output_contract/validator_rules
- compact 优先 remote /responses/compact，失败 fallback local compact
- PM tools schema 和 validator 规则同源
- subagent/worktree/runtime state 结构化保存
- 旧 DB/旧 session 兼容降级

不要先做 UI，不要先只修 events_to_text，不要先堆高风险 tools。按以下 commit 顺序开发，每个 commit 必须有测试。

============================================================
全局不变量
============================================================

1. raw Event 永远是事实来源。
2. active history 是 PM 模型可见状态，不等于 UI transcript。
3. Session.plan 只能是展示/兼容 summary，不得作为 PM 恢复源。
4. ContextSnapshot / MemoryItem v1 保留，作为兼容展示层。
5. 新上下文 checkpoint 必须叫 ContextCheckpoint，表名 context_checkpoints。
6. 禁止新增或复用名为 Checkpoint 的上下文模型；现有 Checkpoint 是 git undo。
7. Event.id 是 TEXT/string，cursor 禁止用整数比较。
8. checkpoint cursor 使用：
   {
     "start": {"event_ts": "...", "event_id": "..."},
     "end": {"event_ts": "...", "event_id": "..."}
   }
9. PM plan/review 主路径禁止继续依赖 rows[-120:]。
10. pm_agent.events_to_text() 保留为 legacy fallback。
11. compact 成功必须写 replacement_history_json；没有 replacement_history 就不能安装 checkpoint。
12. compact 后必须推进 review cursor 到 checkpoint.source_cursor.end。
13. 第一版所有 compact 都发生在 PM turn 边界，阻塞式执行；不要做后台并发 compact。
14. soft compact 失败可继续，但必须 emit failed event 并把失败事实放入 active context。
15. hard compact 失败必须阻止下一轮 PM LLM 请求。
16. Foreman 可观测 tool call/result 必须按 call_id 成对保留或成对压缩。
17. 不尝试恢复外部 CLI provider 内部不可观测 tool protocol。
18. subagent cwd/worktree/branch/status/transcript/native_session_id 必须结构化保存。
19. PM output_contract 与 validator 规则必须来自同一份 contract source。
20. direct answer 场景不得默认 dispatch coding agent。

============================================================
Commit 0：基线调查与兼容边界
============================================================

阅读并记录当前实现：
- src/foreman/client/store/models.py
- src/foreman/client/store/db.py
- src/foreman/client/store/migrations.py
- src/foreman/client/core/context_compression.py
- src/foreman/client/core/context_budget.py
- src/foreman/client/core/pm_agent.py
- src/foreman/client/core/dispatch_service.py
- src/foreman/client/tools/loop.py
- src/foreman/client/tools/runtime.py
- src/foreman/client/agents/base.py
- src/foreman/client/agents/_subprocess.py
- src/foreman/client/agents/runner.py
- src/foreman/client/monitor/hooks.py
- src/foreman/server/app.py
- tests/test_context_compression.py
- tests/test_context_p1b.py
- tests/test_dispatch_service.py
- tests/test_web_page.py

必须确认：
- Event.id 是 string。
- Store.get_events 当前按 Event.ts 排序。
- ContextSnapshot 是 v1 派生缓存。
- Checkpoint 是 git undo 快照。
- DispatchService.compact 当前写 Session.plan + ContextSnapshot + context_compact event。
- PM plan/review 当前仍用 _session_context/events_to_text。
- submit_plan schema 当前 required 包含 instruction。
- validate_final_plan 当前无条件拒绝空 instruction。
- PMToolRuntime 当前已有 read_file/search_repo/run_command/write_file/replace_in_file/browser tools，但缺 globs、cwd、timeout、risk_hint、dry_run/diff_preview、wait/assert。
- LLMClient 当前没有 /responses/compact adapter。

运行基线：
- python -m pytest tests/test_context_compression.py tests/test_context_p1b.py tests/test_dispatch_service.py tests/test_web_page.py

验收：
- 不改业务逻辑。
- PR/commit 描述里列出当前主路径和替换点。
- 记录任何既有失败，不把无关失败混入本任务。

============================================================
Commit 1：DB schema + Store API：ContextFrame / ContextCheckpoint
============================================================

修改文件：
- src/foreman/client/store/models.py
- src/foreman/client/store/db.py
- src/foreman/client/store/migrations.py
- tests/test_context_v2_store.py
- tests/test_store_migrations.py 或 tests/test_migrations.py

1. 新增 SQLModel：

ContextFrame:
- __tablename__ = "context_frames"
- id: str primary key
- session_id: str indexed FK session.id
- event_id: str = ""
- event_ts: str = ""
- turn_id: str = ""
- type: str indexed
- role: str = ""
- lane: int = 6
- agent_id: str = ""
- agent_role: str = ""
- agent_type: str = ""
- parent_agent_id: str = ""
- payload_json: str = "{}"
- source_refs_json: str = "[]"
- payload_hash: str = ""
- created_at: str = ""

ContextCheckpoint:
- __tablename__ = "context_checkpoints"
- id: str primary key
- session_id: str indexed FK session.id
- schema_version: int = 2
- trigger: str indexed
- reason: str indexed
- method: str = "local"  # remote|local|legacy
- source_cursor_json: str = "{}"
- input_frame_ids_json: str = "[]"
- summary_json: str = "{}"
- replacement_history_json: str = "{}"
- runtime_state_json: str = "{}"
- token_usage_json: str = "{}"
- created_at: str = ""

Session 新增字段：
- latest_context_checkpoint_id: str = ""

2. migration：
新增 v3：
- add_column(conn, "session", "latest_context_checkpoint_id", "TEXT NOT NULL DEFAULT ''")

要求：
- migration idempotent。
- fresh DB 依赖 create_all 创建整表。
- 旧 DB 依赖 migration 添加 session 新列。
- 不改旧 ContextSnapshot/MemoryItem/Checkpoint 语义。

3. Store API：
新增：
- add_context_frame(frame)
- add_context_frames(frames)
- get_context_frames(session_id, after_cursor=None, limit=None)
- get_context_frame(frame_id)
- add_context_checkpoint(checkpoint)
- get_context_checkpoints(session_id, limit=None)
- get_context_checkpoint(checkpoint_id)
- get_latest_context_checkpoint(session_id)
- install_context_checkpoint(session_id, checkpoint, plan_summary, compact_event_payload) -> tuple[ContextCheckpoint, Event]
- set_latest_context_checkpoint(session_id, checkpoint_id, plan_summary=None)
- get_events_after_cursor(session_id, cursor)

要求：
- add_context_frames 支持 deterministic replay：重复插入同 id frame 不抛异常、不重复。
- get_context_frames 稳定排序：(event_ts, event_id, created_at, id)。
- get_events_after_cursor 不能按 event_id 数值比较。
- install_context_checkpoint 必须在一个 DB transaction 内：
  1. insert ContextCheckpoint
  2. update Session.latest_context_checkpoint_id
  3. optionally update Session.plan
  4. insert context_compact Event row
- EventBus publish 可以在事务完成后做。
- delete_session 同时删除 ContextFrame/ContextCheckpoint。

验收：
- fresh DB 有 context_frames/context_checkpoints。
- 旧 DB migration 后 session 有 latest_context_checkpoint_id。
- ContextFrame/ContextCheckpoint JSON 字段可 round-trip。
- duplicate add_context_frames 不重复、不失败。
- delete_session 清理 v2 rows。
- install_context_checkpoint 原子更新 checkpoint、session、event。
- 运行：
  python -m pytest tests/test_context_v2_store.py tests/test_store_migrations.py tests/test_migrations.py

============================================================
Commit 2：Context v2 schema、materializer、runtime_state
============================================================

新增文件：
- src/foreman/client/core/context_v2.py
或拆分：
- src/foreman/client/core/context_frames.py
- src/foreman/client/core/context_manager.py
- src/foreman/client/core/context_render.py

新增测试：
- tests/test_context_v2_frames.py
- tests/test_context_v2_runtime.py

1. 定义 schema/dataclass：
- ContextFramePayload helper
- ReplacementHistoryItem
- ActiveContext
- RuntimeState
- ContextUsage
- ContextRestoreWarning

ReplacementHistoryItem JSON 最低字段：
{
  "schema": "foreman.active_history.item.v1",
  "id": "...",
  "type": "message|command_call|command_result|agent_status|runtime_state|context_summary|file_change|test_result|validation_error",
  "role": "system|developer|user|assistant|tool",
  "kind": "original_goal|checkpoint_summary|runtime_state|command_result|agent_status|test_result|previous_validation_error",
  "content": "",
  "payload": {},
  "frame_ids": [],
  "source_refs": [],
  "agent_id": "",
  "tool_call_id": "",
  "model_visible": true,
  "created_at": ""
}

replacement_history_json 存储形态：
{
  "schema": "foreman.replacement_history.v1",
  "method": "remote|local",
  "items": [ReplacementHistoryItem],
  "provider_payload": [],
  "warnings": []
}

2. lane 映射：
- lane 1: system/tool schema/safety constraints
- lane 2: user goal/AGENTS/project hard constraints
- lane 3: workspace/worktree/runtime state
- lane 4: current plan/current step/active agents
- lane 5: ContextPack/session memory/decisions/facts
- lane 6: subagent/tool detailed outputs
- lane 7: pm_output/pm_reasoning/streaming/repeated logs

3. deterministic frame id：
make_frame_id(session_id, event_id, frame_type, payload):
- canonical json dumps(sort_keys=True, ensure_ascii=False)
- sha256 first 16/24 chars
- id = frame_{session_short}_{event_short}_{frame_type}_{hash}

4. materialize_event(event) -> list[ContextFrame]：
必须支持：
- dispatch -> user_message + worktree_state
- pm_plan -> pm_plan
- pm_review -> pm_review
- pm_validation_error -> previous_validation_error
- agent_start -> agent_start + worktree_state
- agent_output -> agent_output，长文本摘要化
- agent_reasoning/pm_reasoning/pm_output -> lane 7 或跳过 active history
- stop/SubagentStop -> agent_stop
- tool_pre -> tool_call / command_call
- tool_post -> tool_result / command_result
- approval_req -> decision/constraint
- git_diff/file change event -> file_change
- test result if payload/command/output indicates pytest/npm test/etc -> test_result
- context_compact -> context_compaction

对 command_execution / aggregated_output：
- 解析 command、cwd、exit_code、stdout/stderr/aggregated_output。
- stdout/stderr 不全量进 frame。
- 保留 command、exit_code、cwd、错误行、失败行、文件路径行、首尾摘要。
- 原始全文只留在 raw Event.payload_json。

5. record_event(session_id, event)：
- 调 materialize_event。
- 调 store.add_context_frames。
- 允许事件未知，不能崩。

6. materialize_session(session_id, force=False)：
- replay raw events。
- 只插入 missing deterministic frames。
- force=True 可重新生成但不重复。

7. extract_runtime_state(session, frames, runner=None)：
输出：
{
  "session_id": "",
  "goal": "",
  "workspace": "",
  "main_workspace": "",
  "cwd": "",
  "worktree": "",
  "branch": "",
  "base_ref": "",
  "head_sha": "",
  "active_agents": [],
  "changed_files": [],
  "last_tests": [],
  "last_commands": [],
  "open_questions": [],
  "next_steps": [],
  "warnings": []
}

active_agents 每项：
{
  "agent_id": "",
  "agent_role": "",
  "agent_type": "",
  "parent_agent_id": "",
  "status": "queued|running|completed|failed|cancelled|interrupted|unknown",
  "cwd": "",
  "worktree": "",
  "branch": "",
  "native_session_id": "",
  "pid": null,
  "model": "",
  "effort": "",
  "transcript_path": "",
  "last_seen_at": "",
  "last_meaningful_output": {}
}

要求：
- 不只靠 agent_stop 推断状态。
- 合并 runner.handle_for_session、Task.status、AgentHandle、ProcessWatcher/heartbeat 如果可用、agent_start/agent_stop frames。
- 如果 runner handle 还在，agent_stop 缺失也视为 running/unknown。
- 如果 agent_stop completed 但后续同 agent 有新输出，以最新可验证状态为准。
- 不编造 branch/head_sha；没有就空字符串。

验收：
- same Event replay 两次产生同 id frames。
- PM tool_pre/tool_post 根据 call_id 成对 materialize。
- Claude hook PreToolUse/PostToolUse 能生成 tool_call/tool_result。
- agent_start payload 有 cwd 时，runtime_state 能拿到 cwd。
- 长 stdout 被摘要化，raw 全文不进入 frame payload。
- lane 7 噪声不能驱逐 lane 3/4/6 anchors。
- 多 agent/multi worktree runtime_state 正确。
- 运行：
  python -m pytest tests/test_context_v2_frames.py tests/test_context_v2_runtime.py

============================================================
Commit 3：PM Contract：output_contract / validator_rules 同源
============================================================

修改文件：
- src/foreman/client/tools/loop.py
- src/foreman/client/core/pm_agent.py
- 新增 src/foreman/client/core/pm_contract.py
- tests/test_pm_contract.py
- tests/test_pm_direct_reply_validator.py

目标：
消除 prompt/schema/validator 不一致，特别是 direct_reply + instruction 的问题。

1. 新增 pm_contract.py：
定义 PlanContract：
- allowed_kinds = ["agent_task", "direct_reply", "blocked", "error"]
- required_fields_by_kind
- non_empty_fields_by_kind
- max lengths
- allowed agents/efforts
- direct_reply rules：
  - kind=direct_reply 时 reply 必须非空。
  - instruction 必须非空，但可以是 "direct reply only" 或用户语言的等价短句。
  - agent/model/effort 可保留兼容字段，但不得触发 runner.launch。
- agent_task rules：
  - instruction 必须非空。
  - agent 必须在 enabled agents。
- blocked/error rules：
  - summary 或 reply/reason 必须非空。
  - 不得启动 runner。

2. submit_plan_tool_spec 改为从 PlanContract 生成 schema。
- 不手写另一份 required 规则。
- schema description 明确：
  - direct_reply 也必须填 instruction。
  - direct_reply.reply 是用户可见最终回复。
  - agent_task.instruction 是要发给 coding CLI 的指令。

3. validate_final_plan 改为调用 PlanContract.validate(obj)。
- validator error code 稳定：
  - final_plan_missing_instruction
  - final_plan_missing_reply
  - final_plan_bad_agent
  - final_plan_bad_effort
  - final_plan_bad_kind
- 对 kind="dispatch" 做兼容映射到 "agent_task"，或者暂不允许 dispatch。
- 不要在没有改 PMPlan/DispatchService 前直接引入 ask_user/inspect_only kind。

4. validator error 反向进入 PM 输入：
当前 PMToolLoop validation failure 只 append transcript message。
新增：
- on_tool_event("pm_validation_error", {"error": code, "round": round_no, "arguments": redacted_args})
- materializer 把 pm_validation_error 转成 previous_validation_error frame。
- 下一轮 ContextManager envelope.context.frames_after_checkpoint 包含 previous_validation_error。

5. PM prompt/envelope 里引用同一份 PlanContract：
- output_contract
- validator_rules
- direct_reply_instruction_required

验收：
- submit_plan schema 和 validate_final_plan 来源同一个 PlanContract。
- direct_reply 且 reply 空 -> final_plan_missing_reply。
- direct_reply 且 instruction 空 -> final_plan_missing_instruction。
- direct_reply 且 instruction="direct reply only" + reply 非空 -> valid。
- agent_task instruction 空 -> final_plan_missing_instruction。
- validator failure emit pm_validation_error event。
- pm_validation_error materialize 成 previous_validation_error frame。
- 运行：
  python -m pytest tests/test_pm_contract.py tests/test_pm_direct_reply_validator.py tests/test_dispatch_service.py

============================================================
Commit 4：PM Active Context Envelope + ContextManager restore
============================================================

修改文件：
- src/foreman/client/core/context_v2.py
- src/foreman/client/core/pm_agent.py
- src/foreman/client/core/dispatch_service.py
- tests/test_context_v2_active_context.py
- tests/test_pm_envelope.py

1. ContextManager：
实现：
- __init__(store, pm_agent=None, runner=None, clock=None)
- materialize_session(session_id, force=False)
- record_event(session_id, event)
- get_latest_checkpoint(session_id)
- build_active_context(session_id, purpose, window_tokens) -> ActiveContext
- restore_from_latest_checkpoint(session_id, purpose, window_tokens) -> ActiveContext

ActiveContext：
{
  "session_id": "",
  "purpose": "pm_plan|pm_review|subagent_launch|compact|ui_preview",
  "envelope": {},
  "stable_prefix": [],
  "replacement_history": [],
  "frames_after_checkpoint": [],
  "runtime_state": {},
  "source_cursor": {},
  "token_usage": {},
  "degraded": false,
  "warnings": [],
  "rendered_text": ""
}

2. build_active_context 规则：
- materialize_session。
- 读取 latest ContextCheckpoint。
- 如果 checkpoint 存在且 replacement_history_json 可解析：
  - envelope.context.checkpoint_replacement_history = replacement_history.items
  - envelope.context.frames_after_checkpoint = frames after source_cursor.end
- 如果 checkpoint 缺失：
  - envelope.context.stable_prefix + selected frames
  - degraded=false, restore_mode="raw_frames"
- 如果 checkpoint 损坏：
  - degraded=true
  - warnings 包含 corrupted_checkpoint
  - fallback raw frames / ContextSnapshot v1 / Session.plan
  - 不删除坏 checkpoint。
- PM 输入必须含：
  - task.original_user_request
  - task.user_intent_type
  - environment.cwd/workspace/worktree/branch/base_ref
  - agents.available/active
  - context.stable_prefix
  - context.checkpoint_replacement_history
  - context.frames_after_checkpoint
  - context.runtime_state
  - tools.available/schemas
  - output_contract
  - validator_rules

3. user_intent_type：
新增 classify_user_intent(goal, explicit_agent, workspace, context):
- direct_answer
- code_change
- repo_inspection
- browser_task
- planning_only

第一版用 deterministic heuristic：
- 明确修复/实现/测试/代码/文件/bug/fix/implement -> code_change
- “看一下仓库/找文件/解释代码” -> repo_inspection
- URL/网页/浏览器动作 -> browser_task
- 问候/确认/纯说明/无需仓库 -> direct_answer
- 不确定 -> planning_only 或 code_change，保守但不得默认 dispatch 简单 direct answer

4. PM prompt render：
新增 build_plan_envelope_prompt(envelope)：
- 仍然返回 string，因为 LLMClient Message.content 是 str。
- 使用 JSON pretty compact，不要自然语言大串混杂。
- output_contract 和 validator_rules 放在最前 1/3。
- tools schema 不要淹没 output_contract。

5. pm_agent.build_plan_prompt / build_review_prompt：
- 支持 active_context/envelope 参数。
- 旧 context string 继续 fallback。
- plan/review prompt 用 envelope 替换 "# Existing session context" 旧段落。
- events_to_text 标注为 legacy fallback。

验收：
- build_active_context 无 checkpoint 时可构造 envelope。
- 有 checkpoint 时优先使用 replacement_history_json。
- checkpoint 后事件只出现在 frames_after_checkpoint。
- corrupted checkpoint fallback 且 degraded=true。
- PM envelope 必含 task/environment/agents/context/tools/output_contract/validator_rules。
- direct_answer 请求 envelope.task.user_intent_type=direct_answer。
- PM prompt 中 output_contract/validator_rules 位于工具 schema 前。
- 运行：
  python -m pytest tests/test_context_v2_active_context.py tests/test_pm_envelope.py

============================================================
Commit 5：PM plan/review 主路径切换到 ContextManager
============================================================

修改文件：
- src/foreman/client/core/dispatch_service.py
- src/foreman/client/core/pm_agent.py
- tests/test_context_v2_dispatch.py
- tests/test_dispatch_service.py

1. DispatchService 初始化：
新增 self.context_manager。
- 构造参数允许注入 fake_context_manager。
- 默认 ContextManager(store, pm_agent, runner, clock)。

2. 新增 helper：
async _build_pm_active_context(session_id, purpose, window_tokens, run_count=0, hard=False) -> ActiveContext

流程：
- context_manager.maybe_compact(...), 先可以 no-op，Commit 7 再补 threshold。
- active_context = context_manager.build_active_context(...)
- return active_context

3. _pm_launch plan：
替换：
- context = self._session_context(session_id)
为：
- active_context = await self._build_pm_active_context(session_id, "pm_plan", window_tokens)
- context = active_context.rendered_text
- plan_kwargs["active_context"] = active_context if pm_agent.plan accepts it
- plan_kwargs["context"] = context 保留兼容

4. PM review：
替换：
- timeline = events_to_text(_events_after(rows, reviewed_event_id))
为：
- active_context = await self._build_pm_active_context(session_id, "pm_review", window_tokens, run_count)
- timeline = render_review_increment(active_context)
- review_kwargs["context"] = active_context.rendered_text
- review_kwargs["active_context"] = active_context if accepted

5. reviewed cursor：
实现：
- reviewed_cursor_from_event_id(rows, reviewed_event_id)
- cursor_max(a, b) using row order by (event_ts,event_id)
- if latest checkpoint source_cursor.end is after reviewed_event_id，推进 reviewed cursor 到 checkpoint end。
- compact 覆盖过的 event 不得再次进入 incremental review。
- 找不到 cursor 时保守 fallback，不丢事件。

6. legacy fallback：
如果 ContextManager 抛异常：
- emit context_restore_failed/degraded event
- fallback 到 _session_context/events_to_text
- active_context.degraded=true
- 不静默。

验收：
- PM plan 使用 ContextManager.build_active_context(purpose="pm_plan")。
- PM review 使用 ContextManager.build_active_context(purpose="pm_review")。
- tests 可断言 events_to_text 不是主路径。
- 有 checkpoint 时 prompt 包含 replacement_history。
- 有 agent_start frame 时 prompt 包含 cwd/worktree/branch/status。
- compact 后 review 不重复消费 checkpoint 覆盖事件。
- ContextManager fail 时 legacy fallback 可用且有 degraded event。
- 运行：
  python -m pytest tests/test_context_v2_dispatch.py tests/test_dispatch_service.py

============================================================
Commit 6：PM Tool Surface 第一批：低风险高信号 tools + schema 默认行为
============================================================

修改文件：
- src/foreman/client/tools/runtime.py
- src/foreman/client/tools/models.py
- tests/test_pm_tools_schema.py
- tests/test_pm_tools_repo.py

目标：
先做 read-only/status 工具和 schema 明确化，给 PM envelope 提供 repo/runtime evidence。

1. 更新现有 tools schema：
read_file:
- 保留 start_line/end_line。
- description 写清默认：
  - 不传 start/end 时读文件前 N 行或 max_chars，上限来自 ToolRuntimeConfig.max_chars。
  - 返回 truncated flag。
- input_schema 加 max_chars optional。

search_repo:
- 新增 include_globs: list[str]
- 新增 exclude_globs: list[str]
- 默认排除 .git/node_modules/dist/build/.venv/__pycache__
- 支持 path + globs 组合。
- 返回 file/line/text，truncated。

run_command:
- 新增 cwd optional，相对 workspace 且必须过 PathGuard。
- 新增 timeout_ms optional。
- 新增 env object optional，只允许显式白名单或安全覆盖；默认空。
- 新增 risk_hint enum:
  - read_only
  - writes_files
  - network
  - destructive_candidate
- normalize_command/Gate/Auditor 能看到 risk_hint。
- 返回 command/cwd/exit_code/stdout_summary/stderr_summary/duration_ms/truncated。

write_file / replace_in_file:
- 新增 dry_run: bool
- 新增 diff_preview: bool
- dry_run=true 时只返回 diff，不写文件。
- diff_preview=true 时返回 diff_stat/unified_diff capped。
- 非 dry-run 保持原 Gate/strategy 行为。

2. 新增 read-only tools：
get_repo_status:
- git status --porcelain
- branch
- detached
- head_sha
- upstream
- ahead/behind if available
- changed_files summary
- is_git_repo/git_available

get_file_tree:
- path
- recursive bool
- max_depth
- include_globs/exclude_globs
- max_results
- include_size
- include_mtime
- 默认跳过 SKIP_DIRS

git_diff / read_diff:
- staged bool
- path optional
- max_chars
- 返回 diff_stat、files、truncated、diff excerpt

detect_project:
- package manager/language markers
- candidate test commands
- candidate lint commands
- confidence
- evidence files

3. policy：
- 这些工具默认 SAFE，除了 run_command/write/replace 的非 dry-run 仍按 NEEDS_STRATEGY/approval。
- 所有文件路径必须 PathGuard。
- 所有输出都 cap max_chars。

验收：
- tool_schema 包含新增字段和 description。
- get_repo_status 在 git/non-git workspace 均返回稳定结构。
- search_repo include/exclude globs 生效。
- run_command cwd/timeout/risk_hint 序列化并进入结果。
- write/replace dry_run 不改文件且返回 diff。
- detect_project 能识别 Python/Node 基本项目。
- 运行：
  python -m pytest tests/test_pm_tools_schema.py tests/test_pm_tools_repo.py

============================================================
Commit 7：Remote compact adapter + local compact fallback + checkpoint 安装
============================================================

修改文件：
- src/foreman/shared/llm/client.py
- src/foreman/client/core/context_v2.py
- src/foreman/client/core/context_compression.py
- src/foreman/client/core/dispatch_service.py
- tests/test_remote_compact_adapter.py
- tests/test_context_v2_checkpoint.py

1. LLMClient 新增：
async responses_compact(input_items, *, instructions="", model="", metadata=None) -> dict

行为：
- provider 必须是 openai/openai-compatible；anthropic 直接 Unsupported。
- endpoint = f"{base_url}/responses/compact"
- 使用同一个 _resolve/_api_key/_request_timeout/_client。
- payload:
  {
    "model": model,
    "input": input_items,
    "instructions": instructions,
    "metadata": metadata or {}
  }
- 返回 raw JSON。
- 不把 encrypted_content 当 human summary。
- tracer 如存在，记录 kind="responses_compact"。

2. capability probe：
ContextManager/CompactAdapter：
- compact capability key = provider/base_url/model
- 首次 remote compact 失败后缓存 unsupported，TTL 可存在内存即可。
- HTTP 404/405/400 unsupported shape -> fallback local。
- 网络/timeout -> fallback local for soft compact；hard compact fallback local 也失败才阻塞。
- 不在每一轮都 probe。

3. render_provider_active_history(active_context)：
- 把 Foreman ReplacementHistoryItem/frames/runtime_state 映射成 provider input items。
- 最低支持 message items。
- command/tool frame 以 text/message 形式表示，不假装完整 provider tool protocol。
- 保留 source_refs/frame_ids 在 content 或 metadata。

4. local compact fallback：
- 输入 active_context，不输入 events_to_text timeline。
- 可以调用 pm_agent.compact，但 prompt 是 active history + runtime_state + anchors。
- local 输出也必须生成 replacement_history_json，不得只写 summary。
- summary_json 用 normalize_checkpoint_summary。

5. compact_now：
流程：
- emit context_compact started event
- build_active_context(purpose="compact")
- estimate before_tokens
- try remote compact
- fallback local compact
- frames_to_replacement_history()
- normalize summary_json
- extract runtime_state
- source_cursor.end = last input frame/event cursor
- token_usage before/after/window/method/provider
- install_context_checkpoint in Store
- emit context_compact completed event via Store transaction result/publish
- rebuild active_context to verify restore works

6. compact failure：
- 不更新 latest_context_checkpoint_id。
- 不更新 Session.plan。
- emit context_compact failed event：
  {
    "status": "failed",
    "schema_version": 2,
    "hard": true|false,
    "method_attempted": "remote|local",
    "error": "...",
    "reason": "..."
  }
- soft failure returns None。
- hard failure raises ContextCompactRequiredError or returns blocking error consumed by DispatchService。

验收：
- remote compact mock 200 使用 /responses/compact。
- remote 404 fallback local。
- remote output provider_payload 存入 replacement_history_json.provider_payload。
- summary_json 仍是 Foreman-readable，不用 encrypted_content。
- local fallback 也生成 replacement_history_json.items。
- compact 成功更新 Session.latest_context_checkpoint_id。
- context_compact event 有 started/completed。
- compact 失败不更新 Session.plan/latest pointer。
- hard failure 阻止 PM plan/review。
- 运行：
  python -m pytest tests/test_remote_compact_adapter.py tests/test_context_v2_checkpoint.py tests/test_context_compression.py

============================================================
Commit 8：Token usage / soft-hard threshold / run-count compact
============================================================

修改文件：
- src/foreman/client/core/context_budget.py
- src/foreman/client/core/context_v2.py
- src/foreman/client/core/dispatch_service.py
- tests/test_context_v2_budget.py
- tests/test_context_p1b.py

1. ContextUsage：
{
  "used_tokens": int,
  "window_tokens": int,
  "percent": float,
  "tokens_until_soft_compact": int,
  "tokens_until_hard_compact": int,
  "soft_threshold": 0.70,
  "hard_threshold": 0.90,
  "run_count_threshold": 8,
  "lane_usage": {"1":0,"2":0,"3":0,"4":0,"5":0,"6":0,"7":0}
}

2. estimate_context_usage(active_context, window_tokens):
- 用 approx_tokens 初版即可。
- 按 frame.lane 汇总。
- stable lanes 1-4 不作为逐出候选，但计入总量。
- lane 7 最先压缩/省略。

3. maybe_compact：
- soft threshold >= 70%：PM turn boundary 先 compact，失败可继续但记录 failed。
- hard threshold >= 90%：必须 compact，失败阻止 PM。
- run_count % 8 == 0：soft compact。
- compact 成功后重建 active_context 和 usage。

4. DispatchService：
- plan 前调用 maybe_compact。
- review 每轮前调用 maybe_compact。
- compact 后 reviewed cursor 推到 checkpoint.source_cursor.end。

验收：
- lane 7 噪声达到阈值会触发 compact。
- lane 3/4/6 anchors 在 compact 输入和输出中保留。
- 70% soft failure 可继续且 active_context 包含 failed fact。
- 90% hard failure 不调用 PM LLM。
- run_count=8 触发 soft compact。
- compact 后 used_tokens 下降。
- 运行：
  python -m pytest tests/test_context_v2_budget.py tests/test_context_p1b.py tests/test_dispatch_service.py

============================================================
Commit 9：Subagent runtime state 完整融合
============================================================

修改文件：
- src/foreman/client/agents/runner.py
- src/foreman/client/agents/_subprocess.py
- src/foreman/client/core/context_v2.py
- src/foreman/client/monitor/process.py
- tests/test_context_v2_subagents.py
- tests/test_runner.py
- tests/test_process.py

目标：
PM 看到的不再是自然语言猜测，而是每个 agent 的结构化运行态。

1. agent_start event payload 补充：
- handle_id
- pid
- command
- cwd
- worktree
- branch if detectable
- base_ref if detectable
- head_sha if detectable
- native_session_id if known later可由后续 frame 更新
- model
- effort
- agent_type/source
- status="running"

2. agent_input frame：
在 DispatchService runner.launch 前写 event/frame：
- agent_id/handle_id 如果 launch 后才知道，先用 planned_agent_id，launch 后关联。
- instruction/message
- expected_output
- workspace/cwd/worktree/branch
- source_refs

3. agent_stop：
当 stream 正常结束、error、cancel/interrupt 时记录：
- status completed|failed|interrupted|cancelled|unknown
- returncode/error if any
- changed_files/tests if available
- last_seen_at

4. Runtime fusion：
extract_runtime_state 合并：
- Runner.handles / handle_for_session
- Task.status
- agent_start/agent_input/agent_output/agent_stop frames
- ProcessWatcher status if available
- native_session_id
- last meaningful command/file/test result

验收：
- 多 agent 多 worktree runtime_state.active_agents 完整。
- agent_stop 缺失但 runner handle still live -> running/unknown，不误判 completed。
- agent_stop completed 但后续有 output -> 以最新输出更新时间为准。
- compact 后 PM prompt 仍含 agent cwd/worktree/branch/status/native_session_id。
- 运行：
  python -m pytest tests/test_context_v2_subagents.py tests/test_runner.py tests/test_process.py

============================================================
Commit 10：PM Tool Surface 第二批：run_tests/apply_patch/browser wait/assert/ask_user_freeform/artifact
============================================================

修改文件：
- src/foreman/client/tools/runtime.py
- src/foreman/client/tools/browser.py
- src/foreman/client/core/cards.py
- src/foreman/server/app.py if needed
- tests/test_pm_tools_advanced.py
- tests/test_browser_tools.py

1. run_tests：
- 输入:
  {
    "command": "",
    "cwd": "",
    "timeout_ms": 120000,
    "env": {},
    "source": "detect_project|user|pm"
  }
- 如果 command 为空，使用 detect_project candidate test command。
- 结构化返回:
  passed, failed, exit_code, duration_ms, failures[], stdout_summary, stderr_summary
- risk: read_only unless command/risk classifier says otherwise。
- 走 Gate/Auditor，不能绕过 run_command policy。

2. apply_patch：
- 输入 unified_diff
- dry_run 默认 true
- diff_preview 默认 true
- path guard all target files
- dry_run 返回 apply status + diff stat，不写文件
- 非 dry-run 需要 NEEDS_STRATEGY/Gate

3. browser_wait_for_text：
- text
- timeout_ms
- selector/ref optional
- 返回 found bool/current_url/excerpt

4. browser_assert_visible：
- text/ref/selector
- 返回 ok bool/evidence

5. ask_user_freeform：
- question
- placeholder optional
- max_length
- creates DecisionCard/notification style request
- PM tool loop pauses until response or timeout
- result contains answer/status
- 必须防止无限等待；timeout 后返回 user_unavailable

6. artifact_create：
- kind: report|log|patch|screenshot_ref|json
- name
- content or source_path
- writes only under .foreman/artifacts or configured state dir
- returns artifact path/ref
- no arbitrary path write

验收：
- run_tests 返回结构化 pass/fail。
- apply_patch dry_run 不改文件。
- browser wait/assert 可在 fake browser runtime 测试。
- ask_user_freeform 生成 pending decision/request，并能接收 fake answer。
- artifact_create 不允许写出 artifact root。
- 运行：
  python -m pytest tests/test_pm_tools_advanced.py tests/test_browser_tools.py

============================================================
Commit 11：Context API + UI 面板
============================================================

修改文件：
- src/foreman/server/app.py
- src/foreman/client/local_app.py
- static app.js/app.css 按仓库结构
- tests/test_context_v2_api.py
- tests/test_web_page.py

API：
1. GET /api/sessions/{session_id}/context
返回：
{
  "usage": {},
  "latest_checkpoint": {},
  "runtime_state": {},
  "active_context_preview": "",
  "degraded": false,
  "warnings": []
}

2. GET /api/sessions/{session_id}/context/checkpoints
返回 checkpoint list：
- id
- created_at
- trigger
- reason
- method
- before_tokens
- after_tokens
- window_tokens
- source_cursor
- replacement_history_items_count

3. GET /api/sessions/{session_id}/context/checkpoints/{checkpoint_id}
返回：
- summary_json
- runtime_state_json
- token_usage_json
- source_cursor_json
- warnings
默认不返回完整 provider_payload/encrypted content。

4. POST /api/sessions/{session_id}/context/compact
body:
{
  "trigger": "manual",
  "reason": "user_requested"
}
调用 ContextManager.compact_now。

兼容：
- 旧 POST /api/sessions/{session_id}/compact 继续可用，但内部转到 v2 compact，并返回兼容字段。

UI：
- Context 面板
- usage meter
- lane usage
- latest checkpoint
- checkpoint list/detail
- active agents
- worktree/branch/cwd
- changed files
- last tests
- next steps
- Compact Now 按钮
- Copy Checkpoint Summary
- contextCompaction started/completed/failed timeline item

独立 UI bug：
- 新消息发送后 conversation view 滚动到底部。
- 不得依赖 Context 面板完成。
- 单独测试。

验收：
- 用户能看到 context usage。
- 用户能手动 compact。
- checkpoint 列表可见。
- checkpoint detail 不泄露 hidden reasoning/encrypted content。
- compact progress 可见。
- 新消息提交后滚动到底部。
- 运行：
  python -m pytest tests/test_context_v2_api.py tests/test_web_page.py

============================================================
Commit 12：Replay / corruption fallback /真实 DB 验证
============================================================

新增：
- tests/test_context_v2_restore.py
- tests/test_context_v2_realistic_session.py
- scripts/verify_context_v2.py

场景：
1. 旧 DB 无 context tables：
- Store.init 创建表并迁移，不崩。

2. 旧 session 无 checkpoint：
- build_active_context 使用 raw frames + ContextSnapshot/Session.plan legacy fallback。
- degraded 或 restore_mode 标记清楚。

3. checkpoint corruption：
- replacement_history_json 损坏。
- runtime_state_json 损坏。
- summary_json 损坏。
- 不删除坏 checkpoint。
- warning/degraded。
- fallback raw events/legacy。

4. 长会话真实样本：
构造：
- dispatch/user goal
- pm plan
- agent_start cwd/worktree/branch
- 大量 pm_output/pm_reasoning
- tool_pre/tool_post command
- long aggregated_output
- file_change
- failed test then passed test
- agent_stop
- manual compact
- resume/review

检查：
- DB 有 context_frames。
- DB 有 context_checkpoints。
- latest_context_checkpoint_id 指向最新 checkpoint。
- replacement_history_json round-trip。
- rendered PM review 输入含：
  - original user goal
  - checkpoint summary
  - cwd/worktree/branch
  - active/last agent status
  - command result
  - changed files
  - last tests
  - next steps
- 不含大段 pm_reasoning 噪声。
- compact 后 after_tokens < before_tokens。
- compact 后 review 不重复消费 checkpoint 覆盖事件。
- materialize_session 再跑一次不重复 frames。

脚本 scripts/verify_context_v2.py：
- 找 latest session 或指定 --session-id。
- materialize frames。
- compact。
- restore。
- 打印 usage/checkpoint/runtime summary。
- exit nonzero on missing required anchors。

验收：
- python -m pytest tests/test_context_v2_restore.py tests/test_context_v2_realistic_session.py
- python scripts/verify_context_v2.py --session-id <id> 可运行。

============================================================
最终必须跑的回归
============================================================

至少运行：
- python -m pytest tests/test_context_compression.py
- python -m pytest tests/test_context_p1b.py
- python -m pytest tests/test_dispatch_service.py
- python -m pytest tests/test_web_page.py
- python -m pytest tests/test_context_v2_store.py
- python -m pytest tests/test_context_v2_frames.py
- python -m pytest tests/test_context_v2_active_context.py
- python -m pytest tests/test_context_v2_checkpoint.py
- python -m pytest tests/test_context_v2_dispatch.py
- python -m pytest tests/test_context_v2_budget.py
- python -m pytest tests/test_pm_contract.py
- python -m pytest tests/test_pm_tools_schema.py

============================================================
实现顺序总结
============================================================

必须按这个顺序：
1. DB/schema/store。
2. materializer/runtime_state。
3. PM contract/validator 同源。
4. active context envelope。
5. PM plan/review 主路径切换。
6. remote/local compact + checkpoint install。
7. token threshold/cursor。
8. subagent runtime 完整融合。
9. tools 第二批。
10. API/UI。
11. replay/corruption/真实 DB 验证。

不要把新增 tools、UI、remote compact 单独先做。没有 ContextFrame/ContextCheckpoint/replacement_history restore，任何 tool 和 UI 都只是给旧拼文本路径增加噪声。
