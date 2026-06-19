"""Tests for the PC app's local server core (TASKS T1.12).

Exercises the headless, testable core (start_local_app) — a real uvicorn server in a thread.
The native pywebview window is a thin shell over the T1.11 (browser-accepted) UI and is verified
by running `foreman app` on a desktop.
"""

from __future__ import annotations

import urllib.request

from foreman.client.local_app import start_local_app
from foreman.shared.config import Config


def test_start_local_app_serves_and_stops(tmp_path):
    cfg = Config()
    cfg.store.db_path = str(tmp_path / "t.db")
    local = start_local_app(cfg, port=8793)
    try:
        assert local.url == "http://127.0.0.1:8793/"
        with urllib.request.urlopen(local.url + "health", timeout=5) as r:
            assert r.status == 200
        with urllib.request.urlopen(local.url, timeout=5) as r:  # index page served
            assert r.status == 200
        # engine wired
        assert local.store is not None and local.runner is not None
    finally:
        local.stop()
    assert not local._thread.is_alive()  # stop() actually shut the server thread down
