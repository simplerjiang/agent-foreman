"""Web Push (VAPID) — deliver approval cards and briefings to the phone PWA.

Uses pywebpush with the VAPID keypair (public key in config, private key in .env).
Subscriptions are stored in the push_subscriptions table. See docs/SECURITY.md (HTTPS/iOS).
"""

from __future__ import annotations

import json

from foreman.shared.config import Config


class Pusher:
    def __init__(self, cfg: Config) -> None:
        self.subject = cfg.push.vapid_subject
        self.private_key = cfg.secrets.vapid_private_key
        self.enabled = cfg.push.enabled and bool(self.private_key)

    async def send(self, subscription: dict, title: str, body: str, data: dict | None = None) -> None:
        """Send a push to one subscription (P3)."""
        if not self.enabled:
            return
        from pywebpush import webpush  # imported lazily

        payload = json.dumps({"title": title, "body": body, "data": data or {}})
        # TODO(P3): run blocking webpush() in a thread; handle 410 (expired) -> prune subscription.
        _ = (webpush, subscription, payload, self.subject, self.private_key)
        raise NotImplementedError("Pusher.send — roadmap P3")
