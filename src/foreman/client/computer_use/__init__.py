"""Operator Toolbelt: computer-use executors (DESIGN §4.7).

Screenshot (hide / show / highlight-magnify cursor), mouse, keyboard, and shell (incl. admin).
Runs in the user's interactive session — no Session-0 limit, because the PC app is a normal session
app (DESIGN §3.1). Capabilities are given in full, but every "move" still flows Auditor → Gate →
decision card (DESIGN §6.4): the Toolbelt classifies each capability into the Gate's risk vocabulary
and fails closed on requires-approval (admin shell, dangerous commands) without an explicit approval.
"""

from .toolbelt import (
    CURSOR_MODES,
    HIDE_CURSOR,
    HIGHLIGHT_CURSOR,
    KIND_KEYBOARD_HOTKEY,
    KIND_KEYBOARD_TYPE,
    KIND_MOUSE_CLICK,
    KIND_MOUSE_DRAG,
    KIND_MOUSE_MOVE,
    KIND_SCREENSHOT,
    KIND_SHELL,
    NEEDS_STRATEGY,
    REQUIRES_APPROVAL,
    SAFE,
    SHOW_CURSOR,
    ToolCall,
    ToolResult,
    Toolbelt,
    capability_risk,
)

__all__ = [
    "Toolbelt",
    "ToolResult",
    "ToolCall",
    "capability_risk",
    "HIDE_CURSOR",
    "SHOW_CURSOR",
    "HIGHLIGHT_CURSOR",
    "CURSOR_MODES",
    "SAFE",
    "NEEDS_STRATEGY",
    "REQUIRES_APPROVAL",
    "KIND_SCREENSHOT",
    "KIND_MOUSE_MOVE",
    "KIND_MOUSE_CLICK",
    "KIND_MOUSE_DRAG",
    "KIND_KEYBOARD_TYPE",
    "KIND_KEYBOARD_HOTKEY",
    "KIND_SHELL",
]
