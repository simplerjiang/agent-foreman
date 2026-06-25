"""Build display-safe snapshots for the subscription-driven relay view.

In protocol v2 the server no longer stores a display cache. A subscribed PWA asks the local
process for a one-shot snapshot, then follows live relay events while the browser is online.

The whole point of the snapshot boundary (§8.3) is that only DISPLAY summaries leave the machine
— never full diffs, raw agent output, or 秘方. These builders are pure (take ORM rows, return
plain dicts) so what's shared is explicit and unit-testable: a session's goal/status/timestamps,
and a card's folded summary + the `diff_stat` *line* ("3 files +124/−80") — never the diff itself.
The full diff / raw return stays on the machine.
"""

from __future__ import annotations

import json

from foreman.shared.protocol import KIND_CACHE_SYNC, KIND_SNAPSHOT, Envelope


def session_summary(session) -> dict:
    """A display-safe summary of one local session (no diffs/raw output — §8.3)."""
    return {
        "session_id": session.id,
        "summary": {
            "goal": session.goal,
            "status": session.status,
            "agent_type": session.agent_type,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        },
    }


def card_summary(card) -> dict:
    """A display-safe summary of one decision card: the folded text + the `diff_stat` LINE only
    (the full diff/raw return stays on the machine and is fetched live — §6.3/§8.3)."""
    try:
        options = json.loads(card.options_json or "[]")
    except (TypeError, ValueError):
        options = []
    return {
        "card_id": card.id,
        "status": "decided" if card.chosen else "pending",
        "payload": {
            "session_id": card.session_id,
            "summary": card.summary,
            "audit_note": card.audit_note,
            "diff_stat": card.diff_stat,
            "options": options,
            "chosen": card.chosen,
            "decided_at": card.decided_at,
            "ts": card.ts,
        },
    }


def build_cache_sync(sessions, cards) -> Envelope:
    """Legacy v1 helper. Retained for compatibility; v2 uses ``build_snapshot`` on demand."""
    return Envelope(
        kind=KIND_CACHE_SYNC,
        payload={
            "sessions": [session_summary(s) for s in sessions],
            "cards": [card_summary(c) for c in cards],
        },
    )


def build_snapshot(sessions, cards, *, corr_id: str = "") -> Envelope:
    """Assemble an on-demand display-safe snapshot for a subscribed browser."""
    env = build_cache_sync(sessions, cards)
    env.kind = KIND_SNAPSHOT
    env.id = corr_id
    return env
