from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.core.agent_guidelines import guideline_context_for_workspace
from foreman.client.core.pm_agent import build_plan_prompt
from foreman.client.store import Store
from foreman.server.app import create_app
from foreman.shared.config import AgentGuidelinesCfg, Config
from foreman.shared.events import EventBus


def test_guideline_context_prefers_workspace_root(tmp_path):
    nested = tmp_path / "pkg"
    nested.mkdir()
    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (nested / "AGENTS.md").write_text("nested rules", encoding="utf-8")

    text = guideline_context_for_workspace(
        str(tmp_path), AgentGuidelinesCfg(filenames=["AGENTS.md"])
    )

    assert "root rules" in text
    assert "nested rules" not in text


def test_guideline_context_searches_inward_and_respects_disabled(tmp_path):
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    (nested / "AGENT.md").write_text("inner rules", encoding="utf-8")

    enabled = guideline_context_for_workspace(
        str(tmp_path), AgentGuidelinesCfg(filenames=["AGENT.md"])
    )
    disabled = guideline_context_for_workspace(
        str(tmp_path), AgentGuidelinesCfg(enabled=False, filenames=["AGENT.md"])
    )

    assert "inner rules" in enabled
    assert disabled == ""


def test_plan_prompt_includes_resolved_guidelines():
    prompt = build_plan_prompt(
        "fix it",
        workspace="repo",
        available_agents=[],
        requested_agent="",
        pm_model="brain",
        requested_effort="",
        agent_guidelines="# Agent guideline files\nroot rules",
    )

    assert "# Agent guideline files" in prompt
    assert "root rules" in prompt


def test_agent_guideline_settings_defaults_save_and_refresh(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = Config()
    client = TestClient(create_app(cfg, store, EventBus()))

    defaults = client.get("/api/settings/agent-guidelines").json()
    assert defaults == {"enabled": True, "filenames": ["AGENT.md", "AGENTS.md"]}

    saved = client.post(
        "/api/settings/agent-guidelines",
        json={"enabled": False, "filenames": ["RULES.md", "", "RULES.md", "AGENT.md"]},
    ).json()

    assert saved == {"enabled": False, "filenames": ["RULES.md", "AGENT.md"]}
    assert client.get("/api/settings/agent-guidelines").json() == saved


def test_agent_guideline_settings_frontend_wired():
    js = TestClient(create_app(Config())).get("/app.js").text

    assert "/api/settings/agent-guidelines" in js
    assert "agentGuidelines" in js and "saveAgentGuidelines" in js
    assert "准则文件列表" in js and "Guideline file list" in js
    assert "AGENT.md" in js and "AGENTS.md" in js
