# Foreman

Languages: [English](#english) | [中文](#中文)

## English

Foreman is an open-source local + cloud PM agent for software projects. It runs on your own computer, supervises local coding agents, plans and reviews work, tracks progress, and can relay a mobile web UI through your own server so you can keep projects moving while away from the keyboard.

Think of it as a PM housekeeper for personal or small-team development: no payroll, fully self-hosted, but any model/API usage still belongs to your own account and budget.

### Why It Exists

CLI coding agents are useful, but they often stall when you leave:

- They hit approval prompts while you are commuting, sleeping, or away from the desk.
- They finish code but still need a human to review diffs, run tests, plan the next step, or decide whether to continue.
- They leave scattered logs that are hard to reconstruct when you come back.

Foreman puts a PM agent in the loop. It can watch development sessions, summarize progress, review outputs, ask for approval on risky actions, and let you dispatch or redirect work from a phone through a self-hosted server.

### Core Features

| Feature | What It Does |
|---|---|
| Local PM Core | Runs on your machine and manages work in allowlisted project directories. |
| Agent Dispatch | Starts and steers configured local coding CLIs from the desktop app, web UI, or command line. |
| Planning Loop | Turns a goal into task steps, tracks session state, and keeps the next action explicit. |
| Progress Monitoring | Watches process output, git state, session events, and idle/blocked states. |
| Review and QA | Uses your configured model provider to review diffs, summarize risks, and produce project briefings. |
| Approval Gates | Holds risky actions such as push, deploy, destructive shell commands, or secret changes until approved. |
| Mobile Control | Serves a PWA for timeline viewing, approval cards, task dispatch, and status checks from a phone. |
| Local + Cloud Relay | Keeps the PM brain and project work local, while an optional server relays mobile commands and live state. |
| Open Source | MIT licensed, self-hosted, and designed so secrets stay in your own deployment. |

### Typical Uses

- Let Foreman continue project development, testing, and planning during idle time.
- Start a task before sleep and check the result from your phone in the morning.
- Let a PM agent review progress and decide whether the coding agent should continue, ask for clarification, or stop.
- Use a cloud relay so your phone can control the PM agent running on your home or office computer.
- Keep project automation open-source and self-owned instead of relying on a hosted black box.

### Architecture

```text
Local PC
  Foreman app / PM Core
    -> local coding agents
    -> project workspace allowlist
    -> local database, review state, approval gates
    -> optional outbound relay connection

Your server
  Foreman server
    -> HTTPS PWA
    -> auth / accounts / relay
    -> forwards mobile commands to the linked local PC

Phone
  Browser-installed PWA
    -> timeline
    -> approvals
    -> dispatch / redirect
    -> project status
```

The executable is mainly a local desktop client. It does not need server-style deployment. Server deployment is only needed when you want phone access through HTTPS or multi-machine/team relay. See [DEPLOYREADME.md](DEPLOYREADME.md).

### Quick Start From Source

Prerequisites:

- Python 3.11+
- Git
- At least one supported local coding CLI installed and authenticated
- Your own model/API credentials if you enable PM review, planning, or briefings

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

Edit `config.yaml` and `.env` before real use:

- Put only your own model provider settings and API key in the local environment.
- Add project directories under `workspaces`.
- Configure which coding agent CLIs Foreman may launch.
- Set `FOREMAN_AUTH_TOKEN` before exposing any web UI outside localhost.

### Useful Commands

```bash
foreman app
foreman serve --config config.yaml
foreman dispatch "Implement the next small task" --workspace /path/to/project
foreman create-admin admin --config config.yaml
foreman version
```

### Security Notes

- Do not expose `foreman app` or `foreman serve` publicly without an auth token and HTTPS.
- Keep API keys, auth tokens, VAPID private keys, SSH keys, and server IPs out of git.
- Prefer a reverse proxy or tunnel with TLS for phone access.
- Use workspace allowlists so Foreman can only operate in intended directories.
- Treat deploy, push, secret changes, and destructive commands as approval-gated operations.

### Documentation

- [DEPLOYREADME.md](DEPLOYREADME.md) - bilingual server deployment guide.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - component contracts and API shape.
- [docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md) - detailed design notes in Chinese.
- [docs/SECURITY.md](docs/SECURITY.md) - remote access and security model.
- [docs/ROADMAP.md](docs/ROADMAP.md) - phased roadmap.

### Code Signing and Privacy

Free code signing for Foreman is provided by [SignPath.io](https://signpath.io), with a certificate issued by the [SignPath Foundation](https://signpath.org).

Foreman is self-hosted. It does not send project data, secrets, or local state to third-party systems unless the operator explicitly configures a provider, relay, notification channel, or update action that requires network access.

### License

MIT

## 中文

Foreman 是一个完全开源的本地 + 云端项目 PM Agent，用来自动管理项目开发、测试、规划和进度审核。它运行在你自己的电脑上，管理本地编码 Agent；也可以通过你自己的服务器把手机 PWA 接进来，让你离开电脑、通勤、睡觉时仍然能远程查看进度、审批风险动作、下发或调整任务。

可以把它理解成一个项目 PM 管家：像请了一个不用发工资的 PM 来盯项目，但模型/API token 的调用成本仍然由你自己的账号承担。

### 为什么需要它

使用 CLI 编码 Agent 时，最痛的场景通常发生在你离开电脑之后：

- Agent 碰到确认框、风险动作或上下文不清，就停在原地。
- 代码写完了，但还需要有人审 diff、跑测试、规划下一步、判断是否继续。
- 回来以后要翻日志、看状态、猜它到底执行到哪里。

Foreman 把 PM Agent 放进流程里：它可以监控开发会话、总结进展、审阅输出、在危险动作前请求审批，并让你通过手机从云端转发控制本地电脑上的 PM Agent。

### 主要功能

| 功能 | 说明 |
|---|---|
| 本地 PM Core | 运行在你的电脑上，只管理 allowlist 里的项目目录。 |
| Agent 调度 | 从桌面 app、Web UI 或命令行启动并引导本地编码 CLI。 |
| 需求规划 | 把目标拆成步骤，维护会话状态，让下一步动作保持明确。 |
| 进度监控 | 观察进程输出、git 状态、事件流，以及 idle / blocked 等状态。 |
| 审阅与 QA | 使用你自己配置的模型服务审阅 diff、总结风险、生成项目简报。 |
| 审批网关 | 对 push、deploy、破坏性命令、secret 修改等高风险动作先暂停等待审批。 |
| 手机操控 | 通过 PWA 在手机上看时间线、批审批卡、下发任务、检查项目状态。 |
| 本地 + 云端转发 | PM 大脑和项目代码留在本机；可选服务端只负责 HTTPS PWA、认证和 relay。 |
| 完全开源 | MIT 协议，自托管，密钥和私有配置由你自己保管。 |

### 典型使用场景

- 在空闲时间继续推进项目开发、测试和规划。
- 睡觉前下发任务，第二天早上从手机查看进展和结果。
- 让 PM Agent 代替人类做阶段性审核，判断编码 Agent 应该继续、停下、还是请求澄清。
- 用云端转发让手机控制家里或办公室电脑上的 PM Agent。
- 保持项目自动化开源、自托管、可审计，而不是依赖黑盒托管服务。

### 架构概览

```text
本地电脑
  Foreman app / PM Core
    -> 本地编码 Agent
    -> 项目工作区 allowlist
    -> 本地数据库、审阅状态、审批网关
    -> 可选的出站 relay 连接

你的服务器
  Foreman server
    -> HTTPS PWA
    -> 认证 / 账号 / relay
    -> 把手机命令转发到已连接的本地电脑

手机
  浏览器安装的 PWA
    -> 时间线
    -> 审批
    -> 下发 / 改向任务
    -> 项目状态
```

exe 主要是本地桌面客户端，不需要像服务端一样部署。只有当你需要 HTTPS 手机访问或多机器/团队 relay 时，才需要部署服务端。详见 [DEPLOYREADME.md](DEPLOYREADME.md)。

### 从源码快速启动

前置条件：

- Python 3.11+
- Git
- 至少一个已安装并登录的本地编码 CLI
- 如果启用 PM 审阅、规划或简报，需要你自己的模型/API 凭据

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

真实使用前请编辑 `config.yaml` 和 `.env`：

- 只填写你自己的模型服务配置和 API key。
- 在 `workspaces` 里加入允许 Foreman 操作的项目目录。
- 配置 Foreman 可以启动哪些编码 Agent CLI。
- 任何 Web UI 暴露到 localhost 之外前，都要设置 `FOREMAN_AUTH_TOKEN`。

### 常用命令

```bash
foreman app
foreman serve --config config.yaml
foreman dispatch "实现下一个小任务" --workspace /path/to/project
foreman create-admin admin --config config.yaml
foreman version
```

### 安全注意

- 不要在没有认证 token 和 HTTPS 的情况下公开暴露 `foreman app` 或 `foreman serve`。
- API key、认证 token、VAPID private key、SSH key、服务器 IP 不进 git。
- 手机访问建议放在反向代理或隧道 TLS 后面。
- 使用 workspace allowlist，限制 Foreman 只能操作指定目录。
- deploy、push、secret 修改、破坏性命令都应当走审批网关。

### 文档

- [DEPLOYREADME.md](DEPLOYREADME.md) - 中英双语服务端部署教程。
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - 组件契约和 API 形状。
- [docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md) - 中文详细设计。
- [docs/SECURITY.md](docs/SECURITY.md) - 远程访问和安全模型。
- [docs/ROADMAP.md](docs/ROADMAP.md) - 分阶段路线图。

### 代码签名与隐私

Foreman 的免费代码签名由 [SignPath.io](https://signpath.io) 提供，证书由 [SignPath Foundation](https://signpath.org) 签发。

Foreman 是自托管软件。除非使用者主动配置需要联网的模型服务、relay、通知渠道或更新动作，否则 Foreman 不会把项目数据、密钥或本地状态发送到第三方系统。

### 许可证

MIT
