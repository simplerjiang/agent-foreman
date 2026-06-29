#!/usr/bin/env python3
"""Apply production hardening defaults to a Foreman server config."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


SERVER_HARDENING = {
    "trust_proxy_headers": True,
    "force_https": True,
    "health_show_db": False,
}


def harden_config(path: Path) -> bool:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")

    server = data.setdefault("server", {})
    if not isinstance(server, dict):
        raise ValueError("server config must be a mapping")

    changed = False
    for key, value in SERVER_HARDENING.items():
        if server.get(key) != value:
            server[key] = value
            changed = True

    if changed:
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return changed


def main(argv: list[str]) -> int:
    target = Path(argv[1] if len(argv) > 1 else "config.yaml")
    changed = harden_config(target)
    print(f"{target}: {'updated' if changed else 'already hardened'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
