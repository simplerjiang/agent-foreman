"""Tests for the Operator Toolbelt — shell / screenshot / mouse / keyboard executors (T4.5, §4.7).

Covers: capability risk classification (screenshot/move=safe, click/drag/type/hotkey=needs-strategy,
admin shell=requires-approval, command text deferred to the Gate, unknown→fail-closed); the
fail-closed guard (requires-approval never runs without approved=True); screenshot cursor render
options (hide/show/highlight + default self-cursor); backend execution via injected fakes; backend
errors surface as ok=False without crashing; and the `plan` → ToolCall gating description. No real
GUI: every backend is faked.
"""

from __future__ import annotations

from foreman.client.computer_use import (
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
    Toolbelt,
    capability_risk,
)
from foreman.shared.config import GatesCfg

# ── fakes ────────────────────────────────────────────────────────────────────────────────────────


class _FakeScreen:
    def __init__(self) -> None:
        self.calls: list = []

    def capture(self, *, region=None, cursor_mode=HIDE_CURSOR) -> bytes:
        self.calls.append({"region": region, "cursor_mode": cursor_mode})
        return b"\x89PNG-fake-bytes"


class _FakeMouse:
    def __init__(self) -> None:
        self.calls: list = []

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def click(self, x, y, button):
        self.calls.append(("click", x, y, button))

    def drag(self, x1, y1, x2, y2, button):
        self.calls.append(("drag", x1, y1, x2, y2, button))


class _FakeKeyboard:
    def __init__(self) -> None:
        self.calls: list = []

    def type(self, text):
        self.calls.append(("type", text))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))


class _FakeShell:
    def __init__(self, returncode=0, stdout="ok", stderr="") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.calls: list = []

    def run(self, command, *, admin=False):
        self.calls.append((command, admin))
        return {"returncode": self.returncode, "stdout": self.stdout, "stderr": self.stderr}


class _BoomBackend:
    """Any method raises — to assert errors surface as ok=False, not a crash."""

    def __getattr__(self, _name):
        def _raise(*_a, **_k):
            raise RuntimeError("backend exploded /secret/path")

        return _raise


def _gate():
    # rm -rf / git push are requires-approval; npm install is needs-strategy.
    return _G(GatesCfg(requires_approval=["rm -rf", "git push"], needs_strategy=["npm install"]))


class _G:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def classify(self, action_text: str) -> str:
        low = action_text.lower()
        if any(p.lower() in low for p in self.cfg.requires_approval):
            return REQUIRES_APPROVAL
        if any(p.lower() in low for p in self.cfg.needs_strategy):
            return NEEDS_STRATEGY
        return SAFE


# ── capability_risk classification ────────────────────────────────────────────────────────────────


def test_risk_screenshot_and_move_are_safe():
    assert capability_risk(KIND_SCREENSHOT) == SAFE
    assert capability_risk(KIND_MOUSE_MOVE) == SAFE


def test_risk_gui_actions_are_needs_strategy():
    for kind in (KIND_MOUSE_CLICK, KIND_MOUSE_DRAG, KIND_KEYBOARD_TYPE, KIND_KEYBOARD_HOTKEY):
        assert capability_risk(kind) == NEEDS_STRATEGY


def test_risk_admin_shell_always_requires_approval():
    # Even a totally benign command, when run as admin, must be requires-approval (§4.7).
    assert capability_risk(KIND_SHELL, admin=True, command="echo hi", gate=_gate()) == REQUIRES_APPROVAL


def test_risk_shell_text_deferred_to_gate():
    g = _gate()
    assert capability_risk(KIND_SHELL, command="git push origin main", gate=g) == REQUIRES_APPROVAL
    assert capability_risk(KIND_SHELL, command="npm install", gate=g) == NEEDS_STRATEGY
    assert capability_risk(KIND_SHELL, command="ls -la", gate=g) == SAFE


def test_risk_shell_without_gate_is_needs_strategy():
    assert capability_risk(KIND_SHELL, command="anything") == NEEDS_STRATEGY


def test_risk_unknown_capability_fails_closed():
    assert capability_risk("teleport") == REQUIRES_APPROVAL


# ── screenshot + cursor render options ──────────────────────────────────────────────────────────


def test_screenshot_default_self_cursor_is_hidden():
    screen = _FakeScreen()
    tb = Toolbelt(screen=screen)
    res = tb.screenshot()
    assert res.ok and res.risk == SAFE
    assert screen.calls[0]["cursor_mode"] == HIDE_CURSOR
    assert res.data["cursor"] == HIDE_CURSOR
    assert res.image == b"\x89PNG-fake-bytes"
    assert res.data["bytes"] == len(b"\x89PNG-fake-bytes")


def test_screenshot_explicit_highlight_cursor():
    screen = _FakeScreen()
    tb = Toolbelt(screen=screen)
    res = tb.screenshot(cursor=HIGHLIGHT_CURSOR, region=(0, 0, 100, 80))
    assert screen.calls[0]["cursor_mode"] == HIGHLIGHT_CURSOR
    assert screen.calls[0]["region"] == (0, 0, 100, 80)
    assert res.data["cursor"] == HIGHLIGHT_CURSOR


def test_screenshot_invalid_cursor_falls_back_to_self_default():
    screen = _FakeScreen()
    tb = Toolbelt(screen=screen, self_cursor=SHOW_CURSOR)
    tb.screenshot(cursor="bogus")
    assert screen.calls[0]["cursor_mode"] == SHOW_CURSOR


def test_screenshot_backend_error_surfaces():
    tb = Toolbelt(screen=_BoomBackend())
    res = tb.screenshot()
    assert not res.ok and res.kind == KIND_SCREENSHOT
    assert "RuntimeError" in res.error


# ── mouse / keyboard execution ────────────────────────────────────────────────────────────────────


def test_move_mouse_runs_backend_and_is_safe():
    mouse = _FakeMouse()
    tb = Toolbelt(mouse=mouse)
    res = tb.move_mouse(10, 20)
    assert res.ok and res.risk == SAFE
    assert mouse.calls == [("move", 10, 20)]


def test_click_runs_backend_needs_strategy():
    mouse = _FakeMouse()
    tb = Toolbelt(mouse=mouse)
    res = tb.click(5, 6, button="right")
    assert res.ok and res.risk == NEEDS_STRATEGY
    assert mouse.calls == [("click", 5, 6, "right")]


def test_drag_and_type_and_hotkey():
    mouse, kb = _FakeMouse(), _FakeKeyboard()
    tb = Toolbelt(mouse=mouse, keyboard=kb)
    assert tb.drag(1, 2, 3, 4).ok
    assert tb.type_text("hello").ok
    assert tb.hotkey("ctrl", "s").ok
    assert mouse.calls == [("drag", 1, 2, 3, 4, "left")]
    assert kb.calls == [("type", "hello"), ("hotkey", ("ctrl", "s"))]


def test_gui_backend_error_surfaces():
    tb = Toolbelt(mouse=_BoomBackend())
    res = tb.click(1, 1)
    assert not res.ok and "RuntimeError" in res.error


# ── shell + fail-closed guard ──────────────────────────────────────────────────────────────────────


def test_safe_shell_runs():
    shell = _FakeShell(returncode=0, stdout="done")
    tb = Toolbelt(shell=shell, gate=_gate())
    res = tb.run_shell("ls -la")
    assert res.ok and res.risk == SAFE
    assert res.data["stdout"] == "done"
    assert shell.calls == [("ls -la", False)]


def test_nonzero_exit_is_not_ok():
    shell = _FakeShell(returncode=2, stderr="boom")
    tb = Toolbelt(shell=shell, gate=_gate())
    res = tb.run_shell("ls -la")
    assert not res.ok and res.data["returncode"] == 2


def test_dangerous_shell_fails_closed_without_approval():
    shell = _FakeShell()
    tb = Toolbelt(shell=shell, gate=_gate())
    res = tb.run_shell("git push origin main")
    assert not res.ok and res.risk == REQUIRES_APPROVAL
    assert res.error == "approval_required"
    assert shell.calls == []  # the backend must NEVER have been touched


def test_admin_shell_fails_closed_without_approval():
    shell = _FakeShell()
    tb = Toolbelt(shell=shell, gate=_gate())
    res = tb.run_shell("echo hi", admin=True)
    assert not res.ok and res.risk == REQUIRES_APPROVAL
    assert shell.calls == []


def test_dangerous_shell_runs_once_approved():
    shell = _FakeShell(returncode=0)
    tb = Toolbelt(shell=shell, gate=_gate())
    res = tb.run_shell("git push origin main", approved=True)
    assert res.ok and res.risk == REQUIRES_APPROVAL
    assert shell.calls == [("git push origin main", False)]


def test_needs_strategy_click_is_not_blocked_at_toolbelt():
    # GUI actions are needs-strategy (gray): the toolbelt runs them; the auto/card/report decision
    # belongs to the dial layer (T4.6), not the toolbelt's hard fail-closed guard.
    mouse = _FakeMouse()
    tb = Toolbelt(mouse=mouse)
    assert tb.click(1, 1).ok


def test_shell_backend_error_surfaces():
    tb = Toolbelt(shell=_BoomBackend(), gate=_gate())
    res = tb.run_shell("ls")
    assert not res.ok and "RuntimeError" in res.error
    # the secret-bearing path is truncated/typed, not a raw repr leaking everything
    assert res.error.startswith("RuntimeError:")


# ── plan() → ToolCall gating description ──────────────────────────────────────────────────────────


def test_plan_describes_capability_with_risk():
    tb = Toolbelt(gate=_gate())
    call = tb.plan(KIND_SHELL, command="git push", rationale="ship it")
    assert call.kind == KIND_SHELL
    assert call.command == "git push"
    assert call.risk == REQUIRES_APPROVAL
    assert call.rationale == "ship it"


def test_plan_screenshot_is_safe_and_carries_params():
    tb = Toolbelt()
    call = tb.plan(KIND_SCREENSHOT, cursor=HIGHLIGHT_CURSOR)
    assert call.risk == SAFE
    assert call.params == {"cursor": HIGHLIGHT_CURSOR}


def test_plan_admin_shell_is_requires_approval():
    tb = Toolbelt(gate=_gate())
    call = tb.plan(KIND_SHELL, command="apt update", admin=True)
    assert call.admin is True and call.risk == REQUIRES_APPROVAL
