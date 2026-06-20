"""Decision Card + step-detail drill-down (DESIGN §6.3).

A **Decision Card** is the folded view you act on: one-line summary + the Auditor's note + a
``📎 changes`` stat + 2–4 one-tap options. You normally only read the summary and tap a button.
But the fold is just a fold — ``[🔍 查看详情]`` drills into the **step detail page** with two tabs:

  - **① 原始返回**: what codex/claude actually said this step — the raw ``agent_output`` /
    ``tool_pre`` / ``tool_post`` / ``stop`` events, reconstructed from the ``events`` table.
  - **② 代码改动**: exactly which files/lines changed — the per-file, per-line unified diff from
    this step's checkpoint (CheckpointManager.diff, T2.7) to the live worktree, with line tags.

The card is the *summary*; the detail page is the *raw evidence* — never one-or-the-other (§6.3).

This is client-side core (it reaches the local Store + CheckpointManager). It is INJECTED into
``server.app.create_app`` as ``cards`` (like ``gate``) so app.py stays shared-only and the diff /
raw output never leave the local process (DESIGN §8.3 / §14). The checkpoint manager is built
through an injectable factory so the diff path is unit-testable without a real git workspace.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from foreman.shared.events import utc_now_iso

from ..store.models import DecisionCard

# Raw-return event types reconstructed for tab ① — what the agent actually said this step (§6.3①).
RAW_EVENT_TYPES: frozenset[str] = frozenset(
    {"agent_output", "tool_pre", "tool_post", "stop", "notification"}
)

# The standard one-tap options on a decision card (§6.3 mock). The human picks — so a full card
# always offers approve / revise / undo / a manual escape hatch, regardless of the verdict.
DEFAULT_OPTIONS: list[dict] = [
    {"action": "approve", "label": "✅ 通过"},
    {"action": "revise", "label": "🔄 让它补"},
    {"action": "undo", "label": "⛔ 撤掉重来"},
    {"action": "manual", "label": "✍️ 我自己打一条复杂指令…"},
]


@dataclass
class DiffLine:
    """One line of a unified diff, tagged for per-line highlighting (§6.3②)."""

    kind: str            # add | del | context | meta
    text: str
    old_n: int | None = None  # line number on the old side (None for additions/meta)
    new_n: int | None = None  # line number on the new side (None for deletions/meta)


@dataclass
class DiffFile:
    path: str
    old_path: str = ""
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    lines: list[DiffLine] = field(default_factory=list)


def _strip_ab(path: str) -> str:
    """Drop git's a/ or b/ prefix from a diff path ('/dev/null' left as-is)."""
    if path in ("a", "b"):
        return ""
    for pre in ("a/", "b/"):
        if path.startswith(pre):
            return path[len(pre):]
    return path


def parse_unified_diff(text: str) -> list[DiffFile]:
    """Parse ``git diff`` output into per-file, per-line structure with line numbers (§6.3②).

    Pure function (no git): drives both the API payload and the unit tests. Handles multi-file
    diffs, new/deleted files (``/dev/null`` sides), renames, and binary files. Counts +/- lines.
    """
    files: list[DiffFile] = []
    cur: DiffFile | None = None
    old_n = new_n = 0
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            cur = DiffFile(path="")
            files.append(cur)
            old_n = new_n = 0
            # Best-effort path from the header; refined by the +++/--- lines below.
            parts = line.split(" ")
            if len(parts) >= 4:
                cur.path = _strip_ab(parts[3]) or _strip_ab(parts[2])
            continue
        if cur is None:
            continue  # ignore any preamble before the first file header
        if line.startswith("--- "):
            cur.old_path = _strip_ab(line[4:].strip())
            continue
        if line.startswith("+++ "):
            new_path = _strip_ab(line[4:].strip())
            if new_path:
                cur.path = new_path  # the new-side path is the file's canonical name
            continue
        if line.startswith("Binary files"):
            cur.binary = True
            cur.lines.append(DiffLine(kind="meta", text=line))
            continue
        if line.startswith("@@"):
            old_n, new_n = _hunk_starts(line)
            cur.lines.append(DiffLine(kind="meta", text=line))
            continue
        if line.startswith("+"):
            cur.additions += 1
            cur.lines.append(DiffLine(kind="add", text=line[1:], new_n=new_n))
            new_n += 1
        elif line.startswith("-"):
            cur.deletions += 1
            cur.lines.append(DiffLine(kind="del", text=line[1:], old_n=old_n))
            old_n += 1
        elif line.startswith("\\"):  # "\ No newline at end of file"
            cur.lines.append(DiffLine(kind="meta", text=line))
        elif line.startswith(" "):
            cur.lines.append(DiffLine(kind="context", text=line[1:], old_n=old_n, new_n=new_n))
            old_n += 1
            new_n += 1
        else:
            # index/mode/similarity lines, blank junk between files, etc. — metadata, don't
            # miscount. (git always prefixes a real context line with a space, so a bare "" is
            # never an in-hunk context line and must not advance the line counters.)
            cur.lines.append(DiffLine(kind="meta", text=line))
    return files


def _hunk_starts(header: str) -> tuple[int, int]:
    """Parse '@@ -a,b +c,d @@' → (old_start, new_start); defaults to (1,1) on a malformed header."""
    old_start = new_start = 1
    try:
        mid = header.split("@@")[1].strip()  # "-a,b +c,d"
        for tok in mid.split():
            if tok.startswith("-"):
                old_start = int(tok[1:].split(",")[0])
            elif tok.startswith("+"):
                new_start = int(tok[1:].split(",")[0])
    except (IndexError, ValueError):
        pass
    return old_start, new_start


def diff_summary(files: list[DiffFile]) -> dict:
    """Roll a parsed diff up into the card's ``📎 changes`` stat: file count + total +/-."""
    return {
        "files": len(files),
        "additions": sum(f.additions for f in files),
        "deletions": sum(f.deletions for f in files),
    }


def format_diff_stat(summary: dict) -> str:
    """The one-line ``📎`` stat text, e.g. '3 个文件 +124 / −80'."""
    return (
        f"{summary['files']} 个文件 "
        f"+{summary['additions']} / −{summary['deletions']}"
    )


def _file_to_dict(f: DiffFile) -> dict:
    return {
        "path": f.path,
        "old_path": f.old_path,
        "additions": f.additions,
        "deletions": f.deletions,
        "binary": f.binary,
        "lines": [
            {"kind": ln.kind, "text": ln.text, "old_n": ln.old_n, "new_n": ln.new_n}
            for ln in f.lines
        ],
    }


def _card_to_dict(c: DecisionCard) -> dict:
    import json
    return {
        "id": c.id,
        "action_id": c.action_id,
        "session_id": c.session_id,
        "summary": c.summary,
        "audit_note": c.audit_note,
        "diff_stat": c.diff_stat,
        "options": json.loads(c.options_json or "[]"),
        "chosen": c.chosen,
        "ts": c.ts,
    }


class CardService:
    """Builds/lists decision cards and assembles the step-detail drill-down (§6.3).

    ``store`` is the local client Store. ``checkpoint_factory(workspace)`` returns something with a
    ``.diff(from_ref)`` method (default: a real CheckpointManager) — injected so the diff path can
    be unit-tested without a git workspace, and so this module never hard-imports a heavy dep path.
    """

    # The options a card offers — the human taps exactly one (§6.3). Validated server-side so a
    # crafted request can't record a bogus decision.
    VALID_OPTIONS: frozenset[str] = frozenset({"approve", "revise", "undo", "manual"})

    def __init__(
        self, store: object, *, bus: object | None = None, checkpoint_factory=None, clock=None
    ) -> None:
        self.store = store
        self.bus = bus
        self._ckpt_factory = checkpoint_factory or _default_checkpoint_factory
        self._clock = clock or utc_now_iso

    def build_card(
        self,
        *,
        action_id: str,
        session_id: str,
        summary: str,
        audit_note: str = "",
        diff_stat: str = "",
        options: list[dict] | None = None,
    ) -> dict:
        """Persist a decision card (the folded summary + Auditor note + one-tap options, §6.3)."""
        import json
        card = DecisionCard(
            id=uuid.uuid4().hex,
            action_id=action_id,
            session_id=session_id,
            summary=summary,
            audit_note=audit_note,
            diff_stat=diff_stat,
            options_json=json.dumps(options if options is not None else DEFAULT_OPTIONS),
            ts=self._clock(),
        )
        if self.store is not None and hasattr(self.store, "add_decision_card"):
            self.store.add_decision_card(card)
        return _card_to_dict(card)

    def list_cards(self, session_id: str | None = None) -> list[dict]:
        """Decision cards as JSON-friendly dicts (newest first). Caller: server app.py (shared-only)."""
        if self.store is None or not hasattr(self.store, "get_decision_cards"):
            return []
        return [_card_to_dict(c) for c in self.store.get_decision_cards(session_id)]

    async def record_choice(self, card_id: str, option: str) -> dict:
        """Record the human's one-tap decision on a card and emit a `card_decided` event (§6.3).

        Returns {"ok": True, "id", "chosen"} or {"ok": False, "error": ...} with error ∈
        {bad_option, no_store, not_found}. Like the Gate (T3.4), this records the decision; actually
        *executing* the chosen path (run / nudge the agent / one-click undo) is the two-way control
        layer (P4 — Runner.send/interrupt), so the event carries ``execution_deferred=True``.
        """
        if option not in self.VALID_OPTIONS:
            return {"ok": False, "error": "bad_option"}
        if self.store is None or not hasattr(self.store, "set_card_choice"):
            return {"ok": False, "error": "no_store"}
        row = self.store.set_card_choice(card_id, chosen=option, decided_at=self._clock())
        if row is None:
            return {"ok": False, "error": "not_found"}
        await self._emit_decided(row)
        return {"ok": True, "id": card_id, "chosen": option}

    async def _emit_decided(self, card) -> None:
        """Record + publish the card decision (persist-first, mirrors Gate/Runner)."""
        from foreman.shared.events import make_event

        event = make_event(
            "card_decided",
            "cards",
            card.session_id,
            payload={
                "card_id": card.id,
                "action_id": card.action_id,
                "chosen": card.chosen,
                # executing the chosen path is the two-way control layer (P4, Runner.send/interrupt).
                "execution_deferred": True,
            },
        )
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)

    def step_detail(self, action_id: str) -> dict | None:
        """Assemble the two-tab step detail for an action: raw return + per-line diff (§6.3).

        Returns None when the action is unknown (the route maps that to 404). Otherwise:
        ``{action_id, raw: [events…], diff: {files: [...], summary: {...}}}``.
        """
        if self.store is None or not hasattr(self.store, "get_action"):
            return None
        action = self.store.get_action(action_id)
        if action is None:
            return None
        return {
            "action_id": action_id,
            "session_id": action.session_id,
            "command": action.command,
            "raw": self._raw_return(action),
            "diff": self._code_changes(action),
        }

    # ── tab ① raw return ─────────────────────────────────────────────────────────────────────
    def _raw_return(self, action) -> list[dict]:
        """The agent's raw events for this step (§6.3①), scoped by the step's checkpoint window.

        A "step" is bracketed by its checkpoint and the next one (checkpoints are taken per card,
        §6.5). We return the session's raw-type events whose timestamp falls in that window; with
        no checkpoint we fall back to all raw-type events for the session.
        """
        if not hasattr(self.store, "get_events"):
            return []
        start, end = self._step_window(action)
        out: list[dict] = []
        for e in self.store.get_events(action.session_id):
            if e.type not in RAW_EVENT_TYPES:
                continue
            ts = e.ts or ""
            if start is not None and ts < start:
                continue
            if end is not None and ts >= end:
                continue
            out.append(_event_row_to_dict(e))
        return out

    def _step_window(self, action) -> tuple[str | None, str | None]:
        """[start, end) timestamps bracketing the action's step, derived from the checkpoint chain."""
        ckpt_id = getattr(action, "checkpoint_id", None)
        if not ckpt_id or not hasattr(self.store, "get_checkpoint"):
            return None, None
        ckpt = self.store.get_checkpoint(ckpt_id)
        if ckpt is None:
            return None, None
        start = ckpt.created_at or None
        end = None
        if hasattr(self.store, "get_checkpoints"):
            # The next checkpoint (by step index) ends this step's window.
            later = [
                c.created_at
                for c in self.store.get_checkpoints(action.session_id)
                if c.step_index > ckpt.step_index and c.created_at
            ]
            if later:
                end = min(later)
        return start, end

    # ── tab ② code changes ───────────────────────────────────────────────────────────────────
    def _code_changes(self, action) -> dict:
        """The per-file, per-line diff from this step's checkpoint to the live worktree (§6.3②)."""
        empty = {"files": [], "summary": diff_summary([])}
        ckpt_id = getattr(action, "checkpoint_id", None)
        if not ckpt_id or not hasattr(self.store, "get_checkpoint"):
            return {**empty, "note": "no checkpoint for this step"}
        ckpt = self.store.get_checkpoint(ckpt_id)
        if ckpt is None or not ckpt.vcs_ref:
            return {**empty, "note": "no checkpoint for this step"}
        session = self.store.get_session(action.session_id) if hasattr(
            self.store, "get_session"
        ) else None
        workspace = getattr(session, "workspace", "") if session else ""
        if not workspace:
            return {**empty, "note": "session workspace unknown"}
        try:
            mgr = self._ckpt_factory(workspace)
            raw = mgr.diff(ckpt.vcs_ref)
        except Exception as exc:  # a missing ref / not-a-repo shouldn't 500 the detail page.
            return {**empty, "note": f"diff unavailable: {type(exc).__name__}"}
        files = parse_unified_diff(raw)
        return {"files": [_file_to_dict(f) for f in files], "summary": diff_summary(files)}


def _event_row_to_dict(row) -> dict:
    import json
    return {
        "id": getattr(row, "id", None),
        "type": row.type,
        "source": getattr(row, "source", ""),
        "payload": json.loads(getattr(row, "payload_json", "") or "{}"),
        "ts": getattr(row, "ts", ""),
    }


def _default_checkpoint_factory(workspace: str):
    from .checkpoint import CheckpointManager
    return CheckpointManager(Path(workspace))


__all__ = [
    "CardService",
    "DiffFile",
    "DiffLine",
    "parse_unified_diff",
    "diff_summary",
    "format_diff_stat",
    "DEFAULT_OPTIONS",
    "RAW_EVENT_TYPES",
]
