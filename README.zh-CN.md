# Foreman 🦺（工头）

**一个自托管的「项目经理 / 工头」Agent，替你管你本地的编码 Agent。**

Foreman 是常驻在你 PC 上的守护进程，像 PM / 工头一样盯着你本地的 AI 编码 Agent
（**Claude Code** 和 **Codex CLI**）：**监控**它们、**调度**它们干活、用**你自己的 LLM** 自动
**审阅**产出，并通过自托管 **PWA + Web Push 向你手机汇报** —— 当 Agent 要做危险动作时把审批卡
推到你手机，你离开电脑时也能给它下发新任务。

> 灵感来自 [Cteno](https://github.com/zalan159/cteno-community)，但**刻意收窄**为「今晚就能跑起来」
> 的单机 MVP。完整设计见 **[docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md)**。

---

## 为什么需要它

用 CLI Agent 干活时有两个痛点：

- **你离开电脑时**：Agent 卡住 / 跑偏 / 撞到「需要确认」的网关就停摆。
- **你回到电脑时**：要翻日志、看 diff，才能想起它刚才干到哪。

Foreman 让一个 LLM 驱动的 PM 7×24 在环：正常推进时**自动审阅放行**，遇到风险点**暂停并推手机**，
你在任何地方都能一键**批准 / 驳回 / 改方向**。

## 它能做什么

| | |
|---|---|
| 👀 **监控** | 实时盯 Claude Code（经 hooks）和 Codex（经输出 + git）。 |
| 🎛️ **调度** | 在工作区拉起并引导 `claude` / `codex` —— PC 或手机都行。 |
| 🔍 **审阅** | 把 diff 交给**你自己的 LLM API** 做结构化评审（风险、建议）。 |
| 🚦 **网关** | 把动作分级 安全 / 需策略 / 需审批；危险的等你点头。 |
| 📱 **汇报** | 把简报与审批卡推到自托管 PWA（iOS/Android），Web Push。 |

## 架构（一分钟版）

```
PC：PM Core(Python) ── 驱动 ──▶ claude -p / codex exec   （你的代码工作区）
        │  ▲                          │
        │  └── hooks / git / 进程 ─────┘   （监控）
        │
        └── FastAPI(REST+WS+WebPush) ──HTTPS（Tailscale）──▶ 📱 手机 PWA
                                                            （时间线 / 审批 / 下发）
```

完整图与组件契约见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 当前状态

🚧 **设计 + 脚手架（P0）**。本仓库目前是设计文档 + 项目骨架。后续按
[路线图](docs/ROADMAP.md) 推进：P1 单机驱动 → P2 审阅 → P3 手机+审批 → P4 两向控制。

## 技术栈

Python 3.11+ · FastAPI · SQLite(SQLModel) · httpx · Web Push(VAPID) · PWA service worker。
LLM 用**你自己的 API**（OpenAI 兼容或 Anthropic 兼容）。

## 快速开始（P0 脚手架）

```bash
pip install -e .
cp .env.example .env            # 填你的 LLM API base_url + key
cp config.example.yaml config.yaml
foreman serve                   # 启动后端、打开数据库、暴露 /health
```

## 文档

- 🇨🇳 **[设计方案 DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md)** —— 主设计文档。
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) —— 组件契约与 API。
- [ROADMAP.md](docs/ROADMAP.md) —— 分阶段计划。
- [SECURITY.md](docs/SECURITY.md) —— 远程访问与威胁模型。

## 许可证

MIT
