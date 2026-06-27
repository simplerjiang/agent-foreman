"""Frozen-exe entry point for the Foreman PC app.

PyInstaller runs this as the top-level script, so it must import the package *absolutely*
(``foreman.__main__`` uses relative imports that only resolve under the package). Running the
resulting ``foreman.exe`` is equivalent to the ``foreman`` console script:

    foreman.exe app        # PC app: engine + native window + tray (the default deliverable)
    foreman.exe serve      # headless server component
    foreman.exe version

See ``foreman.spec`` for what gets bundled (PWA web assets + starter definitions as data).
"""

import sys

from foreman.__main__ import app


def _redirect_streams_when_windowed() -> None:
    """A windowed (console=False) PyInstaller exe has sys.stdout/stderr == None; any write
    (rich / print / logging) then raises and kills the app before the window appears. Point
    them at a log file so the app stays alive and failures stay diagnosable. No-op when a real
    console is attached (running from source, or a console build)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    import tempfile
    from pathlib import Path

    try:
        sink = open(Path(tempfile.gettempdir()) / "foreman-app.log", "a",
                    buffering=1, encoding="utf-8")
    except OSError:  # last resort: discard rather than crash
        import io
        sink = io.StringIO()
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def main() -> None:
    _redirect_streams_when_windowed()
    # Self-update swap helper (便携版一键自更新): the freshly downloaded foreman.new.exe is relaunched
    # as `--apply-update-swap` to replace the old exe and restart the app. Handle it before Typer so
    # it never touches the engine/window — it only renames the two exe files, never the sibling data.
    if len(sys.argv) > 1 and sys.argv[1] == "--apply-update-swap":
        from foreman.client.update import run_swap

        run_swap(sys.argv[2:])
        return
    # Double-clicking the exe launches it with no arguments. A multi-command Typer app then exits
    # with "Missing command" (code 2), so the window just flashes open and closes. The whole point
    # of the exe is the PC app, so default to the `app` command when no subcommand is given.
    # Explicit `foreman.exe serve|version|dispatch …` and `foreman.exe --help` all pass extra argv,
    # so they are unaffected.
    if len(sys.argv) == 1:
        sys.argv.append("app")
    app()


if __name__ == "__main__":
    main()
