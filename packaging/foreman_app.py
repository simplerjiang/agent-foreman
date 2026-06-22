"""Frozen-exe entry point for the Foreman PC app.

PyInstaller runs this as the top-level script, so it must import the package *absolutely*
(``foreman.__main__`` uses relative imports that only resolve under the package). Running the
resulting ``foreman.exe`` is equivalent to the ``foreman`` console script:

    foreman.exe app        # PC app: engine + native window + tray (the default deliverable)
    foreman.exe serve      # headless server component
    foreman.exe version

See ``foreman.spec`` for what gets bundled (PWA web assets + starter definitions as data).
"""

from foreman.__main__ import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
