"""Foreman — a self-hosted PM agent supervising local coding agents.

See docs/DESIGN.zh-CN.md for the architecture. Target layout (DESIGN §14) is three
units — `shared` (both sides), `client` (PC app + agents), `server` (backend + PWA):

    shared   — config, LLM client, event types + bus, wss protocol contract
    client   — agents (Claude Code / Codex + Runner), monitor, core (operator/auditor/
               gate/reviewer/scheduler/supervisor/checkpoint), local store, computer-use
    server   — FastAPI relay + REST/WS, Web Push, auth, server store, PWA (web/)

Reshape in progress (TASKS P0.5): `shared/` is in place; `core`/`agents`/`monitor`/`store`
still sit at the top level pending the move under `client/` (T0.2), and the server side
is consolidated under `server/` (T0.3).
"""

# THE single source of truth for the Foreman version. Bump THIS line on every PR (AGENTS.md §四:
# +0.0.1, carry at 10). pyproject.toml reads it dynamically; /health + the PWA derive from it.
__version__ = "1.0.7"
