(function () {
  "use strict";

  const core = window.ForemanApp || {};
  const html = core.html;
  const useCallback = core.useCallback;
  const useEffect = core.useEffect;
  const useState = core.useState;
  const api = core.api;
  const friendlyError = core.friendlyError;
  const tokenK = core.tokenK;
  const shortPath = core.shortPath;
  const formatTime = core.formatTime;

  function contextValue(value) {
    if (value === undefined || value === null || value === "") return "unknown";
    if (Array.isArray(value)) return value.length ? value.join(", ") : "unknown";
    if (typeof value === "object") {
      try { return JSON.stringify(value); } catch (e) { return "unknown"; }
    }
    return String(value);
  }

  function contextItems(value) {
    if (!Array.isArray(value)) return [];
    return value.filter((x) => x !== undefined && x !== null && String(x).trim()).slice(0, 20);
  }

  function ContextPanel({ sessionRow, d, lang }) {
    const [state, setState] = useState("idle");
    const [data, setData] = useState(null);
    const [checkpoints, setCheckpoints] = useState([]);
    const [detail, setDetail] = useState(null);
    const [error, setError] = useState("");
    const [compactMsg, setCompactMsg] = useState("");
    const sessionId = sessionRow && sessionRow.id;
    const loadContext = useCallback(async () => {
      if (!sessionId) { setData(null); setCheckpoints([]); setDetail(null); return; }
      setState((prev) => prev === "ready" || prev === "degraded" ? prev : "loading");
      setError("");
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
      setCompactMsg("Context compacting...");
      setError("");
      try {
        const res = await api(`/api/sessions/${encodeURIComponent(sessionId)}/context/compact`, {
          method: "POST",
          body: { trigger: "manual", reason: "user_requested" },
        });
        if (!res || !res.ok) {
          const msg = res && res.error ? (res.error.message || res.error.code) : "context_compact_failed";
          setCompactMsg(`Context compact failed. ${msg}. Latest checkpoint was not changed.`);
          setState(data && data.degraded ? "degraded" : "ready");
          return;
        }
        setCompactMsg("Context compacted.");
        await loadContext();
      } catch (e) {
        setCompactMsg(`Context compact failed. ${friendlyError(e, d)}. Latest checkpoint was not changed.`);
        setState(data && data.degraded ? "degraded" : "ready");
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
    const tests = contextItems(runtime.last_tests).map((t) => typeof t === "object" ? JSON.stringify(t) : String(t));
    const steps = contextItems(runtime.next_steps);
    const commands = contextItems(runtime.last_commands).map((c) => typeof c === "object" ? JSON.stringify(c) : String(c));
    const degraded = !!(data && data.degraded);
    return html`<div className="context-panel" data-testid="context-panel">
      ${state === "loading" ? html`<div className="alert info">Loading context...</div>` : null}
      ${degraded ? html`<div className="alert error" data-testid="context-degraded-warning">Context restore degraded. Foreman fell back to raw materialized frames.</div>` : null}
      ${error ? html`<div className="alert error">${error}</div>` : null}
      <section className="context-card" data-testid="context-usage-card">
        <div className="context-card-title">Context Usage <span>${state === "compacting" ? "compacting" : degraded ? "degraded" : "healthy"}</span></div>
        <div className="context-meter">
          <div className="context-meter-track"><span style=${{ width: `${Math.max(0, Math.min(100, pct))}%` }}></span><i style=${{ left: "70%" }}></i><i style=${{ left: "90%" }}></i></div>
          <div className="context-meter-labels"><span>0%</span><span>70% soft</span><span>90% hard</span><span>100%</span></div>
        </div>
        <div className="context-kv">
          <div data-testid="context-usage-used">Used: ${tokenK(usage.used_tokens)} tokens</div>
          <div data-testid="context-usage-window">Window: ${tokenK(usage.window_tokens)} tokens</div>
          <div data-testid="context-usage-percent">Usage: ${pct}%</div>
          <div data-testid="context-soft-remaining">Soft compact in: ${tokenK(usage.tokens_until_soft_compact)}</div>
          <div data-testid="context-hard-remaining">Hard compact in: ${tokenK(usage.tokens_until_hard_compact)}</div>
          <div data-testid="context-restore-mode">Mode: ${data ? data.restore_mode : "unknown"}</div>
        </div>
      </section>
      <div className="context-actions">
        <button className="btn primary sm" data-testid="context-compact-now" onClick=${runManualCompact} disabled=${state === "compacting" || !sessionId}>${state === "compacting" ? "Compacting..." : "Compact Now"}</button>
        <button className="btn sm" data-testid="context-copy-summary" onClick=${copySummary} disabled=${!latest}>Copy Checkpoint Summary</button>
        <button className="btn sm" data-testid="context-refresh" onClick=${loadContext} disabled=${state === "compacting"}>${d.refresh}</button>
        <button className="btn sm" data-testid="context-view-raw-events" disabled=${true}>View Raw Events</button>
      </div>
      ${state === "compacting" ? html`<div className="alert info" data-testid="context-compact-loading">Context compacting...</div>` : null}
      ${compactMsg ? html`<div className=${`alert ${compactMsg.includes("failed") ? "error" : "ok"}`} data-testid="context-compact-error">${compactMsg}</div>` : null}
      <section className="context-card" data-testid="context-runtime-state">
        <div className="context-card-title">Runtime State</div>
        <div className="context-kv">
          <div data-testid="context-runtime-workspace">Workspace: ${contextValue(runtime.workspace)}</div>
          <div data-testid="context-runtime-cwd">CWD: ${contextValue(runtime.cwd)}</div>
          <div data-testid="context-runtime-worktree">Worktree: ${contextValue(runtime.worktree)}</div>
          <div data-testid="context-runtime-branch">Branch: ${contextValue(runtime.branch)}</div>
          <div data-testid="context-runtime-base-ref">Base ref: ${contextValue(runtime.base_ref)}</div>
          <div data-testid="context-runtime-head-sha">Head SHA: ${contextValue(runtime.head_sha)}</div>
        </div>
      </section>
      <section className="context-card" data-testid="context-agents-card">
        <div className="context-card-title">Agents <span>${agents.length}</span></div>
        ${agents.length ? html`<div className="context-agent-table">${agents.map((a) => html`<div className="context-agent-row" data-testid="context-agent-row" key=${a.agent_id || a.handle_id || JSON.stringify(a).slice(0, 40)}>
          <b>${a.agent_id || a.handle_id || "agent"}</b><span>${a.agent_role || a.agent_type || "unknown"}</span>
          <span className=${`agent-status ${String(a.status || "unknown").toLowerCase()}`} data-testid="context-agent-status">${a.status || "unknown"}</span>
          <span title=${a.cwd || ""} data-testid="context-agent-cwd">${shortPath(a.cwd || "", d)}</span>
          <span title=${a.worktree || ""} data-testid="context-agent-worktree">${shortPath(a.worktree || "", d)}</span>
          <span data-testid="context-agent-branch">${a.branch || "unknown"}</span><span data-testid="context-agent-native-session">${a.native_session_id || "-"}</span>
          <span>${a.last_seen_at || ""}</span><span>${a.last_meaningful_output || ""}</span>
        </div>`)}</div>` : html`<div className="emptyline">No active agents captured yet.</div>`}
      </section>
      <section className="context-card" data-testid="latest-checkpoint-card">
        <div className="context-card-title">Latest Checkpoint</div>
        ${latest ? html`<div className="context-kv">
          <div data-testid="latest-checkpoint-id">ID: ${latest.id}</div><div data-testid="latest-checkpoint-trigger">Trigger: ${latest.trigger}</div>
          <div>Reason: ${latest.reason}</div><div data-testid="latest-checkpoint-method">Method: ${latest.method}</div>
          <div data-testid="latest-checkpoint-before-tokens">Before: ${tokenK(latest.before_tokens)}</div><div data-testid="latest-checkpoint-after-tokens">After: ${tokenK(latest.after_tokens)}</div>
          <div data-testid="latest-checkpoint-items-count">Replacement history items: ${latest.replacement_history_items_count}</div><div>Status: ${latest.status}</div>
        </div>` : html`<div className="emptyline">No context checkpoint yet. Foreman is using raw materialized frames for this session.</div>`}
      </section>
      <section className="context-card" data-testid="context-lane-usage">
        <div className="context-card-title">Lane Usage</div>
        <div className="context-lanes">${[1,2,3,4,5,6,7].map((lane) => html`<div data-testid=${`context-lane-${lane}`} key=${lane}><span>Lane ${lane}</span><b>${tokenK(lanes[String(lane)] || 0)}</b></div>`)}</div>
        ${(lanes["7"] || 0) > Math.max(2000, (usage.used_tokens || 0) * 0.2) ? html`<div className="alert info">Lane 7 noise is high; compact may be triggered before the next PM turn.</div>` : null}
      </section>
      <section className="context-card" data-testid="context-evidence-card">
        <div className="context-card-title">Evidence Summary</div>
        <div className="context-evidence"><b>Changed Files</b><ul data-testid="context-changed-files">${changed.length ? changed.map((x) => html`<li key=${x}>${x}</li>`) : html`<li>No changed files captured yet.</li>`}</ul></div>
        <div className="context-evidence"><b>Last Tests</b><ul data-testid="context-last-tests">${tests.length ? tests.map((x) => html`<li key=${x}>${x}</li>`) : html`<li>No test results captured yet.</li>`}</ul></div>
        <div className="context-evidence"><b>Next Steps</b><ul data-testid="context-next-steps">${steps.length ? steps.map((x) => html`<li key=${x}>${x}</li>`) : html`<li>No next steps captured yet.</li>`}</ul></div>
        <div className="context-evidence"><b>Last Commands</b><ul data-testid="context-last-commands">${commands.length ? commands.map((x) => html`<li key=${x}>${x}</li>`) : html`<li>No commands captured yet.</li>`}</ul></div>
      </section>
      <section className="context-card" data-testid="checkpoint-list">
        <div className="context-card-title">Checkpoint List</div>
        ${checkpoints.length ? html`<div className="checkpoint-table">${checkpoints.map((row) => html`<button className="checkpoint-row" data-testid="checkpoint-row" key=${row.id} onClick=${() => openCheckpoint(row)}>
          <span data-testid="checkpoint-row-created">${formatTime(row.created_at, lang)}</span><span data-testid="checkpoint-row-trigger">${row.trigger}</span>
          <span data-testid="checkpoint-row-reason">${row.reason}</span><span data-testid="checkpoint-row-method">${row.method}</span>
          <span data-testid="checkpoint-row-before">${tokenK(row.before_tokens)}</span><span data-testid="checkpoint-row-after">${tokenK(row.after_tokens)}</span>
          <span>${tokenK(Math.max(0, (row.before_tokens || 0) - (row.after_tokens || 0)))}</span><span>${row.replacement_history_items_count}</span>
          <span data-testid="checkpoint-row-status">${row.status}</span>
        </button>`)}</div>` : html`<div className="emptyline">No context checkpoint yet.</div>`}
      </section>
      <section className="context-card" data-testid="checkpoint-detail">
        <div className="context-card-title">Checkpoint Detail</div>
        ${detail ? html`<div><div data-testid="checkpoint-summary"><b>Summary</b><pre>${JSON.stringify(detail.summary || {}, null, 2)}</pre></div><div data-testid="checkpoint-runtime"><b>Runtime</b><pre>${JSON.stringify(detail.runtime_state || {}, null, 2)}</pre></div><div data-testid="checkpoint-token-usage"><b>Token Usage</b><pre>${JSON.stringify(detail.token_usage || {}, null, 2)}</pre></div><div data-testid="checkpoint-source-cursor"><b>Source Cursor</b><pre>${JSON.stringify(detail.source_cursor || {}, null, 2)}</pre></div><div data-testid="checkpoint-warnings"><b>Warnings</b><pre>${JSON.stringify(detail.warnings || [], null, 2)}</pre></div></div>` : html`<div className="emptyline">Select a checkpoint to inspect sanitized detail.</div>`}
      </section>
      <details className="context-card active-context-preview" data-testid="active-context-preview">
        <summary data-testid="active-context-preview-toggle">Active Context Preview</summary>
        <pre data-testid="active-context-preview-content">${(data && data.active_context_preview) || ""}</pre>
      </details>
    </div>`;
  }

  window.ForemanContextUI = {
    ContextPanel,
    contextValue,
    contextItems,
  };
})();
