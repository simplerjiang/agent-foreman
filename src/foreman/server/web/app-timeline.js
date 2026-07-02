(function () {
  "use strict";

  const core = window.ForemanApp || {};
  const html = core.html;
  const useEffect = core.useEffect;
  const useMemo = core.useMemo;
  const useState = core.useState;

  function helperValue(helpers, name, fallback) {
    return helpers && helpers[name] ? helpers[name] : fallback;
  }

  function PmElapsed({ start, lang }) {
    const startMs = useMemo(() => {
      const t = new Date(start).getTime();
      return Number.isNaN(t) ? Date.now() : t;
    }, [start]);
    const [now, setNow] = useState(() => Date.now());
    useEffect(() => {
      const id = setInterval(() => setNow(Date.now()), 1000);
      return () => clearInterval(id);
    }, []);
    const secs = Math.max(0, Math.round((now - startMs) / 1000));
    return html`<span className="pm-elapsed">· ${lang === "zh" ? `已 ${secs} 秒` : `${secs}s`}</span>`;
  }

  function StepRow({ s, d, helpers }) {
    const stepMeta = helperValue(helpers, "STEP_META", {});
    const clip = helperValue(helpers, "clip", (value) => String(value || ""));
    const meta = stepMeta[s.kind] || stepMeta.tool || { k: "kTool", cls: "st-tool" };
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

  function timelineLabel(item, d, helpers) {
    const stepMeta = helperValue(helpers, "STEP_META", {});
    if (item.kind === "cmd") return d.commandsRun;
    if (item.kind === "reply") return item.final ? d.finalReply : d.reply;
    if (item.kind === "live") return d.processLabel;
    if (item.kind === "step") {
      const meta = stepMeta[(item.step && item.step.kind) || "tool"] || stepMeta.tool || {};
      return d[meta.k] || d.processLabel;
    }
    return d.processLabel;
  }

  function CallTimelineItem({ item, i, d, helpers }) {
    const MD = helperValue(helpers, "MD", ({ text }) => text || "");
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
        <div className="stage-name">${timelineLabel(item, d, helpers)}</div>
        ${item.kind === "cmd" ? html`<div className="term-block"><div className=${item.launch ? "cmd-launch" : ""}><span className="cmd-prompt">$</span> ${item.command || ""}</div></div>` : null}
        ${item.kind === "step" ? html`<div className="proc-steps single"><${StepRow} s=${step} d=${d} helpers=${helpers} /></div>` : null}
        ${item.kind === "reply" ? html`<div className=${`stage-reply${item.final ? " final" : ""}`}><${MD} text=${item.text || ""} maxChars=${item.final ? 6000 : 2400} /></div>` : null}
        ${item.kind === "live" ? html`<div className="proc-live"><span className="proc-bar"><span></span></span><span className="proc-txt">${d.executing}...</span></div>` : null}
      </div>
    </div>`;
  }

  function CallCard({ c, d, lang, open, onToggle, helpers }) {
    const firstSubstantiveLine = helperValue(helpers, "firstSubstantiveLine", (value) => String(value || "").slice(0, 60));
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
        <span className="call-toggle">${open ? d.hide : d.open}${open ? " ▲" : " ▼"}</span>
      </div>
      ${running ? html`<div className="call-progress"><span></span></div>` : null}
      ${open ? html`<div className="call-detail timeline">
        ${timeline.length ? timeline.map((item, i) => html`<${CallTimelineItem} key=${i} item=${item} i=${i} d=${d} helpers=${helpers} />`) : html`<div className="stage-muted">${d.noSteps}</div>`}
        ${c.diffs.length ? html`<div className="proc-diffs"><div className="step-sub">${d.changeDetail}</div>${c.diffs.map((df, i) => html`<div className="diff-file" key=${i}><div className="fhead"><span className="muted" style=${{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>${df.file}</span><span className="stat">${df.stat}</span></div>${(df.lines || []).slice(0, 30).map((l, j) => html`<div className=${`diff-line ${l.kind === "add" ? "add" : l.kind === "del" ? "del" : ""}`} key=${j}>${l.kind === "add" ? "+" : l.kind === "del" ? "-" : " "}${l.text || ""}</div>`)}</div>`)}</div>` : null}
      </div>` : null}
    </div>`;
  }

  function BubbleCopy({ text, d, onCopy, inverted = false }) {
    const copyText = String(text || "");
    if (!copyText.trim() || !onCopy) return null;
    return html`<div className="bubble-actions">
      <button type="button" className=${`bubble-copy${inverted ? " invert" : ""}`} aria-label=${d.copy} title=${d.copy} onClick=${(ev) => { ev.stopPropagation(); onCopy(copyText); }}>⧉</button>
    </div>`;
  }

  function ThinkingPanel({ d, text, helpers }) {
    const MD = helperValue(helpers, "MD", ({ text: value }) => value || "");
    const pmThinkingParts = helperValue(helpers, "pmThinkingParts", (value) => ({ title: d.thinkingTrace, body: value || "" }));
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

  function PmActivity({ n, d, helpers }) {
    const stepMeta = helperValue(helpers, "STEP_META", {});
    const meta = stepMeta[n.stepKind] || stepMeta.tool || { k: "kTool", cls: "st-tool" };
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

  function ContextPackPanel({ n, d, lang, helpers }) {
    const tokenK = helperValue(helpers, "tokenK", core.tokenK || ((value) => String(value || 0)));
    const formatTime = helperValue(helpers, "formatTime", core.formatTime || ((value) => String(value || "")));
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

  function ThreadNode({ n, dig, d, lang, openCalls, toggleCall, onCard, onApproval, openDetail, onCopy, helpers }) {
    const MD = helperValue(helpers, "MD", ({ text }) => text || "");
    const formatTime = helperValue(helpers, "formatTime", core.formatTime || ((value) => String(value || "")));
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
          ${n.steps.length ? html`${n.steps.map((s, i) => html`<div className="plan-step" key=${i}><span className="num">${i + 1}</span><span className="txt">${s}</span></div>`)}` : null}
        </div>
      </div>`;
    }
    if (n.kind === "pm-status") {
      return html`<div className="pm-status"><span className="spin"></span><span>${n.text}</span>${n.started ? html`<${PmElapsed} start=${n.started} lang=${lang} />` : null}</div>`;
    }
    if (n.kind === "pm-review") {
      const detail = [n.summary, n.reason, n.followUp ? `-> ${n.followUp}` : ""].filter(Boolean).join("\n\n");
      return html`<details className=${`pm-review${n.done ? " done" : ""}`}>
        <summary><span>${d.pmReviewDiag}</span><span className="pm-review-status">${n.status}</span></summary>
        ${detail ? html`<div className="pm-review-body"><${MD} text=${detail} maxChars=${2400} /></div>` : null}
      </details>`;
    }
    if (n.kind === "pm") {
      return html`<div className="pm-note"><div className="pm-avatar">PM</div><div className="body"><${MD} text=${n.text} maxChars=${4000} /><${BubbleCopy} text=${n.text} d=${d} onCopy=${onCopy} /></div></div>`;
    }
    if (n.kind === "pm-thinking") {
      return html`<${ThinkingPanel} d=${d} text=${n.text} helpers=${helpers} />`;
    }
    if (n.kind === "pm-activity") {
      return html`<${PmActivity} n=${n} d=${d} helpers=${helpers} />`;
    }
    if (n.kind === "context-pack") {
      return html`<${ContextPackPanel} n=${n} d=${d} lang=${lang} helpers=${helpers} />`;
    }
    if (n.kind === "call") {
      const c = dig.calls.get(n.callId);
      if (!c) return null;
      return html`<${CallCard} c=${c} d=${d} lang=${lang} open=${!!openCalls[c.id]} onToggle=${toggleCall} helpers=${helpers} />`;
    }
    if (n.kind === "card") {
      const p = n.payload || {};
      const opts = Array.isArray(p.options) ? p.options : [];
      const isQuestion = !p.action_id;
      return html`<div className="dcard">
        <div className="dcard-head"><span>${isQuestion ? "PM" : "!"}</span><span className="ttl">${isQuestion ? "PM question" : d.decisionNeeded}</span>${isQuestion ? null : html`<span className="risk tag amber">${d.riskMedium}</span>`}</div>
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

  window.ForemanTimelineUI = {
    ThreadNode,
    ContextPackPanel,
    ThinkingPanel,
    PmActivity,
    BubbleCopy,
    CallCard,
    CallTimelineItem,
    StepRow,
    PmElapsed,
  };
})();
