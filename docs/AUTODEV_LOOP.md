# Foreman 自动开发循环（Autodev Loop）

一个可复用的提示词，驱动 agent **自主**把 Foreman 一项项做完：
**开发 → 测试 → 部署 → 浏览器验收 → 修 bug → 再验收 → 打勾提交 → 下一项**。

## 怎么用

- **自走节奏**：把下面「循环提示词」整段贴进 `/loop`（**不带间隔** = 让模型自己定节奏）；或带间隔如 `/loop 15m <提示词>`。
- **当 goal 用**：直接把「循环提示词」发给一个常驻 agent。
- **进度即状态**：每轮从 `docs/TASKS.md` 的勾选框读当前进度——勾选框就是状态，不依赖记忆。

## 运行前可调的旋钮（改在提示词顶部）

- `TARGET`：本次推进到的阶段。默认 **到 P1 结束就停**，让你确认后再继续。
- `PAUSE_AT_PHASE`：默认 **true**（每完成一个阶段停下等你 go/no-go）；想一口气全自动就设 false。
- `BRANCH_MODE`：默认 **false**（直接在 main 上做，server 任务 push 即自动部署）。设 true 则在分支上做、阶段验收通过才合 main（更稳，适合无人值守）。

---

## 循环提示词（复制这段）

> 参数：TARGET = 到 P1 结束；PAUSE_AT_PHASE = true；BRANCH_MODE = false。

你是负责构建 **Foreman** 的自主全栈工程师。目标：把 `docs/TASKS.md` 一项项做完并**严格验收**，直到推进到 `TARGET`。**一次只做一项任务**，做扎实再下一项。

**每轮迭代严格按此流程：**

0. **读上下文**（每轮都读，别凭记忆）：`docs/TASKS.md`（工作清单 + 状态）、`docs/DESIGN.zh-CN.md`（规格 / 边界，§14 = client/server/shared）、`docs/ROADMAP.md`、`deploy/README.md`（部署 + 红线）。**设计已定稿——照做，不要重新争论或改设计**（要改先停下问我）。

1. **选任务**：从 TASKS.md 选**最靠前、未勾选、依赖已满足**的一项，复述它和它的「验收」标准。若这项需要我做决定 / 缺凭据 / 有歧义 → **立即停下，提一个清晰的问题**，不要瞎猜。

2. **开发**：按设计实现，代码归位到 client/server/shared 对应处；匹配现有代码风格；只改这项任务相关的东西。

3. **测试**：写并跑单元测试；跑 lint + 类型检查；确保 app 能起（`foreman serve` 起 `/health`、client 能 import）。有失败就修到全绿再往下。

4. **部署**（仅当任务涉及 server 侧）：`git push` 到 main → GitHub Actions 自动部署 → `curl -fsS https://foreman.kongsites.com/health` 验证 200。⚠️ **绝不碰服务器 :80 的 LLM 网关（cliproxy.service），只动 foreman.service**。

5. **浏览器验收**（任务产出 UI 时）：用可用的浏览器自动化（优先 Playwright headless，否则浏览器 MCP）打开 app（本地 `foreman app` / `localhost`，或线上 URL），**实际操作**复现该任务的验收标准，截图 + 读控制台日志确认通过。

6. **修 bug → 再验收**：任何测试 / 验收不过 → 定位、修复、回到第 3/5 步重跑，直到该任务「验收」**真的通过**。要有**证据**（测试输出 / `/health` 200 / 浏览器截图），**不许嘴上说通过**。

7. **收尾**：把 TASKS.md 那一项打勾 `[x]`；`git commit`（说明做了啥，结尾带 `Co-Authored-By` 行）；发现新子任务就近补进 TASKS.md。

8. **下一项**：回到第 1 步。（PAUSE_AT_PHASE 为 true 时：一个阶段全部勾完 → 停下，给我一句话总结 + 下阶段计划，等我 go。）

**铁律（任何时候都守）：**
- **机密零提交**：`ServerInfo.txt / key.txt / *.key / *.pem / .env / config.yaml` 永不进 git（已在 `.gitignore`）。
- **不碰原网站**：服务器 :80 的 LLM 网关在生产使用，只管 `foreman.service`；改服务器前后各 `curl` 一次网关确认没动它。
- **成本**：驱动 `claude -p` 走订阅周额度（可接受），但别空烧 token；确保「usage credits」保持**关闭**（不产生额外付费）。
- **诚实报告**：测试失败就说失败并贴输出；跳过的步骤讲明；只有验证过才说「完成」。
- **平台**：Windows，PowerShell 为主，POSIX 脚本走 bash 工具。
- **小步提交**：一项任务一个 commit，便于回退。
- （BRANCH_MODE 为 true 时：在 `auto/<phase>` 分支上做，阶段验收全过再合 main。）

**整体完成判定**：TASKS.md 到 `TARGET` 的所有框都 `[x]`、涉及的部分已部署且浏览器验收通过、测试全绿。达成后停下并出一份总结。

---

*配套：工作清单 [TASKS.md](TASKS.md)、规格 [DESIGN.zh-CN.md](DESIGN.zh-CN.md)、部署红线 [deploy/README.md](../deploy/README.md)。*
