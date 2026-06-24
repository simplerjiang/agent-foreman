"""Native splash screen — sci-fi rotating-rings animation with a *truly* transparent background.

The window is a Win32 **layered window** updated via ``UpdateLayeredWindow`` with per-pixel alpha
(``ULW_ALPHA``). Each frame is rendered at 2× by Pillow (anti-aliased, soft drop-shadow for
legibility), downsampled LANCZOS to 1×, premultiplied, and blitted as a 32-bit top-down BGRA DIB.

Unlike a tkinter ``-transparentcolor`` chroma key — which is *binary* (a pixel is either exactly the
key colour or fully opaque, so every anti-aliased / blurred edge becomes an opaque fringe) — a
layered window honours the alpha of every pixel. Soft glows, faded shadows and anti-aliased text
composite correctly over any wallpaper with no colour fringing.

Runs on a daemon thread; call :func:`close_splash` when the main pywebview window is visible.
"""

from __future__ import annotations

import math
import sys
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Palette  (straight RGB; alpha is carried per-pixel)
# ---------------------------------------------------------------------------
ACCENT = (42, 111, 219)
GLOW = (91, 148, 240)
VIOLET = (107, 91, 217)
VIOLET_LIGHT = (152, 137, 232)
WHITE = (255, 255, 255)
DIM = (188, 198, 222)
SHADOW = (4, 8, 18)

# Layout (1× logical; rendered at 2× then downsampled)
W, H = 340, 380
SS = 2
RW, RH = W * SS, H * SS
CX, CY = RW // 2, RH // 2 - 40
FPS = 24
FRAME_MS = 1000 // FPS

# Ring radii (render / 2× space)
R_OUTER = 140
R_MID = 98
R_INNER = 56


def _rgba(c: tuple[int, int, int], a: int = 255) -> tuple[int, int, int, int]:
    return (c[0], c[1], c[2], a)


# ===========================================================================
# Frame rendering (pure Pillow — no Win32; unit-testable on any platform)
# ===========================================================================
class _Renderer:
    """Draws one animation frame to an (W, H) RGBA image. Stateless except for fonts."""

    def __init__(self) -> None:
        from PIL import ImageFont as IF

        self._font_title = self._try_font(IF, ["segoeuib.ttf", "Segoe UI Bold", "Segoe UI"], 44)
        self._font_sub = self._try_font(IF, ["consola.ttf", "Consolas", "Courier New"], 18)

    @staticmethod
    def _try_font(IF, names: list[str], size: int):
        for n in names:
            try:
                return IF.truetype(n, size)
            except (OSError, IOError):
                continue
        return IF.load_default()

    def render(self, tick: int):
        """Return an (W, H) RGBA ``PIL.Image`` for the given frame counter."""
        from PIL import Image, ImageDraw, ImageFilter

        elapsed = tick / FPS

        ang_outer = elapsed * (360 / 8)
        ang_mid = -elapsed * (360 / 6)
        ang_inner = elapsed * (360 / 4)

        text_y = CY + R_OUTER + 40
        tb = self._font_title.getbbox("Foreman")
        tw = tb[2] - tb[0]
        dot_y = text_y + 56
        sub = "Initializing…"
        sb = self._font_sub.getbbox(sub)
        subw = sb[2] - sb[0]

        # --- 1) Soft drop-shadow layer: same shapes in translucent dark, then blur. With real
        #        per-pixel alpha this is a faint halo that lifts the rings off light wallpapers
        #        (NOT the opaque chroma blob the old color-key build produced). --------------------
        shadow = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow)
        sa = 110  # shadow opacity before blur

        self._arc_ring(sdraw, CX, CY, R_OUTER, ang_outer, SHADOW, 9, 40, 20, sa)
        self._arc_ring(sdraw, CX, CY, R_MID, ang_mid, SHADOW, 9, 30, 15, sa)
        self._arc_ring(sdraw, CX, CY, R_INNER, ang_inner, SHADOW, 10, 25, 20, sa)
        for offset, sz in [(0, 12), (120, 10), (240, 9)]:
            a = math.radians(ang_outer + offset)
            dx, dy = CX + R_OUTER * math.cos(a), CY + R_OUTER * math.sin(a)
            sdraw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=_rgba(SHADOW, sa))
        sdraw.ellipse((CX - 30, CY - 30, CX + 30, CY + 30), fill=_rgba(SHADOW, sa))
        sdraw.text((CX - tw // 2, text_y), "Foreman", font=self._font_title, fill=_rgba(SHADOW, 150))
        sdraw.text((CX - subw // 2, dot_y + 22), sub, font=self._font_sub, fill=_rgba(SHADOW, 130))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=7))

        # --- 2) Content layer over the shadow -------------------------------------------------
        frame = shadow
        draw = ImageDraw.Draw(frame)

        # Outer ring (clockwise, 8s) + travelling dots
        self._arc_ring(draw, CX, CY, R_OUTER, ang_outer, ACCENT, 5, 40, 20, 255)
        for offset, sz, col in [(0, 10, GLOW), (120, 8, ACCENT), (240, 7, GLOW)]:
            a = math.radians(ang_outer + offset)
            dx, dy = CX + R_OUTER * math.cos(a), CY + R_OUTER * math.sin(a)
            draw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=_rgba(col))

        # Middle ring (counter-clockwise, 6s)
        self._arc_ring(draw, CX, CY, R_MID, ang_mid, VIOLET, 5, 30, 15, 255)
        for offset, sz, col in [(0, 8, VIOLET_LIGHT), (180, 7, VIOLET_LIGHT)]:
            a = math.radians(ang_mid + offset)
            dx, dy = CX + R_MID * math.cos(a), CY + R_MID * math.sin(a)
            draw.ellipse((dx - sz, dy - sz, dx + sz, dy + sz), fill=_rgba(col))

        # Inner ring (clockwise, 4s)
        self._arc_ring(draw, CX, CY, R_INNER, ang_inner, ACCENT, 6, 25, 20, 255)

        # Centre pulse + halo ring
        pulse = (math.sin(elapsed * math.pi) + 1) / 2
        cr = int(16 + 6 * pulse)
        draw.ellipse((CX - cr, CY - cr, CX + cr, CY + cr), fill=_rgba(GLOW))
        draw.ellipse((CX - 30, CY - 30, CX + 30, CY + 30), outline=_rgba(GLOW), width=2)

        # Crosshair spokes
        for angle_deg in (0, 90, 180, 270):
            a = math.radians(angle_deg)
            x1, y1 = CX + 32 * math.cos(a), CY + 32 * math.sin(a)
            x2, y2 = CX + (R_INNER - 10) * math.cos(a), CY + (R_INNER - 10) * math.sin(a)
            draw.line([(x1, y1), (x2, y2)], fill=_rgba(GLOW), width=1)

        # Title — white fill with a dark stroke so it reads on light *and* dark wallpapers.
        draw.text((CX - tw // 2, text_y), "Foreman", font=self._font_title, fill=_rgba(WHITE),
                  stroke_width=3, stroke_fill=_rgba((6, 10, 22), 235))

        # Loading dots (pulsing)
        for i in range(3):
            p = (math.sin(elapsed * math.pi / 0.6 - i * 1.05) + 1) / 2
            a = int(140 + 115 * p)
            dx = CX + (i - 1) * 24
            draw.ellipse((dx - 6, dot_y - 6, dx + 6, dot_y + 6), fill=_rgba(GLOW, a))

        # Subtitle — likewise outlined for contrast on bright backgrounds.
        draw.text((CX - subw // 2, dot_y + 22), sub, font=self._font_sub, fill=_rgba(DIM),
                  stroke_width=2, stroke_fill=_rgba((6, 10, 22), 210))

        # Downsample 2× → 1×
        resampling = getattr(Image, "Resampling", Image)
        return frame.resize((W, H), getattr(resampling, "LANCZOS", 1))

    @staticmethod
    def _arc_ring(draw, cx: float, cy: float, r: float, angle: float,
                  color: tuple[int, int, int], width: int,
                  arc_deg: float, gap_deg: float, alpha: int) -> None:
        step = arc_deg + gap_deg
        steps_per_arc = max(2, int(arc_deg / 2))
        n_arcs = int(360 / step)
        fill = _rgba(color, alpha)
        for arc_i in range(n_arcs):
            start = angle + arc_i * step
            for j in range(steps_per_arc):
                a0 = math.radians(start + j * arc_deg / steps_per_arc)
                a1 = math.radians(start + (j + 1) * arc_deg / steps_per_arc)
                x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
                x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
                draw.line([(x0, y0), (x1, y1)], fill=fill, width=width)


def premultiplied_bgra(img) -> bytes:
    """Convert an (W, H) RGBA image to premultiplied **BGRA** bytes (memory order B,G,R,A).

    ``UpdateLayeredWindow`` with ``AC_SRC_ALPHA`` requires premultiplied alpha; a 32-bit DIB stores
    each pixel as the little-endian DWORD 0xAARRGGBB, i.e. bytes B,G,R,A. ``ImageChops.multiply``
    computes ``(c * a) / 255`` which is exactly the premultiply, with no numpy dependency.
    """
    from PIL import Image, ImageChops

    if img.mode != "RGBA":
        img = img.convert("RGBA")
    r, g, b, a = img.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    # Merge in B,G,R,A *slot* order so raw bytes land as B,G,R,A (the DIB's memory layout).
    bgra = Image.merge("RGBA", (b, g, r, a))
    return bgra.tobytes("raw", "RGBA")


# ===========================================================================
# Win32 layered-window plumbing
# ===========================================================================
def _build_win32():
    """Build the ctypes interface lazily (Windows only). Returns a namespace object or None."""
    if sys.platform != "win32":
        return None
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    HANDLE = ctypes.c_void_p
    LRESULT = ctypes.c_ssize_t
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t  # LONG_PTR — c_ssize_t avoids the 32-bit-truncation crash on Win64
    UINT = ctypes.c_uint
    DWORD = ctypes.c_uint32
    INT = ctypes.c_int
    LONG = ctypes.c_int32
    WORD = ctypes.c_uint16

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HANDLE, UINT, WPARAM, LPARAM)

    class WNDCLASSEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", UINT),
            ("style", UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", INT),
            ("cbWndExtra", INT),
            ("hInstance", HANDLE),
            ("hIcon", HANDLE),
            ("hCursor", HANDLE),
            ("hbrBackground", HANDLE),
            ("lpszMenuName", ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
            ("hIconSm", HANDLE),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", LONG), ("y", LONG)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", LONG), ("cy", LONG)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", ctypes.c_byte),
            ("BlendFlags", ctypes.c_byte),
            ("SourceConstantAlpha", ctypes.c_byte),
            ("AlphaFormat", ctypes.c_byte),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", DWORD), ("biWidth", LONG), ("biHeight", LONG),
            ("biPlanes", WORD), ("biBitCount", WORD), ("biCompression", DWORD),
            ("biSizeImage", DWORD), ("biXPelsPerMeter", LONG), ("biYPelsPerMeter", LONG),
            ("biClrUsed", DWORD), ("biClrImportant", DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", HANDLE), ("message", UINT), ("wParam", WPARAM),
            ("lParam", LPARAM), ("time", DWORD), ("pt", POINT),
        ]

    # restype/argtypes are mandatory on Win64: the default c_int restype truncates returned
    # handles to 32 bits and corrupts them.
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [HANDLE, UINT, WPARAM, LPARAM]
    user32.RegisterClassExW.restype = ctypes.c_ushort
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
    user32.CreateWindowExW.restype = HANDLE
    user32.CreateWindowExW.argtypes = [
        DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD,
        INT, INT, INT, INT, HANDLE, HANDLE, HANDLE, ctypes.c_void_p,
    ]
    user32.ShowWindow.argtypes = [HANDLE, INT]
    user32.DestroyWindow.argtypes = [HANDLE]
    user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, HANDLE]
    user32.GetDC.restype = HANDLE
    user32.GetDC.argtypes = [HANDLE]
    user32.ReleaseDC.argtypes = [HANDLE, HANDLE]
    user32.GetSystemMetrics.restype = INT
    user32.GetSystemMetrics.argtypes = [INT]
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(MSG), HANDLE, UINT, UINT, UINT,
    ]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.UpdateLayeredWindow.restype = ctypes.c_int
    user32.UpdateLayeredWindow.argtypes = [
        HANDLE, HANDLE, ctypes.POINTER(POINT), ctypes.POINTER(SIZE), HANDLE,
        ctypes.POINTER(POINT), DWORD, ctypes.POINTER(BLENDFUNCTION), DWORD,
    ]
    user32.SetWindowPos.argtypes = [HANDLE, HANDLE, INT, INT, INT, INT, UINT]

    kernel32.GetModuleHandleW.restype = HANDLE
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

    gdi32.CreateCompatibleDC.restype = HANDLE
    gdi32.CreateCompatibleDC.argtypes = [HANDLE]
    gdi32.CreateDIBSection.restype = HANDLE
    gdi32.CreateDIBSection.argtypes = [
        HANDLE, ctypes.POINTER(BITMAPINFO), UINT,
        ctypes.POINTER(ctypes.c_void_p), HANDLE, DWORD,
    ]
    gdi32.SelectObject.restype = HANDLE
    gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
    gdi32.DeleteObject.argtypes = [HANDLE]
    gdi32.DeleteDC.argtypes = [HANDLE]

    ns = type("Win32", (), {})()
    ns.ctypes = ctypes
    ns.user32, ns.gdi32, ns.kernel32 = user32, gdi32, kernel32
    ns.WNDPROC = WNDPROC
    ns.WNDCLASSEX, ns.POINT, ns.SIZE = WNDCLASSEX, POINT, SIZE
    ns.BLENDFUNCTION, ns.BITMAPINFO, ns.MSG = BLENDFUNCTION, BITMAPINFO, MSG
    return ns


_class_seq = 0


class _Splash:
    def __init__(self) -> None:
        global _class_seq
        self._thread: threading.Thread | None = None
        self._alive = True
        # Unique class name per instance: a stale class from a not-yet-unregistered prior splash
        # would otherwise make RegisterClassExW fail and the new splash bail.
        _class_seq += 1
        self._class_name = f"ForemanSplashWindow_{_class_seq}"

    def show(self) -> None:
        self._thread = threading.Thread(target=self._run, name="foreman-splash", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._alive = False  # the render loop polls this and tears the window down on its own thread

    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            from PIL import Image  # noqa: F401  (ensure Pillow present before Win32 setup)
        except ImportError:
            return
        win = _build_win32()
        if win is None:
            return

        import ctypes
        import time

        windll = getattr(ctypes, "windll", None)
        if windll is not None:
            try:
                windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                try:
                    windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

        u, g, k = win.user32, win.gdi32, win.kernel32
        try:
            renderer = _Renderer()
        except Exception:
            return

        # --- Register window class -----------------------------------------------------------
        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == 0x0002:  # WM_DESTROY
                u.PostQuitMessage(0)
                return 0
            return u.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = win.WNDPROC(_wndproc)  # keep alive: GC would dangle the callback
        hinst = k.GetModuleHandleW(None)
        wc = win.WNDCLASSEX()
        wc.cbSize = ctypes.sizeof(win.WNDCLASSEX)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.cbClsExtra = wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = wc.hCursor = wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name
        wc.hIconSm = None
        if not u.RegisterClassExW(ctypes.byref(wc)):
            return  # class name collision (a prior splash already registered it) — bail quietly

        # --- Create the layered, top-most, no-activate popup --------------------------------
        WS_POPUP = 0x80000000
        WS_EX_LAYERED = 0x00080000
        WS_EX_TOPMOST = 0x00000008
        WS_EX_TOOLWINDOW = 0x00000080  # keep out of taskbar / alt-tab
        WS_EX_NOACTIVATE = 0x08000000
        ex = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE

        sw = u.GetSystemMetrics(0)  # SM_CXSCREEN
        sh = u.GetSystemMetrics(1)  # SM_CYSCREEN
        x = (sw - W) // 2
        y = (sh - H) // 2

        hwnd = u.CreateWindowExW(
            ex, self._class_name, "Foreman", WS_POPUP,
            x, y, W, H, None, None, hinst, None,
        )
        if not hwnd:
            u.UnregisterClassW(self._class_name, hinst)
            return

        # --- Build a reusable 32-bit top-down BGRA DIB --------------------------------------
        screen_dc = u.GetDC(None)
        mem_dc = g.CreateCompatibleDC(screen_dc)
        bmi = win.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(bmi.bmiHeader)
        bmi.bmiHeader.biWidth = W
        bmi.bmiHeader.biHeight = -H  # negative ⇒ top-down, matching Pillow's row order
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        dib = g.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = g.SelectObject(mem_dc, dib)

        blend = win.BLENDFUNCTION()
        blend.BlendOp = 0          # AC_SRC_OVER
        blend.BlendFlags = 0
        blend.SourceConstantAlpha = 255
        blend.AlphaFormat = 1      # AC_SRC_ALPHA

        ptDst = win.POINT(x, y)
        size = win.SIZE(W, H)
        ptSrc = win.POINT(0, 0)
        ULW_ALPHA = 0x00000002
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        HWND_TOPMOST = win.ctypes.c_void_p(-1)

        nbytes = W * H * 4
        msg = win.MSG()
        SW_SHOWNOACTIVATE = 4
        shown = False

        def _blit(tick: int) -> None:
            raw = premultiplied_bgra(renderer.render(tick))
            ctypes.memmove(bits, raw, min(len(raw), nbytes))
            u.UpdateLayeredWindow(
                hwnd, screen_dc, ctypes.byref(ptDst), ctypes.byref(size),
                mem_dc, ctypes.byref(ptSrc), 0, ctypes.byref(blend), ULW_ALPHA,
            )

        try:
            tick = 0
            next_frame = time.perf_counter()
            while self._alive:
                _blit(tick)
                if not shown:
                    u.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                    u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                    shown = True
                tick += 1

                # Pump pending messages so the window stays responsive / can be destroyed.
                while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):  # PM_REMOVE
                    if msg.message == 0x0012:  # WM_QUIT
                        self._alive = False
                        break
                    u.TranslateMessage(ctypes.byref(msg))
                    u.DispatchMessageW(ctypes.byref(msg))

                next_frame += FRAME_MS / 1000.0
                delay = next_frame - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_frame = time.perf_counter()  # we fell behind; resync
        finally:
            try:
                g.SelectObject(mem_dc, old)
                g.DeleteObject(dib)
                g.DeleteDC(mem_dc)
                u.ReleaseDC(None, screen_dc)
                u.DestroyWindow(hwnd)
                u.UnregisterClassW(self._class_name, hinst)
            except Exception:
                pass


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


# ---------------------------------------------------------------------------
# Standalone harness:  python -m foreman.client.splash [seconds]
#   * dumps sample frames composited over several backgrounds (visual sanity check)
#   * runs the real layered window for N seconds and grabs the screen region
# ---------------------------------------------------------------------------
def _demo(seconds: float = 6.0) -> None:
    from PIL import Image

    out = Path(__file__).resolve().parent / "_splash_preview"
    out.mkdir(exist_ok=True)
    r = _Renderer()

    backgrounds = {
        "dark": (18, 20, 28, 255),
        "light": (235, 238, 245, 255),
        "green": (40, 120, 70, 255),
        "checker": None,
    }
    for tick in (0, 8, 16):
        frame = r.render(tick)
        for name, col in backgrounds.items():
            if col is None:
                bg = Image.new("RGBA", (W, H), (255, 255, 255, 255))
                d = __import__("PIL.ImageDraw", fromlist=["ImageDraw"]).Draw(bg)
                for yy in range(0, H, 20):
                    for xx in range(0, W, 20):
                        if (xx // 20 + yy // 20) % 2 == 0:
                            d.rectangle((xx, yy, xx + 20, yy + 20), fill=(200, 200, 200, 255))
            else:
                bg = Image.new("RGBA", (W, H), col)
            comp = Image.alpha_composite(bg, frame)
            comp.convert("RGB").save(out / f"frame{tick:02d}_{name}.png")
    print(f"[splash] wrote sample frames → {out}")

    # Round-trip the premultiplied bytes back to a viewable image to verify the BGRA conversion.
    raw = premultiplied_bgra(r.render(8))
    bgra = Image.frombytes("RGBA", (W, H), raw, "raw", "RGBA")
    b, gg, rr, a = bgra.split()  # de-swizzle B,G,R,A → R,G,B,A for inspection
    Image.merge("RGBA", (rr, gg, b, a)).convert("RGB").save(out / "premultiplied_roundtrip.png")
    print("[splash] wrote premultiplied_roundtrip.png")

    if sys.platform == "win32":
        s = _Splash()
        s.show()
        import time
        time.sleep(seconds)
        try:
            from PIL import ImageGrab
            shot = ImageGrab.grab()
            shot.save(out / "live_desktop_grab.png")
            print(f"[splash] grabbed live desktop → {out / 'live_desktop_grab.png'}")
        except Exception as e:
            print(f"[splash] ImageGrab failed: {e}")
        s.close()
        time.sleep(0.3)


if __name__ == "__main__":
    _demo(float(sys.argv[1]) if len(sys.argv) > 1 else 6.0)
