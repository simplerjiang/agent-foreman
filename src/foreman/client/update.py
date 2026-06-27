"""Portable in-place self-update for the packaged Windows exe (便携版一键自更新).

Design (chosen with the user, A2 路线 — 便携·单文件夹·绝不碰数据):

  Foreman\\                  ← the portable folder the user dropped the exe in
  ├─ foreman.exe            ← the ONLY file an update replaces
  ├─ foreman.db             ← data — never touched
  ├─ config.yaml            ← config — never touched
  ├─ .env                   ← secrets — never touched
  └─ (transient) foreman.new.exe / foreman.old.exe   ← swap scratch, auto-removed

The exe locates ``foreman.db`` / ``config.yaml`` / ``.env`` *relative to the working directory*
(see shared.config.load_config + StoreCfg.db_path). When the user double-clicks the exe the cwd is
the exe's own folder, so the data sits beside the exe. An update therefore must (a) replace ONLY
``foreman.exe`` and (b) relaunch with that same folder as cwd — then the data is preserved by
construction. This module never reads or writes the sibling data files.

Replacing a *running* single-file exe on Windows: you cannot overwrite a running image, but you
*can rename* it. So the swap (run inside the freshly downloaded ``foreman.new.exe`` via the hidden
``--apply-update-swap`` entry) is: wait for the old pid to exit → rename ``foreman.exe`` →
``foreman.old.exe`` → rename the running ``foreman.new.exe`` → ``foreman.exe`` → relaunch it. The
next startup deletes the leftover ``foreman.old.exe`` (cleanup_stale).

Everything here is a no-op unless running frozen (PyInstaller). From source there is no exe to swap,
so ``check()`` reports ``available=False`` and ``begin_apply()`` refuses.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# The GitHub repo whose Releases the exe self-updates from. The CI release job publishes an asset
# named ``foreman-<version>-windows.exe`` on tag ``v<version>`` (AGENTS.md §四) — we match on that.
REPO = "simplerjiang/agent-foreman"
_LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
_ASSET_SUFFIX = "-windows.exe"

NEW_EXE = "foreman.new.exe"
OLD_EXE = "foreman.old.exe"

# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — the relaunched app/swap helper outlives us and is not
# tied to our (dying) console/process group. Windows-only flags; guarded by is_frozen() before use.
_DETACHED = 0x00000008 | 0x00000200

# Set by the desktop entry (app_cmd) to a callback that closes the window for a clean shutdown.
# When unset (headless), the apply worker falls back to os._exit so the swap helper can proceed.
_shutdown_hook: Callable[[], None] | None = None


def register_shutdown_hook(fn: Callable[[], None]) -> None:
    """Register how to shut the app down for a restart (the desktop wires window.destroy())."""
    global _shutdown_hook
    _shutdown_hook = fn


def _do_shutdown() -> None:
    if _shutdown_hook is not None:
        try:
            _shutdown_hook()
            return
        except Exception:  # noqa: BLE001 — fall back to a hard exit so the swap can still proceed
            pass
    os._exit(0)


def is_frozen() -> bool:
    """True when running as the PyInstaller-frozen exe (the only context a self-swap makes sense)."""
    return bool(getattr(sys, "frozen", False))


def current_version() -> str:
    from .. import __version__

    return __version__


def parse_version(s: str) -> tuple[int, ...]:
    """``"1.0.2"`` → ``(1, 0, 2)``; tolerant of a leading ``v`` and junk (junk parts → 0)."""
    s = (s or "").strip().lstrip("vV")
    parts = []
    for chunk in s.split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) if parts else (0,)


def exe_path() -> Path:
    return Path(sys.executable)


def cleanup_stale(directory: Path | str | None = None) -> None:
    """Best-effort removal of leftover swap files (foreman.old.exe / foreman.new.exe). Called at
    startup: after a successful swap the old binary is no longer locked, so this finally deletes it.
    Never raises — a locked leftover just gets cleaned on a later launch."""
    if not is_frozen():
        return
    base = Path(directory) if directory else exe_path().parent
    for name in (OLD_EXE, NEW_EXE):
        p = base / name
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass  # still locked (rare race) — next startup will get it


def _fetch_latest(timeout: float) -> dict | None:
    """Query GitHub's latest release; return {version, url, size, name, notes} or None."""
    import httpx

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "foreman-updater"}
    r = httpx.get(_LATEST_URL, headers=headers, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    tag = str(data.get("tag_name") or "")
    asset = next(
        (a for a in (data.get("assets") or []) if str(a.get("name", "")).endswith(_ASSET_SUFFIX)),
        None,
    )
    if not tag or asset is None:
        return None
    return {
        "version": tag.lstrip("vV"),
        "url": asset.get("browser_download_url") or "",
        "size": int(asset.get("size") or 0),
        "name": asset.get("name") or "",
        "notes": str(data.get("body") or "")[:4000],
    }


class Updater:
    """Self-update surface for the local app. Injected (duck-typed) into the server so app.py never
    imports the client (DESIGN §14). ``check()`` is safe to call from source (reports unavailable);
    ``begin_apply()`` only acts when frozen."""

    def __init__(self) -> None:
        self._latest: dict | None = None
        self._applying = False
        self._lock = threading.Lock()
        self._error: str = ""

    def is_frozen(self) -> bool:
        return is_frozen()

    # -- query ---------------------------------------------------------------
    def check(self, timeout: float = 6.0) -> dict:
        """Compare the running version to the latest GitHub Release. Returns a JSON-able dict with
        at least {current, frozen, available}. Network/parse failures degrade to available=False."""
        cur = current_version()
        out: dict = {"current": cur, "frozen": is_frozen(), "available": False}
        try:
            latest = _fetch_latest(timeout)
        except Exception as exc:  # noqa: BLE001 — offline / rate-limited / GitHub hiccup → no update
            out["error"] = type(exc).__name__
            return out
        if not latest:
            return out
        self._latest = latest
        newer = parse_version(latest["version"]) > parse_version(cur)
        out["latest"] = latest["version"]
        out["notes"] = latest["notes"]
        out["size"] = latest["size"]
        # Only *offer* an in-place update when frozen — from source there is no exe to swap.
        out["available"] = bool(newer and is_frozen() and latest["url"])
        if self._applying:
            out["applying"] = True
        if self._error:
            out["apply_error"] = self._error
        return out

    # -- apply ---------------------------------------------------------------
    def begin_apply(self) -> dict:
        """Kick off download + swap in the background and return immediately. The app will go down
        and come back on the new version. No-op (refused) unless frozen with a known newer release."""
        if not is_frozen():
            return {"ok": False, "reason": "not_frozen"}
        with self._lock:
            if self._applying:
                return {"ok": True, "started": True, "already": True}
            info = self._latest
            if not info or parse_version(info["version"]) <= parse_version(current_version()):
                # Re-check in case begin_apply is called before any check() populated the cache.
                try:
                    info = _fetch_latest(6.0)
                except Exception:  # noqa: BLE001
                    info = None
                if not info or parse_version(info["version"]) <= parse_version(current_version()):
                    return {"ok": False, "reason": "no_update"}
                self._latest = info
            if not info.get("url"):
                return {"ok": False, "reason": "no_asset"}
            self._applying = True
            self._error = ""
        threading.Thread(target=self._apply_worker, args=(info,), daemon=True).start()
        return {"ok": True, "started": True, "version": info["version"]}

    def _apply_worker(self, info: dict) -> None:
        exe = exe_path()
        new_exe = exe.with_name(NEW_EXE)
        try:
            _download(info["url"], new_exe, int(info.get("size") or 0))
        except Exception as exc:  # noqa: BLE001 — surface to the UI on the next check()
            self._error = f"download failed: {type(exc).__name__}"
            self._applying = False
            try:
                if new_exe.exists():
                    new_exe.unlink()
            except OSError:
                pass
            return
        # Hand off to the freshly downloaded binary: it waits for *us* to exit, then renames itself
        # into place and relaunches. cwd = the exe folder so it (and the relaunched app) keep finding
        # the sibling data files.
        try:
            subprocess.Popen(
                [str(new_exe), "--apply-update-swap", "--old-pid", str(os.getpid()),
                 "--target", str(exe)],
                cwd=str(exe.parent),
                creationflags=_DETACHED,
                close_fds=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._error = f"swap launch failed: {type(exc).__name__}"
            self._applying = False
            return
        # Give the response a moment to flush, then bring the app down so the swap can replace the exe.
        time.sleep(0.6)
        _do_shutdown()


def _download(url: str, dest: Path, expected_size: int) -> None:
    """Stream ``url`` to ``dest`` atomically (write .part → fsync → rename). Verifies the byte count
    against the release asset size when known, so a truncated download never gets swapped in."""
    import httpx

    part = dest.with_name(dest.name + ".part")
    try:
        if part.exists():
            part.unlink()
    except OSError:
        pass
    written = 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0,
                      headers={"User-Agent": "foreman-updater"}) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_bytes(1024 * 256):
                f.write(chunk)
                written += len(chunk)
            f.flush()
            os.fsync(f.fileno())
    if expected_size and written != expected_size:
        try:
            part.unlink()
        except OSError:
            pass
        raise OSError(f"size mismatch: got {written}, expected {expected_size}")
    os.replace(part, dest)


# ---------------------------------------------------------------------------
# Swap helper — runs INSIDE the downloaded foreman.new.exe via `--apply-update-swap`.
# ---------------------------------------------------------------------------
def run_swap(argv: list[str]) -> None:
    """Entry for ``foreman.new.exe --apply-update-swap --old-pid N --target <foreman.exe>``.

    Replaces the (now-exited) old exe with this running binary, then relaunches the real app. Only
    ever touches the two exe files — the sibling foreman.db / config.yaml / .env are not referenced.
    """
    opts = _parse_kv(argv)
    old_pid = _to_int(opts.get("old-pid"))
    target_raw = (opts.get("target") or "").strip()
    running = exe_path()  # this == foreman.new.exe
    if not target_raw:
        _SwapLog(running.parent).write("no target — abort")
        return
    target = Path(target_raw)
    log = _SwapLog(target.parent)
    log.write(f"swap start: old_pid={old_pid} target={target} running={running}")

    _wait_pid_gone(old_pid, timeout=120.0)
    time.sleep(0.5)  # small grace for the OS to release the old image handle

    backup = target.with_name(OLD_EXE)
    try:
        if backup.exists():
            backup.unlink()
    except OSError:
        pass

    # 1. old foreman.exe → foreman.old.exe (it has exited, so it's unlocked now)
    if target.exists():
        if not _retry(lambda: os.replace(target, backup)):
            log.write("could not move old exe aside — abort (old version stays intact)")
            return
    # 2. this running foreman.new.exe → foreman.exe (renaming a running image is allowed on Windows)
    if not _retry(lambda: os.replace(running, target)):
        log.write("could not move new exe into place — rolling back")
        _retry(lambda: os.replace(backup, target))  # restore the old exe so the folder still works
        return
    log.write("swap done — relaunching")
    # 3. relaunch the app in the same folder (cwd) so it finds the sibling data files.
    try:
        subprocess.Popen([str(target), "app"], cwd=str(target.parent),
                         creationflags=_DETACHED, close_fds=True)
    except Exception as exc:  # noqa: BLE001
        log.write(f"relaunch failed: {exc!r}")
    # foreman.old.exe is cleaned up by the relaunched app's cleanup_stale() on startup.


def _parse_kv(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                out[key] = argv[i + 1]
                i += 2
                continue
            out[key] = ""
        i += 1
    return out


def _to_int(s: str | None) -> int:
    try:
        return int(s or 0)
    except (TypeError, ValueError):
        return 0


def _retry(fn: Callable[[], object], attempts: int = 30, delay: float = 0.5) -> bool:
    """Run ``fn`` until it stops raising OSError (file still locked), up to ``attempts`` times."""
    for _ in range(attempts):
        try:
            fn()
            return True
        except OSError:
            time.sleep(delay)
    return False


def _wait_pid_gone(pid: int, timeout: float) -> None:
    """Block until process ``pid`` is gone (or timeout). Best-effort; uses psutil when present."""
    if pid <= 0:
        return
    deadline = time.monotonic() + timeout
    try:
        import psutil

        while time.monotonic() < deadline:
            if not psutil.pid_exists(pid):
                return
            time.sleep(0.3)
        return
    except Exception:  # noqa: BLE001 — psutil missing → fall back to a plain wait
        pass
    while time.monotonic() < deadline:
        if not _pid_alive_win(pid):
            return
        time.sleep(0.3)


def _pid_alive_win(pid: int) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        # WAIT_TIMEOUT (0x102) → still running; WAIT_OBJECT_0 (0) → exited.
        res = ctypes.windll.kernel32.WaitForSingleObject(h, 0)
        ctypes.windll.kernel32.CloseHandle(h)
        return res == 0x102
    except Exception:  # noqa: BLE001
        return False


class _SwapLog:
    """Tiny append log beside the exe so a failed swap is diagnosable (the helper has no console)."""

    def __init__(self, directory: Path) -> None:
        self._path = directory / "foreman-update.log"

    def write(self, msg: str) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except OSError:
            pass
