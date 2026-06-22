(function () {
  "use strict";

  const { useCallback, useEffect, useMemo, useRef, useState } = React;
  const html = htm.bind(React.createElement);
  const A = antd;
  const Icons = window.icons || {};
  const icon = (name) => (Icons[name] ? html`<${Icons[name]} />` : null);

  const TOKEN_KEY = "foreman.token";
  const LANG_KEY = "foreman.lang";
  const WORKSPACE_KEY = "foreman.workspace";
  const DEBUG_KEY = "foreman.debug";

  const I18N = {
    zh: {
      productSubtitle: "本地工作台",
      navWorkspace: "工作台",
      navDecisions: "决策",
      navBriefings: "简报",
      navRules: "工作方式",
      navSettings: "设置",
      workspaceSubtitle: "选择工作区，给本机 agent 下发任务。",
      decisionsSubtitle: "处理需要你确认的卡片和审批。",
      briefingsSubtitle: "把当前进展整理成可读状态。",
      rulesSubtitle: "维护工作流、技能、代码规范和验收标准。",
      settingsSubtitle: "配置工作区、PM 大脑和界面偏好。",
      sessions: "会话",
      dispatch: "下发任务",
      taskGoal: "任务",
      workspace: "工作区",
      send: "发送",
      timeline: "时间线",
      selectSessionHint: "从左侧选择一个会话。",
      decisions: "决策",
      approvals: "审批",
      briefings: "简报",
      generateBriefing: "生成简报",
      noReports: "暂无简报。",
      noSessions: "暂无活动会话。",
      noDecisions: "暂无待决策。",
      noApprovals: "没有待你处理的。",
      refresh: "刷新",
      back: "返回",
      viewDetail: "查看详情",
      stepDetail: "步骤详情",
      rawReturn: "原始返回",
      codeDiff: "代码改动",
      enablePush: "开启通知",
      autonomy: "自动执行权限",
      dispatchAgent: "Agent",
      dispatchModel: "模型",
      dispatchEffort: "档位",
      modelRefresh: "刷新模型",
      agentDefault: "默认",
      effortDefault: "默认",
      effortLow: "快速",
      effortMedium: "标准",
      effortHigh: "深度",
      modelDefaultHint: "留空 = 使用配置默认模型",
      dispatchNoWorkspace: "未配置工作区：请到设置页添加项目路径。",
      dispatchFailed: "下发失败",
      dispatched: "已下发",
      workspaceNotAllowed: "这个工作区不在已配置的工作区列表里。",
      unknownAgent: "这个 agent 没有启用。",
      emptyGoal: "任务不能为空。",
      noDispatcher: "当前服务不是本地 PC 工作台，不能下发任务。",
      workspaceMissing: "没有可用工作区。",
      workspaceEmpty: "没有配置工作区。",
      workspaceSettings: "工作区",
      workspacePath: "项目路径",
      workspaceName: "显示名称",
      workspacePathHint: "例如 E:\\AutoWorkAgent",
      browseFolder: "浏览",
      folderPickerUnavailable: "当前浏览器不支持直接选择文件夹，请手动输入路径。",
      addWorkspace: "添加/更新工作区",
      remove: "移除",
      uiSettings: "界面",
      debugMode: "调试模式",
      pmSettings: "PM 大脑",
      pmProvider: "服务商",
      pmModel: "模型",
      pmBaseUrl: "接口地址",
      pmApiKey: "API Key",
      pmKeyHint: "已配置 API Key。输入新 key 后保存可替换；留空不修改。",
      pmKeyMissing: "未检测到 API Key。可在这里输入并保存。",
      pmKeyPlaceholder: "留空不修改；输入新 key 后保存",
      clearKey: "清空 Key",
      save: "保存",
      saved: "已保存",
      saveFailed: "保存失败",
      autonomyHelp: "决定 Foreman 在没有你确认时能自动执行多少动作。",
      autonomy0: "0 只报告",
      autonomy1: "1 安全动作自动",
      autonomy2: "2 策略动作弹卡",
      autonomy3: "3 只拦危险动作",
      definitionsTitle: "工作方式",
      kindAll: "全部",
      kindWorkflow: "工作流步骤",
      kindSkill: "任务技能",
      kindStandard: "代码规范",
      kindQa: "验收标准",
      newDefinition: "新建",
      exportDefinitions: "导出",
      importDefinitions: "导入",
      noDefinitions: "暂无工作方式。",
      defnKind: "类型",
      defnName: "名称",
      defnScope: "适用范围 (JSON)",
      defnBody: "内容",
      defnActivate: "保存即启用",
      cancel: "取消",
      edit: "编辑",
      activate: "启用",
      delete: "删除",
      confirmDelete: "确定删除这条工作方式？",
      exportFailed: "导出失败",
      importFailed: "导入失败",
      imported: "已导入",
      approve: "批准",
      reject: "驳回",
      rawJson: "原始数据",
      ev_dispatch: "已下发",
      ev_pm_plan: "PM 计划",
      ev_pm_review: "PM 复查",
      ev_agent_output: "输出",
      ev_stop: "完成",
      ev_error: "错误",
      ev_briefing: "简报",
      ev_approval: "待审批",
      ev_card: "决策卡",
      ev_checkpoint: "检查点",
      ev_gate: "闸门",
      ev_action_executed: "已执行",
      ev_action_undone: "已回退",
      evError: "出错",
      evDone: "已完成",
      evDeferred: "已记录，等待执行层接管",
      briefGenerating: "生成中...",
      briefFailed: "简报生成失败",
      briefNoLlm: "PM 大脑未配置。请检查 .env 和设置页。",
      pushEnabled: "通知已开启",
      pushEnabledBody: "你将在这里收到决策与审批提醒。",
      pushUnsupported: "此浏览器不支持通知",
      pushNotConfigured: "服务器未配置推送",
      pushDenied: "通知权限被拒绝",
      pushFailed: "开启通知失败",
      active: "启用中",
    },
    en: {
      productSubtitle: "Local workspace",
      navWorkspace: "Workspace",
      navDecisions: "Decisions",
      navBriefings: "Briefings",
      navRules: "Playbook",
      navSettings: "Settings",
      workspaceSubtitle: "Pick a workspace and dispatch work to the local agent.",
      decisionsSubtitle: "Handle cards and approvals that need you.",
      briefingsSubtitle: "Turn current progress into readable status.",
      rulesSubtitle: "Maintain workflows, skills, code standards, and QA rubrics.",
      settingsSubtitle: "Configure workspaces, PM brain, and UI preferences.",
      sessions: "Sessions",
      dispatch: "Dispatch",
      taskGoal: "Task",
      workspace: "Workspace",
      send: "Send",
      timeline: "Timeline",
      selectSessionHint: "Select a session from the left.",
      decisions: "Decisions",
      approvals: "Approvals",
      briefings: "Briefings",
      generateBriefing: "Generate briefing",
      noReports: "No briefings yet.",
      noSessions: "No active sessions yet.",
      noDecisions: "No decisions waiting.",
      noApprovals: "Nothing waiting on you.",
      refresh: "Refresh",
      back: "Back",
      viewDetail: "View detail",
      stepDetail: "Step detail",
      rawReturn: "Raw return",
      codeDiff: "Code diff",
      enablePush: "Enable notifications",
      autonomy: "Auto-execution",
      dispatchAgent: "Agent",
      dispatchModel: "Model",
      dispatchEffort: "Level",
      modelRefresh: "Refresh models",
      agentDefault: "Default",
      effortDefault: "Default",
      effortLow: "Fast",
      effortMedium: "Standard",
      effortHigh: "Deep",
      modelDefaultHint: "blank = configured default model",
      dispatchNoWorkspace: "No workspace configured. Add a project path in Settings.",
      dispatchFailed: "Dispatch failed",
      dispatched: "Dispatched",
      workspaceNotAllowed: "This workspace is not in the configured workspace list.",
      unknownAgent: "This agent is not enabled.",
      emptyGoal: "Task cannot be empty.",
      noDispatcher: "This service is not the local PC workspace.",
      workspaceMissing: "No workspace available.",
      workspaceEmpty: "No workspaces configured.",
      workspaceSettings: "Workspace",
      workspacePath: "Project path",
      workspaceName: "Display name",
      workspacePathHint: "e.g. E:\\AutoWorkAgent",
      browseFolder: "Browse",
      folderPickerUnavailable: "This browser cannot open a folder picker. Enter the path manually.",
      addWorkspace: "Add/update workspace",
      remove: "Remove",
      uiSettings: "UI",
      debugMode: "Debug mode",
      pmSettings: "PM brain",
      pmProvider: "Provider",
      pmModel: "Model",
      pmBaseUrl: "Base URL",
      pmApiKey: "API Key",
      pmKeyHint: "API key is configured. Enter a new key and save to replace it; blank leaves it unchanged.",
      pmKeyMissing: "No API key detected. You can enter and save one here.",
      pmKeyPlaceholder: "blank = unchanged; enter a new key to save",
      clearKey: "Clear key",
      save: "Save",
      saved: "Saved",
      saveFailed: "Save failed",
      autonomyHelp: "Controls how much Foreman may execute without asking you first.",
      autonomy0: "0 report only",
      autonomy1: "1 safe actions auto",
      autonomy2: "2 strategy asks",
      autonomy3: "3 only danger blocks",
      definitionsTitle: "Playbook",
      kindAll: "All",
      kindWorkflow: "Workflow steps",
      kindSkill: "Task skill",
      kindStandard: "Code standard",
      kindQa: "QA rubric",
      newDefinition: "New",
      exportDefinitions: "Export",
      importDefinitions: "Import",
      noDefinitions: "No playbook items yet.",
      defnKind: "Kind",
      defnName: "Name",
      defnScope: "Scope (JSON)",
      defnBody: "Body",
      defnActivate: "Activate on save",
      cancel: "Cancel",
      edit: "Edit",
      activate: "Activate",
      delete: "Delete",
      confirmDelete: "Delete this playbook item?",
      exportFailed: "Export failed",
      importFailed: "Import failed",
      imported: "Imported",
      approve: "Approve",
      reject: "Reject",
      rawJson: "Raw data",
      ev_dispatch: "Dispatched",
      ev_pm_plan: "PM plan",
      ev_pm_review: "PM review",
      ev_agent_output: "Output",
      ev_stop: "Done",
      ev_error: "Error",
      ev_briefing: "Briefing",
      ev_approval: "Approval",
      ev_card: "Decision card",
      ev_checkpoint: "Checkpoint",
      ev_gate: "Gate",
      ev_action_executed: "Executed",
      ev_action_undone: "Undone",
      evError: "Error",
      evDone: "Done",
      evDeferred: "recorded, waiting for execution layer",
      briefGenerating: "Generating...",
      briefFailed: "Briefing failed",
      briefNoLlm: "PM brain is not configured. Check .env and Settings.",
      pushEnabled: "Notifications enabled",
      pushEnabledBody: "Decision and approval reminders will appear here.",
      pushUnsupported: "Notifications are not supported in this browser",
      pushNotConfigured: "Push is not configured on the server",
      pushDenied: "Notification permission was denied",
      pushFailed: "Could not enable notifications",
      active: "Active",
    },
  };

  const VIEW_META = {
    workspace: ["navWorkspace", "workspaceSubtitle", "CodeOutlined"],
    decisions: ["navDecisions", "decisionsSubtitle", "CheckCircleOutlined"],
    briefings: ["navBriefings", "briefingsSubtitle", "FileTextOutlined"],
    rules: ["navRules", "rulesSubtitle", "ProfileOutlined"],
    settings: ["navSettings", "settingsSubtitle", "SettingOutlined"],
  };

  const KIND_LABEL = {
    workflow: "kindWorkflow",
    skill: "kindSkill",
    code_standard: "kindStandard",
    qa_rubric: "kindQa",
  };

  const EVENT_ICON = {
    dispatch: "SendOutlined",
    agent_output: "MessageOutlined",
    stop: "CheckCircleOutlined",
    error: "WarningOutlined",
    briefing: "FileTextOutlined",
    approval: "StopOutlined",
    card: "ProfileOutlined",
    checkpoint: "PushpinOutlined",
    gate: "SafetyCertificateOutlined",
    action_executed: "ThunderboltOutlined",
    action_undone: "UndoOutlined",
  };

  const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
  const setToken = (token) => token
    ? localStorage.setItem(TOKEN_KEY, token)
    : localStorage.removeItem(TOKEN_KEY);

  let promptedForToken = false;
  function promptForToken() {
    if (promptedForToken) return;
    promptedForToken = true;
    const token = window.prompt("Access token required (FOREMAN_AUTH_TOKEN):", "");
    if (token && token.trim()) {
      setToken(token.trim());
      location.reload();
    }
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
    constructor(message, status, data) {
      super(message);
      this.status = status;
      this.data = data || {};
    }
  }

  async function api(path, opts = {}) {
    const headers = new Headers(opts.headers || {});
    let body = opts.body;
    if (body !== undefined && typeof body !== "string") {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(body);
    }
    const res = await fetch(path, { ...opts, headers, body });
    const contentType = res.headers.get("content-type") || "";
    let data = null;
    if (contentType.includes("application/json")) data = await res.json().catch(() => null);
    else data = await res.text().catch(() => "");
    if (!res.ok) {
      const detail = data && typeof data === "object" ? data.detail : "";
      throw new ApiError(detail || res.statusText || `HTTP ${res.status}`, res.status, data);
    }
    return data;
  }

  function formatDateTime(value, lang) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en-US", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function shortPath(path, d) {
    if (!path) return d.workspaceMissing;
    const parts = String(path).replace(/\\/g, "/").split("/").filter(Boolean);
    return parts[parts.length - 1] || path;
  }

  function friendlyError(error, d) {
    const detail = String(error && error.message ? error.message : error || "");
    const map = {
      empty_goal: d.emptyGoal,
      no_workspace: d.dispatchNoWorkspace,
      workspace_not_allowed: d.workspaceNotAllowed,
      unknown_agent: d.unknownAgent,
      no_dispatcher: d.noDispatcher,
      "no dispatcher": d.noDispatcher,
      no_llm: d.briefNoLlm,
    };
    return map[detail] || detail || `${error.status || ""}`;
  }

  function modelChoiceOptions(rows) {
    return (rows || []).map((m) => ({
      value: m.id,
      label: m.source ? `${m.id} (${m.source})` : m.id,
    }));
  }

  function extractAgentText(payload) {
    if (!payload || typeof payload !== "object") return "";
    if (typeof payload.text === "string") return payload.text;
    if (typeof payload.result === "string") return payload.result;
    const msg = payload.message;
    if (!msg || typeof msg !== "object") return "";
    const content = msg.content;
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return "";
    const parts = [];
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (typeof block.text === "string") parts.push(block.text);
      else if (block.type === "tool_use") parts.push(`[tool] ${block.name || "tool"}`);
      else if (block.type === "tool_result") parts.push(String(block.content || ""));
    }
    return parts.join("\n").trim();
  }

  function summarizeEvent(event, d) {
    const payload = event.payload || {};
    switch (event.type) {
      case "error":
        return payload.msg || payload.error || d.evError;
      case "dispatch": {
        const deferred = payload.execution_deferred ? ` - ${d.evDeferred}` : "";
        return `${payload.goal || ""}${deferred}`.trim();
      }
      case "pm_plan":
        return [payload.summary, payload.instruction].filter(Boolean).join("\n");
      case "pm_review":
        return [
          payload.done ? "done" : "needs follow-up",
          payload.summary,
          payload.reason,
          payload.follow_up,
        ].filter(Boolean).join("\n");
      case "briefing":
        return payload.title || "";
      case "stop":
        return extractAgentText(payload) || d.evDone;
      case "agent_output":
        return extractAgentText(payload);
      default:
        return extractAgentText(payload);
    }
  }

  function eventMetaChips(event) {
    const payload = event.payload || {};
    if (!["dispatch", "pm_plan"].includes(event.type)) return [];
    return [
      payload.pm_agent && { key: "pm", value: "PM", icon: "TeamOutlined", color: "purple" },
      payload.agent && { key: "agent", value: payload.agent, icon: "RobotOutlined", color: "blue" },
      payload.model && { key: "model", value: payload.model, icon: "ApiOutlined", color: "geekblue" },
      payload.effort && { key: "effort", value: payload.effort, icon: "ThunderboltOutlined", color: "gold" },
    ].filter(Boolean);
  }

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
    return out;
  }

  function renderDiff(diff, d) {
    const files = (diff && diff.files) || [];
    if (!files.length) {
      return html`<${A.Empty} image=${A.Empty.PRESENTED_IMAGE_SIMPLE} description=${(diff && diff.note) || (d.codeDiff + " -")} />`;
    }
    return html`
      <div className="diff-view">
        ${files.map((file) => html`
          <div className="diff-file" key=${file.path}>
            ${file.path} +${file.additions || 0} / -${file.deletions || 0}
            ${(file.lines || []).map((line, idx) => {
              const cls = `diff-line diff-${line.kind || "context"}`;
              const sign = line.kind === "add" ? "+" : line.kind === "del" ? "-" : " ";
              return html`<div className=${cls} key=${idx}>${sign}${line.text || ""}</div>`;
            })}
          </div>
        `)}
      </div>`;
  }

  function AppBridge({ onReady }) {
    const app = A.App.useApp();
    useEffect(() => { onReady(app); }, [app, onReady]);
    return null;
  }

  function useLoader(fn) {
    const [loading, setLoading] = useState(false);
    const run = useCallback(async (...args) => {
      setLoading(true);
      try { return await fn(...args); }
      finally { setLoading(false); }
    }, [fn]);
    return [loading, run];
  }

  function Shell() {
    const storedLang = localStorage.getItem(LANG_KEY);
    const [lang, setLangState] = useState(storedLang === "en" ? "en" : "zh");
    const [languageLoaded, setLanguageLoaded] = useState(Boolean(storedLang));
    const d = I18N[lang];
    const [view, setView] = useState("workspace");
    const [dark, setDark] = useState(
      window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    );
    const [debugMode, setDebugMode] = useState(localStorage.getItem(DEBUG_KEY) === "1");
    const [status, setStatus] = useState({ online: false, text: "..." });
    const [workspaces, setWorkspaces] = useState([]);
    const [agents, setAgents] = useState([]);
    const [modelOptions, setModelOptions] = useState([]);
    const [modelLoading, setModelLoading] = useState(false);
    const [pmModelOptions, setPmModelOptions] = useState([]);
    const [pmModelLoading, setPmModelLoading] = useState(false);
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
    const [agent, setAgent] = useState("");
    const [model, setModel] = useState("");
    const [effort, setEffort] = useState("");
    const [dispatchStatus, setDispatchStatus] = useState("");
    const [briefStatus, setBriefStatus] = useState("");
    const [llm, setLlm] = useState({
      provider: "openai", model: "", base_url: "", api_key_set: true, api_key: "",
    });
    const [llmStatus, setLlmStatus] = useState("");
    const [autonomy, setAutonomyState] = useState(1);
    const [detailOpen, setDetailOpen] = useState(false);
    const [detail, setDetail] = useState({ raw: [], diff: { files: [] } });
    const [defnOpen, setDefnOpen] = useState(false);
    const [defnDraft, setDefnDraft] = useState(null);
    const [ui, setUi] = useState(null);
    const wsRef = useRef(null);
    const fileRef = useRef(null);

    const notifyError = useCallback((err) => {
      const msg = friendlyError(err, I18N[lang]);
      if (ui) ui.message.error(msg);
    }, [lang, ui]);

    const loadWorkspaces = useCallback(async () => {
      try {
        const rows = await api("/api/workspaces");
        setWorkspaces(rows || []);
        const paths = (rows || []).map((w) => w.path);
        const chosen = paths.includes(localStorage.getItem(WORKSPACE_KEY))
          ? localStorage.getItem(WORKSPACE_KEY)
          : paths[0] || "";
        setWorkspace(chosen);
        if (chosen) localStorage.setItem(WORKSPACE_KEY, chosen);
      } catch (e) { setWorkspaces([]); }
    }, []);

    const loadAgents = useCallback(async () => {
      try { setAgents(await api("/api/agents") || []); }
      catch (e) { setAgents([]); }
    }, []);

    const loadModels = useCallback(async (selectedAgent) => {
      const name = selectedAgent || agent || "";
      setModelLoading(true);
      try {
        const suffix = name ? `?agent=${encodeURIComponent(name)}` : "";
        const data = await api(`/api/models${suffix}`);
        setModelOptions(modelChoiceOptions(data && data.models));
      } catch (e) {
        setModelOptions([]);
      } finally {
        setModelLoading(false);
      }
    }, [agent]);

    const loadPmModels = useCallback(async (draft) => {
      const current = draft || {};
      const body = {
        provider: current.provider || "openai",
        model: (current.model || "").trim(),
        base_url: (current.base_url || "").trim(),
      };
      if ((current.api_key || "").trim()) body.api_key = current.api_key.trim();
      setPmModelLoading(true);
      try {
        const data = await api("/api/models/preview", { method: "POST", body });
        setPmModelOptions(modelChoiceOptions(data && data.models));
      } catch (e) {
        setPmModelOptions([]);
      } finally {
        setPmModelLoading(false);
      }
    }, []);

    const loadSessions = useCallback(async () => {
      try {
        try { setSessions(await api("/api/overview") || []); }
        catch (e) { setSessions(await api("/api/sessions") || []); }
      } catch (e) { setSessions([]); }
    }, []);

    const loadCards = useCallback(async () => {
      try { setCards(await api("/api/cards") || []); }
      catch (e) { setCards([]); }
    }, []);

    const loadApprovals = useCallback(async () => {
      try { setApprovals(await api("/api/approvals") || []); }
      catch (e) { setApprovals([]); }
    }, []);

    const loadReports = useCallback(async () => {
      try { setReports(await api("/api/reports") || []); }
      catch (e) { setReports([]); }
    }, []);

    const loadDefinitions = useCallback(async () => {
      try {
        const path = defnFilter ? `/api/definitions?kind=${encodeURIComponent(defnFilter)}` : "/api/definitions";
        setDefinitions(await api(path) || []);
      } catch (e) { setDefinitions([]); }
    }, [defnFilter]);

    const loadLlm = useCallback(async () => {
      try {
        const next = { ...(await api("/api/settings/llm")), api_key: "" };
        setLlm(next);
        await loadPmModels(next);
      }
      catch (e) { /* optional in server mode */ }
    }, [loadPmModels]);

    const loadAutonomy = useCallback(async () => {
      try {
        const data = await api("/api/settings/autonomy");
        setAutonomyState(data.level);
      } catch (e) { /* keep default */ }
    }, []);

    const [dispatching, runDispatch] = useLoader(async () => {
      if (!task.trim()) {
        setDispatchStatus(d.emptyGoal);
        return;
      }
      if (!workspace) {
        setDispatchStatus(d.dispatchNoWorkspace);
        setView("settings");
        return;
      }
      const body = { goal: task.trim(), workspace };
      if (agent) body.agent = agent;
      if (model.trim()) body.model = model.trim();
      if (effort) body.effort = effort;
      try {
        const res = await api("/api/tasks", { method: "POST", body });
        setTask("");
        setDispatchStatus(d.dispatched);
        await loadSessions();
        if (res.session_id) openTimeline(res.session_id);
      } catch (e) {
        setDispatchStatus(`${d.dispatchFailed}: ${friendlyError(e, d)}`);
      }
    });

    const [briefing, runBriefing] = useLoader(async () => {
      setBriefStatus(d.briefGenerating);
      try {
        await api("/api/reports/generate", { method: "POST", body: { kind: "active-briefing" } });
        setBriefStatus("");
        await loadReports();
      } catch (e) {
        setBriefStatus(`${d.briefFailed}: ${friendlyError(e, d)}`);
      }
    });

    const selectedSessionRow = useMemo(
      () => sessions.find((s) => s.id === selectedSession),
      [sessions, selectedSession]
    );
    const selectedAgentName = agent || ((agents[0] && agents[0].name) || "");
    const agentDefault = useMemo(() => {
      const row = agents.find((a) => a.name === selectedAgentName);
      return (row && row.model) || d.modelDefaultHint;
    }, [selectedAgentName, agents, d.modelDefaultHint]);
    const workspaceGroups = useMemo(() => {
      const groups = new Map();
      for (const s of sessions) {
        const key = s.workspace || d.workspaceMissing;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(s);
      }
      return Array.from(groups.entries());
    }, [sessions, d.workspaceMissing]);

    useEffect(() => {
      if (localStorage.getItem(LANG_KEY)) {
        setLanguageLoaded(true);
        return;
      }
      api("/api/settings/language")
        .then((data) => setLangState(data && data.language === "en" ? "en" : "zh"))
        .catch(() => {})
        .finally(() => setLanguageLoaded(true));
    }, []);

    useEffect(() => {
      document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
      if (!languageLoaded) return;
      localStorage.setItem(LANG_KEY, lang);
      api("/api/settings/language", { method: "POST", body: { language: lang } }).catch(() => {});
    }, [lang, languageLoaded]);

    useEffect(() => {
      if (debugMode) localStorage.setItem(DEBUG_KEY, "1");
      else localStorage.removeItem(DEBUG_KEY);
    }, [debugMode]);

    useEffect(() => {
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
      }
      api("/health")
        .then((health) => setStatus({ online: true, text: `v${health.version}` }))
        .catch(() => setStatus({ online: false, text: "offline" }));
      loadWorkspaces();
      loadAgents();
      loadSessions();
      loadCards();
      loadApprovals();
      loadReports();
      loadLlm();
      loadAutonomy();
    }, [loadAgents, loadApprovals, loadAutonomy, loadCards, loadLlm, loadReports, loadSessions, loadWorkspaces]);

    useEffect(() => { loadModels(agent); }, [agent, loadModels]);

    useEffect(() => { loadDefinitions(); }, [loadDefinitions]);

    useEffect(() => {
      const params = new URLSearchParams(location.search);
      const id = params.get("approval");
      const action = params.get("action");
      if (id || action) history.replaceState(null, "", location.pathname);
      if (!id || (action !== "approve" && action !== "reject")) return;
      loadApprovals().then(() => {
        const row = approvals.find((a) => a.id === id);
        if (row) decideApproval(row.id, action, row.nonce);
      });
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    function openTimeline(sessionId) {
      setSelectedSession(sessionId);
      setView("workspace");
      setEvents([]);
      if (wsRef.current) {
        try { wsRef.current.close(); } catch (e) { /* ignore */ }
      }
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const token = getToken();
      const tokenQuery = token ? `&token=${encodeURIComponent(token)}` : "";
      const next = new WebSocket(
        `${proto}://${location.host}/ws?session_id=${encodeURIComponent(sessionId)}${tokenQuery}`
      );
      next.addEventListener("message", (event) => {
        try { setEvents((prev) => [...prev, JSON.parse(event.data)]); }
        catch (e) { console.warn("bad event", e); }
      });
      next.addEventListener("error", () => ui && ui.message.error("WebSocket failed"));
      wsRef.current = next;
    }

    async function chooseCard(cardId, option) {
      if (!cardId || !option) return;
      try {
        await api(`/api/cards/${encodeURIComponent(cardId)}/choose`, {
          method: "POST",
          body: { option },
        });
        await loadCards();
      } catch (e) { notifyError(e); }
    }

    async function openDetail(actionId) {
      setDetailOpen(true);
      setDetail({ raw: [], diff: { files: [] } });
      try {
        setDetail(await api(`/api/actions/${encodeURIComponent(actionId)}/detail`));
      } catch (e) {
        setDetail({ raw: [{ type: "error", source: "ui", payload: { error: friendlyError(e, d) } }], diff: { files: [] } });
      }
    }

    async function decideApproval(id, decision, nonce) {
      try {
        await api(`/api/approvals/${encodeURIComponent(id)}`, {
          method: "POST",
          body: { decision, nonce: nonce || "" },
        });
        await loadApprovals();
      } catch (e) { notifyError(e); }
    }

    async function saveDefinition() {
      const draft = defnDraft || {};
      try {
        if (draft.id) {
          await api(`/api/definitions/${encodeURIComponent(draft.id)}`, {
            method: "PATCH",
            body: { body: draft.body || "", scope_json: draft.scope_json || "{}" },
          });
          if (draft.activate) {
            await api(`/api/definitions/${encodeURIComponent(draft.id)}/activate`, { method: "POST" });
          }
        } else {
          await api("/api/definitions", {
            method: "POST",
            body: {
              kind: draft.kind || "workflow",
              name: (draft.name || "").trim(),
              body: draft.body || "",
              scope_json: draft.scope_json || "{}",
              activate: draft.activate !== false,
            },
          });
        }
        setDefnOpen(false);
        setDefnDraft(null);
        await loadDefinitions();
      } catch (e) { notifyError(e); }
    }

    async function activateDefinition(id) {
      try {
        await api(`/api/definitions/${encodeURIComponent(id)}/activate`, { method: "POST" });
        await loadDefinitions();
      } catch (e) { notifyError(e); }
    }

    async function deleteDefinition(id) {
      if (!window.confirm(d.confirmDelete)) return;
      try {
        await api(`/api/definitions/${encodeURIComponent(id)}`, { method: "DELETE" });
        await loadDefinitions();
      } catch (e) { notifyError(e); }
    }

    async function exportDefinitions() {
      try {
        const bundle = await api("/api/definitions/export");
        const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = "foreman-definitions.json";
        a.click();
        URL.revokeObjectURL(a.href);
      } catch (e) { notifyError(e); }
    }

    async function importDefinitions(event) {
      const file = event.target.files && event.target.files[0];
      event.target.value = "";
      if (!file) return;
      try {
        const bundle = JSON.parse(await file.text());
        const res = await api("/api/definitions/import", { method: "POST", body: { bundle } });
        ui && ui.message.success(`${d.imported}: ${res.imported || 0}`);
        await loadDefinitions();
      } catch (e) { notifyError(e); }
    }

    async function saveWorkspace() {
      const path = (workspaceDraft.path || "").trim();
      if (!path) {
        notifyError(new Error(d.workspaceMissing));
        return;
      }
      try {
        const rows = await api("/api/workspaces", {
          method: "POST",
          body: { path, name: (workspaceDraft.name || "").trim() },
        });
        setWorkspaces(rows || []);
        setWorkspace(path);
        localStorage.setItem(WORKSPACE_KEY, path);
        setWorkspaceDraft({ path: "", name: "" });
        ui && ui.message.success(d.saved);
      } catch (e) { notifyError(e); }
    }

    async function browseWorkspaceFolder() {
      const bridge = window.pywebview && window.pywebview.api;
      if (!bridge || !bridge.select_workspace_folder) {
        ui && ui.message.info(d.folderPickerUnavailable);
        return;
      }
      try {
        const path = await bridge.select_workspace_folder();
        if (!path) return;
        setWorkspaceDraft((prev) => ({
          ...prev,
          path,
          name: prev.name || shortPath(path, d),
        }));
      } catch (e) {
        ui && ui.message.info(d.folderPickerUnavailable);
      }
    }

    async function deleteWorkspace(path) {
      try {
        const rows = await api(`/api/workspaces?path=${encodeURIComponent(path)}`, {
          method: "DELETE",
        });
        const next = rows || [];
        setWorkspaces(next);
        if (workspace === path) {
          const chosen = (next[0] && next[0].path) || "";
          setWorkspace(chosen);
          if (chosen) localStorage.setItem(WORKSPACE_KEY, chosen);
          else localStorage.removeItem(WORKSPACE_KEY);
        }
      } catch (e) { notifyError(e); }
    }

    async function saveLlm() {
      try {
        const body = {
          provider: llm.provider || "openai",
          model: (llm.model || "").trim(),
          base_url: (llm.base_url || "").trim(),
        };
        if ((llm.api_key || "").trim()) body.api_key = llm.api_key.trim();
        const data = await api("/api/settings/llm", { method: "POST", body });
        const next = { ...data, api_key: "" };
        setLlm(next);
        setLlmStatus(d.saved);
        await loadPmModels(next);
        await loadModels(agent);
      } catch (e) { setLlmStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }

    async function clearLlmKey() {
      try {
        const data = await api("/api/settings/llm", { method: "POST", body: { api_key: "" } });
        const next = { ...data, api_key: "" };
        setLlm(next);
        setLlmStatus(d.saved);
        await loadPmModels(next);
        await loadModels(agent);
      } catch (e) { setLlmStatus(`${d.saveFailed}: ${friendlyError(e, d)}`); }
    }

    async function saveAutonomy(value) {
      setAutonomyState(value);
      try {
        const data = await api("/api/settings/autonomy", { method: "POST", body: { level: value } });
        setAutonomyState(data.level);
      } catch (e) { notifyError(e); }
    }

    async function enablePush() {
      if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
        ui && ui.message.error(d.pushUnsupported);
        return;
      }
      try {
        const { key, enabled } = await api("/api/push/vapid-public-key");
        if (!enabled || !key) {
          ui && ui.message.error(d.pushNotConfigured);
          return;
        }
        const perm = await Notification.requestPermission();
        if (perm !== "granted") {
          ui && ui.message.error(d.pushDenied);
          return;
        }
        const reg = await navigator.serviceWorker.ready;
        let sub = await reg.pushManager.getSubscription();
        if (!sub) {
          sub = await reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(key),
          });
        }
        await api("/api/push/subscribe", { method: "POST", body: sub.toJSON ? sub.toJSON() : sub });
        ui && ui.message.success(d.pushEnabled);
        await reg.showNotification("Foreman", {
          body: d.pushEnabledBody,
          icon: "/icon-192.png",
          badge: "/icon-192.png",
          tag: "foreman-push-test",
        });
      } catch (e) {
        ui && ui.message.error(`${d.pushFailed}: ${friendlyError(e, d)}`);
      }
    }

    const menuItems = Object.keys(VIEW_META).map((key) => ({
      key,
      icon: icon(VIEW_META[key][2]),
      label: d[VIEW_META[key][0]],
    }));
    const selectedTitle = d[VIEW_META[view][0]];
    const selectedSubtitle = d[VIEW_META[view][1]];

    return html`
      <${A.ConfigProvider} theme=${{ algorithm: dark ? A.theme.darkAlgorithm : A.theme.defaultAlgorithm, token: { colorPrimary: "#2563eb", borderRadius: 8 } }}>
        <${A.App}>
          <${AppBridge} onReady=${setUi} />
          <${A.Layout} className="app-shell">
            <${A.Layout.Sider} width=${292} breakpoint="lg" collapsedWidth=${0} theme=${dark ? "dark" : "light"} className="sidebar">
              <div className="brand">
                <div>
                  <strong>Foreman</strong>
                  <span>${d.productSubtitle}</span>
                </div>
                <${A.Tag} color=${status.online ? "success" : "error"}>${status.text}</${A.Tag}>
              </div>
              <${A.Menu} theme=${dark ? "dark" : "light"} mode="inline" selectedKeys=${[view]} onClick=${({ key }) => setView(key)} items=${menuItems} />
              <div className="session-panel">
                <${A.Typography.Text} type="secondary" className="sidebar-label">${d.sessions}</${A.Typography.Text}>
                <${SessionList}
                  groups=${workspaceGroups}
                  selected=${selectedSession}
                  onSelect=${openTimeline}
                  d=${d}
                  lang=${lang}
                />
              </div>
            </${A.Layout.Sider}>
            <${A.Layout}>
              <${A.Layout.Header} className="topbar">
                <div>
                  <${A.Typography.Title} level=${3} style=${{ margin: 0 }}>${selectedTitle}</${A.Typography.Title}>
                  <${A.Typography.Text} type="secondary">${selectedSubtitle}</${A.Typography.Text}>
                </div>
                <${A.Space}>
                  <${A.Button} icon=${icon(dark ? "BulbOutlined" : "BulbFilled")} onClick=${() => setDark(!dark)} />
                  <${A.Button} onClick=${() => setLangState(lang === "zh" ? "en" : "zh")}>${lang === "zh" ? "EN" : "中"}</${A.Button}>
                  <${A.Button} icon=${icon("BellOutlined")} onClick=${enablePush}>${d.enablePush}</${A.Button}>
                </${A.Space}>
              </${A.Layout.Header}>
              <${A.Layout.Content} className="content">
                ${view === "workspace" && html`
                  <${WorkspaceView}
                    d=${d}
                    lang=${lang}
                    workspaces=${workspaces}
                    workspace=${workspace}
                    setWorkspace=${(v) => { setWorkspace(v); if (v) localStorage.setItem(WORKSPACE_KEY, v); }}
                    agents=${agents}
                    agent=${agent}
                    setAgent=${setAgent}
                    model=${model}
                    setModel=${setModel}
                    modelOptions=${modelOptions}
                    modelLoading=${modelLoading}
                    refreshModels=${() => loadModels(agent)}
                    effort=${effort}
                    setEffort=${setEffort}
                    task=${task}
                    setTask=${setTask}
                    agentDefault=${agentDefault}
                    dispatching=${dispatching}
                    runDispatch=${runDispatch}
                    dispatchStatus=${dispatchStatus}
                    selectedSessionRow=${selectedSessionRow}
                    events=${events}
                    debugMode=${debugMode}
                  />`}
                ${view === "decisions" && html`
                  <${DecisionsView}
                    d=${d}
                    lang=${lang}
                    cards=${cards}
                    approvals=${approvals}
                    loadCards=${loadCards}
                    loadApprovals=${loadApprovals}
                    chooseCard=${chooseCard}
                    openDetail=${openDetail}
                    decideApproval=${decideApproval}
                  />`}
                ${view === "briefings" && html`
                  <${BriefingsView}
                    d=${d}
                    lang=${lang}
                    reports=${reports}
                    briefing=${briefing}
                    runBriefing=${runBriefing}
                    briefStatus=${briefStatus}
                  />`}
                ${view === "rules" && html`
                  <${RulesView}
                    d=${d}
                    lang=${lang}
                    definitions=${definitions}
                    defnFilter=${defnFilter}
                    setDefnFilter=${setDefnFilter}
                    openNew=${() => { setDefnDraft({ kind: defnFilter || "workflow", scope_json: "{}", body: "", activate: true }); setDefnOpen(true); }}
                    openEdit=${(row) => { setDefnDraft({ ...row, activate: false }); setDefnOpen(true); }}
                    activateDefinition=${activateDefinition}
                    deleteDefinition=${deleteDefinition}
                    exportDefinitions=${exportDefinitions}
                    fileRef=${fileRef}
                    importDefinitions=${importDefinitions}
                  />`}
                ${view === "settings" && html`
                  <${SettingsView}
                    d=${d}
                    workspaces=${workspaces}
                    loadWorkspaces=${loadWorkspaces}
                    workspaceDraft=${workspaceDraft}
                    setWorkspaceDraft=${setWorkspaceDraft}
                    browseWorkspaceFolder=${browseWorkspaceFolder}
                    saveWorkspace=${saveWorkspace}
                    deleteWorkspace=${deleteWorkspace}
                    llm=${llm}
                    setLlm=${setLlm}
                    pmModelOptions=${pmModelOptions}
                    pmModelLoading=${pmModelLoading}
                    refreshPmModels=${() => loadPmModels(llm)}
                    saveLlm=${saveLlm}
                    clearLlmKey=${clearLlmKey}
                    llmStatus=${llmStatus}
                    autonomy=${autonomy}
                    saveAutonomy=${saveAutonomy}
                    debugMode=${debugMode}
                    setDebugMode=${setDebugMode}
                  />`}
              </${A.Layout.Content}>
            </${A.Layout}>
          </${A.Layout}>
          <${A.Modal}
            title=${d.stepDetail}
            open=${detailOpen}
            onCancel=${() => setDetailOpen(false)}
            footer=${html`<${A.Button} onClick=${() => setDetailOpen(false)}>${d.back}</${A.Button}>`}
            width=${900}
          >
            <${DetailTabs} d=${d} lang=${lang} detail=${detail} debugMode=${debugMode} />
          </${A.Modal}>
          <${A.Modal}
            title=${defnDraft && defnDraft.id ? d.edit : d.newDefinition}
            open=${defnOpen}
            onCancel=${() => setDefnOpen(false)}
            onOk=${saveDefinition}
            okText=${d.save}
            cancelText=${d.cancel}
            width=${760}
          >
            <${DefinitionEditor} d=${d} draft=${defnDraft} setDraft=${setDefnDraft} />
          </${A.Modal}>
        </${A.App}>
      </${A.ConfigProvider}>`;
  }

  function SessionList({ groups, selected, onSelect, d, lang }) {
    if (!groups.length) {
      return html`<${A.Empty} image=${A.Empty.PRESENTED_IMAGE_SIMPLE} description=${d.noSessions} />`;
    }
    return html`
      <div className="session-list">
        ${groups.map(([workspace, rows]) => html`
          <div className="workspace-group" key=${workspace}>
            <${A.Typography.Text} type="secondary" className="workspace-title">${shortPath(workspace, d)}</${A.Typography.Text}>
            ${rows.map((s) => html`
              <${A.Button}
                key=${s.id}
                block
                type=${s.id === selected ? "primary" : "default"}
                className="session-item"
                onClick=${() => onSelect(s.id)}
              >
                <span className="session-head">${s.goal || s.id}</span>
                <span className="session-meta">
                  ${s.status || "-"} / ${s.agent_type || "-"} / ${formatDateTime(s.updated_at || s.created_at || s.last_event_ts, lang)}
                  ${s.events === undefined ? "" : ` / ${s.events} events`}
                </span>
              </${A.Button}>
            `)}
          </div>
        `)}
      </div>`;
  }

  function WorkspaceView(props) {
    const {
      d, lang, workspaces, workspace, setWorkspace, agents, agent, setAgent, model, setModel,
      modelOptions, modelLoading, refreshModels, effort, setEffort, task, setTask, agentDefault,
      dispatching, runDispatch, dispatchStatus, selectedSessionRow, events, debugMode,
    } = props;
    return html`
      <div className="view-grid">
        <${A.Card} title=${d.dispatch} extra=${workspace ? html`<${A.Tag} color="blue">${shortPath(workspace, d)}</${A.Tag}>` : null}>
          ${!workspaces.length && html`<${A.Alert} type="warning" showIcon message=${d.workspaceEmpty} description=${d.dispatchNoWorkspace} style=${{ marginBottom: 16 }} />`}
          <${A.Form} layout="vertical" onFinish=${runDispatch}>
            <${A.Form.Item} label=${d.taskGoal}>
              <${A.Input.TextArea} rows=${4} value=${task} onChange=${(e) => setTask(e.target.value)} placeholder=${lang === "zh" ? "例如：重构 auth 模块，push 前问我" : "e.g. refactor auth, ask me before push"} />
            </${A.Form.Item}>
            <${A.Row} gutter=${12}>
              <${A.Col} xs=${24} md=${8}>
                <${A.Form.Item} label=${d.workspace}>
                  <${A.Select}
                    value=${workspace || ""}
                    onChange=${setWorkspace}
                    options=${workspaces.length ? workspaces.map((w) => ({ value: w.path, label: w.name ? `${w.name} - ${w.path}` : w.path })) : [{ value: "", label: d.workspaceEmpty }]}
                  />
                </${A.Form.Item}>
              </${A.Col}>
              <${A.Col} xs=${24} md=${5}>
                <${A.Form.Item} label=${d.dispatchAgent}>
                  <${A.Select}
                    value=${agent}
                    onChange=${setAgent}
                    options=${[{ value: "", label: d.agentDefault }, ...agents.map((a) => ({ value: a.name, label: a.name }))]}
                  />
                </${A.Form.Item}>
              </${A.Col}>
              <${A.Col} xs=${24} md=${7}>
                <${A.Form.Item} label=${d.dispatchModel}>
                  <${A.Space.Compact} block>
                    <${A.AutoComplete}
                      style=${{ width: "100%" }}
                      value=${model}
                      onChange=${setModel}
                      options=${modelOptions}
                      placeholder=${agentDefault}
                      filterOption=${(input, option) => String(option.value || "").toLowerCase().includes(input.toLowerCase())}
                    />
                    <${A.Button}
                      htmlType="button"
                      loading=${modelLoading}
                      icon=${icon("ReloadOutlined")}
                      onClick=${refreshModels}
                      title=${d.modelRefresh}
                    />
                  </${A.Space.Compact}>
                </${A.Form.Item}>
              </${A.Col}>
              <${A.Col} xs=${24} md=${4}>
                <${A.Form.Item} label=${d.dispatchEffort}>
                  <${A.Select}
                    value=${effort}
                    onChange=${setEffort}
                    options=${[
                      { value: "", label: d.effortDefault },
                      { value: "low", label: d.effortLow },
                      { value: "medium", label: d.effortMedium },
                      { value: "high", label: d.effortHigh },
                    ]}
                  />
                </${A.Form.Item}>
              </${A.Col}>
            </${A.Row}>
            <${A.Space}>
              <${A.Button} type="primary" htmlType="submit" loading=${dispatching} icon=${icon("SendOutlined")}>${d.send}</${A.Button}>
              ${dispatchStatus && html`<${A.Typography.Text} type=${dispatchStatus.includes(d.dispatchFailed) ? "danger" : "secondary"}>${dispatchStatus}</${A.Typography.Text}>`}
            </${A.Space}>
          </${A.Form}>
        </${A.Card}>
        <${A.Card}
          title=${d.timeline}
          extra=${selectedSessionRow ? html`<${A.Tag}>${formatDateTime(selectedSessionRow.updated_at || selectedSessionRow.created_at, lang)}</${A.Tag}>` : html`<${A.Typography.Text} type="secondary">${d.selectSessionHint}</${A.Typography.Text}>`}
        >
          <${Timeline} events=${events} d=${d} lang=${lang} debugMode=${debugMode} />
        </${A.Card}>
      </div>`;
  }

  function Timeline({ events, d, lang, debugMode }) {
    if (!events.length) {
      return html`<${A.Empty} image=${A.Empty.PRESENTED_IMAGE_SIMPLE} description=${d.selectSessionHint} />`;
    }
    return html`
      <${A.List}
        itemLayout="vertical"
        dataSource=${events}
        renderItem=${(event) => {
          const eventIcon = EVENT_ICON[event.type] || "InfoCircleOutlined";
          const summary = summarizeEvent(event, d);
          const chips = eventMetaChips(event);
          return html`
            <${A.List.Item}>
              <${A.Space} wrap>
                ${icon(eventIcon)}
                <strong>${d[`ev_${event.type}`] || event.type || "-"}</strong>
                <${A.Typography.Text} type="secondary">${event.source || ""}</${A.Typography.Text}>
                <${A.Typography.Text} type="secondary">${formatDateTime(event.ts, lang)}</${A.Typography.Text}>
              </${A.Space}>
              ${chips.length > 0 && html`
                <${A.Space} className="event-meta" wrap size=${[4, 4]}>
                  ${chips.map((chip) => html`
                    <${A.Tag} key=${chip.key} color=${chip.color} icon=${icon(chip.icon)}>${chip.value}</${A.Tag}>
                  `)}
                </${A.Space}>
              `}
              ${summary && html`<pre className="event-body">${summary.length > 2000 ? `${summary.slice(0, 2000)}...` : summary}</pre>`}
              ${debugMode && event.payload && Object.keys(event.payload).length > 0 && html`
                <${A.Collapse}
                  ghost
                  items=${[{ key: "raw", label: d.rawJson, children: html`<pre className="raw-body">${JSON.stringify(event.payload, null, 2)}</pre>` }]}
                />
              `}
            </${A.List.Item}>`;
        }}
      />`;
  }

  function DecisionsView({ d, lang, cards, approvals, loadCards, loadApprovals, chooseCard, openDetail, decideApproval }) {
    return html`
      <div className="view-grid">
        <${A.Card} title=${d.decisions} extra=${html`<${A.Button} icon=${icon("ReloadOutlined")} onClick=${loadCards}>${d.refresh}</${A.Button}>`}>
          ${!cards.length ? html`<${A.Empty} image=${A.Empty.PRESENTED_IMAGE_SIMPLE} description=${d.noDecisions} />` : html`
            <${A.List}
              dataSource=${cards}
              renderItem=${(card) => {
                const actions = [
                  card.action_id && html`<${A.Button} size="small" icon=${icon("SearchOutlined")} onClick=${() => openDetail(card.action_id)}>${d.viewDetail}</${A.Button}>`,
                  ...(card.options || []).map((opt) => html`<${A.Button} size="small" key=${opt.action} onClick=${() => chooseCard(card.id, opt.action)}>${opt.label || opt.action}</${A.Button}>`),
                ].filter(Boolean);
                return html`
                  <${A.List.Item} actions=${actions}>
                    <${A.List.Item.Meta}
                      title=${card.summary || ""}
                      description=${html`
                        <${A.Space} direction="vertical" size=${4}>
                          <span>${card.audit_note || ""}</span>
                          ${card.diff_stat && html`<${A.Tag}>${card.diff_stat}</${A.Tag}>`}
                        </${A.Space}>`}
                    />
                  </${A.List.Item}>`;
              }}
            />`}
        </${A.Card}>
        <${A.Card} title=${d.approvals} extra=${html`<${A.Button} icon=${icon("ReloadOutlined")} onClick=${loadApprovals}>${d.refresh}</${A.Button}>`}>
          ${!approvals.length ? html`<${A.Empty} image=${A.Empty.PRESENTED_IMAGE_SIMPLE} description=${d.noApprovals} />` : html`
            <${A.List}
              dataSource=${approvals}
              renderItem=${(approval) => html`
                <${A.List.Item}
                  actions=${[
                    html`<${A.Button} type="primary" onClick=${() => decideApproval(approval.id, "approve", approval.nonce)}>${d.approve}</${A.Button}>`,
                    html`<${A.Button} danger onClick=${() => decideApproval(approval.id, "reject", approval.nonce)}>${d.reject}</${A.Button}>`,
                  ]}
                >
                  <${A.List.Item.Meta}
                    title=${html`<${A.Tag} color="red">${approval.risk_level || "requires-approval"}</${A.Tag}>`}
                    description=${approval.action || approval.diff_summary || ""}
                  />
                </${A.List.Item}>`}
            />`}
        </${A.Card}>
      </div>`;
  }

  function DetailTabs({ d, lang, detail, debugMode }) {
    const raw = detail.raw || [];
    const items = [
      { key: "diff", label: d.codeDiff, children: renderDiff(detail.diff || { files: [] }, d) },
    ];
    if (debugMode) {
      items.unshift({
        key: "raw",
        label: d.rawReturn,
        children: raw.length
          ? html`<pre className="raw-body">${raw.map((e) => `[${formatDateTime(e.ts, lang)}] ${e.type || ""} (${e.source || ""})\n${JSON.stringify(e.payload || {}, null, 2)}`).join("\n\n")}</pre>`
          : html`<${A.Empty} image=${A.Empty.PRESENTED_IMAGE_SIMPLE} description=${d.rawReturn} />`,
      });
    }
    return html`
      <${A.Tabs}
        items=${items}
      />`;
  }

  function BriefingsView({ d, lang, reports, briefing, runBriefing, briefStatus }) {
    return html`
      <${A.Card} title=${d.briefings} extra=${html`<${A.Button} type="primary" icon=${icon("FileTextOutlined")} loading=${briefing} onClick=${runBriefing}>${d.generateBriefing}</${A.Button}>`}>
        ${briefStatus && html`<${A.Alert} type=${briefStatus.includes(d.briefFailed) ? "error" : "info"} showIcon message=${briefStatus} style=${{ marginBottom: 16 }} />`}
        ${!reports.length ? html`<${A.Empty} image=${A.Empty.PRESENTED_IMAGE_SIMPLE} description=${d.noReports} />` : html`
          <${A.List}
            dataSource=${reports}
            renderItem=${(report) => html`
              <${A.List.Item}>
                <${A.List.Item.Meta}
                  title=${html`<${A.Space}>${report.title || report.kind || d.briefings}<${A.Tag}>${formatDateTime(report.ts, lang)}</${A.Tag}></${A.Space}>`}
                  description=${html`<pre className="report-body">${report.body_md || ""}</pre>`}
                />
              </${A.List.Item}>`}
          />`}
      </${A.Card}>`;
  }

  function RulesView(props) {
    const {
      d, lang, definitions, defnFilter, setDefnFilter, openNew, openEdit, activateDefinition,
      deleteDefinition, exportDefinitions, fileRef, importDefinitions,
    } = props;
    return html`
      <${A.Card}
        title=${d.definitionsTitle}
        extra=${html`
          <${A.Space} wrap>
            <${A.Select}
              value=${defnFilter}
              onChange=${setDefnFilter}
              style=${{ width: 160 }}
              options=${[
                { value: "", label: d.kindAll },
                { value: "workflow", label: d.kindWorkflow },
                { value: "skill", label: d.kindSkill },
                { value: "code_standard", label: d.kindStandard },
                { value: "qa_rubric", label: d.kindQa },
              ]}
            />
            <${A.Button} type="primary" icon=${icon("PlusOutlined")} onClick=${openNew}>${d.newDefinition}</${A.Button}>
            <${A.Button} icon=${icon("DownloadOutlined")} onClick=${exportDefinitions}>${d.exportDefinitions}</${A.Button}>
            <${A.Button} icon=${icon("UploadOutlined")} onClick=${() => fileRef.current && fileRef.current.click()}>${d.importDefinitions}</${A.Button}>
            <input ref=${fileRef} type="file" accept="application/json,.json" hidden onChange=${importDefinitions} />
          </${A.Space}>`}
      >
        ${!definitions.length ? html`<${A.Empty} image=${A.Empty.PRESENTED_IMAGE_SIMPLE} description=${d.noDefinitions} />` : html`
          <${A.List}
            dataSource=${definitions}
            renderItem=${(row) => html`
              <${A.List.Item}
                actions=${[
                  html`<${A.Button} size="small" onClick=${() => openEdit(row)}>${d.edit}</${A.Button}>`,
                  !row.is_active && html`<${A.Button} size="small" onClick=${() => activateDefinition(row.id)}>${d.activate}</${A.Button}>`,
                  html`<${A.Button} size="small" danger onClick=${() => deleteDefinition(row.id)}>${d.delete}</${A.Button}>`,
                ].filter(Boolean)}
              >
                <${A.List.Item.Meta}
                  title=${html`<${A.Space}>${d[KIND_LABEL[row.kind]] || row.kind}<${A.Typography.Text}>${row.name} v${row.version}</${A.Typography.Text}>${row.is_active && html`<${A.Tag} color="green">${d.active}</${A.Tag}>`}</${A.Space}>`}
                  description=${formatDateTime(row.updated_at || row.created_at, lang)}
                />
              </${A.List.Item}>`}
          />
        `}
      </${A.Card}>`;
  }

  function DefinitionEditor({ d, draft, setDraft }) {
    const row = draft || {};
    const update = (patch) => setDraft({ ...(draft || {}), ...patch });
    return html`
      <${A.Form} layout="vertical">
        <${A.Row} gutter=${12}>
          <${A.Col} span=${12}>
            <${A.Form.Item} label=${d.defnKind}>
              <${A.Select}
                value=${row.kind || "workflow"}
                disabled=${Boolean(row.id)}
                onChange=${(v) => update({ kind: v })}
                options=${[
                  { value: "workflow", label: d.kindWorkflow },
                  { value: "skill", label: d.kindSkill },
                  { value: "code_standard", label: d.kindStandard },
                  { value: "qa_rubric", label: d.kindQa },
                ]}
              />
            </${A.Form.Item}>
          </${A.Col}>
          <${A.Col} span=${12}>
            <${A.Form.Item} label=${d.defnName}>
              <${A.Input} value=${row.name || ""} disabled=${Boolean(row.id)} onChange=${(e) => update({ name: e.target.value })} placeholder="add-feature" />
            </${A.Form.Item}>
          </${A.Col}>
        </${A.Row}>
        <${A.Form.Item} label=${d.defnScope}>
          <${A.Input} value=${row.scope_json || "{}"} onChange=${(e) => update({ scope_json: e.target.value })} placeholder='{"lang":"py"}' />
        </${A.Form.Item}>
        <${A.Form.Item} label=${d.defnBody}>
          <${A.Input.TextArea} rows=${10} value=${row.body || ""} onChange=${(e) => update({ body: e.target.value })} />
        </${A.Form.Item}>
        <${A.Checkbox} checked=${row.activate !== false} onChange=${(e) => update({ activate: e.target.checked })}>${d.defnActivate}</${A.Checkbox}>
      </${A.Form}>`;
  }

  function SettingsView({
    d, workspaces, loadWorkspaces, workspaceDraft, setWorkspaceDraft, saveWorkspace,
    browseWorkspaceFolder, deleteWorkspace, llm, setLlm, pmModelOptions, pmModelLoading,
    refreshPmModels, saveLlm, clearLlmKey, llmStatus, autonomy, saveAutonomy,
    debugMode, setDebugMode,
  }) {
    return html`
      <div className="view-grid">
        <${A.Card} title=${d.workspaceSettings} extra=${html`<${A.Button} icon=${icon("ReloadOutlined")} onClick=${loadWorkspaces}>${d.refresh}</${A.Button}>`}>
          ${!workspaces.length ? html`<${A.Alert} type="warning" showIcon message=${d.workspaceEmpty} description=${d.dispatchNoWorkspace} />` : html`
            <${A.List}
              dataSource=${workspaces}
              renderItem=${(w) => html`
                <${A.List.Item}
                  actions=${[
                    html`<${A.Button} danger size="small" onClick=${() => deleteWorkspace(w.path)}>${d.remove}</${A.Button}>`,
                  ]}
                >
                  <${A.List.Item.Meta} title=${w.name || shortPath(w.path, d)} description=${w.path} />
                </${A.List.Item}>`}
            />
          `}
          <${A.Form} layout="vertical" onFinish=${saveWorkspace} style=${{ marginTop: 16 }}>
            <${A.Row} gutter=${12}>
              <${A.Col} xs=${24} md=${16}>
                <${A.Form.Item} label=${d.workspacePath}>
                  <${A.Space.Compact} block>
                    <${A.Input}
                      value=${workspaceDraft.path}
                      onChange=${(e) => setWorkspaceDraft({ ...workspaceDraft, path: e.target.value })}
                      placeholder=${d.workspacePathHint}
                    />
                    <${A.Button}
                      htmlType="button"
                      icon=${icon("FolderOpenOutlined")}
                      onClick=${browseWorkspaceFolder}
                    >${d.browseFolder}</${A.Button}>
                  </${A.Space.Compact}>
                </${A.Form.Item}>
              </${A.Col}>
              <${A.Col} xs=${24} md=${8}>
                <${A.Form.Item} label=${d.workspaceName}>
                  <${A.Input}
                    value=${workspaceDraft.name}
                    onChange=${(e) => setWorkspaceDraft({ ...workspaceDraft, name: e.target.value })}
                    placeholder="Foreman"
                  />
                </${A.Form.Item}>
              </${A.Col}>
            </${A.Row}>
            <${A.Button} type="primary" htmlType="submit">${d.addWorkspace}</${A.Button}>
          </${A.Form}>
        </${A.Card}>
        <${A.Card} title=${d.pmSettings}>
          <${A.Form} layout="vertical" onFinish=${saveLlm}>
            <${A.Row} gutter=${12}>
              <${A.Col} xs=${24} md=${8}>
                <${A.Form.Item} label=${d.pmProvider}>
                  <${A.Select}
                    value=${llm.provider || "openai"}
                    onChange=${(v) => setLlm({ ...llm, provider: v })}
                    options=${[{ value: "openai", label: "OpenAI-compatible" }, { value: "anthropic", label: "Anthropic" }]}
                  />
                </${A.Form.Item}>
              </${A.Col}>
              <${A.Col} xs=${24} md=${16}>
                <${A.Form.Item} label=${d.pmModel}>
                  <${A.Space.Compact} block>
                    <${A.AutoComplete}
                      style=${{ width: "100%" }}
                      value=${llm.model || ""}
                      onChange=${(v) => setLlm({ ...llm, model: v })}
                      options=${pmModelOptions}
                      filterOption=${(input, option) => String(option.value || "").toLowerCase().includes(input.toLowerCase())}
                    />
                    <${A.Button}
                      htmlType="button"
                      loading=${pmModelLoading}
                      icon=${icon("ReloadOutlined")}
                      onClick=${refreshPmModels}
                      title=${d.modelRefresh}
                    />
                  </${A.Space.Compact}>
                </${A.Form.Item}>
              </${A.Col}>
            </${A.Row}>
            <${A.Form.Item} label=${d.pmBaseUrl}>
              <${A.Input} value=${llm.base_url || ""} onChange=${(e) => setLlm({ ...llm, base_url: e.target.value })} placeholder="https://api.openai.com/v1" />
            </${A.Form.Item}>
            <${A.Form.Item} label=${d.pmApiKey}>
              <${A.Input.Password}
                value=${llm.api_key || ""}
                onChange=${(e) => setLlm({ ...llm, api_key: e.target.value })}
                placeholder=${d.pmKeyPlaceholder}
                autoComplete="off"
              />
            </${A.Form.Item}>
            <${A.Alert} type=${llm.api_key_set ? "info" : "warning"} showIcon message=${llm.api_key_set ? d.pmKeyHint : d.pmKeyMissing} style=${{ marginBottom: 16 }} />
            ${llmStatus && html`<${A.Alert} type=${llmStatus === d.saved ? "success" : "error"} showIcon message=${llmStatus} style=${{ marginBottom: 16 }} />`}
            <${A.Space} wrap>
              <${A.Button} type="primary" htmlType="submit">${d.save}</${A.Button}>
              <${A.Button} danger htmlType="button" onClick=${clearLlmKey}>${d.clearKey}</${A.Button}>
            </${A.Space}>
          </${A.Form}>
        </${A.Card}>
        <${A.Card} title=${d.uiSettings}>
          <${A.Form} layout="vertical">
            <${A.Form.Item} label=${d.autonomy}>
              <${A.Alert} type="info" showIcon message=${d.autonomyHelp} style=${{ marginBottom: 16 }} />
              <${A.Slider}
                min=${0}
                max=${3}
                step=${1}
                marks=${{ 0: d.autonomy0, 1: d.autonomy1, 2: d.autonomy2, 3: d.autonomy3 }}
                value=${autonomy}
                onChangeComplete=${saveAutonomy}
              />
            </${A.Form.Item}>
            <${A.Form.Item} label=${d.debugMode}>
              <${A.Switch} checked=${debugMode} onChange=${setDebugMode} />
            </${A.Form.Item}>
          </${A.Form}>
        </${A.Card}>
      </div>`;
  }

  const rootEl = document.getElementById("root");
  ReactDOM.createRoot(rootEl).render(html`<${Shell} />`);
})();
