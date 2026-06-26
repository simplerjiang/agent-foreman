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
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(globals().get("SPECPATH", ".")).resolve()
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


def _conda_runtime_dlls():
    """Bundle native DLLs required by conda-backed Python builds.

    The Windows build may run from a venv whose ``sys.base_prefix`` points at a Conda
    installation. PyInstaller can analyze the ``.pyd`` extensions but, unless Conda's
    ``Library/bin`` is on PATH at build time, it may miss runtime DLLs such as
    ``ffi.dll``. The resulting single-file exe then crashes during import of modules
    like ``ctypes`` before Foreman can start. Add the known Conda runtime DLLs
    explicitly so builds are reproducible even from a plain PowerShell session.
    """
    library_bin = Path(sys.base_prefix) / "Library" / "bin"
    if not library_bin.exists():
        return []

    dll_names = (
        "ffi.dll",
        "sqlite3.dll",
        "libssl-3-x64.dll",
        "libcrypto-3-x64.dll",
        "liblzma.dll",
        "LIBBZ2.dll",
        "libmpdec-4.dll",
        "libexpat.dll",
    )
    return [(str(library_bin / name), ".") for name in dll_names if (library_bin / name).exists()]

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
    + [m for m in collect_submodules("foreman") if m != "foreman.server.display_cache"]
    + collect_submodules("webview")
    + ["pystray._win32"]
)

a = Analysis(
    ["packaging/foreman_app.py"],
    pathex=[str(SRC)],
    binaries=_conda_runtime_dlls(),
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
