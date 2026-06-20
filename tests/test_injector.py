"""Tests for 事前注入工作区 — WorkspaceInjector (T5.3 / DESIGN §11.2 D).

Covers, over a real tmp_path workspace:
  - inject(): writes CLAUDE.md/AGENTS.md managed block + .foreman/skills/<slug>.md files;
  - agent-targeted guidance file selection (claude → CLAUDE.md, codex → AGENTS.md);
  - user content outside the managed block is preserved; re-inject replaces (idempotent, no stacking);
  - clear(): strips the block, deletes files we created, drops .foreman/skills; reverts the workspace;
  - safety: skill names are slugified (no path traversal); "../.." names skipped; allowed_roots gate.
"""

from __future__ import annotations

from pathlib import Path

from foreman.client.core.injector import (
    MARKER_BEGIN,
    MARKER_END,
    SKILLS_DIR,
    WorkspaceInjector,
)

MATERIAL = {
    "instruction": "Write failing tests first.",
    "standards": [
        {"name": "test-naming", "body": "name tests test_<unit>_<case>"},
        {"name": "our-style", "body": "100-col lines, no bare except"},
    ],
    "skills": [
        {"name": "how-to-test", "body": "# write a failing test, then make it pass"},
    ],
}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── inject ──────────────────────────────────────────────────────────────────────────────────────
def test_inject_writes_guidance_and_skill_files(tmp_path):
    res = WorkspaceInjector().inject(str(tmp_path), MATERIAL)
    assert res["ok"] is True
    claude = tmp_path / "CLAUDE.md"
    agents = tmp_path / "AGENTS.md"
    assert claude.exists() and agents.exists()
    body = _read(claude)
    assert MARKER_BEGIN in body and MARKER_END in body
    assert "本步任务" in body and "Write failing tests first." in body
    assert "test-naming" in body and "100-col lines" in body
    # the skill is a separate file, linked from the guidance block.
    skill_file = tmp_path / SKILLS_DIR / "how-to-test.md"
    assert skill_file.exists()
    assert "write a failing test" in _read(skill_file)
    assert f"{SKILLS_DIR}/how-to-test.md" in body
    assert res["skills"] == ["how-to-test.md"]


def test_inject_targets_agent_specific_file(tmp_path):
    res = WorkspaceInjector().inject(str(tmp_path), MATERIAL, agents="codex")
    assert res["ok"] is True
    assert (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()

    WorkspaceInjector().inject(str(tmp_path), MATERIAL, agents="claude-code")
    assert (tmp_path / "CLAUDE.md").exists()


def test_inject_preserves_user_content(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# My project rules\nAlways be nice.\n", encoding="utf-8")
    WorkspaceInjector().inject(str(tmp_path), MATERIAL, agents="claude")
    body = _read(claude)
    assert "# My project rules" in body and "Always be nice." in body
    assert MARKER_BEGIN in body


def test_reinject_replaces_block_no_stacking(tmp_path):
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL, agents="claude")
    inj.inject(str(tmp_path), MATERIAL, agents="claude")
    body = _read(tmp_path / "CLAUDE.md")
    assert body.count(MARKER_BEGIN) == 1 and body.count(MARKER_END) == 1


def test_inject_no_workspace():
    assert WorkspaceInjector().inject("", MATERIAL) == {"ok": False, "error": "no_workspace"}


def test_inject_empty_material_still_writes_block(tmp_path):
    res = WorkspaceInjector().inject(str(tmp_path), {}, agents="claude")
    assert res["ok"] is True and res["skills"] == []
    assert MARKER_BEGIN in _read(tmp_path / "CLAUDE.md")
    assert not (tmp_path / SKILLS_DIR).exists()


# ── safety: filename slugging + traversal ─────────────────────────────────────────────────────────
def test_skill_name_slugified_no_traversal(tmp_path):
    material = {"skills": [{"name": "../../etc/passwd", "body": "evil"}]}
    res = WorkspaceInjector().inject(str(tmp_path), material, agents="claude")
    assert res["ok"] is True
    # nothing escaped the skills dir; no file outside the workspace.
    assert not (tmp_path.parent / "passwd").exists()
    written = list((tmp_path / SKILLS_DIR).glob("*.md"))
    assert len(written) == 1
    # the slug stays inside the skills dir and contains no separators.
    assert written[0].parent == tmp_path / SKILLS_DIR
    assert "/" not in written[0].name and "\\" not in written[0].name


def test_skill_name_that_slugs_to_nothing_is_skipped(tmp_path):
    material = {"skills": [{"name": "..", "body": "x"}, {"name": "ok", "body": "y"}]}
    res = WorkspaceInjector().inject(str(tmp_path), material, agents="claude")
    assert res["skills"] == ["ok.md"]
    assert ".." in res["skipped"]


def test_allowed_roots_gate(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inj = WorkspaceInjector(allowed_roots=[str(allowed)])
    # inside the allowed root → ok
    assert inj.inject(str(allowed / "proj"), MATERIAL, agents="claude")["ok"] is True
    # outside → refused
    outside = tmp_path / "outside"
    res = inj.inject(str(outside), MATERIAL, agents="claude")
    assert res == {"ok": False, "error": "workspace_not_allowed"}
    assert not outside.exists()


# ── clear ─────────────────────────────────────────────────────────────────────────────────────────
def test_clear_deletes_files_we_created(tmp_path):
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL)
    res = inj.clear(str(tmp_path))
    assert res["ok"] is True
    # files we created are gone; .foreman cleaned up.
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / SKILLS_DIR).exists()
    assert not (tmp_path / ".foreman").exists()


def test_clear_preserves_user_content(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Mine\nkeep me\n", encoding="utf-8")
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL, agents="claude")
    inj.clear(str(tmp_path), agents="claude")
    assert claude.exists()
    body = _read(claude)
    assert "# Mine" in body and "keep me" in body
    assert MARKER_BEGIN not in body


def test_clear_is_idempotent_noop(tmp_path):
    res = WorkspaceInjector().clear(str(tmp_path))
    assert res["ok"] is True and res["removed"] == []


def test_self_healing_on_duplicated_markers(tmp_path):
    """A duplicated marker state (e.g. a crashed half-write) collapses into one block, and strips clean.

    _block_span() spans first BEGIN → last END, so two stacked blocks fold into the single new one
    rather than desyncing and leaking stale scaffolding.
    """
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(
        f"# Mine\nkeep me\n\n{MARKER_BEGIN}\nold\n{MARKER_END}\n{MARKER_BEGIN}\nolder\n{MARKER_END}\n",
        encoding="utf-8",
    )
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL, agents="claude")
    body = _read(claude)
    assert body.count(MARKER_BEGIN) == 1 and body.count(MARKER_END) == 1  # collapsed to one
    assert "# Mine" in body and "keep me" in body
    assert "old" not in body and "older" not in body  # stale block content gone
    # and a clear removes the block, leaving the user content
    inj.clear(str(tmp_path), agents="claude")
    out = _read(claude)
    assert MARKER_BEGIN not in out and "# Mine" in out and "keep me" in out
