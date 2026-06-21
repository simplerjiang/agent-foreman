"""Build the display-cache snapshot a local process pushes to the relay (DESIGN §8.5 ③, T7.5).

When connected, the local process periodically sends the server a *read-only copy* of its
session summaries + decision cards, so the PWA can still view them while the PC is offline.

The whole point of the cache boundary (§8.3) is that only DISPLAY summaries leave the machine —
never full diffs, raw agent output, or 秘方. These builders are pure (take ORM rows, return
plain dicts) so what's shared is explicit and unit-testable: a session's goal/status/timestamps,
and a card's folded summary + the `diff_stat` *line* ("3 files +124/−80") — never the diff itself.
The full diff / raw return is fetched live from the machine when it's online (§8.5 ④).

The periodic push loop that calls `build_cache_sync` and sends the frame over an open
RelayConnector is the credential-gated team rollout (a deployed relay + a real access key) — see
TASKS T7.1; this module supplies the snapshot it will carry.
"""

from __future__ import annotations

import json

from foreman.shared.protocol import KIND_CACHE_SYNC, Envelope


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
    """Assemble the `cache_sync` frame from local session + card rows (§8.5 ③). The relay scopes
    it to the authenticated account — this payload carries NO account id (it would be ignored)."""
    return Envelope(
        kind=KIND_CACHE_SYNC,
        payload={
            "sessions": [session_summary(s) for s in sessions],
            "cards": [card_summary(c) for c in cards],
        },
    )
