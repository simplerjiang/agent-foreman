You are doing an independent END-TO-END ACCEPTANCE of a UI redesign for the "Foreman" web app
(a self-hosted PM-agent dashboard). Be skeptical and concrete. Your job: confirm every page is
FULLY FUNCTIONAL and faithfully implements the handoff design — and report anything broken,
unwired, empty, or that diverges from the design.

## Source of truth (the design)
Read the handoff design in full:
- `design_handoff/ui/project/Foreman Redesign.dc.html` (the prototype: Launch, Workbench with
  Workspace/Decisions/Briefings/Playbook/Settings, mobile). Warm-paper theme, Plus Jakarta Sans.

## The implementation under review
- `src/foreman/server/web/index.html`, `src/foreman/server/web/app.css`,
  `src/foreman/server/web/app.js` (a custom React+htm SPA; NO Ant Design in personal mode).
- New backend: `src/foreman/client/core/cloud.py` (CloudManager), cloud endpoints in
  `src/foreman/server/app.py` (`/api/settings/cloud`, `/connect`, `/disconnect`),
  `src/foreman/client/relay.py` (on_status callback).

## A LIVE seeded server is running
Base URL: http://127.0.0.1:8821  (personal mode, loopback, no auth token needed).
It has one seeded session ("重构 auth 模块") with a full event timeline, one open decision card,
one pending approval, and 8 example playbook definitions.

Use curl to exercise it. Useful endpoints:
- GET /health, GET /api/overview, GET /api/sessions
- GET /api/cards , POST /api/cards/{id}/choose {"option":"approve"}
- GET /api/approvals , POST /api/approvals/{id} {"decision":"approve","nonce":"..."}
- GET /api/definitions , GET /api/reports , POST /api/reports/generate {"kind":"active-briefing"}
- GET /api/settings/llm , GET /api/settings/autonomy , POST /api/settings/autonomy {"level":2}
- GET /api/settings/cloud , POST /api/settings/cloud {"url":"wss://x/relay","access_key":"k"},
  POST /api/settings/cloud/connect , POST /api/settings/cloud/disconnect
- GET /api/workspaces , GET /api/settings/agents , GET /api/models
- GET / (the page), GET /app.js , GET /app.css , GET /vendor/fonts/*.woff2

## What to verify (be exhaustive)
1. The page boots (GET / serves the SPA; app.js + app.css + self-hosted fonts load with 200).
2. Each of the 5 nav views is implemented in app.js and wired to the right API:
   - Workspace: chat thread built from the event stream (dispatch→user bubble, pm_plan→plan card,
     pm_output→PM note, agent_output/tool_pre/git_diff→collapsible subagent "call" panels with
     reply+commands+diff), inline decision card + inline approval, composer that dispatches
     (/api/tasks) with Fast/Std/Deep→effort and a context meter + compact, and a right panel with
     To-dos / Subagents / Terminal tabs derived from events.
   - Decisions: lists /api/cards + /api/approvals with working approve/reject/choose.
   - Briefings: generate (/api/reports/generate) + history from /api/reports.
   - Playbook: 8 definition cards with kind tags + filter pills + new/edit/delete/activate +
     import/export, wired to /api/definitions.
   - Settings: workspaces (add/remove), local agents, PM brain (llm save/clear), CLOUD CONNECTION
     (save url+key, connect/disconnect, live status), autonomy slider, theme, language, push.
3. NEW features are actually functional, not placeholders: cloud connection (save persists, connect
   returns a real status — with no relay it must FAIL GRACEFULLY, not crash), subagent panels,
   to-do list, terminal view, launch splash.
4. Security invariant: untrusted agent output is rendered via React, never `.innerHTML` /
   `dangerouslySetInnerHTML` (grep app.js to confirm).
5. Anything in the design that is missing or rendered empty/non-functional in the implementation.

## Output
A concise findings list. For each finding: SEVERITY (blocker/major/minor), the file or endpoint,
what's wrong, and the concrete fix. If a page is fully functional, say so explicitly. End with a
one-line verdict: "ACCEPTANCE: PASS" only if there are no blocker/major findings, else "ACCEPTANCE: FAIL".
Do NOT modify any files — review only.
