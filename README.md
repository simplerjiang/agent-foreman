# Foreman

Languages: [English](#english) | [中文](#中文)

## English

Foreman is a small PM layer for people who already use coding agents.

It runs on your own computer, watches the local coding work, helps plan the next step, reviews progress, and can expose a phone-friendly PWA through your own server. The point is simple: your project should not stop just because you closed the laptop, went out, or went to sleep.

It is open source and self-hosted. You do not pay Foreman a salary, but any model/API usage is still yours to configure and pay for.

### The Problem

Coding agents are useful, but unattended work is still awkward:

- You leave the desk, the agent hits a prompt, and the run just sits there.
- A task finishes, but nobody checks the diff, test result, or next step.
- You wake up or come back later and have to reconstruct everything from logs.
- You want to approve or redirect work from your phone, but the real project is on your PC.

Foreman is meant to sit in that gap. It is not a hosted coding service. It is a local PM process that helps keep your existing local agents moving.

### What Foreman Does

| Area | What it means |
|---|---|
| Local PM | Runs beside your projects and keeps state in your own local files/database. |
| Task dispatch | Starts configured coding CLIs in allowed workspaces. |
| Planning | Turns a goal into smaller steps and keeps the current step visible. |
| Progress tracking | Watches session output, git state, idle time, done/failed/blocked states. |
| Review | Uses your configured model provider to review changes and summarize risk. |
| Approval gates | Pauses risky actions such as push, deploy, destructive commands, or secret changes. |
| Phone control | Lets you read progress, approve cards, and send new instructions from a PWA. |
| Cloud relay | Lets your phone reach the PM app through your own server while the actual project work stays local. |

### Good Use Cases

- Start a development task at night and check the result in the morning.
- Let Foreman review whether a coding agent should continue, stop, or ask for help.
- Keep a project moving during commute, meetings, meals, or other away-from-keyboard time.
- Approve a safe next step from your phone without exposing the whole computer directly.
- Use an open-source tool instead of handing the whole workflow to a hosted black box.

### What It Is Not

- It is not a free model provider. Bring your own model/API setup.
- It is not magic deployment. Risky actions should still go through approval.
- It is not meant to edit every folder on your machine. Use workspace allowlists.
- It is not a requirement to run a cloud server. If you only use the desktop app locally, the exe/source app is enough.

### How It Fits Together

```text
Your PC
  Foreman app / PM Core
    -> local coding agents
    -> allowed project workspaces
    -> local state, reviews, approvals
    -> optional outbound relay connection

Your server
  Foreman server
    -> HTTPS PWA
    -> auth / accounts / relay
    -> forwards phone commands to the linked PC

Your phone
  PWA in the browser
    -> timeline
    -> approvals
    -> dispatch / redirect
    -> status checks
```

The exe is mainly the local desktop client. You only need server deployment when you want phone access through HTTPS, remote relay, or team-style access keys. See [DEPLOYREADME.md](DEPLOYREADME.md).

### Quick Start From Source

Prerequisites:

- Python 3.11+
- Git
- At least one local coding CLI installed and authenticated
- Your own model/API credentials if you want PM planning, review, or briefings

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
python -m venv .venv
. .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[client,server]"
cp config.example.yaml config.yaml
cp .env.example .env
foreman app
```

Before using it for real work:

- Put your own provider settings and API key in local config/env files.
- Add only the project directories Foreman may touch under `workspaces`.
- Configure the coding CLI commands you want Foreman to launch.
- Set `FOREMAN_AUTH_TOKEN` before exposing any Foreman web UI outside localhost.

### Useful Commands

```bash
foreman app
foreman serve --config config.yaml
foreman dispatch "Implement the next small task" --workspace /path/to/project
foreman create-admin admin --config config.yaml
foreman version
```

### Version Information

Current documented release: `v1.3.8`.

The package version is maintained in one code source: `src/foreman/__init__.py` (`__version__`). The exe, `/health`, and the PWA derive the runtime version from that source; the README and the in-exe Version page carry the human-readable release notes.

Update history:

| Version | Update |
|---|---|
| `v1.3.8` | PM tool activity now appears as a public timeline with tool starts, result summaries, collapsible details, optional public notes, and no hidden reasoning exposure. |
| `v1.3.7` | Codex stdout is now read in chunks and reassembled as JSONL, removing asyncio's per-line limit for large command-output events and cleaning up stream failures. |
| `v1.3.6` | PM thinking summaries now start as a transparent collapsed row using the generated reasoning title; hover changes the icon and clicking expands the full reasoning text. |
| `v1.3.5` | Update dialogs now show the human release notes for every version between the installed exe and the latest available release, instead of the automated CI build text. |
| `v1.3.4` | User and PM conversation bubbles now include a small copy icon for quickly copying message text from desktop and mobile session views. |
| `v1.3.3` | Packaged Windows exe now hides server-side git and diagnostic subprocess windows so switching sessions no longer flashes a transient cmd window. |
| `v1.3.2` | Composer status now shows the selected workspace's git worktree and branch, offers explicit git initialization when needed, and restores each session's saved workspace after reopening. |
| `v1.3.1` | PM reasoning stream deltas now keep leading spaces, so English thought summaries render with normal word spacing. |
| `v1.3.0` | Packaged exe self-update now opens a dialog with live download progress and a cancel button before restart. |
| `v1.2.9` | Version page check-for-updates button, plus one historical update list that includes the current release notes. |
| `v1.2.8` | PM shell runtime controls with live command output, durable tool logs, approval-governed execution, process-tree cancellation, and admin elevation for packaged exe builds. |
| `v1.2.7` | Markdown rendering and spacing improvements for PM reasoning summaries, plus localized Chinese reasoning labeling. |
| `v1.2.6` | Session stop control, single follow-up send button, dropdown model/thinking controls, image paste chips, and visible PM reasoning stream. |
| `v1.2.5` | PM recovery after fatal local agent failure by excluding failed agents and launching a replacement agent. |
| `v1.2.4` | Copilot BYOK GPT-5 launches use the Responses wire API while non-GPT-5 launches stay unchanged. |
| `v1.2.3` | Removed redundant auto-agent explanatory copy while keeping PM-driven agent selection unchanged. |
| `v1.2.2` | Removed the PM provider max output token setting and stopped sending OpenAI-compatible output caps. |
| `v1.2.1` | Bilingual README and exe version pages, visible version history, and stricter version-note rules. |
| `v1.2.0` | PM context token limits exposed in the product configuration flow. |

Full history: [docs/VERSION_HISTORY.md](docs/VERSION_HISTORY.md).

### Security Notes

- Do not expose `foreman app` or `foreman serve` publicly without HTTPS and auth.
- Keep API keys, auth tokens, VAPID private keys, SSH keys, domains, and server IPs out of git.
- Put phone access behind a reverse proxy or tunnel with TLS.
- Keep workspace allowlists narrow.
- Treat deploy, push, secret changes, and destructive commands as approval-gated operations.

### Docs

- [DEPLOYREADME.md](DEPLOYREADME.md) - server deployment, Windows and Linux.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - component contracts and API shape.
- [docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md) - detailed design notes in Chinese.
- [docs/SECURITY.md](docs/SECURITY.md) - remote access and security model.
- [docs/ROADMAP.md](docs/ROADMAP.md) - phased roadmap.

### Code Signing and Privacy

Free code signing for Foreman is provided by [SignPath.io](https://signpath.io), with a certificate issued by the [SignPath Foundation](https://signpath.org).

Foreman is self-hosted. It does not send project data, secrets, or local state to third-party systems unless you configure a provider, relay, notification channel, or update action that needs network access.

### License

MIT

## 中文

Foreman 是给“已经在用编码 Agent 的人”准备的 PM 管家。

它运行在你自己的电脑上，盯着本地项目开发，帮你规划下一步、看进度、审结果；需要远程控制时，也可以通过你自己的服务器把手机 PWA 接进来。目标很直接：人离开电脑以后，项目不要立刻停摆。

它是开源、自托管的。你不用给这个 PM 发工资，但模型/API 的 token 成本还是你自己的。

### 它解决什么问题

编码 Agent 很好用，但一旦无人值守，就会有几个老问题：

- 你离开电脑后，它撞到确认框或风险动作，就停住等人。
- 任务做完了，但没人看 diff、测试结果，也没人判断下一步。
- 你睡醒或回来以后，还要翻日志猜它刚才干到哪。
- 你想在手机上批一下、改一下方向，但真正的项目又在电脑上。

Foreman 就是补这个空档。它不是托管编码平台，也不是替代你现有的 Agent；它更像一个守在本机旁边的 PM 进程，负责把工作流接起来。

### 它能做什么

| 模块 | 说明 |
|---|---|
| 本地 PM | 跑在你的电脑上，状态存在你自己的本地文件/数据库里。 |
| 派发任务 | 在允许的项目目录里启动你配置好的编码 CLI。 |
| 规划步骤 | 把一个目标拆小，并让当前步骤一直可见。 |
| 进度跟踪 | 看输出、git 状态、idle 时间、done/failed/blocked 状态。 |
| 审阅结果 | 用你自己配置的模型服务审 diff、总结风险和下一步。 |
| 审批网关 | push、deploy、删文件、改 secret 这类危险动作先暂停，等你点头。 |
| 手机控制 | 用 PWA 在手机上看进度、批审批卡、补充指令。 |
| 云端转发 | 手机连你自己的服务器，服务器再把命令转给本地电脑；项目代码仍留在本机。 |

### 适合什么场景

- 睡觉前丢一个开发任务，早上看结果。
- 让 PM Agent 判断编码 Agent 应该继续、停下，还是回来问人。
- 通勤、开会、吃饭时，让项目继续跑一段。
- 在手机上批准一个安全的下一步，而不是远程暴露整台电脑。
- 想要开源、自托管、可审计的项目自动化，而不是黑盒托管服务。

### 它不是什么

- 它不是免费模型服务。模型/API 要你自己配置。
- 它不是“自动部署就一定安全”。高风险动作仍然应该审批。
- 它不应该拥有全盘写权限。请认真配置 workspace allowlist。
- 它不强制你部署云端。只在本地使用时，exe 或源码 app 就够了。

### 大概怎么连起来

```text
你的电脑
  Foreman app / PM Core
    -> 本地编码 Agent
    -> 允许操作的项目目录
    -> 本地状态、审阅、审批
    -> 可选的出站 relay 连接

你的服务器
  Foreman server
    -> HTTPS PWA
    -> 认证 / 账号 / relay
    -> 把手机命令转给已连接的电脑

你的手机
  浏览器里的 PWA
    -> 时间线
    -> 审批
    -> 下发 / 改向任务
    -> 查看状态
```

exe 主要是本地桌面客户端。只有你需要 HTTPS 手机访问、远程 relay 或团队 access key 时，才需要部署服务端。部署方式见 [DEPLOYREADME.md](DEPLOYREADME.md)。

### 从源码快速启动

前置条件：

- Python 3.11+
- Git
- 至少一个已安装并登录的本地编码 CLI
- 如果要启用 PM 规划、审阅或简报，需要你自己的模型/API 凭据

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
python -m venv .venv
. .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[client,server]"
cp config.example.yaml config.yaml
cp .env.example .env
foreman app
```

真正使用前，先改好这几处：

- 在本地配置/env 里填写你自己的模型服务和 API key。
- 在 `workspaces` 里只加入 Foreman 可以操作的项目目录。
- 配置 Foreman 可以启动哪些编码 CLI。
- 任何 Web UI 暴露到 localhost 之外前，都要设置 `FOREMAN_AUTH_TOKEN`。

### 常用命令

```bash
foreman app
foreman serve --config config.yaml
foreman dispatch "实现下一个小任务" --workspace /path/to/project
foreman create-admin admin --config config.yaml
foreman version
```

### 版本信息

当前文档版本：`v1.3.8`。

包版本只在一个代码来源维护：`src/foreman/__init__.py` 的 `__version__`。exe、`/health` 和 PWA 都从这里派生运行版本；README 和 exe 内的「版本」页面负责维护给人看的更新说明。

更新历史：

| 版本 | 更新 |
|---|---|
| `v1.3.8` | PM 工具活动现在进入公开时间线，显示工具开始、结果摘要、可折叠详情和可选公开说明，同时不暴露隐藏思维链。 |
| `v1.3.7` | Codex stdout 改为分块读取并重组 JSONL，移除大段命令输出触发的 asyncio 单行读取上限，并在读取失败时清理子进程。 |
| `v1.3.6` | PM 思考摘要现在默认显示为透明折叠行，标题来自 reasoning 里的生成标题；悬浮时图标变化，点击后再展开完整思考内容。 |
| `v1.3.5` | 更新弹窗现在显示已安装 exe 到最新可用版本之间每个版本的人工更新说明，不再显示自动 CI 构建文本。 |
| `v1.3.4` | 用户与 PM 会话泡泡底部增加小复制图标，桌面和移动会话视图都能快速复制消息文本。 |
| `v1.3.3` | 打包 Windows exe 会隐藏服务端 git 与诊断子进程窗口，切换会话时不再闪出临时 cmd 窗口。 |
| `v1.3.2` | 会话输入区改为显示所选工作区的 git worktree 与 branch，必要时提供显式新建 git 仓库按钮，并在重开后恢复每个会话保存的工作区。 |
| `v1.3.1` | PM 思考流 delta 片段会保留前导空格，英文思考摘要不再单词粘连显示。 |
| `v1.3.0` | 打包 exe 自更新改为弹窗模式，增加实时下载进度，并在重启前提供取消下载按钮。 |
| `v1.2.9` | 版本页增加检查更新按钮，并把当前版本说明与历史版本说明合并为同一个历史更新列表。 |
| `v1.2.8` | PM shell 运行控制支持实时命令输出、工具日志落盘、审批约束执行、进程树取消，以及打包 exe 管理员权限启动。 |
| `v1.2.7` | PM 思考摘要改用 Markdown 渲染并改善间距，同时本地化中文标签。 |
| `v1.2.6` | 增加会话停止入口、合并继续发送按钮、模型与 thinking level 下拉、图片粘贴附件，以及可见 PM reasoning 流。 |
| `v1.2.5` | 本地 agent 致命失败后，PM 可排除失败 agent 并启动替代 agent 继续恢复。 |
| `v1.2.4` | Copilot BYOK 使用 GPT-5 系列模型时切换到 Responses wire API，非 GPT-5 启动保持不变。 |
| `v1.2.3` | 移除冗余的自动执行 agent 说明文案，PM 自动选择行为不变。 |
| `v1.2.2` | 移除 PM Provider 最大输出 token 设置，并停止发送 OpenAI 兼容输出上限。 |
| `v1.2.1` | 中英文 README 与 exe 版本页、可见版本历史，以及更严格的版本说明规则。 |
| `v1.2.0` | 在产品配置流程中暴露 PM 上下文 token 上限设置。 |

完整历史：[docs/VERSION_HISTORY.md](docs/VERSION_HISTORY.md)。

### 安全注意

- 没有 HTTPS 和认证时，不要把 `foreman app` 或 `foreman serve` 暴露到公网。
- API key、认证 token、VAPID private key、SSH key、域名、服务器 IP 都不要进 git。
- 手机访问建议放在反向代理或隧道 TLS 后面。
- workspace allowlist 越窄越好。
- deploy、push、secret 修改、破坏性命令都应该走审批网关。

### 文档

- [DEPLOYREADME.md](DEPLOYREADME.md) - 服务端部署，Windows / Linux。
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - 组件契约和 API 形状。
- [docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md) - 中文详细设计。
- [docs/SECURITY.md](docs/SECURITY.md) - 远程访问和安全模型。
- [docs/ROADMAP.md](docs/ROADMAP.md) - 分阶段路线图。

### 代码签名与隐私

Foreman 的免费代码签名由 [SignPath.io](https://signpath.io) 提供，证书由 [SignPath Foundation](https://signpath.org) 签发。

Foreman 是自托管软件。除非你主动配置需要联网的模型服务、relay、通知渠道或更新动作，否则 Foreman 不会把项目数据、密钥或本地状态发送到第三方系统。

### 许可证

MIT
