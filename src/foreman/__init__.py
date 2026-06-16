"""Foreman — a self-hosted PM agent supervising local coding agents.

See docs/DESIGN.zh-CN.md for the architecture. Package layout:

    config   — settings loading (YAML + .env)
    llm      — provider-agnostic LLM client (your own API)
    store    — SQLite models + session
    core     — PM Brain, Reviewer, Gate, Scheduler, Event Bus
    agents   — AgentAdapter protocol + Claude Code / Codex adapters + Runner
    monitor  — Claude Code hook receiver, git watcher, process watcher
    server   — FastAPI app (REST + WS), Web Push, auth
"""

__version__ = "0.1.0"
