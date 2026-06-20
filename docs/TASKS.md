# Foreman 任务拆分（实现步骤）

> 把 [DESIGN.zh-CN.md](DESIGN.zh-CN.md) 的设计边界拆成**可执行、可验收**的步骤。
> 阶段对应 [ROADMAP.md](ROADMAP.md) 的 P0–P7；代码边界见 DESIGN §14：
> **shared**（两端共用）/ **client**（PC 应用 + agents）/ **server**（后端 + PWA）。
>
> 约定：每步尽量是"能独立跑、能验收"的纵向小切片；`[ ]`→`[x]` 表示完成；新发现的子任务就近补进对应阶段。

---

## P0.5 — 仓库重排（动手前的地基）
把现有扁平的 `src/foreman/{core,agents,...}` 重排成 client / server / shared 三块。

- [x] **T0.1** 建 `shared/`：迁入 `config.py`、`llm/client.py`；新增 `events.py`（AgentEvent + 事件类型 + EventBus）、`protocol.py`（wss 协议契约占位）。✅ ruff/import/4 tests 通过。
- [x] **T0.2** 建 `client/`：迁入 `agents/`、`monitor/`、`core/`（operator/auditor/gate/reviewer/scheduler/supervisor/checkpoint/events）、`store/`（本地库）；新增 `computer_use/` 占位。✅ 三关通过（review/security/PM）；client⊥server 边界双向验证。
- [x] **T0.3** 建 `server/`：迁入 `server/{app,push,auth}.py`、`web/`（PWA 前端归这里）；新增 server `store/`（服务器库）占位。✅ 三关通过；/health+web 200，server⊥client，双库无表名冲突。
- [x] **T0.4** 改 `__main__.py`：`foreman app`（client）/ `foreman serve`（server）/ `foreman dispatch`；`pyproject.toml` 入口 + 可选依赖分组（client / server extras）。✅ 三关通过；deploy 脚本改 `.[server]`。
- [x] **验收**：`foreman serve` 仍能起 `/health`；`foreman.client` 与 `foreman.server` 各自独立可导入。✅ 本地 /health 200 + 12 tests；线上部署验证见 T0.4 提交。

## P1 — 单机驱动（client 为主）
dispatch 一个任务 → 看 claude/codex 在真实工作区跑 → 窗口/浏览器看实时事件。

**shared**
- [x] **T1.1** 定稿 `AgentEvent` 与事件类型枚举（DESIGN §7.1）；时间戳 UTC ISO8601。✅ `make_event()`（校验 type）+ `utc_now_iso()`；三关通过；16 tests。
- [x] **T1.2** LLM client 跑通最小调用（OpenAI 兼容 + Anthropic 兼容），读 base_url/model/key。✅ `transport=` 注入 + MockTransport 测两种线格式；三关通过；19 tests。

**client / agents**
- [x] **T1.3** `ClaudeCodeAdapter.start`：spawn `claude -p "<instr>" --output-format stream-json --verbose`（Windows：`claude.cmd`、UTF-8、creationflags）。✅ start+stop+`_build_cmd`+`_spawn` 可测seam；argv 列表(无注入)；三关通过；23 tests。
- [x] **T1.4** `ClaudeCodeAdapter.stream`：逐行 `json.loads` → 映射 `AgentEvent`。✅ result→stop 其余→agent_output；非 JSON/非对象→raw；三关通过；24 tests。
- [x] **T1.5** `ClaudeCodeAdapter.stop`（+ `send`/`--resume` 占位给 P4）。✅ stop（T1.3）+ stream 捕获 `native_session_id`；send/interrupt 明确 P4/P3 stub；三关通过；25 tests。
- [x] **T1.6** `CodexAdapter.start/stream/stop`：`codex exec`，输出解析。✅ 抽出 `SubprocessCliAdapter` 基类，claude/codex 皆瘦子类；共享 `_fakes`；三关通过；29 tests。
- [x] **T1.7** `Runner.launch`：选 adapter → 起 agent → 每个事件 **落库 + 上 EventBus**；两个 CLI 可并行不同会话。✅ 后台 _pump（先落库后上总线）+ wait()；未启用 agent 报错；三关通过；35 tests。

**client / store**
- [x] **T1.8** 本地 SQLite：`sessions / tasks / events` 表 + 读写；`schema_version`。✅ add/get sessions·tasks·events（payload→JSON）；init 记 schema_version；三关通过；33 tests。

**client / CLI + 本地 UI**
- [x] **T1.9** `foreman dispatch "<task>" --workspace <path> --agent claude-code|codex`：建 Root Session → `Runner.launch`。✅ `client/dispatch.py`（build_session_task + run_dispatch，可注入）；CLI 懒加载保边界；三关通过；38 tests。
- [x] **T1.10** 本地 API：`GET /api/sessions`、`GET /api/sessions/{id}/events`、`WS /ws`（EventBus→前端）。✅ store=None→503；WS 先回放后实时、按会话过滤、断连处理；EventBus 加 subscribe_queue；三关通过；42 tests；线上 /health 200 + /api 503（无数据泄露）。
- [x] **T1.11** 最小时间线页（`server/web/`）：列会话 + 实时事件流。✅ app.js 接 /api/sessions + WS；textContent 防 XSS；**Playwright/chromium 浏览器验收通过**（3 事件渲染+截图）；三关通过；44 tests。
- [x] **T1.12** `foreman app`：pywebview 原生窗口套本地 UI + pystray 托盘（开=上线/关=下线）。✅ `start_local_app`（后台 uvicorn 线程，可测）+ app_cmd 懒加载 pywebview 窗口（无则无头回退）；关窗=下线；托盘**暂缓**（避免未测 GUI 循环冲突）；三关通过；45 tests。窗口本身需桌面运行验证（UI 已在 T1.11 浏览器验收）。
- [x] **验收**：`foreman dispatch ... --workspace D:\proj`，窗口/浏览器实时看到 claude 与 codex 的事件流入。✅ dispatch 落库（T1.9）→ /api + /ws 取（T1.10）→ 时间线页渲染（T1.11 浏览器验收）→ `foreman app` 窗口承载（T1.12）。45 tests 全绿。

## P1.5 — 多语言（i18n）+ 输出语言（DESIGN §15）
中/英切换：不只换界面，还贯穿提示词与 LLM 返回语言。
- [x] **T1.13** 语言设置 + 输出语言指令：config `ui.language`（默认 zh）+ 运行时 `config_kv` 覆盖 + `GET/POST /api/settings/language`；shared `language_directive(lang)`；预留接入 Operator/Auditor/Reviewer/Briefing 的 system prompt。✅ shared/i18n + ConfigKV + 端点；三关通过；48 tests。
- [x] **T1.14** UI 语言切换：header zh/en 切换 + 文案字典 + localStorage + 同步后端；**浏览器验收**（切换后界面文案随之改变，截图）。✅ data-i18n + I18N 字典 + 切换标签翻转 + 会话区重渲染；**Playwright 验收通过**（时间线 时间线→Timeline）；三关通过；49 tests。

## P2 — 观测 + 审阅 + 检查点 + 看门狗（client）
- [x] **T2.1** 工作区非仓库时 `git init`。✅ `ensure_repo()`（rev-parse 检测 + git init，argv 无 shell；子目录仓库不重复 init）；三关通过；52 tests。
- [x] **T2.2** Checkpoint Manager：临时索引 `add -A`→`commit-tree`→影子 ref `refs/foreman/ckpt/*`（§6.5）；记 `checkpoints`。✅ `snapshot()`（`GIT_INDEX_FILE` 临时索引→`add -A`→`write-tree`→`commit-tree` 串父链→`update-ref` 影子 ref；`core.autocrlf=false`；不碰暂存区/分支/历史；遵守 .gitignore 不收机密）+ `Store.add/get_checkpoints`；argv 无 shell；三关通过；59 tests。
- [x] **T2.3** 一键回退：恢复到某检查点（含删后建文件）+ 先给当前打点（redo）。✅ `undo_to()`（先 `snapshot` 当前为 redo 点→`_restore_worktree`：`rev-parse` 校验目标 commit→临时索引 `read-tree`+`checkout-index -af` 重写目标文件→删检查点后新建文件→prune 空目录；`core.autocrlf=false` 逐字节一致；遵守 .gitignore 不删机密/node_modules）+ `resolve_step()`；argv 无 shell；agent 状态复位延后至 P4 决策回路层（持有 Runner）。三关通过；66 tests。
- [x] **T2.4** Hook 接收端 `POST /hooks`（PreToolUse/PostToolUse/Stop/Notification）。✅ `HookReceiver`（map→落库→上总线，persist-first 同 Runner；PreToolUse 走 Gate.classify，命中 requires-approval → 记 `approval_req` + 回 deny 决策体，curl 把它回传给 Claude Code 即拦下危险工具）+ `hook_to_event`/`action_text` 纯函数；route `POST /hooks` 在 server/app.py 但**仅依赖 shared**、receiver 注入（同 store/bus，team `serve` 不注入→503，hook 是本地端点 §4.3）；hook 名取 X-Hook 头→body `hook_event_name`；session 关联取 `?session_id`→Claude 原生 id；新增事件类型 `notification`（§4.1/§5.6 看门狗「在等输入」信号，§7.1 枚举漏列，补上）。**完整审批回路（推手机+等+resume）延后 P3**（Gate.request_approval）。三关：①Code Review——diff 小且加性，payload/非 dict/空 body 皆兜底，无 shell 执行（action_text 仅做子串分级）；②安全——/hooks 本地 127.0.0.1、无命令注入、不碰 secrets、deny 回显仅本地、team 端 503 不暴露，鉴权属 T3.5；③PM 验收——对照验收：四类 hook + SubagentStop 全覆盖、PreToolUse↔Gate 联动拦危险动作（§4.3/§6.6）达成。82 tests 全绿 + ruff 通过。
- [x] **T2.5** Git watcher（diff/commit）+ process/idle（psutil）→ 刷新 `last_progress_at`。✅ 三个廉价观测源喂看门狗的 `last_progress_at`（§4.1/§4.3）：①`ProgressTracker`（内存版 last_progress_at 注册表，注入时钟；`touch/last/idle_seconds/is_idle/drop`，stdout/hook/git/CPU 任一信号都 `touch`）；②`GitWatcher.poll`（`rev-parse HEAD`+`status --porcelain` 比对上次状态→`git_commit`/`git_diff` 事件，payload **仅计数不含文件名/内容**；任一 git 变化 `touch` tracker；persist-first 再上总线；首轮立基线；`runner` 注入 seam 无 shell；新增 `drop()`）；③`ProcessWatcher.poll`（psutil 经注入 `sampler` seam：进程存活 + 累计 CPU 增量判活；CPU 推进→`touch`；按 **同一 pid** 比对 delta，重启/PID 复用同 key 自动重立基线不误报；死进程丢基线；读不到→unknown 不扰基线）。`watch()` 皆为定时轮询包壳。三关：①Code Review（subagent）——基线/死进程/PID 复用/浮点阈值边界齐备，importorskip 守 psutil 缺失，风格对齐 test_hooks；②安全（subagent）——无 shell/命令注入、git_diff 仅计数不泄漏路径内容、psutil 仅读传入 pid、AccessDenied 安全降级 unknown、补 GitWatcher.drop 防长跑泄漏；③PM 验收——对照 §4.1/§4.3：三观测源齐、皆刷新 last_progress_at、idle 判定就绪供 T2.6。108 tests 全绿 + ruff 通过。
- [x] **T2.6** Supervisor 看门狗（**全局唯一**）：池健康状态机 + 廉价巡检 + 可疑时升级 LLM + 恢复 playbook（§4.1/§5.6）。✅ `Supervisor`（client/core）= 单个全局看门狗：`pool: dict[key→AgentRecord]`，`register/set_pid/mark_done/unregister` 管池成员；`AgentRecord.state ∈ {starting,running,idle,waiting_input,stalled,errored,dead,done}`（§4.1 八态全覆盖）+ 经 `ProgressTracker`（T2.5）读 `last_progress_at`。**两层巡检**：①`classify()` 纯函数廉价确定性（不花 token）——优先级 进程死>tail报错>tail等输入>stalled>idle>running；阈值**按 agent 类型分设**（codex idle60/stall150 比 claude 120/300 更紧，§4.1）；进程存活/stdout 尾巴皆为注入 seam（`liveness`/`tail_provider`，无 seam 则纯 idle 判定），时钟/`now` 注入可测。②`judge` 仅在「可疑」且已接 LLM 时调用、**绝不每轮**（测试验证 not-suspicious→judge 零调用）。`poll_once` 扫全池：快照迭代 + **逐 agent try/except**（单点抛错只记一条 `error` 事件、不拖垮整圈，§4.1「单点要稳」）。**恢复 playbook**`plan_recovery`：DEAD→restart_from_checkpoint（连崩>max_restarts→escalate_card）/STALLED→nudge/WAITING_INPUT→answer_or_card/ERRORED→backoff_or_card（对齐 §4.1 处置表）；状态变化才发 `health`/`stall`/`recover` 事件（持续同态保持安静），persist-first 同 Runner。`LLMJudge`（②）拼提示词时**追加 `language_directive`**（§15），tail 先脱敏后限长再外发。**恢复执行（真 nudge/interrupt+resume/检查点重启）延后 P4**（决策回路持有 Runner 两向控制；Runner.send=P4 stub、interrupt=P3 stub、§6 Gate 在 P4）——本任务交付「发现+分类+规划+升级事件」，`recover` 事件含 `execution_deferred=True`。**失败转移建议（换 CLI）**作为 P4 卡片内容延后。三关：①Code Review（subagent）——precedence/阈值数学/连崩 escalate 计数/变更才发事件/逐 agent 隔离 皆正确；按建议修：judge 覆盖只许 `_JUDGE_ALLOWED`（防 rogue judge 伪造 DEAD/DONE/STARTING）、tail 每 tick 只读一次、fail_count 任何非 DEAD 即清零；②安全（subagent）——无 subprocess/shell/eval、事件载荷仅元数据不含 stdout/路径、key 由 LLMClient 管不外泄；按建议修：`error` 载荷用 `type:msg` 截 200（防未来 seam 泄路径）、`redact_secrets` 在 tail 外发前掩码 sk-/Bearer/api_key/gh_ 凭据形；③PM 验收——对照 §4.1/§5.6：全局唯一+八态机+廉价巡检+可疑才升级 LLM+恢复 playbook 全达成（执行层 P4，已标注）。**132 tests 全绿（+24 本任务）+ ruff 通过**。client⊥server 边界守住（纯 client/core + shared）。
- [x] **T2.7** Reviewer：检查点处 diff+目标 → 结构化评审。✅ 与 Auditor 互补（Auditor 管动手前、Reviewer 管动手后，§4.1）：`Reviewer.review(goal, diff, *, context, qa_standard)` 把「检查点处 diff + 目标 + QA标准 + 相关文件片段」喂 LLM → 结构化 `ReviewResult{verdict, summary, risks[], suggestions[], needs_human}`，`verdict ∈ {approve, request_changes, escalate}`（§186-187）。**diff 来源**：新增 `CheckpointManager.diff(from_ref, to_ref=None)`——临时索引快照活动工作区与检查点对比，**新建未跟踪文件也进 diff**（裸 `git diff <commit>` 会漏）；`rev-parse --verify` 拒 option-like 输入；`add -A` 遵守 .gitignore 故 .env/secrets/node_modules 永不进 diff。**§15**：system prompt 追加 `language_directive`（stub 漏了，本次补上）。**从严默认（§6.7）**：`parse_review` 解析失败/未知 verdict/空回复 一律 `escalate`+`needs_human=True`，绝不静默 approve；escalate 强制 needs_human；容错抽 JSON（```json 围栏 / 散文中嵌对象 / 标量→list 强转）；diff 超长截断控 token。三关：①Code Review——verdict 闭集校验+大小写归一、围栏/散文抽取、list 强转去空、diff 含新文件且 tree-vs-tree 正确；②安全——全 argv 无 shell、rev-parse --verify 防注入、diff 遵守 .gitignore 不泄机密、仅发用户自有 LLM 端点（同 T2.6 信任边界）；③PM 验收——对照 §4.1/§5.3/§186：检查点 diff+目标+QA+片段输入、四字段结构化输出、三态 verdict、§15 语言指令 全达成。**执行层延后**：request_changes→Runner.send（P4 stub）、escalate→Gate→卡→一键回退（Gate P3/P4；T2.3 `undo_to` 已备回退机制）；live LLM key mock 测+延后（同 T2.6）。**154 tests 全绿（+22）+ ruff 通过**。client⊥server 边界守住（纯 client/core + shared）。
- [x] **验收**：任务完成自动产出评审；任一步可一键回退；卡死/崩溃能被发现并恢复或升级。✅ 新增**阶段级集成测试** `tests/test_p2_acceptance.py`（2 例）把 P2 各块串成一条端到端流，跑在**真实 git 工作区 + 真实 SQLite Store + 真实 EventBus + 真实 ProgressTracker** 上（无网络/无 token，Reviewer 走 httpx.MockTransport）：①任务完成→自动评审——`CheckpointManager.diff(预检查点)`→`Reviewer.review`→结构化 verdict（escalate+needs_human），persist-first 落 `review` 事件再上总线；②一键回退——`undo_to(预检查点)` 把工作区**逐字节复原**（改动还原/新文件删除/被删文件重建）+ 留 redo 点，验证 Reviewer escalate→「⛔撤掉重来」路径真能跑；③卡死/崩溃发现并恢复或升级——单一全局 Supervisor 廉价巡检同时识别 STALLED→`nudge`、DEAD→`restart_from_checkpoint`，连崩>max_restarts→`escalate_card`（升级弹卡），且 LLM judge **仅对可疑 agent 调用、绝不调健康 agent / 不每轮调**；事件 persist-first 落库+上总线双验。三关：①Code Review——时钟注入确定性（codex idle200≥stall150、claude idle10=running、liveness 优先判 DEAD）、连崩计数 1→4 跨 max_restarts、payload 经 json.loads 读；②安全——纯测试无 shell/无注入/无真机密/零网络出网；③PM 验收——对照 ROADMAP P2「Done when」三条验收逐条有据达成（非仅单测绿）。**156 tests 全绿（+2）+ ruff 通过**。**P2 阶段完成**。

## P3 — 手机面 + 审批 + server 起步
- [x] **T3.1** server 库：accounts / access_keys / process_registry（占位 invites / cache）。✅ 在 `ServerStore`（server/store/db.py）上补齐三张表的读写助手，模型/引擎此前已搭好（T0.3）：①**accounts**——`add_account`（admin 建号、created_at 自动盖戳）/`get_account`/`get_account_by_username`/`get_accounts`/`set_account_status`（停用/启用，缺号 no-op）；②**access_keys**——`add_access_key`（**仅存哈希**，明文只在创建时给一次）/`get_access_keys`（active+revoked，新→旧）/`get_access_key_by_hash`（握手路径用，含 revoked 让调用方判 status/expiry）/`revoke_access_key`（**单张吊销、其余照常**，对齐 §7.2「一机一张、可多张、哈希存、可吊销」）/`touch_access_key`（last_seen_at）；③**process_registry**——`register_process`（**按 id upsert**，首插盖 created_at、复注更新原行并返回持久化行）/`set_process_online`（在线/离线+心跳，App 开=上线关=下线 §4.6）/`get_processes`（一人可多机）/`get_online_processes`（可选按账号 scope，供总机路由 §8.5）；`init()` 补记 server `schema_version`（§11.1，与 client 各表名独立无冲突）；`session(expire_on_commit=False)` 使返回行可读。**invites/cache 仅建表占位**（admin 邀请 T7.2 / 展示缓存 T7.5，本任务不加助手）。三关：①Code Review——upsert 更新路径改为返回持久化行（避免回传 created_at 为空的入参对象）、`col(online).is_(True)` 布尔过滤正确、列表稳定排序；②安全——纯 SQLModel 参数化查询无注入、无 shell/subprocess/eval、**access key 只存哈希永不存明文**、server 库不含 LLM key/秘方/diff（§7.2/§8.3）、多租户查询按 account_id 隔离（timing-safe key 比对属 T3.5 鉴权层，已注）；③PM 验收——对照 §7.2：三张主表读写齐全 + 哈希存/单张吊销 + 在线注册与路由查询 + invites/cache 占位达成。**165 passed +2 skipped（+11 本任务）+ ruff 通过**。server⊥client 边界守住（纯 server/store + shared）。
- [x] **T3.2** `wss /relay`：出站长连 + access key 握手 + 心跳 + 重连 + 按账号路由（§8.5）。✅ 双端实现，共用 `shared/protocol.Envelope`（补齐 `to/from_dict|json` + `KIND_*` 帧词汇 + 容错解析：坏帧→空信封不抛）。**server**（`server/relay.py` 纯 server/store + shared，无 client 泄漏）：`Relay`①握手——`authenticate(payload)` 取 hello 帧 `access_key`→**仅按哈希查**（`auth.hash_access_key` sha256；新增 `generate/hash/verify_access_key`，`verify` 走 `compare_digest`）→拒 缺失/未知/revoked/过期(ISO8601 字典序比)/账号停用；**account_id 一律由 key 反查、绝不信客户端帧**（防越权）。②路由——`conns: account_id→[RelayClient]`，`route(account_id, env, process_id=)` **仅向该账号**在线连接转发（0=无人在线→调用方走展示缓存）。③心跳——`_on_frame` 收 heartbeat→刷 `last_heartbeat`+回 pong。④生命周期——`serve(ws)` 瘦壳（鸭子类型 accept/receive_json/send_json/close）：握手→`register`(标 online+`touch_access_key`)→try/finally 收尾 `unregister`(标 offline)，**崩溃也不会留 online 僵尸**；失败握手回 `hello_ack{ok:False}`+close(1008)、**不注册任何东西**；连上/掉线各发一条 `health` 事件上总线。`app.py` 注入 `relay=`（个人模式无 relay→accept+close 1008，不 404）。**client**（`client/relay.py` 纯 shared，无 server 泄漏）：`RelayConnector`——`hello()` 首帧带 key+机器名；`run_once` 握手(非 hello_ack→ConnectionError 重连；显式 ok=False→`RelayAuthError` 致命不重试)→读帧循环回 pong；`run()` **指数退避重连**（`backoff_delay` base*2^n 封顶），**仅在「握手成功且会话存活≥一个退避周期」才重置退避**——避免「认证后秒断」relay 被 1s 死循环锤；`RelayAuthError` 直接停（坏 key 重试无意义）。传输鸭子类型（注入 `connect`/`sleep`/`clock`），默认 `connect` 懒加载 `websockets`（client extra 新增，缺包不影响导入）。三关：①**Code Review**（subagent）——退避状态机/握手解析/register-unregister 配对/过期比较/容错 全核对；按建议修：握手要求 `ok is True`（非 ack→重连而非误判成功）、连上健康+ack 移入 try（publish 抛错不漏注册）、退避加最短会话守卫防秒断锤。②**安全**（subagent）——**修复 HIGH：client 控的 `process_id` 是注册表主键→可越账号覆盖他人机器行**：改为 `process_id = auth.key_id`（**服务端由 key 派生、账号绑定、不信客户端**），并在 `register_process` 加**所有权守卫**（既有行属他账号则拒绝改写、原样返回）双保险；确认 仅存哈希/account_id 服务端派生/route 不跨账号/无明文回显/无注入(仅 json)/掉线必清理。③**PM 验收**——对照 §8.5：①握手认身份 ②按账号路由 ③心跳+断线重连 ④安全(TLS 内传 key/仅哈希) 逐条达成。**live 接入延后**（真 wss 拨号 + Relay/ServerStore 注入 serve + 客户端发起的周期心跳定时器 = 团队上线，归 T7.1/live rollout，已标注；本任务交付契约+握手+pong+退避+路由，全 mock/单测）。**190 tests 全绿（+15）+ ruff 通过**。client⊥server 边界双向无泄漏。
- [ ] **T3.3** PWA：manifest + service worker + Web Push（VAPID）。
- [ ] **T3.4** Gate：危险动作分级 + 审批卡推送 + 批/驳闭环（§6.6）。
- [ ] **T3.5** 鉴权：用户登录 + access key 管理。
- [ ] **验收**：claude 想 `git push` 被拦 → 手机收推送 → 点批准 → 恢复。

## P4 — 决策回路 + 两向控制 + 能力层（⭐ 核心交互）
- [ ] **T4.1** Operator：精简输出 + 提案下一步动作。
- [ ] **T4.2** Auditor：§6.7 提示词骨架（两轴评分、从严默认、防自偏、verdict `pass/revise/reject/escalate`）。
- [ ] **T4.3** Decision Card + 详情页下钻（原始返回 / 逐行 diff，§6.3）。
- [ ] **T4.4** 自治档位（0/1/2/3，默认 1）。
- [ ] **T4.5** Operator Toolbelt（§4.7）：截屏(含鼠标渲染选项)/鼠标/键盘/管理员 shell；computer-use 会话侧执行。
- [ ] **T4.6** 手机下发任务 + 多会话 + 简报。
- [ ] **验收**：一步走完 Operator→Auditor→卡→你点→检查点→执行；全程手机可操作。

## P5 — 定义引擎（⭐ 秘方层）
- [ ] **T5.1** `definitions` + `definition_links` + `workflow_runs` 表。
- [ ] **T5.2** 混合式工作流引擎（固定骨架 + 每步 LLM/skill 驱动 + 卡审批点）。
- [ ] **T5.3** 事前注入工作区（CLAUDE.md / AGENTS.md / skill）。
- [ ] **T5.4** QA 标准驱动审阅。
- [ ] **T5.5** 数据库迁移（schema_version + 迁移器）。

## P6 — UI 编辑器 + 扩展口
- [ ] **T6.1** 手机/网页里增删改 工作流/技能/规范/QA。
- [ ] **T6.2** 定义导出/备份 + 可选 body 加密。
- [ ] **T6.3** `Notifier` 接口（飞书/Telegram/Bark/邮件）+ 插件 entry points。
- [ ] **T6.4** 仓库内置脱敏示例定义。

## P7 — 团队 / 总机模式
- [ ] **T7.1** Relay 总机：多本地进程出站接入 + 按账号路由。
- [ ] **T7.2** 管理员控制台：建用户 + 邀请（无自助注册）。
- [ ] **T7.3** access key：一机一张、可多张、哈希存、可吊销。
- [ ] **T7.4** 多租户隔离（每条记录绑 `account_id`）。
- [ ] **T7.5** 展示缓存（cache_sessions / cache_cards）供本地离线只读。
- [ ] **验收**：3 人各跑各的本地进程、共用一台服务器、互不见秘方与数据。

---
*维护：完成一步即勾选；阶段级里程碑见 [ROADMAP.md](ROADMAP.md)，设计依据见 [DESIGN.zh-CN.md](DESIGN.zh-CN.md)。*
