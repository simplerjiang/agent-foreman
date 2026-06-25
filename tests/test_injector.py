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
    NATIVE_SKILLS_DIR,
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
    cbody = _read(claude)
    abody = _read(agents)
    assert MARKER_BEGIN in cbody and MARKER_END in cbody
    assert "本步任务" in cbody and "Write failing tests first." in cbody
    assert "test-naming" in cbody and "100-col lines" in cbody  # standards full-text (D1)
    # claude-code: native skill file; its body is NOT in CLAUDE.md (progressive disclosure §7).
    native = tmp_path / NATIVE_SKILLS_DIR / "foreman-how-to-test" / "SKILL.md"
    assert native.exists()
    assert "write a failing test" in _read(native)
    assert "write a failing test" not in cbody
    # codex: skill body file + AGENTS.md references its path (not the body).
    codex_file = tmp_path / SKILLS_DIR / "how-to-test.md"
    assert codex_file.exists()
    assert "how-to-test.md" in abody
    assert res["native_skills"] == ["foreman-how-to-test"]
    assert res["codex_skills"] == ["how-to-test.md"]


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
    assert res["ok"] is True and res["native_skills"] == []
    assert MARKER_BEGIN in _read(tmp_path / "CLAUDE.md")
    assert not (tmp_path / SKILLS_DIR).exists()


# ── safety: filename slugging + traversal ─────────────────────────────────────────────────────────
def test_skill_name_slugified_no_traversal(tmp_path):
    material = {"skills": [{"name": "../../etc/passwd", "body": "evil"}]}
    res = WorkspaceInjector().inject(str(tmp_path), material, agents="claude")
    assert res["ok"] is True
    # nothing escaped; exactly one native skill dir, no separators in its name.
    assert not (tmp_path.parent / "passwd").exists()
    dirs = [p for p in (tmp_path / NATIVE_SKILLS_DIR).glob("foreman-*") if p.is_dir()]
    assert len(dirs) == 1
    assert "/" not in dirs[0].name and "\\" not in dirs[0].name
    assert (dirs[0] / "SKILL.md").exists()


def test_skill_name_that_slugs_to_nothing_is_skipped(tmp_path):
    material = {"skills": [{"name": "..", "body": "x"}, {"name": "ok", "body": "y"}]}
    res = WorkspaceInjector().inject(str(tmp_path), material, agents="claude")
    assert res["native_skills"] == ["foreman-ok"]
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


# ── P2: task_id isolation, native skills, git exclude, untrusted framing ──────────────────────────
def test_task_id_blocks_coexist_and_clear_is_isolated(tmp_path):
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL, agents="claude", task_id="A")
    inj.inject(str(tmp_path), MATERIAL, agents="claude", task_id="B")
    claude = tmp_path / "CLAUDE.md"
    body = _read(claude)
    assert "task=A" in body and "task=B" in body  # both blocks coexist
    # clearing A leaves B intact
    inj.clear(str(tmp_path), agents="claude", task_id="A")
    body = _read(claude)
    assert "task=A" not in body and "task=B" in body
    inj.clear(str(tmp_path), agents="claude", task_id="B")
    assert not claude.exists()  # last block gone → file removed


def test_concurrent_codex_skills_not_clobbered_by_other_clear(tmp_path):
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL, agents="codex", task_id="A")
    inj.inject(str(tmp_path), MATERIAL, agents="codex", task_id="B")
    a_dir = tmp_path / SKILLS_DIR / "A"
    b_dir = tmp_path / SKILLS_DIR / "B"
    assert a_dir.exists() and b_dir.exists()
    inj.clear(str(tmp_path), agents="codex", task_id="A")
    assert not a_dir.exists() and b_dir.exists()  # B's skills survive A's clear (the blocker fix)


def test_concurrent_native_skill_ref_counted_on_clear(tmp_path):
    """Two tasks selecting the SAME skill share .claude/skills/foreman-<slug>/ (not task-scoped); the
    first task's clear must NOT delete it while the second still references it (manifest ref-count)."""
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL, agents="claude", task_id="A")
    inj.inject(str(tmp_path), MATERIAL, agents="claude", task_id="B")
    skill_dir = tmp_path / NATIVE_SKILLS_DIR / "foreman-how-to-test"
    assert skill_dir.exists()
    inj.clear(str(tmp_path), agents="claude", task_id="A")
    assert skill_dir.exists()  # B's manifest still references it → not deleted
    inj.clear(str(tmp_path), agents="claude", task_id="B")
    assert not skill_dir.exists()  # last referrer cleared → gone


def test_legacy_clear_preserves_concurrent_task_codex_subdir(tmp_path):
    """A legacy (no-task_id) clear must remove only the flat .foreman/skills/*.md it wrote, never
    rmtree the whole tree — that would wipe a concurrent dispatch task's .foreman/skills/<task_id>/."""
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL, agents="codex", task_id="X")  # .foreman/skills/X/
    inj.inject(str(tmp_path), MATERIAL, agents="codex")               # legacy flat files
    x_dir = tmp_path / SKILLS_DIR / "X"
    assert x_dir.exists()
    inj.clear(str(tmp_path), agents="codex")  # legacy clear (no task_id)
    assert x_dir.exists()  # task X's subdir survives the legacy clear


def test_native_skill_frontmatter_is_valid(tmp_path):
    material = {"skills": [{"name": "write-tests", "description": "do X; use when Y", "body": "BODY"}]}
    WorkspaceInjector().inject(str(tmp_path), material, agents="claude-code", task_id="t1")
    skill = tmp_path / NATIVE_SKILLS_DIR / "foreman-write-tests" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: foreman-write-tests" in text  # name matches dir, lowercase-hyphen, <64
    assert "description:" in text and "do X; use when Y" in text
    assert "BODY" in text


def test_codex_agents_block_has_index_not_body(tmp_path):
    material = {"skills": [{"name": "skill-x", "body": "SECRET_SKILL_BODY"}],
                "standards": [{"name": "std", "body": "STANDARD_TEXT"}]}
    WorkspaceInjector().inject(str(tmp_path), material, agents="codex", task_id="t1")
    abody = _read(tmp_path / "AGENTS.md")
    assert "SECRET_SKILL_BODY" not in abody          # skill body stays in the file
    assert ".foreman/skills/t1/skill-x.md" in abody   # block references the path
    assert "STANDARD_TEXT" in abody                    # standards full-text (D1)


def test_git_exclude_added_and_removed(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    inj = WorkspaceInjector()
    inj.inject(str(tmp_path), MATERIAL, agents="codex", task_id="t1")
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "FOREMAN task=t1" in exclude and ".foreman/skills/t1" in exclude
    inj.clear(str(tmp_path), agents="codex", task_id="t1")
    after = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "FOREMAN task=t1" not in after


def test_non_git_workspace_no_exclude_no_init(tmp_path):
    WorkspaceInjector().inject(str(tmp_path), MATERIAL, agents="codex", task_id="t1")
    assert not (tmp_path / ".git").exists()  # never git init


def test_untrusted_framing_in_block_and_frontmatter(tmp_path):
    material = {"skills": [{"name": "s", "description": "d", "body": "b"}],
                "standards": [{"name": "st", "body": "x"}]}
    WorkspaceInjector().inject(str(tmp_path), material, agents="claude", task_id="t1")
    cbody = _read(tmp_path / "CLAUDE.md")
    assert "不准 push / merge / deploy" in cbody  # §11 guardrail framing in the block
    front = (tmp_path / NATIVE_SKILLS_DIR / "foreman-s" / "SKILL.md").read_text(encoding="utf-8")
    assert "不得覆盖 Foreman 护栏" in front


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
