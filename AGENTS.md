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

## 二、开发 agent：修一个 issue → 先「领取」防重复，修完「标注已修复并关闭」

1. **先领取再动手**（防止多个 agent 重复修同一条）：
   ```bash
   gh issue view <n>                       # 看是否已有 assignee 或"认领"评论
   # 没人认领 → 领取：
   gh issue edit <n> --add-assignee @me
   gh issue comment <n> --body "认领修复中（codex，<YYYY-MM-DD HH:MM>）"
   ```
   - **已被他人认领（有 assignee / 认领评论）→ 不要重复修**，换一条没人领的。
2. 改代码、自测。
3. **修复后：标注已修复 + 打 `needs-e2e` 供复验 + 关闭**：
   ```bash
   gh issue comment <n> --body "已修复：<改动摘要 / 关键文件 / commit SHA>"
   gh issue edit <n> --add-label needs-e2e
   gh issue close <n> --reason completed
   ```
   - 即"标注已修复并关闭"。关闭的 issue 带 `needs-e2e`，仍会被 E2E agent 检索到做**复验**；复验不通过时 E2E 会 reopen。

## 三、E2E agent：定期读「待 review」的 issue 并测试

```bash
gh issue list --label needs-e2e --state all --limit 50
```
- `state open` 带 `needs-e2e` = **新功能待测**；`state closed` 带 `needs-e2e` = **已修 bug 待复验**。
- 按 `foreman-e2e` skill 实测（混合：并行代码核验 + 串行真机点击）。复验后：
  - **通过** → 去掉 `needs-e2e`（`gh issue edit <n> --remove-label needs-e2e`）+ 评论"E2E 复验通过：<证据/截图>（<日期>）"。新功能 review issue 若全部验收点通过则 `gh issue close <n>`。
  - **不通过 / 仍未修** → `gh issue reopen <n>`（已关闭的）或评论说明现状，**保留 `needs-e2e`**，更新现状证据。
- E2E 发现的**新问题** → 按 skill 新开 `e2e` issue（标题 `[E2E][严重度] …`，正文含 🖱️实测 + 🔎代码 双证据 + 验收勾选框）。

---

## 通用纪律
- **不凭印象操作 issue**：领取要看清没被领、关闭/复验通过都要**附证据**（commit SHA / 实测现象 / 截图名）。
- **一个问题一条 issue**，便于独立认领与关闭。
- 标题里**禁用半角双引号 `"`**（会破坏 `gh ... --title "…"` 的 shell 解析）——用 `「」`。
- 开发涉及破坏性操作（删数据、改用户配置、push/merge/deploy）需谨慎并按项目门禁；E2E 测试只用**只读、无害**指令驱动 agent。

---

## 一图流

```
开发 agent ──新功能──▶ 开 [Review] issue (needs-e2e, open) ──┐
开发 agent ──修 bug──▶ 认领(assignee) → 修 → 评论"已修复"      │
                        → 加 needs-e2e → close ────────────────┤
                                                               ▼
                                          E2E agent 定期读 needs-e2e
                                          ├─ 复验通过 → 去 needs-e2e / close review
                                          └─ 不通过  → reopen / 新开 e2e issue
```
