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
- **Checkpoint Manager**: auto git snapshot before each step (per-step granularity) → enables undo.

**Done when:** finishing a task auto-produces an LLM review with risks/suggestions, and any step is one-click revertible.

## P3 — Phone surface + Approvals
- PWA (`manifest` + service worker), installable, Web Push (VAPID).
- **Gate**: dangerous actions pause and push an approval card to the phone.
- Bearer-token auth + device pairing.
- Remote access via Tailscale (`tailscale serve` HTTPS).

**Done when:** a `git push` attempt pauses, your phone buzzes, you tap Approve, it resumes.

## P4 — Decision loop + two-way control ⭐ (the core interaction, see DESIGN.zh-CN.md §6)
- **Operator**: LLM (with MCP tools) condenses agent output and *proposes* the next command/action.
- **Auditor**: a second, independent LLM reviews every proposed command *before* execution; rejects
  garbage/off-track/dangerous ones back to the Operator (you never see the noise).
- **Decision Card**: condensed status + audit note + 2–4 one-tap options + free-text, on PC/phone.
- **Autonomy dial**: default **"ask about everything"** (capability is full; only the commit point is gated).
- Dispatch new tasks from the phone; multiple concurrent sessions; daily + "you're back" briefings.

**Done when:** an agent step flows Operator→Auditor→card→your tap→checkpoint→execute, and you run the
whole thing from your phone.

## P5 — Definition engine ⭐ (the "secret sauce" layer — the real value)
The open-core split: the engine is open; your **workflows / skills / code standards / QA rubrics** live as
data in the DB (see DESIGN.zh-CN.md §11.2). This phase makes them executable.
- `definitions` + `definition_links` + `workflow_runs` tables; (name, version) + is_active.
- **Hybrid workflow engine**: fixed step skeleton (with gates), each step's "how" driven by an LLM + the
  referenced skill.
- **Pre-injection**: before launching claude/codex, materialize the step's skill + code standard into the
  workspace (CLAUDE.md / AGENTS.md / skill files / appended system prompt).
- **QA-rubric-driven review**: Reviewer judges each step against the step's QA rubric; pass → next step.
- **DB migrations**: schema_version + migration runner so upgrades never wipe history.

**Done when:** a task runs an end-to-end DB-defined workflow, each step injected + QA-gated, nothing secret
in the repo.

## P6 — UI editor + extension points
- In-app (phone/web) editor to create/edit/version workflows, skills, code standards, QA rubrics.
- Export/backup of definitions to a private location; optional at-rest encryption of definition bodies.
- `Notifier` interface (Feishu / Telegram / Bark / email) + plugin discovery via Python entry points.
- A small set of generic, redacted example definitions shipped in the repo so OSS users can start.

**Done when:** you build and tweak your whole workflow library from your phone, code untouched.

## P7 — Enhancements (post-MVP)
- Multi-machine routing (capability/trust per machine).
- MCP integration; more agent adapters (Gemini CLI, Aider, …).
- Policy learning (remember your approval decisions to reduce future prompts).
- Conflict/traffic control for concurrent agents writing the same workspace.
