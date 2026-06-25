# 远端控制 上下行链路设计书（云端 ⇄ 本机：订阅驱动镜像 + 命令/审批 + TTL 通知队列）

日期：2026-06-25

分支：`claude/admiring-antonelli-d7625b`

基线：`origin/main` at `26b0457`（含 `65e72d0` 下行设计初稿）

关联 Issue：[#77](https://github.com/simplerjiang/agent-foreman/issues/77)（🔴 Blocker — 远端无法控制本地做开发：relay 只有上行镜像，云端→本地命令/审批下行链路未实现）

定位：完成 **P4「Decision loop + two-way control」** 的"两向"，并**顺带重构上行**，承载在 **P7「Team / relay 总机」** 之上（见 `DESIGN.zh-CN.md` §6 / §8.5）。

状态：**设计已定稿，待实现**。本轮把范围从"只接下行"扩成"**上下行一起重构**"——三个新决定见 §0.1。

> 💬 人话：现在手机/云端只能"看"本机在干嘛（上行镜像），还不能"指挥"本机干活（下行控制）。而且**连"看"这条上行路也设计得不对**——本机不管有没有人看，都每 20 秒把数据往我们服务器塞一份、还永久留着。这份文档把上、下两条线**一起重做**：服务器退成"纯转接的总机"，只在"需要叫人来看/审批"时临时存一条到期就删的小通知。

---

## 0. 这份文档解决什么

用户诉求：在 `foreman.kongsites.com` 远端**直接控制本机做开发**——派发任务、在手机上点审批，让本机的 claude-code / codex 真去干活，并把进度回传给远端看。

E2E 专项测试（2026-06-25）证实：**上行通、下行断**。但复盘上行实现后发现，**上行的"通"是用一个浪费且不该有的方式通的**（无条件定时全量推 + 服务器持久化镜像，§1.2）。所以本轮不是"在旧上行上接个下行"，而是把**上下行作为同一套双向通道一起重构**，覆盖四个维度：

1. **接线**（功能）：云端下发命令 → 本机真执行 → 进度回传；浏览器在线时实时看，离线时被通知叫醒。
2. **无状态**（隐私/空间）：服务器**不再持久化任何显示状态**——会话/卡片只活在本机（真源）与正在看的浏览器（前端缓存）。服务器唯一持久化的是一个**有 TTL 的通知队列**。
3. **可靠**（投递语义）：命令做到**有效恰好一次**，杜绝"假成功 / 丢命令 / 双执行"。
4. **安全**（门禁）：远端能在本机跑代码是全系统最高危面，必须默认关 + 多重护栏。

### 0.1 本轮三个新拍板决定（区别于下行初稿）

| # | 决定 | 影响 |
|---|---|---|
| A | **不考虑 PC 关机查看** | 服务器**不需要**为"PC 离线也能看完整进度"而镜像显示状态 → 删掉服务端 `DisplayCacheService` 持久化（`cache_sessions`/`cache_cards` 表）。显示状态只存本机 + 浏览器。 |
| B | **上行改"订阅驱动"** | 没有浏览器在看时，本机**不 pull、不 stream**，只在"要决策/有结果"时发一条通知。删掉现在那个无条件 20s 全量推循环（`client/relay.py:_sync_loop`）。 |
| C | **服务器只持久化"通知队列"，且必须到期删除** | 服务器空间有限 → 通知队列做到**极小载荷 + ack 即删 + TTL 兜底 + 去重 upsert + 每账号硬上限**，空间利用率最优（§4）。 |

> 💬 人话：①不用为"关了机还能看"去服务器存东西——这条需求砍了；②没人看的时候本机就安静，别空转往云上推；③服务器上唯一留下的，是一张"有人需要你来拍板/活干完了"的便签，看完就撕、过期自动撕。

---

## 1. 现状与差距（已核实）

对照 `26b0457` 源码，**上、下行各有问题**：

### 1.1 下行没接（四处代码事实）

| # | 位置 | 现状 |
|---|---|---|
| 1 | `server/app.py` | 全文搜 `relay.route(` / `KIND_COMMAND` **0 命中** —— 没有任何 HTTP endpoint 会把命令帧推给本机。 |
| 2 | `server/relay.py:195-217` `_on_frame` | 只处理 `KIND_HEARTBEAT` + `KIND_CACHE_SYNC`；注释自述命令转发 "layered on in P4"。 |
| 3 | `client/core/cloud.py:135-145` | 构造 `RelayConnector` **不传 `on_frame`** → 入站命令帧在 `client/relay.py:175` 被静默丢弃。 |
| 4 | — | 没有把 `KIND_COMMAND` 翻译成本地开发任务的执行器。 |

下行一句话：**原料全有，但全没接线。** 关键原语都已建好、已测试，只是没人调用：

- `Relay.route(account_id, env, process_id=)`（`server/relay.py:138`）—— 下行路由，建好但**无人调用**。
- `RelayConnector.on_frame`（`client/relay.py:175`）—— 入站回调，**支持但没传**。
- `DispatchService.create()`（`client/core/dispatch_service.py:106`）、`cards.record_choice()`（`cards.py`）、`gate.resolve()`（`gate.py:204`）—— 本地 UI 走的同一批执行 coroutine。

### 1.2 上行接错了方式（三处代码事实）

| # | 位置 | 现状 | 问题 |
|---|---|---|---|
| 1 | `client/relay.py:187` `_sync_loop` + `:67` `sync_interval=20.0` | 本机**一连上就每 20 秒推一次**，与"有没有人在看 PWA"无关 | 空转浪费带宽 + 无谓数据出境 |
| 2 | `client/core/cloud.py:131` `build_cache_sync(get_sessions(), get_decision_cards(None))` | 每次推的是**全部会话 + 全部卡片的完整快照**（非增量） | 数据量随历史线性增长 |
| 3 | `server/display_cache.py` + `server/store/db.py:308` `upsert_cache_*` | relay 把快照**落进服务器自己的 SQLite**（`cache_sessions`/`cache_cards`，`table=True`），**只 upsert、从不删** | 用户显示数据**长期驻留我们服务器**；删掉的本地会话**永远残留**（E2E 实测残留一条 `e2e-cloud-mirror-seed`） |

差距一句话：**下行——管子全有没接线；上行——接了，但接成了"不管有没有人看都往服务器塞、还永不清理"。**

> 💬 人话：水管、阀门、水龙头都装好了，下行只是没把它们拧到一起。上行更糟：水龙头一直开着往我们家水缸里灌，没人喝也灌、灌满了也不放——既费水又占地方。

**核心打法：上下行共用同一套双向通道与既有执行原语，绝不另起命令总线、也不再另留服务器镜像。** 远端路径与本地路径逐字节一致（同事件、同 nonce/防重放、同执行器），杜绝两条路径漂移。

---

## 2. 目标架构：无状态中转 + 三条线

服务器（总机 relay）退化成**无状态转接 + 一张 TTL 通知便签**。用户的显示数据只活在两处：**本机**（真源）和**正在看的浏览器**（前端缓存）。三条逻辑线全部跑在同一条已建好的反向隧道上：

```
                       ┌───────────────── 总机 Relay（云端，无状态转接）──────────────────┐
   PWA(浏览器)         │   按账号路由 · pending-future 关联 · presence 记账 · 通知队列(DB,TTL) │       本机进程            本地引擎
       │               └──────────────────────────────────────────────────────────────────┘           │                   │
       │  ① 实时面（双向，仅当浏览器订阅时活跃）                                                         │                   │
       │  订阅/pull/命令 ──/ws、/api/dispatch──▶  按账号路由  ── wss 既有反向隧道 ─────────────────────▶ on_frame          │
       │  ◀── 首屏快照 + 增量事件 + ACK ◀──────  按账号 republish ◀── wss ───────────────────────────── 应答/stream ◀──── AgentEvent
       │                                                                                                 │                   │
       │  ② 通知面（离线唤醒，唯一持久化）                                                                │                   │
       │  ◀── Web Push 唤醒 ◀── 写入 TTL 通知队列 ◀───────────── KIND_NOTIFY（决策待处理/结果就绪）◀──── 触发              │
       │  冷启动读队列：「你有 N 条待办」→ 唤醒并 pull 对应本机                                            │                   │
       │                                                                                                 │                   │
       │  ③ 控制面（下行命令/审批，§5）= 实时面的下行半 + Gate/autonomy 既有护栏                          │                   │
```

三条线的关键不变量：

1. **服务器零存显示状态**：实时面的快照/事件**穿过** relay 直达浏览器，relay **不落库**（删除 `DisplayCacheService` 持久化）。
2. **唯一持久化 = 通知队列**：极小载荷、ack 即删、TTL 兜底（§4）。
3. **三条线复用同一套底座**：反向隧道（§5.1）+ `relay.route()` 按账号路由 + `env.id` 关联 + ACK + 安全门禁。**上行 pull 本质就是"下行发一条 sync 请求 + 上行回一帧快照"**——和命令/ACK 是同一台机器，不是另一套协议。

> 💬 人话：服务器从"帮你存一份"降级成"帮你转一下，转完不留底"。唯一留底的是"有人喊你"的便签，而且看完/过期就撕。上行的"刷新"和下行的"下命令"用的是同一根管子、同一套对账规则。

---

## 3. 上行重构：订阅驱动的 pull + stream

把"无条件 20s 全量推 + 服务器缓存"换成"**有人看才动**"。

### 3.1 presence（订阅在场）机制

relay 按账号维护两个在线集合：**本机进程连接**（既有）+ **PWA 订阅连接**（PWA 的 `/ws`）。

- 浏览器打开 PWA → 通过 `/ws` 向 relay **订阅本账号** → relay 该账号订阅者计数 `0→1`。
- 计数 `0→1`：relay 给该账号在线本机推一帧 `KIND_SUBSCRIBE`（presence on）→ 本机**才**开始接受 pull、开始 stream 事件。
- 计数 `1→0`（最后一个浏览器关闭/断开）：relay 推 `KIND_UNSUBSCRIBE`（presence off）→ 本机停 stream，回到**安静态**（只在 §4 触发时发通知）。
- 本机**连上后默认安静**（不 pull、不 stream），直到收到"有订阅者"。

> ⚠️ 关键依赖：presence 是 relay→本机的下行帧 → **上行重构依赖下行通道（§5）先打通**。这正是"上下行必须一起做"的根因：新上行跑在和命令同一条 `relay.route()`/`on_frame` 管子上，没有下行就没有订阅驱动的上行。

### 3.2 首屏 pull + 增量 stream

浏览器订阅成功后：

1. **首屏 pull（一次性全量）**：PWA 发 `KIND_SNAPSHOT_REQ`（带 `process_id` + `env.id=corr`）→ relay route 到本机 → 本机用**既有 display-safe 构造器** `cache_sync.session_summary/card_summary`（**绝不含 diff/原始输出/秘方**，§8.3）组装 `KIND_SNAPSHOT(id=corr)` 回帧 → relay 按账号 republish 给发起的浏览器 → 浏览器存进 **IndexedDB/内存**（前端缓存）。
2. **增量 stream**：之后本机把 `AgentEvent` 映射成 display-safe 的 `KIND_EVENT` **实时上送**，relay 按 `client.account_id` republish 给订阅的浏览器（**绝不用帧里的 account_id**）。
3. **跨事件循环显式过桥**：`EventBus.publish` 在 uvicorn loop，CloudManager 消费在另一后台 loop——必须 `call_soon_threadsafe` 或线程安全队列过桥，否则 "Future attached to a different loop" 崩。

> 💬 人话：浏览器一打开，先问本机要一份"当前长啥样"的快照存在浏览器里；之后本机有新动静就实时推给它。全程服务器只过个手，不留底。

### 3.3 删除服务端显示缓存（迁移）

- **删除**：`server/display_cache.py` 的持久化路径（`DisplayCacheService.sync` 写库）、`cache_sessions`/`cache_cards` 表（建 migration 丢弃）、`server/relay.py` `_on_frame` 里的 `KIND_CACHE_SYNC` 落库分支、`/api/cache/sessions`、`/api/cache/cards` 端点。
- **替换**：PWA 改为「本地缓存优先 + 在线时实时 pull/stream」；不再 GET 服务器缓存。
- **客户端**：删 `client/relay.py:_sync_loop`（20s 定时推）；`cache_sync.py` 的构造器**保留**（被 §3.2 的 pull/stream 复用，只是触发方式从"定时推"变"应答/事件"）。
- **兼容**：`KIND_CACHE_SYNC` 退役（PROTOCOL_VERSION 升级，旧帧宽松忽略，不崩）。

> 💬 人话：把"服务器水缸"整个拆了，水龙头从"定时灌缸"改成"有人要才接一杯"。接水的家伙（摘要构造器）还是原来那套，不浪费。

---

## 4. 通知面：TTL 通知队列（唯一持久化，空间最优）

服务器**唯一**持久化的用户相关数据。目的不是"存进度给你看"，而是"**在浏览器没开着时，把人叫回来看/审批**"——即便此刻 PC 在线（用户不要求覆盖 PC 关机场景，§0.1-A）。

### 4.1 触发与投递

- 本机在两种时刻发一帧极小的 `KIND_NOTIFY` 上行：
  - **decision_needed**：出现待审批卡（Gate 冒卡）。
  - **result_ready**：会话完成 / 失败（有结果可看）。
- relay 收到 → ① **写入通知队列表**（§4.2）；② 尝试 **Web Push** 唤醒该账号已订阅推送的设备（service worker + VAPID）。
- 浏览器（冷启动、之前没在看）打开 → 先读队列 → 展示「你有 N 条待办（M 个决策 / K 个结果）」→ 用户点开 → 走 §3.2 pull 对应本机拿真详情。

> 💬 人话：本机只在"该喊人了"的时候发一张便签——"有个决策等你拍板""活干完了"。服务器收下便签、顺手用系统推送戳一下你手机。你打开 App，便签告诉你"有 2 件事"，点进去再向本机要详情。

### 4.2 数据模型（极小）

一条通知**只携带能渲染一条推送 + 能路由一次 pull 的最小信息**，绝不含 diff/原始输出/秘方：

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `account_id` | 作用域，**由 access-key 派生**，绝不取自帧体 |
| `process_id` | 哪台本机（供 pull 路由） |
| `kind` | `decision_needed` \| `result_ready` |
| `ref` | 要 pull 的 `card_id` / `session_id` |
| `title` | 极短 display-safe 文案（推送正文，如「决策待处理：是否执行 git push?」） |
| `dedup_key` | `account_id + process_id + ref`（去重 upsert，重复 notify 覆盖而非堆积） |
| `created_at` / `expires_at` | 入队时间 / 到期时间 |
| `read_at` | 浏览器确认已读 → 置位后即可删 |

### 4.3 生命周期与空间策略（§0.1-C 的落地）

服务器空间有限，队列做到**四重收缩**：

1. **ack 即删**：浏览器读到并 pull 后回一个 read-ack → relay **立刻删**该条（便签撕掉）。这让队列在正常路径下**几乎为空**。
2. **TTL 兜底**：给 `expires_at` 设短默认（`decision_needed` 例如 7 天、`result_ready` 例如 24 小时，可配）；惰性删除（读/写时顺手清过期）+ 低频后台 sweep。覆盖"推送失败且浏览器再没打开过"的孤儿。
3. **去重 upsert**：同 `dedup_key` 重发 → 覆盖旧行（同一张卡反复 notify 不堆积）。
4. **每账号硬上限**：每账号至多 N 条（如 200），超额**淘汰最旧**（环形缓冲语义）——故障/恶意本机刷通知也撑不爆盒子（fail-closed，仿 §4 限流）。

> 💬 人话：便签盒做了四道防溢出——看完就撕、过期自动撕、同一件事只留最新一张、每人最多 200 张满了挤掉最旧的。正常用的时候盒子基本是空的。

- **Web Push 订阅记录**：另需存一张极小的"推送订阅"表（endpoint + 公钥，按设备/账号）——这是**推送路由令牌、非用户内容**。push 返回 `410 Gone` → 删除失效订阅（自清理）。

---

## 5. 下行：命令 / 审批

### 5.1 传输模型：反向隧道（已就位）

本机在防火墙/NAT 后，连接永远**向外拨**：本机 dial `wss://<域名>/relay` → 送 access key → relay 鉴权后标记在线，长连接保持。PWA 的请求由 relay **按账号路由**到对应本机。

> 💬 人话：这就是 ngrok / Cloudflare Tunnel / VS Code 隧道的同款做法——内网机器主动打洞出去，外面通过"总机"找到它。穿 NAT/防火墙天然没问题。

### 5.2 下行数据路径（去程）

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
4. **本机执行，relay 只转发**：团队 relay 盒子只注入 `bus/relay/auth`，**没有** gate/cards/dispatcher，也不持有秘方（§8.3）——所以执行**必须**发生在本机进程，relay 只搬运意图。
5. **命令永远只是"提案"**：`KIND_COMMAND` 喂进既有 `DispatchService → Gate → autonomy` 管道，**绝不直接 shell/eval**，红线与白名单原样生效（详见 §7）。

### 5.3 进度回传（回程）—— 验收 #4，复用 §3.2 stream

下行命令触发本机执行后，进度**走 §3.2 的同一条增量 stream 回到订阅的浏览器**（不再回退服务器 cache——cache 已删）：

- 浏览器在看（已订阅）→ 实时 `KIND_EVENT` 流，~1s 见进度。
- 浏览器没在看 → 本机在关键节点（要审批 / 出结果）发 §4 的 `KIND_NOTIFY`，把人叫回来后再 pull。
- **延迟**：命令走**已经开着的 wss**（不新建连接），relay→本机一次推送通常 sub-100ms；进度回程目标 ~1s（republish 接到 PWA 的 `/ws`）。

> 💬 人话：你在看，就实时滚进度给你；你没在看，本机就到"该喊你"的节点发个通知把你叫回来——不再往服务器存一份冷数据兜底。

---

## 6. 投递可靠性：从 best-effort 到「有效恰好一次」

朴素实现（`route()` fire-and-forget）对"看进度、点审批"够用，但对"远端发一条开发任务、必须恰好执行一次"**不够**。核心问题一句话：

> **`relay.route()` 返回的是"发给了几条看起来活着的连接"，不是"对端真的收到并执行了"。**

### 6.1 四个「假成功 / 丢命令」失效点

| # | 失效点 | 后果 |
|---|---|---|
| ① | `route()≥1` 只证明"已入发送缓冲"，不等于本机已收到 | 半开连接（NAT 重绑 / 笔记本休眠）让 HTTP 200 **假成功**，命令静默蒸发。 |
| ② | 离线 `route()==0` → 409，命令直接丢、不排队、不重发 | 恰逢重连退避窗口派发即丢失，需手动重试。 |
| ③ | ACK/事件回程丢失 → PWA 重发 | 撞上 `nonce/seq` 防重放：要么被当重放丢弃，要么（无幂等）**双执行**。 |
| ④ | 心跳 30s 间隔 → registry `online` 标志最长 ~30–60s 陈旧 | 连接刚死时 `route()` 仍以为机器在线。 |

> 💬 人话：①"我喊了一嗓子"不等于"对方听见了"；②对方不在时这嗓子就白喊了；③没听见回声就重喊，可能喊重了把事干两遍；④对方其实已经走了，名单上还显示"在线"。

### 6.2 决定：一步到位做「有效恰好一次」

（= 至少一次投递 + 幂等去重）。补三件事，都是小而确定的增量，**命令与上行 pull 共用这套关联机制**（`KIND_SNAPSHOT_REQ`/`SNAPSHOT` 同样用 `env.id` 关联 + 超时）：

1. **端到端 ACK + 关联**：`Relay` 加一个**按账号、有界、带超时**的 pending-futures map，键 `env.id`，本机回 `KIND_ACK` 时在 `relay._on_frame` resolve；`POST /api/dispatch` **await 这个 future（带超时）**，返回的是"本机真的收到/开跑"。→ 修 ①。
2. **重连窗口短排队（store-and-forward）**：`route()==0` 不直接 409，而是按账号**秒级 TTL 排队**（有界 + TTL + 按账号）；PC 一 `register()` 重连就 flush 投递；超 TTL 才降级 409。→ 修 ②。
3. **幂等键 + 结果缓存**：客户端维护 `env.id → 缓存结果`；收到 `KIND_COMMAND` 若该 id 已处理过，**回放缓存 ACK**，既不重跑也不被当重放丢。→ 修 ③。
4. **附带**：心跳 30s→~10s（或开 TCP keepalive），把"假在线"窗口压到几秒。→ 缩小 ④。

> 注意：这里的"重连短队列"（§6.2-2，命令的秒级投递缓冲，命中即删）与 §4 的"通知队列"（唤醒便签，TTL 数天）是**两个不同的东西**，别混淆——前者在内存/短表、求"别丢这一下"，后者在 DB、求"叫得到人"。

### 6.3 必须写死的规则：幂等 ↔ 防重放 如何不打架

- **合法重试**：同一 `env.id` 重发时，服务端**换发新的 `nonce/seq`**（传输令牌）→ 过防重放；但 `env.id` 不变 → 命中客户端幂等缓存 → 回放结果，**不双执行**。
- **攻击者重放**：截获的旧帧 `nonce/seq` 已陈旧 → 在防重放层（§7，叠加强制 MAC）**先被丢**，根本到不了幂等判断。

> 一句话：**`env.id` 管「是不是同一条逻辑命令」，`nonce/seq/MAC` 管「这一次传输是不是新鲜且未被伪造」**，两层各司其职。

---

## 7. 安全硬门禁（验收 #5）

远端命令现在能在用户机器上跑代码——这是全系统最高危面。门禁是**硬门**：远端执行默认 **OFF**，直到本节全部落地且用户显式开启 + 配好白名单。

> 注：上行（pull/stream/notify）只携带 display-safe 摘要、不触发执行，风险面远低于下行命令；但 presence/snapshot 请求**同样按账号路由、同样不信帧里的 account_id**，跨租户隔离一视同仁。

### 7.1 七道护栏

1. **总开关默认 OFF**：`cfg.server.remote_execution_enabled`（默认 `False`）+ settings.json 开关。关 → `cloud.py` `_build_connector` 仍接 `on_frame`（上行 pull/presence 要用），但**命令分支 fail-closed**（收到 `KIND_COMMAND` 直接 ack `disabled`、不执行）。这是主断路器。
2. **工作区白名单前置**：`cfg.workspaces`（**顶层**）非空才允许开启；且开启时强制 `allow_unlisted_workspaces_for_dev=False`。远端 dispatch 走 `DispatchService._resolve_workspace`（fail-closed），绝不接受未经检查的 cwd。
3. **远端危险操作回手机卡**：`source=='phone'` 时 disposition **钳到 card-minimum**——一切都不 auto，每个远端动作都冒确认卡（复用 `Gate.request_approval` + Web Push 的一次性 nonce），即便本地 autonomy 是 level 2/3。**钳制点在 loop 的 disposition 路径按 session source 强制**。远端 `approve` 只能**解决既有 pending** 项，不可触发新 shell。
4. **防伪 + 防重放**：服务端给命令帧盖 `nonce + seq + ts`，客户端按连接跟踪 `last-seq` 丢陈旧/重复帧（防重放）；**per-key MAC（`hash_access_key` 派生、恒时校验）对 `KIND_COMMAND` 强制启用**（防伪造，含被攻破的 relay 运营方注帧）。两者都必选。
5. **限流**：每条入站 `KIND_COMMAND` 过 `SlidingWindowLimiter`（`shared/ratelimit.py`），按 account/process 限流；超限丢 + 审计。**通知上行同样限流**（防刷便签，§4.3 配合硬上限）。
6. **跨账号绑定**：PWA 调用方 token 的账号必须等于路由目标账号；relay 始终以 access-key 派生的 account_id 路由，绝不信帧里的 account_id。
7. **强制审计**：每条远端命令 **persist-then-act** 落审计（account_id / process_id / action / 解析后 workspace / Gate 分类 / disposition / approval_id / 结果），timeline 可见。

### 7.2 威胁清单 → 缓解

| 威胁 | 缓解 |
|---|---|
| **伪造帧**（被攻破的 relay 注帧） | 总开关默认关 + **强制 per-key MAC** + 命令只能变成 `create()` 提案、仍过 Gate |
| **重放**（重发旧命令帧） | `nonce + seq + ts`，客户端丢陈旧/重复；幂等缓存放行合法重试、防重放挡伪造重放（§6.3） |
| **越权/路径逃逸**（指定白名单外路径） | 走 fail-closed `_resolve_workspace` + PathGuard；开启远端执行前强制配白名单 |
| **跨账号劫持**（A 的命令/快照到 B 的机器） | account_id 由 key 哈希派生、`clients_for` 先按账号过滤；绝不信帧里的 account_id |
| **危险操作无人值守自动跑** | 远端一律 card-minimum，回手机审批卡（一次性 nonce）；不可逆永不 auto（autonomy 红线） |
| **秘密外泄**（让 agent 读 .env 回传） | 秘密不放进任何白名单 workspace 根；上行（快照/事件/通知）只推 display-safe 摘要；relay 不持 key、不落库 |
| **洪泛滥用**（刷命令 / 刷通知撑爆盒子） | 命令按 account/process 限流；通知队列 ack 即删 + TTL + 去重 + 每账号硬上限（§4.3） |
| **审计盲区** | 强制 persist-then-act 审计，timeline 可见 |

> 💬 人话：好消息是——确定性 Gate、fail-closed 工作区、nonce 审批、限流器**全都已经有**了。安全这块主要是把下行**穿过**这些既有护栏，再补两样帧级新东西（防重放 + 防伪 MAC），并关在默认关的开关后面。上行因为只搬摘要、不执行，风险低一档。

---

## 8. 分阶段落地

统一编号 **P4.x**（承接 #77 的 P4），**track** 列标明上行/下行/通用。先把双向"管子"打通（P4.1），上行重构与下行命令各自挂上去。

| 阶段 | track | 目标 | 关键触点 | 验收 | 依赖 | 规模 |
|---|---|---|---|---|---|---|
| **P4.0 协议地基** | 通用 | Envelope 加 `id + nonce/seq/ts`；定义 `KIND_SUBSCRIBE/UNSUBSCRIBE`、`KIND_SNAPSHOT_REQ/SNAPSHOT`、`KIND_EVENT`、`KIND_COMMAND/ACK`、`KIND_NOTIFY`；`KIND_CACHE_SYNC` 退役；`PROTOCOL_VERSION→2`。`env.id` 定为幂等键、与传输令牌分离。 | `shared/protocol.py`、`tests/test_relay.py` | 各 kind round-trip；旧 v1 帧宽松忽略不崩 | — | S |
| **P4.1 双向通道地基** | 通用 | `relay.route()` 调通 + pending-futures（端到端 ACK 关联）；客户端 `on_frame` 接线（默认关后面，fail-closed）；relay 按账号记 presence（订阅者计数）。把"管子"双向打通。 | `server/relay.py`、`client/core/cloud.py`、`client/local_app.py`、`client/relay.py` | route→on_frame→ACK 闭环；presence 计数随订阅升降；app_loop 经 startup 钩子绑定 | P4.0 | M+ |
| **P4.2 上行重构** | 上行 | presence-gated pull/stream 取代 20s 定时推；删 `_sync_loop` + `DisplayCacheService` 持久化 + `cache_*` 表 + `/api/cache/*`；PWA 改本地缓存 + 实时 pull；事件跨 loop `call_soon_threadsafe` 过桥 | `client/relay.py`、`cloud.py`、`server/relay.py`、`server/app.py`、`server/web/app.js`、migration | 无订阅→本机安静（不推）；订阅→首屏快照+增量事件；服务器零落库；删会话不再残留云端 | P4.1 | L |
| **P4.3 通知队列** | 上行 | TTL 通知队列表 + `KIND_NOTIFY` 触发（decision_needed/result_ready）+ Web Push（VAPID/service worker）+ ack 即删 + sweep + 去重 upsert + 每账号硬上限 + 推送订阅自清理 | `server/store`（新表+migration）、`server/relay.py`、`server/app.py`、`client/*`、`web/sw.js`、`app.js` | notify 入队/Web Push 唤醒；冷启动读「N 条待办」；ack/过期/超额均删；空间不随历史无界增长 | P4.1 | L |
| **P4.4 下行命令 + 可靠层** | 下行 | `POST /api/dispatch` + `/api/approve` 调 `route()`（强制 process_id，503 personal/409 offline）；重连窗口短队列；客户端命令→`create()/record_choice()/resolve()` 经 `run_coroutine_threadsafe` 投 app loop；幂等缓存；回 `KIND_ACK` | `server/app.py`、`server/relay.py`、`client/core/cloud.py`、`tests/test_remote_command.py` | dispatch 建会话（同 `/api/tasks`）；approve 解既有卡；离线先短排队再 409；幂等不双执行 | P4.1 | L |
| **P4.5 安全硬门禁** | 通用 | 总开关 + 白名单前置 + 远端 card-minimum + `nonce/seq/ts` 防重放 + **强制 MAC** + 命令/通知限流 + card 幂等 + 审计 | `config.py`、`cloud.py`、`gate.py`、`autonomy.py`、`relay.py`、`protocol.py`、`ratelimit.py` | 默认关丢命令（仍许上行）；空白名单拒开启；level 2/3 远端仍全冒卡；陈旧/重放/跨账号/伪造帧被拒；先审计后执行 | P4.4 | L |
| **P4.6 PWA 远程控制 UI** | 下行 | `loadProcesses`；Composer 加目标机器 select→`/api/dispatch`（带 process_id）；审批按钮→`/api/approve` 带 nonce+process_id；i18n + friendlyError 补 `machine_offline/relay_unavailable`；personal mode 隐藏 | `server/web/app.js`、`app.css` | 列在线机并派发到指定机；远端 approve 回源机；新错误码翻译显示 | P4.4, P4.5 | M |
| **P4.7 真机 E2E 复验** | 通用 | foreman-e2e 对 `foreman.kongsites.com` 全链路实测，截图作为 #77 close 证据 | foreman-e2e skill | 订阅看实时进度；云端派发→本地真执行；危险操作回手机卡→审批续跑（Card 路径）；离线→409/通知；服务器零残留 | P4.2, P4.3, P4.5, P4.6 | M |

**关键防呆**（评审已核实并折叠进各阶段）：

- **app_loop 经启动钩子绑定，非构造期注入**——`CloudManager` 在 `local_app.py:186` 构造时 uvicorn loop 尚不存在。改为 FastAPI `lifespan`/`startup` 钩子里 `asyncio.get_running_loop()` → `cloud.bind_app_loop(loop)`；绑定前 `on_frame` fail-closed（ack `not_ready`）。
- **跨事件循环**用 `call_soon_threadsafe` 显式过桥（§3.2）。
- **dispatch/approve 强制带 process_id**，避免多机扇出 ACK 歧义。
- **on_frame 从 P4.1 起就接，但命令分支默认关 + 落审计**——上行 pull/presence 需要 on_frame，故 P4.2 起 on_frame 必在线；命令的"可执行"才关在 P4.5 完整护栏后。
- **两个"队列"别混**：§6.2 命令重连短队列（内存/短表、命中即删）≠ §4 通知队列（DB、TTL 数天）。

---

## 9. 验收标准映射（#77 五条 close 条件）

| #77 验收点 | 关闭阶段 |
|---|---|
| #1 远端派发 + 本机真执行 | P4.4（UI：P4.6）；审批续跑由 **Card 路径**证明（见 §10 决定①） |
| #2 服务端端点调 `relay.route()` | P4.4（管子 P4.1） |
| #3 客户端 `on_frame` 接线、命令进 dispatch | P4.1（接线）+ P4.4（进 dispatch） |
| #4 执行进度经 relay 回传可见 | P4.2（实时 stream）+ P4.3（离线通知唤醒） |
| #5 危险操作确认 / 工作区护栏 | P4.4（默认关）+ P4.5（完整护栏） |

> 上行重构（P4.2）与通知队列（P4.3）是本轮**新增范围**，超出 #77 原"下行"边界 → 建议把 #77 标题/范围扩成"远端控制双向链路（含上行重构）"，或拆一条 sibling issue 跟踪 P4.2/P4.3（见 §10 暂缓项）。

---

## 10. 已拍板决定 & 暂缓项

**本轮新拍板（2026-06-25，§0.1）**：

- **A** 不考虑 PC 关机查看 → 服务器**零持久化显示状态**，删 `DisplayCacheService` 持久化 + `cache_*` 表。
- **B** 上行改**订阅驱动**（presence-gated pull/stream），删 20s 无条件全量推。
- **C** 服务器唯一持久化 = **TTL 通知队列**，ack 即删 + TTL + 去重 + 每账号硬上限，空间最优。

**沿用下行初稿已拍板**：

1. **Gate-审批 resume 移出 #77** → 另开 follow-up。`gate.resolve()` 当前只记录决定、`execution_deferred=True` **不让 agent 续跑**（`gate.py:216/253`）。#77 验收 #1 的"审批后续跑"由 **Card 路径**（`record_choice → loop.on_card_decision` 会 checkpoint+执行）证明；Approval-resume 单列。
2. **被攻破 relay 伪造命令帧在威胁范围内** → **per-key MAC 强制**（非 optional）。
3. **命令投递语义一步到位做「有效恰好一次」**（端到端 ACK + 幂等 + 重连短队列）。

**暂缓 / follow-up**：

- **Issue 拆分**：本设计已超出 #77（纯下行）范围 → 建议要么把 #77 扩成"双向链路 + 上行重构"，要么新开一条 sibling issue 跟踪 P4.2（上行重构）+ P4.3（通知队列）。**待用户定**后再动 GitHub。
- **Gate-审批 resume**：把 `gate.resolve` 接到 `Runner.send/interrupt`，让远端/本地审批真正让 agent 续跑——预存 P4 两向控制缺口，另开 issue。
- **关联 #4**（旧 issue「Cloud e2e: 服务端未接线 team auth/cache/relay」）：本轮实测远端 relay 鉴权已通且 cache 即将删除，建议复核其描述是否过时。
- **与 #78 不混淆**：新错误码（`machine_offline`/`relay_unavailable`）流经与 #78 相同的 `friendlyError` 映射。P4.6 只**新增**翻译，**不**修 #78 的既有裸码泄露。

---

## 11. 测试计划（摘要）

- **协议**（`tests/test_relay.py`）：各 kind（subscribe/snapshot/event/command/ack/notify）带 id+nonce+seq+ts round-trip；畸形帧 → `kind=''` 不抛（fail-closed）；旧 `KIND_CACHE_SYNC` 帧被宽松忽略。
- **上行 presence/pull**（`tests/test_relay.py` + `test_cloud.py`）：无订阅者→本机不 stream；订阅 `0→1`→presence on→首屏 snapshot 回帧带 corr id；`1→0`→presence off→本机安静；snapshot 只过 display-safe。
- **服务端零落库**（改写 `test_display_cache` → `test_no_server_cache`）：`KIND_EVENT/SNAPSHOT` 穿过 relay republish 给订阅浏览器，relay **不写任何持久表**；`cache_*` 表与 `/api/cache/*` 已移除。
- **通知队列**（`tests/test_notify_queue.py`）：`KIND_NOTIFY`→入队 + Web Push 调用；read-ack→删；过期→sweep 删；同 dedup_key→upsert 覆盖；超每账号上限→淘汰最旧；账号隔离（A 看不到 B 的通知）；推送 410→订阅删。
- **服务端命令端点**（`tests/test_remote_command.py`，TestClient + FakeRelay）：需 token（401）；account 取自 token 非 body；`route()==0`→短队列/409；`route()≥1`→200 且 Envelope 带 action+corr id；端到端 ACK 解 pending-future。
- **跨租户隔离**：A 的命令/快照请求到不了 B 的在线机。
- **客户端连接器**（`tests/test_relay.py`，`_FakeClientConn`）：入站 `KIND_COMMAND`→on_frame；回 `KIND_ACK` 带原 id；handler 异常被吞、连接存活；同 id 重发回放缓存不双执行。
- **CloudManager**（`tests/test_cloud.py`）：`_build_connector` 传 on_frame；命令触发 `create()/resolve()/record_choice()` 并 ack；投到 app loop 不阻塞后台线程。
- **安全**（`tests/test_remote_security.py`）：开关 OFF→命令被丢（但上行 pull 仍通）；空白名单→拒开启；越界 workspace→拒；level 2/3 远端仍全冒卡；陈旧/重放/跨账号/伪造（无 MAC）帧被拒；洪泛被限流；审计先于执行。
- **集成端到端**（真 `/relay` 端点）：本机 dial→浏览器订阅→pull 快照→owner POST 命令→帧下达 ws→本机回 ack/event 上行→浏览器 surface；第二租户到不了第一租户的机器；无在线机→短队列/409 不挂起；浏览器离线→notify 入队 + Web Push。
- **真机 E2E**（foreman-e2e，P4.7）：订阅看实时进度；云端派发本地真跑；远端危险操作回手机卡；offline→409 + 通知；开关关丢命令；**测后确认服务器零残留**（不再有 `e2e-cloud-mirror-seed` 类残留）。截图存证。

---

## 12. 关联与参考

- Issue [#77](https://github.com/simplerjiang/agent-foreman/issues/77)（本设计，建议扩范围或拆 sibling 跟踪上行重构）、[#78](https://github.com/simplerjiang/agent-foreman/issues/78)（连接失败裸码泄露 + 首连 3s 误报 timeout，**独立**处理）。
- `DESIGN.zh-CN.md` §6（Decision loop / two-way control）、§8.3（数据边界：秘方本地）、§8.4（安全要点）、§8.5（连接协议：本地进程 ↔ 总机）。**本设计修订 §8.5 ③**：服务端不再持久化显示缓存，改为订阅驱动转接 + TTL 通知队列。
- `ROADMAP.md` P4（Decision loop + two-way control）、P7（Team / relay mode）。
- `SECURITY.md`（红线闸 / 限速 / 审批门策略）。
