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
- [ ] **T2.3** 一键回退：恢复到某检查点（含删后建文件）+ 先给当前打点（redo）。
- [ ] **T2.4** Hook 接收端 `POST /hooks`（PreToolUse/PostToolUse/Stop/Notification）。
- [ ] **T2.5** Git watcher（diff/commit）+ process/idle（psutil）→ 刷新 `last_progress_at`。
- [ ] **T2.6** Supervisor 看门狗（**全局唯一**）：池健康状态机 + 廉价巡检 + 可疑时升级 LLM + 恢复 playbook（§4.1/§5.6）。
- [ ] **T2.7** Reviewer：检查点处 diff+目标 → 结构化评审。
- [ ] **验收**：任务完成自动产出评审；任一步可一键回退；卡死/崩溃能被发现并恢复或升级。

## P3 — 手机面 + 审批 + server 起步
- [ ] **T3.1** server 库：accounts / access_keys / process_registry（占位 invites / cache）。
- [ ] **T3.2** `wss /relay`：出站长连 + access key 握手 + 心跳 + 重连 + 按账号路由（§8.5）。
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
