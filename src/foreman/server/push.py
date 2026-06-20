"""Web Push (VAPID) — deliver approval cards and briefings to the phone PWA (DESIGN §4.6).

Uses pywebpush with the VAPID keypair (public key in config, private key in .env). The
browser subscribes via PushManager and POSTs its subscription to /api/push/subscribe; rows
live in the local store's push_subscriptions table (DESIGN §7.1). When a push service reports
the endpoint is gone (404/410), the caller prunes that subscription.

This module imports only foreman.shared — the store is reached through the injected app state,
so the server/client boundary holds (DESIGN §14). pywebpush is imported lazily so the package
imports without the `server` extra installed.

⚠️ Live delivery is credential-gated: it needs a real VAPID keypair (scripts/gen_vapid.py →
config.yaml public + .env private) and an HTTPS origin. With no private key the Pusher is a
no-op (`enabled=False`); everything here is unit-tested with an injected `webpush` callable.
"""

from __future__ import annotations

import asyncio
import json

from foreman.shared.config import Config


def subscription_info(sub: object) -> dict:
    """Normalize a stored row OR a browser-shaped dict into pywebpush's subscription_info.

    Accepts either a PushSubscription row (attrs endpoint/p256dh/auth) or a dict in the
    browser PushSubscription.toJSON() shape ({endpoint, keys:{p256dh, auth}}) or the flat
    stored shape ({endpoint, p256dh, auth})."""
    if isinstance(sub, dict):
        endpoint = sub.get("endpoint", "")
        # Prefer the nested browser shape; fall back to the flat stored shape only when absent
        # (an explicit empty {"keys": {}} must not silently fall through to flat lookups).
        keys = sub["keys"] if "keys" in sub else {"p256dh": sub.get("p256dh", ""), "auth": sub.get("auth", "")}
    else:
        endpoint = getattr(sub, "endpoint", "")
        keys = {"p256dh": getattr(sub, "p256dh", ""), "auth": getattr(sub, "auth", "")}
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": keys.get("p256dh", ""), "auth": keys.get("auth", "")},
    }


def _endpoint_of(sub: object) -> str:
    return sub.get("endpoint", "") if isinstance(sub, dict) else getattr(sub, "endpoint", "")


def _is_gone(exc: Exception) -> bool:
    """A push endpoint is permanently gone iff the push service answered 404/410 — prune it."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in (404, 410)


class Pusher:
    # send() outcomes (str so they're easy to assert and JSON-safe):
    SENT = "sent"
    DISABLED = "disabled"   # push not configured (no private key) — no-op
    GONE = "gone"           # 404/410: subscription expired, caller should prune
    ERROR = "error"         # transient/other failure; keep the subscription

    def __init__(self, cfg: Config, *, webpush=None) -> None:
        self.subject = cfg.push.vapid_subject
        self.private_key = cfg.secrets.vapid_private_key
        self.public_key = cfg.push.vapid_public_key
        self.enabled = bool(cfg.push.enabled and self.private_key)
        self._webpush = webpush  # injected in tests; lazily imported from pywebpush otherwise

    def _resolve_webpush(self):
        if self._webpush is None:
            from pywebpush import webpush  # imported lazily (needs the `server` extra)

            self._webpush = webpush
        return self._webpush

    async def send(self, subscription: object, title: str, body: str, data: dict | None = None) -> str:
        """Send one push. Blocking pywebpush runs in a thread so the event loop isn't stalled.

        Returns one of SENT / DISABLED / GONE / ERROR. GONE means the endpoint is dead and the
        caller should drop that subscription from the store."""
        if not self.enabled:
            return self.DISABLED
        webpush = self._resolve_webpush()
        payload = json.dumps({"title": title, "body": body, "data": data or {}})
        info = subscription_info(subscription)
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=info,
                data=payload,
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.subject},
            )
        except Exception as exc:  # noqa: BLE001 — pywebpush raises WebPushException; classify by status
            return self.GONE if _is_gone(exc) else self.ERROR
        return self.SENT

    async def send_to_all(
        self, subscriptions: list, title: str, body: str, data: dict | None = None
    ) -> list[str]:
        """Fan a push out to every subscription; return the endpoints that are GONE so the
        caller can prune them from the store."""
        gone: list[str] = []
        for sub in subscriptions:
            if await self.send(sub, title, body, data) == self.GONE:
                gone.append(_endpoint_of(sub))
        return gone
