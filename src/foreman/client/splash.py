"""Native splash screen — sci-fi rotating-rings animation with transparent background.

Renders at 2× via Pillow with anti-aliasing, downsampled to 1× LANCZOS. Uses tkinter with
a chroma-key transparent color so the rings float directly over the desktop. A dark halo is
drawn behind each element to ensure visibility against any wallpaper. Runs on a daemon thread;
call close_splash() when the main pywebview window is visible.
"""

from __future__ import annotations

import math
import sys
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
ACCENT = (42, 111, 219)
GLOW = (91, 148, 240)
VIOLET = (107, 91, 217)
VIOLET_LIGHT = (152, 137, 232)
WHITE = (255, 255, 255)
DIM = (180, 174, 161)
SHADOW = (8, 10, 20)

# Chroma key: bright magenta — never appears in content, punched out by tkinter
BG_KEY = (255, 0, 255)
BG_KEY_HEX = "#ff00ff"

# Layout (1× logical; rendered at 2×)
W, H = 340, 380
SS = 2
RW, RH = W * SS, H * SS
CX, CY = RW // 2, RH // 2 - 40
FPS = 24
FRAME_MS = 1000 // FPS

# Ring radii (render space)
R_OUTER = 140
R_MID = 98
R_INNER = 56


def _rgba(c: tuple[int, int, int], a: int = 255) -> tuple[int, int, int, int]:
    return (*c, a)


class _Splash:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._root = None  # type: ignore[assignment]
        self._alive = True
        self._tick = 0

    def show(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._alive = False
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:
                pass

    def _run(self) -> None:
        try:
            import tkinter as tk
            from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk
        except ImportError:
            return

        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        self._Image = Image
        self._ImageDraw = ImageDraw
        self._ImageFilter = ImageFilter
        self._ImageTk = ImageTk

        root = tk.Tk()
        self._root = root
        root.title("Foreman")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG_KEY_HEX)
        root.attributes("-transparentcolor", BG_KEY_HEX)

        ico = Path(
            getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent.parent.parent)
        ) / "packaging" / "foreman.ico"
        if ico.exists():
            try:
                root.iconbitmap(str(ico))
            except Exception:
                pass

        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

        self._label = tk.Label(root, bg=BG_KEY_HEX, bd=0)
        self._label.pack()

        from PIL import ImageFont as IF
        self._font_title = self._try_font(IF, ["segoeuib.ttf", "Segoe UI Bold", "Segoe UI"], 44)
        self._font_sub = self._try_font(IF, ["consola.ttf", "Consolas", "Courier New"], 18)

        self._tkimg = None
        self._animate()
        root.mainloop()

    @staticmethod
    def _try_font(IF, names: list[str], size: int):
        for n in names:
            try:
                return IF.truetype(n, size)
            except (OSError, IOError):
                continue
        return IF.load_default()

    # ------------------------------------------------------------------
    # Frame render
    # ------------------------------------------------------------------

    def _animate(self) -> None:
        if not self._alive:
            return

        Image = self._Image
        ImageDraw = self._ImageDraw
        ImageFilter = self._ImageFilter

        t = self._tick
        self._tick += 1
        elapsed = t / FPS

        # 1) Shadow layer: draw all elements in dark color, then blur → creates halo
        shadow = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)

        ang_outer = elapsed * (360 / 8)
        ang_mid = -elapsed * (360 / 6)
        ang_inner = elapsed * (360 / 4)

        # Shadow rings (thicker than content for spread)
        self._draw_arc_ring(sdraw, CX, CY, R_OUTER, ang_outer, SHADOW, 10, 40, 20)
        self._draw_arc_ring(sdraw, CX, CY, R_MID, ang_mid, SHADOW, 10, 30, 15)
        self._draw_arc_ring(sdraw, CX, CY, R_INNER, ang_inner, SHADOW, 12, 25, 20)

        # Shadow dots
        for offset, sz in [(0, 14), (120, 12), (240, 11)]:
            a = math.radians(ang_outer + offset)
            dx, dy = CX + R_OUTER * math.cos(a), CY + R_OUTER * math.sin(a)
            sdraw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=_rgba(SHADOW))
        for offset, sz in [(0, 12), (180, 11)]:
            a = math.radians(ang_mid + offset)
            dx, dy = CX + R_MID * math.cos(a), CY + R_MID * math.sin(a)
            sdraw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=_rgba(SHADOW))

        # Shadow centre
        sdraw.ellipse((CX - 34, CY - 34, CX + 34, CY + 34), fill=_rgba(SHADOW))

        # Shadow text
        text_y = CY + R_OUTER + 40
        tb = self._font_title.getbbox("Foreman")
        tw = tb[2] - tb[0]
        sdraw.text((CX - tw // 2, text_y), "Foreman", font=self._font_title, fill=_rgba(SHADOW))
        dot_y = text_y + 56
        for i in range(3):
            dx = CX + (i - 1) * 24
            sdraw.ellipse((dx - 10, dot_y - 10, dx + 10, dot_y + 10), fill=_rgba(SHADOW))
        sub = "Initializing…"
        sb = self._font_sub.getbbox(sub)
        sw = sb[2] - sb[0]
        sdraw.text((CX - sw // 2, dot_y + 22), sub, font=self._font_sub, fill=_rgba(SHADOW))

        # Blur the shadow for a soft halo
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=8))

        # 2) Content layer on top of shadow
        frame = Image.alpha_composite(shadow, Image.new("RGBA", (RW, RH), (0, 0, 0, 0)))
        draw = ImageDraw.Draw(frame)

        # ---- Outer ring (clockwise, 8s) ----
        self._draw_arc_ring(draw, CX, CY, R_OUTER, ang_outer,
                            color=ACCENT, width=5, arc_deg=40, gap_deg=20)
        for offset, sz, col in [(0, 10, GLOW), (120, 8, ACCENT), (240, 7, GLOW)]:
            a = math.radians(ang_outer + offset)
            dx, dy = CX + R_OUTER * math.cos(a), CY + R_OUTER * math.sin(a)
            draw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=col + (255,))

        # ---- Middle ring (counter-clockwise, 6s) ----
        self._draw_arc_ring(draw, CX, CY, R_MID, ang_mid,
                            color=VIOLET, width=5, arc_deg=30, gap_deg=15)
        for offset, sz, col in [(0, 8, VIOLET_LIGHT), (180, 7, VIOLET_LIGHT)]:
            a = math.radians(ang_mid + offset)
            dx, dy = CX + R_MID * math.cos(a), CY + R_MID * math.sin(a)
            draw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=col + (255,))

        # ---- Inner ring (clockwise, 4s) ----
        self._draw_arc_ring(draw, CX, CY, R_INNER, ang_inner,
                            color=ACCENT, width=6, arc_deg=25, gap_deg=20)

        # ---- Centre pulse ----
        pulse = (math.sin(elapsed * math.pi) + 1) / 2
        cr = int(16 + 6 * pulse)
        draw.ellipse((CX - cr, CY - cr, CX + cr, CY + cr), fill=GLOW + (255,))
        draw.ellipse((CX - 30, CY - 30, CX + 30, CY + 30), outline=GLOW + (255,), width=2)

        # ---- Crosshair lines ----
        for angle_deg in [0, 90, 180, 270]:
            a = math.radians(angle_deg)
            x1, y1 = CX + 32 * math.cos(a), CY + 32 * math.sin(a)
            x2, y2 = CX + (R_INNER - 10) * math.cos(a), CY + (R_INNER - 10) * math.sin(a)
            draw.line([(x1, y1), (x2, y2)], fill=GLOW + (255,), width=1)

        # ---- Text ----
        draw.text((CX - tw // 2, text_y), "Foreman", font=self._font_title, fill=WHITE + (255,))

        for i in range(3):
            p = (math.sin(elapsed * math.pi / 0.6 - i * 1.05) + 1) / 2
            a = int(140 + 115 * p)
            dx = CX + (i - 1) * 24
            draw.ellipse((dx - 6, dot_y - 6, dx + 6, dot_y + 6), fill=GLOW + (a,))

        draw.text((CX - sw // 2, dot_y + 22), sub, font=self._font_sub, fill=DIM + (255,))

        # ---- Downsample 2× → 1× ----
        small = frame.resize((W, H), Image.LANCZOS)

        # ---- Composite onto chroma-key background ----
        bg = Image.new("RGB", (W, H), BG_KEY)
        bg.paste(small, (0, 0), small)

        self._tkimg = self._ImageTk.PhotoImage(bg)
        self._label.configure(image=self._tkimg)

        try:
            self._root.after(FRAME_MS, self._animate)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_arc_ring(draw, cx: float, cy: float, r: float, angle: float,
                       color: tuple[int, int, int], width: int,
                       arc_deg: float, gap_deg: float) -> None:
        step = arc_deg + gap_deg
        steps_per_arc = max(2, int(arc_deg / 2))
        n_arcs = int(360 / step)
        fill = color + (255,)
        for arc_i in range(n_arcs):
            start = angle + arc_i * step
            for j in range(steps_per_arc):
                a0 = math.radians(start + j * arc_deg / steps_per_arc)
                a1 = math.radians(start + (j + 1) * arc_deg / steps_per_arc)
                x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
                x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
                draw.line([(x0, y0), (x1, y1)], fill=fill, width=width)


# ---------------------------------------------------------------------------
_instance: _Splash | None = None


def show_splash() -> None:
    global _instance
    _instance = _Splash()
    _instance.show()


def close_splash() -> None:
    global _instance
    if _instance is not None:
        _instance.close()
        _instance = None
