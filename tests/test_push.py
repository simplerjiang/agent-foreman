"""Tests for Web Push / VAPID (TASKS T3.3, DESIGN §4.6).

Covers the Pusher (send/classify/fan-out, with an injected webpush so no live VAPID/HTTPS is
needed), the local store's push_subscriptions helpers, the /api/push/* endpoints, and that the
PWA assets (sw.js / app.js / manifest) wire the subscription flow.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from foreman.client.store import Store
from foreman.client.store.models import PushSubscription
from foreman.server.app import create_app
from foreman.server.push import Pusher, subscription_info
from foreman.shared.config import load_config
from foreman.shared.events import EventBus


# ── fakes ────────────────────────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _PushErr(Exception):
    """Stand-in for pywebpush.WebPushException (has .response.status_code)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"push failed {status}")
        self.response = _Resp(status)


def _enabled_cfg():
    cfg = load_config()
    cfg.push.enabled = True
    cfg.push.vapid_public_key = "BPUBLIC"
    cfg.push.vapid_subject = "mailto:me@example.com"
    cfg.secrets.vapid_private_key = "PRIVATE"
    return cfg


# ── Pusher ───────────────────────────────────────────────────────────────────────────────────
async def test_pusher_disabled_is_noop():
    cfg = load_config()
    cfg.secrets.vapid_private_key = ""  # no private key → disabled
    calls: list = []
    p = Pusher(cfg, webpush=lambda **kw: calls.append(kw))
    assert p.enabled is False
    assert await p.send({"endpoint": "e"}, "t", "b") == Pusher.DISABLED
    assert calls == []  # never touches pywebpush when disabled


async def test_pusher_sends_with_vapid_claims_and_payload():
    cfg = _enabled_cfg()
    calls: list = []
    p = Pusher(cfg, webpush=lambda **kw: calls.append(kw))
    sub = {"endpoint": "https://push/abc", "keys": {"p256dh": "pk", "auth": "ak"}}
    assert await p.send(sub, "Title", "Body", {"url": "/cards/1"}) == Pusher.SENT
    assert len(calls) == 1
    kw = calls[0]
    assert kw["subscription_info"] == {
        "endpoint": "https://push/abc",
        "keys": {"p256dh": "pk", "auth": "ak"},
    }
    assert kw["vapid_private_key"] == "PRIVATE"
    assert kw["vapid_claims"] == {"sub": "mailto:me@example.com"}
    assert json.loads(kw["data"]) == {"title": "Title", "body": "Body", "data": {"url": "/cards/1"}}


async def test_pusher_gone_on_404_and_410():
    cfg = _enabled_cfg()

    def boom_404(**kw):
        raise _PushErr(404)

    def boom_410(**kw):
        raise _PushErr(410)

    assert await Pusher(cfg, webpush=boom_404).send({"endpoint": "e"}, "t", "b") == Pusher.GONE
    assert await Pusher(cfg, webpush=boom_410).send({"endpoint": "e"}, "t", "b") == Pusher.GONE


async def test_pusher_error_on_other_failures():
    cfg = _enabled_cfg()

    def boom_500(**kw):
        raise _PushErr(500)

    def boom_plain(**kw):
        raise RuntimeError("connection reset")

    assert await Pusher(cfg, webpush=boom_500).send({"endpoint": "e"}, "t", "b") == Pusher.ERROR
    assert await Pusher(cfg, webpush=boom_plain).send({"endpoint": "e"}, "t", "b") == Pusher.ERROR


async def test_send_to_all_returns_gone_endpoints_only():
    cfg = _enabled_cfg()

    def webpush(**kw):
        if kw["subscription_info"]["endpoint"] == "dead":
            raise _PushErr(410)

    p = Pusher(cfg, webpush=webpush)
    subs = [
        {"endpoint": "ok", "keys": {"p256dh": "a", "auth": "b"}},
        {"endpoint": "dead", "keys": {"p256dh": "a", "auth": "b"}},
    ]
    assert await p.send_to_all(subs, "t", "b") == ["dead"]


def test_subscription_info_from_row_and_dict():
    row = PushSubscription(id="1", endpoint="https://e", p256dh="pk", auth="ak")
    assert subscription_info(row) == {"endpoint": "https://e", "keys": {"p256dh": "pk", "auth": "ak"}}
    flat = {"endpoint": "https://e2", "p256dh": "pk2", "auth": "ak2"}
    assert subscription_info(flat) == {"endpoint": "https://e2", "keys": {"p256dh": "pk2", "auth": "ak2"}}
    # an explicit (empty) browser-shape "keys" is used as-is, not silently merged with flat fields
    odd = {"endpoint": "https://e3", "keys": {}, "p256dh": "ignored"}
    assert subscription_info(odd) == {"endpoint": "https://e3", "keys": {"p256dh": "", "auth": ""}}


# ── store helpers ─────────────────────────────────────────────────────────────────────────────
def test_store_push_subscription_upsert_get_delete(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    store.add_push_subscription(endpoint="https://e1", p256dh="pk", auth="ak", ua="firefox")
    store.add_push_subscription(endpoint="https://e2", p256dh="pk2", auth="ak2")
    assert {s.endpoint for s in store.get_push_subscriptions()} == {"https://e1", "https://e2"}

    # re-subscribe with the same endpoint updates keys in place (no duplicate row)
    store.add_push_subscription(endpoint="https://e1", p256dh="NEW", auth="NEWAUTH")
    subs = store.get_push_subscriptions()
    assert len(subs) == 2
    e1 = next(s for s in subs if s.endpoint == "https://e1")
    assert e1.p256dh == "NEW" and e1.auth == "NEWAUTH"

    store.delete_push_subscription("https://e1")
    assert {s.endpoint for s in store.get_push_subscriptions()} == {"https://e2"}
    store.delete_push_subscription("https://missing")  # no-op, no error


# ── /api/push/* endpoints ─────────────────────────────────────────────────────────────────────
def _app_with_store(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    cfg = load_config()
    cfg.push.enabled = True
    cfg.push.vapid_public_key = "BPUB"
    return create_app(cfg, store, EventBus()), store


def test_api_vapid_public_key_enabled(tmp_path):
    app, _ = _app_with_store(tmp_path)
    j = TestClient(app).get("/api/push/vapid-public-key").json()
    assert j["key"] == "BPUB" and j["enabled"] is True


def test_api_vapid_public_key_disabled_without_key():
    j = TestClient(create_app(load_config())).get("/api/push/vapid-public-key").json()
    assert j["key"] == "" and j["enabled"] is False


def test_api_push_subscribe_and_unsubscribe(tmp_path):
    app, store = _app_with_store(tmp_path)
    c = TestClient(app)
    body = {"endpoint": "https://push/abc", "expirationTime": None,
            "keys": {"p256dh": "pk", "auth": "ak"}}
    assert c.post("/api/push/subscribe", json=body).json() == {"ok": True}
    subs = store.get_push_subscriptions()
    assert len(subs) == 1 and subs[0].endpoint == "https://push/abc" and subs[0].p256dh == "pk"

    assert c.post("/api/push/unsubscribe", json={"endpoint": "https://push/abc"}).json() == {"ok": True}
    assert store.get_push_subscriptions() == []


def test_api_push_503_without_store():
    c = TestClient(create_app(load_config()))  # store=None
    body = {"endpoint": "e", "keys": {"p256dh": "p", "auth": "a"}}
    assert c.post("/api/push/subscribe", json=body).status_code == 503
    assert c.post("/api/push/unsubscribe", json={"endpoint": "e"}).status_code == 503


# ── PWA assets wire the push flow ─────────────────────────────────────────────────────────────
def test_pwa_assets_wire_push():
    c = TestClient(create_app(load_config()))
    sw = c.get("/sw.js").text
    assert "showNotification" in sw and "notificationclick" in sw
    js = c.get("/app.js").text
    assert "/api/push/vapid-public-key" in js
    assert "pushManager.subscribe" in js and "urlBase64ToUint8Array" in js
    mani = c.get("/manifest.webmanifest").json()
    assert mani["display"] == "standalone" and mani["scope"] == "/"
    assert any(i["sizes"] == "512x512" for i in mani["icons"])
