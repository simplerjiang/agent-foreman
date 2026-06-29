# AGENTS.md — Foreman 多 Agent 协作规约

本项目由多个 agent 协作：**开发 agent**（codex / claude-code，负责开发新功能与修 bug）和 **E2E agent**（负责端到端点击测试，见 `~/.claude/skills/foreman-e2e`）。
**GitHub Issue 是跨 agent 协作的唯一中枢**（仓库 `simplerjiang/agent-foreman`，`gh` 已认证）。所有"待做 / 待测 / 待复验"都走 Issue，**不靠本地文件、不靠口头记忆**。

> 任何会改文件 / 关闭 / 认领 / 评论的 `gh` 操作前，**先 `gh issue view <n>` 确认当前状态**，避免和其它 agent 抢同一条。

---

## 标签约定（label）

| label | 含义 |
|---|---|
| `e2e` | E2E 测试发现的问题（缺陷/可用性/体验） |
| `needs-e2e` | **需要 E2E 验证**的改动：① 新功能上线待测；② bug 已修复待复验 |
| `bug` / `enhancement` | 缺陷 / 缺功能·提升 |

`assignee`（认领人）= **谁正在处理这条 issue**。缺标签时先建：
```bash
gh label create e2e        --description "Found via foreman-e2e end-to-end testing" --color 5319e7 2>/dev/null || true
gh label create needs-e2e  --description "Change awaiting E2E verification" --color 0e8a16 2>/dev/null || true
```

---

## 一、开发 agent：开发新功能后 → 必须开「review issue」提醒 E2E 测试

功能代码落地后，**必须**创建一个 review issue，否则 E2E agent 不知道要测：
```bash
gh issue create --label needs-e2e --title "[Review] <功能名>" --body-file <body.md>
```
正文写清三件事：① **改了什么**（涉及的页面/接口/文件）；② **入口 / 怎么操作**（点哪里、什么前置条件）；③ **验收点**（期望行为，可勾选）。

## 二、开发 agent：修一个 issue → 先「领取」防重复，修完按「第几次修复」收尾（二次修复必须当场 E2E 验过再关）

1. **先领取再动手**（防止多个 agent 重复修同一条）：
   ```bash
   gh issue view <n>                       # 看是否已有 assignee 或"认领"评论
   # 没人认领 → 领取：
   gh issue edit <n> --add-assignee @me
   gh issue comment <n> --body "认领修复中（codex，<YYYY-MM-DD HH:MM>）"
   ```
   - **已被他人认领（有 assignee / 认领评论）→ 不要重复修**，换一条没人领的。
2. 改代码、自测。
3. **修复后怎么收尾，取决于这是第几次修复**——先 `gh issue view <n>` 看历史判断：

   **判定「二次（及以上）修复」**：issue 历史里命中任一即是——
   - 被 E2E `reopen` 过；或
   - 有 E2E 复验评论判为「不通过 / 部分通过 / 仍未修」；或
   - 已存在 ≥1 条更早的「已修复」评论（即这次是回炉重修）。
   都不命中 = **首次修复**。

   - **首次修复** → 标注已修复 + 打 `needs-e2e` + 关闭，交给 E2E **异步**复验：
     ```bash
     gh issue comment <n> --body "已修复：<改动摘要 / 关键文件 / commit SHA>"
     gh issue edit <n> --add-label needs-e2e
     gh issue close <n> --reason completed
     ```
     关闭的 issue 带 `needs-e2e`，仍会被 E2E agent 检索到做**复验**；复验不通过时 E2E 会 reopen（→ 下次即按「二次修复」处理）。

   - **二次（及以上）修复** → **必须当场 E2E 复验通过才能关；没当场验过，不许 `close`**：
     1. 改完代码后，**当场用 `foreman-e2e` skill 在 main / 打包 exe 上真机复验**本 issue 的验收点（不是只跑单测、不是只读代码核验）。
     2. **当场复验通过** → 评论「已修复 + 当场 E2E 复验通过：<🖱️实测现象 / 截图名 / commit SHA>」→ 去 `needs-e2e` → 关闭：
        ```bash
        gh issue comment <n> --body "已修复 + 当场 E2E 复验通过：<🖱️实测现象 / 截图名 / commit SHA>"
        gh issue edit <n> --remove-label needs-e2e
        gh issue close <n> --reason completed
        ```
        （已当场验过 → 去掉 `needs-e2e`，不再甩给异步复验。）
     3. **没当场验过 / 没验通过** → **保持 open、保留 `needs-e2e`**，评论「已改待当场复验：<现状 / 卡点>」，**禁止 `close`**。把它留在队列里，直到有人当场验过为止。

## 三、E2E agent：定期读「待 review」的 issue 并测试

```bash
gh issue list --label needs-e2e --state all --limit 50
```
- `state open` 带 `needs-e2e` = **新功能待测**；`state closed` 带 `needs-e2e` = **已修 bug 待复验**。
- 按 `foreman-e2e` skill 实测（混合：并行代码核验 + 串行真机点击）。复验后：
  - **通过** → 去掉 `needs-e2e`（`gh issue edit <n> --remove-label needs-e2e`）+ 评论"E2E 复验通过：<证据/截图>（<日期>）"。新功能 review issue 若全部验收点通过则 `gh issue close <n>`。
  - **不通过 / 仍未修** → `gh issue reopen <n>`（已关闭的）或评论说明现状，**保留 `needs-e2e`**，更新现状证据。被打回的这条，开发 agent 下次再修时即属「**二次修复**」，按 §二 必须**当场 E2E 验过再关**。
- E2E 发现的**新问题** → 按 skill 新开 `e2e` issue（标题 `[E2E][严重度] …`，正文含 🖱️实测 + 🔎代码 双证据 + 验收勾选框）。

---

## 四、开发 agent：每个 PR 自增版本号（已进入 Prod，从 v1.0.0 起）

项目已是**生产版本**。**每个 PR 都必须把版本号自增一格**——两个 PR 不允许共用同一版本号。

- **自增规则**：`+0.0.1`（patch 加一），**每位满 10 进一位**（每位取值 0–9，到 10 即归 0 并向高位进一）：
  `1.0.0 → 1.0.1 → … → 1.0.9 → 1.1.0 → … → 1.9.9 → 2.0.0`。
- **只改这一处「单一来源」**——`src/foreman/__init__.py` 的 `__version__ = "x.y.z"`。其余全部**自动派生**，不要再手改：
  - `pyproject.toml` 用 `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "foreman.__version__"}` 动态读取；
  - `/health`、`/api/*`、PWA 侧栏/启动页全部从 `__version__`（经 `/health`）取值，**前端不得硬编码版本号**。
- **每次改版本号都必须注明本次更新内容，并能看到历史更新记录**：同步更新 `README.md` 的 `Version Information / 版本信息` 段落、`docs/VERSION_HISTORY.md`，以及 exe 控制台里的「Version / 版本」页面文案。必须写清本版本改了什么，并保留至少最近几个版本的历史记录，不能只显示最新版本。`__version__` 仍是唯一包版本来源；README/页面/历史文件只维护给人看的中英文更新说明。
- **提交前自查**：相对 `origin/main` 的当前版本**正好 +0.0.1**（含进位）：
  ```bash
  git show origin/main:src/foreman/__init__.py | grep __version__   # 看 main 当前版本
  grep -n '__version__' src/foreman/__init__.py                     # 你的新版本（应 = main + 0.0.1）
  ```
- **PR 合并即发布**：合并到 `main` 后，CI（`.github/workflows/deploy.yml`）在测试门禁通过后**并行**做两件事——① `release` job 构建 Windows exe、发 `v<__version__>` 的 GitHub Release（已存在则跳过）；② `deploy` job 把该 commit 部署到 `foreman.kongsites.com`。已连着的 PWA 会轮询 `/health` 检测到新版本并提示刷新。所以**版本号不递增 = 不会发新 Release**（release job 按 tag 幂等跳过）。

---

## 通用纪律
- **二次修复不许裸关**：被 E2E 打回过（reopen / 复验不通过）的 issue，再修时**必须当场跑 `foreman-e2e` 复验通过**（附 🖱️实测证据）才能 `close`；没当场验过只能留 open + `needs-e2e`，**禁止再次「裸关闭甩给异步复验」**。详见 §二。
- **不凭印象操作 issue**：领取要看清没被领、关闭/复验通过都要**附证据**（commit SHA / 实测现象 / 截图名）。
- **一个问题一条 issue**，便于独立认领与关闭。
- **每个 PR 必须自增版本号**（`+0.0.1`，满 10 进位；**只改 `src/foreman/__init__.py` 的 `__version__` 这一处**，其余自动派生）——合并即触发 Release exe + 部署线上站，详见 §四。
- **版本号改动必须带更新说明和历史记录**：同 PR 内更新 README、`docs/VERSION_HISTORY.md` 和 exe 内版本页的中英文说明，不能只 bump `__version__`，也不能只展示最新版本而看不到历史版本更新记录。
- 标题里**禁用半角双引号 `"`**（会破坏 `gh ... --title "…"` 的 shell 解析）——用 `「」`。
- 开发涉及破坏性操作（删数据、改用户配置、push/merge/deploy）需谨慎并按项目门禁；E2E 测试只用**只读、无害**指令驱动 agent。

---

## 一图流

```
开发 agent ──新功能──▶ 开 [Review] issue (needs-e2e, open) ──────────────┐
开发 agent ──修 bug──▶ 认领(assignee) → 修 → 评论"已修复"                  │
        ├─首次修复──▶ 加 needs-e2e → close ──────────────────────────────┤
        └─二次+修复─▶ 当场跑 foreman-e2e ┬─验过──▶ 去 needs-e2e + close   │
                                         └─没验过─▶ 留 open + needs-e2e   │
                                                    (禁止 close)          │
                                                                          ▼
                                                  E2E agent 定期读 needs-e2e
                                                  ├─ 复验通过 → 去 needs-e2e / close review
                                                  └─ 不通过  → reopen(→下次=二次修复) / 新开 e2e issue
```
