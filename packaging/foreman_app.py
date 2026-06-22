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


def main() -> None:
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
