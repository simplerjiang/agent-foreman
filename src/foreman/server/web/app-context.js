(function () {
  "use strict";

  const core = window.ForemanApp || {};
  const html = core.html;
  const useCallback = core.useCallback;
  const useEffect = core.useEffect;
  const useRef = core.useRef;
  const useState = core.useState;
  const api = core.api;
  const friendlyError = core.friendlyError;
  const tokenK = core.tokenK;
  const shortPath = core.shortPath;
  const formatTime = core.formatTime;

  const HIDDEN_CONTEXT_KEY_PARTS = [
    "std" + "out",
    "std" + "err",
    "provider" + "_" + "payload",
    "encrypted" + "_" + "content",
    "hidden" + "_" + "reason" + "ing",
    "pm" + "_" + "reason" + "ing",
    "reason" + "ing",
    "raw" + "_" + "output",
    "aggregated" + "_" + "output",
    "api" + "_" + "key",
    "api" + "key",
    "access" + "_" + "key",
    "access" + "key",
    "tok" + "en",
    "sec" + "ret",
    "pass" + "word",
    "author" + "ization",
    "bear" + "er",
  ];
  const HIDDEN_CONTEXT_KEYS = new Set(HIDDEN_CONTEXT_KEY_PARTS);

  function isHiddenContextKey(key) {
    const k = String(key || "").toLowerCase();
    return HIDDEN_CONTEXT_KEYS.has(k) || HIDDEN_CONTEXT_KEY_PARTS.some((part) => k.includes(part));
  }

  function sanitizeContextTextValue(value, depth = 0) {
    if (depth > 4) return "[truncated]";
    if (Array.isArray(value)) return value.map((item) => sanitizeContextTextValue(item, depth + 1));
    if (!value || typeof value !== "object") return value;
    const out = {};
    Object.entries(value).forEach(([key, item]) => {
      out[key] = isHiddenContextKey(key) ? "[redacted]" : sanitizeContextTextValue(item, depth + 1);
    });
    return out;
  }

  function contextText(value, maxChars = 300) {
    if (value === undefined || value === null || value === "") return "";
    if (typeof value === "string") return value.length > maxChars ? value.slice(0, maxChars) + "..." : value;
    try {
      const text = JSON.stringify(sanitizeContextTextValue(value));
      return text.length > maxChars ? text.slice(0, maxChars) + "..." : text;
    } catch (e) {
      const text = String(value);
      return text.length > maxChars ? text.slice(0, maxChars) + "..." : text;
    }
  }

  function contextValue(value, unknown) {
    if (value === undefined || value === null || value === "") return unknown;
    if (Array.isArray(value)) return value.length ? value.join(", ") : unknown;
    const text = contextText(value);
    return text || unknown;
  }

  function contextItems(value) {
    if (!Array.isArray(value)) return [];
    return value.filter((x) => x !== undefined && x !== null && String(x).trim()).slice(0, 20);
  }

  function contextStatusText(value, d) {
    const status = String(value || "").trim().toLowerCase();
    const labels = {
      active: d.contextStatusActive,
      completed: d.contextStatusCompleted,
      failed: d.contextStatusFailed,
      idle: d.contextStatusIdle,
      running: d.contextStatusRunning,
      unknown: d.contextUnknown,
    };
    return labels[status] || contextText(value) || d.contextUnknown;
  }

  function contextModeText(value, d) {
    const mode = String(value || "").trim().toLowerCase();
    const labels = {
      checkpoint: d.contextModeCheckpoint,
      raw_frames: d.contextModeRawFrames,
      raw_frames_degraded: d.contextModeRawFramesDegraded,
    };
    return labels[mode] || contextText(value) || d.contextUnknown;
  }

  function ContextPanel({ sessionRow, d, lang }) {
    const [state, setState] = useState("idle");
    const [data, setData] = useState(null);
    const [checkpoints, setCheckpoints] = useState([]);
    const [detail, setDetail] = useState(null);
    const [error, setError] = useState("");
    const [compactMsg, setCompactMsg] = useState("");
    const [fullPreview, setFullPreview] = useState(null);
    const [previewLoading, setPreviewLoading] = useState(false);
    const [previewError, setPreviewError] = useState("");
    const previewRequest = useRef(0);
    const sessionId = sessionRow && sessionRow.id;
    const loadContext = useCallback(async () => {
      previewRequest.current += 1;
      if (!sessionId) { setData(null); setCheckpoints([]); setDetail(null); setFullPreview(null); setPreviewLoading(false); return; }
      setState((prev) => prev === "ready" || prev === "degraded" ? prev : "loading");
      setError("");
      setFullPreview(null);
      setPreviewLoading(false);
      setPreviewError("");
      try {
        const [ctx, cps] = await Promise.all([
          api(`/api/sessions/${encodeURIComponent(sessionId)}/context`),
          api(`/api/sessions/${encodeURIComponent(sessionId)}/context/checkpoints`),
        ]);
        setData(ctx);
        setCheckpoints((cps && cps.items) || []);
        setState(ctx && ctx.degraded ? "degraded" : "ready");
      } catch (e) {
        setError(friendlyError(e, d));
        setState("error");
      }
    }, [sessionId, d]);
    useEffect(() => { loadContext(); }, [loadContext]);
    async function runManualCompact() {
      if (!sessionId) return;
      setState("compacting");
      setCompactMsg(d.contextCompactBusy);
      setError("");
      try {
        const res = await api(`/api/sessions/${encodeURIComponent(sessionId)}/context/compact`, {
          method: "POST",
          body: { trigger: "manual", reason: "user_requested" },
        });
        if (!res || !res.ok) {
          const msg = res && res.error ? (res.error.message || res.error.code) : "context_compact_failed";
          setCompactMsg(`${d.contextCompactFailed} ${msg}. ${d.contextLatestUnchanged}`);
          setState(data && data.degraded ? "degraded" : "ready");
          return;
        }
        setCompactMsg(d.contextCompacted);
        await loadContext();
      } catch (e) {
        setCompactMsg(`${d.contextCompactFailed} ${friendlyError(e, d)}. ${d.contextLatestUnchanged}`);
        setState(data && data.degraded ? "degraded" : "ready");
      }
    }
    async function loadFullPreview() {
      if (!sessionId || previewLoading) return;
      const request = ++previewRequest.current;
      setPreviewLoading(true);
      setPreviewError("");
      try {
        const res = await api(`/api/sessions/${encodeURIComponent(sessionId)}/context/preview`);
        if (request === previewRequest.current) setFullPreview(String((res && res.content) || ""));
      } catch (e) {
        if (request === previewRequest.current) setPreviewError(friendlyError(e, d));
      } finally {
        if (request === previewRequest.current) setPreviewLoading(false);
      }
    }
    async function openCheckpoint(row) {
      if (!sessionId || !row || !row.id) return;
      try {
        setDetail(await api(`/api/sessions/${encodeURIComponent(sessionId)}/context/checkpoints/${encodeURIComponent(row.id)}`));
      } catch (e) {
        setError(friendlyError(e, d));
      }
    }
    async function copySummary() {
      let src = detail;
      if (!src && checkpoints[0] && sessionId) {
        try {
          src = await api(`/api/sessions/${encodeURIComponent(sessionId)}/context/checkpoints/${encodeURIComponent(checkpoints[0].id)}`);
          setDetail(src);
        } catch (e) {}
      }
      src = src || (data && data.latest_checkpoint) || null;
      if (!src) return;
      try { await navigator.clipboard.writeText(JSON.stringify(src.summary || src, null, 2)); } catch (e) {}
    }
    const usage = (data && data.usage) || {};
    const runtime = (data && data.runtime_state) || {};
    const latest = data && data.latest_checkpoint;
    const lanes = (usage && usage.lane_usage) || {};
    const pct = Math.round(((usage.percent || 0) * 1000)) / 10;
    const agents = Array.isArray(runtime.active_agents) ? runtime.active_agents : [];
    const changed = contextItems(runtime.changed_files);
    const tests = contextItems(runtime.last_tests).map((t) => contextText(t));
    const steps = contextItems(runtime.next_steps).map((s) => contextText(s));
    const commands = contextItems(runtime.last_commands).map((c) => contextText(c));
    const degraded = !!(data && data.degraded);
    const previewWithSuffix = String((data && data.active_context_preview) || "");
    const previewSuffix = "\n...[preview truncated]";
    const previewTruncated = previewWithSuffix.endsWith(previewSuffix);
    const preview = previewTruncated ? previewWithSuffix.slice(0, -previewSuffix.length) : previewWithSuffix;
    const contextState = state === "compacting" ? d.contextStateCompacting : degraded ? d.contextStateDegraded : d.contextStateHealthy;
    return html`<div className="context-panel" data-testid="context-panel">
      ${state === "loading" ? html`<div className="alert info">${d.contextLoading}</div>` : null}
      ${degraded ? html`<div className="alert error" data-testid="context-degraded-warning">${d.contextRestoreDegraded}</div>` : null}
      ${error ? html`<div className="alert error">${error}</div>` : null}
      <section className="context-card" data-testid="context-usage-card">
        <div className="context-card-title">${d.contextUsage} <span>${contextState}</span></div>
        <div className="context-meter">
          <div className="context-meter-track"><span style=${{ width: `${Math.max(0, Math.min(100, pct))}%` }}></span><i style=${{ left: "70%" }}></i><i style=${{ left: "90%" }}></i></div>
          <div className="context-meter-labels"><span>0%</span><span>${d.contextSoftThreshold}</span><span>${d.contextHardThreshold}</span><span>100%</span></div>
        </div>
        <div className="context-kv">
          <div data-testid="context-usage-used">${d.contextUsed}: ${tokenK(usage.used_tokens)} ${d.contextTokens}</div>
          <div data-testid="context-usage-window">${d.contextWindow}: ${tokenK(usage.window_tokens)} ${d.contextTokens}</div>
          <div data-testid="context-usage-percent">${d.contextUsagePercent}: ${pct}%</div>
          <div data-testid="context-soft-remaining">${d.contextSoftCompactIn}: ${tokenK(usage.tokens_until_soft_compact)}</div>
          <div data-testid="context-hard-remaining">${d.contextHardCompactIn}: ${tokenK(usage.tokens_until_hard_compact)}</div>
          <div data-testid="context-restore-mode">${d.contextMode}: ${contextModeText(data && data.restore_mode, d)}</div>
        </div>
      </section>
      <div className="context-actions">
        <button className="btn primary sm" data-testid="context-compact-now" onClick=${runManualCompact} disabled=${state === "compacting" || !sessionId}>${state === "compacting" ? d.contextCompactBusy : d.contextCompactNow}</button>
        <button className="btn sm" data-testid="context-copy-summary" onClick=${copySummary} disabled=${!latest}>${d.contextCopyCheckpointSummary}</button>
        <button className="btn sm" data-testid="context-refresh" onClick=${loadContext} disabled=${state === "compacting"}>${d.refresh}</button>
        <button className="btn sm" data-testid="context-view-raw-events" disabled=${true}>${d.contextViewRawEvents}</button>
      </div>
      ${state === "compacting" ? html`<div className="alert info" data-testid="context-compact-loading">${d.contextCompactBusy}</div>` : null}
      ${compactMsg ? html`<div className=${`alert ${compactMsg.startsWith(d.contextCompactFailed) ? "error" : "ok"}`} data-testid="context-compact-error">${compactMsg}</div>` : null}
      <section className="context-card" data-testid="context-runtime-state">
        <div className="context-card-title">${d.contextRuntimeState}</div>
        <div className="context-kv">
          <div data-testid="context-runtime-workspace">${d.contextWorkspace}: ${contextValue(runtime.workspace, d.contextUnknown)}</div>
          <div data-testid="context-runtime-cwd">${d.contextCwd}: ${contextValue(runtime.cwd, d.contextUnknown)}</div>
          <div data-testid="context-runtime-worktree">${d.contextWorktree}: ${contextValue(runtime.worktree, d.contextUnknown)}</div>
          <div data-testid="context-runtime-branch">${d.contextBranch}: ${contextValue(runtime.branch, d.contextUnknown)}</div>
          <div data-testid="context-runtime-base-ref">${d.contextBaseRef}: ${contextValue(runtime.base_ref, d.contextUnknown)}</div>
          <div data-testid="context-runtime-head-sha">${d.contextHeadSha}: ${contextValue(runtime.head_sha, d.contextUnknown)}</div>
        </div>
      </section>
      <section className="context-card" data-testid="context-agents-card">
        <div className="context-card-title">${d.contextAgents} <span>${agents.length}</span></div>
        ${agents.length ? html`<div className="context-agent-table">${agents.map((a) => html`<div className="context-agent-row" data-testid="context-agent-row" key=${a.agent_id || a.handle_id || JSON.stringify(a).slice(0, 40)}>
          <b>${contextText(a.agent_id || a.handle_id || d.contextAgent, 120)}</b><span>${contextText(a.agent_role || a.agent_type || d.contextUnknown, 120)}</span>
          <span className=${`agent-status ${String(a.status || "unknown").toLowerCase()}`} data-testid="context-agent-status">${contextStatusText(a.status, d)}</span>
          <span title=${contextText(a.cwd, 300)} data-testid="context-agent-cwd">${shortPath(contextText(a.cwd), d)}</span>
          <span title=${contextText(a.worktree, 300)} data-testid="context-agent-worktree">${shortPath(contextText(a.worktree), d)}</span>
          <span data-testid="context-agent-branch">${contextText(a.branch || d.contextUnknown, 120)}</span><span data-testid="context-agent-native-session">${contextText(a.native_session_id || "-", 160)}</span>
          <span>${contextText(a.last_seen_at, 120)}</span><span>${contextText(a.last_meaningful_output)}</span>
        </div>`)}</div>` : html`<div className="emptyline">${d.contextNoActiveAgents}</div>`}
      </section>
      <section className="context-card" data-testid="latest-checkpoint-card">
        <div className="context-card-title">${d.contextLatestCheckpoint}</div>
        ${latest ? html`<div className="context-kv">
          <div data-testid="latest-checkpoint-id">${d.contextId}: ${latest.id}</div><div data-testid="latest-checkpoint-trigger">${d.contextTrigger}: ${latest.trigger}</div>
          <div>${d.contextReason}: ${latest.reason}</div><div data-testid="latest-checkpoint-method">${d.contextMethod}: ${latest.method}</div>
          <div data-testid="latest-checkpoint-before-tokens">${d.contextBefore}: ${tokenK(latest.before_tokens)}</div><div data-testid="latest-checkpoint-after-tokens">${d.contextAfter}: ${tokenK(latest.after_tokens)}</div>
          <div data-testid="latest-checkpoint-items-count">${d.contextReplacementItems}: ${latest.replacement_history_items_count}</div><div>${d.contextStatus}: ${contextStatusText(latest.status, d)}</div>
        </div>` : html`<div className="emptyline">${d.contextNoCheckpoint}</div>`}
      </section>
      <section className="context-card" data-testid="context-lane-usage">
        <div className="context-card-title">${d.contextLaneUsage}</div>
        <div className="context-lanes">${[1,2,3,4,5,6,7].map((lane) => html`<div data-testid=${`context-lane-${lane}`} key=${lane}><span>${d.contextLane} ${lane}</span><b>${tokenK(lanes[String(lane)] || 0)}</b></div>`)}</div>
        ${(lanes["7"] || 0) > Math.max(2000, (usage.used_tokens || 0) * 0.2) ? html`<div className="alert info">${d.contextLane7High}</div>` : null}
      </section>
      <section className="context-card" data-testid="context-evidence-card">
        <div className="context-card-title">${d.contextEvidenceSummary}</div>
        <div className="context-evidence"><b>${d.contextChangedFiles}</b><ul data-testid="context-changed-files">${changed.length ? changed.map((x) => html`<li key=${x}>${x}</li>`) : html`<li>${d.contextNoChangedFiles}</li>`}</ul></div>
        <div className="context-evidence"><b>${d.contextLastTests}</b><ul data-testid="context-last-tests">${tests.length ? tests.map((x) => html`<li key=${x}>${x}</li>`) : html`<li>${d.contextNoTestResults}</li>`}</ul></div>
        <div className="context-evidence"><b>${d.contextNextSteps}</b><ul data-testid="context-next-steps">${steps.length ? steps.map((x) => html`<li key=${x}>${x}</li>`) : html`<li>${d.contextNoNextSteps}</li>`}</ul></div>
        <div className="context-evidence"><b>${d.contextLastCommands}</b><ul data-testid="context-last-commands">${commands.length ? commands.map((x) => html`<li key=${x}>${x}</li>`) : html`<li>${d.contextNoCommands}</li>`}</ul></div>
      </section>
      <section className="context-card" data-testid="checkpoint-list">
        <div className="context-card-title">${d.contextCheckpointList}</div>
        ${checkpoints.length ? html`<div className="checkpoint-table">${checkpoints.map((row) => html`<button className="checkpoint-row" data-testid="checkpoint-row" key=${row.id} onClick=${() => openCheckpoint(row)}>
          <span data-testid="checkpoint-row-created">${formatTime(row.created_at, lang)}</span><span data-testid="checkpoint-row-trigger">${row.trigger}</span>
          <span data-testid="checkpoint-row-reason">${row.reason}</span><span data-testid="checkpoint-row-method">${row.method}</span>
          <span data-testid="checkpoint-row-before">${tokenK(row.before_tokens)}</span><span data-testid="checkpoint-row-after">${tokenK(row.after_tokens)}</span>
          <span>${tokenK(Math.max(0, (row.before_tokens || 0) - (row.after_tokens || 0)))}</span><span>${row.replacement_history_items_count}</span>
          <span data-testid="checkpoint-row-status">${contextStatusText(row.status, d)}</span>
        </button>`)}</div>` : html`<div className="emptyline">${d.contextNoCheckpointShort}</div>`}
      </section>
      <section className="context-card" data-testid="checkpoint-detail">
        <div className="context-card-title">${d.contextCheckpointDetail}</div>
        ${detail ? html`<div><div data-testid="checkpoint-summary"><b>${d.contextSummary}</b><pre>${JSON.stringify(detail.summary || {}, null, 2)}</pre></div><div data-testid="checkpoint-runtime"><b>${d.contextRuntime}</b><pre>${JSON.stringify(detail.runtime_state || {}, null, 2)}</pre></div><div data-testid="checkpoint-token-usage"><b>${d.contextTokenUsage}</b><pre>${JSON.stringify(detail.token_usage || {}, null, 2)}</pre></div><div data-testid="checkpoint-source-cursor"><b>${d.contextSourceCursor}</b><pre>${JSON.stringify(detail.source_cursor || {}, null, 2)}</pre></div><div data-testid="checkpoint-warnings"><b>${d.contextWarnings}</b><pre>${JSON.stringify(detail.warnings || [], null, 2)}</pre></div></div>` : html`<div className="emptyline">${d.contextSelectCheckpoint}</div>`}
      </section>
      <details className="context-card active-context-preview" data-testid="active-context-preview">
        <summary data-testid="active-context-preview-toggle">${d.contextActivePreview}</summary>
        <pre data-testid="active-context-preview-content">${preview}</pre>
        ${previewTruncated && fullPreview === null ? html`<div className="context-preview-actions"><span className="context-preview-meta">${d.contextFullPreviewHelp}</span><button className="btn sm" data-testid="active-context-preview-load-full" onClick=${loadFullPreview} disabled=${previewLoading}>${previewLoading ? d.contextFullPreviewLoading : d.contextFullPreview}</button></div>` : null}
        ${previewError ? html`<div className="alert error">${previewError}</div>` : null}
        ${fullPreview !== null ? html`<div className="context-full-preview"><div className="context-preview-actions"><span className="context-preview-meta">${d.contextFullPreviewLoaded}</span><button className="btn sm" data-testid="active-context-preview-show-preview" onClick=${() => setFullPreview(null)}>${d.contextPreviewOnly}</button></div><textarea data-testid="active-context-preview-full-content" readOnly=${true} spellCheck=${false} value=${fullPreview}></textarea></div>` : null}
      </details>
    </div>`;
  }

  window.ForemanContextUI = {
    ContextPanel,
    contextText,
    contextValue,
    contextItems,
    contextStatusText,
    contextModeText,
  };
})();
