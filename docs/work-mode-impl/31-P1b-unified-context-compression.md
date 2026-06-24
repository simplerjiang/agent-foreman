# P1b — 统一上下文管理与压缩（token 感知 + lane 分层 + 自动压缩）

> 日期：2026-06-24 ｜ 对应设计书章节：§8B（并触及 §12/§14/§15/§16） ｜ 分支：`codex/work-mode-design`
> 本文件是「工作方式生产级接入」的单一开发步骤文档，覆盖设计书 **§8B**。
> 跨阶段共享的常量表 / Schema / telemetry 字段 / 路径映射 / 术语，统一放在
> [`90-conventions-and-glossary.md`](90-conventions-and-glossary.md)，本文不再重抄，只在用到处链接。
> 排期与依赖全景见 [`00-OVERVIEW-AND-SEQUENCING.md`](00-OVERVIEW-AND-SEQUENCING.md)，评审结论见 [`01-REVIEW-FINDINGS.md`](01-REVIEW-FINDINGS.md)。

---

## 0. 目标与产出

把 P1 已经接进来的「工作方式 L0 索引 / L1 拉取正文」与 Foreman **已有**的会话压缩机制（`ContextPack` / `MemoryItem` / `compact()`），统一成**一套有预算、有优先级、token 感知、可自动触发**的上下文管理，而不是各管各的几个写死的 12000 字符。

本阶段交付：

1. **token 感知预算器**：新增一个轻量模块（建议 `core/context_budget.py`），把「PM 模型窗口 → 各 lane 的字符预算」算出来；现有三处写死 12000 的字符常量改为「窗口比例 + 上限」，缺窗口元数据时退回内置默认窗口常量。
2. **lane 分层**（§8B.3）落到代码：渲染/压缩时按车道优先级逐出，**lane 1-4 永不动**，护住 `constraints`/`verified_facts`。
3. **自动压缩触发器**（§8B.8）：在 plan/review 前估算 (lane 5+6+7) token，超阈值（**【默认 2026-06-24】**窗口占用 ≥70%）**或**每 8 个 run（**【默认 2026-06-24】** N=8）就先调一次现有 `DispatchService.compact` 再继续（先压缩再继续）。两数值低风险，可后续在 config 覆盖。
4. **`COMPACT_SYSTEM` 升级为工作方式感知**（§8B.5）：明确告诉压缩器「**不要把 skill/standard/qa 的逐字正文抄进 pack**，只记 `workmode:<kind>:<name>@v<ver>` 引用 + 一行应用说明；要保留因它们产生的决策/约束」。
5. **KV-cache 稳定前缀**（§8B.4）：L0 索引确定性序列化（`sort_keys`、无时间戳、固定字段序），纳入 `ContextPack.stable_prefix`；任务内 append-only，只在任务边界换 L0。
6. **可观测**：per-lane token 占用 + 每次 auto-compact 的前后 token 进 `work_mode` / `context_compact` 事件（§16）。

> **一句话「本阶段定义之完成」**：长会话 / 多步任务跑下去，PM 调用的上下文**不会被工作方式或时间线悄悄撑爆窗口**——超阈值会自动压缩、L0/L1 正文不持久化进 pack、受保护核心活过压缩，且每次都能从 telemetry 读出「每 lane 花了多少 token、压缩前后差多少」。

---

## 1. 前置依赖

| 依赖步骤 | 为什么必须先完成 |
|---|---|
| [`10-P0-copy-and-L0-metadata.md`](10-P0-copy-and-L0-metadata.md)（P0） | 提供 `resolve_work_mode_context()` 与 L0 元数据（`metadata.description`），本阶段的「L0 确定性序列化 / 纳入 stable_prefix」要序列化的就是它的输出。 |
| [`20-P1-L1-retrieval-budget-telemetry.md`](20-P1-L1-retrieval-budget-telemetry.md)（P1） | 提供 §8 全部预算常量（`WORKMODE_*`）、`work_mode_search/get` 工具、`work_mode` telemetry 事件、L0 索引进 system 的注入点（`pm_agent.py`）。**没有 P1 的 per-lane 雏形与 work_mode 事件，本阶段没有可改造的 token 落点。** |
| [`30-P1b-llm-debug-trace.md`](30-P1b-llm-debug-trace.md)（P1b-trace，§8C） | **先做**。后续每一层上下文调优都靠它看真实 payload（每次 LLM 请求的完整 system+messages+tools 与返回）；本阶段验证「pack 不含逐字正文」「auto-compact 真的发生」时，trace 是最直接的证据来源。 |
| [`90-conventions-and-glossary.md`](90-conventions-and-glossary.md) | 常量默认值、telemetry 字段、术语（lane / stable_prefix / source_ref）统一定义处。 |

**进入本阶段时假定的代码状态**（HEAD == 基线 `1801128`，行号已逐处核实）：

- `resolve_work_mode_context()` 已存在并产出 L0 索引（P0）。
- `PMToolRuntime` 已有 `work_mode_search/get` 工具，且 L0 索引已在 `pm_agent.py` 的 plan/review 消息组装处进入 system（P1）。
- `work_mode` 事件类型已注册进 `shared/events.py` 的 `EVENT_TYPES`，每次派发已 emit `{selected, dropped, index_tokens, pulls, body_tokens, kinds}`（P1）。
  - **核实**：HEAD 上 `EVENT_TYPES`（`shared/events.py:16-23`）**尚无** `work_mode`，仅有 `context_compact`（line 23）。`make_event` 会对未注册类型 fail-fast（`events.py:60-61`）。本阶段假定 P1 已补 `work_mode`；本阶段只**扩展其 payload**。
- `LLMClient` 已可选注入 tracer（P1b-trace），contextvar 已能关联 `{session_id, task_id, phase}`。

---

## 2. 涉及文件与现状

> 所有行号基于 HEAD `1801128`，**已逐处 Read 核实**。设计书若有出入，下表「设计书 vs 实际」列就地标注。

| 文件（绝对路径） | file:line | 当前行为 | 本阶段要做什么 |
|---|---|---|---|
| `…/client/core/context_compression.py` | 全文 1-382 | `ContextPack` v1 渲染/压缩的全部逻辑。`DEFAULT_CONTEXT_BUDGET_CHARS=12000`(15)；`MEMORY_FIELDS`(17-28)；`EVICT_MEMORY_FIELDS`(30-39)；`context_pack_to_text(pack,*,max_chars)`(179)；4 级降级级联(183-249)；`_dump`(322-323) 用 `json.dumps(indent=2)`**非 `sort_keys`**、`_top_memory`(358-361) 取 top-3。 | ① `_dump` 改 `sort_keys=True`（确定性序列化）；② 把「护住 constraints/verified_facts top-3」从只在 minimal 兜底分支(225-241)提到主路径显式保证；③ 新增 L0 确定性序列化 helper 给 `stable_prefix`；④ `max_chars` 由调用现场按 token→char 换算后传入（签名不变，仍收 `int`）。 |
| `…/client/core/pm_agent.py` | 23-24, 61-75, 294-320, 596-616 | `MAX_EVENT_CHARS=20000`(23)、`MAX_COMPACT_CHARS=12000`(24)；`COMPACT_SYSTEM`(61-75，**无** skill/standard/qa/`workmode:` 字样)；`events_to_text` 取最后 120 条、跳过 `pm_output/pm_reasoning`(296-297)；`compact()`(596-616) 调 `context_pack_to_text(pack, max_chars=MAX_COMPACT_CHARS)`(616)。 | ① `COMPACT_SYSTEM` 升级为工作方式感知；② `compact()` 的 `max_chars` 改为由 budgeter 算出（按 `pm_model` 窗口）；③ 暴露 lane 5 的 token 估算给 telemetry。 |
| `…/client/core/dispatch_service.py` | 39, 46, 60-73, 232-279, 486-598, 752-756, 766-810 | `MAX_GOAL_CHARS=8000`(39)、`MAX_CONTEXT_CHARS=12000`(46)、`_within_any`(60-73)；`compact(session_id)`(232-279) 是**独立 API 方法**，loop 内**从不调用**；`_pm_launch`(486-598) 在 line 507 读一次 `context=self._session_context(...)`，用 `reviewed_event_id`(542) 增量跟踪，`runner.wait` 在 545/598；`_session_context`(752-756) 读 `Session.plan` 截 `MAX_CONTEXT_CHARS`；`_store_context_derivatives`(766-810) 唯一 `MemoryItem` 写入者，`scope="session"` 硬编码于 **line 793**（设计书/事实核单写作 791/799，实际 793）。 | ① 在 `_pm_launch` 循环里接 auto-compact 触发器（plan 前 + 每 review 后估 token）；② `_session_context` 截断改 token 感知；③ `compact()` emit 的 `context_compact` 事件加 before/after token；④（横切）新增 `scope=workflow` 的 `MemoryItem` writer 路径 + scope 常量化（P5 复用）。 |
| `…/client/store/models.py` | 57-78 | `MemoryItem`：`scope`(63，注释 `session｜workspace｜workflow｜user`，**无 enum/CHECK**)、`importance`(67)、`confidence`(68)、`supersedes`(73)、`superseded_by`(74)。 | 不改表（§12）。仅在新 writer 里写 `scope="workflow"` 等非 `session` 值——**串必须严格常量化**（typo 静默不匹配）。 |
| `…/client/store/db.py` | 159-179 | `add_memory_item`(159-164)；`get_memory_items(session_id,*,scope=None,kind=None)`(166-179) 对 `scope` 做**精确串匹配**(176)、无校验。 | 不改。新 writer 复用 `add_memory_item`；读非 session scope 用 `get_memory_items(scope="workflow")`。 |
| `…/shared/llm/client.py` | 169-189, 191-211, 222-233, 607-649 | `complete()`(169)、`tool_complete()`(191，ws 路径内部再调 `complete` 207)；`list_model_infos()`(222-233) → `_model_infos`(607-649) 抽 `context_length`(624-631) 与 `max_tokens`(632-638)。窗口元数据**仅前端 UI 在用**，PM/dispatch 路径从不消费。 | 不改 client 本体。budgeter 通过 `list_model_infos()` 拿当前 PM 模型的 `context_length`（缺则用内置默认窗口常量）。 |
| `…/shared/config.py` | 87-100, 226-250 | `LLMCfg.model`(90)、`max_tokens`(92，**输出**上限非窗口)；`Config`(226-250) **无 debug 字段**；窗口大小无静态 per-model 表。 | 不改（窗口表/默认窗口常量放 budgeter 模块）。`debug` 段属 P1b-trace，不在本阶段。 |
| `…/shared/events.py` | 16-23, 51-66 | `EVENT_TYPES`(16-23) 含 `context_compact`(23)、**无** `work_mode`；`make_event`(51-66) 对未注册类型 fail-fast。 | 不改（`work_mode` 由 P1 注册）。本阶段扩展两类事件的 payload。 |

> **关键现状提醒**：HEAD 上 `compact()`（`dispatch_service.py:232`）**完全独立于** `_pm_launch` 循环——循环只在 line 507 读一次 context、line 542 用 `reviewed_event_id` 增量跟踪。本阶段要把 auto-compact 接进循环，必须处理「rolling `Session.plan`」与「increment-since-last-review 窗口」两套机制的协同（见 §3 任务 3 与 §6 风险）。

---

## 3. 开发任务（有序、可勾选）

> 子任务尽量小、可独立验收。其中【横切】项 P5 也依赖，建议本阶段一次建好。

### 任务 1 — token 估算器 + 窗口解析（新模块 `core/context_budget.py`）

- [ ] **1.1** 新建 `…/client/core/context_budget.py`，放本阶段所有新常量与纯函数（**勿塞进 `pm_tools` config 或 `ToolRuntimeConfig`**——与现有结构无关，且 `context_compression.py` 是无 LLMClient 访问的纯 helper）。

  ```python
  # core/context_budget.py（骨架，常量默认值见 90-conventions-and-glossary.md）
  CHARS_PER_TOKEN = 4                      # ~4 char/token 近似；后接真实 tokenizer 时只换这里
  DEFAULT_CTX_WINDOW_TOKENS = 32_000       # 【默认 2026-06-24】provider /models 缺 context_length 时的兜底窗口：保守按 32_000 tokens 估（宁可多压不可溢出），可 config 覆盖
  OUTPUT_RESERVE_TOKENS = 4_000            # 给响应留的余量（与 app.js outputReserve 同思路）
  AUTO_COMPACT_THRESHOLD = 0.70            # 【默认 2026-06-24】(lane5+6+7) 窗口占用 ≥此比例则先 compact，可 config 覆盖
  AUTO_COMPACT_EVERY_N_RUNS = 8            # 【默认 2026-06-24】每 N 个 run 也先压缩再继续（N=8），可 config 覆盖
  LANE_BUDGET_RATIO = {                    # §8B.3 各 lane 占窗口比例（lane 5 / 6 / 7 等）
      "session_memory": 0.25,              # lane 5
      "l1_bodies": 0.15,                   # lane 6
      # lane 7（时间线/工具结果）= 余量，最先压缩
  }

  def approx_tokens(text: str) -> int:
      return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN

  def char_budget(window_tokens: int, ratio: float) -> int:
      return max(0, int(window_tokens * ratio) * CHARS_PER_TOKEN)
  ```

- [ ] **1.2** 加一个 `resolve_window_tokens(llm, pm_model) -> int`：调 `llm.list_model_infos()`，匹配 `pm_model` 的 `context_length`，减去 `OUTPUT_RESERVE_TOKENS`；查不到（很多 OpenAI 兼容代理省略 `context_length`）则返回 `DEFAULT_CTX_WINDOW_TOKENS - OUTPUT_RESERVE_TOKENS`（**【默认 2026-06-24】**缺 `context_length` 时保守按 `DEFAULT_CTX_WINDOW_TOKENS = 32_000` tokens 估，宁可多压不可溢出，可 config 覆盖）。**必须有 fallback，仓库无静态 per-model 表。**
  - 接缝：`_model_infos`（`client.py:607-649`）已抽 `context_length`；`list_model_infos`（`client.py:222-233`）已暴露。这是现成通道，只是 PM 路径此前从不消费。
  - **性能**：`list_model_infos` 会发 `/models` 网络请求。**不要在每轮 loop 里调**——在 `_pm_launch` 入口（plan 前）解析一次，把 `window_tokens` 这个 int 往下传（见任务 3）。

### 任务 2 — 三处 12000 常量改 token 感知（4+ 落点协调改动）

> 设计书把它写成「一个 token 感知预算器」，**实际是 4+ 处分属不同模块的协调改动**（评审 major finding）。逐处列清，避免漏。

- [ ] **2.1** `context_compression.py:179` `context_pack_to_text(pack, *, max_chars)` —— **签名不变**（仍收 `int`）。`DEFAULT_CONTEXT_BUDGET_CHARS`(15) 保留作纯默认。token→char 换算在**调用现场**算好再传（因为本 helper 无 LLMClient/Config/model 访问）。
- [ ] **2.2** `pm_agent.py:616` `compact()` 末尾 —— 把 `max_chars=MAX_COMPACT_CHARS`(24, =12000) 改为 `max_chars=char_budget(window_tokens, LANE_BUDGET_RATIO["session_memory"])`。`compact()` 需新增形参 `window_tokens: int`（由 `DispatchService.compact` 传入）。
- [ ] **2.3** `dispatch_service.py:252` `compact()` 内 `summary[:MAX_CONTEXT_CHARS]` —— 同样改为按 lane 5 char budget 截断；并把解析好的 `window_tokens` 透传给 `self.pm_agent.compact(...)`。
- [ ] **2.4** `dispatch_service.py:756` `_session_context` 的 `[:MAX_CONTEXT_CHARS]` —— 改为 lane 5 char budget。`_session_context` 需能拿到 `window_tokens`（最简：`_pm_launch` 把解析结果存到一个临时局部并以参数传入，或在 `_session_context` 内按 `self` 上缓存的 window 算；**不要**在 `_session_context` 里再发 `/models` 请求）。
- [ ] **2.5** `_fallback_compact`（`dispatch_service.py:251` 调用处）—— 确认其内部若有字符截断也走同一 budget。
- [ ] **保留旧常量名**：`MAX_CONTEXT_CHARS`/`MAX_COMPACT_CHARS`/`DEFAULT_CONTEXT_BUDGET_CHARS` 作为「窗口缺失时的 char 上限」继续存在（即「窗口比例 **+ 上限**」里的「上限」），不删，只是不再是唯一来源。

### 任务 3 — 自动压缩触发器接进 `_pm_launch`（§8B.8）

- [ ] **3.1** 在 `_pm_launch`（`dispatch_service.py:486`）**plan 之前**与**每次 review 之后**（line 565 review 调用前后），估算 (lane5+lane6+lane7) 的 token：lane5 = `approx_tokens(context)`（当前 `Session.plan`），lane7 = `approx_tokens(timeline)`（`events_to_text` 产物），lane6 = 本轮 `work_mode_get` 累积正文 token（P1 已记入 `work_mode` 事件 `body_tokens`，可复用）。
- [ ] **3.2** 若 `(lane5+6+7) > window_tokens * AUTO_COMPACT_THRESHOLD`，**或** `run_count % AUTO_COMPACT_EVERY_N_RUNS == 0`，先 `await self.compact(session_id)` 再继续（先压缩再继续）。**【默认 2026-06-24】** `AUTO_COMPACT_THRESHOLD = 0.70`（窗口占用 ≥70%）、`AUTO_COMPACT_EVERY_N_RUNS = 8`（N=8）；两数值低风险，可后续在 config 覆盖。
- [ ] **3.3** **统一两套上下文机制**（评审 minor finding）：`compact()` 会 reload 全部 events(`dispatch_service.py:240`) 并重写 `Session.plan`(256)；而 loop 用 `reviewed_event_id`(542) 增量跟踪。auto-compact 后必须：
  - 重新读 `context = self._session_context(session_id)`（拿到新压缩的 plan）；
  - 把 `reviewed_event_id` 推进到 compact 覆盖到的最后一条 event id（否则下一轮 review 会把已压进 plan 的事件再当「增量」喂一遍，**双计**）。
  - 接缝：compact 内部已知 `event_ids`（`_store_context_derivatives` 的 `event_ids[-1]`，`dispatch_service.py:779`）——让 `compact()` 返回 `source_end_event_id`，`_pm_launch` 用它对齐 `reviewed_event_id`。
- [ ] **3.4** 触发 auto-compact 时 emit 的 `context_compact` 事件 payload 增 `before_tokens`/`after_tokens`（§8B.8 可观测）。现有 payload 已有 `original_chars`/`summary_chars`(`dispatch_service.py:265-266`)，加 token 字段即可。

### 任务 4 — lane 分层 + 护核提到主路径（§8B.3）

- [ ] **4.1** 在 `context_pack_to_text`（`context_compression.py:179-249`）的**主路径**显式护住 `constraints`/`verified_facts` 的 top-3：当前护核只在 minimal 兜底分支(225-241)生效，主 eviction 循环(204-215)只是因 `EVICT_MEMORY_FIELDS`(30-39) 不含它们而「碰巧」没动。把 `_top_memory(data,"constraints",3)`/`(...,"verified_facts",3)`(358-361) 在进入 eviction 前先 pin 住，eviction 结束后保证它们仍在（即使将来有人改 `EVICT_MEMORY_FIELDS`）。
- [ ] **4.2** lane 优先级落地：渲染顺序保持「先压 lane 7（dynamic_tail/retrieved_evidence，已在 192-203 先于 memory 逐出）→ 丢已消费 lane 6 → pack 内按既有顺序逐出 → lane 1-4 永不动」。**lane 1-4（system/护栏/工具 schema、L0 索引、goal、workflow 当前步）不归 `context_pack_to_text` 管**，它们在 `pm_agent.py` 组装 messages 时拼装、不进 pack——确认本阶段改动不会把它们误纳入逐出。
- [ ] **4.3** L1 正文（lane 6）**绝不持久化进 pack**：确认 `_store_context_derivatives`（`dispatch_service.py:766-810`）与 `memory_items_from_pack`（`context_compression.py:252-277`）不会把 `work_mode_get` 的逐字 body 落成 `MemoryItem`——L1 只该留 `retrieved_evidence` 指针（见任务 5）。

### 任务 5 — `COMPACT_SYSTEM` 升级 + L1 正文记成 `workmode:` 引用（§8B.5，本节是融合核心）

- [ ] **5.1** 改 `COMPACT_SYSTEM`（`pm_agent.py:61-75`），追加明确指令（中英都写，与现有英文一致风格）：
  - 「**不要**把 skill/standard/qa 的逐字正文抄进 pack」；
  - 「把拉取的工作方式正文记成 `retrieved_evidence`，`source_ref = workmode:<kind>:<name>@v<ver>` + 一行『为何拉取/如何应用』，**正文可重拉、不存全文**」；
  - 「**要保留**因应用 standard/skill 产生的决策与约束（落 `decisions`/`constraints`），『因为规范 Y 我们选了 X』要活过压缩；规范 Y 逐字原文不必」；
  - 「`qa_rubric` 判定结果落成 `verified_facts`/`tests`，带 `source_ref`」。
- [ ] **5.2** 落点确认：`retrieved_evidence` 是 `ContextPack` 的**顶层容器**（`context_compression.py:137`），已存在且在逐出顺序里**先于** memory 字段(192-203)——这正是 §8B.5 期望的 landing spot。**注意**：设计书把 `source_refs` 写成「ContextPack 顶层字段」，实际它是**每条 item 的 key**（`_as_items`，`context_compression.py:292-296`），顶层容器名是 `retrieved_evidence`。文档与实现以**实际**为准。

### 任务 6 — KV-cache 稳定前缀：L0 确定性序列化（§8B.4）

- [ ] **6.1** `_dump`（`context_compression.py:322-323`）改为 `json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)`。**这是对既有压缩输出的行为变更**（字节会变），需回归现有压缩相关测试（见 §5）。
- [ ] **6.2** L0 索引序列化做成专用 helper（建议放 `context_budget.py` 或 P0 的 work-mode 模块）：`sort_keys=True`、**无精确时间戳**、固定字段序。同一 L0 输入 → 同一字节串。
- [ ] **6.3** 把 L0 索引纳入 `ContextPack.stable_prefix`（`context_compression.py:129-134` 当前只有 `format`/`rule`）——或在 `pm_agent.py` 组装 system 时，把 L0 放进**稳定前缀区**（与 `build_plan_prompt` 产物分离，L0 注入点是 P1 已定的 messages 组装处，**不在** `loop.py`）。两种落点二选一，本阶段定稿并在测试断言「同输入→同字节」。
- [ ] **6.4** append-only 纪律：任务内**不重排/改写**已发出的工作方式内容；要换 L0 等下一个任务边界。文档化「**热更新 vs 缓存命中**是本质权衡」——definition 热更新本就会改前缀字节、打掉缓存，本阶段只保证「任务内稳定、任务边界才换」，不假装两全（评审 major finding）。

### 任务 7【横切】— `scope=workflow` MemoryItem writer + scope 常量化（P1b-context 建好，P5 复用）

- [ ] **7.1** 在某处集中定义 scope 常量（建议 `store/models.py` 旁或 `context_budget.py`）：`MEMORY_SCOPE_SESSION="session"` / `_WORKSPACE="workspace"` / `_WORKFLOW="workflow"` / `_USER="user"`。`MemoryItem.scope`(`models.py:63`) 是无约束 free-form str、`get_memory_items` 精确串匹配(`db.py:176`)、**无 enum/CHECK**——typo 静默永不匹配，故必须常量化。
- [ ] **7.2** 新增 writer 路径（不复用硬编码 `scope="session"` 的 `_store_context_derivatives`，`dispatch_service.py:793`）：在步边界（P5）或任务边界把结论压成 `scope="workflow"` 的 `MemoryItem`。本阶段先把 writer 函数与 scope 常量建好；P5 接线步边界调用。

### 任务 8 — telemetry：per-lane token + auto-compact 前后（§16）

- [ ] **8.1** P1 已 emit `work_mode` 事件 `{selected, dropped, index_tokens, pulls, body_tokens, kinds}`。本阶段在其 payload **追加** `per_lane_tokens`（lane 5/6/7 各自 token）。字段定义统一登记到 [`90-conventions-and-glossary.md`](90-conventions-and-glossary.md) 的 telemetry 表，**勿在本阶段另起一份 schema**（与 P1b-trace 的 ids 共用约定，见评审 finding）。
- [ ] **8.2** `context_compact` 事件加 `before_tokens`/`after_tokens`（任务 3.4）。
- [ ] **8.3** trace 与 telemetry 的 `seq`/ids 共用（P1b-trace 提供 contextvar）——验收时要能「按 ids 从 telemetry 钻到原始 payload」。

---

## 4. 验收标准（摘自 §14/§15，仅本阶段相关，改写为可勾选）

- [ ] **压缩后 pack 不含逐字正文**：跑一个 PM 任务，PM `work_mode_get` 拉取了某 skill/standard 正文 → 触发 compact → 渲染出的 `ContextPack` **不含** skill/standard 的逐字 body，只含 `workmode:<kind>:<name>@v<ver>` 的 `source_ref` + 一行应用说明。（§14 / §8B.5）
- [ ] **因规范产生的决策/约束存活**：上一条同一场景里，「因为规范 Y 我们选了 X」这类决策/约束**活过压缩**（落在 `decisions`/`constraints`，未被逐出）。（§14）
- [ ] **自动压缩在超阈值时触发**：构造 (lane5+6+7) 超 `AUTO_COMPACT_THRESHOLD * window` 的会话 → `_pm_launch` 在下一次 plan/review 前自动调 `compact`；触发后 lane 1-4 不被动，`constraints`/`verified_facts` top-3 受保护。（§14 / §8B.3/§8B.8）
- [ ] **L0 确定性序列化**：同一 L0 输入两次序列化 → **同字节**（`sort_keys`、无时间戳），保证 KV-cache 前缀稳定。（§14 / §8B.4）
- [ ] **多步上下文不线性膨胀**（与 P5 联调，本阶段先验单会话长跑）：长会话跑 N 轮后，喂给 PM 的会话上下文 token **不随轮数线性涨**（auto-compact 生效）。（§14 / §8B.6）
- [ ] **token 感知预算可证**：`work_mode` 事件能算出每任务 work-mode token 与**每 lane token**；`context_compact` 事件含 auto-compact **前后 token**，证明窗口没被工作方式悄悄吃满。（§16 / §15）
- [ ] **窗口元数据缺失不崩**：provider `/models` 不返 `context_length` 时，budgeter 用 `DEFAULT_CTX_WINDOW_TOKENS` 兜底（**【默认 2026-06-24】** 保守按 32_000 tokens，可 config 覆盖），流程正常。

---

## 5. 测试（集成必须打 tool-loop 真实路径，不允许只测 `build_plan_prompt`）

### 单元测试

- [ ] `context_budget.approx_tokens` / `char_budget` / `resolve_window_tokens`：含 `/models` 返回 `context_length`、返回省略两种 fixture，断言后者走 `DEFAULT_CTX_WINDOW_TOKENS`。
- [ ] `context_pack_to_text` 护核：构造一个超预算 pack，断言 eviction 后 **top-3 `constraints` 与 top-3 `verified_facts` 仍在**（主路径保证，不依赖 minimal 兜底分支）。
- [ ] `_dump` 确定性：同一 dict 两次 `_dump` 字节相同；key 顺序与构造顺序无关（`sort_keys` 生效）。回归既有压缩快照测试（行为变更）。
- [ ] L0 序列化 helper：同输入→同字节；改一个无关字段顺序不改输出。
- [ ] `COMPACT_SYSTEM` 升级后：用一段含「拉取了 standard 正文 + 据此做了决策」的 timeline 喂给真实/mock LLM，断言产出的 pack 把正文记成 `workmode:` 引用、决策落 `decisions`。

### 集成测试（线上路径）

- [ ] **auto-compact 真发生**：用带 `tool_runtime_factory` 的 `PMAgent`（线上 tool-loop 路径，**非** `build_plan_prompt`）跑一个 (lane5+6+7) 故意超阈值的 `_pm_launch`，断言：循环内调用了 `compact()`、`Session.plan` 被重写、`reviewed_event_id` 被推进对齐（无双计）、emit 了带 `before/after_tokens` 的 `context_compact` 事件。
  - 用 P1b-trace 的 tracer 抓真实 LLM 入参，断言压缩后下一轮喂进窗口的内容**不含逐字 L1 正文**。
- [ ] **stable_prefix 稳定**：两次同 L0 输入的 plan，断言发给 `LLMClient` 的 system 稳定前缀区**字节一致**（KV-cache 友好）。
- [ ] **窗口 fallback**：mock `list_model_infos` 不返 `context_length`，断言 `_pm_launch` 用默认窗口正常完成、未抛错。

---

## 6. 风险与回滚

| 风险 | 说明（呼应评审 findings） | 缓解 / 回滚 |
|---|---|---|
| **`_dump` 加 `sort_keys` 是行为变更** | 既有压缩输出字节会变，可能打挂依赖快照的测试与 KV-cache 命中假设。 | 先跑全量压缩相关回归；回滚仅需还原 `context_compression.py:322-323` 一行。 |
| **auto-compact 与增量 review 窗口打架** | `compact()` reload 全部 events 重写 `Session.plan`，loop 用 `reviewed_event_id` 增量；naive 插入会双计或丢增量（评审 minor）。 | 任务 3.3 强制：compact 后重读 context + 用 `source_end_event_id` 对齐 `reviewed_event_id`。回滚：移除触发器调用，`compact` 退回纯 API 方法（HEAD 行为）。 |
| **窗口元数据缺失** | 多数 OpenAI 兼容代理省略 `context_length`，PM 路径此前从不消费（评审 major）。 | budgeter 自带 `DEFAULT_CTX_WINDOW_TOKENS` fallback；缺失时退化为「窗口比例×默认窗口」，等价于略宽的字符预算，不溢出。 |
| **热更新打掉缓存** | definition 热更新改 L0 前缀字节、持续打掉 KV-cache（§8B.4 vs §8B.5 张力，评审 major）。 | 不追求两全：保证「任务内 append-only、任务边界才换 L0」，并在 §0/任务 6.4 文档化此权衡。 |
| **token→char 换算分散在 4+ 处** | 三常量分属三模块、`context_pack_to_text` 只收 `int`（评审 major）。 | 集中在 budgeter 算、以 `int` 往下穿；保留旧常量作「上限」兜底。逐处回滚互不影响（各处独立改动）。 |
| **scope 串 typo 静默失败** | `scope` 无 enum/CHECK，`get_memory_items` 精确匹配（评审 note）。 | 任务 7 强制常量化；新增 writer 与读侧共用同一常量。 |
| **per-lane / auto-compact 字段与 P1b-trace ids 对不上** | 两步各自定义易脱节（评审 major）。 | 字段统一登记到附录 telemetry 表、ids 与 trace 共用 contextvar；验收断言「按 ids 可对账」。 |

**整体回滚**：本阶段所有改动可按子任务粒度独立 revert。最保守回滚 = 还原 `_dump`、移除 `_pm_launch` 内 auto-compact 调用、`compact()`/`context_pack_to_text` 的 `max_chars` 还原为旧常量——即回到 HEAD「手动 compact + 写死 12000」的行为，不影响 P0/P1 已交付能力。

---

## 7. 与设计书 / 其它阶段的对应

**覆盖设计书章节**：§8B（§8B.1 已有机制承认 / §8B.2 原则 / §8B.3 lane 分层与预算 / §8B.4 KV-cache 稳定前缀 / §8B.5 工作方式内容进出压缩 / §8B.6 workflow 与压缩 / §8B.7 两级压缩协同 / §8B.8 自动触发 + token 感知）；并触及 §12（不改表）、§14/§15（验收）、§16（telemetry）。

**上游依赖**：

- [`10-P0-copy-and-L0-metadata.md`](10-P0-copy-and-L0-metadata.md) — `resolve_work_mode_context()` 的 L0 输出是本阶段要确定性序列化的对象。
- [`20-P1-L1-retrieval-budget-telemetry.md`](20-P1-L1-retrieval-budget-telemetry.md) — `WORKMODE_*` 预算常量、`work_mode` 事件、L0 进 system 注入点、`body_tokens`（lane 6 估算来源）。
- [`30-P1b-llm-debug-trace.md`](30-P1b-llm-debug-trace.md)（§8C，**先做**）— 调优本阶段上下文的真实 payload 来源；ids/seq 与本阶段 telemetry 共用。

**下游依赖本阶段的步骤**：

- [`70-P5-workflow-control-flow.md`](70-P5-workflow-control-flow.md)（§10）— 复用本阶段建好的 `scope=workflow` `MemoryItem` writer + scope 常量（§8B.6 步边界压缩）；步边界 inject/clear 由 [`40-P2-coding-agent-channel.md`](40-P2-coding-agent-channel.md) 提供。
- [`60-P4-hard-enforcement.md`](60-P4-hard-enforcement.md)（§9）— `qa_rubric` 判定结果落成 `verified_facts`/`tests` 的约定（任务 5.1）与 P4 的 rubric 验收门衔接。

**共享资产登记处**：本阶段新增的常量（`CHARS_PER_TOKEN`/`DEFAULT_CTX_WINDOW_TOKENS`（**【默认 2026-06-24】**=32_000）/`AUTO_COMPACT_THRESHOLD`（**【默认 2026-06-24】**=0.70）/`AUTO_COMPACT_EVERY_N_RUNS`（**【默认 2026-06-24】**=8）/`LANE_BUDGET_RATIO`/scope 常量）、`work_mode` 事件新增字段（`per_lane_tokens`）、`context_compact` 新增字段（`before/after_tokens`）一律登记到 [`90-conventions-and-glossary.md`](90-conventions-and-glossary.md)，避免各阶段各写一份。
