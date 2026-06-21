"""Tests for notification channels (TASKS T6.3, DESIGN §776-777).

Covers the four built-in channels (Feishu / Telegram / Bark / email) with injected HTTP/SMTP
clients so no network is touched, the config-driven factories, plugin entry-point loading
(including that a broken plugin is skipped), and the NotificationHub fan-out.
"""

from __future__ import annotations

from foreman.shared.config import load_config
from foreman.shared.notify import (
    DISABLED,
    ERROR,
    NOTIFIER_ENTRYPOINT_GROUP,
    SENT,
    BarkNotifier,
    EmailNotifier,
    FeishuNotifier,
    NotificationHub,
    Notifier,
    NotifyButton,
    TelegramNotifier,
    bark_from_config,
    email_from_config,
    feishu_from_config,
    load_plugin_notifiers,
    telegram_from_config,
)


# ── fakes ────────────────────────────────────────────────────────────────────────────────────--
class _Resp:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


def _capture_post(resp: _Resp | None = None):
    """A fake `post` that records calls and returns `resp` (default 200/empty)."""
    calls: list[dict] = []

    def post(url, *, json=None, data=None, timeout=10.0):
        calls.append({"url": url, "json": json, "data": data})
        return resp if resp is not None else _Resp()

    return post, calls


class _EP:
    """A fake importlib.metadata EntryPoint (name + load())."""

    def __init__(self, name, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


# ── Feishu ───────────────────────────────────────────────────────────────────────────────────--
def test_feishu_disabled_without_webhook():
    n = FeishuNotifier("")
    assert n.enabled is False
    assert n.send("t", "b") == DISABLED


def test_feishu_sends_text_with_buttons():
    post, calls = _capture_post(_Resp(200, {"code": 0}))
    n = FeishuNotifier("https://hook/x", post=post)
    out = n.send("Title", "Body", [NotifyButton("Open", "https://app/1")])
    assert out == SENT
    assert calls[0]["url"] == "https://hook/x"
    text = calls[0]["json"]["content"]["text"]
    assert "Title" in text and "Body" in text and "Open: https://app/1" in text


def test_buttons_with_missing_label_or_url_are_dropped():
    # A half-filled button must not render a dangling line or an empty-label keyboard entry.
    post, calls = _capture_post(_Resp(200, {"code": 0}))
    n = FeishuNotifier("https://hook/x", post=post)
    n.send("T", "B", [NotifyButton("", "https://app/1"), NotifyButton("Label", "")])
    text = calls[0]["json"]["content"]["text"]
    assert "https://app/1" not in text and "Label" not in text

    post2, calls2 = _capture_post(_Resp(200, {"ok": True}))
    TelegramNotifier("TOK", "9", post=post2).send("T", "B", [NotifyButton("", "https://app/1")])
    assert "reply_markup" not in calls2[0]["json"]  # no empty-label keyboard entry


def test_feishu_nonzero_code_is_error():
    post, _ = _capture_post(_Resp(200, {"code": 19021, "msg": "bad"}))
    assert FeishuNotifier("https://hook/x", post=post).send("t", "b") == ERROR


def test_feishu_http_error_is_error():
    post, _ = _capture_post(_Resp(500, {}))
    assert FeishuNotifier("https://hook/x", post=post).send("t", "b") == ERROR


def test_feishu_post_raising_is_swallowed():
    def boom(url, **kw):
        raise RuntimeError("network down")

    assert FeishuNotifier("https://hook/x", post=boom).send("t", "b") == ERROR


# ── Telegram ─────────────────────────────────────────────────────────────────────────────────--
def test_telegram_disabled_without_token_or_chat():
    assert TelegramNotifier("", "123").enabled is False
    assert TelegramNotifier("tok", "").enabled is False


def test_telegram_sends_with_inline_keyboard():
    post, calls = _capture_post(_Resp(200, {"ok": True}))
    n = TelegramNotifier("TOK", "999", post=post)
    out = n.send("T", "B", [NotifyButton("Approve", "https://app/a")])
    assert out == SENT
    assert calls[0]["url"] == "https://api.telegram.org/botTOK/sendMessage"
    payload = calls[0]["json"]
    assert payload["chat_id"] == "999"
    assert payload["reply_markup"]["inline_keyboard"] == [[{"text": "Approve", "url": "https://app/a"}]]


def test_telegram_not_ok_is_error():
    post, _ = _capture_post(_Resp(200, {"ok": False, "description": "blocked"}))
    assert TelegramNotifier("TOK", "999", post=post).send("t", "b") == ERROR


# ── Bark ─────────────────────────────────────────────────────────────────────────────────────--
def test_bark_disabled_without_key():
    assert BarkNotifier("").enabled is False


def test_bark_first_button_becomes_tap_url_rest_in_body():
    post, calls = _capture_post(_Resp(200, {"code": 200}))
    n = BarkNotifier("KEY", post=post)
    out = n.send(
        "T", "B", [NotifyButton("Open", "https://app/1"), NotifyButton("Details", "https://app/2")]
    )
    assert out == SENT
    payload = calls[0]["json"]
    assert calls[0]["url"] == "https://api.day.app/push"
    assert payload["device_key"] == "KEY"
    assert payload["url"] == "https://app/1"  # first button → tap action
    assert "Details: https://app/2" in payload["body"]  # remaining buttons appended to body


def test_bark_nonok_code_is_error():
    post, _ = _capture_post(_Resp(200, {"code": 400}))
    assert BarkNotifier("KEY", post=post).send("t", "b") == ERROR


# ── Email ────────────────────────────────────────────────────────────────────────────────────--
def test_email_disabled_without_required_fields():
    assert EmailNotifier(host="", from_addr="a@b.c", to_addrs=["x@y.z"]).enabled is False
    assert EmailNotifier(host="smtp", from_addr="", to_addrs=["x@y.z"]).enabled is False
    assert EmailNotifier(host="smtp", from_addr="a@b.c", to_addrs=[]).enabled is False


def test_email_sends_message_with_buttons_in_body():
    sent: list = []

    def sender(msg, **kw):
        sent.append((msg, kw))

    n = EmailNotifier(
        host="smtp.example.com",
        port=587,
        username="u",
        password="p",
        from_addr="from@x.com",
        to_addrs=["a@x.com", "b@x.com"],
        sender=sender,
    )
    out = n.send("Subj", "Hello", [NotifyButton("Card", "https://app/c")])
    assert out == SENT
    msg, kw = sent[0]
    assert msg["Subject"] == "Subj"
    assert msg["To"] == "a@x.com, b@x.com"
    assert "Card: https://app/c" in msg.get_content()
    assert kw["host"] == "smtp.example.com" and kw["password"] == "p"


def test_email_sender_raising_is_error():
    def boom(msg, **kw):
        raise OSError("smtp refused")

    n = EmailNotifier(host="smtp", from_addr="a@x.com", to_addrs=["b@x.com"], sender=boom)
    assert n.send("t", "b") == ERROR


# ── config factories ─────────────────────────────────────────────────────────────────────────--
def test_factories_return_none_when_disabled():
    cfg = load_config()  # all channels default disabled
    assert feishu_from_config(cfg) is None
    assert telegram_from_config(cfg) is None
    assert bark_from_config(cfg) is None
    assert email_from_config(cfg) is None


def test_factories_build_when_enabled_and_secret_present():
    cfg = load_config()
    cfg.notify.feishu.enabled = True
    cfg.secrets.feishu_webhook = "https://hook/abc"
    cfg.notify.telegram.enabled = True
    cfg.notify.telegram.chat_id = "42"
    cfg.secrets.telegram_bot_token = "TOK"
    cfg.notify.bark.enabled = True
    cfg.secrets.bark_key = "BK"
    cfg.notify.email.enabled = True
    cfg.notify.email.smtp_host = "smtp.x.com"
    cfg.notify.email.from_addr = "a@x.com"
    cfg.notify.email.to_addrs = ["b@x.com"]
    cfg.secrets.smtp_password = "pw"

    assert isinstance(feishu_from_config(cfg), FeishuNotifier)
    tg = telegram_from_config(cfg)
    assert isinstance(tg, TelegramNotifier) and tg.chat_id == "42"
    assert isinstance(bark_from_config(cfg), BarkNotifier)
    em = email_from_config(cfg)
    assert isinstance(em, EmailNotifier) and em.password == "pw"


def test_factory_disabled_when_secret_missing_even_if_enabled():
    cfg = load_config()
    cfg.notify.feishu.enabled = True  # enabled but no webhook secret
    assert feishu_from_config(cfg) is None


# ── plugin entry points ──────────────────────────────────────────────────────────────────────--
class _PluginNotifier(Notifier):
    name = "slack"

    def send(self, title, body, buttons=None):
        return SENT


def test_load_plugin_notifiers_discovers_factory():
    cfg = load_config()

    def factory(config):
        return _PluginNotifier()

    def eps(group):
        assert group == NOTIFIER_ENTRYPOINT_GROUP
        return [_EP("slack", lambda: factory)]

    loaded = load_plugin_notifiers(cfg, entry_points_fn=eps)
    assert [n.name for n in loaded] == ["slack"]


def test_load_plugin_notifiers_skips_broken_plugin():
    cfg = load_config()

    def good_factory(config):
        return _PluginNotifier()

    def bad_loader():
        raise ImportError("missing dep")

    def eps(group):
        return [_EP("broken", bad_loader), _EP("slack", lambda: good_factory)]

    loaded = load_plugin_notifiers(cfg, entry_points_fn=eps)
    assert [n.name for n in loaded] == ["slack"]  # broken one skipped, startup survives


def test_load_plugin_notifiers_skips_none_and_disabled():
    cfg = load_config()

    class _Disabled(Notifier):
        name = "off"
        enabled = False

        def send(self, *a, **k):
            return DISABLED

    def eps(group):
        return [
            _EP("returns_none", lambda: (lambda c: None)),
            _EP("disabled", lambda: (lambda c: _Disabled())),
        ]

    assert load_plugin_notifiers(cfg, entry_points_fn=eps) == []


# ── NotificationHub ──────────────────────────────────────────────────────────────────────────--
def test_hub_from_config_loads_builtins_and_plugins():
    cfg = load_config()
    cfg.notify.feishu.enabled = True
    cfg.secrets.feishu_webhook = "https://hook/x"

    def eps(group):
        return [_EP("slack", lambda: (lambda c: _PluginNotifier()))]

    hub = NotificationHub.from_config(cfg, entry_points_fn=eps)
    names = {n.name for n in hub.notifiers}
    assert names == {"feishu", "slack"}  # only the enabled built-in + the plugin


def test_hub_plugin_cannot_shadow_builtin():
    cfg = load_config()
    cfg.notify.feishu.enabled = True
    cfg.secrets.feishu_webhook = "https://hook/x"

    class _FakeFeishu(Notifier):
        name = "feishu"  # collides with the built-in

        def send(self, *a, **k):
            return SENT

    def eps(group):
        return [_EP("feishu", lambda: (lambda c: _FakeFeishu()))]

    hub = NotificationHub.from_config(cfg, entry_points_fn=eps)
    feishu = [n for n in hub.notifiers if n.name == "feishu"]
    assert len(feishu) == 1
    assert isinstance(feishu[0], FeishuNotifier)  # built-in wins, plugin shadow skipped


def test_hub_notify_fans_out_and_collects_results():
    post_ok, _ = _capture_post(_Resp(200, {"code": 0}))

    class _Boom(Notifier):
        name = "boom"

        def send(self, *a, **k):
            raise RuntimeError("kaboom")

    hub = NotificationHub([FeishuNotifier("https://hook/x", post=post_ok), _Boom()])
    results = hub.notify("T", "B")
    assert results == {"feishu": SENT, "boom": ERROR}  # one channel raising never breaks the rest


async def test_hub_notify_async_runs_fan_out():
    post_ok, calls = _capture_post(_Resp(200, {"code": 0}))
    hub = NotificationHub([FeishuNotifier("https://hook/x", post=post_ok)])
    results = await hub.notify_async("T", "B")
    assert results == {"feishu": SENT}
    assert len(calls) == 1


def test_hub_empty_when_nothing_configured():
    hub = NotificationHub.from_config(load_config(), include_plugins=False)
    assert hub.notifiers == []
    assert hub.notify("t", "b") == {}
