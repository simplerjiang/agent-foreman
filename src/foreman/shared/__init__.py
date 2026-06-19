"""Shared layer — used by BOTH the client (PC app) and the server.

Holds only cross-cutting types and contracts, never client- or server-specific logic
(see docs/DESIGN.zh-CN.md §14):

    config    — settings loading (YAML + .env)
    llm       — provider-agnostic LLM client (your own API)
    events    — AgentEvent, the event-type vocabulary, and the in-process EventBus
    protocol  — the local<->server WebSocket (wss) message contract (placeholder)
"""
