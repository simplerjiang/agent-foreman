# Roadmap

Each phase is a **vertical, runnable slice** — something you can start and demo, not just plumbing.

## P0 — Scaffold ✅ (this repo)
- Project layout, `pyproject.toml`, config + `.env` loading.
- Provider-agnostic LLM client (OpenAI-compatible / Anthropic-compatible).
- SQLite store + models.
- `foreman serve` / `foreman dispatch` entrypoints (stubs).

**Done when:** `foreman serve` boots, reads config, opens the DB, exposes `/health`.

## P1 — Single-machine driving
- `AgentAdapter` for Claude Code (headless `claude -p --output-format stream-json`).
- `AgentAdapter` for Codex (`codex exec`).
- Agent Runner: launch in a workspace, stream structured events into the event bus + DB.
- Minimal **local** web dashboard (no auth yet) showing live session + timeline.

**Done when:** you `dispatch` a task from the terminal, watch claude/codex run, and see events in the browser.

## P2 — Observation + Review
- Claude Code **hooks** receiver (`PreToolUse` / `PostToolUse` / `Stop` / `Notification`).
- Git watcher (diff/commit) + process/idle detection.
- **Reviewer**: on checkpoint, send diff+goal to your LLM → structured verdict.

**Done when:** finishing a task auto-produces an LLM review with risks/suggestions.

## P3 — Phone surface + Approvals
- PWA (`manifest` + service worker), installable, Web Push (VAPID).
- **Gate**: dangerous actions pause and push an approval card to the phone.
- Bearer-token auth + device pairing.
- Remote access via Tailscale (`tailscale serve` HTTPS).

**Done when:** a `git push` attempt pauses, your phone buzzes, you tap Approve, it resumes.

## P4 — Two-way control
- Dispatch new tasks from the phone.
- Multiple concurrent sessions + a dashboard to switch between them.
- Scheduler: daily briefings + "you're back" active briefing.

**Done when:** you create and steer a task entirely from your phone, away from the PC.

## P5 — Enhancements (post-MVP)
- Multi-machine routing (capability/trust per machine).
- MCP integration; more agent adapters (Gemini CLI, Aider, …).
- Policy learning (remember your approval decisions to reduce future prompts).
- Conflict/traffic control for concurrent agents writing the same workspace.
