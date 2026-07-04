# Packaged exe E2E BUG 清单

测试时间: 2026-07-04 10:53-11:00 CST

测试对象:
- packaged exe: `E:\AutoWorkAgent\dist\foreman.exe`
- exe 文件时间: 2026-06-30 11:12:54
- exe 文件大小: 77,270,348 bytes
- exe 自报版本: `v1.2.7`
- 测试启动命令: `foreman.exe app --host 127.0.0.1 --port 18788`
- 测试工作目录: `E:\AutoWorkAgent-packaged-exe-e2e-20260704`
- 对照源码: `origin/main` at `fb95a14 Tighten PM worktree safety constraints`
- 对照源码版本: `src/foreman/__init__.py` = `1.5.2`

## E2E 覆盖

- `foreman.exe version`: 退出码 0，日志输出 `Foreman v1.2.7`。
- app 启动: `http://127.0.0.1:18788/health` 返回 200。
- API smoke:
  - `GET /health` -> 200, `{"ok": true, "version": "1.2.7", "agents": ["claude-code", "codex"]}`
  - `GET /api/sessions` -> 200, `[]`
  - `GET /api/workspaces` -> 200, `[]`
  - `GET /api/settings/llm` -> 200，返回 provider/model/transport 等配置。
- 浏览器桌面 E2E:
  - 首屏工作台可渲染。
  - 设置页可渲染本地 agent、PM 大脑、PM 工具、云端连接等配置。
  - 版本页可渲染当前版本和历史版本。
  - 前端 console error/warn: 未发现。
- 浏览器移动视口 E2E:
  - 390x844 下无横向溢出。
  - 底部导航和移动输入栏可见。
  - 页面稳定后启动浮层消失。
  - 前端 console error/warn: 未发现。

## BUG-001: 本机 packaged exe 严重滞后，无法代表当前 main 做验收

严重度: P1

实际结果:
- 本机 `E:\AutoWorkAgent\dist\foreman.exe` 自报 `v1.2.7`。
- 当前 `origin/main` 的 `src/foreman/__init__.py` 是 `1.5.2`。
- 页面首屏出现 `发现新版本 · v1.5.2`，侧栏仍显示 `本地工作台 · v1.2.7`。

复现步骤:
1. 从隔离 worktree 启动 `E:\AutoWorkAgent\dist\foreman.exe app --host 127.0.0.1 --port 18788`。
2. 打开 `http://127.0.0.1:18788/`。
3. 观察首屏版本提示，或访问 `http://127.0.0.1:18788/health`。

期望结果:
- 用于 packaged exe 真机 E2E 的本地 exe 应与当前待验收 main/release 版本一致。
- `foreman.exe version`、`/health.version`、UI 侧栏版本应一致。
- 如果当前 main 已是最新 release，则不应在刚启动后提示本机 exe 落后多个版本。

验收标准:
- 重新构建或更新本机 packaged exe 后，`foreman.exe version` 输出 `Foreman v1.5.2` 或当时最新 main/release 版本。
- `GET /health` 返回同一版本。
- 首屏不再因为本机包过旧而提示跨多个版本更新。

E2E 回归标准:
- 用更新后的 exe 重新跑本清单的启动、桌面、移动、设置、版本页检查。
- 回归记录必须包含 exe 路径、文件时间、`foreman.exe version`、`/health` 输出。

## BUG-002: 无工作区发送任务时，旧 packaged UI 会丢失用户输入

严重度: P2

实际结果:
- 测试环境没有配置工作区: `GET /api/workspaces` 返回 `[]`。
- 在工作台输入 `packaged exe E2E no-workspace dispatch smoke` 后点击 `发送 ↑`。
- 未创建会话: `GET /api/sessions` 仍返回 `[]`。
- UI 切到设置页展示 `未配置工作区：请到设置页添加项目路径。`。
- 输入框内容被清空，用户刚输入的任务文本丢失。

复现步骤:
1. 使用空工作区配置启动 packaged `v1.2.7` exe。
2. 打开工作台。
3. 在任务输入框输入任意任务文本。
4. 点击 `发送 ↑`。
5. 查看会话列表和输入框状态。

期望结果:
- 无工作区时，发送按钮应禁用；或点击发送后保留用户输入并明确提示需要先配置工作区。
- 后端拒绝时，前端不得清空用户已输入但未成功下发的任务。
- 用户修正工作区后，应能继续使用原草稿。

验收标准:
- 无工作区时点击发送不会清空 composer 草稿。
- 页面显示明确错误: `未配置工作区：请到设置页添加项目路径。`
- `GET /api/sessions` 仍为空，且 UI 不误报任务已发送。

E2E 回归标准:
- 在全新本地数据目录启动更新后的 packaged exe。
- 保持 `/api/workspaces` 为空。
- 输入一条任务并点击发送。
- 断言草稿仍存在，错误提示可见，`/api/sessions` 仍为空。

备注:
- 当前 `origin/main` 源码已能看到 no-workspace 前置拦截，并且只在 `/api/tasks` 成功后清空输入；因此这个问题可能已在源码侧修复，但需要更新 packaged exe 后真机回归确认。

## 待确认观察项

- `%TEMP%\foreman-app.log` 中存在历史 `RuntimeError: Event loop is closed` 与 pywebview 临时目录删除失败记录；本次日志尾部也包含这些历史内容，但没有单独证明它们由本轮启动新产生。暂不列为确定 BUG，建议后续在清空日志后单独做 start/close 回归。

