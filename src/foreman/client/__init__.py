"""Client — the PC app: drives claude/codex, watches them, holds the local store + 秘方.

One user-session process (open=online, close=offline; DESIGN §3.1). Depends only on
`foreman.shared`, never on `foreman.server` (the server is a relay; 秘方 stays local —
DESIGN §8.3/§14).

    agents       — AgentAdapter protocol + Claude Code / Codex adapters + Runner
    core         — operator / auditor / gate / reviewer / scheduler / supervisor / checkpoint
    monitor      — Claude Code hook receiver, git watcher, process/idle watcher
    store        — local SQLite (sessions / tasks / events / 秘方 definitions)
    computer_use — screenshot (cursor render options) / mouse / keyboard (placeholder, §4.7)
"""
