"""Smoke tests for the client layer (TASKS T0.2).

The client (PC app + agents) must import depending ONLY on foreman.shared — never on
foreman.server (the server is a relay; 秘方 stays local). DESIGN §14.
"""

from __future__ import annotations


def test_client_public_surface_imports():
    from foreman.client.agents.base import AgentAdapter, AgentHandle  # noqa: F401
    from foreman.client.agents.runner import Runner  # noqa: F401
    from foreman.client.store import Store  # noqa: F401


def test_client_imports_cleanly_and_no_server_leak():
    """Every client submodule imports, in a FRESH interpreter, and none drags in
    foreman.server. Subprocess so other tests' imports can't pollute sys.modules."""
    import os
    import subprocess
    import sys

    script = (
        "import importlib, pkgutil, sys, foreman.client as c\n"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(c.__path__, 'foreman.client.')]\n"
        "leak=[n for n in sys.modules if n.startswith('foreman.server')]\n"
        "sys.exit('client leaked into server: %s' % leak if leak else 0)\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
