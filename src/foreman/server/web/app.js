(function () {
  "use strict";

  const {
    html,
    useCallback,
    useEffect,
    useMemo,
    useRef,
    useState,
    api,
    friendlyError,
    tokenK,
    shortPath,
    formatTime,
    getToken,
    setToken,
    redirectToLogin,
    PROCESS_KEY,
  } = window.ForemanApp;

  const LANG_KEY = "foreman.lang";
  const THEME_KEY = "foreman.theme";
  const WORKSPACE_KEY = "foreman.workspace";
  const DEFAULT_CONTEXT_TOKENS = 272000;
  const PM_TOOLS_MIN_ROUNDS = 1;
  const PM_TOOLS_DEFAULT_ROUNDS = 6;
  const PM_TOOLS_MAX_ROUNDS = 999;

  // ---------------------------------------------------------------------------
  // i18n
  // ---------------------------------------------------------------------------
  const I18N = {
    zh: {
      productSubtitle: "本地工作台",
      newVersionReady: "新版本已发布", refreshNow: "刷新", later: "稍后",
      appUpdateReady: "发现新版本", updateNow: "立即更新", updating: "正在下载更新",
      updateFailed: "更新失败，请稍后重试或手动下载", updateDownloadProgress: "下载进度",
      updateStarting: "准备下载…", updateDownloading: "正在下载…", updateSwapping: "下载完成，正在重启…",
      updateCancel: "取消下载", updateCancelling: "正在取消…", updateSizeUnknown: "大小未知",
      navWorkspace: "工作台", navDecisions: "决策", navBriefings: "简报", navRules: "工作方式", navSettings: "设置", navVersion: "版本",
      workspaceSubtitle: "选择工作区，给本机 agent 下发任务。",
      decisionsSubtitle: "处理需要你确认的卡片和审批。",
      briefingsSubtitle: "把当前进展整理成可读状态。",
      rulesSubtitle: "维护工作流、技能、代码规范和验收标准 —— PM 规划时按相关性选用，干活时按需取用。",
      settingsSubtitle: "配置工作区、PM 大脑和界面偏好。",
      versionSubtitle: "查看当前版本、历史更新说明，并手动检查可用更新。",
      sessions: "会话", newSession: "新会话",
      editSessionTitle: "修改会话标题", sessionTitle: "会话标题", sessionTitleHint: "输入新的会话标题",
      sessionTitleEmpty: "会话标题不能为空。", sessionTitleTooLong: "会话标题太长了，请控制在 300 字以内。", sessionTitleUpdated: "会话标题已更新",
      launchTag: "正在唤醒你的工程包工头 —— 把活儿交给本地 agent，PM 大脑替你盯着。",
      launchEngine: "引擎已就绪 · PM Core",
      launchAgents: "连接本地 agent",
      launchLoad: "加载工作区与工作方式…",
      launchBrain: "唤醒 PM 大脑",
      personalMode: "团队工作台 · 本地同步",
      selectSessionHint: "从左侧选择一个会话，或在下方下发新任务。",
      running: "运行中", live: "运行中", done: "完成", queued: "排队", cancelled: "已取消",
      stalled: "已卡住",
      reasonWallClock: "规划超时（墙钟）", reasonNoProgress: "长时间无进展",
      reasonRepetition: "模型复读，已自动终止", reasonStalled: "规划被看门狗终止",
      autonomy: "自动权限", briefing: "生成简报", pmThinking: "PM 正在思考...",
      plan: "计划", approved: "已确认", active: "进行中",
      reply: "回复", commandsRun: "执行的命令", fileChanges: "文件改动",
      processLabel: "执行过程", finalReply: "最终回复", waitingResult: "等待结果…", noChanges: "暂无文件改动", executing: "正在执行",
      kCmd: "命令", kEdit: "改动", kRead: "读取", kFind: "检索", kWeb: "联网", kTool: "工具", kPlan: "计划", kThink: "思考",
      fkAdd: "新增", fkUpdate: "修改", fkDelete: "删除", noSteps: "暂无执行步骤", stepsWord: "步", changeDetail: "改动详情",
      open: "展开", hide: "收起",
      decisionNeeded: "需要你拍板", suggestion: "建议", showDiff: "看 diff",
      riskHigh: "高风险", riskMedium: "中风险", riskLow: "低风险",
      context: "上下文", compact: "压缩上下文", compacting: "压缩中...", compactDone: "上下文已压缩", compactFailed: "压缩失败",
      attach: "附件", modelPlaceholder: "模型·默认", thinkingLevel: "thinking level", thinkingTrace: "思考摘要",
      fast: "快速", std: "标准", deep: "深度", send: "发送", sendHint: "发送",
      guiding: "发送中…", queueing: "发送中…", guide: "发送", queueSend: "发送",
      guideHelp: "停止当前会话后再发送新指令。", queueHelp: "发送：等当前回复结束后继续处理，不等整轮 loop 完成。",
      composerPlaceholder: "继续和 PM 对话… 可添加附件，选择档位，或直接下指令",
      mComposerPlaceholder: "继续下指令…",
      tabTodos: "任务清单", tabSubagents: "子代理", tabTerminal: "原始输出",
      addStep: "添加一步… ⏎", todoHint: "清单由 PM 自动拆解；增一步会作为后续指令发给当前会话。",
      subSpawned: "派发了", subAgentsWord: "个子代理",
      mTabChat: "对话", mTabTodo: "清单", mTabSub: "子代理", mTabTerm: "输出",
      decisions: "决策", decisionCards: "决策卡", approvals: "审批",
      noDecisions: "暂无待决策。", noApprovals: "没有待你处理的。", noCardsShort: "暂无决策卡。",
      apply: "采纳", dismiss: "忽略", approve: "批准", reject: "驳回",
      fromSession: "来自会话",
      briefings: "简报", generate: "生成简报", noReports: "暂无简报。",
      history: "历史", copy: "复制", push: "推送到手机", coversSession: "覆盖会话",
      briefGenerating: "生成中...", briefFailed: "简报生成失败", briefNoLlm: "PM 大脑未配置。请检查 .env 和设置页。", copied: "已复制",
      playbook: "工作方式", kindAll: "全部", kindWorkflows: "工作流", kindSkills: "技能", kindStandards: "代码规范", kindQa: "验收标准",
      startWorkflow: "启动", workflowRun: "工作流运行", wfStep: "步骤", wfStatus: "状态", wfBegin: "执行本步", wfSubmit: "推进", wfApprove: "批准", wfReject: "拒绝", wfRefresh: "刷新", wfNeedSession: "请先在工作台选中一个会话，再启动工作流。", wfStarted: "工作流已启动",
      kindWorkflow: "工作流", kindSkill: "技能", kindStandard: "代码规范", kindQaOne: "验收标准",
      importBtn: "导入", exportBtn: "导出", newBtn: "新建",
      noDefinitions: "暂无工作方式。", on: "启用中", off: "未启用",
      edit: "编辑", del: "删除", activate: "启用",
      defnKind: "类型", defnName: "名称", defnScope: "适用范围 (JSON)", defnBody: "内容", defnActivate: "保存即启用",
      defnDescription: "描述（必填 · ≤1024 字，说明做什么 + 何时用）",
      defnDescriptionHint: "L0 选择信号：PM 据此判断这条工作方式该不该用。空描述不进自动选择。",
      workMode: "工作方式", workModePick: "手选工作方式", workModeNone: "暂无可选工作方式", workModeAuto: "自动（PM 按相关性选）",
      cancel: "取消", retry: "重试", save: "保存", saved: "已保存", saveFailed: "保存失败", failed: "失败",
      confirmDeleteTitle: "确认删除", confirmDelete: "确定删除这条工作方式？", confirmSessionDelete: "确定删除这个会话及其本地记录？",
      deleteSession: "删除会话", cancelSession: "停止", sessionCanceled: "已停止会话", notification: "通知",
      sessionBusy: "会话仍有后台任务未结束，请稍后再删除。",
      noContext: "当前会话还没有可压缩的上下文。",
      noStore: "本地数据存储不可用，请重启 Foreman 后重试。",
      sessionNotFound: "没有找到这个会话，请刷新后重试。",
      requestDeclined: "操作未被执行，请检查当前状态后重试。",
      networkError: "网络异常，请检查连接后重试。",
      fileViewer: "文件预览", fileOpened: "已打开文件", fileOpenFailed: "无法打开文件",
      fileNotFound: "文件不存在。", fileTooLarge: "文件太大，无法预览。", fileNotText: "不是文本文件，无法预览。",
      badScopeJson: "适用范围必须是 JSON 对象，例如 {\"lang\":\"py\"}。",
      missingDescription: "请填写描述（说明做什么 + 何时用），否则不会进入自动选择。",
      descriptionTooLong: "描述太长了，请控制在 1024 字以内。",
      imported: "已导入", importFailed: "导入失败", exportFailed: "导出失败",
      workspaces: "工作区", workspaceLabel: "工作区", workspaceWorktree: "worktree", workspaceNoWorktree: "worktree: 无", workspaceBranch: "branch",
      workspaceDetached: "detached", initGitRepo: "新建 git 仓库", initGitRepoBusy: "新建中…", gitInitFailed: "新建 git 仓库失败",
      branchSwitchFailed: "切换分支失败", workspaceDirty: "工作区有未提交改动，请先处理后再切换分支。", badBranch: "分支不可用",
      projectPath: "项目路径", displayName: "显示名称", pathHint: "例如 E:\\AutoWorkAgent",
      browse: "浏览", addWorkspace: "添加 / 更新工作区", remove: "移除", connected: "已连接",
      refresh: "刷新", folderPickerUnavailable: "当前浏览器不支持选择文件夹，请手动输入路径。",
      localAgents: "本地 Agent", agentEnabled: "启用", agentCommand: "启动命令", agentModel: "模型", agentEffort: "档位", agentFullAccess: "工具全开",
      copilotCliHelp: "Copilot CLI 是本地执行 agent。BYOK/provider 环境变量由 Copilot CLI 自己读取，Foreman 不保存这些 Key。更改这些环境变量后，请重启 Foreman 生效。工具全开仅映射为 --allow-all-tools / --allow-all-urls / --add-dir <workspace>，不会默认允许所有路径。",
      agentDisabled: "已禁用", agentNotFound: "未找到命令", agentsSaved: "Agent 设置已保存", noEnabledAgent: "至少要启用一个 Agent。",
      effortDefault: "默认", modelDefaultHint: "留空 = 使用配置默认模型",
      pmBrain: "PM 大脑", pmBrainSub: "给 PM 审阅 / 简报调用的模型。Key 永远留在本地。",
      pmTools: "PM 工具", pmToolsSub: "PM 运行时工具开关和浏览器来源规则。只读仓库工具默认开启。",
      fileRead: "读取文件", fileWrite: "写入文件", shellTool: "运行命令", webFetch: "抓取 URL", webSearch: "网页搜索", browserTool: "浏览器",
      allowedOrigins: "允许的浏览器来源", searxngUrl: "SearXNG 地址", browserHeadless: "无头浏览器", maxRounds: "PM 取证工具轮次",
      pmReviewDiag: "PM 复查诊断",
      pmToolsSaved: "PM 工具设置已保存",
      debug: "调试", debugSub: "排错用的高级开关。默认全关。",
      llmTrace: "LLM 对话明文落盘",
      llmTraceWarn: "开启后会把与大模型的完整对话（含源码与解密后的工作方式）明文写入本机 .foreman/debug/，仅本地保存、不上传、不进 git。改动在下次启动生效。",
      debugSaved: "调试设置已保存（重启生效）",
      provider: "服务商", model: "模型", baseUrl: "接口地址", apiKey: "API Key", transport: "传输方式",
      requestTimeout: "规划超时（秒）", requestTimeoutHelp: "控制 PM 大脑单次规划/复查的墙钟上限；范围 30–3600 秒，默认 300 秒。",
      contextWindow: "上下文上限 token", contextWindowHelp: "用于 PM 上下文预算和自动压缩；默认 272000。",
      reasoningEffort: "推理强度",
      pmKeyHint: "已配置 API Key。输入新 key 后保存可替换；留空不修改。", pmKeyMissing: "未检测到 API Key。可在这里输入并保存。",
      pmKeyPlaceholder: "留空不修改；输入新 key 后保存", clearKey: "清空 Key",
      cloudConn: "云端连接", cloudSub: "把本机接入线上总机 —— 人不在电脑前也能在手机上看进度、点审批。总机不存你的代码与 Key。",
      cloudUrl: "云端地址", accessKey: "接入密钥 Access Key", accessKeyHint: "在云端 /keys.html 生成，一机一张、可单独吊销。",
      connect: "连接", disconnect: "断开", connecting: "连接中…", notConnected: "未连接", connFailed: "连接失败",
      cloudNotConfigured: "请先填写云端地址和接入密钥。",
      cloudAuthFailed: "接入密钥无效或已吊销，请在云端 /keys.html 重新生成。",
      cloudTimeout: "连接超时，请检查网络、代理或云端地址。",
      cloudUnreachable: "无法连接云端，请检查网络、代理或云端地址。",
      cloudKeyHint: "已配置接入密钥。输入新密钥后保存可替换；留空不修改。", cloudKeyMissing: "未配置接入密钥。",
      cloudUnavailable: "当前服务不支持云端连接（仅本机 app 可用）。",
      remoteExec: "允许远端执行", remoteExecHelp: "开启后，已连接的云端可向本机派发任务 / 审批并真正执行（高风险，仅在你信任的总机上开启）。关闭时云端只能远程查看，不在本机执行任何命令。",
      machine: "机器", machineOffline: "目标机器离线，请先让本机连接云端。", relayUnavailable: "云端总机不可用。",
      remoteDisabled: "本机未开启远端执行。", remoteProcessRequired: "请选择目标机器。", remoteRateLimited: "远端请求过快，请稍后再试。",
      notificationsWaiting: "有待处理事项",
      interface: "界面与自动化", autoExec: "自动执行权限", autoExecHelp: "决定 Foreman 在没有你确认时能自动执行多少动作。",
      auto0: "0 只报告", auto1: "1 凡事都问", auto2: "2 自动可逆", auto3: "3 只拦不可逆",
      theme: "主题", light: "浅色", dark: "深色", language: "语言",
      pushNotif: "手机通知", pushNotifSub: "决策与审批推到手机", enable: "开启",
      pushEnabled: "通知已开启", pushUnsupported: "此浏览器不支持通知", pushNotConfigured: "服务器未配置推送", pushDenied: "通知权限被拒绝", pushFailed: "开启通知失败",
      stepDetail: "步骤详情", rawReturn: "原始返回", codeDiff: "代码改动", back: "返回", viewDetail: "查看详情",
      dispatchFailed: "下发失败", emptyGoal: "任务不能为空。",
      dispatchNoWorkspace: "未配置工作区：请到设置页添加项目路径。", workspaceEmpty: "没有配置工作区。",
      noDispatcher: "当前服务不是本地 PC 工作台，不能下发任务。", workspaceMissing: "没有可用工作区。",
      ev_stop: "完成", ev_error: "错误", ev_checkpoint: "检查点", ev_gate: "闸门",
      ev_action_executed: "已执行", ev_action_undone: "已回退", ev_context_compact: "上下文压缩",
      ev_review: "复查", ev_audit: "审查", ev_undo: "回退", ev_recover: "恢复", ev_stall: "卡住",
      noActiveSession: "暂无活动会话。", noAgent: "无 agent",
      readOnlyLog: "只读日志", workspaceRisk: "当前工作区范围很大；工具全开时请确认这是你想授权的路径。",
      versionCurrent: "当前运行版本", versionUnavailable: "等待 /health 返回版本",
      versionSource: "版本来源", versionSourceText: "Foreman 的包版本只从 src/foreman/__init__.py 的 __version__ 读取；exe、/health、PWA 与 README 的版本说明都必须跟随这个版本更新。",
      versionCheckUpdate: "检查更新", versionCheckingUpdate: "检查中...", versionNoUpdate: "没有可安装更新", versionCheckFailed: "检查更新失败",
      versionCurrentTag: "当前",
      versionHistory: "历史更新说明",
      versionMaint: "维护要求", versionMaintText: "每次改 __version__ 时，同步更新 README.md 的 Version Information / 版本信息、docs/VERSION_HISTORY.md，以及 exe 控制台的版本页文案；当前版本说明也必须进入同一个历史更新说明列表。",
    },
    en: {
      productSubtitle: "Local workbench",
      newVersionReady: "A new version is available", refreshNow: "Refresh", later: "Later",
      appUpdateReady: "Update available", updateNow: "Update now", updating: "Downloading update",
      updateFailed: "Update failed — try again later or download manually", updateDownloadProgress: "Download progress",
      updateStarting: "Preparing download...", updateDownloading: "Downloading...", updateSwapping: "Download complete, restarting...",
      updateCancel: "Cancel download", updateCancelling: "Canceling...", updateSizeUnknown: "Size unknown",
      navWorkspace: "Workspace", navDecisions: "Decisions", navBriefings: "Briefings", navRules: "Playbook", navSettings: "Settings", navVersion: "Version",
      workspaceSubtitle: "Pick a workspace and dispatch work to the local agent.",
      decisionsSubtitle: "Handle the cards and approvals that need you.",
      briefingsSubtitle: "Turn current progress into readable status.",
      rulesSubtitle: "Maintain workflows, skills, code standards & QA rubrics — selected by relevance and pulled in on demand.",
      settingsSubtitle: "Configure workspaces, the PM brain, and UI preferences.",
      versionSubtitle: "Review the current version, update history, and check for available updates.",
      sessions: "Sessions", newSession: "New session",
      editSessionTitle: "Edit session title", sessionTitle: "Session title", sessionTitleHint: "Enter a new session title",
      sessionTitleEmpty: "Session title cannot be empty.", sessionTitleTooLong: "Session title is too long; keep it under 300 characters.", sessionTitleUpdated: "Session title updated",
      launchTag: "Waking your engineering foreman — hand work to local agents, the PM brain watches over it.",
      launchEngine: "Engine ready · PM Core",
      launchAgents: "Local agents linked",
      launchLoad: "Loading workspaces & playbook…",
      launchBrain: "Waking PM brain",
      personalMode: "Team workbench · local sync",
      selectSessionHint: "Pick a session on the left, or dispatch a new task below.",
      running: "RUNNING", live: "LIVE", done: "done", queued: "queued", cancelled: "cancelled",
      stalled: "stalled",
      reasonWallClock: "planning timed out (wall clock)", reasonNoProgress: "no progress for too long",
      reasonRepetition: "model repeated its output — auto-aborted", reasonStalled: "planning aborted by watchdog",
      autonomy: "Autonomy", briefing: "Briefing", pmThinking: "PM is thinking...",
      plan: "Plan", approved: "approved", active: "active",
      reply: "Reply", commandsRun: "Commands run", fileChanges: "File changes",
      processLabel: "Process", finalReply: "Final reply", waitingResult: "Waiting for result…", noChanges: "No file changes", executing: "Executing",
      kCmd: "cmd", kEdit: "edit", kRead: "read", kFind: "find", kWeb: "web", kTool: "tool", kPlan: "plan", kThink: "think",
      fkAdd: "add", fkUpdate: "edit", fkDelete: "del", noSteps: "No steps yet", stepsWord: "steps", changeDetail: "Change detail",
      open: "Open", hide: "Hide",
      decisionNeeded: "Decision needed", suggestion: "Suggestion", showDiff: "Show diff",
      riskHigh: "HIGH RISK", riskMedium: "MEDIUM RISK", riskLow: "LOW RISK",
      context: "Context", compact: "Compact", compacting: "Compacting...", compactDone: "Context compacted", compactFailed: "Compact failed",
      attach: "Attach", modelPlaceholder: "model · default", thinkingLevel: "thinking level", thinkingTrace: "thinking",
      fast: "Fast", std: "Std", deep: "Deep", send: "Send", sendHint: "send",
      guiding: "Sending…", queueing: "Sending…", guide: "Send", queueSend: "Send",
      guideHelp: "Stop the current session before sending a new instruction.", queueHelp: "Send: continue after the current reply finishes, without waiting for the full loop.",
      composerPlaceholder: "Continue with the PM… add attachments, pick a level, or just give an order",
      mComposerPlaceholder: "Continue…",
      tabTodos: "To-dos", tabSubagents: "Subagents", tabTerminal: "Raw output",
      addStep: "Add a step… ⏎", todoHint: "Auto-drafted by the PM. Adding a step sends it as a follow-up to this session.",
      subSpawned: "spawned", subAgentsWord: "subagents",
      mTabChat: "Chat", mTabTodo: "To-dos", mTabSub: "Agents", mTabTerm: "Output",
      decisions: "Decisions", decisionCards: "Decision cards", approvals: "Approvals",
      noDecisions: "No decisions waiting.", noApprovals: "Nothing waiting on you.", noCardsShort: "No decision cards.",
      apply: "Apply", dismiss: "Dismiss", approve: "Approve", reject: "Reject",
      fromSession: "from session",
      briefings: "Briefings", generate: "Generate", noReports: "No briefings yet.",
      history: "History", copy: "Copy", push: "Push", coversSession: "covers session",
      briefGenerating: "Generating...", briefFailed: "Briefing failed", briefNoLlm: "PM brain is not configured. Check .env and Settings.", copied: "Copied",
      playbook: "Playbook", kindAll: "All", kindWorkflows: "Workflows", kindSkills: "Skills", kindStandards: "Standards", kindQa: "QA",
      startWorkflow: "Start", workflowRun: "Workflow run", wfStep: "Step", wfStatus: "Status", wfBegin: "Run step", wfSubmit: "Advance", wfApprove: "Approve", wfReject: "Reject", wfRefresh: "Refresh", wfNeedSession: "Pick a session in the workbench first, then start the workflow.", wfStarted: "Workflow started",
      kindWorkflow: "Workflow", kindSkill: "Skill", kindStandard: "Standard", kindQaOne: "QA rubric",
      importBtn: "Import", exportBtn: "Export", newBtn: "New",
      noDefinitions: "No playbook items yet.", on: "active", off: "off",
      edit: "Edit", del: "Delete", activate: "Activate",
      defnKind: "Kind", defnName: "Name", defnScope: "Scope (JSON)", defnBody: "Body", defnActivate: "Activate on save",
      defnDescription: "Description (required · ≤1024 chars: what it does + when to use)",
      defnDescriptionHint: "L0 selection signal: the PM decides relevance from this. Blank → excluded from auto-select.",
      workMode: "Work modes", workModePick: "Pick work modes", workModeNone: "No work modes available", workModeAuto: "Auto (PM picks by relevance)",
      cancel: "Cancel", retry: "Retry", save: "Save", saved: "Saved", saveFailed: "Save failed", failed: "failed",
      confirmDeleteTitle: "Confirm delete", confirmDelete: "Delete this playbook item?", confirmSessionDelete: "Delete this session and its local records?",
      deleteSession: "Delete session", cancelSession: "Stop", sessionCanceled: "Session stopped", notification: "Notification",
      sessionBusy: "A background task is still active; delete it after the task finishes.",
      noContext: "This session has no context to compact yet.",
      noStore: "Local storage is unavailable. Restart Foreman and try again.",
      sessionNotFound: "This session was not found. Refresh and try again.",
      requestDeclined: "The operation was not completed. Check the current state and try again.",
      networkError: "Network error. Check the connection and try again.",
      fileViewer: "File preview", fileOpened: "File opened", fileOpenFailed: "Could not open file",
      fileNotFound: "File not found.", fileTooLarge: "File is too large to preview.", fileNotText: "This is not a text file.",
      badScopeJson: "Scope must be a JSON object, for example {\"lang\":\"py\"}.",
      missingDescription: "Please add a description (what it does + when to use), or it won't be auto-selected.",
      descriptionTooLong: "Description is too long — keep it under 1024 characters.",
      imported: "Imported", importFailed: "Import failed", exportFailed: "Export failed",
      workspaces: "Workspaces", workspaceLabel: "Workspace", workspaceWorktree: "worktree", workspaceNoWorktree: "worktree: none", workspaceBranch: "branch",
      workspaceDetached: "detached", initGitRepo: "Initialize git repo", initGitRepoBusy: "Initializing…", gitInitFailed: "Could not initialize git repo",
      branchSwitchFailed: "Could not switch branch", workspaceDirty: "This workspace has uncommitted changes. Resolve them before switching branches.", badBranch: "Branch is not available",
      projectPath: "Project path", displayName: "Name", pathHint: "e.g. E:\\AutoWorkAgent",
      browse: "Browse", addWorkspace: "Add / update", remove: "Remove", connected: "connected",
      refresh: "Refresh", folderPickerUnavailable: "This browser cannot open a folder picker. Enter the path manually.",
      localAgents: "Local agents", agentEnabled: "Enabled", agentCommand: "Command", agentModel: "Model", agentEffort: "Level", agentFullAccess: "Full access",
      copilotCliHelp: "Copilot CLI is a local execution agent. BYOK/provider environment variables are read by Copilot CLI itself. Foreman does not store those keys. Restart Foreman after changing those environment variables. Full access maps only to --allow-all-tools / --allow-all-urls / --add-dir <workspace>, not all paths.",
      agentDisabled: "Disabled", agentNotFound: "Command not found", agentsSaved: "Agent settings saved", noEnabledAgent: "Enable at least one agent.",
      effortDefault: "Default", modelDefaultHint: "blank = configured default model",
      pmBrain: "PM brain", pmBrainSub: "The model the PM uses to review & brief. Your key never leaves this machine.",
      pmTools: "PM tools", pmToolsSub: "PM runtime tool switches and browser origin rules. Read-only repo tools are on by default.",
      fileRead: "Read files", fileWrite: "Write files", shellTool: "Run commands", webFetch: "Fetch URL", webSearch: "Web search", browserTool: "Browser",
      allowedOrigins: "Allowed browser origins", searxngUrl: "SearXNG URL", browserHeadless: "Headless browser", maxRounds: "PM evidence rounds",
      pmReviewDiag: "PM review diagnostics",
      pmToolsSaved: "PM tool settings saved",
      debug: "Debug", debugSub: "Advanced switches for troubleshooting. All off by default.",
      llmTrace: "Trace LLM conversations to disk",
      llmTraceWarn: "Writes the FULL model conversation (incl. your source + decrypted work modes) in plaintext to .foreman/debug/ on this machine — local only, never uploaded, not committed. Takes effect on next launch.",
      debugSaved: "Debug settings saved (restart to apply)",
      provider: "Provider", model: "Model", baseUrl: "Base URL", apiKey: "API Key", transport: "Transport",
      requestTimeout: "Planning timeout (s)", requestTimeoutHelp: "Wall-clock limit for one PM planning/review call; range 30–3600 seconds, default 300 seconds.",
      contextWindow: "Context limit tokens", contextWindowHelp: "Used for PM context budgeting and auto-compaction; default 272000.",
      reasoningEffort: "Reasoning effort",
      pmKeyHint: "API key is set. Enter a new key and save to replace it; blank keeps it.", pmKeyMissing: "No API key detected. You can enter and save one here.",
      pmKeyPlaceholder: "blank = unchanged; enter a new key to save", clearKey: "Clear key",
      cloudConn: "Cloud connection", cloudSub: "Link this machine to the online relay — watch progress and approve from your phone. The relay never stores your code or keys.",
      cloudUrl: "Cloud URL", accessKey: "Access key", accessKeyHint: "Mint one at /keys.html on the relay — one per machine, individually revocable.",
      connect: "Connect", disconnect: "Disconnect", connecting: "Connecting…", notConnected: "Not connected", connFailed: "Connection failed",
      cloudNotConfigured: "Enter the cloud URL and access key first.",
      cloudAuthFailed: "The access key is invalid or revoked. Generate a new key at /keys.html.",
      cloudTimeout: "Connection timed out. Check the network, proxy, or cloud URL.",
      cloudUnreachable: "Could not reach the cloud relay. Check the network, proxy, or cloud URL.",
      cloudKeyHint: "Access key set. Enter a new key and save to replace it; blank keeps it.", cloudKeyMissing: "No access key configured.",
      cloudUnavailable: "This service does not support cloud connection (local app only).",
      remoteExec: "Allow remote execution", remoteExecHelp: "When on, the connected cloud can dispatch tasks / approvals to this machine and actually run them (high-risk; enable only on a relay you trust). When off, the cloud can only view remotely — no commands run on this machine.",
      machine: "Machine", machineOffline: "The target machine is offline. Connect the PC to the cloud relay first.", relayUnavailable: "The relay is unavailable.",
      remoteDisabled: "Remote execution is disabled on the PC.", remoteProcessRequired: "Choose a target machine.", remoteRateLimited: "Remote requests are rate limited. Try again shortly.",
      notificationsWaiting: "Pending items",
      interface: "Interface & automation", autoExec: "Auto-execution", autoExecHelp: "How much Foreman may do without your confirmation.",
      auto0: "0 report", auto1: "1 ask first", auto2: "2 auto safe", auto3: "3 auto reversible",
      theme: "Theme", light: "Light", dark: "Dark", language: "Language",
      pushNotif: "Push notifications", pushNotifSub: "decisions & approvals to your phone", enable: "Enable",
      pushEnabled: "Notifications enabled", pushUnsupported: "Notifications are not supported in this browser", pushNotConfigured: "Push is not configured on the server", pushDenied: "Notification permission was denied", pushFailed: "Could not enable notifications",
      stepDetail: "Step detail", rawReturn: "Raw return", codeDiff: "Code diff", back: "Back", viewDetail: "View detail",
      dispatchFailed: "Dispatch failed", emptyGoal: "Task cannot be empty.",
      dispatchNoWorkspace: "No workspace configured. Add a project path in Settings.", workspaceEmpty: "No workspaces configured.",
      noDispatcher: "This service is not the local PC workspace.", workspaceMissing: "No workspace available.",
      ev_stop: "Done", ev_error: "Error", ev_checkpoint: "Checkpoint", ev_gate: "Gate",
      ev_action_executed: "Executed", ev_action_undone: "Undone", ev_context_compact: "Context compacted",
      ev_review: "Review", ev_audit: "Audit", ev_undo: "Undo", ev_recover: "Recover", ev_stall: "Stall",
      noActiveSession: "No active sessions yet.", noAgent: "no agent",
      readOnlyLog: "Read-only log", workspaceRisk: "This workspace is very broad; confirm that full tool access is intentional.",
      versionCurrent: "Current runtime version", versionUnavailable: "Waiting for /health version",
      versionSource: "Version source", versionSourceText: "Foreman's package version is read only from __version__ in src/foreman/__init__.py; the exe, /health, PWA, and README version notes must follow that release.",
      versionCheckUpdate: "Check for updates", versionCheckingUpdate: "Checking...", versionNoUpdate: "No installable update found", versionCheckFailed: "Update check failed",
      versionCurrentTag: "Current",
      versionHistory: "Historical update notes",
      versionMaint: "Maintenance rule", versionMaintText: "Whenever __version__ changes, update README.md's Version Information / 版本信息 section, docs/VERSION_HISTORY.md, and the exe console's Version page copy. The current release notes must live in the same historical update list.",
    },
  };
  function normalizeUiLang(value) {
    return String(value || "").trim().toLowerCase().startsWith("zh") ? "zh" : "en";
  }
  function detectedUiLang() {
    const stored = localStorage.getItem(LANG_KEY);
    if (stored) return normalizeUiLang(stored);
    const langs = (navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || ""]);
    return normalizeUiLang(langs[0]);
  }

  const NAV = [
    { key: "workspace", ico: "◳", label: "navWorkspace" },
    { key: "decisions", ico: "◉", label: "navDecisions" },
    { key: "briefings", ico: "▤", label: "navBriefings" },
    { key: "rules", ico: "▦", label: "navRules" },
    { key: "settings", ico: "⚙", label: "navSettings" },
    { key: "version", ico: "v", label: "navVersion" },
  ];
  const KIND_LABEL = { workflow: "kindWorkflow", skill: "kindSkill", code_standard: "kindStandard", qa_rubric: "kindQaOne" };
  const KIND_TAGCOLOR = { workflow: "accent", skill: "violet", code_standard: "amber", qa_rubric: "green" };
  const STREAM_TYPES = new Set(["pm_output", "pm_reasoning", "agent_output", "agent_reasoning"]);
  const VERSION_HISTORY = [
    {
      version: "v1.4.4",
      en: "Context v2 planning now has Codex-style design and implementation task docs covering recoverable checkpoints, PM active context envelopes, remote /responses/compact reuse, and review-ready branch handoff requirements; session views also keep new messages scrolled to the bottom.",
      zh: "Context v2 规划新增 Codex 风格设计书和实施任务书，覆盖可恢复 checkpoint、PM active context envelope、复用远端 /responses/compact、以及便于 GPT-5.5 pro review 的分支交付要求；会话视图发送新消息后也会保持滚动到底部。",
    },
    {
      version: "v1.4.3",
      en: "Pytest maintenance now folds repetitive table-style checks into compact table-driven tests, removes a redundant client import smoke test, and keeps the same behavior coverage with fewer collected pytest nodes.",
      zh: "Pytest 维护整理将重复的表格型断言合并为更紧凑的表驱动测试，移除冗余的 client import smoke test，并在保留相同行为覆盖的同时减少 pytest 收集节点。",
    },
    {
      version: "v1.4.2",
      en: "PM plans can now carry a verified target workspace/worktree; Foreman updates the session workspace and launches the coding agent from that worktree instead of only mentioning Set-Location in the prompt.",
      zh: "PM 计划现在可以携带已验证的目标 workspace/worktree；Foreman 会同步更新会话 workspace，并从该 worktree 启动编码代理，不再只把 Set-Location 写进提示词。",
    },
    {
      version: "v1.4.1",
      en: "Context compression now renders as a collapsed transparent ContextPack row with parsed pretty JSON on expand, and the composer context meter follows the next provider-bound context before and after compaction.",
      zh: "上下文压缩现在显示为透明折叠 ContextPack 行，展开后展示解析并格式化后的 JSON；输入区上下文仪表盘会按压缩前后下一次真正发给 provider 的上下文计数。",
    },
    {
      version: "v1.4.0",
      en: "Subagent cards now show replies, commands, reasoning, and explicit final results in one chronological timeline, keep agent identity separate from model output, preserve Windows CJK CLI text, and make workspace file references clickable.",
      zh: "子代理卡片现在按同一条时间线展示回复、命令、思考和明确的最终结果，把 agent 身份与模型输出分开，保留 Windows 中文 CLI 输出，并让工作区文件引用可点击。",
    },
    {
      version: "v1.3.9",
      en: "Session workspace status now records the original main workspace, falls back when PM worktrees disappear, shows no worktree for new chats, and offers guarded local branch switching.",
      zh: "会话工作区状态现在记录原始 main workspace，PM worktree 消失时回退 main，新对话显示无 worktree，并提供受保护的本地分支切换。",
    },
    {
      version: "v1.3.8",
      en: "PM tool activity now appears as a public timeline with tool-start labels, result summaries, collapsible details, optional public notes, and polished PM thinking expansion.",
      zh: "PM 工具活动现在进入公开时间线，显示工具开始、结果摘要、可折叠详情和可选公开说明；PM 思考展开时也不再重复标题。",
    },
    {
      version: "v1.3.7",
      en: "Codex stdout is now read in chunks and reassembled as JSONL, avoiding asyncio's per-line reader limit for large command-output events and cleaning up stream failures.",
      zh: "Codex stdout 改为分块读取并重组 JSONL，移除大段命令输出触发的 asyncio 单行读取上限，并在读取失败时清理子进程。",
    },
    {
      version: "v1.3.6",
      en: "PM thinking summaries now start collapsed as a transparent generated reasoning-title row, with hover and expanded icon states before revealing the full text.",
      zh: "PM 思考摘要现在默认折叠为透明的 reasoning 生成标题行，悬浮和展开时都有图标状态变化，点击后再显示完整内容。",
    },
    {
      version: "v1.3.5",
      en: "Update dialogs now show the human release notes for every version between the installed exe and the latest available release, falling back to GitHub Release text only if history cannot be loaded.",
      zh: "更新弹窗现在显示已安装 exe 到最新可用版本之间每个版本的人工更新说明；只有版本历史加载失败时才回退到 GitHub Release 文本。",
    },
    {
      version: "v1.3.4",
      en: "User and PM conversation bubbles now include compact copy icons for quickly copying message text in desktop and mobile session views.",
      zh: "用户与 PM 会话泡泡底部增加小复制图标，桌面和移动会话视图都能快速复制消息文本。",
    },
    {
      version: "v1.3.3",
      en: "Packaged Windows exe now hides server-side git and diagnostic subprocess windows, preventing transient cmd flashes when switching sessions.",
      zh: "打包 Windows exe 会隐藏服务端 git 与诊断子进程窗口，避免切换会话时闪出临时 cmd 窗口。",
    },
    {
      version: "v1.3.2",
      en: "Composer status now shows workspace git worktree/branch, offers explicit git initialization, and restores the selected session's saved workspace after reopening.",
      zh: "会话输入区显示工作区 git worktree/branch，提供显式新建 git 仓库入口，并在重开后恢复所选会话保存的工作区。",
    },
    {
      version: "v1.3.1",
      en: "Preserved leading spaces in PM reasoning stream deltas so English thought summaries render with normal word spacing.",
      zh: "保留 PM 思考流 delta 片段的前导空格，修复英文思考摘要单词粘连显示。",
    },
    {
      version: "v1.3.0",
      en: "Changed packaged exe self-update into a dialog with live download progress and a cancel button before restart.",
      zh: "将打包 exe 自更新改为弹窗模式，增加实时下载进度，并在重启前提供取消下载按钮。",
    },
    {
      version: "v1.2.9",
      en: "Added a Version-page update check button and reworked release notes into one historical list that includes the current release.",
      zh: "版本页增加检查更新按钮，并把当前版本说明与历史版本说明合并为同一个历史更新列表。",
    },
    {
      version: "v1.2.8",
      en: "Opened PM shell runtime controls with live command output, durable tool logs, approval-governed execution, process-tree cancellation, and admin elevation for packaged exe builds.",
      zh: "开放 PM shell 运行控制：实时命令输出、工具日志落盘、审批约束执行、进程树取消，以及打包 exe 管理员权限启动。",
    },
    {
      version: "v1.2.7",
      en: "Rendered PM reasoning summaries through Markdown, improved paragraph spacing, and localized the Chinese reasoning label.",
      zh: "用 Markdown 渲染 PM 思考摘要，改善段落间距，并将中文标签本地化为思考摘要。",
    },
    {
      version: "v1.2.6",
      en: "Session stop control, single follow-up send button, dropdown model/thinking controls, image paste chips, and visible PM reasoning stream.",
      zh: "增加会话停止入口、合并继续发送按钮、模型与 thinking level 下拉、图片粘贴附件，以及可见 PM reasoning 流。",
    },
    {
      version: "v1.2.5",
      en: "Let the PM recover from fatal local agent failures by excluding failed agents and relaunching a selected replacement.",
      zh: "PM 可在本地 agent 致命失败后排除失败 agent，并选择替代 agent 重新启动执行。",
    },
    {
      version: "v1.2.4",
      en: "Set Copilot BYOK GPT-5 launches to the Responses wire API while keeping non-GPT-5 launches unchanged.",
      zh: "Copilot BYOK 使用 GPT-5 系列模型时切换到 Responses wire API，非 GPT-5 启动行为保持不变。",
    },
    {
      version: "v1.2.3",
      en: "Removed redundant auto-agent explanatory copy while keeping PM-driven agent selection unchanged.",
      zh: "移除冗余的自动执行 agent 说明文案，PM 自动选择行为不变。",
    },
    {
      version: "v1.2.2",
      en: "Removed the PM provider max output token setting and stopped sending OpenAI-compatible output caps.",
      zh: "移除 PM Provider 最大输出 token 设置，并停止发送 OpenAI 兼容输出上限。",
    },
    {
      version: "v1.2.1",
      en: "Bilingual README and exe version pages, visible version history, and stricter version-note rules.",
      zh: "中英文 README 与 exe 版本页、可见版本历史，以及更严格的版本说明规则。",
    },
    {
      version: "v1.2.0",
      en: "PM context token limits exposed in the product configuration flow.",
      zh: "在产品配置流程中暴露 PM 上下文 token 上限设置。",
    },
    {
      version: "v1.1.9",
      en: "PM askQuestion decision tool.",
      zh: "PM askQuestion 决策工具。",
    },
    {
      version: "v1.1.8",
      en: "Packaged-exe cloud relay offline flap handling.",
      zh: "打包 exe 的云端 relay 离线反复跳变处理。",
    },
    {
      version: "v1.1.7",
      en: "Automatic UI language detection.",
      zh: "UI 语言自动检测。",
    },
    {
      version: "v1.1.6",
      en: "PM tool evidence rounds raised and clamped for larger investigations.",
      zh: "提高并限制 PM 工具取证轮次，支持更长的取证运行。",
    },
  ];

  // ---------------------------------------------------------------------------
  // helpers
  // ---------------------------------------------------------------------------
  function formatDateTime(value, lang) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }
  function isWideWorkspace(p) {
    const v = String(p || "").trim().replace(/\//g, "\\");
    return /^[A-Za-z]:\\?$/.test(v) || /^\\\\[^\\]+\\[^\\]+\\?$/.test(v);
  }
  function effectiveSessionWorkspace(row, fallback) {
    if (!row) return fallback || "";
    const main = row.main_workspace || fallback || "";
    const current = row.workspace || "";
    if (current && row.workspace_exists !== false) return current;
    return main || current || fallback || "";
  }
  function jsonObjectError(text) {
    try {
      const obj = JSON.parse(text || "{}");
      return obj && typeof obj === "object" && !Array.isArray(obj) ? "" : "bad_scope_json";
    } catch (e) {
      return "bad_scope_json";
    }
  }
  function clientSource() {
    const ua = navigator.userAgent || "";
    return /Android|iPhone|iPad|iPod|Mobile|Windows Phone/i.test(ua) ? "phone" : "desktop";
  }
  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
    return out;
  }
  function estTokens(events) {
    let chars = 0;
    for (const e of events) chars += eventContextChars(e);
    return approxTokensFromChars(chars);
  }
  function eventContextChars(e) {
    if (e && (e.type === "pm_output" || e.type === "pm_reasoning")) return 0;
    const p = (e && e.payload) || {};
    const primary = p.text || p.delta || p.summary || p.raw_text || p.goal || "";
    if (primary) return String(primary).length;
    try { return JSON.stringify(p).length; } catch (err) { return 0; }
  }
  function approxTokensFromChars(chars) {
    return Math.round(Math.max(0, Number(chars) || 0) / 4);
  }
  function contextLimitFor(options, selectedModel, fallbackModel) {
    const models = options || [];
    const wanted = String(selectedModel || fallbackModel || "").trim();
    const found = models.find((m) => m.value === wanted || m.id === wanted) || models.find((m) => m.context_length);
    const contextLength = Number(found && found.context_length);
    if (Number.isFinite(contextLength) && contextLength > 0) {
      return contextLength;
    }
    return DEFAULT_CONTEXT_TOKENS;
  }
  function parseObject(text) {
    try {
      const obj = JSON.parse(text || "{}");
      return obj && typeof obj === "object" && !Array.isArray(obj) ? obj : null;
    } catch (e) {
      return null;
    }
  }
  function contextPackView(payload) {
    const p = payload || {};
    const summary = String(p.summary || "");
    const pack = parseObject(summary);
    const state = (pack && pack.session_state && typeof pack.session_state === "object") ? pack.session_state : {};
    const preview = String(state.summary || state.current_step || "").trim();
    const beforeTokens = Math.max(0, Number(p.before_tokens) || 0);
    const afterTokens = Math.max(0, Number(p.after_tokens) || approxTokensFromChars(summary.length));
    const displayPack = pack || {
      parse_error: "ContextPack JSON could not be parsed",
      format: String(p.format || ""),
      summary_chars: Math.max(0, Number(p.summary_chars) || summary.length),
      before_tokens: beforeTokens,
      after_tokens: afterTokens,
    };
    return {
      json: JSON.stringify(displayPack, null, 2),
      preview,
      beforeTokens,
      afterTokens,
      summaryChars: Math.max(0, Number(p.summary_chars) || summary.length),
      format: String(p.format || ""),
    };
  }
  function lastContextPackEvent(events) {
    const idx = lastContextPackIndex(events);
    return idx >= 0 ? events[idx] : null;
  }
  function lastContextPackIndex(events) {
    for (let i = (events || []).length - 1; i >= 0; i -= 1) {
      const e = events[i];
      if (e && e.type === "context_compact") return i;
    }
    return -1;
  }
  function nextContextStats(sessionRow, events) {
    const compactIdx = lastContextPackIndex(events);
    const compact = compactIdx >= 0 ? events[compactIdx] : null;
    const tailTokens = compactIdx >= 0 ? estTokens((events || []).slice(compactIdx + 1)) : 0;
    if (sessionRow && sessionRow.context_compacted && Object.prototype.hasOwnProperty.call(sessionRow, "context_tokens")) {
      return {
        tokens: Math.max(0, Number(sessionRow.context_tokens) || 0) + tailTokens,
        compacted: true,
      };
    }
    if (compact) {
      const view = contextPackView(compact.payload || {});
      return { tokens: view.afterTokens + tailTokens, compacted: true };
    }
    return { tokens: estTokens(events || []), compacted: false };
  }
  function clipboardImageFiles(ev) {
    const items = ev && ev.clipboardData && ev.clipboardData.items;
    if (!items) return [];
    const files = [];
    for (const item of Array.from(items)) {
      if (!item || item.kind !== "file") continue;
      const file = item.getAsFile && item.getAsFile();
      if (file && String(file.type || "").toLowerCase().startsWith("image/")) files.push(file);
    }
    return files;
  }
  function displayAgent(agentType, d) {
    if (!agentType || agentType === "pm-agent") return "PM";
    return agentType;
  }

  // ---- text extraction (ported) ----
  function extractTextParts(value) {
    if (!value || typeof value !== "object") return [];
    const parts = [];
    for (const key of ["text", "result", "thinking", "reasoning", "summary"]) {
      if (typeof value[key] === "string" && value[key].trim()) parts.push(value[key]);
    }
    if (!parts.length && typeof value.delta === "string" && value.delta.trim()) parts.push(value.delta);
    for (const key of ["message", "item"]) {
      if (value[key] && typeof value[key] === "object") parts.push(...extractTextParts(value[key]));
    }
    const content = value.content;
    if (typeof content === "string" && content.trim()) return [...parts, content];
    if (!Array.isArray(content)) return parts;
    const contentParts = [];
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (typeof block.text === "string") contentParts.push(block.text);
      else if (typeof block.delta === "string") contentParts.push(block.delta);
      else if (typeof block.thinking === "string") contentParts.push(block.thinking);
      else if (typeof block.reasoning === "string") contentParts.push(block.reasoning);
      else if (typeof block.summary === "string") contentParts.push(block.summary);
      else if (block.type === "tool_use") contentParts.push(`[tool] ${block.name || "tool"}`);
      else if (block.type === "tool_result") contentParts.push(String(block.content || ""));
      else contentParts.push(...extractTextParts(block));
    }
    return [...parts, ...contentParts].filter(Boolean);
  }
  function extractAgentText(payload) { return extractTextParts(payload).join("\n").trim(); }
  function extractStreamText(payload) {
    if (payload && typeof payload === "object" && typeof payload.delta === "string") {
      return payload.delta;
    }
    return extractAgentText(payload);
  }
  function shellQuote(value) {
    const text = String(value || "");
    return /\s|["']/.test(text) ? `"${text.replace(/"/g, '\\"')}"` : text;
  }
  function commandLine(value) {
    if (Array.isArray(value)) return value.map(shellQuote).join(" ");
    return String(value || "").trim();
  }

  // ---- process-step extraction (codex / claude CLI streams) ----
  // The coding CLIs report what they DO as structured stream events, not just prose: codex
  // `exec --json` emits item.* lines (command_execution / file_change / reasoning / web_search /
  // mcp_tool_call / todo_list / agent_message); Claude `stream-json` emits assistant/user messages
  // whose content blocks are text / thinking / tool_use / tool_result. Both get a dedicated parser
  // below. Copilot CLI runs `--output-format json --stream off`, so it yields a final answer rather
  // than a step stream — it has no dedicated parser and falls through to reply text; if it ever maps
  // onto the assistant/content shape it picks up the Claude path for free. Schemas drift, so every
  // field access is guarded (DESIGN §13.1).
  function clip(value, max = 600) {
    const s = typeof value === "string" ? value : value == null ? "" : String(value);
    return s.length > max ? `${s.slice(0, max)}…` : s;
  }
  function blockText(content) {
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content.map((x) => (x && typeof x === "object" ? x.text || "" : typeof x === "string" ? x : "")).join("\n").trim();
    }
    return "";
  }
  // Claude tool_use block → a typed step. Tool names map to the kind a human reads at a glance.
  function claudeToolStep(b) {
    const name = String(b.name || "tool");
    const inp = b.input && typeof b.input === "object" ? b.input : {};
    const key = b.id ? `cc-${b.id}` : "";
    const n = name.toLowerCase();
    if (n === "bash" || n === "shell" || n === "powershell" || n === "pwsh") return { key, kind: "cmd", title: commandLine(inp.command), detail: inp.description || "", status: "active" };
    if (n === "write") return { key, kind: "edit", title: inp.file_path || "", fileKind: "add", status: "active" };
    if (n === "edit" || n === "multiedit" || n === "notebookedit") return { key, kind: "edit", title: inp.file_path || inp.notebook_path || "", fileKind: "update", status: "active" };
    if (n === "read") return { key, kind: "read", title: inp.file_path || inp.notebook_path || "", status: "active" };
    if (n === "grep" || n === "glob") return { key, kind: "find", title: inp.pattern || inp.path || "", status: "active" };
    if (n === "websearch") return { key, kind: "web", title: inp.query || "", status: "active" };
    if (n === "webfetch") return { key, kind: "web", title: inp.url || "", status: "active" };
    if (n === "todowrite") return { key, kind: "plan", todos: (Array.isArray(inp.todos) ? inp.todos : []).map((t) => ({ text: t.content || t.text || "", done: t.status === "completed" })), status: "active" };
    if (n === "task") return { key, kind: "tool", title: inp.description || inp.subagent_type || "subagent", status: "active" };
    return { key, kind: "tool", title: name, status: "active" };
  }
  function stepsFromAgentPayload(p) {
    if (!p || typeof p !== "object") return [];
    const out = [];
    // Codex exec --json: { type: "item.(started|updated|completed)", item: {...} }
    if (typeof p.type === "string" && p.type.indexOf("item.") === 0 && p.item && typeof p.item === "object") {
      const it = p.item;
      const key = it.id ? `cx-${it.id}` : "";
      const settled = p.type === "item.completed";
      const raw = String(it.status || (settled ? "completed" : "in_progress")).toLowerCase();
      // Non-success terminal states (a policy-declined or cancelled command reports
      // status:"declined"/exit_code:-1) must read as failed, not done.
      const failed = it.error || ["failed", "declined", "denied", "cancelled", "canceled", "error", "rejected", "timeout"].includes(raw);
      const status = failed ? "failed" : raw === "completed" || (settled && raw !== "in_progress") ? "done" : "active";
      if (it.type === "command_execution") out.push({ key, kind: "cmd", title: commandLine(it.command), detail: clip(it.aggregated_output), exit: typeof it.exit_code === "number" ? it.exit_code : null, status });
      else if (it.type === "file_change") for (const ch of Array.isArray(it.changes) ? it.changes : []) { if (ch && ch.path) out.push({ key: it.id ? `cx-${it.id}-${ch.path}` : "", kind: "edit", title: String(ch.path), fileKind: ch.kind || "update", status }); }
      else if (it.type === "web_search") out.push({ key, kind: "web", title: String(it.query || ""), status });
      else if (it.type === "mcp_tool_call") out.push({ key, kind: "tool", title: String(it.tool || "tool"), detail: it.server ? `@${it.server}` : "", status });
      else if (it.type === "todo_list") out.push({ key, kind: "plan", todos: (Array.isArray(it.items) ? it.items : []).map((x) => ({ text: x.text || "", done: !!x.completed })), status });
      else if (it.type === "reasoning" && it.text) out.push({ key, kind: "think", title: clip(it.text, 280), status });
      else if (it.type === "error" && it.message) out.push({ key, kind: "tool", title: clip(it.message, 200), status: "failed" });
      return out;
    }
    // Claude stream-json: { type: "assistant"|"user", message: { content: [...] } }
    const msg = p.message && typeof p.message === "object" ? p.message : p;
    const content = Array.isArray(msg.content) ? msg.content : null;
    if (content) {
      for (const b of content) {
        if (!b || typeof b !== "object") continue;
        if (b.type === "tool_use") out.push(claudeToolStep(b));
        else if (b.type === "tool_result") out.push({ key: b.tool_use_id ? `cc-${b.tool_use_id}` : "", update: true, status: b.is_error ? "failed" : "done", detail: clip(blockText(b.content)) });
        else if (b.type === "thinking" && b.thinking) out.push({ kind: "think", title: clip(b.thinking, 280) });
      }
    }
    return out;
  }
  // Reply text = only the human-facing answer (codex agent_message / plain text / claude text blocks);
  // never tool markers or reasoning — those live in the process timeline now.
  function replyText(p) {
    if (!p || typeof p !== "object") return "";
    if (p.item && typeof p.item === "object" && p.item.type === "agent_message" && p.item.text) return String(p.item.text);
    const parts = [];
    for (const k of ["text", "result"]) if (typeof p[k] === "string" && p[k].trim()) parts.push(p[k]);
    const msg = p.message && typeof p.message === "object" ? p.message : p;
    if (Array.isArray(msg.content)) {
      // A Claude message that ALSO makes a tool call is mid-task narration ("I'll run X"), not the
      // final answer — skip its text so the reply isn't polluted by pre-tool chatter (codex review).
      const hasTool = msg.content.some((b) => b && b.type === "tool_use");
      if (!hasTool) for (const b of msg.content) if (b && b.type === "text" && typeof b.text === "string" && b.text.trim()) parts.push(b.text);
    }
    return parts.join("\n").trim();
  }
  function formatPmJsonObject(obj) {
    if (!obj || typeof obj !== "object") return "";
    const lines = [];
    if (obj.summary) lines.push(String(obj.summary));
    const notes = Array.isArray(obj.deliberation) ? obj.deliberation.filter(Boolean) : [];
    if (notes.length) lines.push(notes.map((x) => `- ${x}`).join("\n"));
    const todos = Array.isArray(obj.todo) ? obj.todo.filter(Boolean) : [];
    if (todos.length) lines.push(todos.map((x, i) => `${i + 1}. ${x}`).join("\n"));
    if (obj.follow_up) lines.push(`→ ${obj.follow_up}`);
    if (!lines.length && obj.body_md) lines.push(String(obj.body_md));
    return lines.join("\n\n").trim();
  }
  function jsonStringPrefix(body, key) {
    const m = String(body || "").match(new RegExp(`"${key}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)`));
    if (!m) return "";
    try { return JSON.parse(`"${m[1].replace(/\\$/, "")}"`).trim(); }
    catch (e) { return m[1].replace(/\\"/g, '"').replace(/\\n/g, "\n").trim(); }
  }
  function jsonArrayStringPrefixes(body, key) {
    const m = String(body || "").match(new RegExp(`"${key}"\\s*:\\s*\\[([\\s\\S]*)`));
    if (!m) return [];
    const fragment = m[1].split(/\]\s*[,}]/)[0] || "";
    return [...fragment.matchAll(/"((?:\\.|[^"\\])*)(?:"|$)/g)]
      .map((x) => {
        try { return JSON.parse(`"${x[1].replace(/\\$/, "")}"`).trim(); }
        catch (e) { return x[1].replace(/\\"/g, '"').replace(/\\n/g, "\n").trim(); }
      })
      .filter(Boolean)
      .slice(0, 8);
  }
  function formatPartialPmJsonObject(body) {
    const lines = [];
    const summary = jsonStringPrefix(body, "summary");
    if (summary) lines.push(summary);
    const notes = jsonArrayStringPrefixes(body, "deliberation");
    if (notes.length) lines.push(notes.map((x) => `- ${x}`).join("\n"));
    const todos = jsonArrayStringPrefixes(body, "todo");
    if (todos.length) lines.push(todos.map((x, i) => `${i + 1}. ${x}`).join("\n"));
    return lines.join("\n\n").trim();
  }
  function cleanPmStreamText(text) {
    const raw = String(text || "").trim();
    if (!raw) return "";
    let body = raw;
    if (body.startsWith("```")) {
      body = body.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
    }
    try {
      const obj = JSON.parse(body);
      return formatPmJsonObject(obj);
    } catch (e) {}
    if (/^[\{\[\]",:\s\}\]]/.test(body) || /"(summary|agent|model|effort|instruction|todo|deliberation|ready|done|reason|follow_up|todo_status)"\s*:/.test(body)) {
      return formatPartialPmJsonObject(body);
    }
    return raw;
  }
  function formatPmReasoningText(text) {
    return String(text || "")
      .replace(/([.!?])(\*\*[^*\n]{1,100}\*\*)/g, "$1\n\n$2")
      .trim();
  }
  function cleanThinkingTitle(text) {
    return String(text || "")
      .replace(/^\s{0,3}#{1,6}\s+/, "")
      .replace(/^\s*[-*+]\s+/, "")
      .replace(/[`*_~]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }
  function pmThinkingTitle(text, fallback) {
    return pmThinkingParts(text, fallback).title;
  }
  function pmThinkingParts(text, fallback) {
    const raw = formatPmReasoningText(text);
    if (!raw) return { title: fallback, body: "" };
    const bold = raw.match(/\*\*([^*\n]{1,140})\*\*/);
    if (bold) {
      const before = raw.slice(0, bold.index).trim();
      const after = raw.slice(bold.index + bold[0].length).trim();
      const body = [before, after].filter(Boolean).join("\n\n");
      return { title: clip(cleanThinkingTitle(bold[1]) || fallback, 140), body };
    }
    const lines = raw.split(/\r?\n/);
    const idx = lines.findIndex((line) => cleanThinkingTitle(line));
    if (idx < 0) return { title: fallback, body: raw };
    const title = cleanThinkingTitle(lines[idx]) || fallback;
    const body = lines.filter((_, i) => i !== idx).join("\n").trim();
    return { title: clip(title, 140), body };
  }
  function isPmToolEvent(e, p) {
    return (e && e.source === "pm-agent") || (p && p.source === "pm-agent");
  }
  function pmToolKey(p) {
    return p && (p.tool_use_id || p.id || p.call_id || "");
  }
  function pmToolInput(p) {
    return p && p.input && typeof p.input === "object" ? p.input : {};
  }
  function pmToolResult(p) {
    if (p && p.result && typeof p.result === "object") return p.result;
    if (p && typeof p.output === "string") {
      try { const obj = JSON.parse(p.output); if (obj && typeof obj === "object") return obj; } catch (e) {}
    }
    return {};
  }
  function pmToolData(p) {
    const result = pmToolResult(p);
    return result && result.data && typeof result.data === "object" ? result.data : {};
  }
  function pmPublicNote(p) {
    const input = pmToolInput(p);
    return String((p && p.public_note) || input.public_note || input.purpose || "").trim();
  }
  function pmVisibleInput(input) {
    const out = {};
    for (const [key, value] of Object.entries(input || {})) {
      if (key !== "public_note" && key !== "purpose") out[key] = value;
    }
    return out;
  }
  function pmToolKind(tool) {
    const name = String(tool || "");
    if (name === "read_file" || name === "list_files" || name === "work_mode_get") return "read";
    if (name === "search_repo" || name === "work_mode_search") return "find";
    if (name === "web_search" || name === "fetch_url" || name.startsWith("browser_")) return "web";
    if (name === "write_file" || name === "replace_in_file") return "edit";
    if (name === "run_command") return "cmd";
    return "tool";
  }
  function pmToolTarget(tool, input) {
    const name = String(tool || "");
    if (name === "run_command") return commandLine(input.command);
    if (name === "search_repo" || name === "web_search") return String(input.query || "").trim();
    if (name === "fetch_url" || name === "browser_open") return String(input.url || "").trim();
    if (name === "work_mode_get") return String(input.name || "").trim();
    if (name === "browser_click" || name === "browser_type") return String(input.ref || "").trim();
    return String(input.path || input.name || input.file_path || "").trim();
  }
  function pmToolPreTitle(p, lang) {
    const note = pmPublicNote(p);
    if (note) return clip(note, 180);
    const tool = String((p && p.tool) || "tool");
    const input = pmToolInput(p);
    const target = pmToolTarget(tool, input);
    const named = target ? ` ${target}` : "";
    if (lang === "zh") {
      if (tool === "read_file") return `我先读取${named}`;
      if (tool === "list_files") return `我先列出${named || "文件"}`;
      if (tool === "search_repo") return `我先检索${named}`;
      if (tool === "run_command") return `我先运行${named}`;
      if (tool === "fetch_url") return `我先抓取${named}`;
      if (tool === "web_search") return `我先联网搜索${named}`;
      if (tool === "write_file") return `我准备写入${named}`;
      if (tool === "replace_in_file") return `我准备修改${named}`;
      if (tool === "ask_question") return "我需要确认一个选择";
      if (tool === "work_mode_search") return "我先查找适用工作方式";
      if (tool === "work_mode_get") return `我先读取工作方式${named}`;
      if (tool.startsWith("browser_")) return `我操作浏览器${named}`;
      return `我调用工具 ${tool}${named}`;
    }
    if (tool === "read_file") return `Reading${named}`;
    if (tool === "list_files") return `Listing${named || " files"}`;
    if (tool === "search_repo") return `Searching${named}`;
    if (tool === "run_command") return `Running${named}`;
    if (tool === "fetch_url") return `Fetching${named}`;
    if (tool === "web_search") return `Searching the web${named}`;
    if (tool === "write_file") return `Writing${named}`;
    if (tool === "replace_in_file") return `Editing${named}`;
    if (tool === "ask_question") return "Asking for a decision";
    if (tool === "work_mode_search") return "Searching playbook";
    if (tool === "work_mode_get") return `Reading playbook item${named}`;
    if (tool.startsWith("browser_")) return `Using browser${named}`;
    return `Using ${tool}${named}`;
  }
  function pmToolOk(p) {
    const result = pmToolResult(p);
    return !(p && p.ok === false) && !(result && result.ok === false) && !(p && p.error) && !(result && result.error);
  }
  function pmToolError(p) {
    const result = pmToolResult(p);
    return String((p && (p.error || p.msg)) || (result && result.error) || "").trim();
  }
  function pmToolPostTitle(p, previous, lang) {
    const tool = String((p && p.tool) || (previous && previous.tool) || "tool");
    const data = pmToolData(p);
    const ok = pmToolOk(p);
    if (!ok) {
      const err = pmToolError(p);
      return lang === "zh" ? `${tool} 失败${err ? `：${err}` : ""}` : `${tool} failed${err ? `: ${err}` : ""}`;
    }
    if (tool === "read_file") {
      const lines = String(data.text || "").split(/\r?\n/).filter((_, i, arr) => i < arr.length - 1 || arr[i]).length;
      return lang === "zh" ? `读取完成，返回 ${lines} 行` : `Read complete, returned ${lines} lines`;
    }
    if (tool === "list_files") {
      const count = Array.isArray(data.files) ? data.files.length : 0;
      return lang === "zh" ? `列出完成，返回 ${count} 个文件` : `List complete, returned ${count} files`;
    }
    if (tool === "search_repo") {
      const count = Array.isArray(data.matches) ? data.matches.length : 0;
      return lang === "zh" ? `检索命中 ${count} 处` : `Search matched ${count} results`;
    }
    if (tool === "run_command") {
      const code = data.returncode != null ? data.returncode : "";
      return lang === "zh" ? `命令完成，exit ${code}` : `Command complete, exit ${code}`;
    }
    if (tool === "fetch_url") {
      return lang === "zh" ? `抓取完成，HTTP ${data.status_code || ""}` : `Fetch complete, HTTP ${data.status_code || ""}`;
    }
    if (tool === "web_search") {
      const count = Array.isArray(data.results) ? data.results.length : 0;
      return lang === "zh" ? `联网搜索返回 ${count} 条线索` : `Web search returned ${count} leads`;
    }
    if (tool === "write_file") return lang === "zh" ? `写入完成，${data.bytes || 0} bytes` : `Write complete, ${data.bytes || 0} bytes`;
    if (tool === "replace_in_file") return lang === "zh" ? `修改完成，替换 ${data.match_count || 0} 处` : `Edit complete, replaced ${data.match_count || 0}`;
    if (tool === "work_mode_search") {
      const count = Array.isArray(data.modes) ? data.modes.length : 0;
      return lang === "zh" ? `工作方式命中 ${count} 条` : `Playbook search matched ${count}`;
    }
    if (tool === "work_mode_get") return lang === "zh" ? `工作方式已读取：${data.name || ""}` : `Playbook item read: ${data.name || ""}`;
    return lang === "zh" ? `${tool} 完成` : `${tool} complete`;
  }
  function pmToolActivityDetail(p, previous) {
    const input = pmVisibleInput(previous && previous.input ? previous.input : pmToolInput(p));
    const result = pmToolResult(p);
    const lines = [];
    if (Object.keys(input).length) lines.push(`input\n${clip(JSON.stringify(input, null, 2), 1800)}`);
    if (result && Object.keys(result).length) lines.push(`result\n${clip(JSON.stringify(result, null, 2), 2400)}`);
    const logPath = (p && p.log_path) || (result && Array.isArray(result.artifact_paths) && result.artifact_paths[0]) || (result && result.data && result.data.log_path) || "";
    if (logPath) lines.push(`log\n${logPath}`);
    return lines.join("\n\n");
  }
  function looksEnglishPmStatus(text) {
    const v = String(text || "").trim();
    if (!v || /[\u3400-\u9fff]/.test(v) || v.length > 180) return false;
    if (/```|[{}[\]<>]|https?:|[\\\/][\w.-]+/.test(v)) return false;
    const letters = (v.match(/[A-Za-z]/g) || []).length;
    const visible = v.replace(/\s/g, "").length || 1;
    return letters >= 8 && letters / visible > 0.45;
  }
  function displayPmStreamText(text, lang, d) {
    return lang === "zh" && looksEnglishPmStatus(text) ? d.pmThinking : text;
  }
  function terminalText(payload) {
    const txt = extractAgentText(payload);
    if (txt) return txt;
    if (!payload || typeof payload !== "object") return String(payload || "");
    for (const key of ["stdout", "stderr", "output", "result", "msg", "error"]) {
      if (payload[key]) return String(payload[key]);
    }
    return "";
  }
  function isOpeningMetaLine(line) {
    const v = String(line || "").trim();
    return /^(i['’]?m ready to help|what would you like me to|the user wants me to|i need to|we need to|i should|let me|sure[,，]?|okay[,，]?\s+i(?:'ll| will))\b/i.test(v)
      || /^(好的|当然|没问题)[,，。！？\s]?/.test(v)
      || /^(我来|我会|我需要|让我|我们需要)/.test(v);
  }

  function firstSubstantiveLine(text) {
    const lines = String(text || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
    return (lines.find((line) => !isOpeningMetaLine(line)) || lines[0] || "").slice(0, 60);
  }
  function latestStepLine(steps) {
    const last = steps && steps.length ? steps[steps.length - 1] : null;
    return last ? (firstSubstantiveLine(last.title || "") || last.kind || "") : "";
  }

  // ---- markdown (ported, minimal-safe) ----
  const INLINE_RE = /(\[[^\]\n]{1,200}\]\(([^)\s]+)(?:\s+"[^"]*")?\)|`[^`\n]+`|\*\*[^*\n]+\*\*|~~[^~\n]+~~|\*[^*\n]+\*)/g;
  function clampMarkdown(text, maxChars) { const v = String(text || ""); return maxChars && v.length > maxChars ? `${v.slice(0, maxChars)}...` : v; }
  function safeHref(href) {
    const v = String(href || "").trim();
    if (/^(https?:|mailto:)/i.test(v)) return v;
    if (v.startsWith("#")) return v;
    if (v.startsWith("/") && !v.startsWith("//")) return v;
    return "";
  }
  function isMobileViewport() {
    return !!(window.matchMedia && window.matchMedia("(max-width: 760px)").matches);
  }
  function isLocalFileRef(value) {
    const v = String(value || "").trim();
    if (!v || v.length > 280 || /[\r\n]/.test(v)) return false;
    if (/^[a-z][a-z0-9+.-]*:/i.test(v) && !/^[A-Za-z]:[\\/]/.test(v)) return false;
    const hasPathShape = v.includes("/") || v.includes("\\") || v.startsWith(".") || /^[A-Za-z]:[\\/]/.test(v);
    return hasPathShape && /(?:^|[\\/])[^\\/]+\.[A-Za-z0-9_-]{1,16}(?::\d+)?$/.test(v);
  }
  function openLocalFileRef(path) {
    let ev;
    if (typeof CustomEvent === "function") ev = new CustomEvent("foreman:file-ref", { detail: { path } });
    else {
      ev = document.createEvent("CustomEvent");
      ev.initCustomEvent("foreman:file-ref", false, false, { path });
    }
    window.dispatchEvent(ev);
  }
  function renderInline(text, keyPrefix) {
    const value = String(text || "");
    const re = new RegExp(INLINE_RE.source, "g");
    const nodes = [];
    const pushText = (v) => { String(v || "").split("\n").forEach((part, i) => { if (i > 0) nodes.push(html`<br key=${`${keyPrefix}-br-${nodes.length}`} />`); if (part) nodes.push(part); }); };
    let last = 0, m;
    while ((m = re.exec(value)) !== null) {
      const tok = m[0];
      if (m.index > last) pushText(value.slice(last, m.index));
      const key = `${keyPrefix}-in-${nodes.length}`;
      if (tok.startsWith("`")) {
        const code = tok.slice(1, -1);
        nodes.push(isLocalFileRef(code)
          ? html`<button key=${key} type="button" className="inline-file-ref" title=${code} onClick=${(ev) => { ev.preventDefault(); ev.stopPropagation(); openLocalFileRef(code); }}><code>${code}</code></button>`
          : html`<code key=${key}>${code}</code>`);
      }
      else if (tok.startsWith("**")) nodes.push(html`<strong key=${key}>${renderInline(tok.slice(2, -2), key)}</strong>`);
      else if (tok.startsWith("~~")) nodes.push(html`<del key=${key}>${renderInline(tok.slice(2, -2), key)}</del>`);
      else if (tok.startsWith("*")) nodes.push(html`<em key=${key}>${renderInline(tok.slice(1, -1), key)}</em>`);
      else if (tok.startsWith("[")) {
        const close = tok.indexOf("](");
        const label = tok.slice(1, close);
        const href = safeHref(tok.slice(close + 2, -1).replace(/\s+"[^"]*"$/, ""));
        nodes.push(href ? html`<a key=${key} href=${href} target="_blank" rel="noreferrer">${renderInline(label, key)}</a>` : label);
      } else pushText(tok);
      last = m.index + tok.length;
    }
    if (last < value.length) pushText(value.slice(last));
    return nodes;
  }
  function splitRow(line) { let v = String(line || "").trim(); if (v.startsWith("|")) v = v.slice(1); if (v.endsWith("|")) v = v.slice(0, -1); return v.split("|").map((c) => c.trim()); }
  function isSep(line) { const cells = splitRow(line); return cells.length > 1 && cells.every((c) => /^:?-{3,}:?$/.test(c)); }
  function isBlockStart(lines, i) {
    const line = lines[i] || "";
    if (/^\s*```/.test(line) || /^#{1,6}\s+/.test(line) || /^\s*>/.test(line) || /^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) return true;
    return line.includes("|") && isSep(lines[i + 1] || "");
  }
  function renderBlocks(text, keyPrefix) {
    const lines = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    const nodes = []; let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i += 1; continue; }
      const key = `${keyPrefix}-b-${nodes.length}`;
      const fence = line.match(/^\s*```\s*([A-Za-z0-9_-]*)\s*$/);
      if (fence) { const body = []; i += 1; while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { body.push(lines[i]); i += 1; } if (i < lines.length) i += 1; nodes.push(html`<pre key=${key}><code>${body.join("\n")}</code></pre>`); continue; }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) { const Tag = `h${heading[1].length}`; nodes.push(html`<${Tag} key=${key}>${renderInline(heading[2], key)}</${Tag}>`); i += 1; continue; }
      if (/^\s*>/.test(line)) { const q = []; while (i < lines.length && /^\s*>/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, "")); i += 1; } nodes.push(html`<blockquote key=${key}>${renderBlocks(q.join("\n"), key)}</blockquote>`); continue; }
      const ul = line.match(/^\s*[-*+]\s+(.+)$/); const ol = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (ul || ol) { const Tag = ul ? "ul" : "ol"; const items = []; const marker = ul ? /^\s*[-*+]\s+(.+)$/ : /^\s*\d+[.)]\s+(.+)$/; while (i < lines.length) { const it = lines[i].match(marker); if (!it) break; items.push(it[1]); i += 1; } nodes.push(html`<${Tag} key=${key}>${items.map((it, j) => html`<li key=${`${key}-li-${j}`}>${renderInline(it, `${key}-li-${j}`)}</li>`)}</${Tag}>`); continue; }
      if (line.includes("|") && isSep(lines[i + 1] || "")) {
        const header = splitRow(line); const rows = []; i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) { rows.push(splitRow(lines[i])); i += 1; }
        nodes.push(html`<div className="markdown-table-wrap" key=${key}><table><thead><tr>${header.map((c, j) => html`<th key=${`${key}-h-${j}`}>${renderInline(c, `${key}-h-${j}`)}</th>`)}</tr></thead><tbody>${rows.map((row, ri) => html`<tr key=${`${key}-r-${ri}`}>${row.map((c, ci) => html`<td key=${`${key}-c-${ri}-${ci}`}>${renderInline(c, `${key}-c-${ri}-${ci}`)}</td>`)}</tr>`)}</tbody></table></div>`);
        continue;
      }
      const para = []; while (i < lines.length && lines[i].trim() && !isBlockStart(lines, i)) { para.push(lines[i]); i += 1; }
      nodes.push(html`<p key=${key}>${renderInline(para.join("\n"), key)}</p>`);
    }
    return nodes;
  }
  function MD({ text, className = "", maxChars = 0 }) {
    const cls = ["markdown-body", className].filter(Boolean).join(" ");
    return html`<div className=${cls}>${renderBlocks(clampMarkdown(text, maxChars), "md")}</div>`;
  }
  function clampPmToolRounds(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return PM_TOOLS_DEFAULT_ROUNDS;
    return Math.min(PM_TOOLS_MAX_ROUNDS, Math.max(PM_TOOLS_MIN_ROUNDS, Math.trunc(n)));
  }
  function normalizeTodoStatus(value) {
    const v = String(value || "pending").toLowerCase();
    if (v === "completed" || v === "done") return "done";
    if (v === "in_progress" || v === "active" || v === "running") return "active";
    if (v === "blocked") return "blocked";
    return "pending";
  }
  function todoRowsFrom(items, fallbackSteps) {
    const rows = [];
    const raw = Array.isArray(items) && items.length ? items : (fallbackSteps || []);
    raw.forEach((item, i) => {
      const title = typeof item === "string" ? item : String((item && (item.title || item.content || item.task)) || "");
      if (!title.trim()) return;
      const status = typeof item === "string" ? (i === 0 ? "active" : "pending") : normalizeTodoStatus(item.status);
      rows.push({ id: `t${rows.length}`, title: title.trim(), status });
    });
    return rows;
  }
  function mergeTodoRows(current, updates, done) {
    const rows = current.map((x) => ({ ...x }));
    const byTitle = new Map(rows.map((x, i) => [x.title, i]));
    for (const item of Array.isArray(updates) ? updates : []) {
      const title = String((item && (item.title || item.content || item.task)) || "").trim();
      if (!title) continue;
      const next = { id: `t${rows.length}`, title, status: normalizeTodoStatus(item.status) };
      if (byTitle.has(title)) rows[byTitle.get(title)] = { ...rows[byTitle.get(title)], status: next.status };
      else { byTitle.set(title, rows.length); rows.push(next); }
    }
    if (done) rows.forEach((x) => { x.status = "done"; });
    return rows;
  }

  // ---------------------------------------------------------------------------
  // event digest → thread / todos / subagents / terminal
  // ---------------------------------------------------------------------------
  function digest(events, d, lang) {
    const nodes = [];
    let lastPlan = null;
    let todos = [];
    const calls = new Map(); // taskId -> call
    const terminal = [];
    const streamGroups = new Map(); // key -> nodeIndex for pm streams
    const pmStreamBuffers = new Map(); // key -> raw text buffer
    const statusNodes = new Map(); // phase -> nodeIndex
    const pmActivityNodes = new Map(); // PM tool call id -> nodeIndex

    const callKey = (e) => e.task_id || `${e.source || "agent"}-${e.session_id || ""}`;
    const hidePmStatus = (phase = "") => {
      for (const [key, idx] of statusNodes.entries()) {
        if (!phase || key === phase) {
          if (nodes[idx]) nodes[idx].hidden = true;
          statusNodes.delete(key);
        }
      }
    };
    const ensureCall = (e) => {
      const k = callKey(e);
      if (!calls.has(k)) {
        calls.set(k, {
          id: k, agent: e.source || (e.payload && e.payload.agent) || "agent",
          status: "active", reply: "", lastReply: "", commands: [], diffs: [],
          steps: [], stepKeys: new Map(), timeline: [], timelineKeys: new Map(),
          ts: e.ts, started: e.ts,
        });
      }
      return calls.get(k);
    };
    // Add a process step, merging when the same logical step is seen again: codex item.started→
    // item.completed (matched by key), Claude tool_use→tool_result (matched by tool_use_id), or the
    // same action echoed by two sources (hook + stream) collapsed by adjacent kind+title.
    const mergeStep = (c, s) => {
      if (!s || (!s.title && !s.todos && !s.update)) return;
      if (s.update) {
        const idx = s.key && c.stepKeys.has(s.key) ? c.stepKeys.get(s.key) : -1;
        if (idx >= 0) { const ex = c.steps[idx]; if (s.status) ex.status = s.status; if (s.detail && !ex.detail) ex.detail = s.detail; }
        return;
      }
      if (s.key && c.stepKeys.has(s.key)) {
        const ex = c.steps[c.stepKeys.get(s.key)];
        if (s.title) ex.title = s.title;
        if (s.detail) ex.detail = s.detail;
        if (s.exit != null) ex.exit = s.exit;
        if (s.fileKind) ex.fileKind = s.fileKind;
        if (s.todos) ex.todos = s.todos;
        if (s.status) ex.status = s.status;
        return;
      }
      const last = c.steps[c.steps.length - 1];
      if (last && s.title && last.kind === s.kind && last.title === s.title && last.status === "active") {
        if (s.status) last.status = s.status;
        if (s.detail && !last.detail) last.detail = s.detail;
        if (s.exit != null) last.exit = s.exit;
        return;
      }
      c.steps.push(s);
      if (s.key) c.stepKeys.set(s.key, c.steps.length - 1);
    };
    const cleanTimelineStep = (s) => {
      const out = { ...(s || {}) };
      delete out.update;
      return out;
    };
    const pushCallTimeline = (c, item) => {
      if (!c || !item) return;
      const key = item.key || "";
      if (key && c.timelineKeys.has(key)) {
        const idx = c.timelineKeys.get(key);
        const prev = c.timeline[idx] || {};
        if (prev.kind === "step" && item.kind === "step") {
          const prevStep = prev.step || {};
          const nextStep = item.step || {};
          c.timeline[idx] = {
            ...prev, ...item, ts: prev.ts || item.ts,
            step: {
              ...prevStep, ...nextStep,
              title: nextStep.title || prevStep.title,
              detail: nextStep.detail || prevStep.detail,
              todos: nextStep.todos || prevStep.todos,
            },
          };
        } else {
          c.timeline[idx] = { ...prev, ...item, ts: prev.ts || item.ts };
        }
        return;
      }
      c.timeline.push(item);
      if (key) c.timelineKeys.set(key, c.timeline.length - 1);
    };
    const updateTimelineStep = (c, key, patch) => {
      if (!c || !key || !c.timelineKeys.has(key)) return;
      const idx = c.timelineKeys.get(key);
      const item = c.timeline[idx];
      if (!item || item.kind !== "step") return;
      item.step = { ...(item.step || {}), ...(patch || {}) };
    };
    const pushCallStep = (c, s, ts) => {
      if (!s) return;
      mergeStep(c, s);
      if (s.update) {
        if (s.key) updateTimelineStep(c, s.key, cleanTimelineStep(s));
        return;
      }
      if (!s.title && !s.todos) return;
      const step = s.key && c.stepKeys.has(s.key) ? c.steps[c.stepKeys.get(s.key)] : s;
      pushCallTimeline(c, { kind: "step", key: s.key || "", ts, step: cleanTimelineStep(step) });
    };
    const pushCallReply = (c, text, ts, final = false) => {
      const value = String(text || "").trim();
      if (!c || !value) return;
      c.lastReply = value;
      if (final) c.reply = value;
      const last = c.timeline[c.timeline.length - 1];
      if (final && last && last.kind === "reply" && String(last.text || "").trim() === value) {
        last.final = true;
        return;
      }
      pushCallTimeline(c, { kind: "reply", ts, text: value, final });
    };
    const upsertPmActivityPre = (e, p) => {
      const key = pmToolKey(p) || `pm-tool-${e.id || nodes.length}`;
      const input = pmToolInput(p);
      const node = {
        kind: "pm-activity", id: e.id || key, key, ts: e.ts,
        tool: p.tool || "tool", stepKind: pmToolKind(p.tool),
        status: "active", input,
        title: pmToolPreTitle(p, lang),
        detail: pmToolActivityDetail(p, { input }),
      };
      if (pmActivityNodes.has(key) && nodes[pmActivityNodes.get(key)]) nodes[pmActivityNodes.get(key)] = { ...nodes[pmActivityNodes.get(key)], ...node };
      else { pmActivityNodes.set(key, nodes.length); nodes.push(node); }
    };
    const upsertPmActivityPost = (e, p) => {
      const key = pmToolKey(p) || `pm-tool-${e.id || nodes.length}`;
      const idx = pmActivityNodes.has(key) ? pmActivityNodes.get(key) : -1;
      const previous = idx >= 0 ? nodes[idx] : null;
      const node = {
        kind: "pm-activity", id: previous ? previous.id : (e.id || key), key, ts: e.ts,
        tool: p.tool || (previous && previous.tool) || "tool",
        stepKind: pmToolKind(p.tool || (previous && previous.tool)),
        status: pmToolOk(p) ? "done" : "failed",
        input: previous && previous.input ? previous.input : pmToolInput(p),
        title: pmToolPostTitle(p, previous, lang),
        detail: pmToolActivityDetail(p, previous),
      };
      if (idx >= 0 && nodes[idx]) nodes[idx] = { ...previous, ...node };
      else { pmActivityNodes.set(key, nodes.length); nodes.push(node); }
    };
    const rememberPmActivityLog = (p) => {
      const key = pmToolKey(p);
      if (!key || !pmActivityNodes.has(key) || !p.log_path) return;
      const node = nodes[pmActivityNodes.get(key)];
      if (!node) return;
      const line = `log\n${p.log_path}`;
      if (!String(node.detail || "").includes(line)) node.detail = [node.detail, line].filter(Boolean).join("\n\n");
    };

    for (const e of events) {
      const t = e.type;
      const p = e.payload || {};
      if (t === "dispatch") {
        const autoAgent = p.pm_agent && !(Array.isArray(p.direct_agents) && p.direct_agents.length);
        nodes.push({ kind: "user", id: e.id || `u-${nodes.length}`, ts: e.ts, goal: p.goal || "", chips: [autoAgent ? null : p.agent, p.model, p.effort].filter(Boolean) });
      } else if (t === "pm_plan") {
        hidePmStatus("plan");
        const steps = Array.isArray(p.todo) ? p.todo.map((x) => String(x)) : (typeof p.todo === "string" && p.todo ? [p.todo] : []);
        lastPlan = { steps, summary: p.summary || "", instruction: p.instruction || "" };
        todos = todoRowsFrom(p.todo_status, steps);
        nodes.push({ kind: "plan", id: e.id || `p-${nodes.length}`, ts: e.ts, steps, summary: p.summary || "", deliberation: Array.isArray(p.deliberation) ? p.deliberation : [], instruction: p.instruction || "" });
      } else if (t === "pm_reply") {
        hidePmStatus();
        const txt = String(p.text || p.reply || "").trim();
        if (txt) nodes.push({ kind: "pm", id: e.id || `pmr-${nodes.length}`, ts: e.ts, text: txt });
      } else if (t === "pm_review") {
        const status = p.done ? (lang === "zh" ? "复查通过" : "review passed") : (lang === "zh" ? "需要跟进" : "needs follow-up");
        todos = mergeTodoRows(todos, p.todo_status, !!p.done);
        if (p.done) hidePmStatus();
        nodes.push({ kind: "pm-review", id: e.id || `pr-${nodes.length}`, ts: e.ts, status, summary: p.summary || "", reason: p.reason || "", followUp: p.follow_up || "", done: !!p.done });
      } else if (t === "pm_output" || t === "pm_reasoning") {
        const rawTxt = extractStreamText(p);
        if (!rawTxt) continue;
        if (p.event_type === "status" || p.status === "working") {
          const key = p.phase || p.stream_id || "pm";
          const statusText = displayPmStreamText(rawTxt, lang, d);
          if (statusNodes.has(key) && nodes[statusNodes.get(key)]) {
            nodes[statusNodes.get(key)].text = statusText;
            nodes[statusNodes.get(key)].ts = e.ts;
          } else {
            statusNodes.set(key, nodes.length);
            // `started` anchors the live "已 N 秒" planning timer (T2.2); kept across text updates.
            nodes.push({ kind: "pm-status", id: e.id || `ps-${nodes.length}`, ts: e.ts, started: e.ts, text: statusText });
          }
          continue;
        }
        const sid = p.stream_id || "";
        const gk = `${t}-${e.source || ""}-${sid || "plain"}`;
        const cleaned = cleanPmStreamText(sid ? `${pmStreamBuffers.get(gk) || ""}${rawTxt}` : rawTxt);
        const txt = t === "pm_reasoning" ? formatPmReasoningText(cleaned) : displayPmStreamText(cleaned, lang, d);
        if (sid) pmStreamBuffers.set(gk, `${pmStreamBuffers.get(gk) || ""}${rawTxt}`);
        if (!txt) continue;
        if (p.phase) hidePmStatus(p.phase);
        if (sid && streamGroups.has(gk)) {
          const idx = streamGroups.get(gk);
          nodes[idx].text = txt;
        } else {
          const node = { kind: t === "pm_reasoning" ? "pm-thinking" : "pm", id: e.id || `pm-${nodes.length}`, ts: e.ts, text: txt };
          if (sid) streamGroups.set(gk, nodes.length);
          nodes.push(node);
        }
      } else if (t === "agent_start") {
        hidePmStatus("launch");
        const c = ensureCall(e);
        const cmd = commandLine(p.command || p.cmd);
        const cwd = p.cwd || "";
        if (cmd) {
          c.commands.push(cmd);
          pushCallTimeline(c, { kind: "cmd", ts: e.ts, command: cmd, launch: true });
          terminal.push({ kind: "cmd", text: cmd, ts: e.ts, agent: e.source, cwd });
        }
        c.ts = e.ts;
        if (!nodes.some((n) => n.kind === "call" && n.callId === c.id)) nodes.push({ kind: "call", id: `call-${c.id}`, callId: c.id, ts: e.ts });
      } else if (t === "agent_output" || t === "agent_reasoning") {
        const c = ensureCall(e);
        if (t === "agent_reasoning") {
          // Reasoning is a process step (💭), not part of the final answer — keep it out of the reply.
          const rtxt = extractAgentText(p);
          if (rtxt) pushCallStep(c, { kind: "think", title: clip(rtxt, 280) }, e.ts);
        } else {
          for (const s of stepsFromAgentPayload(p)) pushCallStep(c, s, e.ts);
          const txt = replyText(p);
          if (txt) pushCallReply(c, txt, e.ts);
          const rawTxt = extractAgentText(p);
          if (rawTxt) terminal.push({ kind: "out", text: rawTxt, ts: e.ts, agent: e.source });
        }
        c.ts = e.ts;
        if (!nodes.some((n) => n.kind === "call" && n.callId === c.id)) nodes.push({ kind: "call", id: `call-${c.id}`, callId: c.id, ts: e.ts });
      } else if (t === "tool_pre") {
        if (isPmToolEvent(e, p)) {
          upsertPmActivityPre(e, p);
          continue;
        }
        // Hook/operator-driven tool calls (e.g. Claude Code) → process steps, same as stream tool_use.
        const c = ensureCall(e);
        const key = p.tool_use_id || p.id || p.call_id ? `tp-${p.tool_use_id || p.id || p.call_id}` : "";
        const cmd = p.command || p.cmd || (p.tool === "run_command" && p.input && p.input.command);
        if (cmd) { const line = commandLine(cmd); pushCallStep(c, { key, kind: "cmd", title: line, status: "active" }, e.ts); terminal.push({ kind: "cmd", text: line, ts: e.ts }); }
        else if (p.tool) pushCallStep(c, { key, kind: "tool", title: String(p.tool), status: "active" }, e.ts);
        c.ts = e.ts;
      } else if (t === "tool_stream") {
        if (isPmToolEvent(e, p)) {
          const text = String(p.delta || "");
          const kind = p.stream === "stderr" ? "err" : "out";
          if (text) terminal.push({ kind, text, ts: e.ts, agent: e.source });
          rememberPmActivityLog(p);
          continue;
        }
        const c = ensureCall(e);
        const key = p.tool_use_id || p.id || p.call_id ? `tp-${p.tool_use_id || p.id || p.call_id}` : "";
        const text = String(p.delta || "");
        const kind = p.stream === "stderr" ? "err" : "out";
        if (text) terminal.push({ kind, text, ts: e.ts, agent: e.source });
        if (key && c.stepKeys.has(key)) {
          const ex = c.steps[c.stepKeys.get(key)];
          const next = `${ex.detail ? `${ex.detail}\n` : ""}${text}`.trim();
          ex.detail = clip(next, 1600);
          ex.status = "active";
          updateTimelineStep(c, key, { detail: ex.detail, status: "active" });
        }
      } else if (t === "tool_post") {
        if (isPmToolEvent(e, p)) {
          upsertPmActivityPost(e, p);
          const result = p.result && typeof p.result === "object" ? p.result : null;
          const data = result && result.data && typeof result.data === "object" ? result.data : {};
          const out = data.stdout || data.stderr || p.output || p.result || "";
          if (data.log_path) terminal.push({ kind: "out", text: `log: ${data.log_path}`, ts: e.ts, agent: e.source });
          else if (out) terminal.push({ kind: "out", text: String(out).slice(0, 4000), ts: e.ts, agent: e.source });
          continue;
        }
        const c = ensureCall(e);
        const result = p.result && typeof p.result === "object" ? p.result : null;
        const data = result && result.data && typeof result.data === "object" ? result.data : {};
        const out = data.stdout || data.stderr || p.output || p.result || "";
        const key = p.tool_use_id || p.id || p.call_id ? `tp-${p.tool_use_id || p.id || p.call_id}` : "";
        if (key && c.stepKeys.has(key)) {
          const ex = c.steps[c.stepKeys.get(key)];
          ex.status = p.is_error || p.error ? "failed" : "done";
          if (out && !ex.detail) ex.detail = clip(out);
          if (data.log_path && !ex.detail) ex.detail = `log: ${data.log_path}`;
          if (typeof p.exit_code === "number") ex.exit = p.exit_code;
          if (typeof data.returncode === "number") ex.exit = data.returncode;
          updateTimelineStep(c, key, { status: ex.status, detail: ex.detail, exit: ex.exit });
        }
        if (data.log_path) terminal.push({ kind: "out", text: `log: ${data.log_path}`, ts: e.ts, agent: e.source });
        else if (out) terminal.push({ kind: "out", text: String(out).slice(0, 4000), ts: e.ts, agent: e.source });
      } else if (t === "git_diff") {
        const c = ensureCall(e);
        const file = p.path || p.file || (p.files && p.files[0] && p.files[0].path) || "";
        const stat = p.stat || (p.additions != null ? `+${p.additions} −${p.deletions || 0}` : "");
        if (file) c.diffs.push({ file, stat, lines: (p.files && p.files[0] && p.files[0].lines) || p.lines || [] });
      } else if (t === "approval_req") {
        // The actionable approval (with its one-time nonce) is appended from /api/approvals; here
        // we only drop a marker into the flow so the conversation shows when one was raised.
        nodes.push({ kind: "system", id: e.id || `ar-${nodes.length}`, ts: e.ts, label: d.approvals, tone: "amber", text: p.action || "" });
      } else if (t === "briefing") {
        nodes.push({ kind: "pm", id: e.id || `b-${nodes.length}`, ts: e.ts, text: `**${p.title || d.briefing}**\n\n${p.body_md || p.summary || ""}` });
      } else if (t === "stop") {
        hidePmStatus();
        const out = terminalText(p);
        // Settle every active call/step, but the result text (claude's authoritative final answer) is
        // written ONLY to the call this stop belongs to — never broadcast to other parallel subagents.
        const target = calls.get(callKey(e)) || (calls.size === 1 ? calls.values().next().value : null);
        for (const c of calls.values()) {
          if (c.status === "active") c.status = "done";
          for (const s of c.steps) if (s.status === "active") {
            s.status = "done";
            if (s.key) updateTimelineStep(c, s.key, { status: "done" });
          }
        }
        if (out && target) pushCallReply(target, out, e.ts, true);
        if (out) terminal.push({ kind: "out", text: out, ts: e.ts, agent: e.source });
        nodes.push({ kind: "system", id: e.id || `s-${nodes.length}`, ts: e.ts, label: d.ev_stop, tone: "green", text: "" });
      } else if (t === "error") {
        hidePmStatus();
        const out = terminalText(p);
        if (out) terminal.push({ kind: "err", text: out, ts: e.ts, agent: e.source });
        // Lead with a localized watchdog reason (wall-clock / no-progress / repetition) when the
        // dispatch error carries one, then the raw technical message (T0.5).
        const reasonLine = friendlyReason(p.reason, d);
        const rawMsg = p.msg || p.error || "";
        const errText = reasonLine && reasonLine !== rawMsg ? [reasonLine, rawMsg].filter(Boolean).join("\n\n") : rawMsg;
        nodes.push({ kind: "system", id: e.id || `e-${nodes.length}`, ts: e.ts, label: d.ev_error, tone: "red", text: errText });
      } else if (t === "notification") {
        const label = p.label || p.title || (p.kind === "cancelled" ? d.sessionCanceled : d.notification);
        nodes.push({ kind: "system", id: e.id || `n-${nodes.length}`, ts: e.ts, label, tone: "muted", text: p.msg || p.text || "" });
      } else if (t === "context_compact") {
        const view = contextPackView(p);
        nodes.push({ kind: "context-pack", id: e.id || `cp-${nodes.length}`, ts: e.ts, label: d.compactDone, ...view });
      } else if (["checkpoint", "gate", "action_executed", "action_undone", "review", "audit", "undo", "recover", "stall"].includes(t)) {
        nodes.push({ kind: "system", id: e.id || `sy-${nodes.length}`, ts: e.ts, label: d[`ev_${t}`] || t, tone: "muted", text: p.summary || p.note || p.disposition || "" });
      }
    }

    if (!todos.length && lastPlan) todos = todoRowsFrom([], lastPlan.steps);

    // subagents from calls — the activity line is the latest process step, falling back to the reply.
    const subagents = Array.from(calls.values()).map((c) => {
      const visibleReply = c.reply || c.lastReply || "";
      const replyLine = firstSubstantiveLine(visibleReply);
      const stepLine = latestStepLine(c.steps);
      return {
        id: c.id, name: c.agent,
        agent: c.agent, status: c.status,
        act: stepLine || replyLine,
        detail: visibleReply,
      };
    });

    return { nodes: nodes.filter((n) => !n.hidden), calls, todos, terminal, subagents };
  }

  // ---------------------------------------------------------------------------
  // small UI atoms
  // ---------------------------------------------------------------------------
  function Empty({ icon, text }) { return html`<div className="empty"><div className="empty-icon">${icon || "✶"}</div><div>${text}</div></div>`; }
  function Switch({ on, onChange }) { return html`<button className=${`switch${on ? " on" : ""}`} onClick=${() => onChange(!on)} aria-pressed=${on} type="button"></button>`; }
  // Self-ticking elapsed counter for the live plan phase: shows the PM step is alive even when no
  // new reasoning delta has arrived for a while, so it never reads as a frozen "正在规划…" (T2.2).
  function PmElapsed({ start, lang }) {
    const startMs = useMemo(() => { const t = new Date(start).getTime(); return Number.isNaN(t) ? Date.now() : t; }, [start]);
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
    const secs = Math.max(0, Math.round((now - startMs) / 1000));
    return html`<span className="pm-elapsed">· ${lang === "zh" ? `已 ${secs} 秒` : `${secs}s`}</span>`;
  }

  // One process-step row in the 执行过程 timeline: a category chip + the action (command / file /
  // query / tool / plan / thought), with a file-kind chip, a command exit badge, and a live spinner
  // or failure mark. Kind → chip label + color class.
  const STEP_META = {
    cmd: { k: "kCmd", cls: "st-cmd" }, edit: { k: "kEdit", cls: "st-edit" },
    read: { k: "kRead", cls: "st-read" }, find: { k: "kFind", cls: "st-find" },
    web: { k: "kWeb", cls: "st-web" }, tool: { k: "kTool", cls: "st-tool" },
    plan: { k: "kPlan", cls: "st-plan" }, think: { k: "kThink", cls: "st-think" },
  };
  function StepRow({ s, d }) {
    const meta = STEP_META[s.kind] || STEP_META.tool;
    const active = s.status === "active";
    const failed = s.status === "failed";
    const fk = s.fileKind ? (s.fileKind === "add" ? d.fkAdd : s.fileKind === "delete" ? d.fkDelete : d.fkUpdate) : "";
    return html`<div className=${`proc-step ${meta.cls}${active ? " active" : ""}${failed ? " failed" : ""}`}>
      <span className="step-chip">${d[meta.k]}</span>
      <div className="step-main">
        ${s.kind === "plan" && Array.isArray(s.todos)
          ? html`<div className="step-todos">${s.todos.map((t, i) => html`<div className=${`step-todo${t.done ? " done" : ""}`} key=${i}><span className="tk">${t.done ? "✓" : "○"}</span>${t.text}</div>`)}</div>`
          : html`<div className="step-title">${s.title || d[meta.k]}</div>`}
        ${s.detail ? html`<div className="step-detail">${clip(s.detail, 300)}</div>` : null}
      </div>
      ${fk ? html`<span className=${`fk fk-${s.fileKind}`}>${fk}</span>` : null}
      ${s.kind === "cmd" && s.exit != null ? html`<span className=${`exitb${s.exit === 0 ? " ok" : " bad"}`}>${s.exit === 0 ? "✓ 0" : `✗ ${s.exit}`}</span>` : null}
      ${active ? html`<span className="step-spin"></span>` : failed ? html`<span className="step-x">!</span>` : null}
    </div>`;
  }

  // Subagent execution card: one chronological timeline. Ordinary agent_output stays in-place as
  // a reply item; only an explicit stop/result payload is labeled as the final reply.
  function callTimelineItems(c, running) {
    const items = Array.isArray(c.timeline) && c.timeline.length ? [...c.timeline] : [];
    if (!items.length) {
      for (const cmd of c.commands || []) items.push({ kind: "cmd", command: cmd, launch: true });
      for (const step of c.steps || []) items.push({ kind: "step", step });
      if (c.reply) items.push({ kind: "reply", text: c.reply, final: true });
    }
    const hasActive = items.some((item) => item.kind === "step" && item.step && item.step.status === "active");
    if (running && !hasActive) items.push({ kind: "live", status: "active" });
    return items;
  }

  function timelineLabel(item, d) {
    if (item.kind === "cmd") return d.commandsRun;
    if (item.kind === "reply") return item.final ? d.finalReply : d.reply;
    if (item.kind === "live") return d.processLabel;
    if (item.kind === "step") {
      const meta = STEP_META[(item.step && item.step.kind) || "tool"] || STEP_META.tool;
      return d[meta.k] || d.processLabel;
    }
    return d.processLabel;
  }

  function CallTimelineItem({ item, i, d }) {
    const step = item.step || {};
    const stepKind = item.kind === "step" ? (step.kind || "tool") : item.kind;
    const active = item.kind === "live" || item.status === "active" || (item.kind === "step" && step.status === "active");
    const failed = item.status === "failed" || (item.kind === "step" && step.status === "failed");
    const badge = failed
      ? html`<span className="stage-badge failed">!</span>`
      : active ? html`<span className="stage-badge active"><span className="stage-spin"></span></span>`
      : html`<span className="stage-badge done">✓</span>`;
    return html`<div className=${`call-stage timeline-${item.kind} st-${stepKind}`} key=${i}>
      ${badge}
      <div className="stage-body">
        <div className="stage-name">${timelineLabel(item, d)}</div>
        ${item.kind === "cmd" ? html`<div className="term-block"><div className=${item.launch ? "cmd-launch" : ""}><span className="cmd-prompt">$</span> ${item.command || ""}</div></div>` : null}
        ${item.kind === "step" ? html`<div className="proc-steps single"><${StepRow} s=${step} d=${d} /></div>` : null}
        ${item.kind === "reply" ? html`<div className=${`stage-reply${item.final ? " final" : ""}`}><${MD} text=${item.text || ""} maxChars=${item.final ? 6000 : 2400} /></div>` : null}
        ${item.kind === "live" ? html`<div className="proc-live"><span className="proc-bar"><span></span></span><span className="proc-txt">${d.executing}...</span></div>` : null}
      </div>
    </div>`;
  }

  function CallCard({ c, d, lang, open, onToggle }) {
    const running = c.status === "active";
    const avatarColor = c.agent && c.agent.toLowerCase().includes("codex") ? "var(--violet)" : "var(--accent)";
    const avatar = (c.agent || "A").slice(0, 1).toUpperCase();
    const timeline = callTimelineItems(c, running);
    const visibleReply = c.reply || c.lastReply || "";
    const replySummary = firstSubstantiveLine(visibleReply);
    return html`<div className=${`call${open ? " open" : ""}${running ? " running" : ""}`}>
      <div className="call-head" onClick=${() => onToggle(c.id)}>
        <span className="call-avatar" style=${{ background: avatarColor }}>${avatar}</span>
        <div style=${{ flex: 1, minWidth: 0 }}>
          <div className="call-title">
            <span className="call-agent">${c.agent}</span>
            ${running
              ? html`<span className="tag accent live"><span className="call-live-dot"></span>${d.running}</span>`
              : html`<span className="tag green">${d.done}</span>`}
            ${running && c.started ? html`<${PmElapsed} start=${c.started} lang=${lang} />` : null}
          </div>
          <div className="call-summary">${Math.max(0, timeline.length - (running ? 1 : 0))} ${d.stepsWord}${c.diffs.length ? ` · ${c.diffs.length} diff` : ""}${replySummary ? ` · ${replySummary.slice(0, 42)}` : ""}</div>
        </div>
        <span className="call-toggle">${open ? d.hide : d.open}${open ? " ▾" : " ▸"}</span>
      </div>
      ${running ? html`<div className="call-progress"><span></span></div>` : null}
      ${open ? html`<div className="call-detail timeline">
        ${timeline.length ? timeline.map((item, i) => html`<${CallTimelineItem} key=${i} item=${item} i=${i} d=${d} />`) : html`<div className="stage-muted">${d.noSteps}</div>`}
        ${c.diffs.length ? html`<div className="proc-diffs"><div className="step-sub">${d.changeDetail}</div>${c.diffs.map((df, i) => html`<div className="diff-file" key=${i}><div className="fhead"><span className="muted" style=${{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>${df.file}</span><span className="stat">${df.stat}</span></div>${(df.lines || []).slice(0, 30).map((l, j) => html`<div className=${`diff-line ${l.kind === "add" ? "add" : l.kind === "del" ? "del" : ""}`} key=${j}>${l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}${l.text || ""}</div>`)}</div>`)}</div>` : null}
      </div>` : null}
    </div>`;
  }

  // ---------------------------------------------------------------------------
  // Launch overlay
  // ---------------------------------------------------------------------------
  function Launch({ d, lang, hiding, steps }) {
    return html`
      <div className=${`launch${hiding ? " is-hiding" : ""}`}>
        <div className="launch-inner">
          <div className="launch-orbit">
            <div className="launch-ring"></div>
            <div className="launch-dot1"></div>
            <div className="launch-dot2"></div>
            <div className="launch-core"></div>
          </div>
          <div className="launch-title">Foreman</div>
          <div className="launch-tag">${d.launchTag}</div>
          <div className="launch-progress"><span style=${{ width: `${steps.pct}%` }}></span></div>
          <div className="launch-steps">
            <div className=${`launch-step ${steps.engine ? "done" : "wait"}`}><span>${steps.engine ? "✓" : "○"}</span><span>${d.launchEngine}</span></div>
            <div className=${`launch-step ${steps.agents ? "done" : "wait"}`}><span>${steps.agents ? "✓" : "○"}</span><span>${d.launchAgents}</span></div>
            <div className=${`launch-step ${steps.data ? "done" : "now"}`}>${steps.data ? html`<span>✓</span>` : html`<span className="spin"></span>`}<span>${d.launchLoad}</span></div>
          </div>
          <div className="launch-foot">${steps.version ? `v${steps.version} · ` : ""}${location.host}</div>
        </div>
      </div>`;
  }

  // ---------------------------------------------------------------------------
  // Sidebar / nav
  // ---------------------------------------------------------------------------
  function NavList({ d, view, onView, counts }) {
    return html`<nav className="sb-nav">
      ${NAV.map((n) => html`
        <button key=${n.key} className=${`nav-item${view === n.key ? " active" : ""}`} onClick=${() => onView(n.key)}>
          <span className="ico">${n.ico}</span><span>${d[n.label]}</span>
          ${counts[n.key] ? html`<span className=${`count ${n.key === "decisions" ? "amber" : "accent"}`}>${counts[n.key]}</span>` : null}
        </button>`)}
    </nav>`;
  }

  function Sidebar({ d, lang, view, onView, counts, sessions, selected, onSelect, onNew, onRename, version }) {
    return html`
      <aside className="sidebar desktop">
        <div className="sb-brand">
          <div className="name">Foreman</div>
          <div className="sub">${d.productSubtitle}${version ? ` · v${version}` : ""}</div>
        </div>
        <${NavList} d=${d} view=${view} onView=${onView} counts=${counts} />
        <div className="sb-section"><span>${d.sessions}</span><span className="add" onClick=${onNew} title=${d.newSession}>+</span></div>
        <div className="sb-sessions">
          ${!sessions.length ? html`<${Empty} icon="✉" text=${d.noActiveSession} />` :
            sessions.map((s) => html`<${SessionItem} key=${s.id} s=${s} d=${d} lang=${lang} active=${s.id === selected} onClick=${() => onSelect(s.id)} onRename=${onRename} />`)}
        </div>
        <div className="sb-user">
          <div className="avatar">${(localStorage.getItem("foreman.user") || "J").slice(0, 1).toUpperCase()}</div>
          <div><div className="uname">${localStorage.getItem("foreman.user") || "jiang"}</div><div className="urole">${d.personalMode}</div></div>
        </div>
      </aside>`;
  }

  function sessionStatusLabel(status, d) {
    const st = String(status || "").toLowerCase();
    if (st.includes("run") || st.includes("active")) return d.running;
    if (st.includes("cancel")) return d.cancelled;
    if (st.includes("stall")) return d.stalled;
    if (st.includes("fail") || st.includes("error")) return d.failed;
    if (st.includes("done") || st.includes("complete")) return d.done;
    if (st.includes("queue")) return d.queued;
    return status || "-";
  }

  // Map a watchdog reason code (dispatch_service error payload) to a human line. Unknown codes
  // fall back to the raw code so a new reason is never silently swallowed.
  function friendlyReason(reason, d) {
    const code = String(reason || "").trim().toLowerCase();
    if (!code) return "";
    if (code === "wall_clock_timeout") return d.reasonWallClock;
    if (code === "no_progress_timeout") return d.reasonNoProgress;
    if (code === "structured_repetition") return d.reasonRepetition;
    return reason;
  }

  // Latest terminal-failure explanation for the header: the most recent `error` event's reason
  // code (preferred), else a generic stalled note, else the raw message — so a hung/aborted PM
  // turn always shows WHY instead of an empty spinner (T0.5).
  function lastFailureReason(events, d) {
    for (let i = (events || []).length - 1; i >= 0; i--) {
      const e = events[i];
      if (!e || e.type !== "error") continue;
      const p = e.payload || {};
      const reason = friendlyReason(p.reason, d);
      if (reason) return reason;
      if (String(p.status || "").toLowerCase() === "stalled") return d.reasonStalled;
      if (p.msg || p.error) return String(p.msg || p.error);
      return "";
    }
    return "";
  }

  function SessionItem({ s, d, lang, active, onClick, onRename }) {
    const st = (s.status || "").toLowerCase();
    const dotColor = st.includes("run") || st.includes("active") ? "var(--accent)" : (s.pending_approvals || s.open_cards) ? "var(--amber)" : st.includes("done") || st.includes("complete") ? "var(--green)" : st.includes("stall") || st.includes("fail") || st.includes("error") ? "var(--red)" : "var(--faint)";
    const live = st.includes("run") || st.includes("active");
    const metaBits = [s.agent_type || "-", sessionStatusLabel(s.status, d), formatTime(s.updated_at || s.last_event_ts || s.created_at, lang)].filter(Boolean);
    return html`
      <div className=${`sess${active ? " active" : ""}`} onClick=${onClick}>
        <div className="sess-head">
          <span className=${`dot${live ? " live" : ""}`} style=${{ background: dotColor }}></span>
          <span className="sess-title editable-title" title=${d.editSessionTitle} onDoubleClick=${(e) => { e.stopPropagation(); onRename && onRename(s); }}>${s.goal || s.id}</span>
        </div>
        <div className="sess-meta">${metaBits.join(" · ")}</div>
      </div>`;
  }

  // top controls (theme/lang/push) reused
  function TopCtrls({ d, lang, dark, onToggleTheme, onToggleLang, onPush }) {
    return html`<div className="topctrls">
      <button className="btn icon" onClick=${onToggleTheme} title=${d.theme}>${dark ? "🌙" : "☀️"}</button>
      <button className="btn" onClick=${onToggleLang}>${lang === "zh" ? "EN" : "中"}</button>
      <button className="btn" onClick=${onPush}>🔔 ${d.enable}</button>
    </div>`;
  }

  // ===========================================================================
  // Workspace
  // ===========================================================================
  function threadExtras(dig, cards, approvals, sessionRow) {
    const sid = sessionRow && sessionRow.id;
    const cn = (cards || []).filter((c) => !c.session_id || c.session_id === sid)
      .map((c) => ({ kind: "card", id: `card-${c.id}`, cardId: c.id, payload: c }));
    const an = (approvals || []).filter((a) => !a.session_id || a.session_id === sid)
      .map((a) => ({ kind: "approval", id: `appr-${a.id}`, approvalId: a.id, payload: a }));
    return [...dig.nodes, ...cn, ...an];
  }

  function Workspace(props) {
    const { d, lang, dig, sessionRow, events, autonomy, openCalls, toggleCall, expandedSub, toggleSub,
      rightTab, setRightTab, onCard, onApproval, openDetail, composer, runCompact, compacting, compactStatus, onBriefing,
      cards, approvals, onCancelSession, onRetrySession, onDeleteSession, onRenameSession, topControls, onCopy } = props;
    const threadNodes = threadExtras(dig, cards, approvals, sessionRow);
    const threadRef = useRef(null);
    const stickToBottomRef = useRef(true);
    const lastSessionIdRef = useRef(sessionRow && sessionRow.id);
    const lastThreadNodeId = threadNodes.length ? threadNodes[threadNodes.length - 1].id : "";
    const agentType = displayAgent(sessionRow && sessionRow.agent_type, d);
    const status = String((sessionRow && sessionRow.status) || "").toLowerCase();
    const statusKey = status.replace(/[\s-]+/g, "_");
    const live = sessionRow && ["planning", "queued", "running", "active", "waiting_approval"].includes(statusKey);
    const failed = status.includes("fail") || status.includes("error");
    const stalled = status.includes("stall");
    const cancelled = status.includes("cancel");
    const done = status.includes("done") || status.includes("complete");
    const ContextPanel = window.ForemanContextUI && window.ForemanContextUI.ContextPanel;
    // A watchdog-aborted PM turn lands as `stalled`; surface it as a terminal failure (red tag +
    // retry) so a hung plan never shows as a perpetual "running" spinner (T0.4 → T0.5).
    const terminalFail = failed || stalled;
    const statusText = live ? d.running : cancelled ? d.cancelled : stalled ? d.stalled : failed ? d.failed : done ? d.done : ((sessionRow && sessionRow.status) || "");
    const failReason = terminalFail ? lastFailureReason(events, d) : "";
    const onBars = Math.max(0, Math.min(4, autonomy + 1));
    const autonomyName = d[`auto${autonomy}`] || `L${autonomy}`;
    useEffect(() => {
      const el = threadRef.current;
      if (!el) return;
      const sid = sessionRow && sessionRow.id;
      const switchedSession = lastSessionIdRef.current !== sid;
      if (switchedSession) {
        lastSessionIdRef.current = sid;
        stickToBottomRef.current = true;
      }
      if (!stickToBottomRef.current && !switchedSession) return;
      requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
    }, [sessionRow && sessionRow.id, threadNodes.length, lastThreadNodeId]);
    function onThreadScroll() {
      const el = threadRef.current;
      if (!el) return;
      stickToBottomRef.current = (el.scrollHeight - el.scrollTop - el.clientHeight) < 72;
    }
    return html`
      <div className="main">
        <div className="sess-header">
          <div style=${{ minWidth: 0 }}>
            <div style=${{ display: "flex", alignItems: "center", gap: "9px" }}>
              <h2 className=${sessionRow ? "editable-title" : ""} title=${sessionRow ? d.editSessionTitle : ""} onDoubleClick=${sessionRow ? () => onRenameSession && onRenameSession(sessionRow) : undefined}>${sessionRow ? (sessionRow.goal || sessionRow.id) : d.navWorkspace}</h2>
              ${sessionRow ? html`<span className=${`tag ${terminalFail ? "red" : done ? "green" : "plain"}`} title=${failReason || ""}><span className=${`dot${live ? " live" : ""}`} style=${{ background: terminalFail ? "var(--red)" : done ? "var(--green)" : "var(--faint)" }}></span>${statusText}</span>` : null}
            </div>
            <div className="meta">${sessionRow ? `${shortPath(sessionRow.workspace, d)} · ${agentType}` : d.workspaceSubtitle}</div>
            ${failReason ? html`<div className="meta" style=${{ color: "var(--red)" }}>${failReason}</div>` : null}
          </div>
          <div style=${{ flex: 1 }}></div>
          ${topControls}
          <div className="autonomy-pill" title=${`${d.autonomy}: ${autonomyName}`}>
            <span className="label">${d.autonomy}</span>
            <div className="autonomy-bars">${[0, 1, 2, 3].map((i) => html`<span key=${i} className=${i < onBars ? "on" : ""}></span>`)}</div>
            <span className="lvl">L${autonomy}</span>
            <span className="name">${autonomyName}</span>
          </div>
          <button className="btn" onClick=${onBriefing}>${d.briefing}</button>
          ${sessionRow && live ? html`<button className="btn danger icon stop-btn" aria-label=${d.cancelSession} title=${d.cancelSession} onClick=${() => onCancelSession(sessionRow.id)}><span className="stop-icon" aria-hidden="true"></span></button>` : null}
          ${sessionRow && terminalFail ? html`<button className="btn primary" onClick=${() => onRetrySession(sessionRow)}>${d.retry}</button>` : null}
          ${sessionRow && !live ? html`<button className="btn" onClick=${() => onDeleteSession(sessionRow.id)}>${d.deleteSession}</button>` : null}
        </div>

        <div className="ws-body">
          <div className="ws-left">
            <div className="thread" ref=${threadRef} onScroll=${onThreadScroll} data-testid="conversation-scroll-container">
              <div className="thread-inner">
                ${!threadNodes.length ? html`<${Empty} icon="◳" text=${d.selectSessionHint} />` :
                  threadNodes.map((n) => html`<${ThreadNode} key=${n.id} n=${n} dig=${dig} d=${d} lang=${lang} openCalls=${openCalls} toggleCall=${toggleCall} onCard=${onCard} onApproval=${onApproval} openDetail=${openDetail} onCopy=${onCopy} />`)}
              </div>
            </div>
            <${Composer} ...${composer} d=${d} lang=${lang} events=${events} compacting=${compacting} runCompact=${runCompact} compactStatus=${compactStatus} sessionRow=${sessionRow} />
          </div>

          <aside className="ws-right desktop">
            <div className="rp-head">
              <div className="ic">🤖</div>
              <div style=${{ minWidth: 0 }}>
                <div className="nm">${agentType}</div>
                <div className="meta">${dig.subagents.length} ${d.subAgentsWord} · ${dig.terminal.length} cmd</div>
              </div>
              ${live ? html`<span className="rp-live"><span className="dot live" style=${{ background: "var(--green)" }}></span>${d.live}</span>` : null}
            </div>
            <div className="rp-tabs">
              <button className=${`rp-tab${rightTab === "todo" ? " on" : ""}`} onClick=${() => setRightTab("todo")}>${d.tabTodos} <span style=${{ opacity: 0.7 }}>${dig.todos.length}</span></button>
              <button className=${`rp-tab${rightTab === "sub" ? " on" : ""}`} onClick=${() => setRightTab("sub")}>${d.tabSubagents} <span style=${{ opacity: 0.7 }}>${dig.subagents.length}</span></button>
              <button className=${`rp-tab${rightTab === "term" ? " on" : ""}`} onClick=${() => setRightTab("term")}>${d.tabTerminal}</button>
              <button
                className=${`rp-tab${rightTab === "ctx" ? " on" : ""}`}
                data-testid="context-tab"
                onClick=${() => setRightTab("ctx")}
              >${d.context}</button>
            </div>
            <div className="rp-body">
              ${rightTab === "todo" ? html`<${TodoPanel} key=${sessionRow ? sessionRow.id : "none"} d=${d} todos=${dig.todos} onAddStep=${composer.onAddStep} />` : null}
              ${rightTab === "sub" ? html`<${SubPanel} d=${d} subagents=${dig.subagents} expandedSub=${expandedSub} toggleSub=${toggleSub} />` : null}
              ${rightTab === "term" ? html`<${TermPanel} d=${d} terminal=${dig.terminal} agentType=${agentType} sessionRow=${sessionRow} onCancelSession=${onCancelSession} />` : null}
              ${rightTab === "ctx" && ContextPanel ? html`<${ContextPanel} d=${d} lang=${lang} sessionRow=${sessionRow} />` : null}
              ${rightTab === "ctx" && !ContextPanel ? html`<div className="alert error">Context panel failed to load.</div>` : null}
            </div>
          </aside>
        </div>
      </div>`;
  }

  function BubbleCopy({ text, d, onCopy, inverted = false }) {
    const copyText = String(text || "");
    if (!copyText.trim() || !onCopy) return null;
    return html`<div className="bubble-actions">
      <button type="button" className=${`bubble-copy${inverted ? " invert" : ""}`} aria-label=${d.copy} title=${d.copy} onClick=${(ev) => { ev.stopPropagation(); onCopy(copyText); }}>⧉</button>
    </div>`;
  }

  function ThinkingPanel({ d, text }) {
    const [open, setOpen] = useState(false);
    const parts = pmThinkingParts(text, d.thinkingTrace);
    return html`<div className=${`pm-thinking${open ? " open" : ""}`}>
      <button type="button" className="pm-thinking-head" aria-expanded=${open} onClick=${() => setOpen((v) => !v)}>
        <span className="pm-thinking-icon" aria-hidden="true">▸</span>
        <span className="pm-thinking-title">${parts.title}</span>
      </button>
      ${open && parts.body ? html`<div className="pm-thinking-body"><${MD} text=${parts.body} maxChars=${4000} /></div>` : null}
    </div>`;
  }

  function PmActivity({ n, d }) {
    const meta = STEP_META[n.stepKind] || STEP_META.tool;
    const active = n.status === "active";
    const failed = n.status === "failed";
    return html`<details className=${`pm-activity ${meta.cls}${active ? " active" : ""}${failed ? " failed" : ""}`}>
      <summary>
        <span className="pm-activity-icon" aria-hidden="true">▸</span>
        <span className="step-chip">${d[meta.k]}</span>
        <span className="pm-activity-title">${n.title}</span>
        ${active ? html`<span className="step-spin"></span>` : failed ? html`<span className="step-x">!</span>` : null}
      </summary>
      ${n.detail ? html`<pre className="pm-activity-body">${n.detail}</pre>` : null}
    </details>`;
  }

  function ContextPackPanel({ n, d, lang }) {
    const status = String(n.status || n.label || "").toLowerCase();
    return html`<details className="context-pack" data-testid="timeline-context-compaction">
      <summary>
        <span className="context-pack-icon" aria-hidden="true">▸</span>
        <span className="context-pack-title">${n.label || d.compactDone}</span>
        ${status.includes("started") ? html`<span data-testid="timeline-context-compaction-started"></span>` : null}
        ${status.includes("failed") ? html`<span data-testid="timeline-context-compaction-failed"></span>` : null}
        ${status.includes("completed") || status.includes("compact") ? html`<span data-testid="timeline-context-compaction-completed"></span>` : null}
        ${n.afterTokens ? html`<span className="context-pack-stat">≈${tokenK(n.afterTokens)}</span>` : null}
        ${n.summaryChars ? html`<span className="context-pack-stat">${n.summaryChars} chars</span>` : null}
        <span className="context-pack-time">${formatTime(n.ts, lang)}</span>
      </summary>
      ${n.preview ? html`<div className="context-pack-preview">${n.preview}</div>` : null}
      <pre className="context-pack-json"><code>${n.json || ""}</code></pre>
    </details>`;
  }

  function ThreadNode({ n, dig, d, lang, openCalls, toggleCall, onCard, onApproval, openDetail, onCopy }) {
    if (n.kind === "user") {
      return html`<div className="bubble-user"><div className="body">
        ${n.goal}
        ${n.chips.length ? html`<div className="chips">${n.chips.map((c, i) => html`<span className="chip" key=${i}>${c}</span>`)}</div>` : null}
        <${BubbleCopy} text=${n.goal} d=${d} onCopy=${onCopy} inverted=${true} />
      </div></div>`;
    }
    if (n.kind === "plan") {
      const notes = Array.isArray(n.deliberation) ? n.deliberation.filter(Boolean) : [];
      return html`<div className="plan-card">
        <div className="plan-head">
          <span className="badge">PM</span><span className="ttl">${d.plan}</span>
          <span className="meta">${n.steps.length} ${lang === "zh" ? "步" : "steps"}</span>
        </div>
        <div className="plan-body">
          ${n.summary ? html`<div className="plan-summary"><${MD} text=${n.summary} maxChars=${1200} /></div>` : null}
          ${notes.length ? html`<div className="plan-notes">${notes.map((x, i) => html`<div key=${i}>${x}</div>`)}</div>` : null}
          ${n.steps.length ? html`
          ${n.steps.map((s, i) => html`<div className="plan-step" key=${i}><span className="num">${i + 1}</span><span className="txt">${s}</span></div>`)}
          ` : null}
        </div>
      </div>`;
    }
    if (n.kind === "pm-status") {
      return html`<div className="pm-status"><span className="spin"></span><span>${n.text}</span>${n.started ? html`<${PmElapsed} start=${n.started} lang=${lang} />` : null}</div>`;
    }
    if (n.kind === "pm-review") {
      const detail = [n.summary, n.reason, n.followUp ? `→ ${n.followUp}` : ""].filter(Boolean).join("\n\n");
      return html`<details className=${`pm-review${n.done ? " done" : ""}`}>
        <summary><span>${d.pmReviewDiag}</span><span className="pm-review-status">${n.status}</span></summary>
        ${detail ? html`<div className="pm-review-body"><${MD} text=${detail} maxChars=${2400} /></div>` : null}
      </details>`;
    }
    if (n.kind === "pm") {
      return html`<div className="pm-note"><div className="pm-avatar">PM</div><div className="body"><${MD} text=${n.text} maxChars=${4000} /><${BubbleCopy} text=${n.text} d=${d} onCopy=${onCopy} /></div></div>`;
    }
    if (n.kind === "pm-thinking") {
      return html`<${ThinkingPanel} d=${d} text=${n.text} />`;
    }
    if (n.kind === "pm-activity") {
      return html`<${PmActivity} n=${n} d=${d} />`;
    }
    if (n.kind === "context-pack") {
      return html`<${ContextPackPanel} n=${n} d=${d} lang=${lang} />`;
    }
    if (n.kind === "call") {
      const c = dig.calls.get(n.callId);
      if (!c) return null;
      return html`<${CallCard} c=${c} d=${d} lang=${lang} open=${!!openCalls[c.id]} onToggle=${toggleCall} />`;
    }
    if (n.kind === "card") {
      const p = n.payload || {};
      const opts = Array.isArray(p.options) ? p.options : [];
      const isQuestion = !p.action_id;
      return html`<div className="dcard">
        <div className="dcard-head"><span>${isQuestion ? "PM" : "⚠️"}</span><span className="ttl">${isQuestion ? "PM question" : d.decisionNeeded}</span>${isQuestion ? null : html`<span className="risk tag amber">${d.riskMedium}</span>`}</div>
        <div className="dcard-body">
          <div className="q"><${MD} text=${p.summary || ""} className="markdown-compact" /></div>
          ${p.audit_note ? html`<div className="d"><${MD} text=${p.audit_note} className="markdown-compact" /></div>` : null}
          <div className="dcard-actions">
            ${opts.map((o, i) => html`<button key=${i} className=${`btn${i === 0 ? " primary" : ""}`} onClick=${() => onCard(n.cardId, o.action)}>${o.label || o.action}</button>`)}
            ${p.action_id ? html`<button className="btn ghost" onClick=${() => openDetail(p.action_id)}>${d.showDiff}</button>` : null}
          </div>
        </div>
      </div>`;
    }
    if (n.kind === "approval") {
      const p = n.payload || {};
      return html`<div className=${`appr${(p.risk_level || "").includes("medium") ? " amber" : ""}`}>
        <span className="ava" style=${{ background: "var(--accent)" }}>${(p.agent || "C").slice(0, 1).toUpperCase()}</span>
        <div className="mid">
          <div style=${{ fontSize: 13, fontWeight: 600 }}>${lang === "zh" ? "想执行命令" : "wants to run"}</div>
          <code>${p.action || p.diff_summary || ""}</code>
        </div>
        <span className="tag red">${p.risk_level || d.riskHigh}</span>
        <div style=${{ display: "flex", gap: 8 }}>
          <button className="btn success sm" onClick=${() => onApproval(n.approvalId, "approve", p.nonce)}>${d.approve}</button>
          <button className="btn sm" onClick=${() => onApproval(n.approvalId, "reject", p.nonce)}>${d.reject}</button>
        </div>
      </div>`;
    }
    if (n.kind === "system") {
      return html`<div className="thread-divider"><div className="line"></div>${n.label}${n.text ? ` · ${String(n.text).slice(0, 80)}` : ""} · ${formatTime(n.ts, lang)}<div className="line"></div></div>`;
    }
    return null;
  }

  function TodoPanel({ d, todos, onAddStep }) {
    const [val, setVal] = useState("");
    // Reveal todos one-by-one as the count grows (e.g. an 8-item plan lands at once): they cascade
    // in instead of dumping, so the plan visibly "fills up" (T2.1). A panel that already mounts with
    // its todos full (reopened session) snaps to all-revealed — no replayed animation.
    const [revealed, setRevealed] = useState(todos.length);
    useEffect(() => {
      if (todos.length <= revealed) { setRevealed(todos.length); return; }
      const id = setInterval(() => setRevealed((r) => {
        const next = r + 1;
        if (next >= todos.length) clearInterval(id);
        return Math.min(next, todos.length);
      }), 240);
      return () => clearInterval(id);
    }, [todos.length]);
    const shown = todos.slice(0, revealed);
    const doneCount = todos.filter((t) => t.status === "done").length;
    const pct = todos.length ? Math.round((doneCount / todos.length) * 100) : 0;
    const submit = () => { const v = val.trim(); if (!v) return; onAddStep(v); setVal(""); };
    return html`<div>
      ${todos.length ? html`<div className="todo-progress"><div className="track"><span style=${{ width: `${pct}%` }}></span></div><span className="lbl">${doneCount}/${todos.length}</span></div>` : null}
      ${!todos.length ? html`<${Empty} icon="☑" text=${d.selectSessionHint} /> ` :
        shown.map((t) => html`<div className=${`todo-row ${t.status}`} key=${t.id}>
          <span className=${`todo-ic ${t.status}`}>${t.status === "done" ? "✓" : t.status === "blocked" ? "!" : ""}</span>
          <div style=${{ flex: 1, minWidth: 0 }}><div className="todo-title">${t.title}</div></div>
        </div>`)}
      <div className="todo-add">
        <input className="input" value=${val} onChange=${(e) => setVal(e.target.value)} onKeyDown=${(e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } }} placeholder=${d.addStep} />
        <button className="btn primary icon" onClick=${submit}>+</button>
      </div>
      <div className="todo-hint"><span style=${{ opacity: 0.7 }}>💡</span><span>${d.todoHint}</span></div>
    </div>`;
  }

  function SubPanel({ d, subagents, expandedSub, toggleSub }) {
    if (!subagents.length) return html`<${Empty} icon="⑂" text=${d.selectSessionHint} />`;
    const running = subagents.filter((s) => s.status === "active").length;
    const done = subagents.filter((s) => s.status === "done").length;
    return html`<div>
      <div className="sub-summary"><span className="dot live" style=${{ background: "var(--accent)" }}></span>${running} ${d.running} · ${done} ${d.done}</div>
      ${subagents.map((s) => {
        const open = expandedSub === s.id;
        return html`<div className=${`sub-card${open ? " open" : ""}`} key=${s.id}>
          <div className="sub-card-head" onClick=${() => toggleSub(s.id)}>
            <span className=${`sub-ic ${s.status}`}>${s.status === "done" ? "✓" : s.status === "queued" ? "◷" : ""}</span>
            <div style=${{ flex: 1, minWidth: 0 }}><div className="sub-name">${s.name}</div><div className="sub-act">${s.act}</div></div>
            <span className="sub-agent">${s.agent}</span>
            <span className="faint" style=${{ fontSize: 11 }}>${open ? "▾" : "▸"}</span>
          </div>
          ${open && s.detail ? html`<div className="sub-detail">${s.detail.slice(0, 1500)}</div>` : null}
        </div>`;
      })}
    </div>`;
  }

  function TermPanel({ d, terminal, agentType, sessionRow, onCancelSession }) {
    const lines = terminal.slice(-200);
    const [input, setInput] = useState("");
    const [echo, setEcho] = useState("");
    const inputRef = useRef(null);
    const canInterrupt = !!(sessionRow && sessionRow.id && onCancelSession);
    const prefix = (l) => [l.agent, l.cwd ? shortPath(l.cwd, d) : ""].filter(Boolean).join(" ");
    const interrupt = () => {
      if (!canInterrupt) return;
      setEcho("^C");
      setInput("");
      onCancelSession(sessionRow.id);
    };
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && String(e.key || "").toLowerCase() === "c") {
        e.preventDefault();
        interrupt();
      } else if (e.key === "Enter") {
        e.preventDefault();
        setEcho(input ? `$ ${input}` : "");
        setInput("");
      }
    };
    return html`<div className="term-full" tabIndex="0" onClick=${() => inputRef.current && inputRef.current.focus()} onKeyDown=${onKey}>
      <div className="bar"><span className="lbl">${d.readOnlyLog} · ${shortPath(sessionRow && sessionRow.workspace, d)} · ${agentType}</span></div>
      <div className="lines">
        ${!lines.length ? html`<div className="cmd-dim">${d.selectSessionHint}</div>` :
          lines.map((l, i) => html`<div key=${i} className=${l.kind === "err" ? "cmd-err" : l.kind === "out" ? "cmd-dim" : ""}>
            ${l.kind === "cmd" ? html`<span>${prefix(l) ? html`<span className="cmd-src">${prefix(l)}</span> ` : null}<span className="cmd-prompt">$</span> ${l.text}</span>` : html`<span>${prefix(l) ? html`<span className="cmd-src">${prefix(l)}</span> ` : null}${l.text}</span>`}
          </div>`)}
        <div className="cmd-note">›<span className="term-cursor"></span></div>
      </div>
      ${echo ? html`<div className="term-echo">${echo}</div>` : null}
      <div className="term-input-row"><span className="cmd-prompt">$</span><input ref=${inputRef} className="term-input" value=${input} onInput=${(e) => setInput(e.target.value)} onKeyDown=${onKey} aria-label="terminal input" spellCheck=${false} /><span className="term-cursor"></span></div>
    </div>`;
  }

  function WorkspaceGitStatus({ d, workspace, hasSession }) {
    const [info, setInfo] = useState(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [refresh, setRefresh] = useState(0);
    useEffect(() => {
      let cancelled = false;
      setError("");
      if (!workspace || !hasSession) { setInfo(null); return () => { cancelled = true; }; }
      api(`/api/workspaces/git-status?path=${encodeURIComponent(workspace)}`)
        .then((data) => { if (!cancelled) setInfo(data || null); })
        .catch(() => { if (!cancelled) setInfo(null); });
      return () => { cancelled = true; };
    }, [workspace, hasSession, refresh]);
    const initRepo = async () => {
      if (!workspace || busy) return;
      setBusy(true); setError("");
      try {
        setInfo(await api("/api/workspaces/init-git", { method: "POST", body: { path: workspace } }));
        setRefresh((n) => n + 1);
      } catch (e) {
        setError(friendlyError(e, d) || d.gitInitFailed);
      } finally {
        setBusy(false);
      }
    };
    const switchBranch = async (event) => {
      const branch = event.target.value;
      if (!workspace || !branch || busy || (info && branch === info.branch && !info.detached)) return;
      setBusy(true); setError("");
      try {
        setInfo(await api("/api/workspaces/checkout-branch", { method: "POST", body: { path: workspace, branch } }));
        setRefresh((n) => n + 1);
      } catch (e) {
        setError(friendlyError(e, d) || d.branchSwitchFailed);
      } finally {
        setBusy(false);
      }
    };
    if (!workspace) return null;
    if (!hasSession) {
      return html`<div className="workspace-status">
        <span className="workspace-status-label">${d.workspaceLabel}</span>
        <span className="workspace-status-path mono" title=${workspace}>${shortPath(workspace, d)}</span>
        <span className="workspace-status-chip mono">${d.workspaceNoWorktree}</span>
      </div>`;
    }
    if (!info || !info.git_available) return null;
    const branch = info.branch ? `${d.workspaceBranch}: ${info.detached ? `${d.workspaceDetached} ${info.branch}` : info.branch}` : "";
    const branches = Array.isArray(info.branches) ? info.branches : [];
    const selectedBranch = !info.detached && branches.includes(info.branch) ? info.branch : "";
    return html`<div className="workspace-status">
      <span className="workspace-status-label">${d.workspaceLabel}</span>
      <span className="workspace-status-path mono" title=${workspace}>${shortPath(workspace, d)}</span>
      ${info.is_git_repo ? html`<span className="workspace-status-chip mono" title=${workspace}>${d.workspaceWorktree}: ${shortPath(workspace, d)}</span>` : null}
      ${info.is_git_repo && branches.length ? html`<label className="workspace-branch-select mono">
        <span>${d.workspaceBranch}</span>
        <select value=${selectedBranch} onChange=${switchBranch} disabled=${busy}>
          ${selectedBranch ? null : html`<option value="">${info.detached && info.branch ? `${d.workspaceDetached} ${info.branch}` : "-"}</option>`}
          ${branches.map((name) => html`<option key=${name} value=${name}>${name}</option>`)}
        </select>
      </label>` : info.is_git_repo && branch ? html`<span className="workspace-status-chip mono">${branch}</span>` : null}
      ${!info.is_git_repo && info.can_init ? html`<button type="button" className="btn ghost sm" onClick=${initRepo} disabled=${busy}>${busy ? d.initGitRepoBusy : d.initGitRepo}</button>` : null}
      ${error ? html`<span className="workspace-status-error">${error}</span>` : null}
    </div>`;
  }

  function Composer(props) {
    const { d, lang, workspaces, workspace, setWorkspace, task, setTask, model, setModel, modelOptions, llm, effort, setEffort,
      attachments, addAttach, addPastedImages, removeAttach, dispatching, runDispatch, dispatchStatus, sessionRow, events,
      compacting, runCompact, compactStatus, processes, selectedProcessId, setSelectedProcessId, teamMode,
      definitions, selectedWorkModeIds, setSelectedWorkModeIds, onCancelSession } = props;
    const wsOpts = workspaces.length ? workspaces : [];
    const effectiveWorkspace = effectiveSessionWorkspace(sessionRow, workspace);
    const wsSelectOptions = wsOpts.some((w) => w.path === effectiveWorkspace) || !effectiveWorkspace
      ? wsOpts
      : [{ path: effectiveWorkspace, name: shortPath(effectiveWorkspace, d) }, ...wsOpts];
    const procOpts = processes || [];
    const [wmOpen, setWmOpen] = useState(false);
    // Only active definitions are pickable work modes; ignore archived/draft siblings.
    const wmOptions = (definitions || []).filter((x) => x && x.is_active);
    const wmSelected = selectedWorkModeIds || [];
    const toggleWm = (id) => {
      if (!setSelectedWorkModeIds) return;
      setSelectedWorkModeIds(wmSelected.includes(id) ? wmSelected.filter((x) => x !== id) : [...wmSelected, id]);
    };
    const wmDesc = (row) => { try { const m = JSON.parse(row.metadata_json || "{}"); if (m && m.description) return m.description; } catch (e) {} return (row.body || "").slice(0, 80); };
    const contextStats = nextContextStats(sessionRow, events || []);
    const est = contextStats.tokens;
    const contextLimit = contextLimitFor(modelOptions, model, llm && llm.model);
    const pct = contextLimit > 0 ? Math.min(95, Math.round((est / contextLimit) * 100)) : 0;
    const sessionStatus = String((sessionRow && sessionRow.status) || "").toLowerCase().replace(/[\s-]+/g, "_");
    const busy = !!sessionRow && ["planning", "queued", "running", "active", "waiting_approval"].includes(sessionStatus);
    const sendBusy = !!dispatching;
    const modelChoices = [];
    const seenModels = new Set();
    const addModelChoice = (value) => {
      const v = String(value || "").trim();
      if (!v || seenModels.has(v)) return;
      seenModels.add(v);
      modelChoices.push(v);
    };
    (modelOptions || []).forEach((o) => addModelChoice(o && (o.value || o.id)));
    addModelChoice(model);
    const onPaste = (e) => {
      const files = clipboardImageFiles(e);
      if (!files.length) return;
      e.preventDefault();
      addPastedImages(files);
    };
    const onKey = (e) => {
      if (e.key === "@") { e.preventDefault(); addAttach(); return; }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!busy && !sendBusy) runDispatch(); }
    };
    return html`<div className="composer">
      <div className="composer-inner">
        ${(sessionRow || (events && events.length)) ? html`<div className="ctx-meter">
          <span>${d.context}</span>
          <div className="track"><span style=${{ width: `${pct}%` }}></span></div>
          <span>≈${tokenK(est)} / ${tokenK(contextLimit)}</span>
          <button className="btn ghost sm" style=${{ marginLeft: "auto" }} onClick=${runCompact} disabled=${compacting || !sessionRow}>⟲ ${compacting ? d.compacting : d.compact}</button>
        </div>` : null}
        ${compactStatus ? html`<div className=${`alert ${compactStatus.includes(d.compactFailed) ? "error" : "info"}`} style=${{ marginBottom: 9 }}>${compactStatus}</div>` : null}
        ${dispatchStatus ? html`<div className=${`alert ${dispatchStatus.includes(d.dispatchFailed) ? "error" : "ok"}`} style=${{ marginBottom: 9 }}>${dispatchStatus}</div>` : null}
        <${WorkspaceGitStatus} d=${d} workspace=${effectiveWorkspace} hasSession=${!!sessionRow} />
        <div className="composer-box">
          ${attachments.length ? html`<div className="composer-attach">${attachments.map((a) => html`<div className="attach-chip" key=${a.id}><span className=${`ic ${a.isImage ? "img" : "file"}`}>${a.isImage ? "🖼" : "📄"}</span><span className="nm">${a.name}</span><span className="rm" onClick=${() => removeAttach(a.id)}>×</span></div>`)}</div>` : null}
          <textarea className="composer-input" data-testid="message-composer" rows="2" value=${task} onChange=${(e) => setTask(e.target.value)} onKeyDown=${onKey} onPaste=${onPaste} placeholder=${d.composerPlaceholder}></textarea>
          <div className="composer-tools">
            <button className="tool-chip" onClick=${addAttach}>📎 ${d.attach}</button>
            ${teamMode ? html`<select className="ws-select machine-select" value=${selectedProcessId || ""} onChange=${(e) => setSelectedProcessId(e.target.value)}>
              <option value="">${d.machine}</option>
              ${procOpts.map((p) => html`<option key=${p.id} value=${p.id} disabled=${!p.online}>${p.online ? "●" : "○"} ${p.name || p.id}</option>`)}
            </select>` : null}
            ${wsSelectOptions.length ? html`<select className="ws-select" value=${effectiveWorkspace} onChange=${(e) => setWorkspace(e.target.value)} disabled=${!!sessionRow}>${wsSelectOptions.map((w) => html`<option key=${w.path} value=${w.path}>📁 ${w.name || shortPath(w.path, d)}</option>`)}</select>` : null}
            <select className="ws-select model-pick" value=${model} onChange=${(e) => setModel(e.target.value)} aria-label=${d.model}>
              <option value="">${d.modelPlaceholder}</option>
              ${modelChoices.map((value) => html`<option key=${value} value=${value}>${value}</option>`)}
            </select>
            ${wmOptions.length ? html`<div style=${{ position: "relative" }}>
              <button className=${`tool-chip${wmSelected.length ? " on" : ""}`} onClick=${() => setWmOpen(!wmOpen)} title=${d.workModePick}>🧩 ${d.workMode}${wmSelected.length ? ` (${wmSelected.length})` : ""}</button>
              ${wmOpen ? html`<div className="wm-pop" style=${{ position: "absolute", bottom: "calc(100% + 6px)", left: 0, zIndex: 30, minWidth: 240, maxWidth: 340, maxHeight: 260, overflow: "auto", background: "var(--surface, #fff)", border: "1px solid var(--border, #ddd)", borderRadius: 10, boxShadow: "0 8px 24px rgba(0,0,0,0.16)", padding: 8 }}>
                <div style=${{ fontSize: 11, opacity: 0.7, padding: "2px 6px 6px" }}>${wmSelected.length ? d.workModePick : d.workModeAuto}</div>
                ${wmOptions.map((row) => html`<label key=${row.id} style=${{ display: "flex", gap: 8, alignItems: "flex-start", padding: "5px 6px", cursor: "pointer", borderRadius: 6 }}>
                  <input type="checkbox" checked=${wmSelected.includes(row.id)} onChange=${() => toggleWm(row.id)} />
                  <span style=${{ minWidth: 0 }}><span style=${{ display: "block", fontSize: 12, fontWeight: 600 }}>${d[KIND_LABEL[row.kind]] || row.kind} · ${row.name}</span><span style=${{ display: "block", fontSize: 11, opacity: 0.7, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>${wmDesc(row)}</span></span>
                </label>`)}
              </div>` : null}
            </div>` : null}
            <select className="ws-select effort-pick" value=${effort || ""} onChange=${(e) => setEffort(e.target.value)} aria-label=${d.thinkingLevel}>
              <option value="">${d.thinkingLevel}</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
            <div style=${{ flex: 1 }}></div>
            <span className="composer-send-hint">⏎ ${d.sendHint}</span>
            ${busy ? html`
              <span className="busy-chip"><span className="spin"></span>${d.pmThinking}</span>
              <button className="btn danger icon stop-btn" aria-label=${d.cancelSession} title=${d.cancelSession} onClick=${() => onCancelSession && sessionRow && onCancelSession(sessionRow.id)} disabled=${sendBusy || !sessionRow}><span className="stop-icon" aria-hidden="true"></span></button>
              <button className="btn primary" data-testid="send-message" title=${d.queueHelp} onClick=${() => runDispatch("queue")} disabled=${sendBusy || !task.trim()}>${sendBusy ? d.queueing : html`${d.send} ↑`}</button>
            ` : html`<button className="btn primary" data-testid="send-message" onClick=${() => runDispatch()} disabled=${sendBusy}>${sendBusy ? html`<span className="spin"></span>` : null}${d.send} ↑</button>`}
          </div>
        </div>
      </div>
    </div>`;
  }

  // ===========================================================================
  // Decisions
  // ===========================================================================
  function Decisions({ d, lang, cards, approvals, onCard, onApproval, openDetail, onGoSession }) {
    return html`<div className="page-mid">
      <div style=${{ fontSize: 13, fontWeight: 800, margin: "0 0 13px", display: "flex", alignItems: "center", gap: 9 }}>${d.decisionCards}${cards.length ? html`<span className="tag amber">${cards.length}</span>` : null}</div>
      <div style=${{ display: "flex", flexDirection: "column", gap: 14, marginBottom: 30 }}>
        ${!cards.length ? html`<${Empty} icon="◉" text=${d.noDecisions} />` :
          cards.map((c) => {
            const isQuestion = !c.action_id;
            return html`<div className="dcard" key=${c.id}>
            <div className="dcard-head"><span>${isQuestion ? "PM" : "⚠️"}</span><span className="ttl">${isQuestion ? "PM question" : d.decisionNeeded}</span>
              ${c.session_id ? html`<span className="dcard-link" onClick=${() => onGoSession(c.session_id)}>↗ ${d.fromSession}</span>` : null}
              ${isQuestion ? null : html`<span className="risk tag amber">${d.riskMedium}</span>`}</div>
            <div className="dcard-body">
              <div className="q"><${MD} text=${c.summary || ""} className="markdown-compact" /></div>
              ${c.audit_note ? html`<div className="d"><${MD} text=${c.audit_note} className="markdown-compact" /></div>` : null}
              ${c.diff_stat ? html`<div style=${{ marginBottom: 13 }}><span className="tag plain">${c.diff_stat}</span></div>` : null}
              <div className="dcard-actions">
                ${(c.options || []).map((o, i) => html`<button key=${i} className=${`btn${i === 0 ? " primary" : ""}`} onClick=${() => onCard(c.id, o.action)}>${o.label || o.action}</button>`)}
                ${c.action_id ? html`<button className="btn ghost" onClick=${() => openDetail(c.action_id)}>${d.showDiff}</button>` : null}
              </div>
            </div>
          </div>`;
          })}
      </div>
      <div style=${{ fontSize: 13, fontWeight: 800, margin: "0 0 13px", display: "flex", alignItems: "center", gap: 9 }}>${d.approvals}${approvals.length ? html`<span className="tag red">${approvals.length}</span>` : null}</div>
      <div style=${{ display: "flex", flexDirection: "column", gap: 11 }}>
        ${!approvals.length ? html`<${Empty} icon="🛡" text=${d.noApprovals} />` :
          approvals.map((a) => html`<div className=${`appr${(a.risk_level || "").includes("medium") ? " amber" : ""}`} key=${a.id}>
            <span className="ava" style=${{ background: "var(--accent)" }}>${(a.agent || a.agent_type || "C").slice(0, 1).toUpperCase()}</span>
            <div className="mid">
              <div style=${{ fontSize: 13, fontWeight: 600 }}>${lang === "zh" ? "想执行命令" : "wants to run"}</div>
              <code>${a.action || a.diff_summary || ""}</code>
              ${a.session_id ? html`<div className="dcard-link" style=${{ marginTop: 7 }} onClick=${() => onGoSession(a.session_id)}>↗ ${d.fromSession}</div>` : null}
            </div>
            <span className="tag red">${a.risk_level || d.riskHigh}</span>
            <div style=${{ display: "flex", gap: 8 }}>
              <button className="btn success sm" onClick=${() => onApproval(a.id, "approve", a.nonce)}>${d.approve}</button>
              <button className="btn sm" onClick=${() => onApproval(a.id, "reject", a.nonce)}>${d.reject}</button>
            </div>
          </div>`)}
      </div>
    </div>`;
  }

  // ===========================================================================
  // Briefings
  // ===========================================================================
  function Briefings({ d, lang, reports, onCopy, toast }) {
    return html`<div className="page-narrow">
      ${!reports.length ? html`<${Empty} icon="▤" text=${d.noReports} />` :
        html`<div>
          ${reports.map((r, idx) => idx === 0 ? html`<div className="card" key=${r.id} style=${{ padding: 0, marginBottom: 24, overflow: "hidden" }}>
            <div style=${{ display: "flex", alignItems: "center", gap: 9, padding: "13px 18px", borderBottom: "1px solid var(--border)", background: "var(--surface2)" }}>
              <span className="plan-head badge" style=${{ width: 22, height: 22 }}>PM</span>
              <span style=${{ fontSize: 14, fontWeight: 700 }}>${r.title || r.kind || d.briefings}</span>
              <span className="meta mono faint" style=${{ marginLeft: "auto", fontSize: 11 }}>${formatDateTime(r.ts, lang)}</span>
            </div>
            <div style=${{ padding: "18px 20px" }}><${MD} text=${r.body_md || ""} /></div>
            <div style=${{ padding: "11px 18px", borderTop: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 14, fontSize: 11 }} className="faint mono">
              ${r.session_id ? `${d.coversSession}` : ""}
              <span style=${{ marginLeft: "auto", display: "flex", gap: 14 }}>
                <span style=${{ cursor: "pointer", color: "var(--accent-text)", fontWeight: 600 }} onClick=${() => onCopy(r.body_md || "")}>⧉ ${d.copy}</span>
              </span>
            </div>
          </div>` : null)}
          <div style=${{ fontSize: 11, fontWeight: 700, letterSpacing: ".05em", textTransform: "uppercase", marginBottom: 11 }} className="faint">${d.history}</div>
          <div style=${{ display: "flex", flexDirection: "column", gap: 8 }}>
            ${reports.slice(1).map((r) => html`<div key=${r.id} style=${{ display: "flex", alignItems: "center", gap: 12, padding: "12px 15px", border: "1px solid var(--border)", borderRadius: 9, background: "var(--surface)", cursor: "pointer" }} onClick=${() => onCopy(r.body_md || "")}>
              <span style=${{ fontSize: 13, fontWeight: 600, flex: 1 }}>${r.title || r.kind}</span>
              <span className="faint mono" style=${{ fontSize: 11 }}>${formatDateTime(r.ts, lang)}</span>
            </div>`)}
          </div>
        </div>`}
    </div>`;
  }

  // ===========================================================================
  // Playbook
  // ===========================================================================
  function Playbook({ d, lang, definitions, filter, setFilter, onNew, onEdit, onActivate, onDelete, onExport, onImportClick, fileRef, onImport, onStartWorkflow }) {
    const pills = [["", "kindAll"], ["workflow", "kindWorkflows"], ["skill", "kindSkills"], ["code_standard", "kindStandards"], ["qa_rubric", "kindQa"]];
    const rows = filter ? (definitions || []).filter((row) => row.kind === filter) : (definitions || []);
    return html`<div className="page-mid">
      <div className="pb-toolbar">
        ${pills.map(([v, l]) => html`<span key=${v} className=${`pill${filter === v ? " on" : ""}`} onClick=${() => setFilter(v)}>${d[l]}</span>`)}
        <div style=${{ flex: 1 }}></div>
        <button className="btn sm" onClick=${onImportClick}>↑ ${d.importBtn}</button>
        <button className="btn sm" onClick=${onExport}>↓ ${d.exportBtn}</button>
        <button className="btn primary sm" onClick=${onNew}>+ ${d.newBtn}</button>
        <input ref=${fileRef} type="file" accept="application/json,.json" hidden onChange=${onImport} />
      </div>
      ${!rows.length ? html`<${Empty} icon="▦" text=${d.noDefinitions} />` :
        html`<div className="pb-grid">${rows.map((row) => html`<div className="pb-card" key=${row.id}>
          <div className="top">
            <span className=${`tag ${KIND_TAGCOLOR[row.kind] || "plain"}`}>${d[KIND_LABEL[row.kind]] || row.kind}</span>
            <span style=${{ marginLeft: "auto" }} className=${row.is_active ? "state-on" : "state-off"}>${row.is_active ? "●" : "○"} ${row.is_active ? d.on : d.off}</span>
          </div>
          <div className="nm">${row.name}</div>
          <div className="desc"><${MD} text=${(() => { try { const m = JSON.parse(row.metadata_json || "{}"); if (m && m.description) return m.description; } catch (e) {} return (row.body || "").slice(0, 160); })()} className="markdown-compact" /></div>
          <div className="foot">
            <span className="scope">${(() => { try { const o = JSON.parse(row.scope_json || "{}"); return Object.keys(o).length ? JSON.stringify(o) : (lang === "zh" ? "全局" : "global"); } catch (e) { return lang === "zh" ? "全局" : "global"; } })()}</span>
            <span className="acts">
              ${row.kind === "workflow" && row.is_active ? html`<span className="act" onClick=${() => onStartWorkflow(row)}>▶ ${d.startWorkflow}</span>` : null}
              ${!row.is_active ? html`<span className="act" onClick=${() => onActivate(row.id)}>${d.activate}</span>` : null}
              <span onClick=${() => onEdit(row)}>${d.edit}</span>
              <span className="del" onClick=${() => onDelete(row.id)}>${d.del}</span>
            </span>
          </div>
        </div>`)}</div>`}
    </div>`;
  }

  // ===========================================================================
  // Settings
  // ===========================================================================
  function Settings(props) {
    const { d, lang, workspaces, workspaceDraft, setWorkspaceDraft, saveWorkspace, browseFolder, deleteWorkspace, loadWorkspaces,
      agentSettings, setAgentSettings, saveAgentSettings, agentStatus, loadAgentSettings,
      llm, setLlm, pmModelOptions, saveLlm, clearLlmKey, llmStatus,
      pmTools, setPmTools, savePmTools, pmToolsStatus, loadPmTools,
      debugSettings, debugStatus, saveDebug,
      cloud, setCloud, saveCloud, saveRemoteExec, connectCloud, disconnectCloud, clearCloudKey, cloudStatus, cloudAvailable,
      autonomy, saveAutonomy, theme, setTheme, lang2, setLang } = props;
    const updateAgent = (name, patch) => setAgentSettings((rows) => (rows || []).map((r) => (r.name === name ? { ...r, ...patch } : r)));
    const updatePmTools = (patch) => setPmTools((cur) => {
      const next = { ...(cur || {}), ...patch };
      if (Object.prototype.hasOwnProperty.call(patch, "max_rounds")) {
        next.max_rounds = clampPmToolRounds(patch.max_rounds);
      }
      return next;
    });
    const lines = (value) => Array.isArray(value) ? value.join("\n") : "";
    const splitLines = (value) => String(value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
    const pmModelChoices = [];
    const seenPmModels = new Set();
    const addPmModelChoice = (value) => {
      const v = String(value || "").trim();
      if (!v || seenPmModels.has(v)) return;
      seenPmModels.add(v);
      pmModelChoices.push(v);
    };
    (pmModelOptions || []).forEach((o) => addPmModelChoice(o && (o.value || o.id)));
    addPmModelChoice(llm && llm.model);
    const broadWorkspace = (workspaces || []).some((w) => isWideWorkspace(w.path));
    const fullAccessAgent = (agentSettings || []).some((row) => row.enabled && row.full_access !== false);
    const sliderRef = useRef(null);
    const onSlide = (e) => {
      const box = sliderRef.current.getBoundingClientRect();
      const x = Math.max(0, Math.min(1, (e.clientX - box.left) / box.width));
      saveAutonomy(Math.round(x * 3));
    };
    return html`<div className="page-narrow">
      <!-- workspaces -->
      <div className="card">
        <div className="card-title">${d.workspaces}<span className="spacer"></span><button className="btn sm" onClick=${loadWorkspaces}>⟳ ${d.refresh}</button></div>
        ${!workspaces.length ? html`<div className="alert warn" style=${{ marginBottom: 14 }}>⚠ ${d.dispatchNoWorkspace}</div>` :
          workspaces.map((w) => html`<div className="ws-item" key=${w.path}><span className="p">${w.path}</span><span className="state-on">● ${d.connected}</span><span className="del" style=${{ cursor: "pointer", color: "var(--red)", fontSize: 12 }} onClick=${() => deleteWorkspace(w.path)}>${d.remove}</span></div>`)}
        ${broadWorkspace && fullAccessAgent ? html`<div className="alert warn" style=${{ marginBottom: 14 }}>⚠ ${d.workspaceRisk}</div>` : null}
        <div className="row col-2-1" style=${{ marginBottom: 12, marginTop: 4 }}>
          <div className="field"><span className="field-label">${d.projectPath}</span>
            <div style=${{ display: "flex", gap: 8 }}>
              <input className="input mono" value=${workspaceDraft.path} onChange=${(e) => setWorkspaceDraft({ ...workspaceDraft, path: e.target.value })} placeholder=${d.pathHint} />
              <button className="btn" onClick=${browseFolder}>${d.browse}</button>
            </div>
          </div>
          <div className="field"><span className="field-label">${d.displayName}</span><input className="input" value=${workspaceDraft.name} onChange=${(e) => setWorkspaceDraft({ ...workspaceDraft, name: e.target.value })} placeholder="Foreman" /></div>
        </div>
        <button className="btn primary" onClick=${saveWorkspace}>${d.addWorkspace}</button>
      </div>

      <!-- local agents -->
      <div className="card">
        <div className="card-title">${d.localAgents}<span className="spacer"></span><button className="btn sm" onClick=${loadAgentSettings}>⟳ ${d.refresh}</button></div>
        <div className="alert info" style=${{ marginBottom: 14 }}>ⓘ ${d.copilotCliHelp}</div>
        ${(agentSettings || []).map((row) => {
          const statusText = !row.enabled ? d.agentDisabled : (row.ok ? (row.version || "OK") : (row.error === "not_found" ? d.agentNotFound : (row.error || "")));
          return html`<div key=${row.name} style=${{ borderTop: "1px solid var(--border)", padding: "14px 0" }}>
            <div style=${{ display: "flex", alignItems: "center", gap: 9, marginBottom: 10, flexWrap: "wrap" }}>
              <strong>${row.name}</strong>
              <span className=${`tag ${row.ok ? "green" : (row.enabled ? "red" : "plain")}`}>${statusText}</span>
              ${row.resolved_path ? html`<span className="faint mono" style=${{ fontSize: 11 }}>${row.resolved_path}</span>` : null}
            </div>
            <div className="row cols2" style=${{ alignItems: "end" }}>
              <div className="field"><span className="field-label">${d.agentCommand}</span><input className="input mono" value=${row.command || ""} onChange=${(e) => updateAgent(row.name, { command: e.target.value })} /></div>
              <div className="field"><span className="field-label">${d.agentModel}</span><input className="input mono" value=${row.model || ""} onChange=${(e) => updateAgent(row.name, { model: e.target.value })} placeholder=${d.modelDefaultHint} /></div>
            </div>
            <div style=${{ display: "flex", gap: 18, marginTop: 10, alignItems: "center", flexWrap: "wrap" }}>
              <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5 }}>${d.agentEnabled} <${Switch} on=${row.enabled} onChange=${(v) => updateAgent(row.name, { enabled: v })} /></label>
              <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5 }}>${d.agentFullAccess} <${Switch} on=${row.full_access !== false} onChange=${(v) => updateAgent(row.name, { full_access: v })} /></label>
              <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5 }}>${d.agentEffort}
                <select className="select" style=${{ width: 110 }} value=${row.effort || ""} onChange=${(e) => updateAgent(row.name, { effort: e.target.value })}>
                  <option value="">${d.effortDefault}</option><option value="low">${d.fast}</option><option value="medium">${d.std}</option><option value="high">${d.deep}</option>
                </select>
              </label>
            </div>
          </div>`;
        })}
        ${agentStatus ? html`<div className=${`alert ${agentStatus.includes(d.saveFailed) ? "error" : "ok"}`} style=${{ margin: "12px 0" }}>${agentStatus}</div>` : null}
        <button className="btn primary" style=${{ marginTop: 12 }} onClick=${saveAgentSettings}>${d.save}</button>
      </div>

      <!-- PM brain -->
      <div className="card">
        <div className="card-title">${d.pmBrain}</div>
        <div className="card-sub">${d.pmBrainSub}</div>
        <div className="row cols2" style=${{ marginBottom: 13 }}>
          <div className="field"><span className="field-label">${d.provider}</span>
            <select className="select" value=${llm.provider || "openai"} onChange=${(e) => setLlm({ ...llm, provider: e.target.value })}><option value="openai">OpenAI-compatible</option><option value="anthropic">Anthropic</option></select>
          </div>
          <div className="field"><span className="field-label">${d.model}</span>
            <select className="select mono" value=${llm.model || ""} onChange=${(e) => setLlm({ ...llm, model: e.target.value })}>
              <option value="">${d.modelDefaultHint}</option>
              ${pmModelChoices.map((value) => html`<option key=${value} value=${value}>${value}</option>`)}
            </select>
          </div>
        </div>
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.baseUrl}</span><input className="input mono" value=${llm.base_url || ""} onChange=${(e) => setLlm({ ...llm, base_url: e.target.value })} placeholder="https://api.openai.com/v1" /></div>
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.transport}</span>
          <select className="select" value=${llm.transport || "http"} onChange=${(e) => setLlm({ ...llm, transport: e.target.value })}><option value="http">HTTP</option><option value="ws">WS stream</option></select>
        </div>
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.requestTimeout}</span>
          <input className="input mono" type="number" min="30" max="3600" value=${llm.request_timeout_s || 300} onChange=${(e) => setLlm({ ...llm, request_timeout_s: Number(e.target.value) || 300 })} />
          <div className="card-sub" style=${{ marginTop: 6, marginBottom: 0 }}>${d.requestTimeoutHelp}</div>
        </div>
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.contextWindow}</span>
          <input className="input mono" type="number" min="1000" max="2000000" value=${llm.context_window_tokens || 272000} onChange=${(e) => setLlm({ ...llm, context_window_tokens: Number(e.target.value) || 272000 })} />
          <div className="card-sub" style=${{ marginTop: 6, marginBottom: 0 }}>${d.contextWindowHelp}</div>
        </div>
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.reasoningEffort}</span>
          <select className="select" value=${llm.reasoning_effort || ""} onChange=${(e) => setLlm({ ...llm, reasoning_effort: e.target.value })}><option value="">${d.effortDefault}</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="max">max</option></select>
        </div>
        <div className="field" style=${{ marginBottom: 11 }}><span className="field-label">${d.apiKey}</span><input className="input mono" type="password" value=${llm.api_key || ""} onChange=${(e) => setLlm({ ...llm, api_key: e.target.value })} placeholder=${d.pmKeyPlaceholder} autoComplete="off" /></div>
        <div className=${`alert ${llm.api_key_set ? "info" : "warn"}`} style=${{ marginBottom: 14 }}>ⓘ ${llm.api_key_set ? d.pmKeyHint : d.pmKeyMissing}</div>
        ${llmStatus ? html`<div className=${`alert ${llmStatus === d.saved ? "ok" : "error"}`} style=${{ marginBottom: 14 }}>${llmStatus}</div>` : null}
        <div style=${{ display: "flex", gap: 9 }}><button className="btn primary" onClick=${saveLlm}>${d.save}</button><button className="btn danger" onClick=${clearLlmKey}>${d.clearKey}</button></div>
      </div>

      <!-- PM tools -->
      <div className="card">
        <div className="card-title">${d.pmTools}<span className="spacer"></span><button className="btn sm" onClick=${loadPmTools}>⟳ ${d.refresh}</button></div>
        <div className="card-sub">${d.pmToolsSub}</div>
        <div style=${{ display: "flex", gap: 18, flexWrap: "wrap", marginBottom: 14 }}>
          ${[
            ["file_read", d.fileRead],
            ["file_write", d.fileWrite],
            ["shell", d.shellTool],
            ["web_fetch", d.webFetch],
            ["web_search", d.webSearch],
            ["browser", d.browserTool],
          ].map(([key, label]) => html`<label key=${key} style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5 }}>${label} <${Switch} on=${key === "file_read" ? pmTools[key] !== false : !!pmTools[key]} onChange=${(v) => updatePmTools({ [key]: v })} /></label>`)}
        </div>
        <div className="row cols2" style=${{ marginBottom: 13 }}>
          <div className="field"><span className="field-label">${d.allowedOrigins}</span><textarea className="input mono" style=${{ minHeight: 92 }} value=${lines(pmTools.allowed_origins)} onChange=${(e) => updatePmTools({ allowed_origins: splitLines(e.target.value) })}></textarea></div>
        </div>
        <div className="row cols2" style=${{ marginBottom: 13 }}>
          <div className="field"><span className="field-label">${d.provider}</span>
            <select className="select" value=${pmTools.web_search_provider || "duckduckgo"} onChange=${(e) => updatePmTools({ web_search_provider: e.target.value })}><option value="duckduckgo">DuckDuckGo</option><option value="searxng">SearXNG</option></select>
          </div>
          <div className="field"><span className="field-label">${d.searxngUrl}</span><input className="input mono" value=${pmTools.searxng_url || ""} onChange=${(e) => updatePmTools({ searxng_url: e.target.value })} placeholder="https://search.example.com" /></div>
        </div>
        <div style=${{ display: "flex", gap: 18, alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
          <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5 }}>${d.browserHeadless} <${Switch} on=${!!pmTools.browser_headless} onChange=${(v) => updatePmTools({ browser_headless: v })} /></label>
          <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5 }}>${d.maxRounds}<input className="input mono" type="number" min=${PM_TOOLS_MIN_ROUNDS} max=${PM_TOOLS_MAX_ROUNDS} step="1" style=${{ width: 76 }} value=${clampPmToolRounds(pmTools.max_rounds)} onChange=${(e) => updatePmTools({ max_rounds: e.target.value })} /></label>
        </div>
        ${pmToolsStatus ? html`<div className=${`alert ${pmToolsStatus === d.pmToolsSaved ? "ok" : "error"}`} style=${{ marginBottom: 14 }}>${pmToolsStatus}</div>` : null}
        <button className="btn primary" onClick=${savePmTools}>${d.save}</button>
      </div>

      <!-- debug -->
      <div className="card">
        <div className="card-title">${d.debug}</div>
        <div className="card-sub">${d.debugSub}</div>
        <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5, marginBottom: 8 }}>${d.llmTrace} <${Switch} on=${!!(debugSettings && debugSettings.llm_trace)} onChange=${(v) => saveDebug(v)} /></label>
        <div className="alert warn" style=${{ marginBottom: 10 }}>⚠ ${d.llmTraceWarn}</div>
        ${debugStatus ? html`<div className="alert ok" style=${{ marginBottom: 10 }}>${debugStatus}</div>` : null}
      </div>

      <!-- cloud connection -->
      <div className="card">
        <div className="card-title">${d.cloudConn}
          <span className=${cloud.connected ? "tag green" : "tag plain"} style=${{ marginLeft: 4 }}>● ${cloud.connected ? d.connected : d.notConnected}</span>
        </div>
        <div className="card-sub">${d.cloudSub}</div>
        ${!cloudAvailable ? html`<div className="alert warn" style=${{ marginBottom: 14 }}>⚠ ${d.cloudUnavailable}</div>` : null}
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.cloudUrl}</span><input className="input mono" value=${cloud.url || ""} onChange=${(e) => setCloud({ ...cloud, url: e.target.value })} placeholder="wss://foreman.yourteam.dev/relay" disabled=${!cloudAvailable} /></div>
        <div className="field" style=${{ marginBottom: 11 }}><span className="field-label">${d.accessKey}</span><input className="input mono" type="password" value=${cloud.access_key || ""} onChange=${(e) => setCloud({ ...cloud, access_key: e.target.value })} placeholder=${cloud.access_key_set ? "••••••••••••" : "fk_live_…"} disabled=${!cloudAvailable} /></div>
        <div className="alert info" style=${{ marginBottom: 14 }}>ⓘ ${d.accessKeyHint}</div>
        <label className="field" style=${{ display: "flex", alignItems: "flex-start", gap: 8, marginBottom: 6, cursor: cloudAvailable ? "pointer" : "default" }}>
          <input type="checkbox" checked=${!!cloud.remote_execution_enabled} disabled=${!cloudAvailable} onChange=${(e) => saveRemoteExec(e.target.checked)} style=${{ marginTop: 3 }} />
          <span style=${{ fontWeight: 600 }}>${d.remoteExec}</span>
        </label>
        <div className="card-sub" style=${{ marginBottom: 14 }}>${d.remoteExecHelp}</div>
        ${cloudStatus ? html`<div className=${`alert ${cloudStatus.includes(d.connFailed) ? "error" : "ok"}`} style=${{ marginBottom: 14 }}>${cloudStatus}</div>` : null}
        <div style=${{ display: "flex", gap: 9 }}>
          <button className="btn" onClick=${saveCloud} disabled=${!cloudAvailable}>${d.save}</button>
          <button className="btn primary" onClick=${connectCloud} disabled=${!cloudAvailable}>${d.connect}</button>
          <button className="btn" onClick=${disconnectCloud} disabled=${!cloudAvailable}>${d.disconnect}</button>
          ${cloud.access_key_set ? html`<button className="btn danger" onClick=${clearCloudKey} disabled=${!cloudAvailable}>${d.clearKey}</button>` : null}
        </div>
      </div>

      <!-- interface & automation -->
      <div className="card">
        <div className="card-title">${d.interface}</div>
        <div style=${{ fontSize: 12.5, fontWeight: 600, marginBottom: 5 }}>${d.autoExec}</div>
        <div className="card-sub" style=${{ marginBottom: 14 }}>${d.autoExecHelp}</div>
        <div className="slider-wrap" ref=${sliderRef} onClick=${onSlide}>
          <div className="slider-fill" style=${{ width: `${(autonomy / 3) * 100}%` }}></div>
          <div className="slider-knob" style=${{ left: `${(autonomy / 3) * 100}%` }}></div>
        </div>
        <div className="slider-marks">
          <span className=${autonomy === 0 ? "on" : ""}>${d.auto0}</span><span className=${autonomy === 1 ? "on" : ""}>${d.auto1}</span><span className=${autonomy === 2 ? "on" : ""}>${d.auto2}</span><span className=${autonomy === 3 ? "on" : ""}>${d.auto3}</span>
        </div>
        <div className="setting-row"><span className="lbl"><div className="t">${d.theme}</div></span><div className="toggle-group"><button className=${`btn sm${theme === "light" ? " primary" : ""}`} onClick=${() => setTheme("light")}>${d.light}</button><button className=${`btn sm${theme === "dark" ? " primary" : ""}`} onClick=${() => setTheme("dark")}>${d.dark}</button></div></div>
        <div className="setting-row"><span className="lbl"><div className="t">${d.language}</div></span><div className="toggle-group"><button className=${`btn sm${lang2 === "zh" ? " primary" : ""}`} onClick=${() => setLang("zh")}>中文</button><button className=${`btn sm${lang2 === "en" ? " primary" : ""}`} onClick=${() => setLang("en")}>EN</button></div></div>
        <div className="setting-row"><span className="lbl"><div className="t">${d.pushNotif}</div><div className="h">${d.pushNotifSub}</div></span><button className="btn" onClick=${props.onPush}>🔔 ${d.enable}</button></div>
      </div>
    </div>`;
  }

  // ===========================================================================
  // Modals
  // ===========================================================================
  function Modal({ title, onClose, children, footer, wide, closeDisabled }) {
    return html`<div className="modal-mask" onClick=${onClose}>
      <div className=${`modal${wide ? " wide" : ""}`} onClick=${(e) => e.stopPropagation()}>
        <div className="modal-head"><span className="t">${title}</span>${closeDisabled ? null : html`<span className="x" onClick=${onClose}>×</span>`}</div>
        <div className="modal-body">${children}</div>
        ${footer ? html`<div className="modal-foot">${footer}</div>` : null}
      </div>
    </div>`;
  }

  function FileViewerModal({ d, file, onClose }) {
    const name = (file && (file.relative_path || file.name || file.path)) || "";
    return html`<${Modal} title=${`${d.fileViewer}: ${name}`} wide onClose=${onClose} footer=${html`<button className="btn" onClick=${onClose}>${d.back}</button>`}>
      <div className="file-viewer-meta">${name}${file && file.bytes ? ` · ${formatBytes(file.bytes, d)}` : ""}</div>
      <pre className="file-viewer-pre"><code>${(file && file.content) || ""}</code></pre>
    </${Modal}>`;
  }

  function formatBytes(n, d) {
    const bytes = Number(n || 0);
    if (!bytes) return d.updateSizeUnknown;
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${bytes} B`;
  }

  function updatePhaseLabel(d, status, updateError, cancelingUpdate, applying) {
    if (cancelingUpdate) return d.updateCancelling;
    if (updateError) return d.updateFailed;
    if (!applying) return d.appUpdateReady;
    const phase = status && status.phase;
    if (phase === "downloading") return d.updateDownloading;
    if (phase === "swapping") return d.updateSwapping;
    return d.updateStarting;
  }

  function updateChangeLines(item, lang) {
    const primary = item && item[lang === "zh" ? "zh" : "en"];
    const fallback = item && item[lang === "zh" ? "en" : "zh"];
    const value = Array.isArray(primary) && primary.length ? primary : fallback;
    if (Array.isArray(value)) return value.filter(Boolean).map((line) => `- ${line}`);
    return value ? [`- ${value}`] : [];
  }

  function formatUpdateNotes(update, lang) {
    const changes = Array.isArray(update && update.changes) ? update.changes : [];
    if (!changes.length) return (update && update.notes) || "";
    return changes.map((item) => {
      const lines = updateChangeLines(item, lang);
      return [item.version || "", ...lines].filter(Boolean).join("\n");
    }).filter(Boolean).join("\n\n");
  }

  function UpdateModal({ d, lang, update, status, updating, updateError, cancelingUpdate, onApply, onCancel, onClose }) {
    const s = status || {};
    const applying = !!(updating || s.applying);
    const cancelPending = !!(cancelingUpdate || s.cancel_requested);
    const total = Number(s.total || update.size || 0);
    const downloaded = Number(s.downloaded || 0);
    const knownTotal = total > 0;
    const rawPercent = Number(s.percent != null ? s.percent : (knownTotal ? (downloaded / total) * 100 : 0));
    const percent = Math.max(0, Math.min(100, rawPercent || 0));
    const width = knownTotal ? percent : (applying ? 36 : 0);
    const pctText = knownTotal ? `${Math.round(percent)}%` : d.updateSizeUnknown;
    const bytesText = knownTotal ? `${formatBytes(downloaded, d)} / ${formatBytes(total, d)}` : formatBytes(update.size, d);
    const notes = formatUpdateNotes(update, lang);
    return html`<${Modal} title=${`${d.appUpdateReady} · v${update.version}`} onClose=${applying ? undefined : onClose} closeDisabled=${applying} footer=${applying
      ? html`<button className="btn danger" onClick=${onCancel} disabled=${cancelPending}>${cancelPending ? html`<span className="spin"></span>` : null}${d.updateCancel}</button>`
      : [html`<button key="l" className="btn" onClick=${onClose}>${d.later}</button>`, html`<button key="u" className="btn primary" onClick=${onApply}>${d.updateNow}</button>`]}>
      <div className="update-modal-status">
        <div className="update-modal-title">${updatePhaseLabel(d, s, updateError, cancelPending, applying)}</div>
        ${notes ? html`<div className="update-modal-notes">${notes}</div>` : null}
      </div>
      ${(applying || updateError) ? html`<div className="update-progress">
        <div className="update-progress-head"><span>${d.updateDownloadProgress}</span><span className="mono">${pctText}</span></div>
        <div className=${`track${knownTotal ? "" : " indeterminate"}`}><span style=${{ width: `${width}%` }}></span></div>
        <div className="update-bytes mono">${bytesText}</div>
      </div>` : null}
      ${updateError ? html`<div className="alert error">${d.updateFailed}</div>` : null}
    </${Modal}>`;
  }

  function SessionTitleModal({ d, title, saving, error, setTitle, onClose, onSave }) {
    return html`<${Modal} title=${d.editSessionTitle} onClose=${onClose} footer=${[
      html`<button key="c" className="btn" onClick=${onClose}>${d.cancel}</button>`,
      html`<button key="s" className="btn primary" onClick=${onSave} disabled=${saving}>${saving ? html`<span className="spin"></span>` : null}${d.save}</button>`,
    ]}>
      <div className="field">
        <span className="field-label">${d.sessionTitle}</span>
        <input className="input" autoFocus value=${title} maxLength=${300} placeholder=${d.sessionTitleHint}
          onInput=${(e) => setTitle(e.target.value)}
          onKeyDown=${(e) => { if (e.key === "Enter") onSave(); if (e.key === "Escape") onClose(); }} />
      </div>
      ${error ? html`<div className="alert error">${error}</div>` : null}
    </${Modal}>`;
  }

  function DefinitionEditor({ d, draft, setDraft }) {
    const row = draft || {};
    const update = (patch) => setDraft({ ...(draft || {}), ...patch });
    return html`<div>
      <div className="row cols2">
        <div className="field"><span className="field-label">${d.defnKind}</span>
          <select className="select" value=${row.kind || "workflow"} disabled=${!!row.id} onChange=${(e) => update({ kind: e.target.value })}>
            <option value="workflow">${d.kindWorkflow}</option><option value="skill">${d.kindSkill}</option><option value="code_standard">${d.kindStandard}</option><option value="qa_rubric">${d.kindQaOne}</option>
          </select>
        </div>
        <div className="field"><span className="field-label">${d.defnName}</span><input className="input mono" value=${row.name || ""} disabled=${!!row.id} onChange=${(e) => update({ name: e.target.value })} placeholder="add-feature" /></div>
      </div>
      <div className="field"><span className="field-label">${d.defnDescription}</span>
        <textarea className="textarea" rows="3" maxLength=${1024} value=${row.description || ""} onChange=${(e) => update({ description: e.target.value })} placeholder=${d.defnDescriptionHint}></textarea>
      </div>
      <div className="field"><span className="field-label">${d.defnScope}</span><input className="input mono" value=${row.scope_json || "{}"} onChange=${(e) => update({ scope_json: e.target.value, scopeError: "" })} placeholder='{"lang":"py"}' /></div>
      ${row.scopeError ? html`<div className="alert error">${row.scopeError}</div>` : null}
      <div className="field"><span className="field-label">${d.defnBody}</span><textarea className="textarea mono" rows="11" value=${row.body || ""} onChange=${(e) => update({ body: e.target.value })}></textarea></div>
      <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 13 }}><input type="checkbox" checked=${row.activate !== false} onChange=${(e) => update({ activate: e.target.checked })} /> ${d.defnActivate}</label>
    </div>`;
  }

  function DetailModal({ d, lang, detail, onClose }) {
    const files = (detail.diff && detail.diff.files) || [];
    return html`<${Modal} title=${d.stepDetail} wide onClose=${onClose} footer=${html`<button className="btn" onClick=${onClose}>${d.back}</button>`}>
      ${detail.command ? html`<div className="term-block"><span className="cmd-prompt">$</span> ${detail.command}</div>` : null}
      <div className="detail-label">${d.codeDiff}</div>
      ${!files.length ? html`<${Empty} icon="±" text=${(detail.diff && detail.diff.note) || "—"} /> ` :
        html`<div className="diff-view">${files.map((f) => html`<div className="diff-file" key=${f.path}><div className="fhead"><span className="muted">${f.path}</span><span className="stat">+${f.additions || 0} / −${f.deletions || 0}</span></div>${(f.lines || []).map((l, i) => html`<div className=${`diff-line ${l.kind === "add" ? "add" : l.kind === "del" ? "del" : ""}`} key=${i}>${l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}${l.text || ""}</div>`)}</div>`)}</div>`}
    </${Modal}>`;
  }

  function VersionInfo({ d, lang, version, onCheckUpdate, checkingUpdate, updateCheckStatus }) {
    const current = version || d.versionUnavailable;
    const currentTag = current && !String(current).startsWith("v") ? `v${current}` : String(current || "");
    return html`<div className="page-narrow version-page">
      <div className="card version-hero">
        <div className="version-hero-main">
          <div className="card-title">${d.versionCurrent}</div>
          <div className="version-number">${current}</div>
        </div>
        <div className="version-actions">
          <span className="tag green">/health</span>
          <button className="btn primary sm" disabled=${checkingUpdate} onClick=${onCheckUpdate}>${checkingUpdate ? d.versionCheckingUpdate : d.versionCheckUpdate}</button>
          ${updateCheckStatus ? html`<div className="version-check-status">${updateCheckStatus}</div>` : null}
        </div>
      </div>
      <div className="card">
        <div className="card-title">${d.versionHistory}</div>
        <div className="version-history">
          ${VERSION_HISTORY.map((item) => html`<div className=${`version-row ${item.version === currentTag ? "current" : ""}`} key=${item.version}>
            <div className="version-tag mono">${item.version}${item.version === currentTag ? html`<span>${d.versionCurrentTag}</span>` : null}</div>
            <div className="version-copy">${lang === "zh" ? item.zh : item.en}</div>
          </div>`)}
        </div>
      </div>
      <div className="version-meta-grid">
        <div className="card">
          <div className="card-title">${d.versionSource}</div>
          <div className="card-sub">${d.versionSourceText}</div>
          <div className="version-path mono">src/foreman/__init__.py::__version__</div>
        </div>
        <div className="card">
          <div className="card-title">${d.versionMaint}</div>
          <div className="card-sub">${d.versionMaintText}</div>
        </div>
      </div>
    </div>`;
  }

  // ===========================================================================
  // Mobile shell
  // ===========================================================================
  function MobileShell(props) {
    const { d, lang, view, setView, mTab, setMTab, drawerOpen, setDrawerOpen, counts, sessionRow,
      dig, mainProps, versionInfoProps, sessions, selected, onSelect, onNew, onRename } = props;
    const titles = { workspace: sessionRow ? (sessionRow.goal || d.navWorkspace) : d.navWorkspace, decisions: d.navDecisions, briefings: d.navBriefings, rules: d.navRules, settings: d.navSettings, version: d.navVersion };
    const live = sessionRow && (sessionRow.status || "").toLowerCase().match(/run|active/);
    const sessionStatus = String((sessionRow && sessionRow.status) || "").toLowerCase().replace(/[\s-]+/g, "_");
    const busy = !!sessionRow && ["planning", "queued", "running", "active", "waiting_approval"].includes(sessionStatus);
    return html`<div className="mobile">
      <div className="appbar">
        <button className="burger" onClick=${() => setDrawerOpen(true)}>☰</button>
        <div style=${{ flex: 1, minWidth: 0 }}><div className=${`ttl${view === "workspace" && sessionRow ? " editable-title" : ""}`} title=${view === "workspace" && sessionRow ? d.editSessionTitle : ""} onDoubleClick=${view === "workspace" && sessionRow ? () => onRename && onRename(sessionRow) : undefined}>${titles[view]}</div><div className="sub">${view === "workspace" && sessionRow ? `${sessionRow.agent_type || ""}` : ""}</div></div>
        ${view === "workspace" && live ? html`<span className="tag green"><span className="dot live" style=${{ background: "var(--green)" }}></span>LIVE</span>` : null}
      </div>
      ${drawerOpen ? html`<div className="m-drawer-mask" onClick=${() => setDrawerOpen(false)}></div>
        <div className="m-drawer">
          <div className="sb-brand"><div className="name">Foreman</div><div className="sub">${d.productSubtitle}</div></div>
          <${NavList} d=${d} view=${view} onView=${(k) => { setView(k); setDrawerOpen(false); }} counts=${counts} />
          <div className="sb-section" style=${{ marginTop: 18 }}><span>${d.sessions}</span><span className="add" onClick=${() => { onNew(); setDrawerOpen(false); }} title=${d.newSession}>+</span></div>
          <div className="sb-sessions" style=${{ flex: "0 1 auto", maxHeight: "40vh" }}>
            ${!(sessions || []).length ? html`<${Empty} icon="✉" text=${d.noActiveSession} />` :
              sessions.map((s) => html`<${SessionItem} key=${s.id} s=${s} d=${d} lang=${lang} active=${s.id === selected} onClick=${() => { onSelect(s.id); setDrawerOpen(false); }} onRename=${(row) => { onRename && onRename(row); setDrawerOpen(false); }} />`)}
          </div>
          <div className="sb-user" style=${{ marginTop: "auto" }}><div className="avatar">J</div><div><div className="uname">jiang</div><div className="urole">${d.personalMode}</div></div></div>
        </div>` : null}
      <div className="m-body">
        ${view === "workspace" ? html`<${MobileWorkspace} d=${d} lang=${lang} dig=${dig} mTab=${mTab} mainProps=${mainProps} />` : null}
        ${view === "decisions" ? html`<div style=${{ padding: 13 }}><${Decisions} ...${mainProps.decisions} /></div>` : null}
        ${view === "briefings" ? html`<div style=${{ padding: 13 }}>${mainProps.briefingsTop}<${Briefings} ...${mainProps.briefings} /></div>` : null}
        ${view === "rules" ? html`<div style=${{ padding: 13 }}><${Playbook} ...${mainProps.playbook} /></div>` : null}
        ${view === "settings" ? html`<div style=${{ padding: 13 }}><${Settings} ...${mainProps.settings} /></div>` : null}
        ${view === "version" ? html`<div style=${{ padding: 13 }}><${VersionInfo} ...${versionInfoProps} /></div>` : null}
      </div>
      ${view === "workspace" && mTab === "chat" ? html`<div className="m-composer">
        <button className="burger" onClick=${mainProps.composer.addAttach}>📎</button>
        ${mainProps.composer.teamMode ? html`<select className="m-machine" value=${mainProps.composer.selectedProcessId || ""} onChange=${(e) => mainProps.composer.setSelectedProcessId(e.target.value)}>
          <option value="">${d.machine}</option>
          ${(mainProps.composer.processes || []).map((p) => html`<option key=${p.id} value=${p.id} disabled=${!p.online}>${p.online ? "●" : "○"} ${p.name || p.id}</option>`)}
        </select>` : null}
        <div className="box"><input value=${mainProps.composer.task} onChange=${(e) => mainProps.composer.setTask(e.target.value)} onPaste=${(e) => { const files = clipboardImageFiles(e); if (files.length) { e.preventDefault(); mainProps.composer.addPastedImages(files); } }} onKeyDown=${(e) => { if (e.key === "@") { e.preventDefault(); mainProps.composer.addAttach(); return; } if (e.key === "Enter") { e.preventDefault(); if (!busy && !mainProps.composer.dispatching) mainProps.composer.runDispatch(); } }} placeholder=${busy ? d.queueHelp : d.mComposerPlaceholder} /></div>
        ${busy ? html`<button className="btn danger sm icon stop-btn" aria-label=${d.cancelSession} title=${d.cancelSession} onClick=${() => mainProps.composer.onCancelSession && sessionRow && mainProps.composer.onCancelSession(sessionRow.id)} disabled=${mainProps.composer.dispatching || !sessionRow}><span className="stop-icon" aria-hidden="true"></span></button><button className="btn primary sm" onClick=${() => mainProps.composer.runDispatch("queue")} disabled=${mainProps.composer.dispatching || !mainProps.composer.task.trim()}>${mainProps.composer.dispatching ? d.queueing : d.send}</button>` : html`<button className="btn primary icon" onClick=${() => mainProps.composer.runDispatch()} disabled=${mainProps.composer.dispatching}>${mainProps.composer.dispatching ? html`<span className="spin"></span>` : "↑"}</button>`}
      </div>` : null}
      ${view === "workspace" ? html`<div className="m-bottom">
        <button className=${`m-tab${mTab === "chat" ? " on" : ""}`} onClick=${() => setMTab("chat")}><span className="ic">💬</span>${d.mTabChat}</button>
        <button className=${`m-tab${mTab === "todo" ? " on" : ""}`} onClick=${() => setMTab("todo")}><span className="ic">☑</span>${d.mTabTodo}</button>
        <button className=${`m-tab${mTab === "sub" ? " on" : ""}`} onClick=${() => setMTab("sub")}><span className="ic">⑂</span>${d.mTabSub}</button>
        <button className=${`m-tab${mTab === "term" ? " on" : ""}`} onClick=${() => setMTab("term")}><span className="ic">▸_</span>${d.mTabTerm}</button>
      </div>` : null}
    </div>`;
  }

  function MobileWorkspace({ d, lang, dig, mTab, mainProps }) {
    const sessionRow = mainProps.sessionRow;
    const threadNodes = threadExtras(dig, mainProps.cards, mainProps.approvals, sessionRow);
    const threadRef = useRef(null);
    const lastThreadNodeId = threadNodes.length ? threadNodes[threadNodes.length - 1].id : "";
    const status = String((sessionRow && sessionRow.status) || "").toLowerCase();
    const statusKey = status.replace(/[\s-]+/g, "_");
    const live = sessionRow && ["planning", "queued", "running", "active", "waiting_approval"].includes(statusKey);
    const failed = status.includes("fail") || status.includes("error") || status.includes("stall");
    const cancelled = status.includes("cancel");
    const done = status.includes("done") || status.includes("complete");
    const statusText = live ? d.running : cancelled ? d.cancelled : failed ? d.failed : done ? d.done : ((sessionRow && sessionRow.status) || "");
    useEffect(() => {
      const el = threadRef.current;
      if (!el) return;
      requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
    }, [sessionRow && sessionRow.id, threadNodes.length, lastThreadNodeId, mTab]);
    if (mTab === "chat") return html`<div className="m-workspace">
      ${sessionRow ? html`<div className="m-session-controls">
        <span className=${`tag ${failed ? "red" : done ? "green" : "plain"}`}><span className=${`dot${live ? " live" : ""}`} style=${{ background: failed ? "var(--red)" : done ? "var(--green)" : "var(--faint)" }}></span>${statusText}</span>
        <span className="meta">${shortPath(sessionRow.workspace, d)}</span>
        <span className="spacer"></span>
        ${live ? html`<button className="btn danger sm icon stop-btn" aria-label=${d.cancelSession} title=${d.cancelSession} onClick=${() => mainProps.onCancelSession(sessionRow.id)}><span className="stop-icon" aria-hidden="true"></span></button>` : null}
        ${failed ? html`<button className="btn primary sm" onClick=${() => mainProps.onRetrySession(sessionRow)}>${d.retry}</button>` : null}
        ${!live ? html`<button className="btn sm" onClick=${() => mainProps.onDeleteSession(sessionRow.id)}>${d.deleteSession}</button>` : null}
      </div>` : null}
      <div className="thread" ref=${threadRef} style=${{ padding: 13 }}><div className="thread-inner">
        ${!threadNodes.length ? html`<${Empty} icon="◳" text=${d.selectSessionHint} />` :
          threadNodes.map((n) => html`<${ThreadNode} key=${n.id} n=${n} dig=${dig} d=${d} lang=${lang} openCalls=${mainProps.openCalls} toggleCall=${mainProps.toggleCall} onCard=${mainProps.onCard} onApproval=${mainProps.onApproval} openDetail=${mainProps.openDetail} onCopy=${mainProps.onCopy} />`)}
      </div></div>
    </div>`;
    if (mTab === "todo") return html`<div style=${{ padding: 13 }}><${TodoPanel} key=${mainProps.sessionRow ? mainProps.sessionRow.id : "none"} d=${d} todos=${dig.todos} onAddStep=${mainProps.composer.onAddStep} /></div>`;
    if (mTab === "sub") return html`<div style=${{ padding: 13 }}><${SubPanel} d=${d} subagents=${dig.subagents} expandedSub=${mainProps.expandedSub} toggleSub=${mainProps.toggleSub} /></div>`;
    return html`<div style=${{ padding: 13 }}><${TermPanel} d=${d} terminal=${dig.terminal} agentType=${displayAgent(mainProps.sessionRow && mainProps.sessionRow.agent_type, d)} sessionRow=${mainProps.sessionRow} onCancelSession=${mainProps.onCancelSession} /></div>`;
  }

  // ===========================================================================
  // Shell
  // ===========================================================================
  function Shell({ embedded = false, onBack = null } = {}) {
    const storedLang = localStorage.getItem(LANG_KEY);
    const [lang, setLangState] = useState(detectedUiLang);
    const [languageLoaded, setLanguageLoaded] = useState(Boolean(storedLang));
    const d = I18N[lang];
    const storedTheme = localStorage.getItem(THEME_KEY);
    const [theme, setThemeState] = useState(storedTheme || ((window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light"));
    const [view, setView] = useState("workspace");
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [mTab, setMTab] = useState("chat");
    const [rightTab, setRightTab] = useState("todo");
    const [booted, setBooted] = useState(false);
    const [hidingLaunch, setHidingLaunch] = useState(false);

    const [status, setStatus] = useState({ online: false, version: "" });
    // Version-update detection: remember the version this page loaded with; when /health later
    // reports a different one (a new PR shipped — AGENTS.md §四 bumps __version__ every deploy),
    // surface a refresh prompt instead of silently running stale front-end code.
    const loadedVersionRef = useRef(null);
    const [updateVersion, setUpdateVersion] = useState("");
    // Packaged-exe self-update (便携版一键自更新): /api/update/check reports a newer GitHub Release;
    // "立即更新" POSTs /api/update/apply → the exe downloads, swaps in place and relaunches.
    const [appUpdate, setAppUpdate] = useState(null); // {version, notes, changes} when an update is offered
    const [updating, setUpdating] = useState(false);
    const [updateStatus, setUpdateStatus] = useState(null);
    const [cancelingUpdate, setCancelingUpdate] = useState(false);
    const [updateError, setUpdateError] = useState(false);
    const [checkingUpdate, setCheckingUpdate] = useState(false);
    const [updateCheckStatus, setUpdateCheckStatus] = useState("");
    const [workspaces, setWorkspaces] = useState([]);
    const [agentsLoaded, setAgentsLoaded] = useState(false);
    const [agentSettings, setAgentSettings] = useState([]);
    const [modelOptions, setModelOptions] = useState([]);
    const [pmModelOptions, setPmModelOptions] = useState([]);
    const [sessions, setSessions] = useState([]);
    const [selectedSession, setSelectedSession] = useState("");
    const [events, setEvents] = useState([]);
    const [cards, setCards] = useState([]);
    const [approvals, setApprovals] = useState([]);
    const [reports, setReports] = useState([]);
    const [definitions, setDefinitions] = useState([]);
    const [defnFilter, setDefnFilter] = useState("");
    const [workspace, setWorkspace] = useState(localStorage.getItem(WORKSPACE_KEY) || "");
    const [workspaceDraft, setWorkspaceDraft] = useState({ path: "", name: "" });
    const [task, setTask] = useState("");
    const [model, setModel] = useState("");
    const [effort, setEffort] = useState("medium");
    // Manually-picked work-mode definition ids (D4, UI-first). P0 sends them; the backend accepts but
    // does NOT yet consume them — resolver pass-through wiring lands in P1.
    const [selectedWorkModeIds, setSelectedWorkModeIds] = useState([]);
    const [attachments, setAttachments] = useState([]);
    const [dispatching, setDispatching] = useState(false);
    const [dispatchStatus, setDispatchStatus] = useState("");
    const [compacting, setCompacting] = useState(false);
    const [compactStatus, setCompactStatus] = useState("");
    const [llm, setLlm] = useState({ provider: "openai", model: "", base_url: "", transport: "http", request_timeout_s: 300, context_window_tokens: 272000, reasoning_effort: "", api_key_set: true, api_key: "" });
    const [llmStatus, setLlmStatus] = useState("");
    const [agentStatus, setAgentStatus] = useState("");
    const [pmTools, setPmTools] = useState({ file_read: true, file_write: false, shell: false, web_fetch: false, web_search: false, browser: false, allowed_origins: [], web_search_provider: "duckduckgo", searxng_url: "", browser_headless: false, max_rounds: 6 });
    const [pmToolsStatus, setPmToolsStatus] = useState("");
    const [debugSettings, setDebugSettings] = useState({ llm_trace: false });
    const [debugStatus, setDebugStatus] = useState("");
    const [cloud, setCloud] = useState({ url: "", access_key: "", access_key_set: false, connected: false, remote_execution_enabled: false });
    const [cloudStatus, setCloudStatus] = useState("");
    const [cloudAvailable, setCloudAvailable] = useState(true);
    const [teamMode, setTeamMode] = useState(() => !!getToken());
    const [processes, setProcesses] = useState([]);
    const [selectedProcessId, setSelectedProcessIdState] = useState(localStorage.getItem(PROCESS_KEY) || "");
    const [notifications, setNotifications] = useState([]);
    const [autonomy, setAutonomyState] = useState(1);
    const [detailOpen, setDetailOpen] = useState(false);
    const [detail, setDetail] = useState({ raw: [], diff: { files: [] } });
    const [fileViewer, setFileViewer] = useState(null);
    const [defnOpen, setDefnOpen] = useState(false);
    const [defnDraft, setDefnDraft] = useState(null);
    const [confirmDefnDelete, setConfirmDefnDelete] = useState(null);
    const [wfRun, setWfRun] = useState(null);  // P5: current workflow run view (null = closed)
    const [confirmSessionDelete, setConfirmSessionDelete] = useState(null);
    const [renameSession, setRenameSession] = useState(null);
    const [renameTitle, setRenameTitle] = useState("");
    const [renameError, setRenameError] = useState("");
    const [renamingSession, setRenamingSession] = useState(false);
    const [openCalls, setOpenCalls] = useState({});
    const [expandedSub, setExpandedSub] = useState(null);
    const [toasts, setToasts] = useState([]);
    const accountWsRef = useRef(null);
    const wsRef = useRef(null);
    const selectedSessionRef = useRef("");
    const fileRef = useRef(null);
    const toastSeq = useRef(0);
    const bootStartedRef = useRef(false);

    const toast = useCallback((text, type) => {
      const id = ++toastSeq.current;
      setToasts((p) => [...p, { id, text, type }]);
      setTimeout(() => setToasts((p) => p.filter((t) => t.id !== id)), 3200);
    }, []);
    const notifyError = useCallback((e) => toast(friendlyError(e, I18N[lang]), "error"), [lang, toast]);

    useEffect(() => { document.documentElement.setAttribute("data-theme", theme); }, [theme]);
    const setTheme = (t) => { setThemeState(t); localStorage.setItem(THEME_KEY, t); };
    const setLang = (l) => setLangState(normalizeUiLang(l));
    const setSelectedProcessId = (id) => {
      setSelectedProcessIdState(id || "");
      if (id) localStorage.setItem(PROCESS_KEY, id);
      else localStorage.removeItem(PROCESS_KEY);
    };

    // loaders
    const loadWorkspaces = useCallback(async () => {
      try {
        const rows = await api("/api/workspaces");
        setWorkspaces(rows || []);
        const paths = (rows || []).map((w) => w.path);
        const chosen = paths.includes(localStorage.getItem(WORKSPACE_KEY)) ? localStorage.getItem(WORKSPACE_KEY) : paths[0] || "";
        setWorkspace(chosen); if (chosen) localStorage.setItem(WORKSPACE_KEY, chosen);
      } catch (e) { setWorkspaces([]); }
    }, []);
    const loadAgentSettings = useCallback(async () => { try { setAgentSettings(await api("/api/settings/agents") || []); } catch (e) { setAgentSettings([]); } finally { setAgentsLoaded(true); } }, []);
    const loadPmTools = useCallback(async () => { try { setPmTools(await api("/api/settings/pm-tools") || {}); } catch (e) { /* server mode */ } }, []);
    const loadDebug = useCallback(async () => { try { setDebugSettings(await api("/api/settings/debug") || { llm_trace: false }); } catch (e) { /* server mode */ } }, []);
    const saveDebug = useCallback(async (on) => { try { const r = await api("/api/settings/debug", { method: "POST", body: { llm_trace: !!on } }); setDebugSettings({ llm_trace: !!(r && r.llm_trace) }); setDebugStatus(d.debugSaved); } catch (e) { notifyError(e); } }, [d]);
    const loadModels = useCallback(async () => { try { const data = await api("/api/models"); setModelOptions((data && data.models || []).map((m) => ({ value: m.id, id: m.id, context_length: m.context_length, source: m.source }))); } catch (e) { setModelOptions([]); } }, []);
    const loadPmModels = useCallback(async (draft) => {
      const cur = draft || {};
      const body = { provider: cur.provider || "openai", model: (cur.model || "").trim(), base_url: (cur.base_url || "").trim(), transport: cur.transport || "http", request_timeout_s: Number(cur.request_timeout_s) || 300, context_window_tokens: Number(cur.context_window_tokens) || 272000, reasoning_effort: cur.reasoning_effort || "" };
      if ((cur.api_key || "").trim()) body.api_key = cur.api_key.trim();
      try { const data = await api("/api/models/preview", { method: "POST", body }); setPmModelOptions((data && data.models || []).map((m) => ({ value: m.id, id: m.id, context_length: m.context_length, source: m.source }))); } catch (e) { setPmModelOptions([]); }
    }, []);
    const loadSessions = useCallback(async () => { try { try { setSessions(await api("/api/overview") || []); } catch (e) { setSessions(await api("/api/sessions") || []); } } catch (e) { setSessions([]); } }, []);
    const loadCards = useCallback(async () => { try { setCards(await api("/api/cards") || []); } catch (e) { setCards([]); } }, []);
    const loadApprovals = useCallback(async () => {
      try { const rows = await api("/api/approvals") || []; setApprovals(rows); return rows; }
      catch (e) { setApprovals([]); return []; }
    }, []);
    const loadProcesses = useCallback(async () => {
      try {
        const rows = await api("/api/processes") || [];
        setTeamMode(true);
        setProcesses(rows);
        const ids = rows.map((p) => p.id);
        const online = rows.filter((p) => p.online);
        const currentRow = rows.find((p) => p.id === selectedProcessId) || null;
        const current = currentRow && (currentRow.online || !online.length) ? currentRow.id : "";
        const next = current || ((online[0] && online[0].id) || (rows[0] && rows[0].id) || "");
        if (next && next !== selectedProcessId) setSelectedProcessId(next);
        return next;
      } catch (e) {
        setProcesses([]);
        setTeamMode(true);
        return "";
      }
    }, [selectedProcessId]);
    const applySnapshot = useCallback((snap) => {
      const sessionsNext = (snap && snap.sessions || []).map((s) => ({ id: s.session_id, ...(s.summary || {}), process_id: snap.process_id || "" }));
      const cardsNext = (snap && snap.cards || []).map((c) => ({ id: c.card_id, card_id: c.card_id, status: c.status, ...(c.payload || {}), process_id: snap.process_id || "" }));
      setSessions(sessionsNext);
      setCards(cardsNext);
      setApprovals(snap && snap.approvals || []);
      setReports(snap && snap.reports || []);
      setDefinitions(snap && snap.definitions || []);
      if (snap && Array.isArray(snap.workspaces)) {
        setWorkspaces(snap.workspaces);
        const paths = snap.workspaces.map((w) => w.path);
        const chosen = paths.includes(localStorage.getItem(WORKSPACE_KEY)) ? localStorage.getItem(WORKSPACE_KEY) : paths[0] || "";
        setWorkspace(chosen);
        if (chosen) localStorage.setItem(WORKSPACE_KEY, chosen);
        else localStorage.removeItem(WORKSPACE_KEY);
      }
      if (snap && snap.autonomy && typeof snap.autonomy.level === "number") setAutonomyState(snap.autonomy.level);
      if (snap && snap.agent_settings) { setAgentSettings(snap.agent_settings || []); setAgentsLoaded(true); }
      if (snap && snap.pm_tools) setPmTools(snap.pm_tools || {});
      if (snap && snap.llm) setLlm({ ...snap.llm, api_key: "" });
      if (snap && snap.debug) setDebugSettings({ llm_trace: !!snap.debug.llm_trace });
      if (snap && snap.cloud) {
        const c = snap.cloud;
        setCloud({ url: c.url || "", access_key: "", access_key_set: !!c.access_key_set, connected: !!c.connected, remote_execution_enabled: !!c.remote_execution_enabled });
        setCloudAvailable(c.available !== false);
      }
      if (snap && Array.isArray(snap.events)) {
        const sid = snap.session_id || ((snap.events[0] && snap.events[0].session_id) || "");
        if (!sid || selectedSessionRef.current === sid) setEvents(snap.events);
      }
      if (snap && snap.language && localStorage.getItem(LANG_KEY)) setLangState(normalizeUiLang(snap.language));
    }, []);
    const loadRemoteSnapshot = useCallback(async (processId, sessionId = "") => {
      if (!processId) return;
      const body = { process_id: processId };
      if (sessionId) body.session_id = sessionId;
      try { applySnapshot(await api("/api/snapshot", { method: "POST", body })); }
      catch (e) { notifyError(e); }
    }, [applySnapshot, notifyError]);
    const loadNotifications = useCallback(async () => {
      try { setNotifications(await api("/api/notifications") || []); }
      catch (e) { setNotifications([]); }
    }, []);
    const loadReports = useCallback(async () => { try { setReports(await api("/api/reports") || []); } catch (e) { setReports([]); } }, []);
    const loadDefinitions = useCallback(async () => { try { const path = defnFilter ? `/api/definitions?kind=${encodeURIComponent(defnFilter)}` : "/api/definitions"; setDefinitions(await api(path) || []); } catch (e) { setDefinitions([]); } }, [defnFilter]);
    const loadLlm = useCallback(async () => { try { const next = { ...(await api("/api/settings/llm")), api_key: "" }; setLlm(next); await loadPmModels(next); } catch (e) { /* server mode */ } }, [loadPmModels]);
    const loadAutonomy = useCallback(async () => { try { setAutonomyState((await api("/api/settings/autonomy")).level); } catch (e) { /* keep */ } }, []);
    const loadCloud = useCallback(async (opts = {}) => {
      try {
        const c = await api("/api/settings/cloud");
        // The 8s background poll must NOT clobber the user's in-progress typing: it only refreshes
        // live status (connected / access_key_set), keeping the url + access_key fields as edited.
        // A full reset happens only on explicit loads (boot / after save / connect / clear).
        setCloud((prev) => opts.background
          ? { ...prev, access_key_set: !!c.access_key_set, connected: !!c.connected, remote_execution_enabled: !!c.remote_execution_enabled }
          : { url: c.url || "", access_key: "", access_key_set: !!c.access_key_set, connected: !!c.connected, remote_execution_enabled: !!c.remote_execution_enabled });
        setCloudAvailable(c.available !== false);
      } catch (e) {
        // A transient fetch error must NOT latch the card off: availability is a structural fact
        // (the server's `available` flag), and the only post-boot caller is gated by `cloudAvailable`
        // — so forcing it false here used to permanently stop cloud polling for the whole session
        // after a single network blip. Leave it as-is; the next poll re-reads the real flag.
      }
    }, []);

    // boot
    useEffect(() => {
      if (localStorage.getItem(LANG_KEY)) { setLanguageLoaded(true); return; }
      setLangState(detectedUiLang());
      setLanguageLoaded(true);
    }, []);
    useEffect(() => { document.documentElement.lang = lang === "zh" ? "zh-CN" : "en"; document.title = lang === "zh" ? "Foreman · 控制台" : "Foreman · Console"; if (!languageLoaded) return; localStorage.setItem(LANG_KEY, lang); api("/api/settings/language", { method: "POST", body: { language: lang } }).catch(() => {}); }, [lang, languageLoaded]);

    useEffect(() => {
      if (bootStartedRef.current) return undefined;
      bootStartedRef.current = true;
      let cancelled = false;
      if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
      api("/health").then((h) => { setStatus({ online: true, version: h.version }); if (loadedVersionRef.current == null && h.version) loadedVersionRef.current = h.version; }).catch(() => setStatus({ online: false, version: "offline" }));
      // Boot on the essentials only. Model + agent discovery hit the provider's /models (or run a
      // CLI --version per agent) and can take the backend request timeout if a key is set but the
      // endpoint is slow — keeping them out of this barrier stops the launch overlay from hanging
      // (codex review finding). They populate the Settings page shortly after, non-blocking.
      api("/api/auth/me").then(async () => {
        setTeamMode(true);
        const processId = await loadProcesses();
        await loadNotifications();
        if (processId) await loadRemoteSnapshot(processId);
      }).catch((e) => {
        if (e && e.status === 503) {
          setToken("");
          setTeamMode(false);
          setProcesses([]);
          setSelectedProcessId("");
          return Promise.all([
            loadWorkspaces(),
            loadSessions(),
            loadCards(),
            loadApprovals(),
            loadReports(),
            loadDefinitions(),
            loadAgentSettings(),
            loadPmTools(),
            loadDebug(),
            loadLlm(),
            loadAutonomy(),
            loadCloud(),
          ]);
        }
        if (e && e.status === 401) { redirectToLogin(); return undefined; }
        notifyError(e);
        return undefined;
      }).finally(() => {
        if (cancelled) return;
        setBooted(true);
        setTimeout(() => setHidingLaunch(true), 350);
      });
      return () => { cancelled = true; };
    }, [loadProcesses, loadRemoteSnapshot, loadNotifications, notifyError]);
    useEffect(() => {
      if (!booted) return undefined;
      const t = setTimeout(() => { loadModels(); loadPmModels(llm); }, 0);
      return () => clearTimeout(t);
    }, [booted]);

    // polling for cards/approvals/sessions
    useEffect(() => {
      const id = setInterval(() => {
        if (teamMode) {
          loadProcesses();
          loadNotifications();
          if (selectedProcessId) loadRemoteSnapshot(selectedProcessId);
        } else {
          loadSessions();
          loadCards();
          loadApprovals();
          if (cloudAvailable) loadCloud({ background: true });
        }
      }, 8000);
      return () => clearInterval(id);
    }, [teamMode, selectedProcessId, loadProcesses, loadNotifications, loadRemoteSnapshot, loadSessions, loadCards, loadApprovals, loadCloud, cloudAvailable]);

    // New-version watcher: poll /health; when the reported version differs from the one this page
    // loaded with, a new build was deployed (every PR bumps __version__ — AGENTS.md §四). Offer a
    // refresh rather than auto-reloading, so the user never loses in-flight input.
    useEffect(() => {
      const id = setInterval(() => {
        api("/health").then((h) => {
          const v = h && h.version;
          if (!v || v === "offline") return;
          setStatus((s) => (s.version === v ? s : { ...s, online: true, version: v }));
          if (loadedVersionRef.current == null) { loadedVersionRef.current = v; return; }
          if (v !== loadedVersionRef.current) setUpdateVersion(v);
        }).catch(() => {});
      }, 30000);
      return () => clearInterval(id);
    }, []);

    // Packaged-exe self-update watcher (便携版一键自更新): ask the local server whether a newer
    // GitHub Release exists for THIS exe. Only the frozen exe reports available=true (from source
    // there's nothing to swap). Check shortly after boot, then every 6h. Never auto-applies.
    const checkAppUpdate = useCallback((manual = false) => {
      if (manual) {
        setCheckingUpdate(true);
        setUpdateCheckStatus("");
        setUpdateError(false);
        setUpdateStatus(null);
      }
      return api("/api/update/check").then((u) => {
        if (u && u.available) {
          setAppUpdate({
            version: u.latest,
            current: u.current || "",
            notes: u.notes || "",
            changes: Array.isArray(u.changes) ? u.changes : [],
            size: u.size || 0,
          });
          setUpdateStatus(u);
          if (manual) setUpdateCheckStatus(`${d.appUpdateReady} · v${u.latest}`);
        } else if (manual) {
          setUpdateCheckStatus(d.versionNoUpdate);
        }
        return u;
      }).catch(() => {
        if (manual) setUpdateCheckStatus(d.versionCheckFailed);
        return null;
      }).finally(() => {
        if (manual) setCheckingUpdate(false);
      });
    }, [d]);
    useEffect(() => {
      const t = setTimeout(checkAppUpdate, 5000);
      const id = setInterval(checkAppUpdate, 6 * 60 * 60 * 1000);
      return () => { clearTimeout(t); clearInterval(id); };
    }, [checkAppUpdate]);

    const pollAppUpdateStatus = useCallback(() => {
      return api("/api/update/status").then((s) => {
        setUpdateStatus(s || null);
        if (s && s.phase === "failed") {
          setUpdating(false);
          setUpdateError(true);
        }
        if (s && s.phase === "cancelled") {
          setUpdating(false);
          setCancelingUpdate(false);
          setUpdateError(false);
          setAppUpdate(null);
          setUpdateStatus(null);
        }
        return s;
      }).catch(() => null);
    }, []);

    useEffect(() => {
      const active = updating || !!(updateStatus && updateStatus.applying);
      if (!active) return undefined;
      pollAppUpdateStatus();
      const id = setInterval(pollAppUpdateStatus, 800);
      return () => clearInterval(id);
    }, [updating, updateStatus && updateStatus.applying, pollAppUpdateStatus]);

    const applyAppUpdate = useCallback(() => {
      setUpdateError(false);
      setCancelingUpdate(false);
      setUpdating(true);
      setUpdateStatus((s) => ({ ...(s || {}), applying: true, phase: "starting", version: (appUpdate && appUpdate.version) || "", total: (appUpdate && appUpdate.size) || 0, downloaded: 0 }));
      api("/api/update/apply", { method: "POST" }).then((r) => {
        if (!r || !r.ok) { setUpdating(false); setUpdateError(true); }
        else pollAppUpdateStatus();
        // On success the app goes down and relaunches on the new version — keep the "updating…"
        // dialog up; the page dies with the server, then the new exe serves a fresh page.
      }).catch(() => { setUpdating(false); setUpdateError(true); });
    }, [appUpdate, pollAppUpdateStatus]);

    const cancelAppUpdate = useCallback(() => {
      setCancelingUpdate(true);
      api("/api/update/cancel", { method: "POST" }).then((r) => {
        if (!r || !r.ok) setUpdateError(true);
        return pollAppUpdateStatus();
      }).catch(() => {
        setUpdateError(true);
      }).finally(() => {
        setCancelingUpdate(false);
      });
    }, [pollAppUpdateStatus]);

    useEffect(() => {
      if (teamMode && selectedProcessId) loadRemoteSnapshot(selectedProcessId);
    }, [teamMode, selectedProcessId, loadRemoteSnapshot]);

    useEffect(() => {
      if (!teamMode) return undefined;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const token = getToken();
      const tq = token ? `?token=${encodeURIComponent(token)}` : "";
      const ws = new WebSocket(`${proto}://${location.host}/ws${tq}`);
      accountWsRef.current = ws;
      ws.addEventListener("message", (ev) => {
        try { handleWsItem(JSON.parse(ev.data)); }
        catch (e) {}
      });
      ws.addEventListener("error", () => {});
      return () => {
        try { ws.close(); } catch (e) {}
        if (accountWsRef.current === ws) accountWsRef.current = null;
      };
    }, [teamMode]);

    async function handleNotificationTarget(rawUrl, action) {
      let u;
      try { u = new URL(rawUrl || "/", location.origin); } catch (e) { return; }
      if (u.origin !== location.origin) return;
      const params = u.searchParams;
      const processId = params.get("process") || "";
      const sessionId = params.get("session") || "";
      const approvalId = params.get("approval") || "";
      const decision = action || params.get("action") || "";
      const viewName = params.get("view") || "";
      if (processId) { setTeamMode(true); setSelectedProcessId(processId); await loadRemoteSnapshot(processId); }
      if (approvalId && (decision === "approve" || decision === "reject")) {
        const rows = await loadApprovals();
        const row = rows.find((a) => a.id === approvalId);
        if (row) await decideApproval(row.id, decision, row.nonce);
        return;
      }
      if (sessionId) { openTimeline(sessionId, processId); return; }
      if (viewName === "decisions" || approvalId) { setView("decisions"); return; }
      if (viewName === "workspace") { setView("workspace"); return; }
      if (["briefings", "rules", "settings", "version"].includes(viewName)) { setView(viewName); return; }
    }

    // notification/deep-link handling
    useEffect(() => {
      const params = new URLSearchParams(location.search);
      const hasTarget = ["approval", "action", "view", "session", "process"].some((k) => params.has(k));
      if (!hasTarget) return;
      const rawUrl = `${location.pathname}${location.search}${location.hash}`;
      history.replaceState(null, "", location.pathname);
      handleNotificationTarget(rawUrl, params.get("action") || "");
    }, []); // eslint-disable-line
    useEffect(() => {
      if (!("serviceWorker" in navigator)) return undefined;
      const onMessage = (ev) => {
        const msg = ev.data || {};
        if (msg.type === "notificationclick") handleNotificationTarget(msg.url || "/", msg.action || "");
      };
      navigator.serviceWorker.addEventListener("message", onMessage);
      return () => navigator.serviceWorker.removeEventListener("message", onMessage);
    });

    function openTimeline(sessionId, processIdOverride = "") {
      selectedSessionRef.current = sessionId;
      setSelectedSession(sessionId); setView("workspace"); setEvents([]);
      const processId = processIdOverride || selectedProcessId;
      if (teamMode && processId) loadRemoteSnapshot(processId, sessionId);
      if (wsRef.current) { try { wsRef.current.close(); } catch (e) {} }
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const token = getToken();
      const tq = token ? `&token=${encodeURIComponent(token)}` : "";
      const next = new WebSocket(`${proto}://${location.host}/ws?session_id=${encodeURIComponent(sessionId)}${tq}`);
      next.addEventListener("message", (ev) => {
        try { handleWsItem(JSON.parse(ev.data)); }
        catch (e) {}
      });
      next.addEventListener("error", () => {});
      wsRef.current = next;
    }
    function newSession() { selectedSessionRef.current = ""; setSelectedSession(""); setEvents([]); setDispatchStatus(""); setCompactStatus(""); setView("workspace"); }

    const sessionRow = useMemo(() => sessions.find((s) => s.id === selectedSession), [sessions, selectedSession]);
    const dig = useMemo(() => digest(events, d, lang), [events, d, lang]);
    // Only undecided cards are actionable — a card with `chosen` set is history (it still lives in
    // /api/cards), so it must not keep showing live approve/reject buttons in the thread or count.
    const openCards = useMemo(() => (cards || []).filter((c) => !c.chosen), [cards]);
    const openFileReference = useCallback(async (path) => {
      const targetWorkspace = (sessionRow && sessionRow.workspace) || workspace;
      if (!targetWorkspace) { toast(d.workspaceMissing, "error"); return; }
      try {
        if (isMobileViewport()) {
          const data = await api(`/api/workspace-file/read?workspace=${encodeURIComponent(targetWorkspace)}&path=${encodeURIComponent(path)}`);
          setFileViewer(data);
        } else {
          await api("/api/workspace-file/open", { method: "POST", body: { workspace: targetWorkspace, path } });
          toast(d.fileOpened, "success");
        }
      } catch (e) {
        notifyError(e);
      }
    }, [sessionRow, workspace, d, toast, notifyError]);

    useEffect(() => {
      const onFileRef = (ev) => openFileReference(ev.detail && ev.detail.path);
      window.addEventListener("foreman:file-ref", onFileRef);
      return () => window.removeEventListener("foreman:file-ref", onFileRef);
    }, [openFileReference]);

    function pushEvent(item) {
      if (!item) return;
      const currentSession = selectedSessionRef.current || selectedSession;
      if (currentSession && item.session_id && item.session_id !== currentSession) return;
      setEvents((prev) => {
        if (item.id && prev.some((r) => r.id === item.id)) return prev;
        return [...prev, item];
      });
    }

    function applyRelayFrame(frame) {
      if (!frame || !frame.kind) return;
      if (frame.kind === "snapshot") {
        applySnapshot({ ...frame.payload, process_id: frame.process_id });
      } else if (frame.kind === "event") {
        pushEvent(frame.payload);
      }
    }

    function handleWsItem(item) {
      if (item && item.type === "relay_frame") {
        applyRelayFrame(item.payload && item.payload.frame);
      } else {
        pushEvent(item);
      }
    }

    async function runDispatch(continueMode) {
      const goalBase = task.trim();
      const attachRefs = attachments.map((a) => `@${a.name}`).join(" ");
      const goal = [goalBase, attachRefs].filter(Boolean).join(" ");
      if (!goal) { setDispatchStatus(d.emptyGoal); return; }
      if (teamMode && !selectedProcessId) { setDispatchStatus(d.remoteProcessRequired); return; }
      const target = effectiveSessionWorkspace(sessionRow, workspace);
      if (!target) { setDispatchStatus(d.dispatchNoWorkspace); setView("settings"); return; }
      setDispatching(true);
      const body = { goal, workspace: target, source: clientSource(), effort };
      if (sessionRow) {
        body.session_id = sessionRow.id;
        body.continue_mode = continueMode === "interrupt" ? "interrupt" : "queue";
      }
      if (model.trim()) body.model = model.trim();
      // D4: manually-picked work modes ride along (backend accepts but doesn't consume yet — P1).
      if (selectedWorkModeIds && selectedWorkModeIds.length) body.work_mode_ids = selectedWorkModeIds;
      try {
        const res = await api("/api/tasks", { method: "POST", body });
        setTask(""); setAttachments([]);
        setDispatchStatus("");
        if (teamMode) await loadRemoteSnapshot(selectedProcessId);
        else await loadSessions();
        if (res.session_id) openTimeline(res.session_id);
      } catch (e) { setDispatchStatus(`${d.dispatchFailed}: ${friendlyError(e, d)}`); }
      finally { setDispatching(false); }
    }
    async function retrySession(row) {
      if (!row || !row.goal) { setDispatchStatus(d.emptyGoal); return; }
      if (teamMode && !selectedProcessId) { setDispatchStatus(d.remoteProcessRequired); return; }
      const target = effectiveSessionWorkspace(row, workspace);
      if (!target) { setDispatchStatus(d.dispatchNoWorkspace); setView("settings"); return; }
      setDispatching(true);
      const body = { goal: row.goal, workspace: target, source: clientSource(), effort };
      if (row.model) body.model = row.model;
      try {
        const res = await api("/api/tasks", { method: "POST", body });
        setDispatchStatus("");
        if (teamMode) await loadRemoteSnapshot(selectedProcessId);
        else await loadSessions();
        if (res.session_id) openTimeline(res.session_id);
      } catch (e) { setDispatchStatus(`${d.dispatchFailed}: ${friendlyError(e, d)}`); }
      finally { setDispatching(false); }
    }
    function onAddStep(text) {
      if (!sessionRow) { toast(d.selectSessionHint, "error"); return; }
      if (teamMode && !selectedProcessId) { toast(d.remoteProcessRequired, "error"); return; }
      const body = { goal: text, workspace: effectiveSessionWorkspace(sessionRow, workspace), source: clientSource(), session_id: sessionRow.id, effort, continue_mode: "queue" };
      api("/api/tasks", { method: "POST", body }).then(() => { teamMode ? loadRemoteSnapshot(selectedProcessId) : loadSessions(); }).catch(notifyError);
    }
    async function runCompact() {
      if (!selectedSession) { setCompactStatus(d.selectSessionHint); return; }
      setCompacting(true); setCompactStatus(d.compacting);
      try { await api(`/api/sessions/${encodeURIComponent(selectedSession)}/compact`, { method: "POST" }); setCompactStatus(d.compactDone); await loadSessions(); openTimeline(selectedSession); }
      catch (e) { setCompactStatus(`${d.compactFailed}: ${friendlyError(e, d)}`); }
      finally { setCompacting(false); }
    }
    async function runBriefing() {
      try { await api("/api/reports/generate", { method: "POST", body: { kind: "active-briefing", session_id: selectedSession || "" } }); toast(d.saved, "success"); await loadReports(); setView("briefings"); }
      catch (e) { toast(`${d.briefFailed}: ${friendlyError(e, d)}`, "error"); }
    }
    async function cancelSession(id) {
      if (!id) return;
      try { await api(`/api/sessions/${encodeURIComponent(id)}/cancel`, { method: "POST" }); toast(d.sessionCanceled, "success"); await loadSessions(); openTimeline(id); }
      catch (e) { notifyError(e); }
    }
    function deleteSession(id) {
      if (!id) return;
      setConfirmSessionDelete({ id });
    }
    function openRenameSession(row) {
      if (!row || !row.id) return;
      setRenameSession(row);
      setRenameTitle(row.goal || row.id);
      setRenameError("");
    }
    async function saveSessionTitle() {
      const row = renameSession;
      const title = renameTitle.trim();
      if (!row || !row.id) return;
      if (!title) { setRenameError(d.sessionTitleEmpty); return; }
      if (title.length > 300) { setRenameError(d.sessionTitleTooLong); return; }
      setRenamingSession(true);
      setRenameError("");
      try {
        if (teamMode && !selectedProcessId) throw new Error(d.remoteProcessRequired);
        await api(`/api/sessions/${encodeURIComponent(row.id)}`, { method: "PATCH", body: { title } });
        if (teamMode) await loadRemoteSnapshot(selectedProcessId);
        else await loadSessions();
        setSessions((prev) => prev.map((s) => (s.id === row.id ? { ...s, goal: title } : s)));
        setRenameSession(null);
        setRenameTitle("");
        toast(d.sessionTitleUpdated, "success");
      } catch (e) {
        setRenameError(friendlyError(e, d));
      } finally {
        setRenamingSession(false);
      }
    }
    async function confirmDeleteSession() {
      const id = confirmSessionDelete && confirmSessionDelete.id;
      if (!id) return;
      try {
        await api(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
        setConfirmSessionDelete(null);
        selectedSessionRef.current = "";
        setSelectedSession("");
        setEvents([]);
        await loadSessions();
      } catch (e) { notifyError(e); }
    }

    async function onCard(cardId, option) {
      if (!cardId || !option) return;
      if (teamMode && !selectedProcessId) { toast(d.remoteProcessRequired, "error"); return; }
      try {
        await api(`/api/cards/${encodeURIComponent(cardId)}/choose`, { method: "POST", body: { option } });
        teamMode ? await loadRemoteSnapshot(selectedProcessId) : await loadCards();
        toast(d.saved, "success");
      }
      catch (e) { notifyError(e); }
    }
    async function decideApproval(id, decision, nonce) {
      if (teamMode && !selectedProcessId) { toast(d.remoteProcessRequired, "error"); return; }
      try {
        await api(`/api/approvals/${encodeURIComponent(id)}`, { method: "POST", body: { decision, nonce: nonce || "" } });
        teamMode ? await loadRemoteSnapshot(selectedProcessId) : await loadApprovals();
        toast(d.saved, "success");
      }
      catch (e) { notifyError(e); }
    }
    async function openDetail(actionId) {
      setDetailOpen(true); setDetail({ raw: [], diff: { files: [] } });
      try { setDetail(await api(`/api/actions/${encodeURIComponent(actionId)}/detail`)); }
      catch (e) { setDetail({ raw: [], diff: { files: [], note: friendlyError(e, d) } }); }
    }

    // definitions
    // Assemble metadata_json: preserve existing keys (e.g. example), stamp the L0 schema, write the
    // structured description. The server enforces description-required fail-closed (P0 task 5); this
    // just sends the field the editor now collects.
    function buildDefnMeta(draft) {
      let meta = {};
      try { meta = JSON.parse(draft.metadata_json || "{}") || {}; } catch (e) { meta = {}; }
      if (typeof meta !== "object" || Array.isArray(meta)) meta = {};
      meta.schema = "foreman.workmode.meta/1";
      const desc = (draft.description || "").trim();
      if (desc) meta.description = desc; else delete meta.description;
      return JSON.stringify(meta);
    }
    async function saveDefinition() {
      const draft = defnDraft || {};
      const scopeError = jsonObjectError(draft.scope_json || "{}");
      if (scopeError) {
        setDefnDraft({ ...draft, scopeError: d.badScopeJson });
        toast(d.badScopeJson, "error");
        return;
      }
      // Client-side mirror of the server gate, for a friendly message instead of a raw 400.
      if (!(draft.description || "").trim()) { toast(d.missingDescription, "error"); return; }
      const metadata_json = buildDefnMeta(draft);
      try {
        if (draft.id) {
          await api(`/api/definitions/${encodeURIComponent(draft.id)}`, { method: "PATCH", body: { body: draft.body || "", scope_json: draft.scope_json || "{}", metadata_json } });
          if (draft.activate) await api(`/api/definitions/${encodeURIComponent(draft.id)}/activate`, { method: "POST" });
        } else {
          await api("/api/definitions", { method: "POST", body: { kind: draft.kind || "workflow", name: (draft.name || "").trim(), body: draft.body || "", scope_json: draft.scope_json || "{}", metadata_json, activate: draft.activate !== false } });
        }
        setDefnOpen(false); setDefnDraft(null); await loadDefinitions(); toast(d.saved, "success");
      } catch (e) { notifyError(e); }
    }
    async function activateDefinition(id) { try { await api(`/api/definitions/${encodeURIComponent(id)}/activate`, { method: "POST" }); await loadDefinitions(); } catch (e) { notifyError(e); } }
    // ── P5: workflow run control ───────────────────────────────────────────────────────────────
    async function startWorkflowRun(row) {
      if (!selectedSession) { toast(d.wfNeedSession, "error"); return; }
      try {
        const res = await api("/api/workflows/start", { method: "POST", body: { session_id: selectedSession, workflow: row.name } });
        setWfRun({ ...res, view: res.step || null }); toast(d.wfStarted, "success");
      } catch (e) { notifyError(e); }
    }
    async function refreshWfRun() {
      if (!wfRun || !wfRun.run_id) return;
      try { setWfRun({ ...wfRun, view: await api(`/api/workflows/${encodeURIComponent(wfRun.run_id)}`) }); }
      catch (e) { /* run finished/cleared → leave last view */ }
    }
    async function wfAction(path, body) {
      if (!wfRun || !wfRun.run_id) return;
      try { await api(path, { method: "POST", body: { run_id: wfRun.run_id, ...(body || {}) } }); await refreshWfRun(); }
      catch (e) { notifyError(e); }
    }
    async function confirmDeleteDefinition() {
      const id = confirmDefnDelete && confirmDefnDelete.id;
      if (!id) return;
      try { await api(`/api/definitions/${encodeURIComponent(id)}`, { method: "DELETE" }); setConfirmDefnDelete(null); await loadDefinitions(); }
      catch (e) { notifyError(e); }
    }
    function deleteDefinition(id) { setConfirmDefnDelete({ id }); }
    async function exportDefinitions() {
      try { const bundle = await api("/api/definitions/export"); const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }); const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "foreman-definitions.json"; a.click(); URL.revokeObjectURL(a.href); }
      catch (e) { toast(d.exportFailed, "error"); }
    }
    async function importDefinitions(ev) {
      const file = ev.target.files && ev.target.files[0]; ev.target.value = ""; if (!file) return;
      try { const bundle = JSON.parse(await file.text()); const res = await api("/api/definitions/import", { method: "POST", body: { bundle } }); toast(`${d.imported}: ${res.imported || 0}`, "success"); await loadDefinitions(); }
      catch (e) { notifyError(e); }
    }

    // settings actions
    async function saveWorkspace() {
      const path = (workspaceDraft.path || "").trim(); if (!path) { toast(d.workspaceMissing, "error"); return; }
      try { const rows = await api("/api/workspaces", { method: "POST", body: { path, name: (workspaceDraft.name || "").trim() } }); setWorkspaces(rows || []); setWorkspace(path); localStorage.setItem(WORKSPACE_KEY, path); setWorkspaceDraft({ path: "", name: "" }); toast(d.saved, "success"); }
      catch (e) { notifyError(e); }
    }
    async function browseFolder() {
      const bridge = window.pywebview && window.pywebview.api;
      if (!bridge || !bridge.select_workspace_folder) { toast(d.folderPickerUnavailable, "error"); return; }
      try { const path = await bridge.select_workspace_folder(); if (!path) return; setWorkspaceDraft((p) => ({ ...p, path, name: p.name || shortPath(path, d) })); }
      catch (e) { toast(d.folderPickerUnavailable, "error"); }
    }
    async function deleteWorkspace(path) {
      try { const rows = await api(`/api/workspaces?path=${encodeURIComponent(path)}`, { method: "DELETE" }); const next = rows || []; setWorkspaces(next); if (workspace === path) { const chosen = (next[0] && next[0].path) || ""; setWorkspace(chosen); if (chosen) localStorage.setItem(WORKSPACE_KEY, chosen); else localStorage.removeItem(WORKSPACE_KEY); } }
      catch (e) { notifyError(e); }
    }
    async function saveAgentSettings() {
      try { const rows = await api("/api/settings/agents", { method: "POST", body: { agents: agentSettings } }); setAgentSettings(rows || []); setAgentStatus(d.agentsSaved); await loadModels(); }
      catch (e) { setAgentStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }
    async function saveLlm() {
      try {
        const body = { provider: llm.provider || "openai", model: (llm.model || "").trim(), base_url: (llm.base_url || "").trim(), transport: llm.transport || "http", request_timeout_s: Number(llm.request_timeout_s) || 300, context_window_tokens: Number(llm.context_window_tokens) || 272000, reasoning_effort: llm.reasoning_effort || "" };
        if ((llm.api_key || "").trim()) body.api_key = llm.api_key.trim();
        const data = await api("/api/settings/llm", { method: "POST", body }); const next = { ...data, api_key: "" }; setLlm(next); setLlmStatus(d.saved); await loadPmModels(next); await loadModels();
      } catch (e) { setLlmStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }
    async function clearLlmKey() {
      try { const data = await api("/api/settings/llm", { method: "POST", body: { api_key: "" } }); setLlm({ ...data, api_key: "" }); setLlmStatus(d.saved); }
      catch (e) { setLlmStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }
    async function savePmTools() {
      try {
        const data = await api("/api/settings/pm-tools", { method: "POST", body: pmTools });
        setPmTools(data || {});
        setPmToolsStatus(d.pmToolsSaved);
      } catch (e) { setPmToolsStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }
    async function saveAutonomy(value) {
      setAutonomyState(value);
      try {
        if (teamMode && !selectedProcessId) { toast(d.remoteProcessRequired, "error"); return; }
        const res = await api("/api/settings/autonomy", { method: "POST", body: { level: value } });
        if (typeof res.level === "number") setAutonomyState(res.level);
        if (teamMode) await loadRemoteSnapshot(selectedProcessId);
      }
      catch (e) { notifyError(e); }
    }
    async function saveCloud() {
      try {
        const body = { url: (cloud.url || "").trim() };
        if ((cloud.access_key || "").trim()) body.access_key = cloud.access_key.trim();
        const c = await api("/api/settings/cloud", { method: "POST", body });
        setCloud({ url: c.url || "", access_key: "", access_key_set: !!c.access_key_set, connected: !!c.connected, remote_execution_enabled: !!c.remote_execution_enabled }); setCloudStatus(d.saved);
      } catch (e) { setCloudStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }
    async function saveRemoteExec(enabled) {
      setCloud((p) => ({ ...p, remote_execution_enabled: enabled }));  // optimistic — the checkbox follows the click
      try {
        const c = await api("/api/settings/cloud", { method: "POST", body: { remote_execution_enabled: enabled } });
        setCloud((p) => ({ ...p, remote_execution_enabled: !!c.remote_execution_enabled })); setCloudStatus(d.saved);
      } catch (e) {
        setCloud((p) => ({ ...p, remote_execution_enabled: !enabled }));  // revert on failure
        setCloudStatus(`${d.saveFailed}: ${friendlyError(e, d)}`);
      }
    }
    async function connectCloud() {
      setCloudStatus(d.connecting);
      try { const c = await api("/api/settings/cloud/connect", { method: "POST" }); setCloud((p) => ({ ...p, connected: !!c.connected, access_key: "" })); setCloudStatus(c.connected ? d.connected : (c.error ? `${d.connFailed}: ${friendlyError(c.error, d)}` : d.connecting)); }
      catch (e) { setCloudStatus(`${d.connFailed}: ${friendlyError(e, d)}`); }
    }
    async function disconnectCloud() {
      try { const c = await api("/api/settings/cloud/disconnect", { method: "POST" }); setCloud((p) => ({ ...p, connected: !!c.connected })); setCloudStatus(d.notConnected); }
      catch (e) { notifyError(e); }
    }
    async function clearCloudKey() {
      try { const c = await api("/api/settings/cloud", { method: "POST", body: { access_key: "" } }); setCloud({ url: c.url || "", access_key: "", access_key_set: !!c.access_key_set, connected: !!c.connected, remote_execution_enabled: !!c.remote_execution_enabled }); setCloudStatus(d.saved); }
      catch (e) { setCloudStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }

    async function enablePush() {
      if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) { toast(d.pushUnsupported, "error"); return; }
      try {
        const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
        if (perm !== "granted") { toast(d.pushDenied, "error"); return; }
        const { key, enabled } = await api("/api/push/vapid-public-key");
        if (!enabled || !key) { toast(d.pushNotConfigured, "error"); return; }
        const reg = await navigator.serviceWorker.ready;
        let sub = await reg.pushManager.getSubscription();
        if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key) });
        await api("/api/push/subscribe", { method: "POST", body: sub.toJSON ? sub.toJSON() : sub });
        toast(d.pushEnabled, "success");
        await reg.showNotification("Foreman", { body: d.pushNotifSub, icon: "/icon-192.png", badge: "/icon-192.png", tag: "foreman-push-test", data: { url: "/?view=decisions" } });
      } catch (e) { toast(`${d.pushFailed}: ${friendlyError(e, d)}`, "error"); }
    }

    function attachmentFromFile(file, index = 0) {
      const type = String(file && file.type || "");
      const ext = type.split("/")[1] || "png";
      const name = String(file && file.name || "").trim() || `pasted-image-${Date.now()}-${index + 1}.${ext}`;
      return {
        id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        name,
        isImage: type.toLowerCase().startsWith("image/") || /\.(png|jpe?g|gif|webp|svg)$/i.test(name),
        type,
        size: Number(file && file.size) || 0,
      };
    }
    function addFiles(files) {
      const rows = Array.from(files || []).map((f, i) => attachmentFromFile(f, i));
      if (rows.length) setAttachments((p) => [...p, ...rows]);
    }
    function addPastedImages(files) {
      addFiles(files);
    }
    function addAttach() {
      const input = document.createElement("input"); input.type = "file"; input.accept = "*/*";
      input.multiple = true;
      input.onchange = () => addFiles(input.files || []);
      input.click();
    }
    const removeAttach = (id) => setAttachments((p) => p.filter((a) => a.id !== id));
    const toggleCall = (id) => setOpenCalls((s) => ({ ...s, [id]: !s[id] }));
    const toggleSub = (id) => setExpandedSub((cur) => (cur === id ? null : id));
    const onCopy = (text) => { try { navigator.clipboard.writeText(text); toast(d.copied, "success"); } catch (e) {} };

    const counts = { workspace: sessions.filter((s) => (s.status || "").toLowerCase().match(/run|active/)).length, decisions: openCards.length + approvals.length };

    const composerProps = {
      workspaces, workspace, setWorkspace: (v) => { setWorkspace(v); if (v) localStorage.setItem(WORKSPACE_KEY, v); },
      task, setTask, model, setModel, modelOptions, llm, effort, setEffort, attachments, addAttach, addPastedImages, removeAttach,
      dispatching, runDispatch, dispatchStatus, onAddStep, onCancelSession: cancelSession,
      processes, selectedProcessId, setSelectedProcessId, teamMode,
      definitions, selectedWorkModeIds, setSelectedWorkModeIds,
    };
    const settingsProps = {
      d, lang, workspaces, workspaceDraft, setWorkspaceDraft, saveWorkspace, browseFolder, deleteWorkspace, loadWorkspaces,
      agentSettings, setAgentSettings, saveAgentSettings, agentStatus, loadAgentSettings,
      llm, setLlm, pmModelOptions, saveLlm, clearLlmKey, llmStatus,
      pmTools, setPmTools, savePmTools, pmToolsStatus, loadPmTools,
      debugSettings, debugStatus, saveDebug,
      cloud, setCloud, saveCloud, saveRemoteExec, connectCloud, disconnectCloud, clearCloudKey, cloudStatus, cloudAvailable,
      autonomy, saveAutonomy, theme, setTheme, lang2: lang, setLang, onPush: enablePush,
    };
    const decisionsProps = { d, lang, cards: openCards, approvals, onCard, onApproval: decideApproval, openDetail, onGoSession: openTimeline };
    const briefingsProps = { d, lang, reports, onCopy, toast };
    const playbookProps = { d, lang, definitions, filter: defnFilter, setFilter: setDefnFilter, onNew: () => { setDefnDraft({ kind: defnFilter || "workflow", scope_json: "{}", body: "", activate: true }); setDefnOpen(true); }, onEdit: (row) => { let desc = ""; try { desc = (JSON.parse(row.metadata_json || "{}") || {}).description || ""; } catch (e) {} setDefnDraft({ ...row, description: desc, activate: !!row.is_active }); setDefnOpen(true); }, onActivate: activateDefinition, onDelete: deleteDefinition, onExport: exportDefinitions, onImportClick: () => fileRef.current && fileRef.current.click(), fileRef, onImport: importDefinitions, onStartWorkflow: startWorkflowRun };

    const launchSteps = { engine: status.online, agents: agentsLoaded, data: booted, pct: booted ? 100 : (status.online ? 60 : 25), version: status.version };
    const versionInfoProps = {
      d, lang, version: status.version,
      onCheckUpdate: () => checkAppUpdate(true),
      checkingUpdate,
      updateCheckStatus,
    };

    const mainProps = {
      decisions: decisionsProps, briefings: briefingsProps,
      briefingsTop: html`<button className="btn primary block" style=${{ marginBottom: 13 }} onClick=${runBriefing}>✦ ${d.generate}</button>`,
      playbook: playbookProps, settings: settingsProps, composer: composerProps,
      openCalls, toggleCall, expandedSub, toggleSub, onCard, onApproval: decideApproval, openDetail, sessionRow,
      cards: openCards, approvals,
      onCancelSession: cancelSession,
      onRetrySession: retrySession,
      onDeleteSession: deleteSession,
      onRenameSession: openRenameSession,
      onCopy,
      topControls: html`<${TopCtrls} d=${d} lang=${lang} dark=${theme === "dark"} onToggleTheme=${() => setTheme(theme === "dark" ? "light" : "dark")} onToggleLang=${() => setLang(lang === "zh" ? "en" : "zh")} onPush=${enablePush} />`,
    };

    return html`<div>
      ${embedded && onBack ? html`<button className="btn control-back" onClick=${onBack}>返回总控制台</button>` : null}
      ${!hidingLaunch ? html`<${Launch} d=${d} lang=${lang} hiding=${booted} steps=${launchSteps} />` : null}

      <div className="toasts">${toasts.map((t) => html`<div key=${t.id} className=${`toast ${t.type || ""}`}>${t.text}</div>`)}</div>

      ${updateVersion ? html`<div className="update-banner">
        <span className="ub-msg">${d.newVersionReady} · v${updateVersion}</span>
        <button className="btn primary sm" onClick=${() => location.reload()}>${d.refreshNow}</button>
        <button className="btn sm ghost" onClick=${() => setUpdateVersion("")}>${d.later}</button>
      </div>` : null}

      ${appUpdate ? html`<${UpdateModal}
        d=${d}
        lang=${lang}
        update=${appUpdate}
        status=${updateStatus}
        updating=${updating}
        updateError=${updateError}
        cancelingUpdate=${cancelingUpdate}
        onApply=${applyAppUpdate}
        onCancel=${cancelAppUpdate}
        onClose=${() => { setAppUpdate(null); setUpdateStatus(null); setUpdateError(false); }}
      />` : null}

      <!-- desktop -->
      <div className="app desktop">
        <${Sidebar} d=${d} lang=${lang} view=${view} onView=${setView} counts=${counts} sessions=${sessions} selected=${selectedSession} onSelect=${openTimeline} onNew=${newSession} onRename=${openRenameSession} version=${status.version} />
        ${view === "workspace" ? html`<${Workspace}
            d=${d} lang=${lang} dig=${dig} sessionRow=${sessionRow} events=${events} autonomy=${autonomy}
            openCalls=${openCalls} toggleCall=${toggleCall} expandedSub=${expandedSub} toggleSub=${toggleSub}
            rightTab=${rightTab} setRightTab=${setRightTab} onCard=${onCard} onApproval=${decideApproval} openDetail=${openDetail}
            composer=${composerProps} runCompact=${runCompact} compacting=${compacting} compactStatus=${compactStatus} onBriefing=${runBriefing}
            cards=${openCards} approvals=${approvals} onCancelSession=${cancelSession} onDeleteSession=${deleteSession}
            onRetrySession=${retrySession} onRenameSession=${openRenameSession} onCopy=${onCopy}
            topControls=${mainProps.topControls} />`
          : html`<div className="main">
              <div className="page-head">
                <div><h2>${d[`nav${view.charAt(0).toUpperCase()}${view.slice(1)}`] || d.navWorkspace}</h2><div className="sub">${d[`${view}Subtitle`] || ""}</div></div>
                <div className="spacer"></div>
                ${view === "briefings" ? html`<button className="btn primary" onClick=${runBriefing}>✦ ${d.generate}</button>` : null}
                <${TopCtrls} d=${d} lang=${lang} dark=${theme === "dark"} onToggleTheme=${() => setTheme(theme === "dark" ? "light" : "dark")} onToggleLang=${() => setLang(lang === "zh" ? "en" : "zh")} onPush=${enablePush} />
              </div>
              <div className="page-body">
                ${view === "decisions" ? html`<${Decisions} ...${decisionsProps} />` : null}
                ${view === "briefings" ? html`<${Briefings} ...${briefingsProps} />` : null}
                ${view === "rules" ? html`<${Playbook} ...${playbookProps} />` : null}
                ${view === "settings" ? html`<${Settings} ...${settingsProps} />` : null}
                ${view === "version" ? html`<${VersionInfo} ...${versionInfoProps} />` : null}
              </div>
            </div>`}
      </div>

      <!-- mobile -->
      <${MobileShell} d=${d} lang=${lang} view=${view} setView=${setView} mTab=${mTab} setMTab=${setMTab}
        drawerOpen=${drawerOpen} setDrawerOpen=${setDrawerOpen} counts=${counts} sessionRow=${sessionRow}
        dig=${dig} mainProps=${mainProps} versionInfoProps=${versionInfoProps} sessions=${sessions} selected=${selectedSession} onSelect=${openTimeline} onNew=${newSession} onRename=${openRenameSession} />

      ${detailOpen ? html`<${DetailModal} d=${d} lang=${lang} detail=${detail} onClose=${() => setDetailOpen(false)} />` : null}
      ${fileViewer ? html`<${FileViewerModal} d=${d} file=${fileViewer} onClose=${() => setFileViewer(null)} />` : null}
      ${renameSession ? html`<${SessionTitleModal} d=${d} title=${renameTitle} saving=${renamingSession} error=${renameError} setTitle=${setRenameTitle} onClose=${() => { setRenameSession(null); setRenameError(""); }} onSave=${saveSessionTitle} />` : null}
      ${defnOpen ? html`<${Modal} title=${defnDraft && defnDraft.id ? d.edit : d.newBtn} onClose=${() => setDefnOpen(false)} footer=${[html`<button key="c" className="btn" onClick=${() => setDefnOpen(false)}>${d.cancel}</button>`, html`<button key="s" className="btn primary" onClick=${saveDefinition}>${d.save}</button>`]}>
        <${DefinitionEditor} d=${d} draft=${defnDraft} setDraft=${setDefnDraft} />
      </${Modal}>` : null}
      ${confirmDefnDelete ? html`<${Modal} title=${d.confirmDeleteTitle} onClose=${() => setConfirmDefnDelete(null)} footer=${[html`<button key="c" className="btn" onClick=${() => setConfirmDefnDelete(null)}>${d.cancel}</button>`, html`<button key="d" className="btn danger" onClick=${confirmDeleteDefinition}>${d.del}</button>`]}>
        <div>${d.confirmDelete}</div>
      </${Modal}>` : null}
      ${wfRun ? html`<${Modal} title=${`${d.workflowRun}: ${wfRun.workflow || ""}`} onClose=${() => setWfRun(null)} footer=${[html`<button key="r" className="btn" onClick=${refreshWfRun}>⟲ ${d.wfRefresh}</button>`, html`<button key="x" className="btn" onClick=${() => setWfRun(null)}>${d.cancel}</button>`]}>
        ${(() => { const v = wfRun.view || {}; const run = v.run || {}; const status = run.step_status || "pending"; const blocked = status === "blocked"; return html`<div style=${{ display: "flex", flexDirection: "column", gap: 10, fontSize: 13 }}>
          <div><b>${d.wfStep}</b> ${(typeof run.step_index === "number" ? run.step_index + 1 : 1)} / ${wfRun.total_steps || "?"} — ${v.name || ""}</div>
          <div><b>${d.wfStatus}</b> <span className=${`tag ${blocked ? "amber" : (status === "passed" ? "green" : "plain")}`}>${status}</span></div>
          ${v.instruction ? html`<div className="desc"><${MD} text=${v.instruction} className="markdown-compact" /></div>` : null}
          ${(v.missing && v.missing.length) ? html`<div className="alert warn">⚠ missing: ${v.missing.join(", ")}</div>` : null}
          <div style=${{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            ${blocked
              ? html`<button className="btn primary" onClick=${() => wfAction("/api/workflows/resume", { approved: true })}>${d.wfApprove}</button><button className="btn danger" onClick=${() => wfAction("/api/workflows/resume", { approved: false })}>${d.wfReject}</button>`
              : html`<button className="btn" onClick=${() => wfAction("/api/workflows/begin")}>${d.wfBegin}</button><button className="btn primary" onClick=${() => wfAction("/api/workflows/submit")}>${d.wfSubmit}</button>`}
          </div>
        </div>`; })()}
      </${Modal}>` : null}
      ${confirmSessionDelete ? html`<${Modal} title=${d.confirmDeleteTitle} onClose=${() => setConfirmSessionDelete(null)} footer=${[html`<button key="c" className="btn" onClick=${() => setConfirmSessionDelete(null)}>${d.cancel}</button>`, html`<button key="d" className="btn danger" onClick=${confirmDeleteSession}>${d.deleteSession}</button>`]}>
        <div>${d.confirmSessionDelete}</div>
      </${Modal}>` : null}
    </div>`;
  }

  window.ForemanControlApp = { Root: Shell };

  const rootEl = document.getElementById("root");
  if (rootEl && !rootEl.dataset.adminRoot) {
    ReactDOM.createRoot(rootEl).render(html`<${Shell} />`);
  }
})();
