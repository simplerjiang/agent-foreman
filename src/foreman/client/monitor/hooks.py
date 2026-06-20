"""Claude Code hook receiver (TASKS T2.4).

Claude Code POSTs hook events (PreToolUse / PostToolUse / Stop / Notification) to
`POST /hooks` on the local backend. This is the cleanest real-time signal source (no
polling — DESIGN §4.3). The FastAPI route lives in server/app.py but stays shared-only;
this client-side `HookReceiver` is INJECTED into it (like store/bus) because:
  * /hooks is a LOCAL endpoint — Claude Code runs on the PC and curls the self-hosted app,
  * the receiver consults the client-side Gate to hold dangerous tool calls (§6.6).

Per-hook handling: map → persist (store) → publish (bus); for PreToolUse on a
`requires-approval` action, record an `approval_req` event and return a *deny* decision
(curl pipes our HTTP body back to Claude Code as the hook result, blocking the tool). The
full approval round-trip (push to phone, wait, resume) is P3 (Gate.request_approval).

See docs/DESIGN.zh-CN.md §4.3 / §6.6.
"""

from __future__ import annotations

from foreman.shared.events import AgentEvent, make_event

# Claude Code hook name → Foreman event type (DESIGN §7.1 vocabulary).
_HOOK_TYPE = {
    "PreToolUse": "tool_pre",
    "PostToolUse": "tool_post",
    "Stop": "stop",
    "SubagentStop": "stop",
    "Notification": "notification",
}

# tool_input fields worth feeding the Gate classifier (Bash command, edited path, fetched URL).
_ACTION_FIELDS = ("command", "file_path", "path", "url", "content")


def hook_to_event(hook_name: str, payload: dict, session_id: str) -> AgentEvent:
    """Map a Claude Code hook payload to a timestamped AgentEvent (T2.4)."""
    return make_event(
        _HOOK_TYPE.get(hook_name, "agent_output"),
        source="hook",
        session_id=session_id,
        payload={"hook": hook_name, **payload},
    )


def action_text(payload: dict) -> str:
    """Flatten a PreToolUse payload into the text the Gate classifies (tool + its inputs)."""
    parts = [str(payload.get("tool_name", ""))]
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        parts += [str(tool_input[k]) for k in _ACTION_FIELDS if isinstance(tool_input.get(k), str)]
    return " ".join(p for p in parts if p).strip()


def _deny(reason: str) -> dict:
    """A Claude Code hook-result body that blocks the tool call (both legacy + current shapes)."""
    return {
        "decision": "block",
        "reason": reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }


class HookReceiver:
    """Receives hook POSTs: map → persist → publish, and Gate-screen PreToolUse calls.

    store may be None (publish-only); gate may be None (no screening). Injected into
    server.app.create_app so app.py imports only shared (DESIGN §14 boundary).
    """

    def __init__(self, store: object | None, bus: object, gate: object | None = None) -> None:
        self.store = store
        self.bus = bus
        self.gate = gate

    async def handle(
        self, hook_name: str, payload: dict, session_id: str | None = None
    ) -> dict:
        """Process one hook POST; return the JSON body Claude Code reads as the hook result."""
        if not isinstance(payload, dict):
            payload = {"raw": payload}
        # Correlate to a Foreman session: explicit ?session_id wins, else Claude's native id.
        sid = session_id or payload.get("session_id") or "unknown"

        event = hook_to_event(hook_name, payload, sid)
        await self._record(event)

        if hook_name == "PreToolUse" and self.gate is not None:
            action = action_text(payload)
            if self.gate.classify(action) == "requires-approval":
                await self._record(
                    make_event(
                        "approval_req", "hook", sid, task_id=event.task_id,
                        payload={
                            "action": action,
                            "tool": payload.get("tool_name", ""),
                            "risk_level": "requires-approval",
                        },
                    )
                )
                return _deny(
                    f"Foreman Gate held this action pending approval (requires-approval): "
                    f"{action[:200]}"
                )
        return {"ok": True}

    async def _record(self, event: AgentEvent) -> None:
        """Persist THEN publish — mirrors Runner so a late-connecting UI can still backfill."""
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        await self.bus.publish(event)
