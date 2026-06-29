# Version History / 版本历史

This file is the human-readable release history for Foreman. The runtime package version still has a single source of truth: `src/foreman/__init__.py::__version__`.

Foreman 的运行版本仍然只有一个代码来源：`src/foreman/__init__.py` 的 `__version__`。本文件只维护给人看的中英文版本历史。

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
