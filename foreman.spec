# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build for the Foreman PC app (`foreman app`) — DESIGN §682 "PyInstaller 打单 exe".
#
#   Build (Windows, from repo root, with the client extras installed):
#       pip install -e ".[client,pty]" pyinstaller
#       pyinstaller foreman.spec --noconfirm
#   Output: dist/foreman.exe  (single file; `dist/` is gitignored).
#
# The exe is the Typer CLI frozen whole, so `foreman.exe app` opens the native window + tray,
# while `foreman.exe serve` / `version` work headlessly (used by the build smoke test).

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SRC = Path("src")

# Data the code locates at runtime via __file__ / importlib.resources — must keep the same
# package-relative layout inside the bundle:
#   server/app.py:  WEB_DIR = Path(__file__).parent / "web"
#   client/core/examples.py:  importlib.resources.files("foreman.examples")/"definitions"
datas = [
    (str(SRC / "foreman" / "server" / "web"), "foreman/server/web"),
    (str(SRC / "foreman" / "examples" / "definitions"), "foreman/examples/definitions"),
]
# pywebview ships JS/HTML shims it loads at runtime; bundle them too.
datas += collect_data_files("webview")

# Dynamic imports PyInstaller's static analysis can't see: uvicorn picks its loop/protocol
# backends by string at runtime; the whole foreman tree is imported lazily across commands;
# pywebview/pystray select a platform backend at import time.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("foreman")
    + collect_submodules("webview")
    + ["pystray._win32"]
)

a = Analysis(
    ["packaging/foreman_app.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Server-only, lazily imported (server/push.py imports pywebpush inside functions). The client
    # exe never needs them; excluding keeps the bundle from failing on the absent server extra.
    excludes=["pywebpush", "py_vapid"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="foreman",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    # Windowed app (no console): the exe is double-clicked by end users, so `app` opens just the
    # native window + tray — no console flash. In this mode sys.stdout/stderr are None, so the
    # launcher (packaging/foreman_app.py) redirects them to a log file before anything prints.
    # Trade-off: CLI subcommands (`serve`/`version`) no longer write to a terminal.
    console=False,
    disable_windowed_traceback=False,
    icon="packaging/foreman.ico",
)
