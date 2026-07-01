# Version History / 版本历史

This file is the human-readable release history for Foreman. The runtime package version still has a single source of truth: `src/foreman/__init__.py::__version__`.

Foreman 的运行版本仍然只有一个代码来源：`src/foreman/__init__.py` 的 `__version__`。本文件只维护给人看的中英文版本历史。

## v1.4.4

English:

- Added the Codex-style Context v2 design document covering structured frames, recoverable checkpoints, PM active context envelopes, and remote `/responses/compact` as the preferred semantic compaction engine.
- Added the GPT-5.5 pro implementation task book with commit-by-commit sequencing, validation gates, and review handoff requirements for every development round.
- PM context preparation now preserves high-signal timeline anchors for compaction fallback, and session views keep newly sent messages scrolled to the bottom.

中文：

- 新增 Codex 风格 Context v2 设计书，覆盖结构化 frames、可恢复 checkpoints、PM active context envelope，以及优先使用远端 `/responses/compact` 作为语义压缩引擎。
- 新增 GPT-5.5 pro 实施任务书，写明逐 commit 开发顺序、验收门禁，以及每轮开发后的 review 交付要求。
- PM 上下文压缩 fallback 会保留高信号时间线锚点；会话视图发送新消息后会保持滚动到底部。

## v1.4.3

English:

- Pytest maintenance now folds repetitive table-style checks into compact table-driven tests.
- A redundant client import smoke test was removed because the stronger fresh-interpreter client import boundary test already covers the same surface.
- The full suite keeps the same behavior coverage with fewer collected pytest nodes.

中文：

- Pytest 维护整理将重复的表格型断言合并为更紧凑的表驱动测试。
- 移除冗余的 client import smoke test，因为更强的新解释器 client import 边界测试已经覆盖同一范围。
- 全量测试继续保留相同行为覆盖，同时减少 pytest 收集节点。

## v1.4.2

English:

- PM final plans can now include a verified target workspace/worktree path.
- When that path is either an allowed workspace or a git worktree registered under the current main workspace, Foreman updates `session.workspace` before launching the coding agent.
- The composer worktree/branch status, workspace file actions, and follow-up dispatches now see the same target worktree that the coding agent actually uses, instead of staying pinned to the main checkout.

中文：

- PM 最终计划现在可以携带已验证的目标 workspace/worktree 路径。
- 当该路径属于允许的工作区，或是当前 main workspace 登记过的 git worktree 时，Foreman 会在启动编码代理前更新 `session.workspace`。
- 输入区 worktree/branch 状态、工作区文件操作和后续下发现在会看到编码代理实际使用的目标 worktree，而不是继续停在 main checkout。

## v1.4.1

English:

- Context compression events now render as a collapsed transparent row instead of exposing raw ContextPack JSON in the timeline.
- Expanding the row shows parsed, pretty-printed ContextPack JSON and never falls back to raw summary text when parsing fails.
- The composer context meter now tracks the context that the next PM call will send to the LLM provider: raw event context before compaction, and the stored ContextPack after compaction.

中文：

- 上下文压缩事件现在默认显示为透明折叠行，不再在线程里裸露 ContextPack 原始 JSON。
- 展开折叠行后显示解析并格式化后的 ContextPack JSON；解析失败时也不会回显原始 summary 文本。
- 输入区上下文仪表盘现在按下一次 PM 调用真正发送给 LLM provider 的上下文计数：压缩前按事件上下文估算，压缩后按已保存的 ContextPack 计算。

## v1.4.0

English:

- Subagent cards now use a single chronological timeline for worker replies, launch commands, streamed command/tool steps, reasoning, and explicit final results.
- In-progress Codex output is shown as a normal reply at its event position instead of being mislabeled as the final reply.
- The Final reply label is only used for an explicit stop/result payload; runs without a detectable final answer no longer show a synthetic final section.
- Subagent titles now stay as the agent identity instead of borrowing model output, and Windows console output decoding preserves CJK text where the CLI emits local-encoded bytes.
- Inline workspace file references such as `docs/example.md` now open with the desktop file association or use an in-app text preview on mobile.

中文：

- 子代理卡片现在用同一条时间线展示 worker 回复、启动命令、流式命令/工具步骤、思考和明确的最终结果。
- Codex 运行中的输出会作为普通回复显示在事件发生位置，不再被误标为「最终回复」。
- 「最终回复」只用于明确的 stop/result 结果；无法判定最终答复的运行不再显示虚假的最终板块。
- 子代理标题现在固定为 agent 身份，不再借用模型输出；Windows 控制台输出按 UTF-8 和本地编码解码，保留 CLI 发出的中文文本。
- `docs/example.md` 这类工作区文件引用现在可点击：桌面按系统文件关联打开，手机使用内置文本预览。

## v1.3.9

English:

- Sessions now persist their original main workspace separately from the current session workspace.
- Follow-up dispatches use the live session worktree when it still exists and fall back to the recorded main workspace when the worktree is gone.
- The composer shows `worktree: none` for a new chat, and selected sessions can switch among existing local branches from a guarded dropdown that refuses dirty worktrees.

中文：

- 会话现在会把原始 main workspace 和当前会话 workspace 分开保存。
- 继续会话时，如果当前 session worktree 仍存在就继续使用；如果 worktree 已消失，则回退到记录下来的 main workspace。
- 输入区新对话会显示 `worktree: 无`；已选会话可通过受保护的下拉框切换已有本地分支，工作树有未提交改动时会拒绝切换。

## v1.3.8

English:

- PM tool activity now appears as a public timeline with tool-start labels, result summaries, and collapsible details.
- PM tools can provide optional `public_note` or `purpose` text for natural user-facing activity notes.
- PM thinking expansion no longer repeats the generated title inside the expanded body.

中文：

- PM 工具活动现在进入公开时间线，显示工具开始、结果摘要和可折叠详情。
- PM 工具可以提供可选的 `public_note` 或 `purpose`，用于展示更自然的用户可见活动说明。
- PM 思考展开后不再在正文里重复生成标题。

## v1.3.7

English:

- Read local agent stdout in byte chunks and reassemble JSONL records, avoiding asyncio's per-line reader limit for large Codex `exec --json` events.
- Preserve large command-output events without fixed-size truncation while keeping structured Codex process steps.
- Record stream reader failures as agent errors and stop the child process so a failed read does not leave the session half-failed.

中文：

- 本地 agent stdout 改为按字节块读取并自行组装 JSONL，避免 Codex `exec --json` 大事件触发 asyncio 单行读取上限。
- 保留大段命令输出事件，不再用固定大小截断，同时继续保留 Codex 结构化步骤。
- 读取流异常会记录为 agent 错误并停止子进程，避免会话提前失败后进程残留。

## v1.3.6

English:

- PM thinking summaries now render as collapsed transparent rows by default.
- The collapsed row uses the first generated bold reasoning title instead of a fixed label.
- Hovering the title changes the disclosure icon, and clicking it expands the complete reasoning text.
- The existing muted text styling is preserved for the title and expanded Markdown content.

中文：

- PM 思考摘要现在默认渲染为透明背景的折叠行。
- 折叠行使用 reasoning 里第一个加粗生成标题，不再显示固定标签。
- 鼠标悬浮标题时图标变化，点击后再展开完整思考内容。
- 标题与展开后的 Markdown 内容继续沿用现有的柔和文字颜色。


## v1.3.5

English:

- Update checks now fetch the human-readable version history from `docs/VERSION_HISTORY.md`.
- The update dialog shows every release note between the installed exe version and the latest available release.
- The automated GitHub Release build text is kept only as a fallback when the version history cannot be loaded.

中文：

- 更新检查现在会读取 `docs/VERSION_HISTORY.md` 中给人看的版本历史。
- 更新弹窗会显示已安装 exe 版本到最新可用版本之间每个版本的更新说明。
- 自动 GitHub Release 构建文本只在版本历史加载失败时作为兜底显示。

## v1.3.4

English:

- Added a compact copy icon to user conversation bubbles.
- Added the same quick-copy control to PM reply bubbles.
- Reused the existing clipboard and copied-toast path across desktop and mobile session views.

中文：

- 用户会话泡泡底部增加小复制图标。
- PM 回复泡泡底部增加同样的快速复制入口。
- 桌面与移动会话视图复用现有剪贴板和“已复制”提示流程。

## v1.3.3

English:

- Hid server-side git and diagnostic subprocess windows in the packaged Windows exe.
- Fixed a transient cmd window flash when switching sessions after workspace git status was added.
- Kept workspace git status, branch display, and explicit git initialization behavior unchanged.

中文：

- 在打包 Windows exe 中隐藏服务端 git 与诊断子进程窗口。
- 修复工作区 git 状态上线后，切换会话时短暂闪出 cmd 窗口的问题。
- 保持工作区 git 状态、branch 显示和显式新建 git 仓库行为不变。

## v1.3.2

English:

- Replaced the composer success message with workspace git status.
- Shows the selected workspace's git worktree and branch when available.
- Offers an explicit Initialize git repo action for configured workspaces that are not git repositories.
- Preserves and displays each session's saved workspace after reopening the app or switching sessions.

中文：

- 将会话输入区的成功发送提示替换为工作区 git 状态。
- 可显示所选工作区的 git worktree 与 branch。
- 对已配置但还不是 git 仓库的工作区，提供显式「新建 git 仓库」按钮。
- 重开软件或切换会话后，按会话记录保存的工作区显示与继续发送。

## v1.3.1

English:

- Preserved the raw leading spaces on PM reasoning stream deltas.
- Fixed English thought summaries rendering as glued-together words during streaming.

中文：

- 保留 PM 思考流 delta 片段里的原始前导空格。
- 修复英文思考摘要在流式显示时单词粘连的问题。

## v1.3.0

English:

- Changed packaged exe self-update from a top banner to a modal dialog.
- Added live download progress from the local updater state endpoint.
- Added a cancel button that aborts the download before the restart/swap phase begins.

中文：

- 将打包 exe 自更新从顶部提示改为弹窗模式。
- 从本地 updater 状态接口显示实时下载进度。
- 增加取消按钮，可在进入重启/替换阶段前中止下载。

## v1.2.9

English:

- Added a Check for updates button to the Version page.
- Reworked the Version page and README version sections around one historical update list.
- Moved the current release notes into that same history list.

中文：

- 在版本页增加检查更新按钮。
- 将版本页与 README 的版本说明重构为一个历史更新列表。
- 将当前版本更新说明一并放入同一个历史更新列表。

## v1.2.8

English:

- Streamed PM `run_command` stdout and stderr into live `tool_stream` events and `.foreman/tool-logs/*.log` artifacts.
- Removed the static PM shell command allowlist; Gate, Auditor, and explicit user approval now govern command execution.
- Removed PM shell execution timeouts and made cancellation terminate the process tree.
- Required admin elevation for PyInstaller-built `foreman.exe`.

中文：

- 将 PM `run_command` 的 stdout 和 stderr 流式写入实时 `tool_stream` 事件与 `.foreman/tool-logs/*.log` 文件。
- 移除 PM shell 静态命令允许列表，命令执行改由 Gate、Auditor 和用户显式审批约束。
- 移除 PM shell 执行超时，并让取消操作终止整个进程树。
- PyInstaller 构建的 `foreman.exe` 要求管理员权限启动。

## v1.2.7

English:

- Rendered PM reasoning summaries through the existing Markdown renderer instead of a tiny monospace pre block.
- Added light formatting so concatenated reasoning headings get readable paragraph breaks.
- Localized the Chinese PM reasoning label to `思考摘要`.

中文：

- 将 PM 思考摘要改为通过现有 Markdown 渲染器显示，不再使用过小的等宽预格式块。
- 增加轻量格式化，让拼接在一起的 reasoning 标题拥有可读段落间距。
- 将中文 PM reasoning 标签本地化为 `思考摘要`。

## v1.2.6

English:

- Added a stop action in active conversations so a running session can be cancelled from the composer area.
- Merged the busy follow-up controls into one queued send button.
- Replaced task composer model and thinking-level freeform/segmented controls with dropdowns.
- Added pasted image chips for Ctrl+V image clipboard input.
- Surfaced PM reasoning chunks as a small gray streaming trace, including OpenAI-compatible HTTP tool-call streams.

中文：

- 在运行中的会话里增加停止入口，可从输入区直接取消当前会话。
- 将运行中继续发送的多个按钮合并为一个排序发送按钮。
- 将任务输入区的模型和 thinking level 控件改成下拉框。
- 支持 Ctrl+V 粘贴图片并显示为附件 chip。
- 将 PM reasoning chunk 用灰色小字流式显示，并覆盖 OpenAI 兼容 HTTP tool-call 流。

## v1.2.5

English:

- Added a PM recovery decision step after fatal local coding-agent failures.
- Excluded failed agents from recovery candidates and stopped only when all enabled local agents were unavailable.
- Relaunched the PM-selected replacement agent through the Foreman runtime with fresh work-mode injection and cursor tracking.

中文：

- 在本地编码 agent 致命失败后增加 PM 恢复决策步骤。
- 从恢复候选里排除失败 agent，仅在所有已启用本地 agent 都不可用时停止。
- 通过 Foreman runtime 重新启动 PM 选择的替代 agent，并注入新的工作方式上下文与游标跟踪。

## v1.2.4

English:

- Set `COPILOT_PROVIDER_WIRE_API=responses` when Foreman launches Copilot CLI with GPT-5 series models.
- Kept non-GPT-5 Copilot launches unchanged.

中文：

- Foreman 使用 GPT-5 系列模型启动 Copilot CLI 时设置 `COPILOT_PROVIDER_WIRE_API=responses`。
- 非 GPT-5 的 Copilot 启动行为保持不变。

## v1.2.3

English:

- Removed the redundant auto-agent explanatory copy from the task composer and dispatch timeline chips.
- Kept PM-driven agent selection behavior unchanged.

中文：

- 从任务输入区和下发时间线标签中移除冗余的自动执行 agent 说明文案。
- PM 自动选择执行 agent 的实际行为不变。

## v1.2.2

English:

- Removed the configurable PM provider max output token setting from the Settings UI and settings API.
- Stopped sending output cap fields to OpenAI-compatible Chat Completions and Responses WebSocket providers.
- Kept an internal Anthropic default because the Messages API requires `max_tokens`.

中文：

- 从设置页和设置 API 移除 PM Provider 的可配置最大输出 token。
- 停止向 OpenAI 兼容 Chat Completions 与 Responses WebSocket Provider 发送输出上限字段。
- Anthropic Messages API 必须传 `max_tokens`，因此仅保留代码内默认值。

## v1.2.1

English:

- Added bilingual version information to the GitHub README.
- Added an in-exe `Version` page in the Foreman console.
- Added a visible historical update record instead of showing only the latest change.
- Updated `AGENTS.md` so every version bump must include release notes and history updates.

中文：

- 在 GitHub README 中增加中英文版本信息。
- 在 Foreman exe 控制台中增加「版本」页面。
- 增加可见的历史更新记录，不再只显示最新一次更新。
- 更新 `AGENTS.md`，要求每次版本号变更都必须同步维护更新说明和历史记录。

## v1.2.0

English:

- Exposed PM context token limits in the product configuration flow.

中文：

- 在产品配置流程中暴露 PM 上下文 token 上限设置。

## v1.1.9

English:

- Added the PM `askQuestion` decision tool.

中文：

- 增加 PM `askQuestion` 决策工具。

## v1.1.8

English:

- Fixed cloud relay offline flap handling for the packaged exe flow.

中文：

- 修复打包 exe 场景下云端 relay 离线反复跳变的处理。

## v1.1.7

English:

- Added automatic UI language detection.

中文：

- 增加 UI 语言自动检测。

## v1.1.6

English:

- Raised and clamped PM tool evidence rounds to support larger investigation runs.

中文：

- 提高并限制 PM 工具取证轮次，支持更长的取证运行。
