# 远端控制下行链路设计书（云端 → 本机 命令/审批）

日期：2026-06-25

分支：`claude/musing-maxwell-559ccc`

基线：`origin/main` at `69acca5`

关联 Issue：[#77](https://github.com/simplerjiang/agent-foreman/issues/77)（🔴 Blocker — 远端无法控制本地做开发：relay 只有上行镜像，云端→本地命令/审批下行链路未实现）

定位：完成 **P4「Decision loop + two-way control」** 的"两向"那一半，承载在 **P7「Team / relay 总机」** 之上（见 `DESIGN.zh-CN.md` §6 / §8.5）。

状态：**设计已定稿，待实现**。三个范围决定 + 投递可靠级别均已拍板（见 §7）。

> 💬 人话：现在手机/云端只能"看"本机在干嘛（上行镜像），不能"指挥"本机干活（下行控制）。这份文档讲怎么把"指挥"这条线接通，并且接得**高效、可靠、安全**。

---

## 0. 这份文档解决什么

用户诉求：在 `foreman.kongsites.com` 远端**直接控制本机做开发**——派发任务、在手机上点审批，让本机的 claude-code / codex 真去干活，并把进度回传给远端看。

E2E 专项测试（2026-06-25）证实：**上行通、下行断**。本文给出把下行接通的完整设计，覆盖三个维度：

1. **接线**（功能）：云端下发命令 → 本机真执行 → 进度回传。
2. **可靠**（投递语义）：做到**有效恰好一次**，杜绝"假成功 / 丢命令 / 双执行"。
3. **安全**（门禁）：远端能在本机跑代码是全系统最高危面，必须默认关 + 多重护栏。

---

## 1. 现状与差距（已核实，从简）

对照 `69acca5` 源码：

**上行已通（真机验证）**：本机 app 拨 `wss://foreman.kongsites.com/relay`，TLS + WS 升级 + hello 握手 + 远端 relay 鉴权整条全通（无效 key 被正确拒为 `auth`）。relay 当前承载：本地→云端 **显示缓存**（session/card 摘要，绝不含 diff/秘方）+ 心跳 ping/pong。

**下行没接（四处代码事实）**：

| # | 位置 | 现状 |
|---|---|---|
| 1 | `server/app.py` | 全文搜 `relay.route(` / `KIND_COMMAND` **0 命中** —— 没有任何 HTTP endpoint 会把命令帧推给本机。 |
| 2 | `server/relay.py:195-217` `_on_frame` | 只处理 `KIND_HEARTBEAT` + `KIND_CACHE_SYNC`；注释自述命令转发 "layered on in P4"。 |
| 3 | `client/core/cloud.py:135-145` | 构造 `RelayConnector` **不传 `on_frame`** → 入站命令帧在 `client/relay.py:175` 被静默丢弃。 |
| 4 | — | 没有把 `KIND_COMMAND` 翻译成本地开发任务的执行器。 |

差距一句话：**原料全有，但全没接线。** 关键原语都已建好、已测试，只是没人调用：

- `Relay.route(account_id, env, process_id=)`（`server/relay.py:138`）—— 下行路由，建好但**无人调用**。
- `RelayConnector.on_frame`（`client/relay.py:175`）—— 入站回调，**支持但没传**。
- `DispatchService.create()`（`client/core/dispatch_service.py:106`）、`cards.record_choice()`（`cards.py`）、`gate.resolve()`（`gate.py:204`）—— 本地 UI 走的同一批执行 coroutine。

> 💬 人话：水管、阀门、水龙头都装好了，就是没把它们拧到一起。我们要做的不是造新管子，而是接线。

**核心打法：把下行接到这几个既有原语上，绝不另起一套命令总线。** 这样远端路径与本地路径逐字节一致（同事件、同 nonce/防重放、同执行器），杜绝两条路径漂移。

---

## 2. 核心架构：反向隧道 + 三段式下行

### 2.1 传输模型：反向隧道（已就位）

本机在防火墙/NAT 后，连接永远**向外拨**：本机 dial `wss://<域名>/relay` → 送 access key → relay 鉴权后标记在线，长连接保持。PWA 的请求由 relay **按账号路由**到对应本机。

> 💬 人话：这就是 ngrok / Cloudflare Tunnel / VS Code 隧道的同款做法——内网机器主动打洞出去，外面通过"总机"找到它。穿 NAT/防火墙天然没问题。

### 2.2 下行数据路径（去程）

```
PWA(手机)                总机 Relay(云端)            本机进程                 本地引擎
   │ POST /api/dispatch     │                          │                       │
   │ (Bearer token) ───────▶│ require_account→account.id│                       │
   │                        │ Envelope(KIND_COMMAND,    │                       │
   │                        │   id=corr, payload={...}) │                       │
   │                        │ relay.route(account.id, ──┼─ wss 推帧 ───────────▶│ on_frame(env)
   │                        │   env, process_id) │      │                       │ ├ 校验/门禁
   │                        │                          │                       │ ├ run_coroutine_
   │                        │                          │                       │ │  threadsafe→app loop
   │                        │                          │                       │ └ DispatchService
   │                        │                          │                       │     .create() 执行
   │◀── 200 {已收到/开跑} ──│◀── KIND_ACK(id=corr) ─────┼─ wss 回帧 ◀───────────│
```

关键点：

1. **入口两个端点**：`POST /api/dispatch`（派发任务）、`POST /api/approve`（审批卡）。各自 `require_account(request)` → `account.id`，build `Envelope(kind=KIND_COMMAND)`，调 `relay.route(account.id, env, process_id=...)`。
2. **跨租户硬隔离**：`account.id` 永远取自 **token**（`require_account`），**绝不取自请求体**；`relay.clients_for` 先按 account 过滤再按 process_id，A 账号的命令到不了 B 的机器。
3. **强制带 `process_id`**：`process_id=None` 会群发到该账号**所有**在线机器 → 各回一个同 `env.id` 的 ACK，关联歧义 + 重复建会话。故 dispatch/approve **强制指定目标机器**。
4. **本机执行，relay 只转发**：团队 relay 盒子只注入 `bus/relay/auth/cache`，**没有** gate/cards/dispatcher，也不持有秘方（§8.3）——所以执行**必须**发生在本机进程，relay 只搬运意图。
5. **命令永远只是"提案"**：`KIND_COMMAND` 喂进既有 `DispatchService → Gate → autonomy` 管道，**绝不直接 shell/eval**，红线与白名单原样生效（详见 §4）。

### 2.3 进度回传（回程）—— 验收 #4

本机执行进度经 relay **上送** PWA，让远端看见执行结果：

- 客户端 `RelayConnector` 加 `outbox` 队列 + `_send_loop`（仿现有 `_sync_loop`），把本机 `AgentEvent` 映射成 **display-safe** 的 `KIND_EVENT`（复用 `cache_sync.session_summary/card_summary`，**绝不推 diff/秘方**，§8.3 边界）。
- 服务端 `relay._on_frame` 加 `KIND_EVENT/KIND_ACK` 分支，按 `client.account_id` republish 到 PWA bus（绝不用帧里的 account_id）。
- **跨事件循环显式过桥**：`EventBus.publish` 在 uvicorn loop 上 `await q.put`，而 CloudManager 消费在另一后台 loop——光"在对的 loop 订阅"不够，会 "Future attached to a different loop" 崩。必须 `call_soon_threadsafe` 或线程安全队列过桥。
- **延迟**：命令走**已经开着的 wss**（不新建连接），relay→本机一次推送通常 sub-100ms；进度回程目标 ~1s（需把 republish 接到 PWA 的 `/ws`，而非只靠 8s 轮询）。

---

## 3. 投递可靠性：从 best-effort 到「有效恰好一次」

朴素实现（`route()` fire-and-forget）对"看进度、点审批"够用，但对"远端发一条开发任务、必须恰好执行一次"**不够**。核心问题一句话：

> **`relay.route()` 返回的是"发给了几条看起来活着的连接"，不是"对端真的收到并执行了"。**

### 3.1 四个「假成功 / 丢命令」失效点

| # | 失效点 | 后果 |
|---|---|---|
| ① | `route()≥1` 只证明"已入发送缓冲"，不等于本机已收到 | 半开连接（NAT 重绑 / 笔记本休眠）让 HTTP 200 **假成功**，命令静默蒸发。 |
| ② | 离线 `route()==0` → 409，命令直接丢、不排队、不重发 | 恰逢重连退避窗口派发即丢失，需手动重试。 |
| ③ | ACK/事件回程丢失 → PWA 重发 | 撞上 `nonce/seq` 防重放：要么被当重放丢弃，要么（无幂等）**双执行**。 |
| ④ | 心跳 30s 间隔 → registry `online` 标志最长 ~30–60s 陈旧 | 连接刚死时 `route()` 仍以为机器在线。 |

> 💬 人话：①"我喊了一嗓子"不等于"对方听见了"；②对方不在时这嗓子就白喊了；③没听见回声就重喊，可能喊重了把事干两遍；④对方其实已经走了，名单上还显示"在线"。

### 3.2 决定：一步到位做「有效恰好一次」

（= 至少一次投递 + 幂等去重）。补三件事，都是小而确定的增量：

1. **端到端 ACK + 关联**：`Relay` 加一个**按账号、有界、带超时**的 pending-futures map，键 `env.id`，本机回 `KIND_ACK` 时在 `relay._on_frame` resolve；`POST /api/dispatch` **await 这个 future（带超时）**，返回的是"本机真的收到/开跑"，不再是"发出去了"。→ 修 ①。
2. **重连窗口短排队（store-and-forward）**：`route()==0` 不直接 409，而是按账号**秒级 TTL 排队**（有界 + TTL + 按账号）；PC 一 `register()` 重连就 flush 投递；超 TTL 才降级 409 + cache 回退。→ 修 ②。
3. **幂等键 + 结果缓存**：客户端维护 `env.id → 缓存结果`；收到 `KIND_COMMAND` 若该 id 已处理过，**回放缓存 ACK**，既不重跑也不被当重放丢。→ 修 ③。
4. **附带**：心跳 30s→~10s（或开 TCP keepalive），把"假在线"窗口压到几秒。→ 缩小 ④。

### 3.3 必须写死的规则：幂等 ↔ 防重放 如何不打架

这是最容易出错的细节，单列：

- **合法重试**：同一 `env.id` 重发时，服务端**换发新的 `nonce/seq`**（传输令牌）→ 过防重放；但 `env.id` 不变 → 命中客户端幂等缓存 → 回放结果，**不双执行**。
- **攻击者重放**：截获的旧帧 `nonce/seq` 已陈旧 → 在防重放层（§4，叠加强制 MAC）**先被丢**，根本到不了幂等判断。

> 一句话：**`env.id` 管「是不是同一条逻辑命令」，`nonce/seq/MAC` 管「这一次传输是不是新鲜且未被伪造」**，两层各司其职。

---

## 4. 安全硬门禁（验收 #5）

远端命令现在能在用户机器上跑代码——这是全系统最高危面。门禁是**硬门**：远端执行默认 **OFF**，直到本节全部落地且用户显式开启 + 配好白名单。

### 4.1 七道护栏

1. **总开关默认 OFF**：`cfg.server.remote_execution_enabled`（默认 `False`）+ settings.json 开关。关 → `cloud.py` `_build_connector` 传 `on_frame=None`，命令帧像今天一样被丢（零行为变化）。这是主断路器。
2. **工作区白名单前置**：`cfg.workspaces`（**顶层**，非 ServerCfg）非空才允许开启；且开启时强制 `allow_unlisted_workspaces_for_dev=False`，否则白名单被开发后门绕过。远端 dispatch 走 `DispatchService._resolve_workspace`（fail-closed），绝不接受未经检查的 cwd。
3. **远端危险操作回手机卡**：`source=='phone'` 时 disposition **钳到 card-minimum**——一切都不 auto，每个远端动作都冒确认卡（复用 `Gate.request_approval` + Web Push 的一次性 nonce），即便本地 autonomy 是 level 2/3。**钳制点在 loop 的 disposition 路径按 session source 强制**，不在 `on_frame` 手挥。远端 `approve` 只能**解决既有 pending** 项，不可触发新 shell。
4. **防伪 + 防重放**：服务端给命令帧盖 `nonce + seq + ts`，客户端按连接跟踪 `last-seq` 丢陈旧/重复帧（防重放）；**per-key MAC（`hash_access_key` 派生、恒时校验）对 `KIND_COMMAND` 强制启用**（防伪造，含被攻破的 relay 运营方注帧）。两者都是必选——`nonce/seq` 只防重放，MAC 才防伪造。
5. **限流**：每条入站 `KIND_COMMAND` 过 `SlidingWindowLimiter`（`shared/ratelimit.py`），按 account/process 限流；超限丢 + 审计。
6. **跨账号绑定**：PWA 调用方 token 的账号必须等于路由目标账号；relay 始终以 access-key 派生的 account_id 路由，绝不信帧里的 account_id。
7. **强制审计**：每条远端命令 **persist-then-act** 落审计（account_id / process_id / action / 解析后 workspace / Gate 分类 / disposition / approval_id / 结果），在 timeline 可见——远端触发的执行必须可追溯。

### 4.2 威胁清单 → 缓解

| 威胁 | 缓解 |
|---|---|
| **伪造帧**（被攻破的 relay 注帧） | 总开关默认关 + **强制 per-key MAC** + 命令只能变成 `create()` 提案、仍过 Gate |
| **重放**（重发旧命令帧） | `nonce + seq + ts`，客户端丢陈旧/重复；幂等缓存放行合法重试、防重放挡伪造重放（§3.3） |
| **越权/路径逃逸**（指定白名单外路径） | 走 fail-closed `_resolve_workspace` + PathGuard；开启远端执行前强制配白名单 |
| **跨账号劫持**（A 的命令到 B 的机器） | account_id 由 key 哈希派生、`clients_for` 先按账号过滤；绝不信帧里的 account_id |
| **危险操作无人值守自动跑**（git push / rm -rf / deploy） | 远端一律 card-minimum，回手机审批卡（一次性 nonce）；不可逆永不 auto（autonomy 红线） |
| **秘密外泄**（让 agent 读 .env 回传） | 秘密不放进任何白名单 workspace 根（在旁邻 .env）；事件上行只推 display-safe 摘要；relay 不持 key |
| **洪泛滥用**（刷命令耗尽机器 / 刷审批卡） | 按 account/process 限流（fail-closed） |
| **审计盲区**（执行无可追溯记录） | 强制 persist-then-act 审计，timeline 可见 |

> 💬 人话：好消息是——确定性 Gate、fail-closed 工作区、nonce 审批、限流器**全都已经有**了。安全这块的工作主要是把下行**穿过**这些既有护栏，再补两样帧级的新东西（防重放 + 防伪 MAC），并把整套关在默认关的开关后面。

---

## 5. 分阶段落地

| 阶段 | 目标 | 关键触点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|
| **P4.0 协议地基** | Envelope 加关联 id + `nonce/seq/ts`；文档化 command/ack 载荷约定；`PROTOCOL_VERSION→2`。`env.id` 定为幂等键、与传输令牌分离。无行为变化。 | `shared/protocol.py`、`tests/test_relay.py` | id/nonce/seq round-trip；旧 v1 帧仍宽松解析 | — | S |
| **P4.1 服务端入口 + 可靠层** | `POST /api/dispatch` + `/api/approve` 调 `relay.route()`（强制带 process_id，503 personal / 409 offline）；relay 加 pending-futures map（端到端 ACK）+ 重连窗口短队列 | `server/app.py`、`server/relay.py`、`tests/test_remote_command.py` | 端点调到 route()；account 取自 token；离线先短排队再 409；ACK 关联返回真实"已收到" | P4.0 | M+ |
| **P4.2 客户端接收端 + 默认关 + 审计** | 注入 dispatcher/gate/cards；`on_frame` 接线；命令→`create()/record_choice()/resolve()` 经 `run_coroutine_threadsafe` 投到 app loop；幂等缓存；回 `KIND_ACK`。**从第一天关在默认关开关后 + 落审计** | `client/core/cloud.py`、`client/local_app.py`、`client/relay.py`、`shared/config.py` | dispatch 帧建会话（同 `/api/tasks`）；approve 帧解既有卡；幂等不双执行；畸形帧不掉连接 | P4.1 | L |
| **P4.3 上行实时进度** | `outbox` + `_send_loop`；CloudManager 订 EventBus（跨 loop 用 `call_soon_threadsafe` 过桥）；server republish 按账号；只推 display-safe；mid-task 掉线回退 cache + "机器离线"标记 | `client/relay.py`、`cloud.py`、`cache_sync.py`、`server/relay.py`、`server/app.py` | 远端 ~1s 见进度；republish 按账号隔离；重连不漏订阅 | P4.2 | L |
| **P4.4 安全硬门禁** | 总开关 + 白名单前置 + 远端 card-minimum + `nonce/seq/ts` 防重放 + **强制 MAC** + 限流 + card 幂等 + 审计 | `config.py`、`cloud.py`、`gate.py`、`autonomy.py`、`relay.py`、`protocol.py`、`ratelimit.py`、`dispatch_service.py` | 默认关丢命令；空白名单拒开启；level 2/3 远端仍全冒卡；陈旧/重放/跨账号/伪造帧被拒；先审计后执行 | P4.2 | L |
| **P4.5 PWA 远程控制 UI** | `loadProcesses` + 轮询；Composer 加目标机器 select→`/api/dispatch`（带 process_id）；复用现有审批按钮→`/api/approve` 带 nonce+process_id；i18n + friendlyError 补 `machine_offline/relay_unavailable`；personal mode 隐藏 | `server/web/app.js`、`app.css` | 列出在线机器并可派发到指定机；远端 approve 回路到来源机；新错误码翻译显示 | P4.1, P4.4 | M |
| **P4.6 真机 E2E 复验** | foreman-e2e 对 `foreman.kongsites.com` 全链路实测，截图作为 #77 close 证据 | foreman-e2e skill | 云端派发→本地真执行+进度回传；危险操作回手机卡→审批续跑（Card 路径）；离线→409/cache | P4.3, P4.4, P4.5 | M |

**关键防呆**（评审已核实并折叠进各阶段）：

- **app_loop 经启动钩子绑定，非构造期注入**——`CloudManager` 在 `local_app.py:186` 构造时 uvicorn loop 尚不存在（线程在 `:204-205` 才启动）。改为 FastAPI `lifespan`/`startup` 钩子里 `asyncio.get_running_loop()` → `cloud.bind_app_loop(loop)`；绑定前 `on_frame` fail-closed（ack `not_ready`）。
- **跨事件循环**用 `call_soon_threadsafe` 显式过桥（§2.3）。
- **dispatch/approve 强制带 process_id**，避免多机扇出 ACK 歧义。
- **P4.2 起即默认关 + 落审计**，确保 P4.4 完整护栏落地前不存在"裸奔的可执行 handler"。

---

## 6. 验收标准映射（#77 五条 close 条件）

| #77 验收点 | 关闭阶段 |
|---|---|
| #1 远端派发 + 本机真执行 | P4.1 + P4.2（UI：P4.5）；审批续跑由 **Card 路径**证明（见 §7 决定①） |
| #2 服务端端点调 `relay.route()` | P4.1 |
| #3 客户端 `on_frame` 接线、命令进 dispatch | P4.2 |
| #4 执行进度经 relay 回传可见 | P4.3 |
| #5 危险操作确认 / 工作区护栏 | P4.2（默认关）+ P4.4（完整护栏） |

---

## 7. 已拍板决定 & 暂缓项

**已拍板（2026-06-25）**：

1. **Gate-审批 resume 移出 #77** → 另开 follow-up。`gate.resolve()` 当前只记录决定、`execution_deferred=True` **不让 agent 续跑**（`gate.py:216/253`，预存 P4 两向控制缺口，影响本地也影响远端）。#77 验收 #1 的"审批后续跑"由 **Card 路径**（`record_choice → loop.on_card_decision` 会 checkpoint+执行）证明；Approval-resume（接 `Runner.send/interrupt`）是净新增工作，单列。
2. **被攻破 relay 伪造命令帧在威胁范围内** → **per-key MAC 强制**（非 optional）。
3. **投递语义一步到位做「有效恰好一次」**（端到端 ACK + 幂等 + 重连短队列），不走 best-effort fire-and-forget（详见 §3）。

**暂缓 / follow-up**：

- **Gate-审批 resume**：把 `gate.resolve` 接到 `Runner.send/interrupt`，让远端/本地审批真正让 agent 续跑——预存 P4 两向控制缺口，另开 issue。
- **关联 #4**：旧 issue「Cloud e2e: 服务端未接线 team auth/cache/relay」——本轮实测远端 relay 鉴权其实已通，建议一并复核其描述是否过时。
- **与 #78 不混淆**：新 dispatch/approve 错误码（`machine_offline`/`relay_unavailable`）流经与 #78 相同的 `friendlyError` 映射（`app.js`）。P4.5 只**新增**这些码的翻译，**不**修 #78 的既有裸码泄露——避免两个改动撞车。

---

## 8. 测试计划（摘要）

- **协议**（`tests/test_relay.py`）：Envelope 带 id+nonce+seq+ts round-trip；畸形命令帧 → `kind=''` 不抛（fail-closed）。
- **服务端端点**（`tests/test_remote_command.py`，FastAPI TestClient + FakeRelay）：需 token（401）；account 取自 token 非 body；`route()==0`→短队列/409；`route()≥1`→200 且 Envelope 带 action+关联 id；端到端 ACK 解析 pending-future。
- **跨租户隔离**（仿 `test_display_cache` 多租户）：A 的命令到不了 B 的在线机。
- **客户端连接器**（`tests/test_relay.py`，`_FakeClientConn`）：入站 `KIND_COMMAND` 到 on_frame；回 `KIND_ACK` 带原 id；handler 异常被吞、连接存活；同 id 重发回放缓存不双执行。
- **CloudManager**（`tests/test_cloud.py`，FakeStore + fake dispatcher）：`_build_connector` 传 on_frame；命令触发 `create()/resolve()/record_choice()` 并 ack；`create/resolve` 投到 app loop 不阻塞后台线程。
- **事件上行**：`KIND_EVENT` 上行按账号转发；订阅在后台 loop 创建并在重连时拆除（不漏订阅）；只过 display-safe。
- **安全**（`tests/test_remote_security.py`）：开关 OFF→命令被丢；空白名单→拒开启；越界 workspace→拒；level 2/3 远端仍全冒卡；陈旧/重放/跨账号/伪造（无 MAC）帧被拒；洪泛被限流；审计先于执行。
- **集成端到端**（真 `/relay` 端点，仿 `test_e2e_process_pushes_snapshot_owner_reads_it_back`）：本机 dial→owner POST 命令→帧下达 ws→本机回 ack/event 上行→服务端 surface；第二租户到不了第一租户的机器；无在线机→短队列/409 不挂起。
- **真机 E2E**（foreman-e2e，P4.6）：云端派发本地真跑 + 进度回流；远端危险操作回手机卡；offline→409；开关关丢命令。截图存证。

---

## 9. 关联与参考

- Issue [#77](https://github.com/simplerjiang/agent-foreman/issues/77)（本设计）、[#78](https://github.com/simplerjiang/agent-foreman/issues/78)（连接失败裸码泄露 + 首连 3s 误报 timeout，**独立**处理）。
- `DESIGN.zh-CN.md` §6（Decision loop / two-way control）、§8.3（数据边界：秘方本地）、§8.4（安全要点）、§8.5（连接协议：本地进程 ↔ 总机）。
- `ROADMAP.md` P4（Decision loop + two-way control）、P7（Team / relay mode）。
- `SECURITY.md`（红线闸 / 限速 / 审批门策略）。
