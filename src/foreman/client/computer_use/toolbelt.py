"""Operator Toolbelt — the Operator's "hands": shell / screenshot / mouse / keyboard (DESIGN §4.7).

These are the capabilities the Operator (§6.1) uses to actually *do* things in the user's
interactive desktop session. The PC app is a normal session app (not a Windows service), so there
is no Session-0 limit — the window and computer-use are directly available (DESIGN §3.1).

The cardinal rule of §4.7: **capabilities are given in full, but every "move" still flows through
the safety chain**. This module is the *executor* — it does not decide autonomy on its own. It does
two deterministic things that belong at the capability layer:

  1. **Classifies** each capability into the Gate's risk vocabulary (safe | needs-strategy |
     requires-approval) so the decision loop (Auditor → Gate → card) can gate it (§6.6). Admin /
     privileged shell (Windows UAC `runas`, Linux `sudo`) is **always** requires-approval — once run
     it usually can't be undone (§4.7).
  2. **Fails closed**: a capability classified requires-approval will NOT execute unless an explicit
     ``approved=True`` is passed in. So even a buggy or compromised caller cannot make the Toolbelt
     run a privileged command without an approval having flowed through the Gate (§6.7 ① "绝不让
     LLM 当唯一闸门").

**Screenshot cursor render options** (the explicitly-requested feature, §4.7): a capture can hide,
show, or highlight/magnify the cursor. Default policy: the Operator hides the cursor when "looking"
at the screen for itself (less occlusion); a capture meant *for you* (a decision card / demo)
highlights the cursor so you can see exactly where it is about to click.

Backends are **injected** (duck-typed) so the whole module is unit-testable with no real GUI: pass
fakes that record calls. The default real backends lazy-import the heavy libs (``mss``/``Pillow``
for capture, ``pynput``/``pyautogui`` for mouse/keyboard) only when first used, so importing this
module never requires those packages. Live desktop hookup (real capture/click on a running PC) is
exercised only when those libs are present — the gating/classification/fail-closed logic here is
fully covered by the injected-backend tests.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any

# ── cursor render options for screenshots (DESIGN §4.7) ──────────────────────────────────────────
HIDE_CURSOR = "hide"  # clean frame / pointer was occluding content
SHOW_CURSOR = "show"  # leave the pointer as-is
HIGHLIGHT_CURSOR = "highlight"  # draw a ring / magnifier at the pointer — "it's about to click HERE"
CURSOR_MODES: frozenset[str] = frozenset({HIDE_CURSOR, SHOW_CURSOR, HIGHLIGHT_CURSOR})

# Risk levels — same vocabulary as the Gate (DESIGN §6.6).
SAFE = "safe"
NEEDS_STRATEGY = "needs-strategy"
REQUIRES_APPROVAL = "requires-approval"

# Capability kinds (each maps to an ``actions`` row, kind="mcp_tool", DESIGN §7.1).
KIND_SCREENSHOT = "screenshot"
KIND_MOUSE_MOVE = "mouse_move"
KIND_MOUSE_CLICK = "mouse_click"
KIND_MOUSE_DRAG = "mouse_drag"
KIND_KEYBOARD_TYPE = "keyboard_type"
KIND_KEYBOARD_HOTKEY = "keyboard_hotkey"
KIND_SHELL = "shell"


@dataclass
class ToolCall:
    """An intended capability use, classified but not yet run — for Auditor / Gate / decision card.

    Maps to an ``actions`` row (DESIGN §7.1, kind="mcp_tool"). The decision loop (T4.6) audits + gates
    this, then calls the matching Toolbelt execute method with ``approved=`` set from the outcome."""

    kind: str
    command: str = ""
    admin: bool = False
    risk: str = SAFE
    rationale: str = ""
    params: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """The outcome of one capability invocation — JSON-friendly for events / decision cards."""

    ok: bool
    kind: str
    risk: str = SAFE
    detail: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""
    image: bytes | None = field(default=None, repr=False)  # raw PNG (screenshots); off JSON surface


def capability_risk(
    kind: str,
    *,
    admin: bool = False,
    command: str = "",
    gate: Any = None,
) -> str:
    """Map a capability to the Gate's risk vocabulary (DESIGN §4.7 risk table / §6.6).

    - **screenshot / mouse_move** → ``safe`` (read-only; moving the pointer changes nothing).
    - **mouse_click / mouse_drag / keyboard_***  → ``needs-strategy``: a GUI action could hit
      something irreversible ("delete / send / pay"), but the deterministic layer cannot tell which
      — so it is gray (audited + carded per the dial), never silently auto at the toolbelt itself.
    - **shell** → admin/privileged is **always** ``requires-approval`` (§4.7); otherwise defer the
      command *text* to the Gate's own classifier (``rm -rf`` / `git push` / drop table … get caught
      there). Without a Gate, a non-admin shell command is conservatively ``needs-strategy``.
    """
    if kind == KIND_SHELL:
        if admin:
            return REQUIRES_APPROVAL  # UAC / sudo — usually un-undoable, always ask first
        if gate is not None and hasattr(gate, "classify"):
            return gate.classify(command)
        return NEEDS_STRATEGY
    if kind in (KIND_SCREENSHOT, KIND_MOUSE_MOVE):
        return SAFE
    if kind in (
        KIND_MOUSE_CLICK,
        KIND_MOUSE_DRAG,
        KIND_KEYBOARD_TYPE,
        KIND_KEYBOARD_HOTKEY,
    ):
        return NEEDS_STRATEGY
    # Unknown capability → fail closed (DESIGN §6.7 从严默认).
    return REQUIRES_APPROVAL


class Toolbelt:
    """The Operator's hands. Backends are injected (duck-typed) for testability; defaults are lazy.

    Backend protocols (any object exposing these methods works):
      - ``screen.capture(*, region, cursor_mode) -> bytes`` — PNG bytes of the (optionally cropped)
        screen with the cursor hidden / shown / highlighted.
      - ``mouse.move(x, y)`` / ``mouse.click(x, y, button)`` / ``mouse.drag(x1, y1, x2, y2, button)``
      - ``keyboard.type(text)`` / ``keyboard.hotkey(*keys)``
      - ``shell.run(command, *, admin) -> dict`` with keys ``returncode``/``stdout``/``stderr``.
    """

    def __init__(
        self,
        *,
        screen: Any = None,
        mouse: Any = None,
        keyboard: Any = None,
        shell: Any = None,
        gate: Any = None,
        self_cursor: str = HIDE_CURSOR,
    ) -> None:
        self._screen = screen
        self._mouse = mouse
        self._keyboard = keyboard
        self._shell = shell
        self.gate = gate
        # Default cursor mode when the Operator captures for *itself* (DESIGN §4.7): hidden.
        self.self_cursor = self_cursor if self_cursor in CURSOR_MODES else HIDE_CURSOR

    # ── capability → ToolCall (for the decision loop to audit / gate / card) ─────────────────────
    def plan(
        self,
        kind: str,
        *,
        admin: bool = False,
        command: str = "",
        rationale: str = "",
        **params,
    ) -> ToolCall:
        """Describe an intended capability use without running it — for Auditor / Gate / card.

        The decision loop (T4.6) calls this to get a gating-ready description, audits + gates it,
        and only then calls the matching execute method with ``approved=`` set accordingly."""
        risk = capability_risk(kind, admin=admin, command=command, gate=self.gate)
        return ToolCall(
            kind=kind,
            command=command,
            admin=admin,
            risk=risk,
            rationale=rationale,
            params=dict(params),
        )

    # ── screenshot (read-only; cursor render options, §4.7) ──────────────────────────────────────
    def screenshot(
        self,
        *,
        cursor: str | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> ToolResult:
        """Capture the screen. ``cursor`` ∈ {hide, show, highlight}; defaults to ``self_cursor``.

        Read-only and ``safe``: never gated. Multimodal frames cost tokens downstream, so capture on
        demand — don't burst (DESIGN §13.6)."""
        mode = cursor if cursor in CURSOR_MODES else self.self_cursor
        if self._screen is None:
            self._screen = _default_screen()
        try:
            png = self._screen.capture(region=region, cursor_mode=mode)
        except Exception as exc:  # backend / display failure — surface, don't crash the loop
            return ToolResult(False, KIND_SCREENSHOT, SAFE, error=_emsg(exc))
        return ToolResult(
            True,
            KIND_SCREENSHOT,
            SAFE,
            detail=f"captured ({mode} cursor)",
            data={"cursor": mode, "region": region, "bytes": len(png) if png else 0},
            image=png,  # raw PNG kept off the JSON-friendly fields; callers that need pixels read .image
        )

    # ── mouse ────────────────────────────────────────────────────────────────────────────────────
    def move_mouse(self, x: int, y: int) -> ToolResult:
        """Move the pointer. ``safe`` — moving alone changes nothing."""
        return self._run_backend(
            KIND_MOUSE_MOVE, SAFE, lambda: self._mouse_be().move(x, y),
            detail=f"move ({x},{y})", data={"x": x, "y": y},
        )

    def click(
        self, x: int | None = None, y: int | None = None, *, button: str = "left",
        approved: bool = False,
    ) -> ToolResult:
        """Click (optionally at x,y). ``needs-strategy`` — a click can hit an irreversible control."""
        return self._gui_action(
            KIND_MOUSE_CLICK, lambda: self._mouse_be().click(x, y, button),
            approved=approved, detail=f"click {button} ({x},{y})",
            data={"x": x, "y": y, "button": button},
        )

    def drag(
        self, x1: int, y1: int, x2: int, y2: int, *, button: str = "left", approved: bool = False,
    ) -> ToolResult:
        """Drag from (x1,y1) to (x2,y2). ``needs-strategy``."""
        return self._gui_action(
            KIND_MOUSE_DRAG, lambda: self._mouse_be().drag(x1, y1, x2, y2, button),
            approved=approved, detail=f"drag ({x1},{y1})→({x2},{y2})",
            data={"x1": x1, "y1": y1, "x2": x2, "y2": y2, "button": button},
        )

    # ── keyboard ─────────────────────────────────────────────────────────────────────────────────
    def type_text(self, text: str, *, approved: bool = False) -> ToolResult:
        """Type text. ``needs-strategy`` (could fill a destructive form field)."""
        return self._gui_action(
            KIND_KEYBOARD_TYPE, lambda: self._keyboard_be().type(text),
            approved=approved, detail=f"type ({len(text)} chars)", data={"length": len(text)},
        )

    def hotkey(self, *keys: str, approved: bool = False) -> ToolResult:
        """Press a key combination (e.g. ctrl+s). ``needs-strategy``."""
        return self._gui_action(
            KIND_KEYBOARD_HOTKEY, lambda: self._keyboard_be().hotkey(*keys),
            approved=approved, detail="+".join(keys), data={"keys": list(keys)},
        )

    # ── shell (incl. admin / privileged) ─────────────────────────────────────────────────────────
    def run_shell(self, command: str, *, admin: bool = False, approved: bool = False) -> ToolResult:
        """Run a shell command. Admin/privileged → ``requires-approval`` (always asked, §4.7).

        The command *text* is also classified by the Gate (``rm -rf`` / `git push` … → caught there).
        A ``requires-approval`` outcome fails closed without ``approved=True``."""
        risk = capability_risk(KIND_SHELL, admin=admin, command=command, gate=self.gate)
        if risk == REQUIRES_APPROVAL and not approved:
            return ToolResult(
                False, KIND_SHELL, risk, error="approval_required",
                data={"command": command, "admin": admin},
            )
        if self._shell is None:
            self._shell = _default_shell()
        try:
            res = self._shell.run(command, admin=admin)
        except Exception as exc:
            return ToolResult(False, KIND_SHELL, risk, error=_emsg(exc), data={"command": command})
        rc = int(res.get("returncode", 0)) if isinstance(res, dict) else 0
        return ToolResult(
            rc == 0,
            KIND_SHELL,
            risk,
            detail=f"exit {rc}",
            data={
                "command": command,
                "admin": admin,
                "returncode": rc,
                "stdout": (res.get("stdout", "") if isinstance(res, dict) else ""),
                "stderr": (res.get("stderr", "") if isinstance(res, dict) else ""),
            },
        )

    # ── internals ────────────────────────────────────────────────────────────────────────────────
    def _gui_action(self, kind, fn, *, approved, detail, data) -> ToolResult:
        """Shared execute path for click/drag/type/hotkey: classify, fail-closed, run backend."""
        risk = capability_risk(kind, gate=self.gate)
        if risk == REQUIRES_APPROVAL and not approved:
            return ToolResult(False, kind, risk, error="approval_required", data=data)
        return self._run_backend(kind, risk, fn, detail=detail, data=data)

    def _run_backend(self, kind, risk, fn, *, detail, data) -> ToolResult:
        try:
            fn()
        except Exception as exc:
            return ToolResult(False, kind, risk, error=_emsg(exc), data=data)
        return ToolResult(True, kind, risk, detail=detail, data=data)

    def _mouse_be(self):
        if self._mouse is None:
            self._mouse = _default_mouse()
        return self._mouse

    def _keyboard_be(self):
        if self._keyboard is None:
            self._keyboard = _default_keyboard()
        return self._keyboard


def _emsg(exc: Exception) -> str:
    """Type + short message — never the full repr (an exc could carry a path / secret)."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


# ── default real backends (lazy-imported; only used on a live desktop) ───────────────────────────
def _default_screen():
    return _MssScreen()


def _default_mouse():
    return _PyAutoGuiMouse()


def _default_keyboard():
    return _PyAutoGuiKeyboard()


def _default_shell():
    return _SubprocessShell()


class _MssScreen:
    """Real screen capture via ``mss`` + ``Pillow`` with cursor render options. Lazy imports."""

    def capture(self, *, region=None, cursor_mode=HIDE_CURSOR) -> bytes:
        import io

        import mss  # type: ignore
        from PIL import Image  # type: ignore

        with mss.mss() as sct:
            mon = sct.monitors[0] if region is None else {
                "left": region[0], "top": region[1], "width": region[2], "height": region[3],
            }
            shot = sct.grab(mon)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        # mss never captures the OS cursor → HIDE is the natural state. For SHOW/HIGHLIGHT we draw it.
        if cursor_mode in (SHOW_CURSOR, HIGHLIGHT_CURSOR):
            pos = _cursor_pos()
            if pos is not None:
                origin = (0, 0) if region is None else (region[0], region[1])
                _draw_cursor(img, (pos[0] - origin[0], pos[1] - origin[1]), cursor_mode)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def _cursor_pos():
    try:
        import pyautogui  # type: ignore

        p = pyautogui.position()
        return int(p[0]), int(p[1])
    except Exception:
        return None


def _draw_cursor(img, pos, mode) -> None:
    """Draw a highlight ring (HIGHLIGHT) or a small dot (SHOW) at ``pos`` on a Pillow image."""
    from PIL import ImageDraw  # type: ignore

    x, y = pos
    draw = ImageDraw.Draw(img, "RGBA")
    if mode == HIGHLIGHT_CURSOR:
        r = 24
        draw.ellipse([x - r, y - r, x + r, y + r], outline=(255, 196, 0, 255), width=4)
        draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=(255, 0, 0, 255))
    else:  # SHOW — modest dot so the pointer is visible without obscuring content
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(0, 120, 255, 255))


class _PyAutoGuiMouse:
    def _be(self):
        import pyautogui  # type: ignore

        return pyautogui

    def move(self, x, y):
        self._be().moveTo(x, y)

    def click(self, x, y, button):
        self._be().click(x=x, y=y, button=button)

    def drag(self, x1, y1, x2, y2, button):
        be = self._be()
        be.moveTo(x1, y1)
        be.dragTo(x2, y2, button=button)


class _PyAutoGuiKeyboard:
    def _be(self):
        import pyautogui  # type: ignore

        return pyautogui

    def type(self, text):
        self._be().typewrite(text)

    def hotkey(self, *keys):
        self._be().hotkey(*keys)


class _SubprocessShell:
    """Run a command (argv list, no shell) and capture output. Admin elevation is platform-specific.

    Privileged execution (Windows UAC ``runas`` / Linux ``sudo``) requires interactive consent and is
    NOT auto-elevated here — it is only ever reached after a requires-approval card is approved. We
    raise so the caller surfaces "admin elevation not wired" rather than silently running unprivileged.
    """

    def run(self, command: str, *, admin: bool = False) -> dict:
        if admin:
            raise NotImplementedError("admin/privileged elevation is deferred (UAC/sudo, §4.7)")
        # argv list — never shell=True — so the command string can't be re-parsed for injection.
        import shlex

        argv = shlex.split(command, posix=False)
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            argv, capture_output=True, text=True, timeout=300
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }


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
