# 附录 — 约定 / 预算常量 / Schema / telemetry 事件 / 文件路径映射 / 术语表

> 顶部元信息
> - 日期：2026-06-24
> - 对应设计书章节：§4.2 / §8 / §8B.3 / §8C.2 / §16（并汇集各阶段共享的所有跨阶段事实）
> - 分支：`codex/work-mode-design`（本实现分支基线 == `main` 的 `1801128`）
> - 设计书：[`docs/WORK_MODE_EFFECTIVE_INTEGRATION_DESIGN.md`](../WORK_MODE_EFFECTIVE_INTEGRATION_DESIGN.md)
> - 本附录是**贯穿全程**的参考。开发任意阶段前**先读本文件**，再读对应阶段文档。

---

## 0. 目标与产出

**本阶段交付什么**：一份**单一权威的共享参考**，把"工作方式生产级接入"全程会被多个阶段反复用到的东西集中定稿，避免每个阶段各写一份、彼此漂移。具体包括：

- 全部新增预算常量（§8 / §8B）的**唯一定义表**：名字、默认值、含义、放哪、谁读。
- L0 元数据 Schema（`foreman.workmode.meta/1`，§4.2）的字段定义与校验规则。
- `work_mode` telemetry 事件（§8 / §16）与 LLM debug trace（§8C.2）的**统一字段表**，并约定二者共享的关联 id（`contextvars`）。
- 设计书相对文件名 → 本仓真实绝对路径 + 已核实 `file:line` 的映射表（**含设计书行号与实际行号的出入更正**）。
- 术语表（L0/L1/L2、lane、ContextPack、MemoryItem、scope、稳定前缀…）。
- 一处集中的**跨阶段约定**（命名常量、scope 字符串、目录命名、向后兼容规则、安全边界）。

**本阶段定义之完成**：任一接手工程师在开始任意 Pn 阶段前，只读本附录 + 该阶段文档，即可拿到所有共享常量/Schema/事件字段/真实行号，**无需回翻设计书**，且各阶段对同一常量/字段的写法保证一致。

> 注意：本附录**不引入代码改动**。它是文档基础设施。常量/Schema 的**实际落地**发生在各 Pn 阶段（见每节"落地阶段"标注）。

---

## 1. 前置依赖

- 无代码前置（本文件先于一切开发存在）。
- 读者应先读：
  - 索引与排期：[`00-OVERVIEW-AND-SEQUENCING.md`](00-OVERVIEW-AND-SEQUENCING.md)
  - 审阅结论（blocker/major/minor 全表）：[`01-REVIEW-FINDINGS.md`](01-REVIEW-FINDINGS.md)
- 进入任意阶段时假定的代码状态：**HEAD == `1801128`**。本附录所有 `file:line` 均按此基线核实；若你所在分支已偏离基线，以你分支的真实行号为准并就地更正。

---

## 2. 涉及文件与现状（设计书相对名 → 真实路径 + 核实行号）

下表是**全程的文件路径映射 + 关键接缝位置**。所有行号均已在基线 `1801128` 上**亲自打开核实**；与设计书写法不一致处用 "(设计书写作 X，实际 Y)" 标注。各阶段文档引用某个接缝时，直接引用本表，不要各自重写。

| 设计书相对名 | 真实绝对路径 | 关键 `file:line`（核实） | 当前行为 / 备注 |
|---|---|---|---|
| `local_app.py` | `E:/AutoWorkAgent-work-mode-design/src/foreman/client/local_app.py` | `DispatchService` 实例化 `158`；`tool_runtime_factory` lambda `163-169`，其中 `PMToolRuntime.from_config(cfg, workspace, gate=gate, auditor=auditor)` 在 `166-168` | **全仓库唯一**实例化 `DispatchService` 处。resolver 注入点 = 这个 lambda（闭包捕获 `store`）。`store` 在 `160` 传入，作用域内可用 |
| `dispatch_service.py` | `.../client/core/dispatch_service.py` | `MAX_GOAL_CHARS=8000` @ `39`；`MAX_CONTEXT_CHARS=12000` @ `46`；`_within_any(path:str, roots)` @ `60-73`；`create()` @ `103-`；`_within_any` 内用 `Path.resolve(strict=False)` + `is_relative_to`（`71-72`，Windows 安全） | 不引用任何 `injector`/`inject`/`clear`（见 §6 风险）。`_within_any` 入参是 `str` |
| `definition_service.py` | `.../client/core/definition_service.py` | `KNOWN_KINDS={"workflow","skill","code_standard","qa_rubric"}` @ `29`；`KNOWN_STATUS` @ `32`；`MAX_NAME=200` @ `34`；`MAX_BODY=200_000` @ `35`；`create_definition` @ `124-`；`update_definition` @ `179-`；`_validate` @ `406-418` | `_validate` 只校验 kind∈KNOWN_KINDS / name 非空≤200 / body≤200_000 / scope_json 是 JSON-object / metadata_json 是 JSON-object。**从不读 metadata_json 内部**；无 `description` 参数/校验（§4.3 待加） |
| `injector.py` | `.../client/core/injector.py` | `MARKER_BEGIN`/`MARKER_END` 固定常量 @ `36-37`（**无 task_id**）；`SKILLS_DIR=".foreman/skills"` @ `40`；`AGENT_GUIDANCE_FILES` @ `44-48`；`_SLUG_RE` @ `53`，`_slug()` @ `56-61` (设计书写作 `injector.py:53-61` 指 slug 化，实际 `_slug` 在 `56-61`、`_SLUG_RE` 在 `53`)；`_within_any(path:Path, roots)` @ `64-77` | 托管块标记是固定常量、整段替换。`_within_any` 入参是 `Path`（与 dispatch 版的 `str` 版本语义同但签名异）。`_build_block`/`clear`/`_write_skills` 见 P2 文档 |
| `pm_agent.py` | `.../client/core/pm_agent.py` | `MAX_EVENT_CHARS=20000` @ `23`；`MAX_COMPACT_CHARS=12000` @ `24`；`PLAN_SYSTEM` @ `26`；`REVIEW_SYSTEM` @ `48`；`COMPACT_SYSTEM` @ `61-75`；`compact()` @ `596-`；`review()` @ `565-`；`build_plan_prompt` @ `459`（生产路径再追加 tool ctx `471-477`） | `COMPACT_SYSTEM` 当前无任何 skill/standard/qa 或 `workmode:` 字样（§8B.5 待升级）。**L0 注入点在此文件的 messages 组装处，不在 loop.py** |
| `context_compression.py` | `.../client/core/context_compression.py` | `DEFAULT_CONTEXT_BUDGET_CHARS=12000` @ `15`；`MEMORY_FIELDS` @ `17-28`；`EVICT_MEMORY_FIELDS` @ `30-39`；`FIELD_STATUS` @ `41-52`；`FIELD_KIND` @ `54-65` | `context_pack_to_text` 用 `json.dumps(indent=2)`**非 sort_keys**；eviction 用 `pop` 原地改 dict（§8B.4 KV-cache 风险）。`retrieved_evidence` 是顶层证据容器；`source_refs` 是 per-item key（**非顶层字段**） |
| `workflow_engine.py` | `.../client/core/workflow_engine.py` | `_inject`/`_clear`/`_resolve_material`、`begin_step`/`submit_step`/`resume_after_gate` | **全仓零实例化**。现成的 inject↔clear 成对逻辑只活在此，普通 PM 派发路径不经过它（见 §6 P2 blocker） |
| `qa_review.py` | `.../client/core/qa_review.py` | `WorkflowQAReviewer` | workflow QA 门（P5 复用）；**【已拍板 2026-06-24｜D2】** P4 不动它，普通任务 rubric 软约束走 `PMAgent.review`，硬门推迟 V2 |
| `reviewer.py` | `.../client/core/reviewer.py` | `LLMClient.complete` 调用点 @ `275` | workflow QA reviewer（P5 复用）；P1b-trace set-point 之一。**【已拍板 2026-06-24｜D2】** P4 的 check 命令实跑门推迟 V2、本阶段不实现 |
| `tools/runtime.py` | `.../client/tools/runtime.py` | `specs()` list @ `84-203`（最后一条 `browser_close` 收于 `203`）；`from_config(cls, cfg, workspace, *, gate=None, auditor=None)` @ `52-82`（无 store）；`__init__` @ `36-50`；`call()` @ `235-`，dispatch 链 `242-259`，`browser_` 前缀判断 @ `258-259`，`unknown_tool` 兜底 @ `260`，`try/except` 包裹 `241/261-266`；`_truncate` @ `629-632`（阈值 `cfg.max_chars=12000`，**不可直接套 work-mode 6000**） | work_mode 工具/handler/resolver **全为待新增**。三接缝：①`specs()` list 加 `ToolSpec`；②handler 加为方法；③`call()` 链在 `259` 之后、`260` 之前加分支。resolver 须经 `from_config` 增 keyword-only `resolver=None` + `__init__` 存 `self._work_mode_resolver` |
| `tools/loop.py` | `.../client/tools/loop.py` | `PMToolLoop.__init__` @ `26-38`，`max_rounds:int=6` @ `32`，`self.max_rounds=max(1,max_rounds)` @ `37`；`run()` @ `40-`（`transcript=list(messages)` `40-49`，每轮重发 `52-53`）；`_complete()` @ `152-168`（有 `tool_complete` 用 native，否则 `complete()`+JSON）；`build_tool_prompt_context()` @ `178-206`；`_calls_from_json` @ `263-284` | `PMToolLoop` **不组装 system message**，只把调用方给的 `messages` 每轮原样重发。L0 真正注入点在 `pm_agent.py`（见上行）。`max_rounds` 是独立构造参数，**不读** `runtime.cfg.max_rounds` |
| `tools/models.py` | `.../client/tools/models.py` | `Risk`/`SAFE`/`NEEDS_STRATEGY`/`REQUIRES_APPROVAL`/`EXTERNAL_WEB` @ `9-13`（`SAFE="safe"` @ `10`）；`ToolSpec(name, description, input_schema, risk=SAFE)` @ `16-37`（`to_prompt` 含 risk `23-29`，`to_native` 不含 risk `31-36`）；`ToolResult(id, name, ok, data={}, error="", truncated=False, risk=SAFE, taint, artifact_paths)` @ `64-74`；`ToolRuntimeConfig` @ `90-108`（`max_rounds=6` @ `105`，`max_chars=12000` @ `107`，**无 store/resolver 字段**） | `SAFE` 已在 runtime.py 顶部导入，新工具直接用。`ToolRuntimeConfig` 不承载 work-mode 字段；resolver 走 `PMToolRuntime` 直接构造参数 |
| `tools/policy.py` | `.../client/tools/` | `PathGuard._is_relative_or_same` @ `50-54`（`path.is_relative_to`），整类 `19-54` | resolver 做 scope 路径包含判断时**优先复用本类**（同包，避免 `tools→core` 反向依赖），勿裸 import `dispatch_service._within_any` |
| `store/models.py` | `.../client/store/models.py` | `MemoryItem` @ `57-78`（`scope` @ `63` 注释 `session\|workspace\|workflow\|user`；`kind` @ `64`；`status` @ `66`；`importance=50` @ `67`；`confidence=50` @ `68`；`source_refs_json` @ `69`；`supersedes` @ `73`；`superseded_by` @ `74`）；`Definition` @ `194-206`（`scope_json="{}"` @ `201`，`body` @ `202`，`metadata_json="{}"` @ `203`）；`DefinitionLink` @ `208-216`；`WorkflowRun` @ `218-` | `metadata_json` 当前**无任何消费者**（只存不读）——L0 落点。`scope` 是无约束 free-form str，无 enum/CHECK |
| `store/db.py` | `.../client/store/db.py` | `Store.__init__(cipher=None)` @ `40-49`；`add_definition` 加密 body @ `412`、还原明文 @ `422/428`；`get_definition`/`get_definitions(*, kind, name, active_only=False)` @ `442-466`（`active_only=True` 过滤 @ `458-459`）；`get_active_definition(kind, name)`**位置参数** @ `468-478`；`update_definition` 重加密 body @ `526`；`set_definition_active` @ `480-506`（强制 (kind,name) 恰一 live）；`DefinitionLink` 存取 @ `558-598`，删级联 @ `538-556` | **仅 body 加密**；`scope_json`/`metadata_json` 明文。加密=Fernet，明文/密文行可共存。L1 `work_mode_get` 可直接读返回对象的明文 body |
| `store/migrations.py` | `.../client/store/migrations.py` | `CLIENT_VERSION_TABLE="schemaversion"` @ `25`；`CLIENT_MIGRATIONS` 仅一条 v1 `decisioncard.diff_stat` @ `32-34` | **无任何 Definition 表迁移**——L0 schema 是约定不是 DDL，加 description 路径**无需迁移**（§12 不改表） |
| `agents/_subprocess.py` | `.../client/agents/_subprocess.py` | CLI 以 workspace 为 cwd 启动 @ `95` | P2 文件注入语义依据 |
| `agents/claude_code.py` `codex.py` `runner.py` | `.../client/agents/` | — | P2 两个 CLI 通道分支 |
| `shared/llm/client.py` | `.../shared/llm/client.py` | `Message(role, content)` @ `29-32`；`LLMToolCall(id,name,arguments)` @ `35-39`；`LLMToolResponse(text, tool_calls)` @ `42-45`；`__init__`（keyword-only 可选注入 transport/ws_connect/settings_resolver，各默认 None）@ `90-98`；`complete()` @ `169-189`；`tool_complete()` @ `191-211`（ws 路径内部再调 `self.complete` @ `207`）；`_resolve()`/`settings_resolver` @ `117-130`，`_api_key()` @ `132-143`，`_transport_mode()` @ `146`；key 仅在私有 transport 拼头（`259/287/317/349/373/451`） | 两个 choke point（`complete`/`tool_complete`），provider 无关。`complete()` 即使流式也返回最终累积文本（`335`/`488`）。**不收 session/task**（需 contextvars 关联）。tracer 注入遵循现有 keyword-only 可选注入模式（加 `tracer=None`） |
| `shared/config.py` | `.../shared/config.py` | `Secrets(BaseSettings)` @ `16`，`model_config = SettingsConfigDict(env_prefix="FOREMAN_", env_file=".env", ...)` @ `19`；`Config` 模型 @ `226-243`（**无 `debug` 字段**）；`load_config` @ `253-265` | **仅 `Secrets` 是 env 驱动**；所有结构化段是 config.yaml 纯 `BaseModel`，无 env 绑定。`debug.llm_trace`/`FOREMAN_DEBUG_LLM_TRACE` 是全新结构，需新 `DebugCfg` 段 + 显式 env→config glue（见 §6 P1b-trace） |
| `server/logbuffer.py` | `.../server/logbuffer.py` | `RingBufferHandler(capacity=500)` @ `20-26`，进程级单例 | **内存环**、重启即清、只存 logging 文本记录、**永不存 payload/secret**。与 §8C 的磁盘 JSONL trace 是不同进程、不同 sink、不同敏感度——**不可复用** |
| `server/web/app.js` | `.../server/web/app.js` | zh subtitle `rulesSubtitle` @ `23`；en subtitle @ `103`(设计书写作"app.js:23 中英文 subtitle"，实际 zh@23 / en@103，**须同改两行**)；composer @ `917-957`；`runDispatch` @ `1498-1517`；I18N zh/en 各一份相隔约 80 行 | personal 入口（index.html 加载 app.js）。team 入口 app.html 加载 admin-app.js，与本设计无关 |

> 三处 `_within_any` 提醒：`dispatch_service.py:60`（`str` 入参）、`injector.py:64`（`Path` 入参）语义一致但签名不同；`tools/policy.py:50` 的 `PathGuard._is_relative_or_same` 是 tools 包内等价物。**resolver 在 tools 包内做 scope 过滤时复用 policy.py 版本**，避免 `tools→core` 反向依赖。

---

## 3. 共享约定 / 常量 / Schema / 事件字段（本附录核心）

> 下面每节都标注【落地阶段】= 真正写进代码的阶段；本附录只定稿"长什么样、叫什么、放哪"。各阶段引用本节，不重抄。

### 3.1 预算常量（§8 work-mode 层）—— 唯一定义表

【落地阶段】P1（`20-P1-L1-retrieval-budget-telemetry.md`）。【存放位置约定】新建模块 `src/foreman/client/core/work_mode_context.py`（**不要**塞进 `ToolRuntimeConfig` / `cfg.pm_tools`——后者无 work-mode 字段，§2 已核实）。

| 常量 | 默认 | 含义 | 谁读 |
|---|---|---|---|
| `WORKMODE_MAX_SELECTED` | `8` | 进 L0 索引的最多条数（Tool-RAG 截断 top-K） | resolver 漏斗 §5 |
| `WORKMODE_INDEX_DESC_CHARS` | `200` | **L0 索引里** description 截断长度（注意：比存储上限 1024 小——索引要更省） | resolver 组装 L0 |
| `WORKMODE_INDEX_MAX_TOKENS` | `1500` | 整个 L0 索引块的硬上限；超了再砍 K | resolver / 预算器 |
| `WORKMODE_BODY_MAX_CHARS` | `6000` | 单次 `work_mode_get` 正文上限，超则 `truncated=True` | `work_mode_get` handler |
| `WORKMODE_MAX_PULLS` | `6` | 一次规划内 `work_mode_get` 的最多次数 | runtime / loop 计数 |

注意事项（已核实）：
- 存储上限 `MAX_BODY=200_000`（`definition_service.py:35`）**远高于** `WORKMODE_BODY_MAX_CHARS=6000`。L1 截断是 handler 内**全新逻辑**，不能套 runtime 现有 `_truncate`（阈值 12000，`runtime.py:629-632`）。`ToolResult(..., truncated=True)` 是合法 keyword（`models.py:71`）；body 缺失时**显式**返回 `error="not_found"`，勿仅靠 `call()` 的 try/except 兜底。

### 3.2 现有上下文预算常量（§8B 统一上下文层）—— 三处 12000 的真相

【落地阶段】P1b-context（`31-P1b-unified-context-compression.md`）。这三个常量分属**三个不同模块**，`token` 感知改造要协调 4+ 落点：

| 常量 | 值 | 真实位置 | 用途 / 改造提示 |
|---|---|---|---|
| `DEFAULT_CONTEXT_BUDGET_CHARS` | `12000` | `context_compression.py:15` | `context_pack_to_text` 的 max_chars 默认；**纯 helper，无 LLMClient/Config/model 访问**——token→char 须在调用现场算好后以 int 穿入，或改其签名 |
| `MAX_COMPACT_CHARS` | `12000` | `pm_agent.py:24` | `compact()` 输出 `context_pack_to_text(max_chars=...)`（`pm_agent.py:616`，实际遮蔽上面的默认） |
| `MAX_CONTEXT_CHARS` | `12000` | `dispatch_service.py:46` | `_session_context` 读 `Session.plan` 截断；用于 `252/756/920/922` |
| `MAX_GOAL_CHARS` | `8000` | `dispatch_service.py:39` | goal 截断 @ `122` |
| `MAX_EVENT_CHARS` | `20000` | `pm_agent.py:23` | `events_to_text` 默认；时间线先取末 120 事件、跳过 pm_output/pm_reasoning，再截断 |

token 感知改造的硬事实：
- **仓库无任何静态 per-model `ctx_window` 表**。窗口大小只能从 provider `/models` 的 `context_length` 拿（`client.py:607-649` 的 `_model_infos` 提取 `context_length`/`max_tokens`），而很多 OpenAI 兼容代理**省略** `context_length`。预算器**必须自带一个默认窗口常量**作为 fallback：默认 **32_000 tokens** 【默认 2026-06-24】（保守偏小，宁可多压不可溢出），可 config 覆盖（登记于 §3.11）。
- `cfg.llm.max_tokens` 默认 `2048`（`config.py:90`）是**输出上限**，不是窗口大小。
- `context_length` 当前只有前端 `app.js` 在用（`288-290` 算 `contextLength - outputReserve`）；PM/dispatch 路径从不消费。建议服务端镜像这条前端公式。

### 3.3 lane 分层与预算比例（§8B.3）

【落地阶段】P1b-context。比例按当前 PM 模型窗口算（token 感知），不是写死字符。逐出顺序复用 `EVICT_MEMORY_FIELDS`（`context_compression.py:30-39`）。

| # | Lane | 默认预算 | 逐出优先级（越靠后越受保护） | 稳定性 |
|---|---|---|---|---|
| 1 | 系统提示 + 护栏 + 工具 schema + policy | ~8% | 永不逐出 | 稳定前缀 |
| 2 | 工作方式 **L0 索引** | ≤1500 tok | 永不逐出（每任务重算） | 稳定前缀 |
| 3 | 用户 goal | ≤5% | 永不逐出 | 半稳定 |
| 4 | workflow 当前步状态 | ≤3% | 永不逐出（在 workflow 中） | 半稳定 |
| 5 | 会话压缩记忆（ContextPack） | ≤25% | 按既有逐出顺序，受保护核心最后动 | 半稳定 |
| 6 | 工作方式 **L1 拉取正文** | ≤15% | 用完即弃；绝不持久化进 pack（可重拉） | 易变尾部 |
| 7 | 实时时间线 / 工具结果 | 余量 | **最先压缩** | 易变尾部 |

逐出动作顺序（逼近窗口时，自动）：lane 7 压成 lane 5（ContextPack）→ 丢弃已消费的 lane 6 → pack 内按既有顺序逐出（**护住 top-3 constraints + top-3 verified_facts**）→ lane 1-4 永不动。

护核提醒（已核实，§6 minor）：top-3 保护当前**只在 `context_compression.py:225-241` 的 minimal fallback 分支**生效；主 eviction 循环（`204-215`）只因 `EVICT_MEMORY_FIELDS` 不含 constraints/verified_facts 而没碰它们。P1b 改 lane/eviction 时须把保护提升为主路径显式保证并加测试。

### 3.4 L0 元数据 Schema：`foreman.workmode.meta/1`（§4.2）

【落地阶段】P0（`10-P0-copy-and-L0-metadata.md`）。【存放位置】`Definition.metadata_json`（`store/models.py:203`，明文存，无需迁移）。

```json
{
  "schema": "foreman.workmode.meta/1",
  "description": "<=1024 字：做什么 + 何时用（L0 唯一必填）",
  "when_to_use": "可选，自然语言触发条件",
  "keywords": ["test", "migration"],
  "inputs": [{"name": "target", "required": false, "desc": "..."}],
  "check": {"type": "command", "cmd": "ruff check ."},
  "priority": 0,
  "est_tokens": 1234
}
```

字段约定：
- `description`：**唯一必填**，≤1024 字，照 Agent Skills 约束说"做什么"且"何时用"。这是 L0 索引里唯一进 prompt 的人类语义。空 description → **不进 L0 索引**（fail-closed，靠排除而非写时拒绝实现）。
- `keywords`：供 L0 词法检索（Tool-RAG）。
- `check`（可选）：让 code_standard/qa_rubric 升级为可验证门（§9 / P4）。形如 `{"type":"command","cmd":"..."}`。
- `priority`（可选）：相关性排序 tie-break。
- `est_tokens`：保存时测 body 得出，供预算器估算。

`scope_json` vs `metadata_json` 分工（§4.1）：
- `scope_json`（`models.py:201`）= **硬适用性**：workspace 前缀、agent、path globs、languages。
- `metadata_json`（`models.py:203`）= **选择信号 + 自描述**：上面这套 schema。

**description 必填校验的向后兼容铁律**（§4.3 / §12，blocker 级排序，见 §6）：
1. `_validate`（`definition_service.py:406-418`）当前只校验 metadata_json **能 parse 成 dict**，从不读内部；`description` 既非列也非 service 参数，仅是 metadata_json 内约定。
2. 必填校验**只在 create/update 写路径**生效；**绝不**套用到 `import_bundle` 路径（后者走 `_json_object_or_default` 宽松路径，否则旧 bundle 幂等重导入会破）。create 与 import 校验**有意分歧**，保持。
3. 上线顺序：先让 UI 能填/发 metadata_json + 回填 examples.py 种子（当前 `{"example": true}` 无 description）与存量 definition → 再把 gate 设 fail-closed。次序颠倒会打挂存量/种子重导入。
4. "无 description 不进自动选择" = **resolver 排除**，不是写时拒绝。

### 3.5 `work_mode` telemetry 事件（§8 / §16）

【落地阶段】P1（基础字段）+ P1b（per-lane / auto-compact 字段）。每次派发 emit 一条 `work_mode` 事件。

基础字段（P1）：

```json
{
  "type": "work_mode",
  "selected": [{"id": "...", "kind": "skill", "name": "...", "est_tokens": 1234}],
  "dropped":  [{"id": "...", "kind": "...", "name": "...", "reason": "top_k|scope|budget"}],
  "index_tokens": 1180,
  "pulls": [{"name": "...", "kind": "...", "body_tokens": 980, "truncated": false}],
  "body_tokens": 980,
  "kinds": {"skill": 3, "code_standard": 2, "qa_rubric": 1}
}
```

P1b 追加字段（§8B.8）：

```json
{
  "per_lane_tokens": {"system": 1600, "l0_index": 1180, "goal": 420,
                      "workflow_step": 0, "session_memory": 3100,
                      "l1_pulls": 980, "timeline": 5200},
  "auto_compact": {"triggered": true, "before_tokens": 14820, "after_tokens": 6010, "trigger": "threshold_70pct"}
}
```

事件回答的运营问题（§16）：每任务 work-mode token（趋势应平，不随 active definition 总数线性涨）；pull 命中率（`work_mode_get` 调用数 / 候选数）；selected/dropped 比；review 因 rubric 触发 follow-up 的比例。

### 3.6 LLM debug trace（§8C.2）—— JSONL 一条一调用

【落地阶段】P1b-trace（`30-P1b-llm-debug-trace.md`，优先做）。挂在 `LLMClient.complete()`/`tool_complete()` 两个 choke point。默认**关**。

```json
{
  "ts": "2026-06-24T08:00:00Z", "seq": 42,
  "session_id": "...", "task_id": "...",
  "phase": "plan|review-2|compact|tool-round-3|operator|auditor|briefing",
  "provider": "openai", "model": "gpt-5.5", "transport": "http", "json_mode": true,
  "request":  {"system": "...", "messages": [{"role":"user","content":"..."}],
               "tools": [{"name":"work_mode_get", "...": "..."}]},
  "response": {"text": "...", "tool_calls": [{"name":"...","arguments":{}}]},
  "metrics":  {"req_chars": 18234, "resp_chars": 2210,
               "approx_req_tokens": 4560, "approx_resp_tokens": 553, "latency_ms": 8123},
  "error": null
}
```

字段映射到现有 dataclass（已核实）：`request.messages`←`Message(role,content)`（`client.py:29-32`）；`request.tools`←`tool_complete` 的 `tools: list[dict]`（provider-native schema，含 risk 之外的 name/description/input_schema）；`response.text`/`response.tool_calls`←`LLMToolResponse`（`client.py:42-45`）；`provider`/`model`/`transport`←`_resolve()`/`_transport_mode()` 的**每请求结果**（不是 `cfg.llm.*`，因 settings_resolver 可每请求覆盖）。

trace 关键约定：
- **关联 = `contextvars.ContextVar`**（全仓现无 contextvars，全新）。set-point 必须覆盖**全部 8 个调用方**：`pm_agent` plan / review / compact、`operator.py:212`、`auditor.py:305`、`briefing.py:236`、`supervisor.py:459`、`loop.py:_complete`（`154/167`）。漏任一个其 trace 的 `phase`/`session_id` 为 null。
- trace 的 `seq` / ids 与 §3.5 `work_mode` 事件**共享同一 contextvar 来源**，使聚合指标可一键钻到原始 payload。两边的 id 字段在 **P1/P1b-trace 之间对齐定稿**，避免各定义对不上。
- **ws 防重入**：`tool_complete()` 在 ws 路径内部再调 `self.complete()`（`client.py:207`），tracer 若朴素包两个公开方法会一次 emit 两条 → `seq`/token 在 ws 路径虚高。tracer 只记最外层，或 ws 路径跳过内层 complete。
- `phase=tool-round-N` 的 contextvar 要设在 `loop.py:_complete` 周围，不是外层 loop，否则每轮归属不清。
- `tool_complete()` **无 on_stream**（非流式）；"流式记最终累积文本"只适用 `complete()`（其返回值即最终文本，`335`/`488`）。

trace 开关与落盘（§8C.4-8C.5）：
- 开关：config `debug.llm_trace=true` 或 env `FOREMAN_DEBUG_LLM_TRACE=1`。**注意**：env_prefix 只对 `Secrets(BaseSettings)` 生效；`debug` 是全新结构化段，需新 `DebugCfg` BaseModel + 显式 env→config glue。
- 落盘：`debug.log_dir`（默认 `.foreman/debug/`），按 session 一文件 `llm-trace-<session_id>.jsonl`，append-only。**本地 only、不上 server/cloud、不进 git、单文件大小上限 + 轮转 + 保留期**（仓库无现成 rotation helper，自建）。key 因 message 层记录天然不入，再加一道兜 `sk-*`/`Bearer` 的 redactor。
  - 轮转/保留默认 【默认 2026-06-24】：单文件 ≤50 MB、保留最近 20 文件或 14 天（先到先汰），可 config 覆盖（登记于 §3.11）。

### 3.7 scope 字符串常量（§8B.5 / §8B.6）

【落地阶段】P1b-context 先建 writer + 常量，P5 复用。

- `MemoryItem.scope`（`models.py:63`）是**无约束 free-form str**，无 enum/CHECK；`get_memory_items(scope=...)` 做**精确串匹配**，typo 静默不匹配。
- 合法值（注释约定）：`session` | `workspace` | `workflow` | `user`。
- 当前**唯一 writer** `_store_context_derivatives`（`dispatch_service.py:791/799`）**硬编码 `scope="session"`**。写 `scope="workflow"`（步边界压缩记忆，§8B.6）需**全新 writer 路径**。
- **约定**：把 scope 值集中定义为常量（如 `work_mode_context.py` 内 `SCOPE_SESSION="session"` …），所有 writer 引用常量，杜绝 typo。

### 3.8 source_ref 命名约定（§8B.5）

- 工作方式 L1 正文压缩时**不存全文**，只记指针：`source_ref = "workmode:<kind>:<name>@v<ver>"` + 一行"为何拉取/如何应用"。落进**已存在的** `retrieved_evidence` 顶层容器（`context_compression.py:137`，先于 memory 字段逐出）或 `MemoryItem`。
- 因 standard 产生的决策/约束落 `MemoryItem(kind=decision/constraint)`，被既有逐出规则保护（要活过压缩）；规范逐字原文不必。
- `COMPACT_SYSTEM`（`pm_agent.py:61-75`）须升级：明示"不要把 skill/standard/qa 逐字正文抄进 pack，记成带 `workmode:` source_ref 的引用 + 一行应用说明；保留应用它们而产生的决策与约束"。

### 3.9 目录 / 文件命名约定（P2 通道）

【落地阶段】P2（`40-P2-coding-agent-channel.md`）。

- claude-code skill：`.claude/skills/foreman-<slug>/SKILL.md`，frontmatter `name: foreman-<slug>`（小写连字符 <64，与目录名一致）+ `description`（<1024）。`foreman-` 前缀隔离用户自有 skills；clear 只删 `foreman-*`。**全新写法**，不复用现有 `_write_skills`（后者写 `.foreman/skills/<slug>.md` 纯 md、无 frontmatter）。
- codex skill：`.foreman/skills/<slug>.md`（`SKILLS_DIR`，`injector.py:40`）+ `AGENTS.md` 托管块放 L0 索引（名字+描述+路径，不放正文）。
- 托管块标记须带 `task_id`（当前 `MARKER_BEGIN/END` 是固定常量、无 id，`injector.py:36-37`）；skills 写到带 task_id 子目录，clear 只删本任务子目录（防并发误删，blocker 见 §6）。
- 勿提交：注入 `.foreman/`、`.claude/skills/foreman-*`、`CLAUDE.md`/`AGENTS.md` 托管块、`.foreman/debug/` 进 workspace `.git/info/exclude`（当前 injector 无此逻辑，全新）。

### 3.10 ToolSpec / handler 接缝（P1，§6）

【落地阶段】P1。三接缝点（全部 `runtime.py`，已核实可编辑）：①`specs()` list（`84-203`）加两条 `ToolSpec`；②handler 加为 `PMToolRuntime` 方法；③`call()` 链（`242-260`）加分支，**插在 `browser_` 前缀判断（`258-259`）之后、`unknown_tool` 兜底（`260`）之前**。`ToolSpec` 第四参 `risk=SAFE`（`models.py:21`，`to_native` 不发 risk）。设计书 §6 的 ToolSpec/schema 即权威定义，照抄。

> **原子 PR 铁律**（§6/依赖）：`ToolSpec` + handler + dispatch 分支 + `from_config` 增 `resolver=None` + `local_app.py:166` lambda 增传 + `pm_agent.py` 的 L0 注入 system，**必须同一个 PR 落**。拆开会出现 `from_config` 有参数没人传、handler 拿到 `resolver=None`。

### 3.11 跨阶段已拍板决定与默认值（2026-06-24）

本节是这些决定/数值的**单一权威登记处**；各阶段文档引用本节，不重抄。决定的完整裁决理由与全文见 [`01-REVIEW-FINDINGS.md`](01-REVIEW-FINDINGS.md) §4.0。

**数值默认（可后续在 config 覆盖）：**

| 项 | 默认值 | 含义 / 改造提示 | 落地阶段 |
|---|---|---|---|
| trace 轮转/保留 【默认 2026-06-24】 | 单文件 ≤50 MB；保留最近 20 文件**或** 14 天（先到先汰） | LLM debug trace 自建 rotation helper（仓库无现成），按本默认实现（§3.6） | P1b-trace（`30-...`） |
| auto-compact 触发 【默认 2026-06-24】 | 窗口占用 ≥70% **或**每 8 个 run（N=8），先压缩再继续 | 触发器二选一命中即压缩；与增量 review 窗口须统一（§6 minor） | P1b-context（`31-...`） |
| token→char fallback 窗口 【默认 2026-06-24】 | provider `/models` 未返回 `context_length` 时按 **32_000 tokens** 估（保守偏小，宁可多压不可溢出），可 config 覆盖 | 即 §3.2 要求的"预算器自带默认窗口常量" | P1b-context（`31-...`） |

**约定（已锁定）：**

- 【已拍板 2026-06-24】D1（设计书 §7.1 vs §8B.7 矛盾裁决）：**`code_standard` 全文进托管块**——整段规范写进 workspace 根 `CLAUDE.md`/`AGENTS.md` 托管块，每轮重读、能活过 CLI auto-compact（与 §8B.7 一致，接受 `CLAUDE.md` 可能变大）；**skill 仍走「L0 索引 + 文件」渐进，不全文进托管块**。落地 P2（`40-...`）。
- 【已拍板 2026-06-24】D2（§9 / P4 check 硬执行）：**本阶段只做软约束**，硬执行门推迟到 V2，P4 当前不实现。详见 [`01-REVIEW-FINDINGS.md`](01-REVIEW-FINDINGS.md) §4.0。
- 【已拍板 2026-06-24】D3（§4.3 / §12 存量 definition description）：**批量 LLM 回填纳入 P0 一并交付**（P0 因此变重，引入 PM LLM 调用与人工抽检）。详见 [`01-REVIEW-FINDINGS.md`](01-REVIEW-FINDINGS.md) §4.0。
- 【已拍板 2026-06-24】D4（§13 P1 / M1 composer 手选）：**勾选 UI 归 P0、UI 先行**（P0 加勾选控件 + 本地选择状态，并把 `work_mode_ids` 作为"接受但暂不消费"的可选字段），**真正消费（resolver 用这些 id 手选直通/过滤）仍在 P1**。详见 [`01-REVIEW-FINDINGS.md`](01-REVIEW-FINDINGS.md) §4.0。

---

## 4. 验收标准（本附录自身的"对"）

本附录无运行时行为，验收点是**一致性与可引用性**：

- [ ] 每个新增常量（§3.1 / §3.2）在本附录有**唯一**定义；各阶段文档引用本表而非重定义；默认值与设计书 §8 / §8B 一致。
- [ ] L0 Schema（§3.4）字段集与设计书 §4.2 完全一致；description 必填的向后兼容铁律（4 条）写明。
- [ ] `work_mode` 事件（§3.5）与 LLM trace（§3.6）字段表覆盖设计书 §16 / §8C.2 全部字段；二者**共享 id 来源**的约定写明。
- [ ] 文件路径映射表（§2）每行有真实绝对路径 + 核实 `file:line`；与设计书行号有出入处均标注 "(设计书写作 X，实际 Y)"。
- [ ] scope 字符串（§3.7）、source_ref 格式（§3.8）、目录命名（§3.9）有唯一约定，被 P1b/P2/P5 引用。
- [ ] 术语表（§8）覆盖 L0/L1/L2、lane、ContextPack、MemoryItem、stable_prefix、Tool-RAG、渐进式披露、choke point。

（设计书 §14/§15 的**运行时**验收点归属各 Pn 阶段文档，不在本附录复述。）

---

## 5. 测试

本附录是文档，无单元/集成测试。**但它定义的常量/事件字段是各阶段测试的断言依据**：

- P1 集成测试（打 tool-loop 真实路径）断言"L0 索引 ≤ `WORKMODE_INDEX_MAX_TOKENS`""`work_mode` 事件字段齐全"时，字段名/阈值**以本附录 §3.1 / §3.5 为准**。
- P1b-trace 测试断言"每次 complete/tool_complete 产一条 JSONL，含完整 request/response + phase/ids/metrics"时，JSONL schema **以本附录 §3.6 为准**；并断言 ws 路径不双记、key 不入。
- P1b-context 测试断言 lane 预算、护核、确定性序列化时，lane 表/比例**以本附录 §3.3 为准**。
- 任何阶段测试**禁止只测非生产的 `build_plan_prompt`**（设计书 §14）；集成测试必须断言内容进了实际发给 `LLMClient` 的入参。

维护约定：**任一阶段实现中发现本附录的常量/字段/行号需要改动，必须回改本附录**（单一事实源），并在该阶段 PR 描述里点名"已同步 90-conventions"。

---

## 6. 风险与回滚（呼应评审 findings）

本附录无代码、无回滚。下面汇集**全程**会被各阶段踩的坑（评审 blocker/major 摘要），各阶段文档应链接回这里而非各写一份：

- **[blocker] P2 inject↔clear 是从零建，不是复用**：`dispatch_service.py` 零 `injector`/`inject`/`clear` 引用；成对逻辑只活在**零实例化**的 `WorkflowEngine`。普通 PM 派发路径无 inject。P2 须：给 `DispatchService` 注入 `WorkspaceInjector`（`local_app.py:158` 接线）、自建 material、在 `_pm_launch` 三个返回出口 clear（而非每次 `runner.wait` 后）。
- **[blocker] P2 并发隔离 + clear 误删**：标记是固定常量无 task_id；`clear()` 无条件 `rmtree(.foreman/skills)` 会删并发任务文件。task_id 隔离必须与 P2 接线**同批**落。
- **[major] P1 `work_mode_ids` 跨三层连改**：`create()`/`_pm_launch`/server `_DispatchBody`/`/api/tasks`/composer/`runDispatch` 全部缺该参数。须保持无 `work_mode_ids` 旧请求兼容（§12）。
- **[major] P0 description 必填的向后兼容陷阱**：见 §3.4 的 4 条铁律。先 UI 能填 + 回填 examples/存量 → 再 fail-closed；import 路径不套同一硬校验。
- **[major] P1b 三处 12000 跨 3 模块 + 无 ctx_window 表**：见 §3.2。token→char 在调用现场算，预算器自带 fallback 窗口常量。
- **[major] P1b KV-cache vs 热更新张力**：`context_pack_to_text` 用 `json.dumps(indent=2)` 非 sort_keys、eviction 原地 `pop`，跨轮字节不稳；须改 `_dump` 为 sort_keys + 无时间戳 + 固定字段序（对现有压缩输出是行为变更，需回归）。L0 只在任务边界换、任务内 append-only。
- **[major] P1b-trace config debug 段全新 + 8 set-point + ws 双记**：见 §3.6。
- **[major] P0/P1 L0 真实注入点在 `pm_agent.py` 不在 `loop.py`**：`PMToolLoop` 只重发调用方 messages（`loop.py:40-53`）。集成测试断言 L0 进了实际发给 `LLMClient` 的入参，不是 `build_plan_prompt` 字符串。
- **[major] P1 resolver 注入是原子改动 + 路径判断归属**：见 §3.10 原子 PR 铁律；scope 路径包含判断复用 `tools/policy.py` 的 `PathGuard`（同包），勿裸 import `dispatch_service._within_any`。
- **[minor] `max_rounds` 双来源**：`ToolRuntimeConfig.max_rounds`（`models.py:105`，从 cfg 透传）与 `PMToolLoop` 构造参数（`loop.py:32` 默认 6）独立，loop 不读 runtime.cfg。想用 config 调轮数须显式接线。
- **[minor] 自动压缩触发 vs 增量 review 窗口冲突**：`compact()` reload 全部 events + 重写 `Session.plan`；循环用 `reviewed_event_id` 增量跟踪。auto-compact 插入时须统一两套机制。触发阈值 【默认 2026-06-24】：窗口占用 ≥70% 或每 8 个 run（N=8），先压缩再继续（登记于 §3.11）。
- **[minor] web subtitle zh@23 / en@103 须同改**；新增 description/metadata 输入框须成对加 zh/en i18n 键。
- **[note] WORKMODE_BODY_MAX_CHARS 截断、scope=workflow writer、_build_block 安全措辞、standards 全文 vs 精简**：见各阶段文档与 §3.x。

---

## 7. 与设计书 / 其它阶段的对应

| 本附录节 | 设计书章节 | 落地阶段（下游依赖本附录定稿） |
|---|---|---|
| §2 文件路径映射 | §2 现状 / 全程 | 全部阶段 |
| §3.1 work-mode 预算常量 | §8 | P1（`20-...`） |
| §3.2 三处 12000 + token 感知 | §8B.1 / §8B.8 | P1b-context（`31-...`） |
| §3.3 lane 分层 | §8B.3 | P1b-context |
| §3.4 L0 Schema + description 兼容 | §4.2 / §4.3 / §12 | P0（`10-...`）、resolver in P0、消费 in P1 |
| §3.5 `work_mode` telemetry | §8 / §16 | P1（基础）+ P1b（per-lane/auto-compact） |
| §3.6 LLM debug trace | §8C / §8C.2 | P1b-trace（`30-...`） |
| §3.7 scope 字符串 | §8B.5 / §8B.6 | P1b-context（建 writer）、P5（`70-...`，复用） |
| §3.8 source_ref + COMPACT_SYSTEM | §8B.5 | P1b-context |
| §3.9 目录命名 | §7 / §11 | P2（`40-...`） |
| §3.10 ToolSpec 接缝 | §6 | P1 |
| §3.11 跨阶段已拍板决定与默认值（2026-06-24） | §7.1 / §8B.7 / §9 / §4.3 / §12 / §13 | P0（D3/D4 UI）、P1（D4 消费）、P1b（N1/N2/N3）、P2（D1） |
| §6 风险汇总 | §11 / §14 + 评审 findings | 全部阶段（链接回本节） |

阶段文档清单（交叉链接用）：
- [`00-OVERVIEW-AND-SEQUENCING.md`](00-OVERVIEW-AND-SEQUENCING.md) — 索引与排期
- [`01-REVIEW-FINDINGS.md`](01-REVIEW-FINDINGS.md) — 评审结论全表
- [`10-P0-copy-and-L0-metadata.md`](10-P0-copy-and-L0-metadata.md) — P0 文案 + L0 元数据 + 选择漏斗
- [`20-P1-L1-retrieval-budget-telemetry.md`](20-P1-L1-retrieval-budget-telemetry.md) — P1 L1 拉取工具 + 预算 + 度量
- [`30-P1b-llm-debug-trace.md`](30-P1b-llm-debug-trace.md) — P1b-trace LLM 调试追踪（优先做）
- [`31-P1b-unified-context-compression.md`](31-P1b-unified-context-compression.md) — P1b-context 统一上下文与压缩
- [`40-P2-coding-agent-channel.md`](40-P2-coding-agent-channel.md) — P2 coding-agent 文件注入通道
- [`50-P3-tool-rag-upgrade.md`](50-P3-tool-rag-upgrade.md) — P3 词法→embedding 升级
- [`60-P4-hard-enforcement.md`](60-P4-hard-enforcement.md) — P4 硬执行门
- [`70-P5-workflow-control-flow.md`](70-P5-workflow-control-flow.md) — P5 workflow 控制流

推荐实现顺序：`P0 → P1 → P1b-trace → P1b-context → P2 → P3 → P4 → P5`。

---

## 8. 术语表

| 术语 | 含义 |
|---|---|
| **渐进式披露（progressive disclosure）** | 把一条 definition 拆成 L0/L1/L2 三层按需加载，而非整段灌入。源自 Anthropic Agent Skills。 |
| **L0 / 发现层** | 始终常驻系统提示的轻量索引：`{kind,name,description,est_tokens}`，~100 tok/条，**不含 body**。 |
| **L1 / 激活层** | 模型判断相关时才取的 definition 正文（body）。PM 通道经 `work_mode_get` 拉取；coding-agent 通道经 SKILL.md / `.foreman/skills`。 |
| **L2 / 执行层** | 正文里引用的附加脚本/附件，用到时才读。 |
| **Tool-RAG** | 选项多时按相关性只取 top-K，而非全量发送（RAG-MCP）。本设计 V1 用词法、V2 换 embedding（P3）。 |
| **resolver / `resolve_work_mode_context()`** | P0 的三步漏斗（硬过滤 scope → 词法排序 → top-K），输出 L0 索引（不含 body）。P1 的 `work_mode_search/get` 背后即它。 |
| **lane（车道）** | §8B.3 把一次 PM 调用的窗口分成 7 条有序车道，各有预算与逐出优先级。 |
| **ContextPack** | `context_compression.py` 的压缩产物：`stable_prefix`/`session_state`/`working_memory`(verified_facts vs claims 分离)/`retrieved_evidence`/`dynamic_tail`/`omitted`。`source_refs` 是 per-item key，**非顶层字段**。 |
| **MemoryItem** | `store/models.py:57-78` 的结构化记忆，带 `importance`/`confidence`/`supersedes`/`scope`。scope 是无 enum 的 free-form str。 |
| **scope** | MemoryItem 适用范围：`session`/`workspace`/`workflow`/`user`。精确串匹配，typo 静默失败——须用常量。 |
| **稳定前缀（stable prefix）** | KV-cache 友好的不变头部（lane 1-2）。单 token 变化会失效其后缓存（10× 成本差）。L0 须确定性序列化。 |
| **append-only** | 上下文只追加、不改写过去内容，保 KV-cache 命中。 |
| **compact / auto-compact** | 接近窗口上限时把轨迹总结后重启窗口，保留决策/约束、丢冗余工具输出。Foreman 现仅手动（`DispatchService.compact`），P1b 加自动触发。 |
| **choke point** | 所有 PM 大脑路径必经的两个方法 `LLMClient.complete()`(`client.py:169`) 与 `tool_complete()`(`191`)。trace 只挂这两处即覆盖全部 provider/调用方。 |
| **source_ref** | JIT 指针。工作方式正文压缩后记 `workmode:<kind>:<name>@v<ver>`，正文可重拉。 |
| **托管块（managed block）** | `CLAUDE.md`/`AGENTS.md` 中由 `MARKER_BEGIN`/`MARKER_END`(`injector.py:36-37`) 框定、Foreman 独占的区段。inject↔clear 成对。 |
| **手选 / 自动** | 手选 = composer 勾的 id 直通漏斗步 2/3（仍受总预算约束）；自动 = 默认仅 L0 常驻、正文靠 L1 拉取（便宜）。 |
| **definition / 四种 kind** | `workflow` / `skill` / `code_standard` / `qa_rubric`（`KNOWN_KINDS`，`definition_service.py:29`）。 |
