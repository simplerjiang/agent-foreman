(function () {
  "use strict";

  const { useCallback, useEffect, useMemo, useRef, useState } = React;
  const html = htm.bind(React.createElement);

  const TOKEN_KEY = "foreman.token";
  const LANG_KEY = "foreman.lang";
  const THEME_KEY = "foreman.theme";
  const WORKSPACE_KEY = "foreman.workspace";
  const DEFAULT_CONTEXT_TOKENS = 128000;

  // ---------------------------------------------------------------------------
  // i18n
  // ---------------------------------------------------------------------------
  const I18N = {
    zh: {
      productSubtitle: "本地工作台",
      navWorkspace: "工作台", navDecisions: "决策", navBriefings: "简报", navRules: "工作方式", navSettings: "设置",
      workspaceSubtitle: "选择工作区，给本机 agent 下发任务。",
      decisionsSubtitle: "处理需要你确认的卡片和审批。",
      briefingsSubtitle: "把当前进展整理成可读状态。",
      rulesSubtitle: "维护工作流、技能、代码规范和验收标准 —— PM 规划时按相关性选用，干活时按需取用。",
      settingsSubtitle: "配置工作区、PM 大脑和界面偏好。",
      sessions: "会话", newSession: "新会话",
      launchTag: "正在唤醒你的工程包工头 —— 把活儿交给本地 agent，PM 大脑替你盯着。",
      launchEngine: "引擎已就绪 · PM Core",
      launchAgents: "连接本地 agent",
      launchLoad: "加载工作区与工作方式…",
      launchBrain: "唤醒 PM 大脑",
      personalMode: "个人模式 · 离线优先",
      selectSessionHint: "从左侧选择一个会话，或在下方下发新任务。",
      running: "运行中", live: "运行中", done: "完成", queued: "排队", cancelled: "已取消",
      autonomy: "自动权限", briefing: "生成简报", pmThinking: "PM 正在思考...",
      plan: "计划", approved: "已确认", active: "进行中",
      reply: "回复", commandsRun: "执行的命令", fileChanges: "文件改动",
      open: "展开", hide: "收起",
      decisionNeeded: "需要你拍板", suggestion: "建议", showDiff: "看 diff",
      riskHigh: "高风险", riskMedium: "中风险", riskLow: "低风险",
      context: "上下文", compact: "压缩上下文", compacting: "压缩中...", compactDone: "上下文已压缩", compactFailed: "压缩失败",
      attach: "附件", agentAuto: "执行 agent 由 PM 自动选择", modelPlaceholder: "模型·默认",
      fast: "快速", std: "标准", deep: "深度", send: "发送", sendHint: "发送",
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
      deleteSession: "删除会话", cancelSession: "取消会话", sessionCanceled: "已取消会话", notification: "通知",
      sessionBusy: "会话仍有后台任务未结束，请稍后再删除。",
      noContext: "当前会话还没有可压缩的上下文。",
      noStore: "本地数据存储不可用，请重启 Foreman 后重试。",
      sessionNotFound: "没有找到这个会话，请刷新后重试。",
      requestDeclined: "操作未被执行，请检查当前状态后重试。",
      networkError: "网络异常，请检查连接后重试。",
      badScopeJson: "适用范围必须是 JSON 对象，例如 {\"lang\":\"py\"}。",
      missingDescription: "请填写描述（说明做什么 + 何时用），否则不会进入自动选择。",
      descriptionTooLong: "描述太长了，请控制在 1024 字以内。",
      imported: "已导入", importFailed: "导入失败", exportFailed: "导出失败",
      workspaces: "工作区", projectPath: "项目路径", displayName: "显示名称", pathHint: "例如 E:\\AutoWorkAgent",
      browse: "浏览", addWorkspace: "添加 / 更新工作区", remove: "移除", connected: "已连接",
      refresh: "刷新", folderPickerUnavailable: "当前浏览器不支持选择文件夹，请手动输入路径。",
      localAgents: "本地 Agent", agentEnabled: "启用", agentCommand: "启动命令", agentModel: "模型", agentEffort: "档位", agentFullAccess: "工具全开",
      agentDisabled: "已禁用", agentNotFound: "未找到命令", agentsSaved: "Agent 设置已保存", noEnabledAgent: "至少要启用一个 Agent。",
      effortDefault: "默认", modelDefaultHint: "留空 = 使用配置默认模型",
      pmBrain: "PM 大脑", pmBrainSub: "给 PM 审阅 / 简报调用的模型。Key 永远留在本地。",
      pmTools: "PM 工具", pmToolsSub: "PM 运行时工具开关和白名单。只读仓库工具默认开启。",
      fileRead: "读取文件", fileWrite: "写入文件", shellTool: "运行命令", webFetch: "抓取 URL", webSearch: "网页搜索", browserTool: "浏览器",
      allowedCommands: "允许的命令", allowedOrigins: "允许的浏览器来源", searxngUrl: "SearXNG 地址", browserHeadless: "无头浏览器", maxRounds: "循环 / 最大轮次",
      pmToolsSaved: "PM 工具设置已保存",
      debug: "调试", debugSub: "排错用的高级开关。默认全关。",
      llmTrace: "LLM 对话明文落盘",
      llmTraceWarn: "开启后会把与大模型的完整对话（含源码与解密后的工作方式）明文写入本机 .foreman/debug/，仅本地保存、不上传、不进 git。改动在下次启动生效。",
      debugSaved: "调试设置已保存（重启生效）",
      provider: "服务商", model: "模型", baseUrl: "接口地址", apiKey: "API Key", transport: "传输方式",
      reasoningEffort: "推理强度",
      pmKeyHint: "已配置 API Key。输入新 key 后保存可替换；留空不修改。", pmKeyMissing: "未检测到 API Key。可在这里输入并保存。",
      pmKeyPlaceholder: "留空不修改；输入新 key 后保存", clearKey: "清空 Key",
      cloudConn: "云端连接", cloudSub: "把本机接入线上总机 —— 人不在电脑前也能在手机上看进度、点审批。总机不存你的代码与 Key。",
      cloudUrl: "云端地址", accessKey: "接入密钥 Access Key", accessKeyHint: "在云端 /keys.html 生成，一机一张、可单独吊销。",
      connect: "连接", disconnect: "断开", connecting: "连接中…", notConnected: "未连接", connFailed: "连接失败",
      cloudNotConfigured: "请先填写云端地址和接入密钥。",
      cloudKeyHint: "已配置接入密钥。输入新密钥后保存可替换；留空不修改。", cloudKeyMissing: "未配置接入密钥。",
      cloudUnavailable: "当前服务不支持云端连接（仅本机 app 可用）。",
      interface: "界面与自动化", autoExec: "自动执行权限", autoExecHelp: "决定 Foreman 在没有你确认时能自动执行多少动作。",
      auto0: "0 只报告", auto1: "1 凡事都问", auto2: "2 自动可逆", auto3: "3 只拦不可逆",
      theme: "主题", light: "浅色", dark: "深色", language: "语言",
      pushNotif: "手机通知", pushNotifSub: "决策与审批推到手机", enable: "开启",
      pushEnabled: "通知已开启", pushUnsupported: "此浏览器不支持通知", pushNotConfigured: "服务器未配置推送", pushDenied: "通知权限被拒绝", pushFailed: "开启通知失败",
      stepDetail: "步骤详情", rawReturn: "原始返回", codeDiff: "代码改动", back: "返回", viewDetail: "查看详情",
      dispatched: "已下发", continued: "已发送到当前会话", dispatchFailed: "下发失败", emptyGoal: "任务不能为空。",
      dispatchNoWorkspace: "未配置工作区：请到设置页添加项目路径。", workspaceEmpty: "没有配置工作区。",
      noDispatcher: "当前服务不是本地 PC 工作台，不能下发任务。", workspaceMissing: "没有可用工作区。",
      ev_stop: "完成", ev_error: "错误", ev_checkpoint: "检查点", ev_gate: "闸门",
      ev_action_executed: "已执行", ev_action_undone: "已回退", ev_context_compact: "上下文压缩",
      ev_review: "复查", ev_audit: "审查", ev_undo: "回退", ev_recover: "恢复", ev_stall: "卡住",
      noActiveSession: "暂无活动会话。", noAgent: "无 agent",
      readOnlyLog: "只读日志", workspaceRisk: "当前工作区范围很大；工具全开时请确认这是你想授权的路径。",
    },
    en: {
      productSubtitle: "Local workbench",
      navWorkspace: "Workspace", navDecisions: "Decisions", navBriefings: "Briefings", navRules: "Playbook", navSettings: "Settings",
      workspaceSubtitle: "Pick a workspace and dispatch work to the local agent.",
      decisionsSubtitle: "Handle the cards and approvals that need you.",
      briefingsSubtitle: "Turn current progress into readable status.",
      rulesSubtitle: "Maintain workflows, skills, code standards & QA rubrics — selected by relevance and pulled in on demand.",
      settingsSubtitle: "Configure workspaces, the PM brain, and UI preferences.",
      sessions: "Sessions", newSession: "New session",
      launchTag: "Waking your engineering foreman — hand work to local agents, the PM brain watches over it.",
      launchEngine: "Engine ready · PM Core",
      launchAgents: "Local agents linked",
      launchLoad: "Loading workspaces & playbook…",
      launchBrain: "Waking PM brain",
      personalMode: "Personal · offline-first",
      selectSessionHint: "Pick a session on the left, or dispatch a new task below.",
      running: "RUNNING", live: "LIVE", done: "done", queued: "queued", cancelled: "cancelled",
      autonomy: "Autonomy", briefing: "Briefing", pmThinking: "PM is thinking...",
      plan: "Plan", approved: "approved", active: "active",
      reply: "Reply", commandsRun: "Commands run", fileChanges: "File changes",
      open: "Open", hide: "Hide",
      decisionNeeded: "Decision needed", suggestion: "Suggestion", showDiff: "Show diff",
      riskHigh: "HIGH RISK", riskMedium: "MEDIUM RISK", riskLow: "LOW RISK",
      context: "Context", compact: "Compact", compacting: "Compacting...", compactDone: "Context compacted", compactFailed: "Compact failed",
      attach: "Attach", agentAuto: "agent auto-picked by PM", modelPlaceholder: "model · default",
      fast: "Fast", std: "Std", deep: "Deep", send: "Send", sendHint: "send",
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
      deleteSession: "Delete session", cancelSession: "Cancel session", sessionCanceled: "Session cancelled", notification: "Notification",
      sessionBusy: "A background task is still active; delete it after the task finishes.",
      noContext: "This session has no context to compact yet.",
      noStore: "Local storage is unavailable. Restart Foreman and try again.",
      sessionNotFound: "This session was not found. Refresh and try again.",
      requestDeclined: "The operation was not completed. Check the current state and try again.",
      networkError: "Network error. Check the connection and try again.",
      badScopeJson: "Scope must be a JSON object, for example {\"lang\":\"py\"}.",
      missingDescription: "Please add a description (what it does + when to use), or it won't be auto-selected.",
      descriptionTooLong: "Description is too long — keep it under 1024 characters.",
      imported: "Imported", importFailed: "Import failed", exportFailed: "Export failed",
      workspaces: "Workspaces", projectPath: "Project path", displayName: "Name", pathHint: "e.g. E:\\AutoWorkAgent",
      browse: "Browse", addWorkspace: "Add / update", remove: "Remove", connected: "connected",
      refresh: "Refresh", folderPickerUnavailable: "This browser cannot open a folder picker. Enter the path manually.",
      localAgents: "Local agents", agentEnabled: "Enabled", agentCommand: "Command", agentModel: "Model", agentEffort: "Level", agentFullAccess: "Full access",
      agentDisabled: "Disabled", agentNotFound: "Command not found", agentsSaved: "Agent settings saved", noEnabledAgent: "Enable at least one agent.",
      effortDefault: "Default", modelDefaultHint: "blank = configured default model",
      pmBrain: "PM brain", pmBrainSub: "The model the PM uses to review & brief. Your key never leaves this machine.",
      pmTools: "PM tools", pmToolsSub: "PM runtime tool switches and allowlists. Read-only repo tools are on by default.",
      fileRead: "Read files", fileWrite: "Write files", shellTool: "Run commands", webFetch: "Fetch URL", webSearch: "Web search", browserTool: "Browser",
      allowedCommands: "Allowed commands", allowedOrigins: "Allowed browser origins", searxngUrl: "SearXNG URL", browserHeadless: "Headless browser", maxRounds: "Loop / max rounds",
      pmToolsSaved: "PM tool settings saved",
      debug: "Debug", debugSub: "Advanced switches for troubleshooting. All off by default.",
      llmTrace: "Trace LLM conversations to disk",
      llmTraceWarn: "Writes the FULL model conversation (incl. your source + decrypted work modes) in plaintext to .foreman/debug/ on this machine — local only, never uploaded, not committed. Takes effect on next launch.",
      debugSaved: "Debug settings saved (restart to apply)",
      provider: "Provider", model: "Model", baseUrl: "Base URL", apiKey: "API Key", transport: "Transport",
      reasoningEffort: "Reasoning effort",
      pmKeyHint: "API key is set. Enter a new key and save to replace it; blank keeps it.", pmKeyMissing: "No API key detected. You can enter and save one here.",
      pmKeyPlaceholder: "blank = unchanged; enter a new key to save", clearKey: "Clear key",
      cloudConn: "Cloud connection", cloudSub: "Link this machine to the online relay — watch progress and approve from your phone. The relay never stores your code or keys.",
      cloudUrl: "Cloud URL", accessKey: "Access key", accessKeyHint: "Mint one at /keys.html on the relay — one per machine, individually revocable.",
      connect: "Connect", disconnect: "Disconnect", connecting: "Connecting…", notConnected: "Not connected", connFailed: "Connection failed",
      cloudNotConfigured: "Enter the cloud URL and access key first.",
      cloudKeyHint: "Access key set. Enter a new key and save to replace it; blank keeps it.", cloudKeyMissing: "No access key configured.",
      cloudUnavailable: "This service does not support cloud connection (local app only).",
      interface: "Interface & automation", autoExec: "Auto-execution", autoExecHelp: "How much Foreman may do without your confirmation.",
      auto0: "0 report", auto1: "1 ask first", auto2: "2 auto safe", auto3: "3 auto reversible",
      theme: "Theme", light: "Light", dark: "Dark", language: "Language",
      pushNotif: "Push notifications", pushNotifSub: "decisions & approvals to your phone", enable: "Enable",
      pushEnabled: "Notifications enabled", pushUnsupported: "Notifications are not supported in this browser", pushNotConfigured: "Push is not configured on the server", pushDenied: "Notification permission was denied", pushFailed: "Could not enable notifications",
      stepDetail: "Step detail", rawReturn: "Raw return", codeDiff: "Code diff", back: "Back", viewDetail: "View detail",
      dispatched: "Dispatched", continued: "Sent to current session", dispatchFailed: "Dispatch failed", emptyGoal: "Task cannot be empty.",
      dispatchNoWorkspace: "No workspace configured. Add a project path in Settings.", workspaceEmpty: "No workspaces configured.",
      noDispatcher: "This service is not the local PC workspace.", workspaceMissing: "No workspace available.",
      ev_stop: "Done", ev_error: "Error", ev_checkpoint: "Checkpoint", ev_gate: "Gate",
      ev_action_executed: "Executed", ev_action_undone: "Undone", ev_context_compact: "Context compacted",
      ev_review: "Review", ev_audit: "Audit", ev_undo: "Undo", ev_recover: "Recover", ev_stall: "Stall",
      noActiveSession: "No active sessions yet.", noAgent: "no agent",
      readOnlyLog: "Read-only log", workspaceRisk: "This workspace is very broad; confirm that full tool access is intentional.",
    },
  };

  const NAV = [
    { key: "workspace", ico: "◳", label: "navWorkspace" },
    { key: "decisions", ico: "◉", label: "navDecisions" },
    { key: "briefings", ico: "▤", label: "navBriefings" },
    { key: "rules", ico: "▦", label: "navRules" },
    { key: "settings", ico: "⚙", label: "navSettings" },
  ];
  const KIND_LABEL = { workflow: "kindWorkflow", skill: "kindSkill", code_standard: "kindStandard", qa_rubric: "kindQaOne" };
  const KIND_TAGCOLOR = { workflow: "accent", skill: "violet", code_standard: "amber", qa_rubric: "green" };
  const STREAM_TYPES = new Set(["pm_output", "pm_reasoning", "agent_output", "agent_reasoning"]);

  // ---------------------------------------------------------------------------
  // token + fetch
  // ---------------------------------------------------------------------------
  const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
  const setToken = (t) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY));
  let promptedForToken = false;
  function promptForToken() {
    if (promptedForToken) return;
    promptedForToken = true;
    const t = window.prompt("Access token required (FOREMAN_AUTH_TOKEN):", "");
    if (t && t.trim()) { setToken(t.trim()); location.reload(); }
  }
  const rawFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const sameOrigin = url.startsWith("/") || url.startsWith(location.origin);
    const headers = new Headers(init.headers || {});
    const token = getToken();
    if (sameOrigin && token) headers.set("Authorization", `Bearer ${token}`);
    const res = await rawFetch(input, { ...init, headers });
    if (res.status === 401 && sameOrigin) promptForToken();
    return res;
  };
  class ApiError extends Error {
    constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
  }
  async function api(path, opts = {}) {
    const headers = new Headers(opts.headers || {});
    let body = opts.body;
    if (body !== undefined && typeof body !== "string") { headers.set("Content-Type", "application/json"); body = JSON.stringify(body); }
    const res = await fetch(path, { ...opts, headers, body });
    const ct = res.headers.get("content-type") || "";
    let data = ct.includes("application/json") ? await res.json().catch(() => null) : await res.text().catch(() => "");
    if (!res.ok) {
      const detail = data && typeof data === "object" ? data.detail : "";
      throw new ApiError(detail || res.statusText || `HTTP ${res.status}`, res.status, data);
    }
    return data;
  }

  // ---------------------------------------------------------------------------
  // helpers
  // ---------------------------------------------------------------------------
  function formatTime(value, lang) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  function formatDateTime(value, lang) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }
  function shortPath(p, d) {
    if (!p) return (d && d.workspaceMissing) || "-";
    const parts = String(p).replace(/\\/g, "/").split("/").filter(Boolean);
    return parts[parts.length - 1] || p;
  }
  function isWideWorkspace(p) {
    const v = String(p || "").trim().replace(/\//g, "\\");
    return /^[A-Za-z]:\\?$/.test(v) || /^\\\\[^\\]+\\[^\\]+\\?$/.test(v);
  }
  function friendlyError(error, d) {
    const detail = String(error && error.message ? error.message : error || "");
    if (/failed to fetch|networkerror|network error|load failed/i.test(detail)) return d.networkError;
    const map = {
      empty_goal: d.emptyGoal, no_workspace: d.dispatchNoWorkspace, workspace_not_allowed: d.workspaceMissing,
      unknown_agent: d.noEnabledAgent, no_enabled_agent: d.noEnabledAgent, no_dispatcher: d.noDispatcher,
      "no dispatcher": d.noDispatcher, no_llm: d.briefNoLlm, bad_scope_json: d.badScopeJson,
      not_configured: d.cloudNotConfigured, cloud_unavailable: d.cloudUnavailable,
      session_busy: d.sessionBusy, no_context: d.noContext, no_store: d.noStore,
      session_not_found: d.sessionNotFound, decline: d.requestDeclined,
      missing_description: d.missingDescription, description_too_long: d.descriptionTooLong,
    };
    return map[detail] || detail || `${(error && error.status) || ""}`;
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
    for (const e of events) {
      const p = e.payload || {};
      chars += (p.text || p.delta || p.summary || p.raw_text || p.goal || "").length;
      if (!p.text && !p.delta && !p.summary && !p.raw_text && !p.goal) chars += JSON.stringify(p).length;
    }
    return Math.round(chars / 4);
  }
  function contextLimitFor(options, selectedModel, fallbackModel) {
    const models = options || [];
    const wanted = String(selectedModel || fallbackModel || "").trim();
    const found = models.find((m) => m.value === wanted || m.id === wanted) || models.find((m) => m.context_length);
    const contextLength = Number(found && found.context_length);
    const outputReserve = Number(found && found.max_tokens);
    if (Number.isFinite(contextLength) && contextLength > 0) {
      return Number.isFinite(outputReserve) && outputReserve > 0 && outputReserve < contextLength
        ? contextLength - outputReserve
        : contextLength;
    }
    return DEFAULT_CONTEXT_TOKENS;
  }
  function tokenK(value) {
    const n = Math.max(0, Number(value) || 0);
    if (n >= 1000) return `${Math.round(n / 100) / 10}k`;
    return `${Math.round(n)}`;
  }
  function displayAgent(agentType, d) {
    if (!agentType || agentType === "pm-agent") return d.agentAuto;
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
  function shellQuote(value) {
    const text = String(value || "");
    return /\s|["']/.test(text) ? `"${text.replace(/"/g, '\\"')}"` : text;
  }
  function commandLine(value) {
    if (Array.isArray(value)) return value.map(shellQuote).join(" ");
    return String(value || "").trim();
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
      if (tok.startsWith("`")) nodes.push(html`<code key=${key}>${tok.slice(1, -1)}</code>`);
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
          status: "active", reply: "", commands: [], diffs: [], ts: e.ts, started: e.ts,
        });
      }
      return calls.get(k);
    };

    for (const e of events) {
      const t = e.type;
      const p = e.payload || {};
      if (t === "dispatch") {
        const autoAgent = p.pm_agent && !(Array.isArray(p.direct_agents) && p.direct_agents.length);
        nodes.push({ kind: "user", id: e.id || `u-${nodes.length}`, ts: e.ts, goal: p.goal || "", chips: [autoAgent ? d.agentAuto : p.agent, p.model, p.effort].filter(Boolean) });
      } else if (t === "pm_plan") {
        hidePmStatus("plan");
        const steps = Array.isArray(p.todo) ? p.todo.map((x) => String(x)) : (typeof p.todo === "string" && p.todo ? [p.todo] : []);
        lastPlan = { steps, summary: p.summary || "", instruction: p.instruction || "" };
        todos = todoRowsFrom(p.todo_status, steps);
        nodes.push({ kind: "plan", id: e.id || `p-${nodes.length}`, ts: e.ts, steps, summary: p.summary || "", deliberation: Array.isArray(p.deliberation) ? p.deliberation : [], instruction: p.instruction || "" });
      } else if (t === "pm_review") {
        // The PM's post-run review (after each agent run): show its verdict + summary/follow-up so
        // "done" / "needs follow-up" status surfaces in the thread (DispatchService._emit_pm_review).
        const status = p.done ? (lang === "zh" ? "复查通过" : "review passed") : (lang === "zh" ? "需要跟进" : "needs follow-up");
        const txt = [`**${status}**`, p.summary || "", p.reason || "", p.follow_up ? `→ ${p.follow_up}` : ""].filter(Boolean).join("\n\n");
        todos = mergeTodoRows(todos, p.todo_status, !!p.done);
        if (p.done) hidePmStatus();
        nodes.push({ kind: "pm", id: e.id || `pr-${nodes.length}`, ts: e.ts, text: txt });
      } else if (t === "pm_output" || t === "pm_reasoning") {
        const rawTxt = extractAgentText(p);
        if (!rawTxt) continue;
        if (p.event_type === "status" || p.status === "working") {
          const key = p.phase || p.stream_id || "pm";
          const statusText = displayPmStreamText(rawTxt, lang, d);
          if (statusNodes.has(key) && nodes[statusNodes.get(key)]) {
            nodes[statusNodes.get(key)].text = statusText;
            nodes[statusNodes.get(key)].ts = e.ts;
          } else {
            statusNodes.set(key, nodes.length);
            nodes.push({ kind: "pm-status", id: e.id || `ps-${nodes.length}`, ts: e.ts, text: statusText });
          }
          continue;
        }
        if (t === "pm_reasoning") continue;
        const sid = p.stream_id || "";
        const gk = `${t}-${e.source || ""}-${sid || "plain"}`;
        const txt = displayPmStreamText(cleanPmStreamText(sid ? `${pmStreamBuffers.get(gk) || ""}${rawTxt}` : rawTxt), lang, d);
        if (sid) pmStreamBuffers.set(gk, `${pmStreamBuffers.get(gk) || ""}${rawTxt}`);
        if (!txt) continue;
        if (p.phase) hidePmStatus(p.phase);
        if (sid && streamGroups.has(gk)) {
          const idx = streamGroups.get(gk);
          nodes[idx].text = txt;
        } else {
          const node = { kind: "pm", id: e.id || `pm-${nodes.length}`, ts: e.ts, text: txt };
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
          terminal.push({ kind: "cmd", text: cmd, ts: e.ts, agent: e.source, cwd });
        }
        c.ts = e.ts;
        if (!nodes.some((n) => n.kind === "call" && n.callId === c.id)) nodes.push({ kind: "call", id: `call-${c.id}`, callId: c.id, ts: e.ts });
      } else if (t === "agent_output" || t === "agent_reasoning") {
        const c = ensureCall(e);
        const txt = extractAgentText(p);
        if (txt) c.reply = c.reply ? `${c.reply}\n${txt}` : txt;
        if (txt && t === "agent_output") terminal.push({ kind: "out", text: txt, ts: e.ts, agent: e.source });
        c.ts = e.ts;
        if (!nodes.some((n) => n.kind === "call" && n.callId === c.id)) nodes.push({ kind: "call", id: `call-${c.id}`, callId: c.id, ts: e.ts });
      } else if (t === "tool_pre") {
        const c = ensureCall(e);
        const cmd = p.command || p.cmd || (p.tool && p.input ? `${p.tool} ${typeof p.input === "string" ? p.input : JSON.stringify(p.input)}` : "") || p.tool || "";
        if (cmd) { c.commands.push(String(cmd)); terminal.push({ kind: "cmd", text: String(cmd), ts: e.ts }); }
      } else if (t === "tool_post") {
        const out = p.output || p.result || "";
        if (out) terminal.push({ kind: "out", text: String(out).slice(0, 4000), ts: e.ts, agent: e.source });
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
        for (const c of calls.values()) if (c.status === "active") c.status = "done";
        const out = terminalText(p);
        if (out) terminal.push({ kind: "out", text: out, ts: e.ts, agent: e.source });
        nodes.push({ kind: "system", id: e.id || `s-${nodes.length}`, ts: e.ts, label: d.ev_stop, tone: "green", text: "" });
      } else if (t === "error") {
        hidePmStatus();
        const out = terminalText(p);
        if (out) terminal.push({ kind: "err", text: out, ts: e.ts, agent: e.source });
        nodes.push({ kind: "system", id: e.id || `e-${nodes.length}`, ts: e.ts, label: d.ev_error, tone: "red", text: p.msg || p.error || "" });
      } else if (t === "notification") {
        const label = p.label || p.title || (p.kind === "cancelled" ? d.sessionCanceled : d.notification);
        nodes.push({ kind: "system", id: e.id || `n-${nodes.length}`, ts: e.ts, label, tone: "muted", text: p.msg || p.text || "" });
      } else if (["checkpoint", "gate", "action_executed", "action_undone", "review", "audit", "undo", "recover", "stall", "context_compact"].includes(t)) {
        nodes.push({ kind: "system", id: e.id || `sy-${nodes.length}`, ts: e.ts, label: d[`ev_${t}`] || t, tone: "muted", text: p.summary || p.note || p.disposition || "" });
      }
    }

    if (!todos.length && lastPlan) todos = todoRowsFrom([], lastPlan.steps);

    // subagents from calls
    const subagents = Array.from(calls.values()).map((c) => ({
      id: c.id, name: c.reply ? firstSubstantiveLine(c.reply) : c.agent,
      agent: c.agent, status: c.status,
      act: c.commands.length ? c.commands[c.commands.length - 1] : (c.reply ? firstSubstantiveLine(c.reply) : ""),
      detail: c.reply || "",
    }));

    return { nodes: nodes.filter((n) => !n.hidden), calls, todos, terminal, subagents };
  }

  // ---------------------------------------------------------------------------
  // small UI atoms
  // ---------------------------------------------------------------------------
  function Empty({ icon, text }) { return html`<div className="empty"><div className="empty-icon">${icon || "✶"}</div><div>${text}</div></div>`; }
  function Switch({ on, onChange }) { return html`<button className=${`switch${on ? " on" : ""}`} onClick=${() => onChange(!on)} aria-pressed=${on} type="button"></button>`; }

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
          <div className="launch-foot">v${steps.version || "0.1.0"} · ${location.host}</div>
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

  function Sidebar({ d, lang, view, onView, counts, sessions, selected, onSelect, onNew }) {
    return html`
      <aside className="sidebar desktop">
        <div className="sb-brand">
          <div className="name">Foreman</div>
          <div className="sub">${d.productSubtitle} · v0.1.0</div>
        </div>
        <${NavList} d=${d} view=${view} onView=${onView} counts=${counts} />
        <div className="sb-section"><span>${d.sessions}</span><span className="add" onClick=${onNew} title=${d.newSession}>+</span></div>
        <div className="sb-sessions">
          ${!sessions.length ? html`<${Empty} icon="✉" text=${d.noActiveSession} />` :
            sessions.map((s) => html`<${SessionItem} key=${s.id} s=${s} d=${d} lang=${lang} active=${s.id === selected} onClick=${() => onSelect(s.id)} />`)}
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
    if (st.includes("fail") || st.includes("error")) return d.failed;
    if (st.includes("done") || st.includes("complete")) return d.done;
    if (st.includes("queue")) return d.queued;
    return status || "-";
  }

  function SessionItem({ s, d, lang, active, onClick }) {
    const st = (s.status || "").toLowerCase();
    const dotColor = st.includes("run") || st.includes("active") ? "var(--accent)" : (s.pending_approvals || s.open_cards) ? "var(--amber)" : st.includes("done") || st.includes("complete") ? "var(--green)" : "var(--faint)";
    const live = st.includes("run") || st.includes("active");
    const metaBits = [s.agent_type || "-", sessionStatusLabel(s.status, d), formatTime(s.updated_at || s.last_event_ts || s.created_at, lang)].filter(Boolean);
    return html`
      <div className=${`sess${active ? " active" : ""}`} onClick=${onClick}>
        <div className="sess-head">
          <span className=${`dot${live ? " live" : ""}`} style=${{ background: dotColor }}></span>
          <span className="sess-title">${s.goal || s.id}</span>
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
      cards, approvals, onCancelSession, onRetrySession, onDeleteSession, topControls } = props;
    const threadNodes = threadExtras(dig, cards, approvals, sessionRow);
    const agentType = displayAgent(sessionRow && sessionRow.agent_type, d);
    const status = String((sessionRow && sessionRow.status) || "").toLowerCase();
    const statusKey = status.replace(/[\s-]+/g, "_");
    const live = sessionRow && ["planning", "queued", "running", "active", "waiting_approval"].includes(statusKey);
    const failed = status.includes("fail") || status.includes("error");
    const cancelled = status.includes("cancel");
    const done = status.includes("done") || status.includes("complete");
    const statusText = live ? d.running : cancelled ? d.cancelled : failed ? d.failed : done ? d.done : ((sessionRow && sessionRow.status) || "");
    const onBars = Math.max(0, Math.min(4, autonomy + 1));
    const autonomyName = d[`auto${autonomy}`] || `L${autonomy}`;
    return html`
      <div className="main">
        <div className="sess-header">
          <div style=${{ minWidth: 0 }}>
            <div style=${{ display: "flex", alignItems: "center", gap: "9px" }}>
              <h2>${sessionRow ? (sessionRow.goal || sessionRow.id) : d.navWorkspace}</h2>
              ${sessionRow ? html`<span className=${`tag ${failed ? "red" : done ? "green" : "plain"}`}><span className=${`dot${live ? " live" : ""}`} style=${{ background: failed ? "var(--red)" : done ? "var(--green)" : "var(--faint)" }}></span>${statusText}</span>` : null}
            </div>
            <div className="meta">${sessionRow ? `${shortPath(sessionRow.workspace, d)} · ${agentType}` : d.workspaceSubtitle}</div>
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
          ${sessionRow && live ? html`<button className="btn danger" onClick=${() => onCancelSession(sessionRow.id)}>${d.cancelSession}</button>` : null}
          ${sessionRow && failed ? html`<button className="btn primary" onClick=${() => onRetrySession(sessionRow)}>${d.retry}</button>` : null}
          ${sessionRow && !live ? html`<button className="btn" onClick=${() => onDeleteSession(sessionRow.id)}>${d.deleteSession}</button>` : null}
        </div>

        <div className="ws-body">
          <div className="ws-left">
            <div className="thread">
              <div className="thread-inner">
                ${!threadNodes.length ? html`<${Empty} icon="◳" text=${d.selectSessionHint} />` :
                  threadNodes.map((n) => html`<${ThreadNode} key=${n.id} n=${n} dig=${dig} d=${d} lang=${lang} openCalls=${openCalls} toggleCall=${toggleCall} onCard=${onCard} onApproval=${onApproval} openDetail=${openDetail} />`)}
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
            </div>
            <div className="rp-body">
              ${rightTab === "todo" ? html`<${TodoPanel} d=${d} todos=${dig.todos} onAddStep=${composer.onAddStep} />` : null}
              ${rightTab === "sub" ? html`<${SubPanel} d=${d} subagents=${dig.subagents} expandedSub=${expandedSub} toggleSub=${toggleSub} />` : null}
              ${rightTab === "term" ? html`<${TermPanel} d=${d} terminal=${dig.terminal} agentType=${agentType} sessionRow=${sessionRow} />` : null}
            </div>
          </aside>
        </div>
      </div>`;
  }

  function ThreadNode({ n, dig, d, lang, openCalls, toggleCall, onCard, onApproval, openDetail }) {
    if (n.kind === "user") {
      return html`<div className="bubble-user"><div className="body">
        ${n.goal}
        ${n.chips.length ? html`<div className="chips">${n.chips.map((c, i) => html`<span className="chip" key=${i}>${c}</span>`)}</div>` : null}
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
      return html`<div className="pm-status"><span className="spin"></span><span>${n.text}</span></div>`;
    }
    if (n.kind === "pm") {
      return html`<div className="pm-note"><div className="pm-avatar">PM</div><div className="body"><${MD} text=${n.text} maxChars=${4000} /></div></div>`;
    }
    if (n.kind === "call") {
      const c = dig.calls.get(n.callId);
      if (!c) return null;
      const open = !!openCalls[c.id];
      const avatarColor = c.agent && c.agent.toLowerCase().includes("codex") ? "var(--violet)" : "var(--accent)";
      const avatar = (c.agent || "A").slice(0, 1).toUpperCase();
      return html`<div className=${`call${open ? " open" : ""}`}>
        <div className="call-head" onClick=${() => toggleCall(c.id)}>
          <span className="call-avatar" style=${{ background: avatarColor }}>${avatar}</span>
          <div style=${{ flex: 1, minWidth: 0 }}>
            <div className="call-title">
              <span className="call-agent">${c.agent}</span>
              <span className=${`tag ${c.status === "active" ? "accent" : "green"}`}>${c.status === "active" ? d.running : d.done}</span>
            </div>
            <div className="call-summary">${c.commands.length} cmd${c.diffs.length ? ` · ${c.diffs.length} diff` : ""}${c.reply ? ` · ${c.reply.slice(0, 50)}` : ""}</div>
          </div>
          <span className="call-toggle">${open ? d.hide : d.open}${open ? " ▾" : " ▸"}</span>
        </div>
        ${open ? html`<div className="call-detail">
          ${c.reply ? html`<div><div className="detail-label">${d.reply}</div><${MD} text=${c.reply} maxChars=${6000} /></div>` : null}
          ${c.commands.length ? html`<div><div className="detail-label">${d.commandsRun}</div><div className="term-block">${c.commands.map((cmd, i) => html`<div key=${i}><span className="cmd-prompt">$</span> ${cmd}</div>`)}</div></div>` : null}
          ${c.diffs.length ? html`<div><div className="detail-label">${d.fileChanges}</div>${c.diffs.map((df, i) => html`<div className="diff-file" key=${i}><div className="fhead"><span className="muted" style=${{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>${df.file}</span><span className="stat">${df.stat}</span></div>${(df.lines || []).slice(0, 30).map((l, j) => html`<div className=${`diff-line ${l.kind === "add" ? "add" : l.kind === "del" ? "del" : ""}`} key=${j}>${l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}${l.text || ""}</div>`)}</div>`)}</div>` : null}
        </div>` : null}
      </div>`;
    }
    if (n.kind === "card") {
      const p = n.payload || {};
      const opts = Array.isArray(p.options) ? p.options : [];
      return html`<div className="dcard">
        <div className="dcard-head"><span>⚠️</span><span className="ttl">${d.decisionNeeded}</span><span className="risk tag amber">${d.riskMedium}</span></div>
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
    const doneCount = todos.filter((t) => t.status === "done").length;
    const pct = todos.length ? Math.round((doneCount / todos.length) * 100) : 0;
    const submit = () => { const v = val.trim(); if (!v) return; onAddStep(v); setVal(""); };
    return html`<div>
      ${todos.length ? html`<div className="todo-progress"><div className="track"><span style=${{ width: `${pct}%` }}></span></div><span className="lbl">${doneCount}/${todos.length}</span></div>` : null}
      ${!todos.length ? html`<${Empty} icon="☑" text=${d.selectSessionHint} /> ` :
        todos.map((t) => html`<div className=${`todo-row ${t.status}`} key=${t.id}>
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

  function TermPanel({ d, terminal, agentType, sessionRow }) {
    const lines = terminal.slice(-200);
    const prefix = (l) => [l.agent, l.cwd ? shortPath(l.cwd, d) : ""].filter(Boolean).join(" ");
    return html`<div className="term-full">
      <div className="bar"><span className="lbl">${d.readOnlyLog} · ${shortPath(sessionRow && sessionRow.workspace, d)} · ${agentType}</span></div>
      <div className="lines">
        ${!lines.length ? html`<div className="cmd-dim">${d.selectSessionHint}</div>` :
          lines.map((l, i) => html`<div key=${i} className=${l.kind === "err" ? "cmd-err" : l.kind === "out" ? "cmd-dim" : ""}>
            ${l.kind === "cmd" ? html`<span>${prefix(l) ? html`<span className="cmd-src">${prefix(l)}</span> ` : null}<span className="cmd-prompt">$</span> ${l.text}</span>` : html`<span>${prefix(l) ? html`<span className="cmd-src">${prefix(l)}</span> ` : null}${l.text}</span>`}
          </div>`)}
        <div className="cmd-note">›<span className="term-cursor"></span></div>
      </div>
    </div>`;
  }

  function Composer(props) {
    const { d, lang, workspaces, workspace, setWorkspace, task, setTask, model, setModel, modelOptions, llm, effort, setEffort,
      attachments, addAttach, removeAttach, dispatching, runDispatch, dispatchStatus, sessionRow, events,
      compacting, runCompact, compactStatus, definitions, selectedWorkModeIds, setSelectedWorkModeIds } = props;
    const wsOpts = workspaces.length ? workspaces : [];
    const [wmOpen, setWmOpen] = useState(false);
    // Only active definitions are pickable work modes; ignore archived/draft siblings.
    const wmOptions = (definitions || []).filter((x) => x && x.is_active);
    const wmSelected = selectedWorkModeIds || [];
    const toggleWm = (id) => {
      if (!setSelectedWorkModeIds) return;
      setSelectedWorkModeIds(wmSelected.includes(id) ? wmSelected.filter((x) => x !== id) : [...wmSelected, id]);
    };
    const wmDesc = (row) => { try { const m = JSON.parse(row.metadata_json || "{}"); if (m && m.description) return m.description; } catch (e) {} return (row.body || "").slice(0, 80); };
    const est = estTokens(events || []);
    const contextLimit = contextLimitFor(modelOptions, model, llm && llm.model);
    const pct = Math.min(95, Math.round((est / contextLimit) * 100));
    const onKey = (e) => {
      if (e.key === "@") { e.preventDefault(); addAttach(); return; }
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); runDispatch(); }
    };
    return html`<div className="composer">
      <div className="composer-inner">
        ${(events && events.length) ? html`<div className="ctx-meter">
          <span>${d.context}</span>
          <div className="track"><span style=${{ width: `${pct}%` }}></span></div>
          <span>≈${tokenK(est)} / ${tokenK(contextLimit)}</span>
          <button className="btn ghost sm" style=${{ marginLeft: "auto" }} onClick=${runCompact} disabled=${compacting || !sessionRow}>⟲ ${compacting ? d.compacting : d.compact}</button>
        </div>` : null}
        ${compactStatus ? html`<div className=${`alert ${compactStatus.includes(d.compactFailed) ? "error" : "info"}`} style=${{ marginBottom: 9 }}>${compactStatus}</div>` : null}
        ${dispatchStatus ? html`<div className=${`alert ${dispatchStatus.includes(d.dispatchFailed) ? "error" : "ok"}`} style=${{ marginBottom: 9 }}>${dispatchStatus}</div>` : null}
        <div className="composer-box">
          ${attachments.length ? html`<div className="composer-attach">${attachments.map((a) => html`<div className="attach-chip" key=${a.id}><span className=${`ic ${a.isImage ? "img" : "file"}`}>${a.isImage ? "🖼" : "📄"}</span><span className="nm">${a.name}</span><span className="rm" onClick=${() => removeAttach(a.id)}>×</span></div>`)}</div>` : null}
          <textarea className="composer-input" rows="2" value=${task} onChange=${(e) => setTask(e.target.value)} onKeyDown=${onKey} placeholder=${d.composerPlaceholder}></textarea>
          <div className="composer-tools">
            <button className="tool-chip" onClick=${addAttach}>📎 ${d.attach}</button>
            ${wsOpts.length ? html`<select className="ws-select" value=${workspace} onChange=${(e) => setWorkspace(e.target.value)} disabled=${!!sessionRow}>${wsOpts.map((w) => html`<option key=${w.path} value=${w.path}>📁 ${w.name || shortPath(w.path, d)}</option>`)}</select>` : null}
            <input className="ws-select model-pick" value=${model} onChange=${(e) => setModel(e.target.value)} list="composer-models" placeholder=${d.modelPlaceholder} aria-label=${d.model} />
            <datalist id="composer-models">${(modelOptions || []).map((o) => html`<option key=${o.value} value=${o.value}></option>`)}</datalist>
            <span className="tool-chip dashed">🤖 ${d.agentAuto}</span>
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
            <div className="seg">
              <button className=${`opt${effort === "low" ? " on" : ""}`} onClick=${() => setEffort("low")}>${d.fast}</button>
              <button className=${`opt${effort === "medium" ? " on" : ""}`} onClick=${() => setEffort("medium")}>${d.std}</button>
              <button className=${`opt${effort === "high" ? " on" : ""}`} onClick=${() => setEffort("high")}>${d.deep}</button>
            </div>
            <div style=${{ flex: 1 }}></div>
            <span className="composer-send-hint">⏎ ${d.sendHint}</span>
            <button className="btn primary" onClick=${runDispatch} disabled=${dispatching}>${d.send} ↑</button>
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
          cards.map((c) => html`<div className="dcard" key=${c.id}>
            <div className="dcard-head"><span>⚠️</span><span className="ttl">${d.decisionNeeded}</span>
              ${c.session_id ? html`<span className="dcard-link" onClick=${() => onGoSession(c.session_id)}>↗ ${d.fromSession}</span>` : null}
              <span className="risk tag amber">${d.riskMedium}</span></div>
            <div className="dcard-body">
              <div className="q"><${MD} text=${c.summary || ""} className="markdown-compact" /></div>
              ${c.audit_note ? html`<div className="d"><${MD} text=${c.audit_note} className="markdown-compact" /></div>` : null}
              ${c.diff_stat ? html`<div style=${{ marginBottom: 13 }}><span className="tag plain">${c.diff_stat}</span></div>` : null}
              <div className="dcard-actions">
                ${(c.options || []).map((o, i) => html`<button key=${i} className=${`btn${i === 0 ? " primary" : ""}`} onClick=${() => onCard(c.id, o.action)}>${o.label || o.action}</button>`)}
                ${c.action_id ? html`<button className="btn ghost" onClick=${() => openDetail(c.action_id)}>${d.showDiff}</button>` : null}
              </div>
            </div>
          </div>`)}
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
    return html`<div className="page-mid">
      <div className="pb-toolbar">
        ${pills.map(([v, l]) => html`<span key=${v} className=${`pill${filter === v ? " on" : ""}`} onClick=${() => setFilter(v)}>${d[l]}</span>`)}
        <div style=${{ flex: 1 }}></div>
        <button className="btn sm" onClick=${onImportClick}>↑ ${d.importBtn}</button>
        <button className="btn sm" onClick=${onExport}>↓ ${d.exportBtn}</button>
        <button className="btn primary sm" onClick=${onNew}>+ ${d.newBtn}</button>
        <input ref=${fileRef} type="file" accept="application/json,.json" hidden onChange=${onImport} />
      </div>
      ${!definitions.length ? html`<${Empty} icon="▦" text=${d.noDefinitions} />` :
        html`<div className="pb-grid">${definitions.map((row) => html`<div className="pb-card" key=${row.id}>
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
      cloud, setCloud, saveCloud, connectCloud, disconnectCloud, clearCloudKey, cloudStatus, cloudAvailable,
      autonomy, saveAutonomy, theme, setTheme, lang2, setLang } = props;
    const updateAgent = (name, patch) => setAgentSettings((rows) => (rows || []).map((r) => (r.name === name ? { ...r, ...patch } : r)));
    const updatePmTools = (patch) => setPmTools((cur) => ({ ...(cur || {}), ...patch }));
    const lines = (value) => Array.isArray(value) ? value.join("\n") : "";
    const splitLines = (value) => String(value || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
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
            <input className="input mono" value=${llm.model || ""} onChange=${(e) => setLlm({ ...llm, model: e.target.value })} list="pm-models" placeholder="gpt-5.5" />
            <datalist id="pm-models">${(pmModelOptions || []).map((o) => html`<option key=${o.value} value=${o.value}></option>`)}</datalist>
          </div>
        </div>
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.baseUrl}</span><input className="input mono" value=${llm.base_url || ""} onChange=${(e) => setLlm({ ...llm, base_url: e.target.value })} placeholder="https://api.openai.com/v1" /></div>
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.transport}</span>
          <select className="select" value=${llm.transport || "http"} onChange=${(e) => setLlm({ ...llm, transport: e.target.value })}><option value="http">HTTP</option><option value="ws">WS stream</option></select>
        </div>
        <div className="field" style=${{ marginBottom: 13 }}><span className="field-label">${d.reasoningEffort}</span>
          <select className="select" value=${llm.reasoning_effort || ""} onChange=${(e) => setLlm({ ...llm, reasoning_effort: e.target.value })}><option value="">${d.effortDefault}</option><option value="low">${d.fast}</option><option value="medium">${d.std}</option><option value="high">${d.deep}</option><option value="max">max</option></select>
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
          <div className="field"><span className="field-label">${d.allowedCommands}</span><textarea className="input mono" style=${{ minHeight: 92 }} value=${lines(pmTools.allowed_commands)} onChange=${(e) => updatePmTools({ allowed_commands: splitLines(e.target.value) })}></textarea></div>
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
          <label style=${{ display: "flex", gap: 8, alignItems: "center", fontSize: 12.5 }}>${d.maxRounds}<input className="input mono" style=${{ width: 76 }} value=${pmTools.max_rounds || 6} onChange=${(e) => updatePmTools({ max_rounds: Number(e.target.value) || 6 })} /></label>
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
  function Modal({ title, onClose, children, footer, wide }) {
    return html`<div className="modal-mask" onClick=${onClose}>
      <div className=${`modal${wide ? " wide" : ""}`} onClick=${(e) => e.stopPropagation()}>
        <div className="modal-head"><span className="t">${title}</span><span className="x" onClick=${onClose}>×</span></div>
        <div className="modal-body">${children}</div>
        ${footer ? html`<div className="modal-foot">${footer}</div>` : null}
      </div>
    </div>`;
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

  // ===========================================================================
  // Mobile shell
  // ===========================================================================
  function MobileShell(props) {
    const { d, lang, view, setView, mTab, setMTab, drawerOpen, setDrawerOpen, counts, sessionRow,
      dig, mainProps, sessions, selected, onSelect, onNew } = props;
    const titles = { workspace: sessionRow ? (sessionRow.goal || d.navWorkspace) : d.navWorkspace, decisions: d.navDecisions, briefings: d.navBriefings, rules: d.navRules, settings: d.navSettings };
    const live = sessionRow && (sessionRow.status || "").toLowerCase().match(/run|active/);
    return html`<div className="mobile">
      <div className="appbar">
        <button className="burger" onClick=${() => setDrawerOpen(true)}>☰</button>
        <div style=${{ flex: 1, minWidth: 0 }}><div className="ttl">${titles[view]}</div><div className="sub">${view === "workspace" && sessionRow ? `${sessionRow.agent_type || ""}` : ""}</div></div>
        ${view === "workspace" && live ? html`<span className="tag green"><span className="dot live" style=${{ background: "var(--green)" }}></span>LIVE</span>` : null}
      </div>
      ${drawerOpen ? html`<div className="m-drawer-mask" onClick=${() => setDrawerOpen(false)}></div>
        <div className="m-drawer">
          <div className="sb-brand"><div className="name">Foreman</div><div className="sub">${d.productSubtitle}</div></div>
          <${NavList} d=${d} view=${view} onView=${(k) => { setView(k); setDrawerOpen(false); }} counts=${counts} />
          <div className="sb-section" style=${{ marginTop: 18 }}><span>${d.sessions}</span><span className="add" onClick=${() => { onNew(); setDrawerOpen(false); }} title=${d.newSession}>+</span></div>
          <div className="sb-sessions" style=${{ flex: "0 1 auto", maxHeight: "40vh" }}>
            ${!(sessions || []).length ? html`<${Empty} icon="✉" text=${d.noActiveSession} />` :
              sessions.map((s) => html`<${SessionItem} key=${s.id} s=${s} d=${d} lang=${lang} active=${s.id === selected} onClick=${() => { onSelect(s.id); setDrawerOpen(false); }} />`)}
          </div>
          <div className="sb-user" style=${{ marginTop: "auto" }}><div className="avatar">J</div><div><div className="uname">jiang</div><div className="urole">${d.personalMode}</div></div></div>
        </div>` : null}
      <div className="m-body">
        ${view === "workspace" ? html`<${MobileWorkspace} d=${d} lang=${lang} dig=${dig} mTab=${mTab} mainProps=${mainProps} />` : null}
        ${view === "decisions" ? html`<div style=${{ padding: 13 }}><${Decisions} ...${mainProps.decisions} /></div>` : null}
        ${view === "briefings" ? html`<div style=${{ padding: 13 }}>${mainProps.briefingsTop}<${Briefings} ...${mainProps.briefings} /></div>` : null}
        ${view === "rules" ? html`<div style=${{ padding: 13 }}><${Playbook} ...${mainProps.playbook} /></div>` : null}
        ${view === "settings" ? html`<div style=${{ padding: 13 }}><${Settings} ...${mainProps.settings} /></div>` : null}
      </div>
      ${view === "workspace" && mTab === "chat" ? html`<div className="m-composer">
        <button className="burger" onClick=${mainProps.composer.addAttach}>📎</button>
        <div className="box"><input value=${mainProps.composer.task} onChange=${(e) => mainProps.composer.setTask(e.target.value)} onKeyDown=${(e) => { if (e.key === "@") { e.preventDefault(); mainProps.composer.addAttach(); return; } if (e.key === "Enter") { e.preventDefault(); mainProps.composer.runDispatch(); } }} placeholder=${d.mComposerPlaceholder} /></div>
        <button className="btn primary icon" onClick=${mainProps.composer.runDispatch} disabled=${mainProps.composer.dispatching}>↑</button>
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
    const threadNodes = threadExtras(dig, mainProps.cards, mainProps.approvals, mainProps.sessionRow);
    if (mTab === "chat") return html`<div className="thread" style=${{ padding: 13 }}><div className="thread-inner">
      ${!threadNodes.length ? html`<${Empty} icon="◳" text=${d.selectSessionHint} />` :
        threadNodes.map((n) => html`<${ThreadNode} key=${n.id} n=${n} dig=${dig} d=${d} lang=${lang} openCalls=${mainProps.openCalls} toggleCall=${mainProps.toggleCall} onCard=${mainProps.onCard} onApproval=${mainProps.onApproval} openDetail=${mainProps.openDetail} />`)}
    </div></div>`;
    if (mTab === "todo") return html`<div style=${{ padding: 13 }}><${TodoPanel} d=${d} todos=${dig.todos} onAddStep=${mainProps.composer.onAddStep} /></div>`;
    if (mTab === "sub") return html`<div style=${{ padding: 13 }}><${SubPanel} d=${d} subagents=${dig.subagents} expandedSub=${mainProps.expandedSub} toggleSub=${mainProps.toggleSub} /></div>`;
    return html`<div style=${{ padding: 13 }}><${TermPanel} d=${d} terminal=${dig.terminal} agentType=${displayAgent(mainProps.sessionRow && mainProps.sessionRow.agent_type, d)} sessionRow=${mainProps.sessionRow} /></div>`;
  }

  // ===========================================================================
  // Shell
  // ===========================================================================
  function Shell() {
    const storedLang = localStorage.getItem(LANG_KEY);
    const [lang, setLangState] = useState(storedLang === "en" ? "en" : "zh");
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

    const [status, setStatus] = useState({ online: false, version: "0.1.0" });
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
    const [llm, setLlm] = useState({ provider: "openai", model: "", base_url: "", transport: "http", reasoning_effort: "", api_key_set: true, api_key: "" });
    const [llmStatus, setLlmStatus] = useState("");
    const [agentStatus, setAgentStatus] = useState("");
    const [pmTools, setPmTools] = useState({ file_read: true, file_write: false, shell: false, web_fetch: false, web_search: false, browser: false, allowed_commands: ["python --version"], allowed_origins: [], web_search_provider: "duckduckgo", searxng_url: "", browser_headless: false, max_rounds: 6 });
    const [pmToolsStatus, setPmToolsStatus] = useState("");
    const [debugSettings, setDebugSettings] = useState({ llm_trace: false });
    const [debugStatus, setDebugStatus] = useState("");
    const [cloud, setCloud] = useState({ url: "", access_key: "", access_key_set: false, connected: false });
    const [cloudStatus, setCloudStatus] = useState("");
    const [cloudAvailable, setCloudAvailable] = useState(true);
    const [autonomy, setAutonomyState] = useState(1);
    const [detailOpen, setDetailOpen] = useState(false);
    const [detail, setDetail] = useState({ raw: [], diff: { files: [] } });
    const [defnOpen, setDefnOpen] = useState(false);
    const [defnDraft, setDefnDraft] = useState(null);
    const [confirmDefnDelete, setConfirmDefnDelete] = useState(null);
    const [wfRun, setWfRun] = useState(null);  // P5: current workflow run view (null = closed)
    const [confirmSessionDelete, setConfirmSessionDelete] = useState(null);
    const [openCalls, setOpenCalls] = useState({});
    const [expandedSub, setExpandedSub] = useState(null);
    const [toasts, setToasts] = useState([]);
    const wsRef = useRef(null);
    const fileRef = useRef(null);
    const toastSeq = useRef(0);

    const toast = useCallback((text, type) => {
      const id = ++toastSeq.current;
      setToasts((p) => [...p, { id, text, type }]);
      setTimeout(() => setToasts((p) => p.filter((t) => t.id !== id)), 3200);
    }, []);
    const notifyError = useCallback((e) => toast(friendlyError(e, I18N[lang]), "error"), [lang, toast]);

    useEffect(() => { document.documentElement.setAttribute("data-theme", theme); }, [theme]);
    const setTheme = (t) => { setThemeState(t); localStorage.setItem(THEME_KEY, t); };
    const setLang = (l) => setLangState(l);

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
    const loadModels = useCallback(async () => { try { const data = await api("/api/models"); setModelOptions((data && data.models || []).map((m) => ({ value: m.id, id: m.id, context_length: m.context_length, max_tokens: m.max_tokens, source: m.source }))); } catch (e) { setModelOptions([]); } }, []);
    const loadPmModels = useCallback(async (draft) => {
      const cur = draft || {};
      const body = { provider: cur.provider || "openai", model: (cur.model || "").trim(), base_url: (cur.base_url || "").trim(), transport: cur.transport || "http", reasoning_effort: cur.reasoning_effort || "" };
      if ((cur.api_key || "").trim()) body.api_key = cur.api_key.trim();
      try { const data = await api("/api/models/preview", { method: "POST", body }); setPmModelOptions((data && data.models || []).map((m) => ({ value: m.id, id: m.id, context_length: m.context_length, max_tokens: m.max_tokens, source: m.source }))); } catch (e) { setPmModelOptions([]); }
    }, []);
    const loadSessions = useCallback(async () => { try { try { setSessions(await api("/api/overview") || []); } catch (e) { setSessions(await api("/api/sessions") || []); } } catch (e) { setSessions([]); } }, []);
    const loadCards = useCallback(async () => { try { setCards(await api("/api/cards") || []); } catch (e) { setCards([]); } }, []);
    const loadApprovals = useCallback(async () => { try { setApprovals(await api("/api/approvals") || []); } catch (e) { setApprovals([]); } }, []);
    const loadReports = useCallback(async () => { try { setReports(await api("/api/reports") || []); } catch (e) { setReports([]); } }, []);
    const loadDefinitions = useCallback(async () => { try { const path = defnFilter ? `/api/definitions?kind=${encodeURIComponent(defnFilter)}` : "/api/definitions"; setDefinitions(await api(path) || []); } catch (e) { setDefinitions([]); } }, [defnFilter]);
    const loadLlm = useCallback(async () => { try { const next = { ...(await api("/api/settings/llm")), api_key: "" }; setLlm(next); await loadPmModels(next); } catch (e) { /* server mode */ } }, [loadPmModels]);
    const loadAutonomy = useCallback(async () => { try { setAutonomyState((await api("/api/settings/autonomy")).level); } catch (e) { /* keep */ } }, []);
    const loadCloud = useCallback(async () => {
      try {
        const c = await api("/api/settings/cloud");
        setCloud({ url: c.url || "", access_key: "", access_key_set: !!c.access_key_set, connected: !!c.connected });
        setCloudAvailable(c.available !== false);
      } catch (e) { setCloudAvailable(false); }
    }, []);

    // boot
    useEffect(() => {
      if (localStorage.getItem(LANG_KEY)) { setLanguageLoaded(true); return; }
      api("/api/settings/language").then((data) => setLangState(data && data.language === "en" ? "en" : "zh")).catch(() => {}).finally(() => setLanguageLoaded(true));
    }, []);
    useEffect(() => { document.documentElement.lang = lang === "zh" ? "zh-CN" : "en"; if (!languageLoaded) return; localStorage.setItem(LANG_KEY, lang); api("/api/settings/language", { method: "POST", body: { language: lang } }).catch(() => {}); }, [lang, languageLoaded]);

    useEffect(() => {
      if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
      api("/health").then((h) => setStatus({ online: true, version: h.version })).catch(() => setStatus({ online: false, version: "offline" }));
      // Boot on the essentials only. Model + agent discovery hit the provider's /models (or run a
      // CLI --version per agent) and can take the backend request timeout if a key is set but the
      // endpoint is slow — keeping them out of this barrier stops the launch overlay from hanging
      // (codex review finding). They populate the Settings page shortly after, non-blocking.
      Promise.allSettled([loadWorkspaces(), loadSessions(), loadCards(), loadApprovals(), loadReports(), loadAutonomy(), loadCloud()]).then(() => {
        setBooted(true);
        setTimeout(() => setHidingLaunch(true), 350);
      });
      loadAgentSettings();
      loadLlm();
      loadPmTools();
      loadDebug();
      loadModels();
    }, [loadWorkspaces, loadSessions, loadCards, loadApprovals, loadReports, loadAutonomy, loadCloud, loadAgentSettings, loadLlm, loadPmTools, loadDebug, loadModels]);
    useEffect(() => { loadDefinitions(); }, [loadDefinitions]);

    // polling for cards/approvals/sessions
    useEffect(() => {
      const id = setInterval(() => { loadSessions(); loadCards(); loadApprovals(); if (cloudAvailable) loadCloud(); }, 8000);
      return () => clearInterval(id);
    }, [loadSessions, loadCards, loadApprovals, loadCloud, cloudAvailable]);

    // deep-link approvals
    useEffect(() => {
      const params = new URLSearchParams(location.search);
      const id = params.get("approval"); const action = params.get("action");
      if (id || action) history.replaceState(null, "", location.pathname);
      if (!id || (action !== "approve" && action !== "reject")) return;
      loadApprovals().then(() => { const row = approvals.find((a) => a.id === id); if (row) decideApproval(row.id, action, row.nonce); });
    }, []); // eslint-disable-line

    function openTimeline(sessionId) {
      setSelectedSession(sessionId); setView("workspace"); setEvents([]);
      if (wsRef.current) { try { wsRef.current.close(); } catch (e) {} }
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const token = getToken();
      const tq = token ? `&token=${encodeURIComponent(token)}` : "";
      const next = new WebSocket(`${proto}://${location.host}/ws?session_id=${encodeURIComponent(sessionId)}${tq}`);
      next.addEventListener("message", (ev) => {
        try { const item = JSON.parse(ev.data); setEvents((prev) => { if (item.id && prev.some((r) => r.id === item.id)) return prev; return [...prev, item]; }); }
        catch (e) {}
      });
      next.addEventListener("error", () => {});
      wsRef.current = next;
    }
    function newSession() { setSelectedSession(""); setEvents([]); setDispatchStatus(""); setCompactStatus(""); setView("workspace"); }

    const sessionRow = useMemo(() => sessions.find((s) => s.id === selectedSession), [sessions, selectedSession]);
    const dig = useMemo(() => digest(events, d, lang), [events, d, lang]);
    // Only undecided cards are actionable — a card with `chosen` set is history (it still lives in
    // /api/cards), so it must not keep showing live approve/reject buttons in the thread or count.
    const openCards = useMemo(() => (cards || []).filter((c) => !c.chosen), [cards]);

    async function runDispatch() {
      const goalBase = task.trim();
      const attachRefs = attachments.map((a) => `@${a.name}`).join(" ");
      const goal = [goalBase, attachRefs].filter(Boolean).join(" ");
      if (!goal) { setDispatchStatus(d.emptyGoal); return; }
      const target = sessionRow ? (sessionRow.workspace || workspace) : workspace;
      if (!target) { setDispatchStatus(d.dispatchNoWorkspace); setView("settings"); return; }
      setDispatching(true);
      const body = { goal, workspace: target, source: clientSource(), effort };
      if (sessionRow) body.session_id = sessionRow.id;
      if (model.trim()) body.model = model.trim();
      // D4: manually-picked work modes ride along (backend accepts but doesn't consume yet — P1).
      if (selectedWorkModeIds && selectedWorkModeIds.length) body.work_mode_ids = selectedWorkModeIds;
      try {
        const res = await api("/api/tasks", { method: "POST", body });
        setTask(""); setAttachments([]);
        setDispatchStatus(sessionRow ? d.continued : d.dispatched);
        await loadSessions();
        if (res.session_id) openTimeline(res.session_id);
      } catch (e) { setDispatchStatus(`${d.dispatchFailed}: ${friendlyError(e, d)}`); }
      finally { setDispatching(false); }
    }
    async function retrySession(row) {
      if (!row || !row.goal) { setDispatchStatus(d.emptyGoal); return; }
      const target = row.workspace || workspace;
      if (!target) { setDispatchStatus(d.dispatchNoWorkspace); setView("settings"); return; }
      setDispatching(true);
      const body = { goal: row.goal, workspace: target, source: clientSource(), effort };
      if (row.model) body.model = row.model;
      try {
        const res = await api("/api/tasks", { method: "POST", body });
        setDispatchStatus(d.dispatched);
        await loadSessions();
        if (res.session_id) openTimeline(res.session_id);
      } catch (e) { setDispatchStatus(`${d.dispatchFailed}: ${friendlyError(e, d)}`); }
      finally { setDispatching(false); }
    }
    function onAddStep(text) {
      if (!sessionRow) { toast(d.selectSessionHint, "error"); return; }
      const body = { goal: text, workspace: sessionRow.workspace || workspace, source: clientSource(), session_id: sessionRow.id, effort };
      api("/api/tasks", { method: "POST", body }).then(() => { toast(d.continued, "success"); loadSessions(); }).catch(notifyError);
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
    async function confirmDeleteSession() {
      const id = confirmSessionDelete && confirmSessionDelete.id;
      if (!id) return;
      try {
        await api(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
        setConfirmSessionDelete(null);
        setSelectedSession("");
        setEvents([]);
        await loadSessions();
      } catch (e) { notifyError(e); }
    }

    async function onCard(cardId, option) {
      if (!cardId || !option) return;
      try { await api(`/api/cards/${encodeURIComponent(cardId)}/choose`, { method: "POST", body: { option } }); await loadCards(); toast(d.saved, "success"); }
      catch (e) { notifyError(e); }
    }
    async function decideApproval(id, decision, nonce) {
      try { await api(`/api/approvals/${encodeURIComponent(id)}`, { method: "POST", body: { decision, nonce: nonce || "" } }); await loadApprovals(); toast(d.saved, "success"); }
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
        const body = { provider: llm.provider || "openai", model: (llm.model || "").trim(), base_url: (llm.base_url || "").trim(), transport: llm.transport || "http", reasoning_effort: llm.reasoning_effort || "" };
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
      try { setAutonomyState((await api("/api/settings/autonomy", { method: "POST", body: { level: value } })).level); }
      catch (e) { notifyError(e); }
    }
    async function saveCloud() {
      try {
        const body = { url: (cloud.url || "").trim() };
        if ((cloud.access_key || "").trim()) body.access_key = cloud.access_key.trim();
        const c = await api("/api/settings/cloud", { method: "POST", body });
        setCloud({ url: c.url || "", access_key: "", access_key_set: !!c.access_key_set, connected: !!c.connected }); setCloudStatus(d.saved);
      } catch (e) { setCloudStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
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
      try { const c = await api("/api/settings/cloud", { method: "POST", body: { access_key: "" } }); setCloud({ url: c.url || "", access_key: "", access_key_set: !!c.access_key_set, connected: !!c.connected }); setCloudStatus(d.saved); }
      catch (e) { setCloudStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }

    async function enablePush() {
      if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) { toast(d.pushUnsupported, "error"); return; }
      try {
        const { key, enabled } = await api("/api/push/vapid-public-key");
        if (!enabled || !key) { toast(d.pushNotConfigured, "error"); return; }
        const perm = await Notification.requestPermission();
        if (perm !== "granted") { toast(d.pushDenied, "error"); return; }
        const reg = await navigator.serviceWorker.ready;
        let sub = await reg.pushManager.getSubscription();
        if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(key) });
        await api("/api/push/subscribe", { method: "POST", body: sub.toJSON ? sub.toJSON() : sub });
        toast(d.pushEnabled, "success");
        await reg.showNotification("Foreman", { body: d.pushNotifSub, icon: "/icon-192.png", badge: "/icon-192.png", tag: "foreman-push-test" });
      } catch (e) { toast(`${d.pushFailed}: ${friendlyError(e, d)}`, "error"); }
    }

    function addAttach() {
      const input = document.createElement("input"); input.type = "file"; input.accept = "*/*";
      input.onchange = () => { const f = input.files && input.files[0]; if (!f) return; setAttachments((p) => [...p, { id: Date.now(), name: f.name, isImage: /\.(png|jpe?g|gif|webp|svg)$/i.test(f.name) }]); };
      input.click();
    }
    const removeAttach = (id) => setAttachments((p) => p.filter((a) => a.id !== id));
    const toggleCall = (id) => setOpenCalls((s) => ({ ...s, [id]: !s[id] }));
    const toggleSub = (id) => setExpandedSub((cur) => (cur === id ? null : id));
    const onCopy = (text) => { try { navigator.clipboard.writeText(text); toast(d.copied, "success"); } catch (e) {} };

    const counts = { workspace: sessions.filter((s) => (s.status || "").toLowerCase().match(/run|active/)).length, decisions: openCards.length + approvals.length };

    const composerProps = {
      workspaces, workspace, setWorkspace: (v) => { setWorkspace(v); if (v) localStorage.setItem(WORKSPACE_KEY, v); },
      task, setTask, model, setModel, modelOptions, llm, effort, setEffort, attachments, addAttach, removeAttach,
      dispatching, runDispatch, dispatchStatus, onAddStep,
      definitions, selectedWorkModeIds, setSelectedWorkModeIds,
    };
    const settingsProps = {
      d, lang, workspaces, workspaceDraft, setWorkspaceDraft, saveWorkspace, browseFolder, deleteWorkspace, loadWorkspaces,
      agentSettings, setAgentSettings, saveAgentSettings, agentStatus, loadAgentSettings,
      llm, setLlm, pmModelOptions, saveLlm, clearLlmKey, llmStatus,
      pmTools, setPmTools, savePmTools, pmToolsStatus, loadPmTools,
      debugSettings, debugStatus, saveDebug,
      cloud, setCloud, saveCloud, connectCloud, disconnectCloud, clearCloudKey, cloudStatus, cloudAvailable,
      autonomy, saveAutonomy, theme, setTheme, lang2: lang, setLang, onPush: enablePush,
    };
    const decisionsProps = { d, lang, cards: openCards, approvals, onCard, onApproval: decideApproval, openDetail, onGoSession: openTimeline };
    const briefingsProps = { d, lang, reports, onCopy, toast };
    const playbookProps = { d, lang, definitions, filter: defnFilter, setFilter: setDefnFilter, onNew: () => { setDefnDraft({ kind: defnFilter || "workflow", scope_json: "{}", body: "", activate: true }); setDefnOpen(true); }, onEdit: (row) => { let desc = ""; try { desc = (JSON.parse(row.metadata_json || "{}") || {}).description || ""; } catch (e) {} setDefnDraft({ ...row, description: desc, activate: !!row.is_active }); setDefnOpen(true); }, onActivate: activateDefinition, onDelete: deleteDefinition, onExport: exportDefinitions, onImportClick: () => fileRef.current && fileRef.current.click(), fileRef, onImport: importDefinitions, onStartWorkflow: startWorkflowRun };

    const launchSteps = { engine: status.online, agents: agentsLoaded, data: booted, pct: booted ? 100 : (status.online ? 60 : 25), version: status.version };

    const mainProps = {
      decisions: decisionsProps, briefings: briefingsProps,
      briefingsTop: html`<button className="btn primary block" style=${{ marginBottom: 13 }} onClick=${runBriefing}>✦ ${d.generate}</button>`,
      playbook: playbookProps, settings: settingsProps, composer: composerProps,
      openCalls, toggleCall, expandedSub, toggleSub, onCard, onApproval: decideApproval, openDetail, sessionRow,
      cards: openCards, approvals,
      onCancelSession: cancelSession,
      onRetrySession: retrySession,
      onDeleteSession: deleteSession,
      topControls: html`<${TopCtrls} d=${d} lang=${lang} dark=${theme === "dark"} onToggleTheme=${() => setTheme(theme === "dark" ? "light" : "dark")} onToggleLang=${() => setLang(lang === "zh" ? "en" : "zh")} onPush=${enablePush} />`,
    };

    return html`<div>
      ${!hidingLaunch ? html`<${Launch} d=${d} lang=${lang} hiding=${booted} steps=${launchSteps} />` : null}

      <div className="toasts">${toasts.map((t) => html`<div key=${t.id} className=${`toast ${t.type || ""}`}>${t.text}</div>`)}</div>

      <!-- desktop -->
      <div className="app desktop">
        <${Sidebar} d=${d} lang=${lang} view=${view} onView=${setView} counts=${counts} sessions=${sessions} selected=${selectedSession} onSelect=${openTimeline} onNew=${newSession} />
        ${view === "workspace" ? html`<${Workspace}
            d=${d} lang=${lang} dig=${dig} sessionRow=${sessionRow} events=${events} autonomy=${autonomy}
            openCalls=${openCalls} toggleCall=${toggleCall} expandedSub=${expandedSub} toggleSub=${toggleSub}
            rightTab=${rightTab} setRightTab=${setRightTab} onCard=${onCard} onApproval=${decideApproval} openDetail=${openDetail}
            composer=${composerProps} runCompact=${runCompact} compacting=${compacting} compactStatus=${compactStatus} onBriefing=${runBriefing}
            cards=${openCards} approvals=${approvals} onCancelSession=${cancelSession} onDeleteSession=${deleteSession}
            onRetrySession=${retrySession}
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
              </div>
            </div>`}
      </div>

      <!-- mobile -->
      <${MobileShell} d=${d} lang=${lang} view=${view} setView=${setView} mTab=${mTab} setMTab=${setMTab}
        drawerOpen=${drawerOpen} setDrawerOpen=${setDrawerOpen} counts=${counts} sessionRow=${sessionRow}
        dig=${dig} mainProps=${mainProps} sessions=${sessions} selected=${selectedSession} onSelect=${openTimeline} onNew=${newSession} />

      ${detailOpen ? html`<${DetailModal} d=${d} lang=${lang} detail=${detail} onClose=${() => setDetailOpen(false)} />` : null}
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

  const rootEl = document.getElementById("root");
  ReactDOM.createRoot(rootEl).render(html`<${Shell} />`);
})();
