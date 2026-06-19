"""Smoke tests for the server layer (TASKS T0.3).

Server imports depending only on foreman.shared (never client); /health boots; the PWA
front-end (now under server/web/) is mounted; the server store tables construct.
"""

from __future__ import annotations


def test_server_imports_and_no_client_leak():
    """All server submodules import in a fresh interpreter, dragging in no client modules."""
    import os
    import subprocess
    import sys

    script = (
        "import importlib, pkgutil, sys, foreman.server as s\n"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(s.__path__, 'foreman.server.')]\n"
        "leak=[n for n in sys.modules if n.startswith('foreman.client')]\n"
        "sys.exit('server leaked into client: %s' % leak if leak else 0)\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_health_and_web_mounted(tmp_path):
    from fastapi.testclient import TestClient

    from foreman.server.app import create_app
    from foreman.shared.config import load_config

    client = TestClient(create_app(load_config(tmp_path / "none.yaml")))
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["ok"] is True
    # web/ now lives under server/ and is mounted at "/" -> index.html is served
    assert client.get("/").status_code == 200


def test_server_store_tables():
    from foreman.server.store import ServerStore
    from foreman.server.store import models as m

    store = ServerStore(":memory:")
    store.init()
    names = {t.__table__.name for t in m.SERVER_TABLES}
    assert {
        "accounts", "access_keys", "process_registry",
        "cache_sessions", "cache_cards", "invites", "schema_version",
    } == names
