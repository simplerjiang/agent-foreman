# Foreman 🦺

**A self-hosted PM agent for your local coding agents.**

Foreman is a daemon that runs on your PC and acts like a project manager / foreman over your local
AI coding agents (**Claude Code** and **Codex CLI**). It **monitors** them, **dispatches** work,
**reviews** their output with your own LLM, and **reports to your phone** via a self-hosted PWA —
buzzing you for approval when an agent wants to do something risky, and letting you hand it new
tasks while you're away from the keyboard.

> Inspired by [Cteno](https://github.com/zalan159/cteno-community), but deliberately scoped down to
> a single-machine MVP you can actually run tonight. See the full design in
> **[docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md)**.

---

## Why

Two painful moments when working with CLI agents:

- **When you walk away** — the agent stalls, drifts, or hits a "are you sure?" gate and just stops.
- **When you come back** — you have to archaeology the logs to recall where it got to.

Foreman puts an LLM-driven PM in the loop 24/7: it auto-reviews and lets safe work flow, **pauses
and pings your phone** at risk gates, and gives you a one-tap **approve / reject / redirect** from
anywhere.

## What it does

| | |
|---|---|
| 👀 **Monitor** | Watches Claude Code (via hooks) & Codex (via output + git) in real time. |
| 🎛️ **Dispatch** | Launch and steer `claude` / `codex` in a workspace — from the PC or your phone. |
| 🔍 **Review** | Sends diffs to **your own LLM API** for a structured verdict (risks, suggestions). |
| 🚦 **Gate** | Classifies actions safe / needs-strategy / requires-approval; risky ones wait for you. |
| 📱 **Report** | Pushes briefings & approval cards to a self-hosted PWA (iOS/Android), Web Push. |

## Architecture (1-minute version)

```
PC: PM Core (Python) ── drives ──▶ claude -p / codex exec   (your code workspaces)
        │  ▲                            │
        │  └── hooks / git / process ───┘   (monitoring)
        │
        └── FastAPI (REST + WS + WebPush) ──HTTPS via Tailscale──▶ 📱 Phone PWA
                                                                   (timeline / approve / dispatch)
```

Full diagram & component contracts: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Status

🚧 **Design + scaffold (P0).** This repo currently contains the design docs and a project skeleton.
Implementation follows the [roadmap](docs/ROADMAP.md): P1 single-machine driving → P2 review +
checkpoints → P3 phone+approvals → P4 decision loop (operator + auditor + decision cards) → P5
definition engine (open-core "secret sauce") → P6 UI editor → P7 team/relay mode.

Runs **personal** (local process serves its own PWA) or **team** (one shared server acts as a relay
for multiple users' local processes, each connecting with an access key). Same codebase. See
[DESIGN.zh-CN.md §8](docs/DESIGN.zh-CN.md).

## Stack

Python 3.11+ · FastAPI · SQLite (SQLModel) · httpx · Web Push (VAPID) · PWA service worker.
You bring your **own LLM API** (OpenAI-compatible or Anthropic-compatible).

## Quick start (P0 scaffold)

```bash
pip install -e .
cp .env.example .env            # put your LLM API base_url + key
cp config.example.yaml config.yaml
foreman serve                   # boots backend, opens DB, exposes /health
```

## Docs

- 🇨🇳 **[设计方案 DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md)** — the primary design document.
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — component contracts & APIs.
- [ROADMAP.md](docs/ROADMAP.md) — phased plan.
- [SECURITY.md](docs/SECURITY.md) — remote access & threat model.

## Code signing policy

Free code signing for Foreman (agent-foreman) is provided by [SignPath.io](https://signpath.io),
with a certificate issued by the [SignPath Foundation](https://signpath.org).

Roles:
- **Author / Committer:** `simplerjiang` — maintains the source code.
- **Reviewer:** `simplerjiang` — reviews changes from non-committers.
- **Approver:** `simplerjiang` — approves each code-signing request.

**Privacy:** This program will not transfer any information to other networked systems unless
specifically requested by the user or the person installing or operating it. Foreman is self-hosted;
phone reporting and the optional cloud relay are user-enabled features. The in-app self-update
downloads a new signed release from this project's GitHub Releases only when the user explicitly
chooses to update.

## License

MIT
