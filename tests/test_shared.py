"""Smoke tests for the shared layer (TASKS T0.1).

The shared layer must import on its own — no reverse dependency on client/server, and
no heavy deps — so both deployables (PC app + server) can rely on it.
"""

from __future__ import annotations


def test_shared_imports_standalone():
    import foreman.shared.config as config
    import foreman.shared.events as events
    import foreman.shared.protocol as protocol
    from foreman.shared.llm import LLMClient, Message  # noqa: F401

    assert hasattr(config, "Config")
    assert hasattr(config, "load_config")
    assert "dispatch" in events.EVENT_TYPES
    assert protocol.PROTOCOL_VERSION >= 1


def test_config_loads_defaults(tmp_path):
    from foreman.shared.config import load_config

    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert cfg.server.port == 8787
    assert cfg.llm.provider in {"openai", "anthropic"}
    assert sorted(cfg.agents) == ["claude-code", "codex"]
    assert cfg.agents["claude-code"].command == "claude"
    assert cfg.agents["codex"].command == "codex"


def test_config_loads_env_next_to_config(tmp_path, monkeypatch):
    from foreman.shared.config import load_config

    monkeypatch.delenv("FOREMAN_LLM_API_KEY", raising=False)
    cfg_dir = tmp_path / "foreman"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("llm:\n  model: test-model\n", encoding="utf-8")
    (cfg_dir / ".env").write_text("FOREMAN_LLM_API_KEY=adjacent-key\n", encoding="utf-8")

    cfg = load_config(cfg_dir / "config.yaml")

    assert cfg.llm.model == "test-model"
    assert cfg.secrets.llm_api_key == "adjacent-key"


def test_agent_event_and_bus():
    from foreman.shared.events import AgentEvent, EventBus

    ev = AgentEvent(type="dispatch", source="test", session_id="s1")
    assert ev.task_id is None and ev.payload == {}
    EventBus()  # constructs without error


def test_shared_does_not_pull_in_client_or_server():
    """Boundary guard: importing all of shared, in a FRESH interpreter, must not drag in
    client/server. Uses a subprocess so other tests' imports can't pollute sys.modules."""
    import os
    import subprocess
    import sys

    script = (
        "import importlib, pkgutil, sys, foreman.shared as s\n"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(s.__path__, 'foreman.shared.')]\n"
        "leak=[n for n in sys.modules if n.startswith(('foreman.client','foreman.server'))]\n"
        "sys.exit('shared leaked: %s' % leak if leak else 0)\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
