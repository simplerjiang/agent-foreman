"""Native splash screen — dark sci-fi rotating-rings animation.

Renders at 2× via Pillow with anti-aliasing + gaussian-blur glow, downsampled to 1× LANCZOS.
Runs on a daemon thread; call close_splash() when the main pywebview window is visible.
"""

from __future__ import annotations

import math
import sys
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
BG = (10, 14, 26)  # dark sci-fi background
ACCENT = (42, 111, 219)
GLOW = (91, 148, 240)
VIOLET = (107, 91, 217)
VIOLET_LIGHT = (152, 137, 232)
WHITE = (242, 239, 231)
DIM = (110, 104, 89)
BG_KEY_HEX = "#f0f0f0"

# Layout
W, H = 340, 380
SS = 2
RW, RH = W * SS, H * SS
CX, CY = RW // 2, RH // 2 - 40  # centre of the ring system (shifted up for text below)
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

    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            import tkinter as tk
            from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk
        except ImportError:
            return

        # Make this process DPI-aware so tkinter uses physical pixels (prevents Windows from
        # bitmap-scaling the window, which makes it blurry and oversized).
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
        self._font_title = self._try_font(IF, ["segoeuib.ttf", "Segoe UI Bold", "Segoe UI", "arial.ttf"], 44)
        self._font_sub = self._try_font(IF, ["consola.ttf", "Consolas", "Courier New"], 18)
        self._font_ver = self._try_font(IF, ["consola.ttf", "Consolas", "Courier New"], 14)

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

        # Main frame (dark background with rounded-rect mask)
        frame = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
        # Draw rounded dark rectangle as the splash body
        body = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(body)
        corner = 32
        bdraw.rounded_rectangle((0, 0, RW - 1, RH - 1), radius=corner, fill=_rgba(BG))
        # Subtle border
        bdraw.rounded_rectangle((0, 0, RW - 1, RH - 1), radius=corner, outline=_rgba(ACCENT, 40), width=2)
        frame = Image.alpha_composite(frame, body)

        draw = ImageDraw.Draw(frame)

        # Glow layer (for bloom effects)
        glow = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow)

        # ---- Ambient radial glow behind rings ----
        for i in range(6):
            r = R_OUTER + 30 - i * 8
            a = 12 + i * 3
            gdraw.ellipse((CX - r, CY - r, CX + r, CY + r), fill=_rgba(ACCENT, a))

        # ---- Outer ring (clockwise, 8s) ----
        ang_outer = elapsed * (360 / 8)
        self._draw_dashed_ring(draw, CX, CY, R_OUTER, ang_outer,
                               color=ACCENT, alpha=120, width=3, dash=8, gap=12, segs=90)
        # Outer ring dots
        for offset, sz, col in [(0, 8, GLOW), (90, 6, GLOW), (200, 5, ACCENT)]:
            a = math.radians(ang_outer + offset)
            dx, dy = CX + R_OUTER * math.cos(a), CY + R_OUTER * math.sin(a)
            draw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=_rgba(col))
            gdraw.ellipse((dx - sz * 3, dy - sz * 3, dx + sz * 3, dy + sz * 3), fill=_rgba(col, 35))

        # ---- Middle ring (counter-clockwise, 6s) ----
        ang_mid = -elapsed * (360 / 6)
        self._draw_dashed_ring(draw, CX, CY, R_MID, ang_mid,
                               color=VIOLET, alpha=140, width=3, dash=4, gap=8, segs=72)
        for offset, sz, col in [(0, 6, VIOLET_LIGHT), (140, 5, VIOLET_LIGHT)]:
            a = math.radians(ang_mid + offset)
            dx, dy = CX + R_MID * math.cos(a), CY + R_MID * math.sin(a)
            draw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=_rgba(col))
            gdraw.ellipse((dx - sz * 3, dy - sz * 3, dx + sz * 3, dy + sz * 3), fill=_rgba(col, 30))

        # ---- Inner ring (clockwise, 4s) ----
        ang_inner = elapsed * (360 / 4)
        self._draw_dashed_ring(draw, CX, CY, R_INNER, ang_inner,
                               color=ACCENT, alpha=160, width=4, dash=3, gap=5, segs=54)

        # ---- Centre pulse ----
        pulse = (math.sin(elapsed * math.pi / 1.0) + 1) / 2  # 2s cycle
        cr = int(14 + 6 * pulse)
        ca = int(180 + 75 * pulse)
        draw.ellipse((CX - cr, CY - cr, CX + cr, CY + cr), fill=_rgba(GLOW, ca))
        # Centre glow
        for i in range(4):
            gr = cr + 10 + i * 12
            ga = int(40 * (1 - i * 0.25) * (0.5 + 0.5 * pulse))
            gdraw.ellipse((CX - gr, CY - gr, CX + gr, CY + gr), fill=_rgba(GLOW, ga))
        # Faint outer halo
        draw.ellipse((CX - 28, CY - 28, CX + 28, CY + 28), outline=_rgba(GLOW, 50), width=1)

        # ---- Crosshair lines (subtle) ----
        line_len = R_INNER - 24
        for angle_deg in [0, 90, 180, 270]:
            a = math.radians(angle_deg)
            x1 = CX + 24 * math.cos(a)
            y1 = CY + 24 * math.sin(a)
            x2 = CX + line_len * math.cos(a)
            y2 = CY + line_len * math.sin(a)
            draw.line([(x1, y1), (x2, y2)], fill=_rgba(GLOW, 30), width=1)

        # ---- Composite glow ----
        glow_b = glow.filter(ImageFilter.GaussianBlur(radius=14))
        frame = Image.alpha_composite(frame, glow_b)
        draw = ImageDraw.Draw(frame)

        # ---- Text: "Foreman" ----
        text_y = CY + R_OUTER + 40
        tb = self._font_title.getbbox("Foreman")
        tw = tb[2] - tb[0]
        draw.text((CX - tw // 2, text_y), "Foreman", font=self._font_title, fill=_rgba(WHITE))

        # ---- Breathing dots ----
        dot_y = text_y + 56
        for i in range(3):
            p = (math.sin(elapsed * math.pi / 0.6 - i * 1.05) + 1) / 2
            a = int(60 + 195 * p)
            dx = CX + (i - 1) * 24
            draw.ellipse((dx - 5, dot_y - 5, dx + 5, dot_y + 5), fill=_rgba(GLOW, a))

        # ---- "Initializing…" ----
        sub = "Initializing…"
        sb = self._font_sub.getbbox(sub)
        sw = sb[2] - sb[0]
        draw.text((CX - sw // 2, dot_y + 22), sub, font=self._font_sub, fill=_rgba(DIM))

        # ---- Downsample 2× → 1× ----
        small = frame.resize((W, H), Image.LANCZOS)
        # Alpha→chroma-key: paste onto bg key
        bg = self._Image.new("RGBA", (W, H), (240, 240, 240, 255))
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

    def _draw_dashed_ring(self, draw, cx: float, cy: float, r: float,
                          angle: float, color: tuple[int, int, int], alpha: int,
                          width: int, dash: int, gap: int, segs: int) -> None:
        period = dash + gap
        for i in range(segs):
            if i % period >= dash:
                continue
            a0 = math.radians(angle + i * 360 / segs)
            a1 = math.radians(angle + (i + 1) * 360 / segs)
            x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
            x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
            draw.line([(x0, y0), (x1, y1)], fill=_rgba(color, alpha), width=width)


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
