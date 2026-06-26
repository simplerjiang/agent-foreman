# Foreman 接入 Copilot CLI 本地 Agent 计划书

## 1. 背景与目标

当前 Foreman 的本地执行 agent 主要支持：

- `claude-code`
- `codex`

但在部分 Windows 电脑上，用户可能没有安装 Claude Code / Codex CLI，却已经安装了 GitHub Copilot CLI，例如：

```text
D:\Foreman\tools\copilot\copilot.exe
```

本计划的目标是给 Foreman 增加一个新的本地 agent 类型：

```text
copilot-cli
```

使用户可以在 Foreman 的本地设置页里启用 Copilot CLI，并让 Foreman 像调度 Codex / Claude Code 一样调度 Copilot CLI 执行开发任务。

## Review 结论（兼容性 / 远端控制 / 显示能力）

本计划整体可行，但要满足「不影响原有功能」和「远端控制 + 显示闭环」，实现时必须补齐以下约束：

1. **不要默认抢占原有 agent 路径**：`copilot-cli` 应加入支持列表并出现在设置页，但默认建议为 `enabled: false`，或仅在检测到 `copilot` 可执行且用户首次初始化时自动启用。否则老用户升级后，`_resolve_agent()` / PM 自动选择可能因为排序或 enabled 列表变化，把任务派给一个未安装或未配置的 Copilot CLI。
2. **必须更新 PM 自动规划白名单**：当前 `src/foreman/client/core/pm_agent.py` 里有 `VALID_AGENTS = {"claude-code", "codex"}`，`src/foreman/client/tools/loop.py` 里也有 `_DEFAULT_PLAN_AGENTS = ("claude-code", "codex")`。如果不加入 `copilot-cli`，则“手动指定 agent”可能可用，但 PM 自动选择/工具循环会忽略或回退到旧 agent。
3. **远端控制链路具备复用基础**：服务端 `/api/dispatch` 已透传 `agent/model/effort/session_id` 到本机进程；本机 `DispatchService._resolve_agent()` 会按 `cfg.agents` enabled 列表校验。因此只要本机配置中启用 `copilot-cli` 且 Runner 注册 adapter，远端下发任务可以路由到 `copilot-cli`。
4. **远端显示具备基础能力**：`client/cache_sync.py` 的 snapshot 会上报 session 的 `agent_type`，所以使用 `copilot-cli` 创建的会话能在远端显示为 `agent_type=copilot-cli`；事件流仍复用 `agent_start / agent_output / agent_reasoning / stop / error`。
5. **远端 UI 的“可选 agent 列表”还需显式设计**：当前 `/api/processes` 只返回机器元数据，snapshot 也只返回 session/card 摘要，不包含“这台机器可用哪些 agent”。如果远端控制台要让用户选择 `copilot-cli`，应把本机 enabled/ok agent 列表作为 display-safe metadata 暴露到 snapshot 或 process metadata；否则只能依赖 PM 自动选择或手工传 `agent=copilot-cli`。
6. **`--allow-all-paths` 会弱化 workspace 边界**：Foreman 的 workspace allowlist 只保证“在哪个 cwd 启动 agent”。一旦 Copilot CLI 传 `--allow-all-paths`，它可能访问工作区外路径。MVP 更稳妥的 full-access 映射应优先使用 `--allow-all-tools --allow-all-urls --add-dir <workspace>`，不要默认 `--allow-all-paths`；如果确实需要全盘访问，应在 UI 明确升级为高危选项。

据此，本文后续方案以“新增能力默认不改变老用户运行路径”为原则：新增 `copilot-cli` 支持，但默认不让它影响原有 Claude/Codex 派发，用户显式启用后才参与本地和远端控制。

## 2. 产品目标

### 2.1 用户故事

作为一个 Foreman 用户，当我的电脑没有安装 `codex` 或 `claude`，但已经安装了 `copilot` CLI 时，我希望：

1. Foreman 能自动识别或允许我配置 `copilot` 命令。
2. 设置页的「本地 Agent」里出现 `copilot-cli` 选项。
3. 我可以启用 `copilot-cli`，并禁用不可用的 `codex` / `claude-code`。
4. 发起任务时，Foreman 可以选择或直接使用 `copilot-cli`。
5. Copilot CLI 的输出能进入现有会话时间线、原始输出、子代理面板。
6. 远端控制链路下发任务时，也能把任务派给本机的 `copilot-cli`。

### 2.2 非目标

第一阶段不做以下内容：

- 不实现 Copilot CLI 的完整交互式 TUI 控制。
- 不依赖 GitHub 登录；BYOK provider 模式下 Copilot CLI 可以不登录 GitHub。
- 不把 Copilot CLI 的 API key 写入 Foreman 的 `config.yaml`。
- 不新建独立的 Copilot 账号管理页面。
- 不改动 Foreman PM Brain 的 LLM provider 配置体系；Copilot CLI 是「执行 agent」，不是 PM Brain。

## 3. 当前代码结构观察

### 3.1 Agent 抽象

现有 agent 抽象位于：

- `src/foreman/client/agents/base.py`
- `src/foreman/client/agents/_subprocess.py`
- `src/foreman/client/agents/codex.py`
- `src/foreman/client/agents/claude_code.py`
- `src/foreman/client/agents/runner.py`

关键结构：

- `AgentCfg`：通用 agent 配置，包含 `enabled / command / mode / model / effort / full_access`。
- `AgentAdapter`：统一 `start / send / stream / interrupt / stop`。
- `SubprocessCliAdapter`：封装子进程启动、stdout/stderr 流式读取、JSON line 解析、停止。
- `Runner.sync_config()`：把 `cfg.agents` 注册成实际 adapter。

### 3.2 后端设置 API

现有 API 位于：

- `src/foreman/server/app.py`

相关逻辑：

- `_SUPPORTED_AGENTS = frozenset(default_agents())`
- `default_agents()` 当前只返回 `claude-code` 和 `codex`。
- `/api/agents` 返回启用 agent，供派发表单使用。
- `/api/settings/agents` GET/POST 读写本地 agent 设置。
- `_clean_agents()` 会过滤不在 `_SUPPORTED_AGENTS` 内的 agent。

因此新增 `copilot-cli` 的后端入口应从 `default_agents()` 开始，而不是只改 UI。

### 3.3 前端设置页

现有前端位于：

- `src/foreman/server/web/app.js`

相关逻辑：

- 设置页调用 `/api/settings/agents`。
- 前端按返回的 `agentSettings` 动态渲染本地 agent 行。
- 每行已有字段：
  - 启用开关
  - 命令
  - 模型
  - 档位
  - 工具全开
  - 命令诊断状态

因此 MVP 阶段前端只需少量文案/提示优化，主要依赖后端返回新增 `copilot-cli` 行。

## 4. 技术方案总览

新增一个 Copilot CLI adapter：

```text
CopilotCliAdapter(SubprocessCliAdapter)
```

注册名：

```text
copilot-cli
```

默认配置：

```yaml
agents:
  copilot-cli:
    enabled: false
    command: copilot
    model: ""
    effort: high
    full_access: true
    mode: headless
```

原计划启动命令如下，但 Review 后不建议作为 MVP 默认；它只适合作为用户显式开启“允许所有路径”后的高危模式示例：

```powershell
copilot -p "<instruction>" --model gpt-5.5 --effort high --no-auto-update --output-format json --allow-all-tools --allow-all-paths --allow-all-urls
```

Review 后建议将 MVP 命令调整为工作区限定版本：

```powershell
copilot -p "<instruction>" --model gpt-5.5 --effort high --no-auto-update --output-format json --allow-all-tools --allow-all-urls --add-dir "<workspace>"
```

只有当用户明确选择“允许访问所有路径”时，才额外传 `--allow-all-paths`。

如果 `full_access=false`，则不传 `--allow-all*`，让 Copilot CLI 自己提示或拒绝高风险操作。由于 Foreman 的 headless 运行不适合交互确认，MVP 建议：

- 本地默认 `full_access=true` 时只表示“允许工具自动执行 + 允许网络访问 + 授权当前 workspace 目录”；不应默认等同 `--allow-all-paths`。
- UI 明确显示「工具全开」风险提示。
- 远端执行仍受 `remote_execution_enabled` 总开关控制。

## 5. 后端开发计划

### 5.1 新增 Copilot adapter

新增文件：

```text
src/foreman/client/agents/copilot_cli.py
```

职责：

1. 继承 `SubprocessCliAdapter`。
2. `name = "copilot-cli"`。
3. 实现 `_build_cmd()`。
4. 实现 `_build_resume_cmd()`。
5. 根据 `full_access` 决定是否传自动授权参数。
6. 根据 `model` / `effort` 拼接 Copilot CLI 参数。
7. 禁用自动更新，避免 Foreman 运行中自行下载新版 CLI。

建议实现逻辑：

```python
class CopilotCliAdapter(SubprocessCliAdapter):
    name = "copilot-cli"

    def _access_args(self) -> list[str]:
        if not self._full_access():
            return []
        return ["--allow-all-tools", "--allow-all-urls"]

    def _effort_args(self, effort: str) -> list[str]:
        return ["--effort", effort] if effort else []

    def _build_cmd(self, instruction: str, model: str = "", effort: str = "") -> list[str]:
        return [
            self.cfg.command,
            "-p", instruction,
            "--no-auto-update",
            "--output-format", "json",
            *self._model_args(model),
            *self._effort_args(effort),
            *self._access_args(),
        ]
```

由于安全推荐需要传 `--add-dir <workspace>`，而当前 `SubprocessCliAdapter._build_cmd()` 签名不包含 workspace，Copilot adapter 有两种实现方式：

1. **推荐**：覆盖 `start()` / `send()`，在构造命令时加入 workspace-aware 参数。
2. 或调整 `SubprocessCliAdapter`，让 `_build_cmd()` 可选接收 workspace，但这会影响 Codex/Claude adapter，需更谨慎。

为降低对原有功能影响，MVP 推荐第 1 种：只在 `CopilotCliAdapter` 内覆盖最小必要逻辑。

注意：

- Copilot CLI 支持 `--model`，可复用 `_model_args()`。
- Copilot CLI 支持 `--effort` / `--reasoning-effort`。
- `--output-format json` 输出 JSONL，先沿用 `SubprocessCliAdapter._line_to_event()` 的保守解析。
- 如果 JSON schema 后续确认稳定，再覆盖 `_line_to_event()` 做更细映射。

### 5.2 会话续接策略

Copilot CLI 支持：

- `--continue`
- `--resume`
- `--resume=<session-id>`
- `--session-id <id>`

MVP 建议：

1. 首次启动传 `--session-id <Foreman session_id>`，让 Copilot CLI 与 Foreman 会话绑定。
2. `send()` / `_build_resume_cmd()` 使用 `--session-id <native_session_id or Foreman session_id>` 继续同一会话。
3. 如果 Copilot CLI 输出里能解析原生 session id，则写入 `handle.native_session_id`。
4. 如果解析不到，则使用 Foreman 的 `session_id` 作为稳定 session id。

建议命令形态：

```powershell
copilot --session-id <session_id> -p "<instruction>" ...
```

续接：

```powershell
copilot --session-id <session_id> -p "<follow-up>" ...
```

这样可以避免依赖 `--continue` 的「最近会话」语义，降低多会话并发时串线风险。

### 5.3 注册到 Runner

修改：

```text
src/foreman/client/agents/runner.py
```

新增 import：

```python
from .copilot_cli import CopilotCliAdapter
```

在 `sync_config()` 中增加：

```python
if (c := self.cfg.agents.get("copilot-cli")) and c.enabled:
    self.adapters["copilot-cli"] = CopilotCliAdapter(c)
```

### 5.4 配置默认值

修改：

```text
src/foreman/shared/config.py
```

在 `default_agents()` 中新增：

```python
"copilot-cli": AgentCfg(
  command="copilot",
  enabled=False,
  mode="headless",
  model="",
  effort="high",
),
```

考虑兼容性，默认启用策略有两种：

#### 方案 A：默认启用

优点：

- 装了 Copilot CLI 的用户打开 Foreman 就能看到并使用。
- 未安装时设置页会显示「未找到命令」，与现有 Codex/Claude 行为一致。

缺点：

- `/health` 的 enabled agents 可能显示一个实际不可用的 agent，除非后端 status 过滤。

#### 方案 B：默认禁用但显示

优点：

- 更安全，不会误派发。

缺点：

- 用户需要手动启用。

建议：

- Review 后建议 MVP 使用 **方案 B**：默认禁用但显示。这样不会改变老用户现有派发路径，也不会让未登录/未配置 provider 的 Copilot CLI 进入自动选择。
- 后续可做“首次启动自动发现”：若 `shutil.which("copilot")` 成功且 `codex/claude-code` 均不可用，再提示用户一键启用，而不是静默启用。

### 5.5 更新支持列表

由于 `_SUPPORTED_AGENTS = frozenset(default_agents())`，只要 `default_agents()` 增加 `copilot-cli`，后端设置 API 会自动允许保存它。

但要检查：

- `_clean_agents()` 是否按 `_SUPPORTED_AGENTS` 排序，UI 顺序是否合理。
- `_agent_config_rows()` 是否过滤 `name in _SUPPORTED_AGENTS`。
- `/api/agents` 是否正确返回启用的 `copilot-cli`。

同时必须更新 PM 自动规划相关白名单：

- `src/foreman/client/core/pm_agent.py`：`VALID_AGENTS` 增加 `copilot-cli`。
- `src/foreman/client/tools/loop.py`：`_DEFAULT_PLAN_AGENTS` 增加 `copilot-cli`，或改为完全由后端传入 enabled agents。
- 相关测试覆盖 PM 自动选择可以选择 `copilot-cli`，且老配置下仍优先保持原有行为。

### 5.6 命令诊断

`/api/settings/agents` 当前会返回：

- `ok`
- `version`
- `resolved_path`
- `error`

需要确认 `_agent_status()` 对 `copilot version` 或 `copilot --version` 是否兼容。

建议：

- 对 `copilot-cli` 优先调用：

```powershell
copilot version
```

或：

```powershell
copilot --version
```

实际本机验证：

```text
GitHub Copilot CLI 1.0.63
```

如果现有 `_agent_status()` 是通用 `command --version`，则无需新增特殊逻辑；否则补一个 per-agent version command 映射。

### 5.7 环境变量继承

Copilot CLI BYOK provider 依赖用户级环境变量：

```text
COPILOT_PROVIDER_BASE_URL
COPILOT_PROVIDER_TYPE
COPILOT_PROVIDER_API_KEY
COPILOT_PROVIDER_WIRE_API
COPILOT_MODEL
COPILOT_PROVIDER_MODEL_ID
COPILOT_PROVIDER_WIRE_MODEL
```

`SubprocessCliAdapter._spawn()` 默认继承 `os.environ`，因此 Foreman 启动进程能拿到当前环境。

但 Windows 用户级环境变量在已打开的进程中不会自动刷新。需要在 UI 或文档里提示：

- 配完 provider 后重启 Foreman。
- 或 Foreman 设置页增加「环境变量检测」提示。

MVP 建议只做文档提示；后续再做环境诊断。

### 5.8 输出解析

MVP：

- 直接复用 `SubprocessCliAdapter._line_to_event()`。
- JSON object 输出保留原 payload。
- 非 JSON 行作为 `agent_output`。
- Copilot CLI 退出码非零时复用现有 `error` event。

增强版：

- 根据 Copilot JSONL schema 映射：
  - reasoning / thinking → `agent_reasoning`
  - assistant text → `agent_output`
  - tool call start / done → `agent_output` 或后续专门 event
  - final result → `stop`
- 提取 Copilot session id 写入 `handle.native_session_id`。

## 6. 前端开发计划

### 6.1 设置页

现有设置页动态渲染 `/api/settings/agents` 返回的 rows，因此新增 `copilot-cli` 后会自动出现。

MVP 前端改动：

1. 不新增独立 UI 结构。
2. 增加文案：
   - `copilot-cli` 可作为本地 agent。
   - 如果使用 BYOK，需要先配置 Copilot CLI provider 环境变量并重启 Foreman。
3. 保持现有字段：
   - 启用
   - 启动命令
   - 模型
   - 档位
   - 工具全开

建议默认展示：

```text
名称：copilot-cli
命令：copilot
模型：默认 / gpt-5.5
档位：high
工具全开：开
```

### 6.2 派发表单

`/api/agents` 返回启用 agent 后，现有派发表单应自动出现 `copilot-cli`。

需验证：

- agent 下拉框显示 `copilot-cli`。
- `agentAuto` 模式是否会选择 `copilot-cli`。
- 如果 `codex` / `claude-code` 都不可用但仍 enabled，PM 自动选择是否会误选不可用 agent。

MVP 建议：

- 不改自动选择逻辑。
- 在设置页让用户禁用不可用的 Codex/Claude。

增强建议：

- `/api/agents` 只返回 `enabled && ok` 的 agent，避免派发到不可执行命令。
- 或派发时检测 `resolved_path`，不可用则返回 `agent_not_found`。

### 6.3 状态与风险提示

由于 Copilot CLI 的 `--allow-all*` 权限很高，应复用现有「工具全开 + broad workspace」风险提示。

新增前端提示建议：

```text
Copilot CLI 使用 --allow-all-tools / --allow-all-urls / --add-dir <workspace> 时，会允许该 CLI 在当前工作区内自主读写和运行命令。只有显式开启“允许所有路径”时才会额外使用 --allow-all-paths，请只对可信工作区和可信机器开启。
```

### 6.4 远端控制页面

远端控制下发任务最终仍走本机 `/api/dispatch` / relay command / Runner，因此只要后端支持 `copilot-cli`，远端控制无需单独改协议。

需要验证：

- 远端 dispatch body 的 `agent` 字段可传 `copilot-cli`。
- 本机 `remote_execution_enabled=true` 时能执行。
- 远端页面 agent selector 是否来自 `/api/agents` 或 snapshot 中的 agents。
- 若当前远端 UI 没有 agent selector，PM auto-pick 能否 pick 到 `copilot-cli`。

Review 结论：

- **控制能力**：后端协议已经具备透传基础，新增 adapter 后可复用；无需新增 relay frame kind。
- **显示能力**：session snapshot 已包含 `agent_type`，因此结果可显示；但远端 UI 若要“选择这台机器上的 Copilot CLI”，还缺少本机 agent availability metadata。

建议增加一个 display-safe 字段，例如在 snapshot payload 中加入：

```json
{
  "agents": [
    {"name": "copilot-cli", "enabled": true, "ok": true, "model": "gpt-5.5", "effort": "high"}
  ]
}
```

注意该字段不得包含 API key、完整环境变量或本地敏感路径；`resolved_path` 是否上报需要谨慎，默认可不上报或只上报 basename。

## 7. 配置文件改造

### 7.1 `config.example.yaml`

新增示例：

```yaml
agents:
  claude-code:
    enabled: true
    command: claude
    model: ""
    effort: ""
    full_access: true
    mode: headless
  codex:
    enabled: true
    command: codex
    model: ""
    effort: ""
    full_access: true
    mode: headless
  copilot-cli:
    enabled: false
    command: copilot
    model: ""
    effort: high
    full_access: true
    mode: headless
```

### 7.2 用户本地 `config.yaml`

在当前机器上建议配置：

```yaml
agents:
  claude-code:
    enabled: false
    command: claude
    model: ""
    effort: ""
    full_access: true
    mode: headless
  codex:
    enabled: false
    command: codex
    model: ""
    effort: ""
    full_access: true
    mode: headless
  copilot-cli:
    enabled: true
    command: D:/Foreman/tools/copilot/copilot.exe
    model: gpt-5.5
    effort: high
    full_access: true
    mode: headless
```

注意：

- `config.yaml` 是本地文件，不应提交。
- 如果 `copilot` 已加入 PATH，可以用 `command: copilot`。
- 如果 PATH 在 Foreman 进程中没刷新，使用绝对路径更稳。

## 8. 安全设计

### 8.1 权限边界

Copilot CLI 作为本地执行 agent，本质上可以读写文件和执行命令。

Foreman 应继续依赖现有防线：

1. workspace allowlist：只能在已配置工作区启动。
2. gates：危险操作仍由 Foreman PM / 决策卡拦截。
3. `remote_execution_enabled`：远端控制默认关闭。
4. `full_access`：用户显式开启后才传 `--allow-all*`。

### 8.2 密钥处理

Copilot CLI BYOK 密钥来自环境变量：

```text
COPILOT_PROVIDER_API_KEY
```

禁止：

- 禁止把密钥写进 `config.example.yaml`。
- 禁止在 `/api/settings/agents` 返回密钥。
- 禁止把密钥记录到 event payload。
- 禁止把完整 child process env 持久化。

### 8.3 命令显示脱敏

`agent_start` event 当前会记录 `command`。

Copilot CLI 命令本身不应包含 API key，因此安全。

如果未来允许在 command 中写环境变量或 token，应增加脱敏逻辑。

## 9. 测试计划

### 9.1 单元测试

新增：

```text
tests/test_copilot_adapter.py
```

覆盖：

1. `_build_cmd()` 基本命令。
2. model 参数：`--model gpt-5.5`。
3. effort 参数：`--effort high`。
4. full_access=true 时传：
   - `--allow-all-tools`
   - `--allow-all-urls`
  - `--add-dir <workspace>`
5. 默认不传 `--allow-all-paths`；只有显式高危配置开启后才传。
6. full_access=false 时不传自动授权。
7. `--no-auto-update` 始终存在。
8. `--output-format json` 始终存在。
9. resume / session-id 策略正确。

### 9.2 Runner 测试

更新或新增：

```text
tests/test_runner.py
```

覆盖：

- `default_agents()` 包含 `copilot-cli`。
- `Runner.sync_config()` 会注册 `CopilotCliAdapter`。
- 禁用 `copilot-cli` 后不注册。

### 9.3 设置 API 测试

更新：

```text
tests/test_local_api.py
```

覆盖：

- `/api/settings/agents` 返回 `copilot-cli`。
- POST 可保存 `copilot-cli` 的 command/model/effort/full_access。
- 不允许未知 agent。
- 至少启用一个 agent 的校验仍有效。

### 9.4 前端静态测试

更新：

```text
tests/test_web_page.py
```

覆盖：

- `app.js` 中本地 Agent 设置页仍动态渲染 agent rows。
- 文案包含 Copilot CLI / BYOK 环境变量提示。
- `node --check src/foreman/server/web/app.js` 通过。

### 9.5 集成测试

本机真实测试：

1. 安装 Copilot CLI。
2. 设置 BYOK 环境变量。
3. `copilot -p "只输出 OK" --silent --no-auto-update --allow-all-tools --allow-all-urls --add-dir <workspace> --effort high` 返回 OK。
4. 启动 Foreman。
5. 设置页确认 `copilot-cli` 为 OK。
6. 禁用 `claude-code` / `codex`。
7. 使用 `copilot-cli` 派发一个只读任务。
8. 确认时间线出现：
   - `agent_start`
   - `agent_output`
   - `stop`
9. 远端控制开启后，从 PWA 派发到本机，确认使用 `copilot-cli` 建会话。

## 10. 开发任务拆解

本节把实现拆成可分配、可验收的任务卡。推荐按顺序开发；每张任务卡完成后都应跑对应测试，避免到最后才发现“积木搭歪了”。

### T0：预研与命令实测

目标：确认当前 Copilot CLI 版本的真实参数、输出格式、退出行为，避免 adapter 按猜测开发。

涉及文件：

- `docs/COPILOT_CLI_AGENT_PLAN.zh-CN.md`
- 可选新增：`docs/COPILOT_CLI_JSONL_OBSERVATION.zh-CN.md`

步骤：

1. 在开发机记录 `copilot --help`、`copilot version`、`copilot -p` 的实际输出。
2. 用 BYOK 环境变量跑一次只读 smoke：要求返回稳定文本，例如“只输出 OK”。
3. 分别验证以下参数是否存在且行为符合预期：
  - `-p` / prompt 参数。
  - `--model`。
  - `--effort` 或 `--reasoning-effort`。
  - `--no-auto-update`。
  - `--output-format json`。
  - `--session-id` / `--resume` / `--continue`。
  - `--add-dir`。
  - `--allow-all-tools`、`--allow-all-urls`、`--allow-all-paths`。
4. 保存至少一段 JSON/JSONL 样例，包含：普通输出、reasoning、工具调用、错误退出。
5. 记录退出码规则：成功、模型/密钥错误、权限/交互确认、参数错误分别如何表现。

验收标准：

- 明确 Copilot CLI 的首选命令形态。
- 明确默认不使用 `--allow-all-paths` 是否可行。
- 明确 JSON 输出是否真的是 JSONL；如果不是，adapter 设计必须改为兼容整段 JSON 或纯文本。

### T1：后端 adapter MVP

目标：新增 `copilot-cli` adapter，能以 headless 子进程形式启动、收集输出、产生 Foreman 事件。

涉及文件：

- 新增：`src/foreman/client/agents/copilot_cli.py`
- 可能修改：`src/foreman/client/agents/_subprocess.py`
- 新增：`tests/test_copilot_adapter.py`

步骤：

1. 新增 `CopilotCliAdapter`，注册名固定为 `copilot-cli`。
2. 先复用 `SubprocessCliAdapter` 的进程管理能力，但只在 Copilot adapter 内做必要覆写，避免影响 Claude/Codex。
3. 构造首次启动命令：
  - 包含 `self.cfg.command`。
  - 包含 prompt 参数。
  - 包含 `--no-auto-update`。
  - 包含 `--output-format json`，如果 T0 证明不可用，则改为兼容文本输出。
  - 有 model 时传 `--model <model>`。
  - 有 effort 时传 `--effort <effort>` 或 T0 确认的参数名。
  - full_access=true 时传 `--allow-all-tools --allow-all-urls --add-dir <workspace>`。
  - 默认不传 `--allow-all-paths`。
4. 处理工作区参数：由于当前 `_build_cmd()` 不带 workspace，优先在 `CopilotCliAdapter.start()` / `send()` 中封装 workspace-aware 命令，不改公共基类签名。
5. 实现续接命令：优先使用 `--session-id <Foreman session_id>`，不要使用“最近会话”语义的 `--continue` 作为默认。
6. 保证 event 行为：
  - 启动时产生 `agent_start`。
  - stdout 文本至少映射为 `agent_output`。
  - reasoning 若能识别则映射为 `agent_reasoning`。
  - 非零退出码产生 `error`。
  - 成功退出必须让 session 能进入结束态，不能卡在 running。
7. 单测覆盖命令构造、full_access 参数、workspace 参数、session-id 续接、非 JSON 输出降级。

验收标准：

- `tests/test_copilot_adapter.py` 通过。
- 不修改或破坏 `CodexAdapter` / `ClaudeCodeAdapter` 的命令构造。
- 不在事件 payload 中出现 API key 或完整环境变量。

### T2：配置、Runner 与 API 接入

目标：让后端支持保存、启用、注册 `copilot-cli`，但默认不改变老用户运行路径。

涉及文件：

- `src/foreman/shared/config.py`
- `src/foreman/client/agents/runner.py`
- `src/foreman/server/app.py`
- `config.example.yaml`
- `tests/test_runner.py`
- `tests/test_local_api.py`

步骤：

1. 在 `default_agents()` 增加 `copilot-cli`，默认 `enabled=False`。
2. 默认 command 使用 `copilot`，不要写入本机绝对路径。
3. 默认 model 为空或示例值由用户配置；不要把当前机器的 `gpt-5.5` 强行写成全局默认，除非项目决定默认绑定该模型。
4. 默认 effort 使用 `high`，但保存时仍复用现有 effort 校验。
5. 在 `Runner.sync_config()` 中仅当 `copilot-cli.enabled` 为 true 时注册 `CopilotCliAdapter`。
6. 确认 `_SUPPORTED_AGENTS = frozenset(default_agents())` 能自动允许 `copilot-cli` 设置保存。
7. 确认 `/api/settings/agents` 能返回 disabled 的 `copilot-cli` 行，供 UI 显示。
8. 确认 `/api/agents` 只返回 enabled agent；如果命令不可用是否过滤，可作为 T4 增强处理。
9. 更新 `config.example.yaml`，只给示例，不写密钥。

验收标准：

- 老配置只启用 `claude-code` / `codex` 时，`/api/agents` 返回结果不变。
- 新配置启用 `copilot-cli` 后，`/api/agents` 出现 `copilot-cli`。
- 禁用 `copilot-cli` 后，Runner 不注册它。
- 未知 agent 仍被拒绝。

### T3：PM 自动规划接入

目标：让 PM 自动选择和工具循环真正认识 `copilot-cli`，否则远端“自动派发”会回退到旧 agent。

涉及文件：

- `src/foreman/client/core/pm_agent.py`
- `src/foreman/client/tools/loop.py`
- 相关 PM/dispatch 测试文件

步骤：

1. 将 `copilot-cli` 加入 `VALID_AGENTS`。
2. 将 `copilot-cli` 加入 `_DEFAULT_PLAN_AGENTS`，或更理想地让默认候选完全来自运行时传入的 enabled agents。
3. 检查 `parse_plan()`：当 LLM 返回 `agent=copilot-cli` 且它在 enabled_agents 中时，不能被过滤掉。
4. 检查 `_simple_reply_plan()`：简单任务也能在只启用 `copilot-cli` 时选择它。
5. 检查 PM tool loop fallback：当 enabled 只有 `copilot-cli` 时，fallback plan 不应回退到 `claude-code`。
6. 增加测试：
  - only `copilot-cli` enabled 时，PM plan 使用 `copilot-cli`。
  - `claude-code` / `codex` 老配置下行为不变。
  - LLM 返回未知 agent 时仍安全回退。

验收标准：

- PM 自动选择、工具循环、简单任务路径均能选择 `copilot-cli`。
- 未启用 `copilot-cli` 时，PM 不会选择它。
- 老 agent 的优先级和行为不出现意外变化。

### T4：命令诊断与可用性过滤

目标：让设置页能告诉用户 Copilot CLI 是否可用，并降低误派发到不可执行命令的概率。

涉及文件：

- `src/foreman/server/app.py`
- `src/foreman/server/web/app.js`
- `tests/test_local_api.py`

步骤：

1. 核对 `_agent_status()` 当前通用 version 检测是否适配 `copilot`。
2. 如通用检测不可靠，为 `copilot-cli` 增加 version 命令映射，优先使用 T0 验证过的命令。
3. 设置页展示：
   - command resolved path 或“不显示敏感路径”的简化信息。
   - version。
   - not found / timeout / failed 的友好提示。
4. 决策是否让 `/api/agents` 过滤不可用命令：
   - MVP 可暂不改，只在设置页提示。
   - 如果过滤，需要避免破坏现有测试和用户手工配置场景。
5. 增加文案提示：配置 Copilot BYOK 环境变量后，需要重启 Foreman 进程。

验收标准：

- Copilot CLI 已安装时，设置页显示 OK/version。
- 未安装时，设置页显示未找到，但不会影响 Claude/Codex 使用。
- POST 保存 agent 设置后 Runner 配置同步生效。

### T5：远端控制与 display-safe metadata

目标：远端不仅能透传 `agent=copilot-cli`，还知道目标机器是否支持它，并能显示执行结果。

涉及文件：

- `src/foreman/client/cache_sync.py`
- `src/foreman/server/app.py`
- `src/foreman/server/auth_manager.py`
- `src/foreman/server/web/app.js`
- 远端 relay / snapshot 相关测试

步骤：

1. 保持 `/api/dispatch` 协议不变，继续透传 `agent/model/effort/session_id`。
2. 在本机 snapshot 或 process metadata 中加入 display-safe agents 列表，字段建议：
  - `name`
  - `enabled`
  - `ok`
  - `model`
  - `effort`
3. 不上报 API key、完整 env、完整命令行、敏感本地绝对路径。
4. 远端 UI 的 agent selector 读取目标机器的 agents metadata。
5. 如果 metadata 暂不可用，UI 应保留 PM 自动选择，不阻塞远端派发。
6. 验证 `remote_execution_enabled=false` 时，远端 dispatch 被拒绝。
7. 验证 `remote_execution_enabled=true` 且本机启用 `copilot-cli` 时，远端可指定 `copilot-cli`。
8. 验证远端 snapshot 中 session 显示 `agent_type=copilot-cli`。

验收标准：

- 远端页面能看出目标机器支持 `copilot-cli`。
- 远端派发到 `copilot-cli` 后，session/card/event 显示正常。
- 关闭远端执行总开关时，控制链路拒绝执行但显示链路仍可用。

### T6：前端设置与派发体验

目标：把 `copilot-cli` 做成用户可理解、可配置、可安全启用的选项。

涉及文件：

- `src/foreman/server/web/app.js`
- `tests/test_web_page.py`

步骤：

1. 设置页本地 Agent 列表自动显示 `copilot-cli`。
2. 增加 Copilot CLI 专属说明：它是执行 agent，不是 PM Brain provider。
3. 增加 BYOK 环境变量提示：配置后需重启 Foreman。
4. 工具全开提示改成 workspace 限定表达，避免误解为默认全盘访问。
5. 如果未来加入“允许所有路径”开关，应单独作为高危选项，不复用普通 full_access 文案。
6. 派发表单能选择 `copilot-cli`；PM 自动选择文案不需要特殊化。
7. 远端页面 agent selector 如果有目标机器 metadata，应显示 `copilot-cli` 的可用状态。

验收标准：

- `node --check src/foreman/server/web/app.js` 通过。
- 设置页保存/刷新后 `copilot-cli` 状态保持一致。
- 文案不暗示 API key 会由 Foreman 保存。

### T7：输出解析增强与会话续接

目标：在 MVP 可运行基础上，提高时间线可读性和多轮 follow-up 稳定性。

涉及文件：

- `src/foreman/client/agents/copilot_cli.py`
- `tests/test_copilot_adapter.py`
- 事件解析相关测试

步骤：

1. 基于 T0 样例覆盖 `_line_to_event()` 或 adapter 专属解析函数。
2. 将 assistant 文本映射为 `agent_output`。
3. 将 reasoning/thinking 映射为 `agent_reasoning`。
4. 将 tool call start/done 映射为可读 output，除非项目已有专门事件类型。
5. 尝试提取 Copilot 原生 session id，写入 `handle.native_session_id`。
6. `send()` 优先使用 Foreman session id 或 native session id 续接同一上下文。
7. 对未知 JSON 字段保守降级，不丢原始 payload。

验收标准：

- 时间线不只是大段原始 JSON，而能看到可读输出。
- 未知 JSON schema 不会导致 adapter 崩溃。
- follow-up 能继续同一任务上下文，至少不会串到最近其它会话。

### T8：回归、真实 smoke 与 E2E review issue

目标：验证新能力上线，同时符合项目多 agent 协作规约。

涉及内容：

- 后端单测。
- 前端语法/静态测试。
- 本机真实 Copilot CLI smoke。
- 远端控制 smoke。
- GitHub `needs-e2e` review issue。

步骤：

1. 跑 Copilot adapter 单测、Runner 测试、local API 测试。
2. 跑 PM 自动选择相关测试。
3. 跑 web 静态测试和 `node --check`。
4. 在当前机器启用：
  - `claude-code.enabled=false`
  - `codex.enabled=false`
  - `copilot-cli.enabled=true`
5. 本机派发只读任务，确认出现 `agent_start / agent_output / stop` 或明确错误。
6. 远端开启 `remote_execution_enabled=true`，从 PWA 指定 `copilot-cli` 派发只读任务。
7. 远端关闭 `remote_execution_enabled=false`，确认派发被拒绝。
8. 确认老配置场景：只启用 Claude/Codex 时，派发和 PM 自动选择行为不变。
9. 功能完成后按 `AGENTS.md` 创建 `[Review]` issue 并打 `needs-e2e`，正文写清入口、配置前置、验收点。

验收标准：

- 单元测试、Ruff、JS 检查通过。
- 本地和远端 smoke 均通过。
- 已创建 E2E review issue，方便后续真机复验。

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| Copilot CLI JSON schema 变动 | 输出解析失效 | MVP 保守保留原 payload，增强解析做兼容分支 |
| Copilot CLI 需要交互确认 | headless 卡住 | full_access=true 时传 `--allow-all*`；否则提示用户不建议 headless |
| 用户环境变量未被 Foreman 进程继承 | provider 调用失败 | 设置页提示重启 Foreman；后续增加环境检测 |
| 多会话用 `--continue` 串线 | 远端控制错会话 | 使用 `--session-id <Foreman session_id>` 而不是 `--continue` |
| 工具全开权限过大 | 安全风险 | 默认用 `--add-dir <workspace>` 限定路径；`--allow-all-paths` 作为高危显式选项 |
| 未安装 Copilot CLI | 派发失败 | 设置页命令诊断显示未找到；建议 `/api/agents` 只返回可用 agent |
| PM 自动规划白名单未更新 | PM 仍只会选择 Claude/Codex | 更新 `VALID_AGENTS` / `_DEFAULT_PLAN_AGENTS` 并加测试 |
| 远端不知道本机有哪些 agent | 控制台无法选择 `copilot-cli` | snapshot/process metadata 增加 display-safe agents 列表 |

## 12. 验收清单

- [ ] `copilot-cli` 出现在设置页本地 Agent 列表。
- [ ] `copilot-cli` 命令诊断能显示版本或 OK。
- [ ] 用户可以保存 `command/model/effort/full_access/enabled`。
- [ ] 派发表单可选择 `copilot-cli`。
- [ ] 禁用 `codex` / `claude-code` 后，仍可用 `copilot-cli` 派发任务。
- [ ] 任务输出进入现有时间线和原始输出面板。
- [ ] 任务结束能产生 stop 或错误 event。
- [ ] `send()` 能继续同一任务上下文，至少不崩溃。
- [ ] 远端控制下发任务可路由到本机 `copilot-cli`。
- [ ] 远端页面能看到目标机器支持 `copilot-cli`，或至少能通过 PM 自动选择正确派发。
- [ ] 远端 snapshot 中 `agent_type=copilot-cli` 的 session 能正确显示。
- [ ] `remote_execution_enabled=false` 时远端派发被拒绝。
- [ ] 老配置只启用 `claude-code` / `codex` 时，派发、PM 自动规划和 `/api/agents` 行为不变。
- [ ] 单元测试、Ruff、JS 语法检查通过。

## 13. 推荐开发顺序

推荐以“先不影响老功能，再逐步接入远端”的顺序推进：

1. **T0 预研先行**：确认 Copilot CLI 参数、JSON/JSONL schema、退出码和 session 续接能力。
2. **T1 做最小 adapter**：先让 `copilot-cli` 能被 Runner 启动并产出基本事件，不急着做复杂解析。
3. **T2 接入配置和 API**：让设置页能看到 disabled 的 `copilot-cli`，启用后 Runner 才注册。
4. **T3 接入 PM 自动规划**：更新 `VALID_AGENTS` / `_DEFAULT_PLAN_AGENTS`，保证 PM 自动选择不会忽略 `copilot-cli`。
5. **T4 做命令诊断**：确认设置页能显示 Copilot CLI 是否可用，并提示重启 Foreman 继承环境变量。
6. **T6 做前端本地体验**：完善设置页、派发表单和风险文案。
7. **T5 做远端 metadata 与控制验证**：先保证远端能知道目标机器支持哪些 agent，再验证 `/api/dispatch` 指定 `copilot-cli`。
8. **T7 做输出解析增强**：基于真实 schema 优化 reasoning/tool/final 显示和 `send()` 续接。
9. **T8 做总回归与 E2E review issue**：跑单测、JS 检查、本地 smoke、远端 smoke，并按 `AGENTS.md` 创建 `needs-e2e` review issue。

如果排期紧张，可以分两批发布：

- **第一批 MVP**：T0、T1、T2、T3、T4、T6，加本地 smoke；目标是本机可启用、可派发、老功能不受影响。
- **第二批远端增强**：T5、T7、T8；目标是远端可选择目标机器 agent、输出更可读、E2E 流程闭环。

## 14. 当前机器建议配置

这台机器已安装 Copilot CLI 到：

```text
D:\Foreman\tools\copilot\copilot.exe
```

建议本地 `config.yaml` 最终使用：

```yaml
agents:
  claude-code:
    enabled: false
    command: claude
    model: ""
    effort: ""
    full_access: true
    mode: headless
  codex:
    enabled: false
    command: codex
    model: ""
    effort: ""
    full_access: true
    mode: headless
  copilot-cli:
    enabled: true
    command: D:/Foreman/tools/copilot/copilot.exe
    model: gpt-5.5
    effort: high
    full_access: true
    mode: headless
```

同时保持用户级环境变量：

```text
COPILOT_PROVIDER_BASE_URL=https://api.kongsites.com/v1
COPILOT_PROVIDER_TYPE=openai
COPILOT_PROVIDER_WIRE_API=responses
COPILOT_MODEL=gpt-5.5
COPILOT_PROVIDER_MODEL_ID=gpt-5.5
COPILOT_PROVIDER_WIRE_MODEL=gpt-5.5
COPILOT_PROVIDER_API_KEY=<已设置>
```

完成以上后，Foreman 即可把 Copilot CLI 当作第三种本地执行 agent 使用。
