"""Briefing — the human-readable status report pushed to the phone (DESIGN §4.1 / §5.5).

A briefing condenses an agent session's recent activity into "what got done, where it's stuck,
any risks, suggested next step" — generated with YOUR LLM (config.llm + .env), stored in the
``reports`` table, and (best-effort) Web-Pushed to the phone. Kinds: ``handoff`` / ``active-briefing``
(you're back at the desk) / ``daily`` (DESIGN §7.1).

Like the Reviewer/Operator this is an LLM-text component: a language-neutral prompt skeleton suffixed
with ``language_directive`` (§15) so the human-facing text comes back in the chosen language, plus a
conservative parser. It is **client-side core**, INJECTED into ``server.app.create_app`` as
``briefings`` so app.py stays shared-only and session content never leaves the local process
(DESIGN §8.3 / §14). The LLM is reached via ``LLMClient`` (mockable transport — no network/tokens in
tests); live use needs the user's own key, deferred behind config like the other LLM roles.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from foreman.shared.events import make_event, utc_now_iso
from foreman.shared.i18n import language_directive
from foreman.shared.llm import LLMClient, Message

from ..store.models import Report

VALID_KINDS: frozenset[str] = frozenset({"handoff", "active-briefing", "daily"})
DEFAULT_KIND = "active-briefing"

BRIEF_SYSTEM = (
    "You are the Foreman PM writing a short status briefing for the human about an AI coding "
    "agent's work. You are given the session goal (or a roster of sessions) and a condensed "
    "activity log of recent events. Write an honest, concise report: what got done, where it is "
    "stuck or blocked, any risks, and a suggested next step. Be factual — do NOT invent progress "
    "that is not in the log. Respond with ONLY a JSON object: "
    '{"title": str, "body_md": str}. body_md is Markdown.'
)

# Keep the activity log bounded so a chatty session can't blow the token budget.
DEFAULT_MAX_ACTIVITY_CHARS = 12000
DEFAULT_MAX_EVENTS = 60


@dataclass
class BriefingResult:
    title: str = ""
    body_md: str = ""


def _as_str(value: object) -> str:
    return "" if value is None else str(value).strip()


def _extract_json_object(raw: str) -> dict | None:
    """Pull the first JSON object out of an LLM reply (handles ```json fences / surrounding prose)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if "```" in text:
            text = text[: text.rfind("```")]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def parse_brief(raw: str) -> BriefingResult:
    """Parse an LLM reply into a ``BriefingResult``; degrade gracefully on unparseable output.

    A briefing is informational (not a safety gate), so unlike the Auditor/Reviewer there is no
    fail-closed verdict — if the reply isn't JSON we still surface the raw text (capped) so the
    human sees *something* rather than an empty card.
    """
    obj = _extract_json_object(raw)
    if obj is None:
        text = (raw or "").strip()
        return BriefingResult(title="简报", body_md=text[:4000] or "（简报输出为空）")
    title = _as_str(obj.get("title")) or "简报"
    body = _as_str(obj.get("body_md")) or _as_str(obj.get("body"))
    return BriefingResult(title=title, body_md=body)


def build_brief_prompt(goal: str, activity: str, *, kind: str = DEFAULT_KIND,
                       max_activity_chars: int = DEFAULT_MAX_ACTIVITY_CHARS) -> str:
    """Assemble the user prompt; keep the *tail* of an over-long activity log (most recent first)."""
    body = activity or "（无活动记录）"
    if len(body) > max_activity_chars:
        body = "…[older activity truncated]…\n" + body[-max_activity_chars:]
    return "\n\n".join([f"# Briefing kind\n{kind}", f"# Goal\n{goal}", f"# Recent activity\n{body}"])


def _report_to_dict(r: Report) -> dict:
    return {
        "id": r.id,
        "session_id": r.session_id,
        "kind": r.kind,
        "title": r.title,
        "body_md": r.body_md,
        "sent": r.sent,
        "ts": r.ts,
    }


class BriefingService:
    """Generate + store + push briefings, and list them. ``language`` drives output (§15)."""

    def __init__(
        self,
        llm: LLMClient | None,
        store: object,
        *,
        bus: object | None = None,
        pusher: object | None = None,
        language: str = "zh",
        clock=None,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        self.llm = llm
        self.store = store
        self.bus = bus
        self.pusher = pusher
        self.language = language
        self._clock = clock or utc_now_iso
        self.max_events = max_events

    async def generate(self, session_id: str | None = None, kind: str = DEFAULT_KIND) -> dict:
        """Generate a briefing for one session (or all, when session_id is None) and store it.

        Returns ``{"ok": True, "report": {...}}`` or ``{"ok": False, "error": ...}`` with error ∈
        {no_store, no_llm}. Pushing the briefing to the phone is best-effort (a disabled/absent
        Pusher or no subscriptions is a no-op; GONE endpoints are pruned)."""
        if self.store is None or not hasattr(self.store, "add_report"):
            return {"ok": False, "error": "no_store"}
        if self.llm is None:
            return {"ok": False, "error": "no_llm"}
        kind = kind if kind in VALID_KINDS else DEFAULT_KIND
        goal, activity = self._gather_activity(session_id)
        result = await self._run_llm(goal, activity, kind)
        sent = await self._push(result)
        report = Report(
            id=uuid.uuid4().hex,
            session_id=session_id,
            kind=kind,
            title=result.title,
            body_md=result.body_md,
            sent=sent,
            ts=self._clock(),
        )
        self.store.add_report(report)
        await self._emit_briefing(report)
        return {"ok": True, "report": _report_to_dict(report)}

    def list_reports(self, session_id: str | None = None) -> list[dict]:
        """Stored briefings as JSON-friendly dicts (newest first). Caller: server app.py (§14)."""
        if self.store is None or not hasattr(self.store, "get_reports"):
            return []
        return [_report_to_dict(r) for r in self.store.get_reports(session_id)]

    # ── internals ─────────────────────────────────────────────────────────────────────────────
    async def _run_llm(self, goal: str, activity: str, kind: str) -> BriefingResult:
        """Call the LLM; a transient failure degrades to a note rather than 500'ing the request."""
        system = BRIEF_SYSTEM + "\n" + language_directive(self.language)
        prompt = build_brief_prompt(goal, activity, kind=kind)
        try:
            raw = await self.llm.complete(
                [Message("system", system), Message("user", prompt)], json_mode=True
            )
        except Exception as exc:  # noqa: BLE001 — a phone "generate" tap shouldn't error out
            return BriefingResult(title="简报", body_md=f"（简报生成失败：{type(exc).__name__}）")
        return parse_brief(raw)

    def _gather_activity(self, session_id: str | None) -> tuple[str, str]:
        """Collect (goal, activity_text) for the briefing — one session or a roster of all of them."""
        if session_id is not None:
            return self._gather_one(session_id)
        return self._gather_all()

    def _gather_one(self, session_id: str) -> tuple[str, str]:
        session = (
            self.store.get_session(session_id) if hasattr(self.store, "get_session") else None
        )
        goal = getattr(session, "goal", "") if session else ""
        lines: list[str] = []
        if hasattr(self.store, "get_events"):
            events = self.store.get_events(session_id)[-self.max_events :]
            for e in events:
                payload = json.loads(getattr(e, "payload_json", "") or "{}")
                snippet = json.dumps(payload, ensure_ascii=False)[:200]
                lines.append(f"[{e.ts or ''}] {e.type} ({getattr(e, 'source', '')}): {snippet}")
        return goal, "\n".join(lines)

    def _gather_all(self) -> tuple[str, str]:
        """A roster line per session (goal, status, event count, last event) for a daily briefing."""
        lines: list[str] = []
        has_events = hasattr(self.store, "get_events")
        if hasattr(self.store, "get_sessions"):
            for s in self.store.get_sessions():
                events = self.store.get_events(s.id) if has_events else []
                last = events[-1].type if events else ""
                lines.append(
                    f"- {s.goal or s.id} [{s.status}] · {len(events)} events · last: {last or '—'}"
                )
        return "全部活动会话", "\n".join(lines)

    async def _push(self, result: BriefingResult) -> bool:
        """Best-effort Web Push of the briefing to every subscribed browser (§4.6). True if sent."""
        if self.pusher is None or not hasattr(self.store, "get_push_subscriptions"):
            return False
        subs = self.store.get_push_subscriptions()
        if not subs:
            return False
        # send_to_all returns the GONE endpoints; if any wasn't gone we count it delivered.
        gone = await self.pusher.send_to_all(
            subs, result.title or "🦺 Foreman 简报", (result.body_md or "")[:200],
            {"url": "/", "tag": "briefing"},
        )
        gone_set = set(gone or [])
        for endpoint in gone_set:
            if hasattr(self.store, "delete_push_subscription"):
                self.store.delete_push_subscription(endpoint)
        # Delivered iff at least one subscription's endpoint wasn't reported gone (identity-based,
        # robust to dup/extra entries in the gone list — not a fragile length comparison).
        return any(getattr(s, "endpoint", "") not in gone_set for s in subs)

    async def _emit_briefing(self, report: Report) -> None:
        """Record + publish a `briefing` event (persist-first, mirrors Runner/Gate)."""
        event = make_event(
            "briefing",
            "brain",
            report.session_id or "",
            payload={"report_id": report.id, "kind": report.kind, "title": report.title},
        )
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)


__all__ = [
    "BriefingService",
    "BriefingResult",
    "parse_brief",
    "build_brief_prompt",
    "VALID_KINDS",
    "DEFAULT_KIND",
    "BRIEF_SYSTEM",
]
