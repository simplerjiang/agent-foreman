# PM 规划阶段「终结判定」重构设计书

> 状态：**草案 v3 / codex 评审：有条件通过**（评审记录见 §11）；**传输路线已实测锁定：保留 ws + A1 强制工具**（附录 B）
> 日期：2026-06-25
> 作者：claude-code（开发 agent）
> 协作：codex CLI 已参与讨论与验收（结论 + 阻塞项见 §11）
> 关联记忆：`codex-stalls-watchdog`、`foreman-e2e-2026-06-24`（会话假 running 挂死）

---

## 0. 一句话

PM 的「出计划」这一步用**无约束自由文本流 + 事后正则解析**来驱动控制流，没有任何「怎样算生成完成」的结构化判定，导致模型陷入复读循环时无人兜底——本设计把「终结」从"事后猜文本"改为**解码器/协议层面强制的事实**，并补上消费端早切、停滞看门狗与可见化 UX。

---

## 0.5 基调（贯穿全程的设计纪律）

> 下面 8 条是本次所有改动的北极星。任何 PR / 任务在自检和验收时都先对照这 8 条；与之冲突的实现一律重做。

1. **终结靠协议，不靠猜文本。** "计划完成" = 收到**一次 schema 合法的工具调用**（A1 的 `function_call` done），**不是**事后正则解析、**不是**数 token、**不是**等模型自己打停止符。
2. **永不用无约束自由文本驱动控制流。** PM 的每个控制流决策（出计划 / 复核 / 压缩）都走**结构化工具调用**；自由文本只用于给人看的叙述。
3. **纵深防御，不赌单点。** 即便上了 A1，**早切 + 看门狗 + 解析容错三层仍全部保留**；任一层失效，下一层必须兜住。
4. **失败要响、要可见。** LLM 异常 / 停滞 → 落到**明确终态**（`failed` / `stalled`）+ reason 上 UI；**禁止假 `running`**、禁止静默吞错。
5. **一处修复，四处覆盖。** `plan` / `PMToolLoop` / `review` / `compact` 四个同款风险点**一起治**，不留同构坑。
6. **不破坏既有能力。** 保留 PM 取证轮（`read_file` / `search_repo`）；强制 `submit_plan` **只发生在提交轮**，绝不掐死取证。
7. **传输不动地基。** 保留 `transport=ws` + A1（已实测）；**不改 CLIProxyAPI、不改上游 Provider**。
8. **改完必须真机可验。** 按 `AGENTS.md`：每个落地 PR 开 `needs-e2e`；被 E2E 打回过的项属二次修复，**必须当场 `foreman-e2e` 复验通过再关**，不许裸关甩异步。

---

## 1. 故障实录（Incident）

**会话**：`3d67d3124eee4f84bc13ab9284dbf176`（`status=running`，从未结束）
**任务**：「这是你这个软件的代码，你自己去审阅一下代码，看看目前有什么阻碍点。」
**数据来源**：`dist/foreman.db`

| 指标 | 数值 |
|---|---|
| 会话事件总数 | 25,969 |
| `pm_output`（pm-agent） | 25,888（全部是同一个流 `plan:840d359d…` 的 `response.output_text.delta`） |
| `pm_reasoning` | 80 |
| `dispatch`（desktop） | 1（= 用户这条入站任务本身，**无任何 subagent 被派发出去**） |
| 时间跨度 | 08:46:13 → 08:59:51（约 **13.5 分钟**，末事件时仍在吐） |
| 流 `seq` 峰值 | 27,689 |
| 重建后文本 | ~46,000 字符 |
| **完整计划 JSON 被重复生成** | **47 遍**（`"ready":true}` 标记 46 次，`{"summary` 起始 47 次） |
| 阶段（phase） | 始终是 `plan`，**从未进入派发/执行** |

模型第 1 遍（08:46:20）就产出了一份**完全合格、`ready:true`** 的计划；其后 46 遍是逐字复读，毫无新增信息。reasoning 摘要里模型反复在说「**Planning JSON output… I'm…**」。

### 1.1 用户侧观感（界面非常不友好）

> 「PM agent 只回了一段话，后面没有任何新的思维链显示，也没有 todo 显示，也没看到 subagent 启动，但是上下文一直在增长。」「也没有 PM agent 调用工具的反馈。」

---

## 2. 根因分析

### 2.1 代码事实（已核对）

- **`PLAN_SYSTEM`**（`src/foreman/client/core/pm_agent.py:26-46`）：以**自然语言**要求 `Respond with ONLY JSON: {"summary": str, …, "ready": bool}`。计划结构只活在 prompt 里，**没有任何 API 层强制**。
- **传输层**（`src/foreman/shared/llm/client.py:180`）：注释明写 `the Responses path has no response_format`——PM 走的这条 ws/Responses 通道**完全没有结构化约束**，纯自由文本。
- **终止信号**（`client.py:410-477`）：流式累加 `response.output_text.delta`，**只认服务端的 `response.completed`** 才停。模型复读时永不发 `completed` → 这边一直收、一直堆、永不返回。
- **事后解析**（`_extract_json_object`, `pm_agent.py:159-180`）：先 `json.loads(整段)`，失败后退化为「首个 `{` → **最后一个** `}`」截取再 parse。**47 个对象首尾相连时，这段截取会把全部 47 个一起喂给 `json.loads`，必然失败 → 返回 `None`**。
- **降级**（`parse_plan`, `pm_agent.py:192`）：`obj = _extract_json_object(raw) or {}` → 空字典 → 落回 `fallback_agent/effort/instruction`。
- **同款脆弱解析另有一处（codex 补充）**：`PMToolLoop`（`src/foreman/client/tools/loop.py:55-62`、`267-288`）先等完整 LLM 返回再用同样的"首 `{` → 末 `}`"解析 final_plan / tool_calls。**只修 `pm_agent.py` 不够，必须一并覆盖 `tools/loop.py`。**
- **校正（codex 补充）**：PM **并非"根本不调工具"**——`local_app.py:163-168` 注入 `PMToolRuntime`、`pm_agent.py:513-550` 会走 `PMToolLoop`、`dispatch_service.py:560-564` 已接 `on_tool_event`。准确表述是：**本次故障样本未产生任何 PM 工具事件，卡在 plan 文本生成、未走到提交/执行**；代码层具备 PM 工具循环，但同样缺"终结兜底"。
- **超时是假超时（codex 补充）**：`client.py:461` 的 timeout 只作用于单次 `recv()`；只要持续吐 delta 就一直"续命"，**不是整轮墙钟超时**——这正是 13 分钟不死的机制。

### 2.2 两层根因

1. **模型层**：用无约束自由文本生成 + prompt 里口头约定 JSON，**没有让解码器在"一个对象闭合"时强制停止**。模型吐完一份合法 JSON 后不打停止符，直接复读——这是一个**早已知的 LLM 失败模式（repetition loop）**，而这里没有任何东西能拦它。
2. **消费端层（Foreman 自己的 bug）**：① 唯一的终止判据是服务端 `response.completed`，**没有"第一个完整顶层对象即收口"的早切**；② **没有停滞/复读看门狗**；③ 解析器用"首 `{`→末 `}`"，**对多对象串联零容错**，即便流停下产出也是废的。

### 2.3 症状 → 根因映射

| 用户观察 | 根因 |
|---|---|
| PM 只回一段话 | 那段是 plan 开头的 status「PM 正在规划…」。其后流出的是**内部计划 JSON**，UI 不当对话渲染 |
| 没有新思维链 | reasoning 增量(80)困在 plan 阶段未外露；模型"思考"本身在原地打转 |
| 没有 todo | todo 要等计划**定稿提交**、phase 切换才渲染；计划流永不收口 → 永不定稿 |
| 没看到 subagent | 派发发生在计划收口之后；计划没收口 → 0 子代理 |
| **没有工具调用反馈** | 本次故障样本中 PM **未产生任何工具事件**（代码层有 `PMToolLoop` 但未走到提交）；它卡在 plan 文本生成，到不了"会用工具"的执行阶段 |
| 上下文一直增长 | 2.8 万条 delta / 4.6 万字符重复 JSON 无上限地灌入事件流与上下文 |

---

## 3. 设计原则：别的 harness 怎么做

**生产级 harness（Claude Code 自身、Codex CLI、Cursor）几乎从不用"自由文本流 + 事后正则解析"驱动控制流。** 它们把"agent 是否做出了决策"做成**有类型、会终结、可机器校验**的事件。"怎样算终结"有两条工业标准答案：

- **(a) 强制单次工具调用（Anthropic / Claude Code 本身）**：把"出计划"定义为一个工具 `submit_plan(...)`，用 `tool_choice` 强制只调它。模型吐**恰好一个** `tool_use` 块（参数受 `input_schema` 约束），API 返回 `stop_reason: "tool_use"`。**终结 = 收到一个完整 tool_use 块**；复读在协议上不可能。
- **(b) 严格结构化输出（OpenAI Structured Outputs）**：`response_format: {type:"json_schema", strict:true, schema:…}`。解码器被 schema 编译成的语法约束，根对象 `}` 一闭合，**下一个唯一合法 token 就是 EOS**。**终结 = schema 根对象闭合**；模型连第 2 个 `{"summary` 都生不出来。

> 关键结论：**"终结"不该靠数 token、也不该靠事后猜文本，而应是解码器/协议强制的不变量 + API 的 stop 信号。** `max_output_tokens` 是钝器（会把合法但长的计划从中间截断成残片），只配当落在合法边界上的最后保险。

---

## 4. 解决方案（分层，纵深防御）

### ✅ 传输路线已定（经线上实测）：保留 ws + 走 A1 强制工具，无需改代理/Provider

**线上配置**（`dist/foreman.db`/configkv）：`provider=openai`、`model=gpt-5.5`、`base_url=https://api.kongsites.com/v1`、`transport=ws`。ws 端是用户自建的 **CLIProxyAPI**（`github.com/router-for-me/CLIProxyAPI`，源码在 `E:\MyAIAPI\CLIProxyAPI`），桥接到 Codex 订阅后端。

**代理侧静态核对**：`/responses` 的 ws 处理是**近透传**——`normalizeResponseCreateRequest`（`sdk/api/handlers/openai/openai_responses_websocket.go:316-334`）只删 `type`、设 `stream/input/model`，**不剥离 `tools/tool_choice/text.format`**；转发循环原样回放上游所有事件，并有成套 `function_call` 工具调用修复逻辑。说明工具调用本就在这条 ws 上往返。

**线上实测（探针，见附录 B）结论：**
- **A1 强制工具调用 → ws 上完全可用**：发 `tools` + `tool_choice:{type:function,name:…}`，上游真回 `output_item.added(function_call)` + 流式 `function_call_arguments.delta` + `done` + `completed`。**无需改 CLIProxyAPI、无需改上游 Provider。**
- **A2 strict json_schema → ws 上不被 enforce**：`text.format` 被忽略，回普通 message 文本。**A2 在此后端不可用，排除。**

→ **路线锁定：保留 `transport=ws`（用户偏好、UX 更好）+ 采用 A1 强制工具。所有改动落在 Foreman 客户端**（`client.py` 发 `tools/tool_choice` + 解析 function-call 事件；去掉 ws `tool_complete` 的纯文本短路）。**原"扩展 ws proxy / 新增 HTTP 路径"的前置工程取消。** 这同时解掉 codex 阻塞项①（它从 Foreman 客户端短路代码误判为"后端不支持"，实测证明支持）。

### 方案 A（根治）：plan 改为「终结性工具调用」（A1，ws 已实测可用）

**采用 A1，排除 A2**（A2 的 strict json_schema 在线上 ws 后端不被 enforce，见附录 B）。

- **A1 — 终结性工具（terminal `submit_plan`）**：定义 `submit_plan` 工具（schema 见 §5）。
  **关键：不是从第一轮就强制只能 `submit_plan`**——否则会掐死现有 `read_file/search_repo` 取证轮、削弱 PM 能力（`tests/test_dispatch_service.py:639-647` 覆盖了 tool_pre/tool_post 先于 pm_plan）。正确做法：**取证轮 `tool_choice:auto`，到"提交计划"那一轮把 `submit_plan` 作为 terminal tool 强制调用**（ws 帧带 `tool_choice:{type:"function",name:"submit_plan"}`）。
  **终结信号（ws/Responses）**：收到该工具的 `response.output_item.done`（function_call）→ 读其拼好的 `arguments` 作为计划，`response.completed` 收口；**不再事后正则解析**。强制单工具 → 模型只会发一次调用，**47× 复读在协议上不可能**。

> A2（strict json_schema）保留为备选记录：仅当未来切到支持 `text.format`/`response_format` 的后端时才考虑；当前 ws 后端实测不支持，不纳入实现。

### 方案 B（兜底其一，列为 P0）：消费端增量解析早切

即便上了 A，仍保留：对 delta 流跑**增量 JSON / 括号深度计数**，**第一个平衡顶层对象出现后、且通过 schema/必填字段校验**，再**主动 abort 流**。
**codex 警示**：不能只靠括号深度——必须正确处理**字符串内的转义、`{}`、代码围栏**，否则噪声/字符串里的 `{}` 会误切；务必"先校验合法再 abort"。需**同时覆盖 `tools/loop.py` 的 final_plan / tool_calls 协议**。本次故障若有此项，08:46:20 即结束而非 08:59:51。

### 方案 C（兜底其二，列为 P0）：停滞 / 复读看门狗

独立监流，命中任一即 abort 并把会话置**明确终态**且在 UI 暴露原因：
- **复读**：优先**结构化检测**——"已产出一个完整合法顶层对象后又出现新顶层对象 / 同对象指纹重复"；n-gram 仅作辅助；
- **无进展**：deltas 在流但 phase 不推进超过 `T_stall` 秒；
- **墙钟**：单次 PM LLM 调用超过 `T_wall` 秒硬上限（**整轮**墙钟，非单次 `recv()`）。

**codex 警示**：**不能原样复用现有 `Supervisor`**——它（`supervisor.py:19-24`）是 CLI agent 看门狗，注释明示只做检测/发事件、**不执行 abort**。PM ws plan 需要**LLM 调用级**的墙钟 + 重复检测 + **可取消任务 + 关闭 ws**。另：当前终态只有 `failed/cancelled`（`dispatch_service.py:48-49`），**需新增/明确 `stalled`** 终态并贯通状态机与 UI。

### 方案 D（UX，非根治）：让 plan 阶段"有生命"

现状（codex 核对）：UI 把 `pm_output` 流式文本当 PM 消息渲染（`app.js:636-665`），**todo 只有收到 `pm_plan` 后才出现**（`app.js:622-627`）；"取消会话"只标 `cancelled`、**不强杀外部进程也不取消正在跑的 PM ws 调用**（`dispatch_service.py:281-300`）。

1. **计划拆成增量工具调用**：`add_todo(text) × N` → `commit_plan()`，每次调用都是可渲染持久事件 → **todo 在 UI 一条条冒出**；"完成" = 收到 `commit_plan`。（接近 Claude Code TodoWrite / plan-mode。）
2. **plan 阶段可见 + 真能停**：把 `reasoning_summary` 增量做成实时「PM 正在规划…（已 N 秒）」+ 流式文本 + **Stop 按钮**；Stop 必须**真正取消正在跑的 PM ws 调用**（接 §C 的可取消任务），而非仅标 `cancelled`。
3. **plan 独立收紧调用**：plan 用紧 schema（数组/字符串长度设界）、effort 适中，与执行分开；现状 `effort:high` + 无约束 = 给模型打转的空间。

---

## 5. `submit_plan` 工具 / Schema 设计（草案）

```jsonc
{
  "name": "submit_plan",
  "description": "Emit exactly one launch plan for the selected coding agent. Call once.",
  "input_schema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["summary", "agent", "effort", "instruction", "todo", "deliberation", "ready"],
    "properties": {
      "summary":      { "type": "string", "maxLength": 600 },
      "agent":        { "type": "string", "enum": ["claude-code", "codex"] },
      "model":        { "type": "string", "maxLength": 80 },
      "effort":       { "type": "string", "enum": ["low", "medium", "high", ""] },
      "instruction":  { "type": "string", "maxLength": 6000 },
      "todo":         { "type": "array", "maxItems": 12, "items": { "type": "string", "maxLength": 200 } },
      "deliberation": { "type": "array", "maxItems": 8,  "items": { "type": "string", "maxLength": 300 } },
      "ready":        { "type": "boolean" }
    }
  }
}
```

字段沿用现有 `PMPlan`（`pm_agent.py:78-88`），**保证 `parse_plan` 下游零改动**——只是把"喂给 parse_plan 的 dict 从哪来"由"正则截文本"换成"工具参数/strict JSON"。`maxItems/maxLength` 即结构性边界，替代 token 上限。

---

## 6. 落地分期与影响文件

| 阶段 | 内容 | 主要文件 | 风险 |
|---|---|---|---|
| **P0 止血** | 方案 B（早切，含转义/围栏 + 校验后再切）+ 方案 C（LLM 调用级墙钟 + 结构化复读检测 + 可取消 + 明确 `stalled` 终态 + UI 可见）；并修脆弱解析容错（取**第一个**合法对象而非首{→末}）。**范围必须含 `tools/loop.py`、`REVIEW_SYSTEM`、`COMPACT_SYSTEM` 三处同款风险**（codex 阻塞项③） | `client.py`、`pm_agent.py`、`tools/loop.py`、`dispatch_service.py`、web | 低-中（加兜底为主；状态机/取消语义需小心） |
| ~~P-prereq 后端能力~~ | **已取消**：线上实测 ws 后端原生支持强制工具调用（附录 B），无需改 CLIProxyAPI / Provider | — | — |
| **P1 根治** | 方案 A1（ws）：① `client.py` ws `response.create` 帧带 `tools/tool_choice`、解析 `output_item.added/function_call_arguments.delta/...done` 装配 tool_call、去掉 `tool_complete` ws 纯文本短路（`client.py:207-211`）；② `submit_plan` 取证轮 auto + 提交轮强制、以 function_call done 为终结、删事后正则；回归 plan/review/compact 全链路（codex 阻塞项②） | `client.py`、`pm_agent.py`、`tools/loop.py`、`dispatch_service.py` | 中（需回归 PM 取证→final_plan→dispatch） |
| **P2 UX** | 方案 D：增量 todo 可见 + 实时规划状态 + Stop 真取消 ws 调用 | `dispatch_service.py`、`app.js`、事件类型 | 中 |
| **P3 收尾** | 删除死路径（旧正则解析）、补文档、补单测 | `pm_agent.py`、`tools/loop.py`、docs | 低 |

> 说明：`REVIEW_SYSTEM`(`pm_agent.py:48`) 与 `COMPACT_SYSTEM`(`pm_agent.py:61`) 同为"prompt 口头约定 JSON + 事后解析"，**同类复读/挂死风险**（`pm_agent.py:657-680` 走同一 ws/json_mode 风险面）。codex 要求：**P0 的超时+解析兜底就必须覆盖它们**，根治可随 P1 推进、不应拖到最后。

---

## 6.5 任务拆分（WBS：可认领 / 带依赖 / 带验收）

> 约定（同 `docs/TASKS.md`）：每个 `T*` 是**能独立跑、能验收的纵向小切片**；`[ ]`→`[x]` 表示完成。**认领走 GitHub Issue**（`AGENTS.md` §二「先领取再动手」），「建议认领」仅作分工参考，不预占。`P0` 与传输路线无关、可立即动手；`P1` 已解锁（ws+A1 实测可用）。

### 顺序与依赖
```
P0（止血，独立）─┬─ T0.1 早切器 ─┐
                 ├─ T0.2 解析容错 ┤
                 ├─ T0.3 看门狗 ──┼─▶ T0.4 状态机 ─▶ T0.5 UI 可见
                 └─ T0.6 覆盖 review/compact
P1（根治 A1/ws）  T1.1 帧带tools ─▶ T1.2 解析function_call ─▶ T1.3 去ws短路 ─▶ T1.4 submit_plan工具 ─▶ T1.5 parse吃arguments ─▶ T1.6 回归
P2（UX，依赖 P1+T0.3）  T2.1 todo渐显 ∥ T2.2 实时状态+Stop ─▶ T2.3 Stop真取消
P3（收尾，依赖 P1/P2）  T3.1 删死路径 ─▶ T3.2 单测/文档 ─▶ T3.3 二次修复纪律
```

### P0 — 止血（与传输路线无关）

- [ ] **T0.1** 增量 JSON **早切器**（新 shared util）：括号深度 + **字符串转义/代码围栏**处理，"第一个完整顶层对象 **且** 通过字段校验即收口并 abort 流"。｜依赖：无｜建议认领：claude-code｜**验收**：单测覆盖多对象串联 / 转义内 `{}` / ```json 围栏 / 前后噪声，均返回第一个合法对象、不误切。
- [ ] **T0.2** 修脆弱解析：`_extract_json_object`（`pm_agent.py:159`）改"取**第一个**平衡对象"，并复用到 `tools/loop.py:267-288`。｜依赖：T0.1｜建议认领：claude-code｜**验收**：47 份串联 JSON 输入 → 取到第 1 份合法对象、不再降级 fallback。
- [ ] **T0.3** PM **LLM 调用级看门狗**（`client.py` ws/HTTP 外层）：(a) 整轮墙钟 `T_wall`(60–90s)；(b) 结构化复读检测（完整对象后再现新对象/指纹重复）；(c) 无进展 `T_stall`(15–30s)。命中 → abort + **关 ws** + 抛可识别异常。｜依赖：无（与 T0.1 并行）｜建议认领：codex｜**验收**：mock"持续吐 delta、永不 completed"的流 → `T_wall` 内 abort。
- [ ] **T0.4** **会话状态机**：新增/明确 `stalled` 终态（`dispatch_service.py:48-49`），把看门狗 abort 映射到 `failed`/`stalled` 并落 reason 事件。｜依赖：T0.3｜建议认领：codex｜**验收**：触发看门狗 → 会话不再 `running`，DB 有终态 + reason。
- [ ] **T0.5** **UI 可见**（`app.js`）：渲染 `stalled`/`failed` + reason，running 不再永久。｜依赖：T0.4｜建议认领：claude-code｜**验收**：E2E 真机看到失败原因，非空转。
- [ ] **T0.6** **覆盖 review/compact**：T0.1–T0.3 的早切+墙钟+容错应用到 `REVIEW_SYSTEM`/`COMPACT_SYSTEM` 路径（`pm_agent.py:657-680`）。｜依赖：T0.1–T0.3｜建议认领：claude-code｜**验收**：review/compact 同样能在第一个对象收口、能墙钟失败。
- [ ] **P0 验收（合）**：① 复读流在第一个合法对象处收口并继续 dispatch；② 无 completed 流在 `T_wall` 内失败 + UI 可见 reason；③ 四路（plan/loop/review/compact）均覆盖。

### P1 — 根治：A1 强制工具（ws）

- [ ] **T1.1** `client.py` ws `response.create` 帧支持透传 `tools` + `tool_choice`（`_responses_ws_once`，`client.py:427`）。｜依赖：无｜建议认领：codex｜**验收**：抓帧确认 `tools/tool_choice` 已发；对照附录 B 探针格式。
- [ ] **T1.2** `client.py` ws **解析 function-call 事件**：`output_item.added(function_call)` + `function_call_arguments.delta` 累加 + `...done` → 装配 `LLMToolCall`；以 function_call done/`completed` 为终结。｜依赖：T1.1｜建议认领：codex｜**验收**：对真实 ws（或回放附录 B 事件流）能还原出完整 `{name, arguments}`。
- [ ] **T1.3** 去掉 `tool_complete()` 的 ws 纯文本短路（`client.py:207-211`）→ ws 返回真实 `tool_calls`。｜依赖：T1.2｜建议认领：codex｜**验收**：ws 路径 `tool_complete` 返回非空 `tool_calls`。
- [ ] **T1.4** 定义 `submit_plan` 工具 schema（§5）接入 `PMToolLoop`：取证轮 `tool_choice:auto`，**提交轮强制 `submit_plan`**。｜依赖：T1.3｜建议认领：claude-code｜**验收**：取证轮仍能调 `read_file/search_repo`；提交轮只产一次 `submit_plan`。
- [ ] **T1.5** `parse_plan` 改吃 `tool_call.arguments`（dict 直入），**删事后正则路径**；`loop.py` 同步。｜依赖：T1.4｜建议认领：claude-code｜**验收**：`PMPlan` 字段语义不变；正则路径无残留。
- [ ] **T1.6** **回归** plan→dispatch→review 全链路 + tool_pre/tool_post 顺序（`tests/test_dispatch_service.py:639-647`）。｜依赖：T1.5｜建议认领：claude-code｜**验收**：既有用例全绿。
- [ ] **P1 验收（合）**：诱发复读的输入 → **只产生一次 `submit_plan` 调用、秒级终结、正常 dispatch**；`foreman-e2e` 真机跑一次"审阅代码"任务确认（基调 §8）。

### P2 — UX

- [ ] **T2.1** 计划**增量可见**：todo 随提交渲染（`app.js:622-627`）。｜依赖：P1｜建议认领：claude-code｜**验收**：E2E 看到 todo 渐显。
- [ ] **T2.2** plan 阶段**实时状态**：reasoning 流 + "已 N 秒" + **Stop 按钮**。｜依赖：P1｜建议认领：claude-code｜**验收**：plan 中界面有进度、非白屏。
- [ ] **T2.3** **Stop 真取消**正在跑的 ws 调用（接 T0.3 可取消任务），不只标 `cancelled`（`dispatch_service.py:281-300`）。｜依赖：T0.3、T2.2｜建议认领：codex｜**验收**：点 Stop → ws 实际断、会话即停。

### P3 — 收尾

- [ ] **T3.1** 删死路径（旧正则解析残留）。｜依赖：P1｜**验收**：无 dead code。
- [ ] **T3.2** 补单测 / 文档；§7 验收 A–G 全绿。｜依赖：P1/P2｜**验收**：A–G 勾满。
- [ ] **T3.3** 二次修复纪律：本设计涉及曾被 E2E 打回的项，按 `AGENTS.md` §二**当场 E2E 复验再关**。｜依赖：相关项落地后｜**验收**：附 🖱️实测证据 + commit SHA。

---

## 7. 验收标准（Acceptance）—— 供 codex 验收

- [ ] **A. 复读不可发生**：构造会诱发复读的输入（或 mock 一个吐 3 份相同对象的流），plan **必在第一个完整对象处终结**，不产生第 2 个对象的派生事件。
- [ ] **B. 终结判定不依赖文本猜测**：终结来自 `stop_reason==tool_use`（A1）或根对象闭合（A2）或括号深度归零早切（B），代码中**无**"首{→末}"式跨对象截取作为正常路径。
- [ ] **C. 挂死可自愈**：模拟 LLM 不发 `response.completed` 且持续吐 delta，看门狗在 `T_wall` 内 abort，会话置 `failed/stalled`，**UI 显示失败原因**，不再永久 `running`。
- [ ] **D. 解析器健壮**：`_extract_json_object`（或其替代）对"多对象串联""```json 围栏""前后噪声"均返回**第一个**合法对象，单测覆盖。
- [ ] **E. UX 可见**：plan 阶段 UI 有实时进度（计时/流式），todo 渐进出现，存在可用 Stop；正常任务能看到 subagent 启动与工具调用反馈。
- [ ] **F. 无回归**：现有正常任务的 plan→dispatch→review 全链路通过既有用例；`PMPlan` 字段语义不变。
- [ ] **G. 证据**：在 main / 打包 exe 上**真机复验**本设计涉及的 plan 路径（参照 `foreman-e2e` skill），附实测现象/截图/commit SHA。

---

## 8. 测试计划

1. **单测**：`_extract_json_object` 多对象/围栏/噪声；看门狗的复读、无进展、墙钟三条触发；`parse_plan` 在 A1/A2 产出上的等价性。
2. **流式 mock**：注入一个"吐 N 份相同对象、永不 completed"的假流，断言早切 + 看门狗行为与会话状态机落到 `failed`。
3. **集成 / E2E**：用 `foreman-e2e` 在真窗口跑一条真实"审阅代码"任务，确认 plan 秒级收口、todo 渐显、subagent 启动、工具调用有反馈。

---

## 9. 回滚与风险

- A1/A2 改的是 LLM 调用契约：以 feature flag / 配置开关灰度，异常可回退到"旧自由文本 + 已修健壮解析（P0）"。
- ws/Responses 通道是否支持 strict json_schema / 强制 tool_choice，需按当前后端确认（见 §10 Q2）。
- 看门狗阈值 `T_stall/T_wall` 需经验取值，过紧会误杀长任务（默认建议 `T_wall` 偏大、复读检测优先）。

---

## 10. 留给 codex 的讨论点（Open Questions）

- **Q1**：根治选 **A1 强制工具** 还是 **A2 strict json_schema**？（A1 与 Foreman"工具调用可见化"方向更一致、利于 UX 方案 D；A2 改动面更小。倾向 A1，请 codex 评判。）
- **Q2**：当前 PM 走的 ws/Responses 后端，是否稳定支持 `tool_choice` 强制单工具 / `response_format=json_schema(strict)`？若不支持，是否切到标准 HTTP Responses/Chat 路径？
- **Q3**：看门狗阈值 `T_stall` / `T_wall` 取值，以及"复读检测"用 n-gram 还是"已见完整对象后再现 `{`"？
- **Q4**：`REVIEW_SYSTEM` / `COMPACT_SYSTEM` 是否在 P1 一并切结构化，还是 P3 单独处理？
- **Q5**：分期顺序认可吗？是否应先上 P0 止血再谈根治？

---

## 11. codex 评审记录与处置（2026-06-25）

**评审方式**：`codex exec`（无沙盒 / bypass，只读，不改文件、不跑测试）对照本仓库真实代码评审。
**结论：有条件通过。** 主根因判断与修复方向成立，但原 v1 不能照做——后端不支持强制工具/strict schema、"plan 不调工具"表述有误、A1 直接强制会破坏取证链路。以上已在 v2 §2/§4/§6 修订。

**codex 对 §10 五问的处置（已采纳）：**
- **Q1**：选 **A1**，但**不是第一轮强制唯一工具**——保留取证工具，**最终提交轮把 `submit_plan` 作为 terminal tool 强制**；A2 仅作非工具路径过渡。→ 已改 §4-A。
- **Q2**：codex 据 Foreman 客户端代码判"不支持"。**后续线上实测推翻了这一点**：ws 后端**原生支持强制工具调用**（A1 可用），仅 strict json_schema（A2）不支持。→ 路线锁定 **ws + A1**，§4「前置工程」取消、§6「P-prereq」标记已取消，证据见附录 B。
- **Q3**：plan 建议 `T_wall` 60–90s、`T_stall` 15–30s；复读检测优先**结构化**（完整对象后再现新对象/指纹重复），n-gram 辅助。→ 已改 §4-C。
- **Q4**：P0 的超时+解析兜底**必须覆盖** plan / review / compact / `PMToolLoop`；根治随 P1，不拖到最后。→ 已改 §6。
- **Q5**：认可"先 P0 止血、再 P1 根治"。→ 维持。

**阻塞项（必须满足，验收门槛）：**
1. ~~落实后端结构化能力~~ → **已解决（实测）**：ws 后端原生支持强制工具调用，路线锁定 ws + A1，无需改代理/Provider（附录 B）。
2. A1 改为 **terminal `submit_plan`**，不破坏 PM 取证→final_plan→dispatch 链路（回归 `tests/test_dispatch_service.py:639-647`）。
3. P0 范围补齐 `tools/loop.py`、`REVIEW_SYSTEM`、`COMPACT_SYSTEM`。
4. 新增 PM **LLM 调用级**总墙钟 + 复读检测 + abort/cancel 语义，并映射到明确会话状态（`stalled`）与 UI。
5. 修正 v1「plan 根本不调工具」表述（已改）。

**建议项**：早切先过 schema 校验再 abort、正确处理字符串转义/代码围栏；Stop 必须真正取消正在跑的 ws 调用。

**传输路线（已定）**：保留 `transport=ws` + A1 强制工具；A2 排除。改动全在 Foreman 客户端，不动 CLIProxyAPI。

---

## 附录 A：故障原始证据（可复现）

```python
# dist/foreman.db
sid = '3d67d3124eee4f84bc13ab9284dbf176'
# 事件构成
#   pm_output/pm-agent : 25888   全部 stream_id=plan:840d359d…  event_type=response.output_text.delta
#   pm_reasoning       : 80
#   dispatch/desktop   : 1
# 重建 plan 流文本：~46,000 字符；"ready":true} 出现 46 次；{"summary 出现 47 次
# phase 仅 [None, 'plan']；时间 08:46:13 → 08:59:51（仍在吐）
```

---

## 附录 B：ws 传输能力实测（2026-06-25，线上 `api.kongsites.com`）

**方法**：直接对线上 ws 端点 `wss://api.kongsites.com/v1/responses` 发 `response.create`（与 Foreman 同帧格式），分别测「强制工具调用」与「strict json_schema」，记录返回的事件类型。

**A1 — 强制工具调用**（请求带 `tools:[get_weather]` + `tool_choice:{type:"function",name:"get_weather"}`）→ **支持**：
```
response.created → response.in_progress
response.output_item.added        item.type=function_call name=get_weather
response.function_call_arguments.delta ×5   ('{"' 'city' '":"' 'Paris' '"}')
response.function_call_arguments.done
response.output_item.done         item.type=function_call name=get_weather
response.completed
（另有 codex.rate_limits 事件 → 确认上游为 Codex 订阅后端）
```

**A2 — 结构化输出**（请求带 `text.format:{type:"json_schema",strict:true,...}`）→ **不被 enforce**：
```
response.output_item.added(reasoning) → output_item.done(reasoning)
response.output_item.added(message)  → content_part.added
response.output_text.delta ×10 → output_text.done → response.completed
（无 schema 约束，回普通文本 message）
```

**结论**：保留 ws；用 A1（强制工具）根治 plan 终结判定；A2 在此后端不可用。CLIProxyAPI 的 `/responses` ws 为近透传，无需改动。
