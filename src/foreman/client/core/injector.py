"""事前注入工作区 — write a step's skill/standard material into the workspace before the agent runs.

DESIGN §11.2 D ("前面教、后面考", 双保险): the first half — **事前注入**. Before claude/codex starts a
workflow step, the engine resolves that step's **skills + code standards** (T5.2, ``injected_md``);
this module *materializes* them into the workspace so the agent reads them on startup:

  - ``CLAUDE.md`` (what Claude Code loads) and ``AGENTS.md`` (what codex loads) get the always-on
    rules — the step instruction + every code standard — inside a **managed marker block**, so any
    user-authored content in those files is preserved (we only own the block between the markers).
  - each **skill** ("做法手册", a how-to manual) is written to ``.foreman/skills/<slug>.md`` and the
    managed block links to it — keeping the guidance file lean while richer skill docs sit on disk.

It is **reversible**: ``clear`` strips the managed block (deleting a file we created outright) and
removes the ``.foreman/skills`` directory, so the workspace returns to how the user left it — these
are AI-managed scaffolding, not something to leak into the user's commits/diffs.

Client-side only (touches files in the local workspace; nothing here reaches the shared server,
DESIGN §8.3 / §14). Pure filesystem + string assembly — no subprocess/shell/eval, fully unit-testable.

**Safety (§6.6 / §6.7):**
  - skill names become filenames, so they are slugified to ``[A-Za-z0-9._-]`` with leading dots/dashes
    stripped — a name like ``../../etc/passwd`` can never escape ``.foreman/skills`` (separators are
    replaced; ``..`` slugs to empty and is skipped). Names that slug to nothing are reported, not written.
  - an optional ``allowed_roots`` allowlist (defense in depth, mirrors the dispatch workspace check) makes
    ``inject`` refuse a workspace outside every approved root, even though the caller passes a vetted path.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

# Managed-block markers — HTML comments so they stay invisible in rendered Markdown. We own ONLY the
# text between BEGIN and END; everything else in the file is the user's and is left untouched.
MARKER_BEGIN = "<!-- FOREMAN:BEGIN — auto-generated per-step guidance, do not edit -->"
MARKER_END = "<!-- FOREMAN:END -->"

# Where richer skill docs land (a Foreman-managed namespace inside the workspace).
SKILLS_DIR = ".foreman/skills"

# Which guidance file each agent reads on startup (DESIGN §11.2 D). Aliases included so a session's
# ``agent_type`` ("claude-code" / "codex") maps cleanly.
AGENT_GUIDANCE_FILES = {
    "claude": "CLAUDE.md",
    "claude-code": "CLAUDE.md",
    "codex": "AGENTS.md",
}

# With no agent specified we write both — having both files never hurts and covers either CLI.
_DEFAULT_GUIDANCE_FILES = ("CLAUDE.md", "AGENTS.md")

_SLUG_RE = re.compile(r"[^0-9A-Za-z._-]+")


def _slug(name: str) -> str:
    """A safe filename stem from a skill name: keep ``[A-Za-z0-9._-]``, strip leading dots/dashes.

    Path separators are replaced, so traversal is impossible; ``..`` collapses to ``""`` (skipped).
    """
    return _SLUG_RE.sub("-", (name or "").strip()).strip("-.")


def _within_any(path: Path, roots: list[str]) -> bool:
    """True if ``path`` is one of ``roots`` or nested under one (workspace allowlist, §6.6)."""
    try:
        p = path.resolve(strict=False)
    except (OSError, ValueError):
        return False
    for r in roots:
        try:
            rp = Path(r).resolve(strict=False)
        except (OSError, ValueError):
            continue
        if p == rp or p.is_relative_to(rp):
            return True
    return False


def _guidance_files_for(agents) -> list[str]:
    """Resolve the guidance filenames to write for the given agent type(s)."""
    if not agents:
        return list(_DEFAULT_GUIDANCE_FILES)
    if isinstance(agents, str):
        agents = [agents]
    out: list[str] = []
    for a in agents:
        fn = AGENT_GUIDANCE_FILES.get(str(a).strip().lower())
        if fn and fn not in out:
            out.append(fn)
    return out or list(_DEFAULT_GUIDANCE_FILES)


class WorkspaceInjector:
    """Materialize a step's injection material into the workspace, and revert it again (§11.2 D).

    ``allowed_roots`` (optional) gates which workspaces may be written to — defense in depth on top of
    the dispatch-layer allowlist that already vets the path.
    """

    def __init__(self, *, allowed_roots: list[str] | None = None) -> None:
        self.allowed_roots = list(allowed_roots) if allowed_roots else None

    # ── inject (事前注入) ─────────────────────────────────────────────────────────────────────────
    def inject(self, workspace: str, material: dict, *, agents=None) -> dict:
        """Write this step's guidance into ``workspace`` before the agent starts.

        ``material`` is a step view (T5.2 ``_resolve_material``): ``instruction`` plus ``skills`` and
        ``standards`` lists of ``{name, body}``. Writes ``CLAUDE.md`` / ``AGENTS.md`` (managed block:
        instruction + standards + skill links) and each skill to ``.foreman/skills/<slug>.md``.

        Returns ``{ok, files: [...written paths...], skills: [...], skipped: [...]}`` or
        ``{ok: False, error}`` (error ∈ {no_workspace, workspace_not_allowed}).
        """
        ws_str = (workspace or "").strip()
        if not ws_str:
            return {"ok": False, "error": "no_workspace"}
        ws = Path(ws_str)
        if self.allowed_roots is not None and not _within_any(ws, self.allowed_roots):
            return {"ok": False, "error": "workspace_not_allowed"}
        ws.mkdir(parents=True, exist_ok=True)

        skills = list(material.get("skills") or [])
        standards = list(material.get("standards") or [])
        instruction = str(material.get("instruction") or "").strip()

        skill_files, skipped = self._write_skills(ws, skills)
        block = _build_block(instruction, standards, skill_files)

        written: list[str] = [str((ws / SKILLS_DIR / f).resolve(strict=False)) for f in skill_files]
        for fname in _guidance_files_for(agents):
            target = ws / fname
            _upsert_block(target, block)
            written.append(str(target.resolve(strict=False)))
        return {"ok": True, "files": written, "skills": skill_files, "skipped": skipped}

    def _write_skills(self, ws: Path, skills: list[dict]) -> tuple[list[str], list[str]]:
        """Write each skill body to ``.foreman/skills/<slug>.md``; return (filenames, skipped names)."""
        if not skills:
            return [], []
        skills_dir = ws / SKILLS_DIR
        skills_dir.mkdir(parents=True, exist_ok=True)
        files: list[str] = []
        skipped: list[str] = []
        for s in skills:
            name = str(s.get("name") or "")
            slug = _slug(name)
            if not slug:  # a name that sanitizes to nothing (e.g. "..") is reported, never written.
                skipped.append(name)
                continue
            fname = f"{slug}.md"
            body = str(s.get("body") or "")
            header = f"# {name}\n\n" if name else ""
            (skills_dir / fname).write_text(header + body + "\n", encoding="utf-8")
            files.append(fname)
        return files, skipped

    # ── clear (恢复工作区) ────────────────────────────────────────────────────────────────────────
    def clear(self, workspace: str, *, agents=None) -> dict:
        """Revert an injection: strip the managed block from guidance files + drop ``.foreman/skills``.

        A guidance file that holds nothing but our block (i.e. we created it) is deleted; one with
        user content keeps that content. Idempotent — clearing an un-injected workspace is a no-op.
        """
        ws_str = (workspace or "").strip()
        if not ws_str:
            return {"ok": False, "error": "no_workspace"}
        ws = Path(ws_str)
        removed: list[str] = []
        # Clear from both default files plus any agent-specific ones, so a clear is thorough.
        names = set(_DEFAULT_GUIDANCE_FILES) | set(_guidance_files_for(agents))
        for fname in names:
            target = ws / fname
            if _strip_block(target):
                removed.append(str(target.resolve(strict=False)))
        skills_dir = ws / SKILLS_DIR
        if skills_dir.exists():
            # Fixed relative path inside the workspace — never the workspace root. rmtree won't follow
            # a symlinked top-level target; a vetted workspace is assumed (caller checks the allowlist).
            shutil.rmtree(skills_dir, ignore_errors=True)
            if not skills_dir.exists():  # only report what actually got removed
                removed.append(str(skills_dir.resolve(strict=False)))
            parent = ws / ".foreman"
            try:  # remove the .foreman namespace too, but only if we left it empty.
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
        return {"ok": True, "removed": removed}


# ── block assembly + file upsert (pure helpers) ────────────────────────────────────────────────────
def _build_block(instruction: str, standards: list[dict], skill_files: list[str]) -> str:
    """The Markdown body that goes inside the managed block of CLAUDE.md / AGENTS.md."""
    parts: list[str] = ["> 本段由 Foreman 自动生成（每步重写）。请遵守以下规范与做法。"]
    if instruction:
        parts.append(f"## 本步任务\n{instruction}")
    for st in standards:
        name = str(st.get("name") or "")
        body = str(st.get("body") or "")
        parts.append(f"## 代码规范：{name}\n{body}" if name else f"## 代码规范\n{body}")
    if skill_files:
        links = "\n".join(f"- [{f}]({SKILLS_DIR}/{f})" for f in skill_files)
        parts.append(f"## 本步可用技能（详见文件）\n{links}")
    return "\n\n".join(parts)


def _block_span(existing: str) -> tuple[int, int] | None:
    """The (start, end) char span of a managed block: first BEGIN → end of the *last* END.

    Using first-BEGIN/last-END (not paired index()) makes upsert/strip self-healing: any duplicated or
    out-of-order markers from a crashed half-write are collapsed into the single new block / removed
    wholesale, rather than desyncing and leaking scaffolding. ``None`` if there is no valid block.
    """
    b = existing.find(MARKER_BEGIN)
    e = existing.rfind(MARKER_END)
    if b == -1 or e == -1 or e < b:
        return None
    return b, e + len(MARKER_END)


def _upsert_block(path: Path, block: str) -> None:
    """Write ``block`` into ``path`` between the markers, preserving everything outside them."""
    new_block = f"{MARKER_BEGIN}\n{block}\n{MARKER_END}"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    span = _block_span(existing)
    if span is not None:
        content = existing[: span[0]] + new_block + existing[span[1] :]
    elif existing.strip():
        content = existing.rstrip("\n") + "\n\n" + new_block + "\n"
    else:
        content = new_block + "\n"
    path.write_text(content, encoding="utf-8")


def _strip_block(path: Path) -> bool:
    """Remove the managed block from ``path``; delete the file if nothing else remains. True if changed."""
    if not path.exists():
        return False
    existing = path.read_text(encoding="utf-8")
    span = _block_span(existing)
    if span is None:
        return False
    remainder = (existing[: span[0]] + existing[span[1] :]).strip()
    if remainder:
        path.write_text(remainder + "\n", encoding="utf-8")
    else:  # the file held only our block (we created it) — remove it entirely.
        path.unlink()
    return True


__all__ = [
    "WorkspaceInjector",
    "MARKER_BEGIN",
    "MARKER_END",
    "SKILLS_DIR",
    "AGENT_GUIDANCE_FILES",
]
