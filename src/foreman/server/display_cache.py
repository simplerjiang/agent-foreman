"""Display cache service (team/relay mode) — DESIGN §8.5 ③ / §7.2.

The relay (总机) keeps a *read-only copy* of each account's session summaries + decision cards
so the PWA can still view them while the user's local process (the source of truth) is OFFLINE.
A local process pushes a snapshot up the relay link (a `cache_sync` frame); this service stores
it; the PWA reads it back through the account-scoped REST endpoints.

Boundary (DESIGN §8.3/§8.4/§14): the cache holds ONLY display summaries — never full diffs, raw
agent output, LLM keys, or 秘方 (those stay on each user's machine and are fetched live when the
PC is online). Every read/write is scoped by `account_id`, which the relay derives from the
authenticated access key — NEVER from anything the client puts in the payload — so one tenant can
never read or overwrite another's cache. This module imports only the server store + shared, so
app.py can inject it the same way it injects the Gate/Relay/AuthManager.
"""

from __future__ import annotations

import json
import uuid

from foreman.shared.events import utc_now_iso

from .store.models import CacheCard, CacheSession

# Bounds so a buggy/hostile local process can't blow up the relay box (fail-closed, like the
# definition service). The cache is for short display summaries, so these are generous ceilings.
MAX_ITEMS = 1000            # per kind, per sync — extra items are dropped (logged via the count)
MAX_SUMMARY_BYTES = 64 * 1024  # one session summary / card payload, serialized


def _items(value: object) -> list:
    """The first MAX_ITEMS of a sync list, or [] for anything that isn't a list. Fail-closed:
    a non-list `sessions`/`cards` (a JSON object, number, etc. from a buggy/hostile process) is
    treated as 'nothing to sync' rather than crashing — slicing a dict/int would raise (§8.4)."""
    if not isinstance(value, list):
        return []
    return value[:MAX_ITEMS]


def _clip_json(obj: object, limit: int) -> str | None:
    """Serialize a display object to JSON, or None if it's not a dict or exceeds `limit` bytes
    (oversized → dropped, not truncated, so a half-cut payload can never be served)."""
    if not isinstance(obj, dict):
        return None
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(text.encode("utf-8")) > limit:
        return None
    return text


class DisplayCacheService:
    """Account-scoped read/write of the relay's display cache (cache_sessions / cache_cards).

    `store` is a ServerStore. Time / id generators are injectable for deterministic tests.
    """

    def __init__(self, store, *, now=utc_now_iso, gen_id=lambda: uuid.uuid4().hex) -> None:
        self.store = store
        self._now = now
        self._gen_id = gen_id

    # ── write: a local process pushed its current display snapshot (§8.5 ③) ───────────────────
    def sync(
        self,
        account_id: str,
        sessions: object = None,
        cards: object = None,
    ) -> dict:
        """Upsert a snapshot of one account's session summaries + decision cards.

        Items are display-safe shapes the local process built:
          session = {"session_id": str, "summary": {...}}
          card    = {"card_id": str, "status": str, "payload": {...}}

        Malformed items (missing id, non-dict summary/payload, oversized) are SKIPPED rather than
        raising, so one bad row never drops the whole sync. `account_id` is the authenticated
        account (the relay derives it from the key — §8.4); it is the ONLY scope used, never any
        account id inside the payload. Returns counts of what was stored vs skipped.
        """
        if not account_id:
            return {"ok": False, "error": "no_account"}
        now = self._now()
        stored_sessions = self._sync_sessions(account_id, sessions, now)
        stored_cards = self._sync_cards(account_id, cards, now)
        return {
            "ok": True,
            "sessions": stored_sessions,
            "cards": stored_cards,
        }

    def _sync_sessions(self, account_id: str, sessions: object, now: str) -> int:
        stored = 0
        for item in _items(sessions):
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or "").strip()
            if not session_id:
                continue
            summary_json = _clip_json(item.get("summary", {}), MAX_SUMMARY_BYTES)
            if summary_json is None:
                continue
            self.store.upsert_cache_session(
                CacheSession(
                    id=self._gen_id(),
                    account_id=account_id,
                    session_id=session_id,
                    summary_json=summary_json,
                    updated_at=now,
                )
            )
            stored += 1
        return stored

    def _sync_cards(self, account_id: str, cards: object, now: str) -> int:
        stored = 0
        for item in _items(cards):
            if not isinstance(item, dict):
                continue
            card_id = str(item.get("card_id") or "").strip()
            if not card_id:
                continue
            payload_json = _clip_json(item.get("payload", {}), MAX_SUMMARY_BYTES)
            if payload_json is None:
                continue
            self.store.upsert_cache_card(
                CacheCard(
                    id=self._gen_id(),
                    account_id=account_id,
                    card_id=card_id,
                    payload_json=payload_json,
                    status=str(item.get("status") or ""),
                    updated_at=now,
                )
            )
            stored += 1
        return stored

    # ── read: the PWA views the cache while the PC is offline (§8.5 ③) ─────────────────────────
    def list_sessions(self, account_id: str) -> list[dict]:
        """The account's cached session summaries (newest first). Scoped to the account (§8.4)."""
        out: list[dict] = []
        for row in self.store.get_cache_sessions(account_id):
            out.append(
                {
                    "session_id": row.session_id,
                    "summary": _load_json(row.summary_json),
                    "updated_at": row.updated_at,
                    "cached": True,  # marks this as the read-only offline copy for the UI
                }
            )
        return out

    def list_cards(self, account_id: str, session_id: str | None = None) -> list[dict]:
        """The account's cached decision cards (newest first), optionally filtered to one session.

        The cache schema has no session column, so the filter reads the stored payload's
        `session_id` — display data only, scoped to the account (§8.4)."""
        out: list[dict] = []
        for row in self.store.get_cache_cards(account_id):
            payload = _load_json(row.payload_json)
            if session_id is not None and str(payload.get("session_id") or "") != session_id:
                continue
            out.append(
                {
                    "card_id": row.card_id,
                    "status": row.status,
                    "payload": payload,
                    "updated_at": row.updated_at,
                    "cached": True,
                }
            )
        return out


def _load_json(text: str) -> dict:
    """Parse a stored JSON object, falling back to {} on corruption (a bad row never crashes a read)."""
    try:
        obj = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}
