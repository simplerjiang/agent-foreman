# Foreman 设计方案（v0.1）

> 一个常驻在你 PC 上的「项目经理 / 工头（PM / Foreman）」Agent：
> 它替你**监控**本地的编码 Agent（Claude Code / Codex CLI）、**调度**它们干活、
> **审阅**它们的产出，并通过**手机 PWA + Web Push** 向你汇报、在风险点向你请求审批，
> 还能让你在手机上**下发新任务**。

本文是设计文档（先于实现），目标是把架构、组件边界、数据模型、关键流程、安全模型和分阶段路线图讲清楚，
让后续实现有据可依。

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
- 不做多用户 / 团队协作（单用户、单人鉴权）。
- 不做应用商店上架（用 PWA「添加到主屏幕」即可）。
- 不做复杂的多机器编排（先把单机做扎实，接口预留）。

---

## 2. 核心概念

| 概念 | 说明 |
|------|------|
| **PM Brain（工头大脑）** | 用你的 LLM API 驱动的决策循环：判断 Agent 状态、决定何时审阅 / 升级 / 简报。 |
| **Root Session（主会话）** | 一个目标的长生命周期容器：包含目标、计划、若干 Task、状态、工作区路径。 |
| **Task（任务）** | 主会话下的一次具体执行，绑定某个 Agent（claude / codex），有指令与状态。 |
| **Agent Adapter（适配器）** | 把不同 CLI（claude / codex）抽象成统一接口，PM 通过它启动/喂指令/读输出。 |
| **Gate（网关 / 审批闸）** | 把动作分级（安全 / 需策略 / 需审批），危险动作在此暂停并请求人工批准。 |
| **Review（审阅）** | 在检查点把 diff + 上下文交给 LLM 评审，产出「通过 / 要改 / 升级人工」结论。 |
| **Briefing（简报）** | PM 生成的人类可读摘要，定时或「你回来时」推送到手机。 |
| **Control Surface（控制面）** | 手机 PWA：看时间线、批审批、下发任务。 |

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

---

## 4. 组件详解

### 4.1 PM Core

#### PM Brain（决策循环）
- 输入：事件流（Agent 输出、hook 事件、git 变化、定时器）。
- 职责：判断每个会话当前处于 `running / idle / blocked / waiting-approval / done / failed`；
  决定下一步动作（继续观察 / 触发审阅 / 升级为审批 / 生成简报 / 重新下发指令）。
- 实现：状态机 + LLM 辅助判断。**确定性的事**（进程死没死、有没有 diff）用代码判断，
  **需要语义判断的事**（输出像不像卡住了、要不要升级）才调 LLM，省 token 也更稳。

#### Reviewer（自动审阅）
- 触发：Agent 到达检查点（Claude Code `Stop` hook、任务声明完成、产生一批 diff）。
- 输入：`git diff` + 任务目标 + 相关文件片段。
- 输出：结构化评审 `{verdict, summary, risks[], suggestions[], needs_human}`。
- `verdict ∈ {approve, request_changes, escalate}`。
- `escalate` 或命中危险动作 → 交给 Gate 生成审批卡片。

#### Gate（权限 / 审批闸）
- 动作分级（借鉴 Cteno）：
  - **安全（safe）**：读文件、在已批准工作区改代码、跑测试 → 直接放行。
  - **需策略（needs-strategy）**：大规模重构、加依赖、改协议 → 需要先有计划/说明。
  - **需审批（requires-approval）**：`git push`、部署、改 secrets、删除/破坏性操作 → **暂停**，推审批卡到手机。
- 与 Claude Code 的 `PreToolUse` hook 联动：危险工具调用先被拦截，待批。

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
- 配置 `base_url` + `api_key` + `model`，由你提供。
- 用于 **PM Brain / Reviewer / Briefing**。
- 注意区分：**Foreman 的「大脑」用你的 API；被驱动的 claude/codex CLI 用它们各自的登录/额度**。
  两者解耦，互不影响。

### 4.5 Store（SQLite）

本地优先，单文件 SQLite（`foreman.db`），用 SQLModel/SQLAlchemy。见 §7 数据模型。

### 4.6 Web Backend + PWA + Web Push

- **后端**：FastAPI（`uvicorn`），提供：
  - REST：会话/任务/事件/审批/简报 的增删查。
  - WebSocket：把 Event Bus 实时推到打开着的 PWA。
  - Web Push：`pywebpush` + VAPID，应用关闭也能推（审批、简报）。
  - Auth：单用户 Bearer Token（见 §8）。
- **前端 PWA**：轻量（原生 JS 或 Preact + Vite），
  `manifest.webmanifest` + `service worker`（`sw.js`）实现可安装 + 推送接收。
  页面：仪表盘（活跃会话）、时间线、审批卡片、任务下发框、简报列表。

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

## 6. 权限与审批模型

| 级别 | 示例动作 | 默认处理 |
|------|----------|----------|
| safe | 读文件、改已批准工作区代码、跑测试、本地构建 | 自动放行，仅记录 |
| needs-strategy | 大重构、加/升依赖、改公共接口/协议、改 CI | 需先有计划说明；可配置为自动或转审批 |
| requires-approval | `git push`、发布/部署、改 secrets/凭据、`rm -rf`/删表等破坏性操作 | **暂停**并推手机审批 |

- 策略以 YAML 配置（`config.yaml` 的 `gates:`），支持按工作区/会话覆盖。
- 与 Claude Code `PreToolUse` hook 联动：匹配到危险命令/工具 → 返回「阻止」并触发审批。
- **白名单工作区**：只有显式批准的目录才允许写。

---

## 7. 数据模型（SQLite）

```
sessions(id, goal, plan, status, workspace, agent_type, created_at, updated_at)
tasks(id, session_id→sessions, instruction, status, agent_handle, created_at, updated_at)
events(id, session_id, task_id, type, source, payload_json, ts)
reviews(id, task_id, verdict, summary, risks_json, suggestions_json, needs_human, ts)
approvals(id, session_id, task_id, action, risk_level, diff_summary, status, reason, requested_at, decided_at)
reports(id, session_id, kind, title, body_md, sent, ts)          # kind: handoff|active-briefing|daily
push_subscriptions(id, endpoint, p256dh, auth, ua, created_at)   # WebPush 订阅
config_kv(key, value)                                            # 运行期可变配置
```

- `status(session) ∈ {planning, running, idle, blocked, waiting_approval, done, failed, paused}`
- `events.type` 枚举：`agent_output | tool_pre | tool_post | stop | git_diff | git_commit | review | approval_req | approval_decided | briefing | error | dispatch`
- 所有时间用 UTC ISO8601。

---

## 8. 远程访问与安全

手机要从外网访问 PC 上的 Web Backend，且 Web Push 必须 **HTTPS**。推荐方案（任选其一）：

| 方案 | 优点 | 说明 |
|------|------|------|
| **Tailscale**（推荐） | 零配置、点对点加密、`tailscale serve` 直出 HTTPS | 在中国可用性较好，私有网络不暴露公网 |
| **Cloudflare Tunnel** | 免费公网 HTTPS 域名、无需开端口 | 需要一个域名 |
| **frp** 自建 | 完全自控 | 需要一台有公网 IP 的服务器 |

安全要点：
- **单用户 Bearer Token**：首次启动生成强随机 token，手机端保存；所有 API/WS 校验。
- **设备配对**：手机首次连接需在 PC 端确认（显示配对码）。
- **最小暴露**：默认只监听 `127.0.0.1`，对外仅经隧道暴露；不直接开公网端口。
- **审批动作签名**：审批请求带一次性 nonce，防重放。
- **工作区白名单 + 危险命令网关**：纵深防御，即使被越权也卡在 Gate。
- **Secrets**：你的 LL​M API key、VAPID 私钥放 `.env`（不入库、不进 git）。

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

- `PreToolUse`：危险工具调用前 → POST，必要时返回阻止（触发审批）。
- `PostToolUse`：工具调用后 → POST（观测做了什么）。
- `Stop` / `SubagentStop`：一段工作结束 → 触发 Reviewer。
- `Notification`：Agent 主动通知（如等待输入）→ 可直接推手机。

示例配置见 `hooks/claude-hooks.example.json`（用 `curl` POST 到 `http://127.0.0.1:8787/hooks`）。
Codex 侧暂以输出解析 + git 观测为主，后续探索 MCP/wrapper 等价信号。

---

## 11. 分阶段路线图

> 详见 [ROADMAP.md](ROADMAP.md)。摘要：

- **P0 脚手架**：配置、LLM client、SQLite、入口命令。
- **P1 单机驱动**：Agent Runner（headless claude/codex）+ 事件捕获 + 本地 Web 仪表盘。
- **P2 观测 + 审阅**：Hook 接入、Git/Process 观测、Reviewer。
- **P3 手机面 + 审批**：PWA + Web Push + Gate 审批闭环 + 远程访问（Tailscale）。
- **P4 两向控制**：手机下发任务、多会话、简报/调度。
- **P5 增强**：多机器、MCP、更多 Agent、策略学习。

每个阶段都应是「可独立运行、可演示」的纵向切片。

---

## 12. 风险与开放问题

1. **CLI 输出格式漂移**：claude/codex 升级可能改输出 → 适配器要容错、版本探测。
2. **PTY 在 Windows 的坑**：编码/换行/中断信号 → MVP 尽量 headless 规避。
3. **「卡住」的判定**：纯启发式易误判 → 结合 hook 的 `Notification` 信号更可靠。
4. **Web Push 在 iOS**：需 iOS 16.4+ 且必须「添加到主屏幕」后才支持 → 文档需提示。
5. **审批延迟**：你不在线时会话挂起多久？需要超时策略（保守等待 vs 自动安全回退）。
6. **Token 成本**：PM Brain/Reviewer 频繁调 LLM → 用确定性判断兜底、批量审阅、可配模型档位。
7. **安全暴露面**：远程可下发任务 = 高权限入口 → 鉴权 + 网关 + 工作区白名单缺一不可。

---

## 13. 目录结构

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
