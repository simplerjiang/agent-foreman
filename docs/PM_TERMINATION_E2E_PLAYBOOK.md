# PM 终结判定（#39）E2E 深度验收流程书

> 给 **E2E 执行方**（codex / 另一个 claude）用的**真机深度验收**清单。配套设计书：[`docs/PM_PLAN_TERMINATION_DESIGN.md`](./PM_PLAN_TERMINATION_DESIGN.md)（验收口径 §7 / 基调 §0.5 / 对照表 §11.1）。
> 真窗口点击标准动作以 `~/.claude/skills/foreman-e2e` skill 为**单一来源**；本文件规定**这次要把哪些潜在 bug 主动逼出来、每条用什么双证据判定、什么门槛才允许关 #39**。
>
> **核心纪律：不放过、不假设、不把风险留给用户。** 每条断言都要 **🖱️ UI 现象 + 🔎 DB/日志**两路证据；happy-path 过了不算过，**对抗用例（逼复读 / 逼挂死 / 逼取消 / 逼边界）必须逐条真机触发**。

---

## 0. 为什么要深测（不是点一遍就算）

#39 的根因链：规划期模型把同一份计划 JSON **复读 ~47 次** → 旧解析器（首`{`→末`}`）在拼接串上崩 → 降级**空计划** → 会话**假 `running` 卡死**、界面无任何失败提示。

本轮改了 **4 个高风险面**（plan / `PMToolLoop` / review / compact），引入了**强制工具 A1、调用级看门狗、三终态、早切、UI 失败可见**。**任何一面没被真机压到，都是潜在的线上挂死**。所以本流程书的用例是**按"能让它再挂一次"的思路设计的对抗测试**，不是功能走查。

> 💬 **二次修复**：#39 被 E2E 打回过，按 `AGENTS.md` §二**禁止裸关**——必须**当场逐条点过 + 双证据**才能 close；任一对抗用例没逼出预期行为，就留 open + `needs-e2e`。

---

## 1. 前置条件（做错任何一条 → 用例假失败，必须先校准）

| 项 | 要求 | 校验方式 |
|---|---|---|
| **被测物** | 本轮**新打包 `dist/foreman.exe`**（main 已合 PR #100）；或源码 `foreman app`（加载 `.env` key） | exe 的修改时间 = 本次打包；`foreman.exe version` 对得上最新 commit |
| **PM 大脑（正向）** | `provider=openai` / `model=gpt-5.5` / `base_url=https://api.kongsites.com/v1` / `transport=ws` | 设置页核对；**填错模型名（如 `gpt-4o`）会让所有 LLM 流真失败** |
| **PM 大脑（对抗）** | 备一个**不存在模型名**、一个**超慢/会断**的配置，用于 B 组逼失败 | 仅在 B 组临时切，测毕**改回 gpt-5.5** |
| **密钥** | 在 `dist/.env`，**严禁打印/截图/回显** 到报告或 issue | 报告出现 key 即作废 |
| **坐标/点击** | computer-use 坐标 **1:1**，原子 PowerShell 单击，避免连点漂移 | 见 foreman-e2e skill |
| **压力测试库** | 准备一个**文件多、目录深、有歧义**的真实小仓库（≥30 文件），用于逼规划器多轮取证 | 见 §1.1 |

### 1.1 压力测试库（让规划器"想多读、容易复读"）
正向 happy-path 用普通小任务即可；但**逼复读/逼多轮取证**需要一个"信息密度高、要求略含糊"的目标：
- 一个 30~80 文件、含多模块/多语言、README 信息不全的仓库（可直接指向本 Foreman 仓的 `src/foreman/`）。
- 任务措辞故意**含糊+大范围**，例如「审阅这个代码库，找出所有可能的并发与状态机风险，给出修复计划」——历史上正是这类"大而含糊"的 plan 容易诱发模型反复改写计划 JSON。

---

## 2. 只读取证工具箱（🔎 这一路证据是本次深测的关键，别只靠截图）

> **路径**：从 exe 跑时 DB=`dist/foreman.db`、日志=`%TEMP%\foreman-app.log`；从源码跑时 DB=仓库根 `foreman.db`、日志走终端。**全部只读，不写库、不删数据。**

**(a) 会话终态（验"不再假 running"）**
```bash
sqlite3 dist/foreman.db "SELECT id,status,substr(goal,1,40),updated_at FROM session ORDER BY updated_at DESC LIMIT 8;"
```
- 期望终态 ∈ `done|failed|stalled|cancelled`；**绝不能**停在 `planning`/`running` 而界面/进程已无活动。

**(b) 事件流（验"只产一次计划、无第 2 个对象、工具顺序对、reason 落库"）**
```bash
sqlite3 dist/foreman.db "SELECT ts,type,source,substr(payload_json,1,160) FROM event WHERE session_id='<SID>' ORDER BY ts;"
```
- 查 `tool_pre`/`tool_post` **成对且 tool_pre 在 pm_plan 之前**；查计划/派发事件**只出现一次**；查含 `reason` 的 `error` 事件。
```bash
sqlite3 dist/foreman.db "SELECT type,payload_json FROM event WHERE session_id='<SID>' AND (type LIKE '%error%' OR payload_json LIKE '%reason%');"
```

**(c) 日志（验"看门狗真的开火、ws 真的关"）**
```powershell
Get-Content $env:TEMP\foreman-app.log -Tail 120   # 找 LLMStalledError / wall_clock / no_progress / repeat / ws close / cancel
```

> **双证据原则**：每条用例的 ❌负向断言，凡能从 DB/日志佐证的，**必须**附上对应 SQL/日志片段（脱敏），不能只写"我看着没复读"。

---

## 3. 深度验收用例（A–E 五组，逐条触发 + 双证据 + 负向断言 + 阈值）

> 记法：**前置 → 操作 → 🖱️UI期望 → 🔎DB/日志期望 → ❌MUST NOT → ⏱阈值 → 📷截图**。截图落 `e2e-test-report/screenshots/`，命名 `UC<n>-<帧>.png`。

### A 组 — 根因与根治（#39 是否真死透）

#### UC-A1 ▶ 大而含糊任务：plan 收口一次、绝不复读、绝不空计划
对应 §7 **A/B/F** + **P1 验收（合）**。**本组第一主判据。**
- 前置：gpt-5.5 + §1.1 压力库 + 含糊大范围任务。
- 操作：发任务，全程盯规划→计划→派发。
- 🖱️UI：规划**秒级~十几秒收口**；产出**一份真实计划**（summary/agent/instruction/todo 齐全）；正常派发 subagent。
- 🔎DB：事件流里**计划相关事件只一条**；`session.status` 顺利进入 `running`/`done`，无中途回退空计划的痕迹；**无第 2 个计划对象的派生事件**。
- ❌MUST NOT：出现 `"PM tool loop reached max rounds…"` 的**空 fallback 计划**；出现同一计划被重复渲染/派生；规划卡 > 阈值仍 running。
- ⏱：正常收口 **≤ 30s**（压力库可放宽到 ≤ 60s，但必须收口）。
- 📷：规划中、计划产出、派发后 subagent。

#### UC-A2 ▶ 强制提交轮真的开火（最后一轮 `submit_plan` 单工具）
对应 §0.5-1/§7 A 的机制级证据。
- 操作：在 UC-A1 同一会话上，用 §2(b) 查事件流。
- 🔎DB/日志：能看出**取证轮（auto）→ 提交轮**的推进；提交以**一次工具调用**终结，**不是**靠文本里的 `final_plan` 收尾。
- ❌MUST NOT：日志里出现"原生路径却用文本 final_plan 终结"；提交轮产生 >1 次 `submit_plan`。
- 📷：事件流截图（标注单次 submit）。

#### UC-A3 ▶ 取证轮工具未被掐死（§0.5-6 不破坏既有能力）
- 前置：任务**必须先读文件才能规划**（如「对比 `loop.py` 与 `pm_agent.py` 的解析路径再给计划」）。
- 🖱️UI / 🔎DB：规划期能看到 **`read_file`/`search_repo` 的 tool_pre/tool_post**，之后才出计划。
- ❌MUST NOT：强制 `submit_plan` 把取证轮也掐了（规划期一个取证工具都没调就直接逼出计划）。
- 📷：取证工具调用反馈。

#### UC-A4 ▶ Schema 边界：超长/超量计划被夹紧而非崩
对应本轮 Medium-2 加固（`validate_final_plan` 夹 §5 maxLength/maxItems）。
- 操作：诱导模型产出**很长的 instruction / 很多 todo**（任务里要求"尽量详尽、列出尽可能多的步骤"）。
- 🖱️UI / 🔎DB：界面/库里的计划字段被**截断到上限**（summary≤600、instruction≤6000、todo≤12 条等），**应用不崩、会话正常**。
- ❌MUST NOT：超长字段导致渲染错乱 / 会话失败 / 写库异常。

### B 组 — 失败要响、要可见（看门狗三触发 × 三终态 × reason）

> **这组是"把潜在挂死主动逼出来"的核心**。三种触发要**分别**逼到，每种都验 UI 红态 + reason + DB 终态。

#### UC-B1 ▶ LLM 直接失败（坏模型）→ `failed` + reason 可见
- 操作：临时把模型改成**不存在的名字**，发任意规划任务。
- 🖱️UI：会话落**红色失败态**，错误节点**显示 reason 文案**（人话），有**重试**入口。
- 🔎DB/日志：`session.status='failed'`（或 `stalled`）；event 有 `reason`；日志有对应异常。
- ❌MUST NOT：**永久 `running`**；空白无原因；无重试。
- 测毕：**模型改回 gpt-5.5**。

#### UC-B2 ▶ 停滞/无进展（慢或半截流）→ `stalled` + reason
- 操作：制造"只吐 delta、迟迟不收尾"的情形（用超慢/会断的对抗配置，或在弱网下发大任务）。
- 🖱️UI / 🔎DB/日志：在 `T_wall`(60–90s) / `T_stall`(15–30s) 内落 **`stalled`** + reason（墙钟/无进展）；不无限转。
- ⏱：**不超过 ~90s** 必须有终态。
- ❌MUST NOT：超过墙钟仍 running 且无 reason。

#### UC-B3 ▶ 三类 reason 文案都对得上（wall_clock / no_progress / repetition / stalled）
- 操作：把 B1/B2（必要时多触发几次）拿到的 reason，与前端 `friendlyReason` 文案逐一比对。
- 🖱️UI / 🔎DB：每类底层 reason 都渲染成**对应的人话**，中英文都不漏键（不出现 `reasonXXX` 原始 key 或空串）。
- 📷：每类 reason 的失败卡片各一张。

### C 组 — Stop 真取消（三相位）+ 重试自愈

#### UC-C1 ▶ 规划中 Stop → ws 实断、会话立停
对应 §7 E + T2.3（codex 复验重点）。
- 操作：会话在**规划中（live）**时点「取消会话/Stop」。
- 🖱️UI：会话**立即** `cancelled`。
- 🔎DB/日志：`session.status='cancelled'`；**取消后不再有该会话的新规划/派发事件**；日志可见 ws 关闭/CancelledError。
- ❌MUST NOT：只改标签、底层 ws 仍在跑（取消后还冒新事件）；残留僵尸进程。

#### UC-C2 ▶ 派发/运行中 Stop、复核中 Stop（另两个相位）
- 操作：分别在 **subagent 运行中**、**review 阶段**点 Stop。
- 期望同 UC-C1：**真停、无后续事件**。
- 📷：三相位各一组（停前 live / 停后无新事件）。

#### UC-C3 ▶ 终态重试能自愈
- 操作：对 B 组的 `failed`/`stalled` 会话点**重试**（gpt-5.5 下）。
- 🖱️UI / 🔎DB：重试**真的重跑**并能正常出计划/派发（retry 覆盖 failed 和 stalled）。
- ❌MUST NOT：重试无反应 / 重试仍卡死。

### D 组 — UX 有生命 + 跨会话不串台

#### UC-D1 ▶ 规划期计时 + todo 渐显 + 工具反馈
对应 §7 E（T2.1/T2.2）。
- 🖱️UI：`pm-status` **「已 N 秒」每秒在动**（无新内容也不冻屏）；todo **逐条渐显**（0→N）；subagent 启动与 tool_pre/tool_post 有反馈。
- ⏱：计时**每 1s 自增**；todo 渐显可见（不是整块瞬现）。
- 📷：计时中、todo 渐显中间帧、工具反馈。

#### UC-D2 ▶ 跨会话快速切换：todo / 计时 / 状态都不串台
对应 codex Low（`TodoPanel` 按 session key 重挂载）+ 防 `PmElapsed`/状态错位。
- 操作：会话 A（有 todo、在计时）↔ 会话 B（不同 todo/状态）**快速来回切 3~5 次**。
- 🖱️UI：每个会话**只显示自己的** todo/计时/状态；**不从上一个会话的进度继续重放历史 todo 动画**；满载会话直接全显。
- ❌MUST NOT：A 的 todo 在 B 重放；计时数字串台；状态标签错挂。

### E 组 — 一处修复四处覆盖（review / compact 同治）

#### UC-E1 ▶ compact 路径在压力下不挂死
对应 §0.5-5 + P0 验收（合）③。
- 操作：把一个会话养到**长上下文触发 compact**（或手动压缩）。
- 🖱️UI / 🔎DB：compact 能在**第一个合法对象收口**、不复读不挂死；异常同样落终态 + reason（与 plan 路一致）。
- ❌MUST NOT：compact 静默挂起 / 假 running / 崩库。

#### UC-E2 ▶ review 路径同治
- 操作：跑一轮会触发 PM review 的任务。
- 期望同 E1：早切收口、失败可见、不挂死。

---

## 4. 通过判据（高门槛）& 收尾（关 #39 的硬条件）

**只有当下面全部成立，#39 才允许 close：**
- [ ] A 组全过：大而含糊任务**收口一次、无空 fallback、无第 2 计划对象**（UC-A1）；强制提交轮单工具终结（A2）；取证轮工具未被掐（A3）；超界计划被夹紧不崩（A4）。
- [ ] B 组全过：**failed 与 stalled 都真机逼到过**，三类 reason 文案都对（B1/B2/B3）；**无一例永久 running**。
- [ ] C 组全过：**三相位 Stop 都真断 ws、无后续事件**（C1/C2）；重试能自愈（C3）。
- [ ] D 组全过：计时/渐显/反馈在场（D1）；**跨会话不串台**（D2）。
- [ ] E 组全过：review 与 compact 同样早切收口、失败可见（E1/E2）。
- [ ] **每条都附双证据**（🖱️截图 + 🔎对应 SQL/日志片段，脱敏）。

**收尾动作（按 `AGENTS.md` §二，#39 属二次修复）：**
1. 回写设计书：§6.5 勾 `P0 验收（合）`（行 213）/`P1 验收（合）`（行 223）/`T3.3`（行 235）；§7 勾 **E/G**；§11.1 把 E/G 由 ⏳ 改 ✅，附截图名/SHA。
2. 关 #39（**当场验过才能关**）：
   ```bash
   gh issue view 39
   gh issue comment 39 --body "已修复 + 当场 E2E 深度复验通过：A/B/C/D/E 五组逐条双证据（截图名 + DB/日志片段 + commit SHA）"
   gh issue edit 39 --remove-label needs-e2e
   gh issue close 39 --reason completed
   ```
3. PR #100 关联的 `needs-e2e` review issue（#97/#98 等）：通过则去 `needs-e2e` + 评「E2E 复验通过：<证据>」。

**任一项不通过：**
- 保持 / `gh issue reopen 39`，**保留 `needs-e2e`**，评论现状 + 双证据，**禁止裸关**。
- 新发现的问题 → 新开 `e2e` issue（标题 `[E2E][严重度]…`，正文 🖱️+🔎 双证据 + 勾选框；标题用 `「」`，禁半角双引号）。

---

## 5. 分工建议

| 角色 | 偏重 | 做什么 |
|---|---|---|
| **codex** | 🔎 代码/数据核验 + 机制级佐证 | 核 §11.1 A–F；查 event 流证明"单次 submit_plan / 无第 2 对象 / 取证轮在场"；从日志佐证看门狗三触发；对 A4 schema 夹紧做代码侧确认 |
| **另一个 claude** | 🖱️ 真机对抗点击 | 按 §3 在 exe 上**逐组逼**（含坏模型/慢流/三相位 Stop/跨会话切换）；截图 + 出中文报告落 `e2e-test-report/` |

> 两边共同纪律：只读无害指令驱动；**不打印密钥**；操作 issue 前先 `gh issue view`、附证据（SHA/实测/截图名/SQL 片段）。

---

## 6. 一图流

```
打新 exe(main 含 #100) → 设 gpt-5.5 + ws + 压力库 ──┐
                                                     ▼
 A 根治: A1 收口一次·无空计划·无第2对象 / A2 强制单工具 / A3 取证轮在场 / A4 边界夹紧
 B 失败可见: B1 failed / B2 stalled(墙钟·无进展) / B3 三类 reason 文案 ← 主动逼失败
 C Stop 真取消: C1 规划中 / C2 派发·复核中 / C3 重试自愈 ← 三相位真断 ws
 D UX: D1 计时·渐显·反馈 / D2 跨会话不串台
 E 四处覆盖: E1 compact / E2 review 同治
                                                     ▼
 每条 = 🖱️UI + 🔎DB/日志 双证据；五组全过 + 双证据 → 勾 §6.5/§7 E·G + 当场关 #39
 任一不过 → reopen / 留 open + needs-e2e + 新开 e2e issue（禁止裸关）
```
