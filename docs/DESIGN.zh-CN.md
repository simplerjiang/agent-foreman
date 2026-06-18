# Foreman 设计方案（v0.1）

> 一个常驻在你 PC 上的「项目经理 / 工头（PM / Foreman）」Agent：
> 它替你**监控**本地的编码 Agent（Claude Code / Codex CLI）、**调度**它们干活、
> **审阅**它们的产出，并通过**手机 PWA + Web Push** 向你汇报、在风险点向你请求审批，
> 还能让你在手机上**下发新任务**。

本文是设计文档（先于实现），目标是把架构、组件边界、数据模型、关键流程、安全模型和分阶段路线图讲清楚，
让后续实现有据可依。

> 💬 **一分钟人话版**：
> 你平时让 Claude Code / Codex 这种命令行 AI 帮你写代码。它们干活时你得盯着——怕它卡住、怕它乱来、
> 怕它偷偷干危险的事（比如把代码推上线、删文件）。**Foreman 就是替你盯梢的"工头"**：它在你电脑上一直开着，
> 看着这些 AI 干活，干得好就放行、顺手帮你审一遍代码；要干危险动作就先**摁住**，给你手机发条消息——
> "它想 push，准不准？"你在手机上点"准/不准"就行。你人不在电脑前，也能用手机给它派新活、看它干到哪了。
>
> 📖 **本文阅读约定**：凡是出现技术黑话的地方，后面都跟一个 `💬 人话：…` 的小注解，用大白话再解释一遍。
> 只想快速了解的，可以只看每节开头和这些 `💬` 注解。

---

## 1. 目标与定位

### 1.1 它解决什么问题

你在用 Claude Code / Codex 这类 CLI Agent 干活时，存在两个痛点：

1. **离开电脑时**：Agent 可能卡住、跑偏、或撞到「需要确认」的危险动作（push / 部署 / 删库），你不在场就停摆。
2. **回到电脑时**：要花时间翻日志、看 diff、回忆「它刚才干到哪了」。

Foreman 让一个 LLM 驱动的 PM 角色 7×24 盯着这些 Agent：
正常推进时它**自动审阅、放行、记录**；遇到风险时它**暂停并把审批卡片推到你手机**；
你随时可以在手机上看简报、批准/驳回、或下发新指令。

### 1.2 与参考项目 Cteno 的关系

[Cteno](https://github.com/zalan159/cteno-community) 是一个雄心更大的 Rust 三层系统（本地控制面 `CtenoHost` +
原生 ReAct runtime + 跨端控制面），目标是「跨机器、跨时间的 Agent 责任承担」。

Foreman **借鉴其理念**（长任务为中心、网关化自治、Agent 平权、本地优先、手机作为审批/简报面），
但**刻意收窄范围**以便快速做出可用 MVP：

> 💬 **人话：上面那几个词啥意思？**
> - **长任务为中心**：不是"问一句答一句"，而是盯着一件能干好几小时的大活。
> - **网关化自治**：平时让 AI 自己干（自治），但在危险动作前设一道"闸门"（网关）拦一下，要你点头。
> - **Agent 平权**：Claude、Codex、以后接的别的 AI，一视同仁，谁来都用同一套规矩管。
> - **本地优先**：数据、控制都在你自己机器上，服务器只是个"中转站"，不把你的东西攒在云上。
> - **MVP**：Minimum Viable Product，能跑起来、够用的最小版本，先用上再慢慢加。

| 维度 | Cteno | Foreman |
|------|-------|---------|
| 语言 | Rust | Python（更快出 MVP） |
| 跨机器 | 一等公民 | 先做单机，多机留作扩展 |
| 手机端 | 原生 + 自研协议 | 自托管 PWA + Web Push（无需上架） |
| Agent runtime | 自研 ReAct | 不自研，只**驱动**现成 CLI（Claude Code / Codex） |
| LLM | 多 provider | 用**你自己的 API**（OpenAI 兼容 / Anthropic 兼容） |

一句话：**Cteno 想做平台；Foreman 想先做一个你今晚就能跑起来的「带手机遥控的 Agent 工头」。**

### 1.3 非目标（MVP 阶段不做）

- 不自研 Agent 推理 runtime（直接复用 Claude Code / Codex）。
- 不做应用商店上架（用 PWA「添加到主屏幕」即可）。
- 不做复杂的多机器**任务编排**（多机协同跑同一活；先把单机做扎实，接口预留）。

> ✅ **已纳入范围（v0.2 调整）**：**多用户 / 团队模式**——一台共用服务器当"总机"，多人各用各的本地进程，
> 通过 access key 接入（见 §8）。同一套代码既可**个人自用（以后开源）**，也可**团队共用**。

---

## 2. 核心概念

这张表是全文的"词典"。技术名词在左，**大白话**在右——看不懂某个词时回这里查。

| 概念（技术名） | 一句话说明 | 💬 大白话 |
|------|------|------|
| **PM Brain（工头大脑）** | 用你的 LLM API 驱动的决策循环：判断 Agent 状态、决定何时审阅 / 升级 / 简报。 | 工头的脑子。用你的 AI 帮它判断"现在该干嘛"：继续看着、还是该审一下、还是该问你。 |
| **Root Session（主会话）** | 一个目标的长生命周期容器：包含目标、计划、若干 Task、状态、工作区路径。 | 一件大活的"档案袋"。装着目标、计划、拆出来的小任务、进度、在哪个文件夹干。 |
| **Task（任务）** | 主会话下的一次具体执行，绑定某个 Agent（claude / codex），有指令与状态。 | 大活拆出来的一个小步骤，交给某个 AI 去做。 |
| **Agent Adapter（适配器）** | 把不同 CLI（claude / codex）抽象成统一接口，PM 通过它启动/喂指令/读输出。 | "转接头"。claude 和 codex 用法不一样，套个转接头后，工头用同一套动作就能指挥它俩。 |
| **Gate（网关 / 审批闸）** | 把动作分级（安全 / 需策略 / 需审批），危险动作在此暂停并请求人工批准。 | "闸门"。危险动作（push、删库）到这儿先被拦下，等你手机点头才放行。 |
| **Review（审阅）** | 在检查点把 diff + 上下文交给 LLM 评审，产出「通过 / 要改 / 升级人工」结论。 | AI 写完一段，工头再用 AI 帮你审一遍代码改动，给个结论：行 / 要返工 / 拿不准找你。 |
| **Briefing（简报）** | PM 生成的人类可读摘要，定时或「你回来时」推送到手机。 | 工作汇报。定时或你一回来，就给你一段"刚才干了啥、卡在哪、建议下一步"。 |
| **Control Surface（控制面）** | 手机 PWA：看时间线、批审批、下发任务。 | 你的手机界面：看进度、批/驳、派活。 |
| **Event（事件）** | 系统里发生的一件事（AI 输出、工具调用、出错、git 改动…），先落库再分发。 | 流水账上的一条记录。所有发生的事都记一笔，手机上看到的时间线就是它。 |
| **Workflow（工作流）** | 一件活的步骤骨架 + 每步挂的积木 + 审批点；存数据库（详见 §11.2）。 | 干活的"剧本/流程"，你的核心秘方之一。 |
| **Skill（技能）** | 某类任务的做法手册，需要时喂给 AI；存数据库。 | "这种活怎么做好"的说明书。 |
| **Code Standard（代码规范）** | 命名/结构/禁用项等约束；注入工作区 + 审阅时校验；存数据库。 | 你家写代码的"规矩"。 |
| **QA Rubric（QA 标准）** | 验收清单/打分标准，Reviewer 据此判过不过；存数据库。 | "怎么算合格"的尺子。 |
| **Operator（操作员）** | 用你 LLM 驱动、配满 MCP 工具的"副驾"：读输出、精简、提判断、提出要执行的指令（详见 §6）。 | 那个替你动手、替你拿主意的 AI。 |
| **Auditor（指令审核员）** | 独立的第二个 LLM，动手前审核操作员每条指令该不该跑，唱反调挡垃圾/危险。 | 专门挑刺、把关的"安检员"。 |
| **Decision Card（决策卡）** | 精简状态 + 审核意见 + 2–4 个候选动作 + 手打指令口，推到 PC/手机给你点。 | 你只管点的"选择题卡片"。 |
| **Checkpoint / Undo（检查点/一键回退）** | 每步开干前 git 后台存档，可一键回退到任意点。 | 后悔药，比 Copilot 的 undo 更狠。 |
| **Autonomy Dial（自治档位）** | 0 只汇报 / 1 凡事都问 / 2 自动干小事 / 3 大胆自治，控制它多主动。 | 给它放权的"松紧旋钮"。 |
| **Local Process（本地进程）** | 跑在你机器上、驱动 claude/codex、持有本地库与秘方的常驻程序。一个人可有多台。 | 真正干活、存数据的"你那台"。 |
| **Relay（总机 / 中继）** | 团队模式下服务器的角色：认账号、按 access key 把 PWA 接到对应本地进程，只转发不存秘密。 | 接线的"总机"。 |
| **Account / Access Key（账号 / 接入密钥）** | 管理员建账号；账号生成 access key（一机一张、可多张），填进本地进程认身份。 | 账号是人，key 是给本地进程插的"SIM 卡"。 |

> 💬 **再统一解释两个高频词**：
> **Agent（智能体）** = 能自己干活的 AI 程序，这里特指 Claude Code / Codex 这类命令行 AI。
> **CLI** = Command Line Interface，命令行程序，就是在黑框框里敲命令用的那种软件。

---

## 3. 总体架构

```
┌──────────────────────────────────── PC（本地，常驻）────────────────────────────────────┐
│                                                                                          │
│   ┌──────────────────────────── PM Core（Python 守护进程）────────────────────────────┐  │
│   │                                                                                    │  │
│   │   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐                      │  │
│   │   │ PM Brain │   │ Reviewer │   │   Gate   │   │ Scheduler  │                      │  │
│   │   │  (LLM)   │   │  (LLM)   │   │  审批闸  │   │ 周期评估    │                      │  │
│   │   └────┬─────┘   └────┬─────┘   └────┬─────┘   └─────┬──────┘                      │  │
│   │        └──────────────┴──────┬───────┴───────────────┘                             │  │
│   │                    ┌─────────┴─────────┐                                           │  │
│   │                    │   Event Bus       │  ← 所有组件读写事件流                       │  │
│   │                    └─────────┬─────────┘                                           │  │
│   │                              │                                                     │  │
│   │                    ┌─────────┴─────────┐                                           │  │
│   │                    │  Store (SQLite)   │  sessions / tasks / events / reviews ...  │  │
│   │                    └───────────────────┘                                           │  │
│   └────────┬───────────────────────────────────────────────┬───────────────────────────┘  │
│            │                                                │                              │
│   ┌────────┴──────────┐                          ┌──────────┴──────────┐                    │
│   │   Agent Runner    │                          │   Monitor / 观测     │                    │
│   │  （适配器层）       │                          │  - Hook Receiver    │                    │
│   │  - Claude Code     │                          │  - Git Watcher      │                    │
│   │  - Codex CLI       │                          │  - Process/Log Tail │                    │
│   └────────┬───────────┘                          └─────────────────────┘                    │
│            │ headless exec / pty                       ▲                                      │
│   ┌────────┴───────────────────────────┐              │ Claude Code Hooks(POST)              │
│   │  claude -p / codex exec（在工作区） │──────────────┘                                      │
│   └────────────────────────────────────┘                                                     │
│                                                                                              │
│   ┌──────────────────────── Web Backend（FastAPI: REST + WS + WebPush）────────────────────┐ │
│   └───────────────────────────────────────┬─────────────────────────────────────────────────┘ │
└───────────────────────────────────────────┼───────────────────────────────────────────────────┘
                                            │  HTTPS（Tailscale / Cloudflare Tunnel / frp）
                                            │
                                    ┌───────┴────────┐
                                    │   手机 PWA      │  iOS/Android 浏览器，可「添加到主屏幕」
                                    │  时间线 / 简报   │  Web Push 推送（应用关闭也能收）
                                    │  审批 / 下发任务 │
                                    └────────────────┘
```

### 3.1 三个进程边界

1. **PM Core + Web Backend**：同一个 Python 进程（asyncio），常驻。
2. **Agent 子进程**：claude / codex 由 Runner 以子进程方式拉起，独立工作区。
3. **手机 PWA**：浏览器里的静态页面，通过 HTTPS 访问 Web Backend。

> 📌 上图是**个人模式**（PWA 直连本地）。**团队模式**下，PWA 部署在共用服务器、由一个**总机（Relay）**把多人的本地进程接起来——拓扑与身份模型见 **§8**。同一套代码两种模式。

---

## 4. 组件详解

### 4.1 PM Core

#### PM Brain / Operator（决策循环 + 操作员）
- 输入：事件流（Agent 输出、hook 事件、git 变化、定时器）。
- 职责：判断每个会话状态（`running / idle / blocked / waiting-approval / done / failed`）；
  **精简** agent 输出、**提出**下一步要执行的指令/动作（即"操作员"角色，配满 MCP 工具，见 §6.1）。
- 实现：状态机 + LLM 辅助判断。**确定性的事**（进程死没死、有没有 diff）用代码判断，
  **需要语义判断的事**（输出像不像卡住、要不要升级）才调 LLM。
- 它**提出**的每条指令都先过 Auditor，再按自治档位决定是否出决策卡——它自己不能"自批自执行"。

#### Auditor（指令审核员）
- 角色：**独立的第二个 LLM**，在操作员每条指令执行**之前**审核（见 §6.1/§6.2）。
- 输入：操作员的指令 + 理由 + 预期影响 + 相关上下文 + 适用的代码规范/QA 标准。
- 要求：**独立上下文、唱反调**——prompt 明确"你的职责是拦下垃圾/跑偏/危险/浪费的指令，拿不准就打回"。
- 输出：`{verdict: pass|reject, reasons[], suggestions[]}`；`reject` → 退回操作员改了重提（你看不到这些噪音）。
- 可扩展：以后档位调高、审核更吃重时，可升级为**多审核员投票**（tokens 当前近似免费，撑得起）。

#### Reviewer（自动审阅）
- 与 Auditor 互补：**Auditor 管"动手前要不要做"，Reviewer 管"动手后做得好不好"。**
- 触发：Agent 到达检查点（Claude Code `Stop` hook、任务声明完成、产生一批 diff）。
- 输入：`git diff` + 任务目标 + 该步的 QA 标准 + 相关文件片段。
- 输出：结构化评审 `{verdict, summary, risks[], suggestions[], needs_human}`。
- `verdict ∈ {approve, request_changes, escalate}`；判为垃圾时，决策卡直接提供 `[⛔ 撤掉重来]`（一键回退）。

#### Gate（权限 / 审批闸）
- 动作分级（借鉴 Cteno）：
  - **安全（safe）**：读文件、在已批准工作区改代码、跑测试 → 直接放行。
  - **需策略（needs-strategy）**：大规模重构、加依赖、改协议 → 需要先有计划/说明。
  - **需审批（requires-approval）**：`git push`、部署、改 secrets、删除/破坏性操作 → **暂停**，推审批卡到手机。
- 与 Claude Code 的 `PreToolUse` hook 联动：危险工具调用先被拦截，待批。
- 原则：**可逆动作放手，不可逆动作必拦**（undo 救不回的，只能事前问你）。

#### Checkpoint Manager（检查点 / 一键回退）
- 每"一步"开干**前**自动打检查点（用 git 在后台存档：commit/stash/tag，记在 `checkpoints` 表的 `vcs_ref`）。
- 提供"回退到任意检查点"：PC/手机一键 `git reset` 到该点 + 复位 agent 状态（见 §6.5）。
- 不污染你的正式提交历史（用独立 ref / 分支；接受后再合并/压缩）。
- 边界：只保**工作区文件**可回退；网络/数据库/部署等**不可逆副作用**靠 Gate 事前拦，不靠 undo。

#### Scheduler（调度 / 周期评估）
- 周期性（如每 N 分钟）让 PM Brain 巡检所有活跃会话。
- 「你回来了」检测（活跃窗口/解锁事件，可选）→ 触发 Active Briefing。
- 定时简报（如每天早上 09:00 汇总昨夜进展）。

#### Event Bus
- 进程内 async 发布/订阅。所有事件先落库（`events` 表）再分发，保证可回放、可在手机时间线展示。

### 4.2 Agent Runner & Adapters

统一接口（`AgentAdapter`）：

```python
class AgentAdapter(Protocol):
    name: str  # "claude-code" | "codex"
    async def start(self, task: Task, workspace: Path) -> AgentHandle: ...
    async def send(self, handle: AgentHandle, text: str) -> None: ...   # 追加指令（两向控制）
    async def stream(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]: ...  # 结构化事件
    async def interrupt(self, handle: AgentHandle) -> None: ...         # 暂停/打断
    async def stop(self, handle: AgentHandle) -> None: ...
```

- **Claude Code 适配器**：优先用无头模式
  `claude -p "<prompt>" --output-format stream-json --verbose`，逐行解析 JSON 事件；
  会话续接用 `--resume <session_id>` / `--continue`。配合 hooks 做实时观测。
- **Codex 适配器**：`codex exec "<prompt>"`（非交互），解析其输出；需要交互续接时用 pty。
- **PTY vs headless**：MVP 以 headless 为主（在 Windows 上更省心）。
  需要真正「交互式喂输入」时，用 PTY（Linux/macOS: `ptyprocess`；Windows: `pywinpty`）。
- **Windows 注意**：用户在 Windows 11。优先 headless；pty 走 `pywinpty`；
  子进程注意 `creationflags`、编码（UTF-8）、`claude` 实为 `.cmd` shim。

### 4.3 Monitor（观测）

三路观测，互补：

1. **Hook Receiver**：Claude Code 的 hooks（`PreToolUse` / `PostToolUse` / `Stop` /
   `Notification` / `SubagentStop`）配置成 `curl` POST 到本地 `http://127.0.0.1:<port>/hooks`。
   这是**最干净的实时信号源**，无需轮询。见 `hooks/claude-hooks.example.json`。
2. **Git Watcher**：watch 工作区 `.git`，捕获 diff / commit，喂给 Reviewer。
3. **Process / Log Tail**：判断子进程存活、CPU/IO 是否空闲（疑似卡住）、尾随 stdout。

> Codex 暂无等价 hook 机制，主要靠输出解析 + git 观测；后续可加 MCP / wrapper。

### 4.4 LLM Client（用你自己的 API）

- Provider 无关：支持 **OpenAI 兼容**（`/v1/chat/completions`）与 **Anthropic 兼容**（`/v1/messages`）。
- 配置 `base_url` + `api_key` + `model`，由你提供。**配置来源两条路**（本地进程按此优先级取）：
  1. **个人模式**：本地 `.env` / `config.yaml`；
  2. **团队模式**：账号在 PWA 里设、服务器加密存的 `account_llm_config`，连接后下发（见 §8.3）。
- 用于 **PM Brain / Operator / Auditor / Reviewer / Briefing** 等 Foreman 自身的 LLM 调用。
- 注意区分：**Foreman 的「大脑」用你的 API；被驱动的 claude/codex CLI 用它们各自的登录/额度**。
  两者解耦，互不影响。

### 4.5 Store（SQLite）

本地优先，单文件 SQLite（`foreman.db`），用 SQLModel/SQLAlchemy。见 §7 数据模型。

### 4.6 Web Backend + PWA + Web Push

- **后端**：FastAPI（`uvicorn`），提供：
  - REST：会话/任务/事件/审批/简报/**决策卡/步骤详情** 的增删查。
  - **步骤详情接口**：`GET /api/actions/{id}/detail` → 组装出 ① 原始返回（该步的 `agent_output`/`tool_*` 事件）+ ② 代码改动（该步检查点 ↔ 当前的 `git diff`，逐文件逐行）。
  - WebSocket：把 Event Bus 实时推到打开着的 PWA。
  - Web Push：`pywebpush` + VAPID，应用关闭也能推（审批、简报）。
  - Auth：单用户 Bearer Token（见 §8）。
- **前端 PWA**：轻量（原生 JS 或 Preact + Vite），
  `manifest.webmanifest` + `service worker`（`sw.js`）实现可安装 + 推送接收。
  页面：仪表盘（活跃会话）、时间线、**决策卡**、**步骤详情页（原始返回 / 代码改动 两个标签）**、任务下发框、简报列表。
  > 💬 决策卡上的 `[🔍 查看详情]` 就跳到"步骤详情页"——见 §6.3。

---

## 5. 关键工作流

### 5.1 下发任务（手机 → PC）
```
手机 PWA 输入「帮我把 X 重构成 Y，push 前问我」
  → POST /api/tasks
  → PM Brain 创建 Root Session + 计划
  → Agent Runner 在工作区拉起 claude/codex
  → 事件开始流入时间线
```

### 5.2 监控
```
hooks / git / process 事件 → Event Bus → 落库
  → PM Brain 周期评估：running? idle? blocked?
  → blocked（如等待输入/报错）→ 生成提示，必要时推手机
```

### 5.3 自动审阅
```
Stop hook / 任务完成 / 一批 diff
  → Reviewer：diff + 目标 → LLM → {verdict, risks, ...}
  → approve：记录并继续
  → request_changes：把修改意见再喂回 Agent（send）
  → escalate / 命中危险动作 → 交 Gate
```

### 5.4 审批（PC → 手机 → PC）
```
Gate 生成审批卡（动作、风险、diff 摘要）
  → Web Push 推到手机
  → 你在 PWA 点「批准 / 驳回（可附理由）」
  → POST /api/approvals/{id}
  → Gate 放行（恢复 Agent）或中止（interrupt + 反馈）
```

### 5.5 简报（PC → 手机）
```
Scheduler 定时 / 「你回来」检测
  → PM Brain 汇总：做了什么、卡在哪、有何风险、建议下一步（带证据链接）
  → 落 reports 表 + Web Push
```

---

## 6. 决策、审核与审批回路（核心交互模型）

这一节是 Foreman 的"心脏"。目标：**让你从"逐行读输出、手写 prompt、盯着防垃圾"的人，变成"看精简汇报、点按钮指挥"的人。**

### 6.1 四个角色

| 角色 | 干啥 | 谁来当 |
|------|------|--------|
| **Operator（操作员）** | 看 agent 输出、精简、提判断、**提出**下一步要执行的指令/动作。给它配满 MCP 的"手"（命令行、读写文件、操作电脑）。 | 你的 LLM（一套 prompt） |
| **Auditor（指令审核员）** | **独立**审核操作员每一条要执行的指令：该不该跑？是不是垃圾/跑偏/危险？不过就打回。 | 你的 LLM（另一套 prompt，**独立上下文、唱反调**） |
| **Gate（审批闸）** | 把动作分级：可逆的放，**不可逆的拦下**问你。 | 代码规则 |
| **You（你）** | 在 PC/手机上看**决策卡**，一点定夺；或手打复杂指令。 | 人 |

> 💬 **人话：为什么要两个 LLM？** 一个"动手的"配一个"挑刺的"，像开车的副驾旁边再坐个安检员。
> 操作员有干活的冲动，容易自我感觉良好；审核员专职唱反调、专挑"这条命令是不是垃圾/会不会闯祸"。
> **动手前先审核、动手后再审阅**，两道独立把关，垃圾代码很难溜过去。

### 6.2 一条指令的完整流水线

```
Operator 想执行一条指令（带：要干啥 + 为什么 + 预期影响）
        │
        ▼
 ① Auditor 独立审核  ──打回（附理由）──▶ 退回 Operator 改了重提（你根本看不到这些噪音）
        │ 通过（并附上审核意见）
        ▼
 ② Gate 分级：  可逆？──不可逆──▶ 必须走审批（推你手机）
        │ 可逆
        ▼
 ③ 按【自治档位】：当前 = "凡事都问" → 生成决策卡
        │
        ▼
 ④ 你在 PC/手机点：[✅ 通过] [🔄 改方向] [⛔ 撤掉] [✍️ 手打复杂指令]
        │ 批准
        ▼
 ⑤ 先打【检查点】→ 执行 → 记录 →（发现是垃圾）一键回退到检查点
```

### 6.3 决策卡（Decision Card）——默认看精简，想细看一键下钻

不再默认甩你一坨原始日志。每到关键节点，Operator + Auditor 合出一张卡；**但精简只是"折叠态"，你随时能展开看到底改了什么**：

```
┌─ codex 完成「登录重构」 ───────────────────────┐
│ 💬 一句话：抽成 hook，删了 80 行，加了 2 个测试。     │
│ 🕵️ 审核员：测试只覆盖正常路径，异常没测；其余 OK。    │
│ 📎 改动：3 个文件 +124 / −80     [🔍 查看详情]        │
│ 选一个： [✅ 通过] [🔄 让它补异常测试] [⛔ 撤掉重来]   │
│          [✍️ 我自己打一条复杂指令…]                  │
└────────────────────────────────────────────────┘
```

**[🔍 查看详情] 跳转到"步骤详情页"，两个标签页：**
- **① 原始返回**：codex / claude / copilot 这一步**到底说了什么**——完整输出、推理、调用了哪些工具（从 `events` 里的 `agent_output` / `tool_pre` / `tool_post` 还原）。
- **② 代码改动**：**到底改了哪些文件、哪几行**——逐文件、逐行的 diff（这一步的检查点 ↔ 当前的 `git diff`，带行高亮）。

> 💬 **人话**：卡片是"摘要"，详情页是"原始证据"。你平时只看摘要点按钮；一旦觉得不对劲，一键翻到它**原话**和**逐行改动**，自己核对——既不用一直逐行盯，又随时查得到底。

### 6.4 自治档位（Autonomy Dial）——能力给满，落子由你松紧

> 决定它"多主动、多少事问你"。**能力（MCP 的手）一直是满的，变的只是"落子要不要先问你"。**

- **档0 只汇报** · **档1 凡事都问（⭐ 当前默认）** · **档2 自动干可逆小事、大事弹卡片** · **档3 大胆自治，只拦不可逆**
- 可按工作流 / 阶段 / 工作区分别设。信任是慢慢加上来的——**你 undo 越好用，就越敢往上调档。**

### 6.5 一键回退（Checkpoint / Undo）——比 Copilot 更狠

- **每"一步"开干前自动打检查点**（git 后台存档，不污染你的正式提交历史）。
- 觉得哪步是垃圾 → **一键回退**，PC/手机都能点；还能往回跳好几步（"时光机"）。
- **审阅/QA 发现垃圾时，决策卡直接给你 `[⛔ 撤掉重来]` 按钮**——你都不用自己看出来它写歪了。

### 6.6 Gate 的动作分级（可逆 vs 不可逆）

| 级别 | 示例动作 | 默认处理 |
|------|----------|----------|
| safe（可逆） | 读文件、改已批准工作区代码、跑测试、本地构建 | 走流水线；可逆 → 高档位时可自动 |
| needs-strategy | 大重构、加/升依赖、改公共接口/协议、改 CI | 需先有计划说明；可配置自动或转审批 |
| requires-approval（不可逆） | `git push`、发布/部署、改 secrets/凭据、`rm -rf`/删表等 | **一律暂停**并推手机审批（undo 救不回来的，只能事前问） |

- 分级策略写在 `config.yaml` 的 `gates:`，支持按工作区/会话覆盖。
- 与 Claude Code `PreToolUse` hook 联动：危险工具调用先被拦、待审。
- **白名单工作区**：只有显式批准的目录才允许写。
- 🔑 **一句话原则：可回退的，放手干；不可逆的，必先问。审核员管"要不要做"，审阅员管"做得好不好"。**

---

## 7. 数据模型

数据分两个库：**本地进程库**（每人各自机器上，含全部历史与「秘方」）和**服务器 / 总机库**（共用服务器上，只有账号、key、路由与展示缓存）。

### 7.1 本地进程库（local SQLite）

```
sessions(id, goal, plan, status, workspace, agent_type, created_at, updated_at)
tasks(id, session_id→sessions, instruction, status, agent_handle, created_at, updated_at)
events(id, session_id, task_id, type, source, payload_json, ts)
reviews(id, task_id, verdict, summary, risks_json, suggestions_json, needs_human, ts)
approvals(id, session_id, task_id, action, risk_level, diff_summary, status, reason, requested_at, decided_at)
reports(id, session_id, kind, title, body_md, sent, ts)          # kind: handoff|active-briefing|daily
push_subscriptions(id, endpoint, p256dh, auth, ua, created_at)   # WebPush 订阅
config_kv(key, value)                                            # 运行期可变配置；含 autonomy 档位

# —— 决策与执行回路（见 §6）——
actions(id, session_id, task_id, kind, command, rationale, expected_effect, reversible, status, checkpoint_id, created_at, executed_at)
            # status: proposed|audited|carded|approved|rejected|executed|undone
audits(id, action_id→actions, verdict, reasons_json, suggestions_json, model, ts)   # verdict: pass|reject（审核员的结论）
decision_cards(id, action_id→actions, session_id, summary, audit_note, options_json, chosen, decided_at, ts)
checkpoints(id, session_id, task_id, step_index, vcs_ref, label, created_at)         # vcs_ref: git commit/stash/tag，回退用

# —— 可扩展层：你的"秘方"（四种积木），存库、不进 git、UI 里改（见 §11.2）——
definitions(id, kind, name, version, status, scope_json, body, metadata_json, is_active, created_at, updated_at)
            # kind: workflow|skill|code_standard|qa_rubric ；(name,version) 唯一；is_active 标记当前启用版
definition_links(id, from_id→definitions, to_id→definitions, relation, step_index)
            # 把"工作流第 N 步 → 用哪块积木"连起来；relation: uses_skill|uses_standard|judged_by
workflow_runs(id, session_id→sessions, workflow_id→definitions, step_index, step_status, started_at, ended_at)
            # 某次会话按某工作流跑到第几步、每步什么状态

# —— 升级用 ——
schema_version(version, applied_at)                              # 数据库结构版本号，给迁移用（见 §11.1）
```

### 7.2 服务器 / 总机库（仅团队模式）

```
accounts(id, username, display_name, role, status, created_at)        # role: admin|member
access_keys(id, account_id→accounts, key_hash, label, last_seen_at, status, expires_at, created_at)
            # 一机一张；只存哈希；可单独吊销/设有效期
process_registry(id, account_id→accounts, access_key_id, name, online, last_heartbeat, created_at)
            # 当前在线的本地进程（出站长连接注册在此）
account_llm_config(account_id→accounts, provider, base_url, model, key_ciphertext, max_tokens, updated_at)
            # 各账号自己设的 LLM 配置；key 加密存储、按账号隔离；连接后下发给该账号的本地进程
cache_sessions(account_id, session_id, summary_json, updated_at)      # 展示缓存（只读副本，供本地离线时查看）
cache_cards(account_id, card_id, payload_json, status, updated_at)    # 决策卡缓存，供离线查看/推送
invites(id, code_hash, account_id, expires_at, used_at)               # 管理员邀请码（哈希存储）
schema_version(version, applied_at)
```

- 🔒 服务器库**仍不含** `definitions`（你的工作流「秘方」）/ 完整 diff / 原始返回——这些只在**本地进程库**，按需经总机实时拉取（见 §8.3）。
- 🔑 **例外：LLM key 可由各账号在 PWA 里自行设置、存在服务器**（`account_llm_config`，**加密存储、按账号隔离**），连接后下发给该账号的本地进程使用。好处：一个人多台机器只需设一次。也可不用、改在本地 `.env` 设（见 §8.3/§8.4）。

- `status(session) ∈ {planning, running, idle, blocked, waiting_approval, done, failed, paused}`
- `events.type` 枚举：`agent_output | tool_pre | tool_post | stop | git_diff | git_commit | review | action_proposed | audit | card_decided | checkpoint | undo | approval_req | approval_decided | briefing | error | dispatch`
- `definitions.body` 存内容本身：技能/规范用 Markdown，工作流/QA 标准用 YAML/JSON（文本）。`scope_json` 记"何时适用"（语言、路径通配、触发条件）。
- 所有时间用 UTC ISO8601。

---

## 8. 部署模式、多用户与安全

### 8.1 两种部署模式（同一套代码）

| 模式 | 谁连谁 | 适合 |
|------|--------|------|
| **个人 / 开源模式** | 本地进程自己开 PWA（localhost 或自有隧道），无服务器、无账号 | 一个人自用；开源用户开箱即用 |
| **团队 / 共用模式** | 一台服务器当**总机（Relay）**，多人本地进程**出站**连入，PWA 经总机接到各自机器 | 多人共用一台服务器 |

```
                    ┌─────────── 服务器(一台 · 公网) ───────────┐
  你的 PWA     ───▶ │  ① PWA 静态页 + 登录                        │
  同事A PWA    ───▶ │  ② 管理员控制台:建用户 / 账号 / access key   │
  同事B PWA    ───▶ │  ③ ★总机 Relay:按账号把消息转给对应本地进程   │
                    └──────▲────────────▲────────────▲───────────┘
                           │ 出站长连接(带 access key)
              ┌────────────┘            │            └────────────┐
       你的本地进程(可多台)        同事A 本地进程         同事B 本地进程
       · 跑 claude/codex            (各自机器)            (各自机器)
       · 本地 SQLite(会话/秘方)  ← 真相在本地,服务器只转发
```

> 💬 **人话**：服务器像电话**总机**。本地进程"拨号进来"报上 access key，总机就知道"这是谁的机器"，把你手机（PWA）接到**你自己**那台机器上。本地进程在内网/防火墙后，所以是**它主动连出去**，不需要给每台 PC 开公网端口。

### 8.2 身份模型：管理员 / 用户 / access key

流程（对应团队需求）：

1. **管理员**在控制台**建用户**（如 3 个账号），发**邀请 / 初始密码**；**不开放自助注册**（共用服务器更可控）。
2. 用户登录 PWA，**生成 access key**（可生成多张）。
3. 把 key 填进**自己的本地进程**配置；一个人可有**多台机器 / 多个进程**。
4. 本地进程带 key 出站连服务器 → 总机认得这是哪个账号的机器 → PWA 与你自己的本地进程接通。

- **access key = 给本地进程插的"SIM 卡"**：认一个账号；**一台机器一张，一个账号可发多张**，丢了 / 换机可**单独吊销**，不影响别的机器。
- 区分两种凭据：**用户登录**（人用 PWA）vs **access key**（本地进程认账号）。

### 8.3 数据放哪：秘方本地 + LLM key 按账号 + 展示缓存（你选的混合方案）

- **秘方（工作流 / 技能 / 代码规范 / QA 标准）只在各自本地进程**——共用服务器**绝不**存，3 人的秘方互不相见。
- **LLM key 各自设置、可存服务器**：每个账号在 PWA 里设自己的 `base_url / model / key`，**加密、按账号隔离**地存在服务器，连接后下发给该账号的本地进程。一个人多台机器只设一次。（也可改在本地 `.env` 设，二选一。）
- **会话摘要 / 事件 / 决策卡**在服务器**缓存一份**，这样本地进程离线时，PWA 仍能看到最近状态（只读）；本地一上线就以本地为准同步。
- 缓存可只存"展示必需"的精简内容；原始返回 / 完整 diff（§6.3 详情页）按需向本地进程实时拉取。

> 💬 **边界一句话**：服务器**可以**握你的 LLM key（你自己设、加密存、只给你的机器用），但**握不到你的工作流秘方**——秘方永远只在本地。

### 8.4 安全要点

- **远程访问 / HTTPS**：Web Push 必须 HTTPS。团队模式服务器直接用公网域名 + TLS；个人模式可用 Tailscale / Cloudflare Tunnel / frp。
- **总机持有的"秘密"仅限各账号自设的 LLM 配置**：服务器存账号、access key 哈希、路由、展示缓存、以及 `account_llm_config`；**不存**工作流秘方、不存完整 diff/原始返回。
- **LLM key 加密存储**：`key_ciphertext` 用服务器侧主密钥加密（主密钥放服务器 `.env` / KMS，不与数据库同处）；传输走 TLS；下发给本地进程时按账号隔离。
  - ⚖️ **取舍**：这换来了"一处设置、多机通用"的便利，代价是服务器被攻破时该账号 LLM key 可能泄露。鉴于你的 LLM 网关就在同一台服务器、key 仅对应本机服务，影响面有限；仍建议加密 + 可随时在 PWA 里轮换。
- **access key**：服务器只存哈希；明文仅生成时显示一次；可逐个吊销 / 设有效期。
- **多租户隔离**：每条记录绑 `account_id`，用户只看自己的；管理员看系统健康，看不到他人内容（含他人 LLM key 明文）。
- **审批动作签名**：审批请求带一次性 nonce，防重放。
- **工作区白名单 + 危险命令网关**：纵深防御，即使被越权也卡在 Gate。
- **其余 Secrets**：VAPID 私钥、服务器主密钥放服务器 `.env`；本地进程的 `.env` 仍可独立设 LLM key（不走服务器那条）。所有 `.env` 不入库、不进 git。

> 💬 **人话：几个安全词**
> - **access key 只存哈希**：服务器存的是 key 的"指纹"不是原文，库被翻了也还原不出你的 key。
> - **nonce / 防重放**：一次性口令，用过作废，防别人录下你"批准"那条再重发冒充你。
> - **纵深防御**：多设几道关卡，第一道破了还有"闸门"挡着，光偷到密码也删不了你的库。
> - **多租户隔离**：一台服务器上多个账号，各看各的，互相看不见。

---

## 9. 技术选型

| 层 | 选型 |
|----|------|
| 语言/运行时 | Python 3.11+（asyncio） |
| Web 框架 | FastAPI + uvicorn |
| ORM/存储 | SQLModel（SQLAlchemy）+ SQLite |
| 配置 | pydantic-settings + YAML + `.env` |
| LLM 调用 | httpx（OpenAI 兼容 / Anthropic 兼容，自封装） |
| 子进程/PTY | asyncio subprocess（headless）；`pywinpty`/`ptyprocess`（交互） |
| 文件观测 | watchfiles |
| Web Push | pywebpush + py-vapid |
| 前端 | PWA：原生 JS 或 Preact + Vite；service worker |
| 进程管理 | 一个入口 `foreman serve`；可选 NSSM/计划任务做开机自启 |

---

## 10. 与 Claude Code Hooks 的集成

Claude Code 支持在事件点执行命令（hooks）。我们把它们指向本地接收端：

> 💬 **人话：hook（钩子）是啥？**
> 就是"在某件事发生的那一刻，自动帮你跑一条命令"的机制。比如"Claude 每次要用某个工具之前"——
> 这就是一个时刻（事件），我们在这个时刻挂一个"钩子"，让它自动把"它要干啥"汇报给 Foreman。
> 这样 Foreman 不用一直去问、去猜，事情一发生就第一时间知道——既实时又省力。

- `PreToolUse`：危险工具调用前 → POST，必要时返回阻止（触发审批）。
- `PostToolUse`：工具调用后 → POST（观测做了什么）。
- `Stop` / `SubagentStop`：一段工作结束 → 触发 Reviewer。
- `Notification`：Agent 主动通知（如等待输入）→ 可直接推手机。

示例配置见 `hooks/claude-hooks.example.json`（用 `curl` POST 到 `http://127.0.0.1:8787/hooks`）。
Codex 侧暂以输出解析 + git 观测为主，后续探索 MCP/wrapper 等价信号。

---

## 11. 可更新性与可扩展性

单机/自托管软件最怕的就是"装上之后僵在那儿——改不动、升不了级、加个功能就得动核心"。
这一节专门讲怎么让 Foreman **能持续升级**、**能加新功能而不推倒重来**。

### 11.1 可更新性（能不断升级，且不丢数据）

Foreman 跑在两个地方，两边都要能更新：**PC 端**（工头大脑 + 驱动 claude/codex）和
**服务器端**（手机能访问的 Web 后端 + PWA）。手段如下：

1. **代码自动更新**
   - 服务器端：**GitHub Actions**——你把代码 push 到 `main`，服务器自动拉新代码、装依赖、重启。（本仓库正在配置）
   - PC 端：一条 `foreman update` 命令 = 拉新代码 + 装依赖 + 重启；或者检测到有新版时，在手机/PC 弹一句"有更新，点一下升级"。
2. **版本号（SemVer）+ 更新日志**：像 `0.3.1` 这样给每版编号，配一份 CHANGELOG。`/health` 接口和手机界面都显示当前版本。
   > 💬 **人话**：给每个版本编个号，出问题能说清"我用的是哪一版"，也能一眼看出 PC 和服务器是不是同一版、对不对得上。
3. **数据库迁移（单机软件最容易翻车、也最关键的一点）**：你的会话、历史都存在 SQLite 里。升级时表结构可能要变（比如多一个字段）。如果不管不顾直接覆盖，老数据就废了。所以给数据库记一个"结构版本号"，升级时**自动跑迁移脚本**，把老数据平滑升上来，一条不丢。
   > 💬 **人话**：就像手机 App 更新后，你以前的聊天记录还在——靠的就是"数据迁移"。Foreman 也要做这个，升级才不会清空你的历史。
4. **配置向后兼容**：`config.yaml` 以后加新选项时都带默认值，你的旧配置文件**不用改**也能继续跑。
5. **PC ↔ 服务器 通信带版本号**：两端对话的消息里带个 `version`。版本对不上时给一句人话提示（"PC 端太旧，请先升级"），而不是甩你一脸看不懂的报错。

### 11.2 可扩展性（能加新流程 / 新标准，但不动核心代码）

可扩展性分两层，先说你最看重的那一层：

- **第一层（核心，你的秘方）**：**工作流、技能、代码规范、QA 标准**。这些是让 Foreman 真正好用的"内容"，存在**数据库**里，在**界面上改**，**完全不碰代码**。
- **第二层（偶尔才用）**：接一种新 AI、加一个新通知渠道。这层要写一点代码，但用统一接口隔离，核心不动。

#### A. 引擎和"秘方"分家（这套叫 open-core / 开放内核）

一句话：**代码开源，秘方私有。**

> 🍜 **人话比方**：开源出去的是**厨房和厨师**（引擎）。你的工作流 / 规范 / 标准 / 技能，是你的**祖传菜谱**。厨房给别人看没关系——因为菜谱锁在你自己的数据库里，根本不在代码里。

开源的引擎只干一件事：**"照着你库里存的剧本去干活、去验收"**。它本身不含任何具体规矩。

#### B. 四种"积木"，都存在数据库里

| 积木 | 大白话 | 里面存什么 |
|------|--------|-----------|
| **工作流 Workflow** | 干一件活的**流程 / 剧本**（主线） | 有哪些步骤、每步用哪些积木、哪一步要卡审批 |
| **技能 Skill** | 某类活的**做法手册** | 一段"这种活怎么做好"的说明，临时喂给 AI 看 |
| **代码规范 Code Standard** | 你家的**规矩** | 命名、结构、禁用项、必须遵守的写法 |
| **QA 标准** | **验收清单** | 怎么算过关、不过关扣什么分 |

**混合式工作流怎么跑（你选的方案）**——一句话：**骨架是死的，每步的干法是活的。**

1. 工作流是**固定的步骤骨架**（第1步→第2步→…，该卡审批的地方卡住）——这部分**可控、可复现**。
2. 每一步**"具体怎么做"**，引擎临时把对应的**技能 + 代码规范**喂给 AI 去发挥——这部分**灵活**。
3. 每一步干完，引擎拿这步的 **QA 标准** 让 AI 审一遍，过了才走下一步。

> 举个例子，一个"加新功能"工作流可能长这样：
> 1. 先写测试（技能"怎么写测试" + 规范"测试命名"）→ QA：是否覆盖主路径
> 2. 再写实现（技能"实现要点" + 你的代码规范）→ QA：是否合规范、有没有偷工
> 3. 自审 + 跑 lint → QA：lint 全过
> 4. push 前 → **审批闸**：推你手机问一句"准不准"

#### C. 怎么存、怎么改、怎么保证开源也不泄露

- **存数据库**：一张通用的 `definitions` 表装下全部四种（用 `kind` 字段区分），带版本号，可启用 / 停用 / 回滚；再加一张关系表，把"某工作流的某一步 → 用哪些积木"连起来。以后想加**第五种**（比如"测试策略"），加个 `kind` 就行，**不用改表结构**。
- **在手机 / 网页 UI 里改（你选的）**：界面上增删改这些积木，直接存进库，**不碰文件、不用重新部署**。数据库就是"唯一真相源"。
- **开源也不泄露**：`foreman.db` 不进 git；仓库里只放几个**脱敏示例**让别人能上手；你的真东西只躺在你自己的库里。（可选：把 `body` 加密落库，连 .db 文件被偷也只是一堆密文。）

#### D. 规矩怎么真正"管住"干活的 AI（你选的：注入 + 审阅，双保险）

- **事前注入**：启动 claude/codex 前，引擎把这一步要用的**技能 + 代码规范**写进工作区（比如生成 `CLAUDE.md` / `AGENTS.md` / skill 文件，或追加进系统提示），让 AI 一上来就照着做。
- **事后审阅**：这一步干完，Reviewer 再拿 **QA 标准**查一遍。
- 合起来就是 **"前面教、后面考"**，两道都过才算数。

#### E. 第二层：偶尔才用的代码级扩展

- **接新 AI**：照着 `AgentAdapter` 接口写个"转接头"（Gemini CLI / Aider / 自研都行），核心不动。
- **接新通知渠道**：定义 `Notifier` 接口（就一个动作：`发送(标题, 正文, 按钮)`），飞书 / Telegram / Bark / 邮件各写一个实现。
- **留插件口子**：Python entry points，以后第三方能发 `foreman-plugin-xxx` 包，启动自动加载。

### 11.3 一句话总结

- **能更新** 靠：自动部署 + 版本号 + **数据库迁移** + 向后兼容。
- **能扩展** 靠：**把工作流 / 技能 / 规范 / QA 标准做成数据库里的"积木"，在界面上改、引擎照着跑**。开源的是引擎，你的秘方锁在库里不外泄。偶尔的代码级扩展（新 AI / 新通知）用统一接口隔离，**核心永远不动**。

---

## 12. 分阶段路线图

> 详见 [ROADMAP.md](ROADMAP.md)。摘要：

- **P0 脚手架**：配置、LLM client、SQLite、入口命令。
- **P1 单机驱动**：Agent Runner（headless claude/codex）+ 事件捕获 + 本地 Web 仪表盘。
- **P2 观测 + 审阅 + 检查点**：Hook 接入、Git/Process 观测、Reviewer（先用配置里的 QA 标准）、每步 git 检查点。
- **P3 手机面 + 审批**：PWA + Web Push + Gate 审批闭环 + 远程访问。
- **P4 决策回路 + 两向控制（⭐ 核心交互，见 §6）**：Operator 提案 → Auditor 审核 → 决策卡（PC/手机点）→ 一键回退；手机下发任务、多会话、简报。自治档位默认"凡事都问"。
- **P5 定义引擎（⭐ 秘方层，核心价值）**：`definitions` 表 + 混合式工作流引擎 + 事前注入工作区 + QA 标准驱动审阅 + 数据库迁移。
- **P6 UI 编辑器 + 扩展口**：手机/网页里增删改工作流/技能/规范/QA；`Notifier` 接口、插件 entry points。
- **P7 团队 / 中继模式**：服务器当总机 + 管理员控制台（建用户/邀请）+ access key（一机一张）+ 本地进程出站长连 + 多租户隔离 + 展示缓存（秘方不上服务器）。见 §8。

> 💬 P5 是 Foreman 真正值钱的部分，但它得先踩在 P1（能驱动）、P2（能审阅）、P3（有界面）的肩膀上，所以排在中后段。
> 数据库的"积木"表和注入机制可以在 P2 起就小步先行。

每个阶段都应是「可独立运行、可演示」的纵向切片。
> 💬 **纵向切片**：每个阶段都做成"从头到尾能跑通、能演示"的一条窄线，而不是先把地基全铺完再说。

---

## 13. 风险与开放问题

1. **CLI 输出格式漂移**：claude/codex 升级可能改输出 → 适配器要容错、版本探测。
2. **PTY 在 Windows 的坑**：编码/换行/中断信号 → MVP 尽量 headless 规避。
3. **「卡住」的判定**：纯启发式易误判 → 结合 hook 的 `Notification` 信号更可靠。
4. **Web Push 在 iOS**：需 iOS 16.4+ 且必须「添加到主屏幕」后才支持 → 文档需提示。
5. **审批延迟**：你不在线时会话挂起多久？需要超时策略（保守等待 vs 自动安全回退）。
6. **Token 取舍原则**：当前把**文本类 LLM 调用当作几乎免费**，所以**质量优先**——敢多审几遍、审得细、技能写详细点（毕竟是你的 know-how 在把关）。但**绝不批量浪费多模态/重复劳动**：不无脑 OCR / 识图、不把没变的文件反复重发、能缓存的结果就缓存。一句话：**文本随便用，识图省着用。**
7. **安全暴露面**：远程可下发任务 = 高权限入口 → 鉴权 + 网关 + 工作区白名单缺一不可。

---

## 14. 目录结构

```
agent-foreman/
├── README.md / README.zh-CN.md
├── pyproject.toml / config.example.yaml / .env.example
├── docs/      DESIGN.zh-CN.md（本文）/ ARCHITECTURE.md / ROADMAP.md / SECURITY.md
├── src/foreman/
│   ├── __main__.py          # foreman serve / dispatch
│   ├── config.py            # 配置加载
│   ├── core/                # brain / reviewer / gate / scheduler / events
│   ├── agents/              # base 协议 / claude_code / codex / runner
│   ├── monitor/             # hooks / git_watch / process
│   ├── llm/                 # client（你的 API）
│   ├── store/               # models / db
│   └── server/              # app(FastAPI) / push(WebPush) / auth
├── web/        index.html / manifest.webmanifest / sw.js / app.js   # PWA
├── hooks/      claude-hooks.example.json
└── scripts/    gen_vapid.py
```

---

*本设计为 v0.1，欢迎在实现中迭代。下一步：搭脚手架并打通 P1（单机驱动 + 本地仪表盘）。*
