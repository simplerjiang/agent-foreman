# 审阅结论 — 工作方式生产级接入设计书评审

> 日期：2026-06-24 ｜ 评审对象：[`WORK_MODE_EFFECTIVE_INTEGRATION_DESIGN.md`](../WORK_MODE_EFFECTIVE_INTEGRATION_DESIGN.md)（v2 重写版） ｜ 基线：`origin/main` 的 `1801128`
> 配套：总览与排期见 [00-OVERVIEW-AND-SEQUENCING.md](00-OVERVIEW-AND-SEQUENCING.md)；常量/Schema/路径映射见 [90-conventions-and-glossary.md](90-conventions-and-glossary.md)。
> 本文件汇总：① 总评 ② 分级 findings（blocker/major 各自展开）③ 设计书引用更正表 ④ 开发前必须拍板的 open questions ⑤ 一句话结论。

---

## 1. 总评

**成熟度**：设计书 v2 的架构方向是对的且相当成熟——把「全量灌入」的 demo 反模式，重写成业界主流的**三层渐进式披露（L0 索引 / L1 按需正文 / L2 执行）+ Tool-RAG 检索 + token 感知预算 + 压缩协同 + 调试追踪**，并诚实标注了过度承诺边界（§17）。它正确识别了「能力都在但零运行时接线」这一现状，并复用了仓库既有的成熟件（`ContextPack` 的 `verified_facts`/`claims` 分离、逐出顺序、`MemoryItem` 的 scope 字段、`metadata_json` 现成落点）。

**主要优点**：

- 渐进式披露 + 「正文绝不进任何 prompt 的常驻部分」是清晰、可线性扩展的主线。
- 上下文预算从「拍脑袋截断」升级为「量化 + 可观测的 telemetry」。
- 安全边界（untrusted definition body、秘方不出本地进程、trace 敏感度等同本地 DB）想得周到。
- 分阶段「每阶段都生产级、独立 PR、独立可验收」的拆法务实。

**整体可行性**：**可行**。架构无需推翻。但**设计书在「现状/可复用」的描述上系统性偏乐观**——多处把「需从零新建/连改多层」写成「复用现成、一句话透传」。下文 findings 的实质不是「方向错」，而是「**改动量与接线复杂度被设计书低估**」，以及若干 `file:line` 与实际有出入。这些已在各阶段文档就地校正并落成有序任务。

**最关键的系统性风险**（贯穿多个 finding）：凡设计书说「复用现成 X」处，实现者务必先 grep 验证 X 是否真被实例化/调用——本设计书有数处「现成逻辑只活在零实例化的类里」或「现成函数从不被该路径调用」。

---

## 2. 分级 findings

| # | 级别 | 区域 | 影响阶段 |
|---|---|---|---|
| B1 | **blocker** | P2 生命周期接线（§7.3 inject→clear） | P2 |
| B2 | **blocker** | P2 并发隔离 + clear 误删（§7.3） | P2 |
| M1 | major | P1 `work_mode_ids` 透传跨三层（§13 P1） | P0, P1 |
| M2 | major | P0 description 必填的向后兼容陷阱（§4.3/§12） | P0 |
| M3 | major | P1b 三处 12000 改 token 感知的连带面（§8B.8） | P1b-context |
| M4 | major | P1b KV-cache 稳定前缀 ↔ definition 热更新张力（§8B.4 vs §8B.5/§17） | P1b-context |
| M5 | major | P1b-trace 与 telemetry id 共用 + 8 个调用点（§8C.3） | P1b-trace, P1 |
| M6 | major | P1b-trace ws 路径双记 + tool_complete 非流式（§8C.1） | P1b-trace |
| M7 | major | P1b-trace config debug 段是全新结构（§8C.4） | P1b-trace |
| M8 | major | L0 真实注入点不在 `loop.py`（§6/§8.1） | P0, P1 |
| M9 | major | resolver 注入是原子改动 + Windows 路径复用归属（§5/§6） | P0, P1 |
| m1 | minor | P1 `max_rounds` 双来源（§8.1） | P1 |
| m2 | minor | P1b auto-compact 触发器与增量 review 窗口冲突（§8B.8） | P1b-context |
| m3 | minor | P1b verified_facts/claims 保护只在 fallback 分支（§8B.3） | P1b-context |
| m4 | minor | P0 web subtitle 文案 zh/en 两处 + i18n 成对（§13 P0） | P0 |
| m5 | minor | P2 claude-code 原生 skills 是全新写法（§7.1） | P2 |
| m6 | minor | `_build_block` 安全措辞缺失 + standards 全文 vs 精简（§7.1/§11） | P2 |
| n1 | note | `WORKMODE_BODY_MAX_CHARS=6000` 截断是 handler 内全新逻辑（§8/§6） | P1 |
| n2 | note | `scope='workflow'/'workmode'` MemoryItem 是全新 writer，scope 无校验（§8B.5/§8B.6） | P1b-context, P5 |

### 2.1 Blocker（必须解决，否则上线即损坏）

#### B1 — P2 生命周期接线：inject→clear 并非「现成可复用」

设计书 §7.3 把「`DispatchService` 在 `runner.wait` 后调 `injector.clear`，inject↔clear 成对」描述成现状/可复用。**实际：`dispatch_service.py` 完全不引用 `injector`/`inject`/`clear`。** 现成的成对逻辑只活在 `WorkflowEngine`（`_inject`/`_clear`），而 `WorkflowEngine` 全仓零实例化（`local_app.py` 不构造它）。普通 PM 派发路径根本没有 inject，也就没有 clear 可复用。

**建议（P2 必须从零做）**：① 给 `DispatchService` 新增构造参数注入 `WorkspaceInjector`（`local_app.py:158` 接线）；② 在 launch 前造一份等形 material（PM 通道无现成 material builder，需自建，形如 `WorkflowEngine._resolve_material` 产的 `{instruction, skills, standards}`）并调 inject；③ 在 `_pm_launch` 的三个返回出口（约 581/588/595）而非每次 `runner.wait` 后调 clear（用 try/finally）。**改动量远大于设计书一句话暗示。**

#### B2 — P2 并发隔离 + clear 误删

托管块标记是固定常量 `MARKER_BEGIN/END`（`injector.py:36-37`），**无 task_id**；`_upsert_block` 整段替换。同 workspace 两个并发任务会互相覆盖 `CLAUDE.md` 托管块；更糟的是 `clear()` 无条件 `rmtree(.foreman/skills)`（`ignore_errors=True`），并发任务 A 结束 clear 会连任务 B 的 skill 文件一起删。

**建议**：task_id 隔离**必须与 B1 接线同批做**，不能留到后面：marker 携带 task_id，`_block_span` 按 id 选块；skills 写到带 task_id 的子目录，clear 只删本任务子目录。否则一上线就有并发数据损坏。

### 2.2 Major（设计书低估了改动量/接缝，须按更正实现）

#### M1 — `work_mode_ids` 透传跨 server+client+web 三层且全部缺失

设计书把「`work_mode_ids` 透传 `create()→_pm_launch`」压成一句，实际是跨三层的连改且全部缺失：`create()`（`dispatch_service.py:103-113`）与 `_pm_launch`（`486-495`）签名无该参数；server `_DispatchBody`（`app.py:180-189`）无字段；`/api/tasks`（`app.py:1375-1402`）不向 `dispatcher.create` 传；composer UI（`app.js:917-957`）无勾选控件、`runDispatch`（`1498-1517`）body 不含该字段。手选功能端到端不存在。

**建议**：拆成显式子任务，全程保持无 `work_mode_ids` 旧请求兼容（§12）；并把 composer 勾选 UI 明确归到 P0/P1 边界——否则 P0 的 resolver 做好了也没有产品入口能喂手选 id。

#### M2 — description 必填校验的向后兼容陷阱

`description` 当前既不是列也不是 service 参数，只是 `metadata_json` 这个不透明 JSON 里的约定；`_validate` 只校验 `metadata_json` 能 parse 成 dict（`definition_service.py:406-418`），从不读内部。`examples.py` 种子写的是 `{"example":true}` 无 description。若 P0 直接在 create/update 加「`metadata.description` 必填、fail-closed」，会立刻打挂存量 definition、种子重导入、以及 `import_bundle`（走 `_json_object_or_default` 的独立宽松路径）的幂等重导入。

**建议（P0 顺序硬约束）**：先让 UI 能填/发 `metadata_json`（编辑器加输入框 + `saveDefinition` 发 `metadata_json`，后端已就绪，纯前端）→ 先回填 examples 与存量 → 再把「必填」gate 设为 fail-closed，且 **import 路径不得套用同一硬校验**。「无 description 不进自动选择」用 **resolver 排除**实现，而非写时拒绝。

#### M3 — 三处 12000 常量改 token 感知的连带面

设计书写成「一个 token 感知预算器」，实际三处常量分属三个不同所有权模块且耦合 4+ 落点：`DEFAULT_CONTEXT_BUDGET_CHARS`（`context_compression.py:15`，纯 helper，无 LLMClient/Config/model 访问）、`MAX_COMPACT_CHARS`（`pm_agent.py:24`）、`MAX_CONTEXT_CHARS`（`dispatch_service.py:46`，另用于 `_session_context`/`_fallback`）。`context_pack_to_text` 只收 `max_chars:int`，无法自行拿 ctx_window。此外仓库无任何静态 per-model ctx_window 表，window 只能从 provider `/models` 的 `context_length` 拿（很多 OpenAI 兼容代理省略该字段）。

**建议**：token→char 换算在调用现场（`pm_agent.compact:616`、`dispatch_service:252/756`）算好后以 int 往下穿，或改 `context_pack_to_text` 签名；预算器须自带默认窗口常量作为 `context_length` 缺失时的 fallback；明确这是 **4+ 处协调改动**，不是单点替换。

#### M4 — KV-cache 稳定前缀 与 definition 热更新的张力

设计书要求 L0 索引确定性序列化纳入 `ContextPack.stable_prefix` 以保 KV-cache 命中，同时又要求 L0「每任务由 resolver 重新解析（definition 会热更新）」。但现有 `context_pack_to_text` 用 `json.dumps(indent=2)` 且非 `sort_keys`，eviction 用 `pop` 原地改 dict（`context_compression.py:196/208/322-323`），同一逻辑 pack 跨轮序列化字节不稳定——确定性序列化在现状不满足。且热更新的 definition 本就会改变前缀字节，频繁热更新会持续打掉缓存。

**建议**：把 L0 序列化做成 `sort_keys`+无时间戳+固定字段序（对现有 `_dump` 是行为变更，影响既有压缩输出，需回归）；明确热更新只在任务边界换 L0、任务内 append-only 不重排；**接受「热更新 vs 缓存命中」本质权衡，文档化而非假装两全**。

#### M5 — trace 与 §16 telemetry id 共用 + 8 个调用点

设计书称 trace 的 `seq`/ids 与 §16 `work_mode` telemetry 共用、用 contextvars 关联。但仓库**零** `contextvars`/`ContextVar`/`tracer`（全新），且 `complete()`/`tool_complete()` 不收 session/task。set-point 必须覆盖 8 个独立调用点（pm_agent plan/review/compact、`operator.py:212`、`auditor.py:305`、`briefing.py:236`、`supervisor.py:459`、`loop.py:154/167`），漏任一个其 trace 的 phase/session_id 为 null，也就无法与 telemetry 对账。

**建议**：枚举全部 8 个 set-point；定义 trace 与 telemetry 共享的 id 来源（同一 contextvar），在 §14 验收里断言「seq 单调 + 按 ids 能对上 work_mode 事件」；把 telemetry 事件 schema 与 trace ids 字段在同一步定稿（P1↔P1b-trace 对齐）。

#### M6 — ws 路径双记 + tool_complete 非流式

ws transport 的 `tool_complete()`（`client.py:206-208`）内部再调 `self.complete()`，若 tracer 朴素地包住两个公开方法，一次 ws `tool_complete` 会 emit 两条 trace，使 seq/token 在 ws 路径虚高。另 `tool_complete` 无 `on_stream`（非流式），§8C「流式记最终累积文本」只适用 `complete()`。

**建议**：tracer 须防重入（只记最外层，或 ws 路径跳过内层 `complete`）；`phase=tool-round-N` 的 contextvar 要设在 `loop.py:_complete` 周围而非外层 loop，否则每轮归属不清。

#### M7 — config debug 段是全新结构

设计书称沿用现有 `FOREMAN_` pydantic settings，但只有 `Secrets(BaseSettings)` 是 env 驱动（`config.py:16-19`）；所有结构化段（含未来 debug 段）是从 `config.yaml` 加载的纯 `BaseModel`，无 env 绑定。`Config` 模型（226-243）无 debug 字段。`FOREMAN_DEBUG_LLM_TRACE` 不会自动生效。

**建议**：P1b-trace 前先排一个小的 config 管线任务：新增 `DebugCfg` BaseModel 段 + 显式 env→config glue。同时自建 `.foreman/debug/` 目录常量 + 大小轮转/保留期（仓库无现成 rotation helper）。

#### M8 — L0 索引真实注入点不在 `loop.py`

设计书反复说「L0 索引写进 `PMToolLoop` 的 system message，每轮都在」。`PMToolLoop.run()` 只是把调用方传入的 messages 每轮原样重发（`loop.py:40-53`），system message 的实际拼装在 `PMAgent`（`pm_agent.py`）。但 `PMAgent.plan` 的生产路径是 `build_plan_prompt`（459）产基础 prompt 再追加 tool runtime context（471-477）——§14 又禁止「只测非生产 `build_plan_prompt`」。

**建议**：实现前先在 `pm_agent.py` 定位 `messages[0]=system` 的组装处，明确 L0 索引插入点（建议工具循环 prompt/system，与 `build_plan_prompt` 产物分离），并在集成测试断言 L0 进了**实际发给 `LLMClient` 的入参**，而非 `build_plan_prompt` 字符串。

#### M9 — resolver 注入是原子改动 + Windows 路径复用归属

§6 handler 伪码假设 `PMToolRuntime` 持有 `self._work_mode_resolver`，但 `PMToolRuntime` 完全不知 store/DefinitionService，`from_config`（`runtime.py:52-82`）不收 store。resolver 须由 `local_app.py:166` lambda 闭包捕获 store 注入。另：§5 让 resolver 复用 `dispatch_service._within_any`（str 入参，`client.core`），但若 runtime 在 `client.tools` 内做过滤，跨包 import 会造成 `tools→core` 反向依赖；且 `injector.py:64` 另有一份 Path 入参的同名副本。

**建议**：把 ①ToolSpec ②handler ③dispatch ④`from_config` 增 resolver 参 ⑤`local_app` lambda 增传 作为**一个原子 PR**。resolver 放独立 `work_mode_context.py`（归 `client.core`），路径包含判断按归属包就近复用（tools 内用 `policy.PathGuard._is_relative_or_same`），勿裸 `import dispatch_service._within_any`；三处 `_within_any` 须确认语义一致避免分叉。

### 2.3 Minor / Note（不阻塞，但实现时须处理）

- **m1（P1 `max_rounds` 双来源）**：`ToolRuntimeConfig.max_rounds`（`models.py:105`）与 `PMToolLoop` 构造参 `max_rounds`（`loop.py:32` 默认 6）独立。**实测**：`pm_agent.plan` 构造 loop 时读 `getattr(runtime.cfg,"max_rounds",6)`（`pm_agent.py:481`），即 ToolRuntimeConfig 的值，经 `from_config` 透传——改 `cfg.pm_tools.max_rounds` **会**生效（P1 文档已据实核正）。
- **m2（auto-compact ↔ 增量 review 窗口）**：`compact()`（`dispatch_service.py:232`）是独立 API，从不在 `_pm_launch` 循环内调；循环用 `reviewed_event_id` 增量跟踪。把 auto-compact 朴素插进循环可能与增量窗口双计/打架——触发时须统一两套机制（compact 后同步 `reviewed_event_id`/`Session.plan`）。
- **m3（护核只在 fallback 分支）**：top-3 constraints/verified_facts 保护只在最终 minimal 重建分支（`context_compression.py:225-241`）生效，主 eviction 循环只是因 `EVICT_MEMORY_FIELDS` 不含它们而没碰。改 lane/eviction 时把护核**提到主路径显式保证并加测试**，别依赖 fallback 分支。
- **m4（subtitle zh/en 两处）**：zh subtitle 在 `app.js:23`（`rulesSubtitle`）、en 在 `app.js:103`；只改 23 会留下英文「auto-injected」过度承诺。新增 description/metadata 输入框还需成对新增 zh/en i18n 键。
- **m5（claude-code 原生 skills 全新写法）**：现有 `_write_skills`（`injector.py:137-156`）写 `.foreman/skills/<slug>.md` 纯 md，无 frontmatter/`foreman-` 前缀/子目录。§7.1 的 `.claude/skills/foreman-<slug>/SKILL.md`+frontmatter 是全新功能，不能直接复用；`.git/info/exclude` 排除逻辑 injector 当前也完全没有。
- **m6（`_build_block` 措辞 + standards 全文 vs 精简）**：`_build_block`（193-205）当前只有中性「请遵守」，无 untrusted/不得 push-merge-deploy 字样；且把 standards body 逐字全文塞进 `CLAUDE.md`，与 §7.1「托管块只放精简 code_standards」矛盾（§8B.7 又要全文活过压缩）。P2 须加 untrusted 框定，并定稿 standards 全文/精简策略。
- **n1（`WORKMODE_BODY_MAX_CHARS=6000` 截断）**：存储上限 `MAX_BODY=200_000`（`definition_service.py:34/35`）远高于 6000；runtime 现有 `_truncate`（629-632）阈值 12000，不能直接套。`est_tokens`/`truncated` 须在 handler 内自实现；`body is None` 显式返回 `error='not_found'`。
- **n2（`scope='workflow'/'workmode'` MemoryItem 全新 writer）**：`MemoryItem.scope` 是无约束 free-form str，唯一 writer（`_store_context_derivatives`，`dispatch_service.py` line **793**）硬编码 `scope='session'`。无 enum/CHECK，typo 静默不匹配。写非 session scope 需新 writer 路径 + scope 串严格常量化（集中定义常量避免 typo）。

---

## 3. 设计书引用更正表（供各阶段引用）

| 设计书引用 | 更正（实际） |
|---|---|
| §13 P0「`app.js:23` 中英文 subtitle」 | zh subtitle 在 `app.js:23`（`rulesSubtitle`），en 在 **`app.js:103`**；必须同改两行。 |
| §5/§11「slug 化防穿越 `injector.py:53-61`」 | slug 函数 `_slug` 实为 `injector.py:56-61`（`_SLUG_RE` 在 53）；`allowed_roots` 不在 53-61：`_within_any` 在 64-77、`__init__` 存储在 101-102、inject 校验在 119-120。 |
| §7.3「`DispatchService` 在 `runner.wait` 后调 `injector.clear`，inject↔clear 成对」 | 现状不存在：`dispatch_service.py` 零 `injector`/`inject`/`clear` 引用；`runner.wait` 在 484(direct)/545(plan)/598(followup)，其后无 clear。成对逻辑只在未实例化的 `WorkflowEngine`。P2 须从零建 inject 点 + 在三个返回出口(约 581/588/595) clear。 |
| §13 P1「`work_mode_ids` 透传 `create()→_pm_launch`」 | 全链缺失：`create()` 签名 103-113、`_pm_launch` 486-495 均无；还需同改 server `_DispatchBody`(180-189)、`/api/tasks`(1375-1402)、composer(`app.js:917-957`)、`runDispatch`(1498-1517)。调用链 `create→_safe_pm_launch[193]→_pm_launch[417]` 真实可作落点。 |
| §6「`PMToolRuntime.from_config` 增传持 store 的 resolver」 | `from_config`(`runtime.py:52-82`) 当前只收 `cfg/workspace/gate/auditor`，不收 store；须新增 keyword-only `resolver=None`，`__init__`(38-50) 加 `self._work_mode_resolver`，由 `local_app.py:166` lambda 闭包捕获 store 注入。`ToolRuntimeConfig` 不应承载 resolver/store。 |
| §6/§8.1「L0 索引写进 `PMToolLoop` 的 system message」 | `PMToolLoop`(`loop.py:40-53`) 只重发调用方 messages；system 实际拼装在 `PMAgent`（生产路径经 `build_plan_prompt:459` + tool ctx:471-477）。L0 注入点在 `pm_agent.py`，不在 `loop.py`。 |
| §7.3「托管块带 task_id 标记，并发不互相覆盖」 | 现状无 task_id：MARKER 固定常量(`injector.py:36-37`)，`_upsert_block`(222-233) 整段替换；`clear()` 无条件 `rmtree(.foreman/skills)` 会误删并发任务文件。隔离须在 P2 接线同批实现。 |
| §8B.1「`ContextPack` 顶层有 `source_refs` 字段」 | `source_refs` 是 per-item key（非顶层字段）；顶层证据容器名为 `retrieved_evidence`（`context_compression.py:137`）——即 §8B.5 L1 正文应记入处，已存在且先于 memory 字段逐出。 |
| §8C.4「`debug.llm_trace` / `FOREMAN_DEBUG_LLM_TRACE` 沿用现有 pydantic settings」 | 仅 `Secrets(BaseSettings)` 是 env 驱动(`config.py:16-19`)；结构化段是 config.yaml 纯 `BaseModel` 无 env 绑定，`Config`(226-243) 无 debug 字段。debug 段 + env→config glue 全新，需先排小任务。 |
| §4.3「`DefinitionService.create/update` 增加 description 必填校验（暗示后端待加）」 | description 既非列也非参数，仅 `metadata_json` 内约定；后端 `_DefinitionCreateBody/Update`(`app.py:208-225`)、`create/update_definition`(1488-1519)、`bad_metadata_json` 错误码已就绪。P0 是纯前端（编辑器输入框 + `saveDefinition` 发 `metadata_json`）+ service 校验；必填 gate 须先回填 examples/存量、且不套用到 import 路径。 |
| §8B.8「三处 12000 字符常量改窗口比例」 | 三常量分属三模块：`DEFAULT_CONTEXT_BUDGET_CHARS`(`context_compression.py:15`)、`MAX_COMPACT_CHARS`(`pm_agent.py:24`)、`MAX_CONTEXT_CHARS`(`dispatch_service.py:46`)；`context_pack_to_text` 只收 int `max_chars`。token→char 须在调用现场算。仓库无静态 per-model ctx_window 表，window 仅来自 provider `/models` 的 `context_length`(常被代理省略)，须自带 fallback 窗口常量。 |
| §8B.6「`scope=workflow` MemoryItem」 | `MemoryItem.scope` 是无校验 free-form str；唯一 writer 硬编码 `scope='session'`（`dispatch_service.py` 实际 line **793**，核单曾写 791/799）。非 session scope 需新 writer 路径，scope 串须常量化。 |
| §8C.1「一个 choke point 覆盖全部」 | choke point 是 `complete()`(`client.py:169`) 与 `tool_complete()`(191) **两个**；contextvar set-point 须覆盖 **8 个**调用方(pm_agent plan/review/compact、operator:212、auditor:305、briefing:236、supervisor:459、loop:154/167)；ws 的 `tool_complete` 内部再调 `complete` 会双记，tracer 须防重入。 |
| §8B.4「L0 确定性序列化保 KV-cache 前缀稳定（现状暗示可达）」 | 现状不满足：`context_pack_to_text` 用 `json.dumps(indent=2)` 非 `sort_keys`，eviction 用 `pop` 原地改 dict(`context_compression.py:196/208/322-323`)，跨轮字节不稳。须改 `_dump` 为 `sort_keys`/无时间戳（对现有压缩输出是行为变更，需回归）。 |

> 注：行号均以基线 `1801128` 为准。`dispatch_service.py` 的 `scope='session'` 一处，评审核单原写 791/799，P1b-context 作者逐行核实后定为 **793**——以阶段文档亲核值为准。

---

## 4. 开发前必须拍板的 Open Questions（去重合并）

### 4.0 已拍板决定（2026-06-24）

> 本小节由项目 owner 于 2026-06-24 拍板，锁定下方 open questions 中的若干关键项。锁定的决定以「【已拍板 2026-06-24】」标注，低风险数值默认以「【默认 2026-06-24】」标注。下文 §4 编号列表与 §5 结论均已据此更新。

**已拍板决定（D1–D4）**

- **D1【设计书 §7.1 vs §8B.7 矛盾的裁决】code_standard 注入策略 = 全文进托管块**【已拍板 2026-06-24】：整段规范写进 workspace 根 CLAUDE.md/AGENTS.md 托管块，每轮重读、能活过 CLI auto-compact（与 §8B.7 一致；接受 CLAUDE.md 可能变大）。skill 仍走「L0 索引 + 文件」渐进，不全文进托管块。
- **D2【§9 / P4】check 硬执行 = 本阶段只做软约束**【已拍板 2026-06-24】：硬执行门（任务结束实跑 check 命令、QA/check 不过则强制 follow_up、workflow 不进下一步）推迟到 V2，P4 当前不实现。软约束（rubric/standard 进 review 影响 done/follow_up）已主要由 P1 的 review 通道承担。
- **D3【§4.3 / §12】存量 definition description = 批量 LLM 回填纳入 P0 一并交付**【已拍板 2026-06-24】（不再只作运维步骤）：P0 新增「对存量 definition 跑 summarize_to_description(body)->≤1024 字 + 人工抽检后写回 metadata_json」任务。这会给 P0 引入 PM LLM 调用与抽检环节，P0 因此变重——文档须如实标注。
- **D4【§13 P1 / M1】composer 手选 work_mode_ids 勾选 UI = 归 P0，UI 先行**【已拍板 2026-06-24】：P0 加勾选控件 + 本地选择状态，并为避免后端 400 在 P0 同步把 work_mode_ids 作为「_DispatchBody/runDispatch 接受但暂不消费」的可选字段；真正消费（resolver 用这些 id 做手选直通/过滤）仍在 P1。

**数值默认（N1–N3，低风险，可后续在 config 覆盖）**

- **N1 debug LLM trace 轮转/保留**【默认 2026-06-24】：单文件上限 50 MB、保留最近 20 个文件或 14 天（先到先汰）。
- **N2 auto-compact 触发**【默认 2026-06-24】：窗口占用 ≥70%（已定）或每 8 个 run（N=8），先压缩再继续。
- **N3 token→char fallback 窗口**【默认 2026-06-24】：当 provider /models 未返回 context_length 时，默认按 32_000 tokens 估（保守偏小，宁可多压不可溢出）；可 config 覆盖。

---

下列问题需由用户/负责人在动手前定夺（合并了评审 blocker 暗含的决策与各 step 作者回报的 open_questions）：

**架构/接线选型**

1. **resolver 注入选型**（P1，M9）：做法 1（lambda 只捕获 store，goal/agent/manual_ids 在 `_pm_launch` 另解析，工具自动漏斗打分缺真实 goal）vs 做法 2（扩 `tool_runtime_factory` 签名为 `(workspace,*,goal,agent,manual_ids)`，需同改 `pm_agent.py:451` 调用处，漏斗用真实任务上下文）。**文档推荐做法 2。**
2. **composer 勾选 work_mode_ids UI 归 P0 还是 P1**（M1）：若不在 P0，手选功能要等 P1 才有产品入口。**文档暂按归 P1**（与后端透传同批）。→【已定：归 P0，UI 先行；后端消费 P1（D4）】【已拍板 2026-06-24】
3. **KV-cache 稳定前缀的 L0 落点**（P1b-context，M4）：塞进 `ContextPack.stable_prefix`，还是在 `pm_agent.py` 组装 messages 时单设稳定前缀区（与 `build_plan_prompt` 产物分离）。取决于 P1 实际把 L0 注入 system 的方式。
4. **`work_mode_get` 的 `kind` 缺省解析**（P1，n1）：name 唯一但跨 kind 可能重名——遍历 `KNOWN_KINDS` 取首个 active 同名，还是要求 PM 必带 kind。

**P0 范围/必填**

5. **P0 metadata UI 输入框字段范围**：仅最小化落 `description`，还是 P0 就提供 `keywords`/`priority`/`est_tokens` 输入框（文档建议 `est_tokens` 留给 P1 与预算器一起）。
6. **存量 definition LLM 批量回填是否纳入 P0 交付**（M2）：还是 P0 只交付种子回填（确定性）+ 回填脚本骨架，存量批量生成作为运维步骤（文档按后者写）。【已定：纳入 P0 一并交付（D3）】【已拍板 2026-06-24】
7. **新增错误码命名**：`missing_description` / `description_too_long` 需与既有 `_DEFN_ERR_STATUS` 风格及前端 `friendlyError` i18n 对齐。

**P1b-trace 细节**

8. **config 改后是否需重启重建 tracer**（M7）：接受初版重启生效，还是要求热重建。
9. **`FOREMAN_DEBUG_LLM_TRACE` env 覆盖范围**：只覆盖开关布尔（文档定），还是也覆盖 `log_dir`/轮转参数。
10. **git 排除方式二选一**：直接在 Foreman 仓库 `.gitignore` 追加 `.foreman/`，还是 LLMTracer 自动写所在 git 仓库的 `.git/info/exclude`（与 P2 针对目标 workspace 的 exclude 注入不同对象，须各做其一不重复）。
11. **debug trace 轮转/保留阈值具体数值**（N 天 / M 文件 / 单文件 MB 上限）：设计书未给数值，需 P1b-trace 设定。【已给默认：单文件 50 MB、保留最近 20 个文件或 14 天先到先汰（N1）】【默认 2026-06-24】

**P1b-context 细节**

12. **auto-compact「每 N 个 run」的 N 默认值**：设计书只给 70% 窗口阈值，N 待定。【已给默认：N=8（窗口占用 ≥70% 或每 8 个 run，先压缩再继续）（N2）】【默认 2026-06-24】
13. **token→char fallback 默认窗口常量取值**（M3）：很多 OpenAI 兼容代理省略 `context_length`，需定 fallback 窗口数值。【已给默认：context_length 缺失时按 32_000 tokens 估，保守偏小、可 config 覆盖（N3）】【默认 2026-06-24】
14. **window 解析缓存粒度**：`list_model_infos` 会发 `/models` 网络请求；在 `_pm_launch` 入口解析一次往下传 vs self 上缓存（`_session_context` 是无参便捷方法）。
15. **`scope=workflow` writer 与 scope 常量归属模块**：`store/models.py` 旁 vs `context_budget.py`；P5 复用方式（步边界何时调用）属 P5 范畴。

**P2 细节**

16. **standards 全文 vs 精简**（m6）：§7.1 要托管块只放精简 code_standards，§8B.7 要全文活过 CLI auto-compact，现实现 `_build_block` 是逐字全文。**【已定：code_standard 全文进托管块、skill 走索引+文件渐进（D1）】【已拍板 2026-06-24】**（已不再是待拍板项）。
17. **三份 `_within_any` 是否本阶段统一抽公共模块**（M9）：文档建议不强行合并、只标注分叉风险，但须确认三者 `resolve(strict=False)`+`is_relative_to` 语义一致。
18. **直发路径（`_direct_launch`）是否参与工作方式注入**：文档建议两路都接（直发可空 material 仅写 `.git/info/exclude` 兜底），若产品上直发不应注入则只接 clear 兜底。
19. **marker 改带 task_id 后是否保留旧固定 marker 作向后兼容默认**：文档建议保留默认值不破 `WorkflowEngine._inject` 等旧调用，P5 接线时复核。
20. **P2 注入/清理 telemetry 并入 P1 `work_mode` 事件还是独立事件**：字段（task_id/agent/skills_written/standards/index_tokens）须与附录 90 schema 对齐。

**P3 细节**

21. **向量存储落点**：方案 A（存 `metadata_json` 明文、无迁移、随 export bundle）vs 方案 B（新增 `Definition.embedding_json` 列、要幂等迁移）。**文档推荐 A。**
22. **embedder 失败回退层级**：本地 `LocalHashEmbedder`（仍有语义近邻）再退词法，还是直接退 `LexicalScorer`。
23. **P1 的 resolver `index()` 是否 async**：影响 P3 `EmbeddingScorer` 需 await query 向量时是否要把 `index()` 与唯一调用点同步改 async。

**P4 细节**

24. **check 命令门禁策略**：走 `gate.classify` + Auditor + 用户审批；P4 硬门推迟 V2，本阶段暂不需决策。
25. **P1 是否把「选中工作方式集合」连同 `metadata_json`/body 透传进 `_pm_launch`**：P4 依赖此取 check 命令与 rubric body；若 P1 只透传 L0 索引（name+description），P4 需先补 resolver 保留 Definition 行。**建议 P1 落地时即把选中集合约定为含 metadata 的完整行。**
26. **多条 qa_rubric 同时选中的拼接/优先级**：全部拼进 review（受 6000 截断）还是按 priority 取 top-N。

**P5 细节**

27. **轻量 step 派发用哪个 agent/model/effort**：从 `session.agent_type` 取，还是新增 start body 的 agent 字段。
28. **QA 触发方式**：step 派发后自动调 `review_step`（需 diff 来源——checkpoint ref 还是 runner 产出）还是 UI 人工确认 diff 后触发；diff 为空时 Reviewer 判定待定。
29. **step 完成判定**：`runner.wait` 返回 None 不带 outcome，谁判定一步「干完了」再调 `submit_step`（UI 手动 vs 接 PM 一次性 review）。
30. **workflow run 的注入隔离键**：P2 用 task_id，workflow run 无 task_id——用 run_id 还是为 run 造合成 task_id，需与 P2 对齐。

**贯穿（必须 P1↔P1b-trace 之间定稿一处）**

31. **`work_mode` telemetry 与 LLM trace 的共享 id 字段集**（M5）：精确的 contextvar id 来源与对账字段命名，须在 P1 与 P1b-trace 间正式定稿（附录 90 给出建议 schema，但对账精确命名留待两阶段对齐）。

---

## 5. 一句话结论

**能照此顺序顺利开发**——架构方向成熟、无需推翻，各阶段已按基线源码亲核并落成有序、可勾选、可回滚的任务。**前提是**：① 动手前先按 §4 拍板上述 open questions——其中 owner 已于 2026-06-24 拍板锁定一批（见 §4.0）：**standards = 全文进托管块（D1）**、**check = 软约束、硬门推迟 V2（D2）**、**存量 description 批量 LLM 回填纳入 P0（D3）**、**composer UI 归 P0/UI 先行、后端消费 P1（D4）**，N1–N3 数值已给默认；**仍需注意 resolver 注入选型（推荐做法 2）与 telemetry↔trace 共享 id 的两阶段定稿**；② 严格遵守两条铁律——**P1 必须原子 PR**、**P2 的 inject↔clear+task_id 隔离必须同批落**；③ 把设计书所有「复用现成」处当作「先 grep 验证再动手」，因为本设计书系统性低估了「现状/可复用」的接线复杂度（多处现成逻辑只活在零实例化的类里）。
