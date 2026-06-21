"""Notification channels — one unified Notifier interface + built-in channels + plugin loading.

DESIGN §11.2E / §776-777 (the second-layer, "偶尔才用的代码级扩展"): a notification channel is
just **one action** — ``send(title, body, buttons)``. Feishu / Telegram / Bark / email each ship an
implementation; the core never changes. Third parties publish ``foreman-plugin-xxx`` packages that
register a notifier under the ``foreman.notifiers`` entry-point group, and Foreman auto-loads them
on startup (§777).

Boundary (DESIGN §14): this lives in ``shared`` (both the PC app and the team server may want to
notify) and imports only stdlib + httpx (a base dep) + ``foreman.shared.config`` — no client/server
import. Every channel takes an **injectable** network/SMTP client, so the whole module is
unit-testable without ever touching the network.

Secrets — bot tokens, webhook URLs, SMTP passwords — come from ``.env`` / ``Secrets`` (FOREMAN_
prefix), never from config.yaml or git. ``send`` returns one of SENT / DISABLED / ERROR and is
written to **never raise**: a broken channel must not take down the caller (the same hardening
rationale as Pusher in server/push.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from foreman.shared.config import Config

log = logging.getLogger("foreman.notify")

# send() outcomes — plain str so they're easy to assert and JSON-safe (mirrors Pusher's vocabulary).
SENT = "sent"
DISABLED = "disabled"  # channel not configured → no-op
ERROR = "error"        # transient / other failure

# Third-party notifiers register under this entry-point group; load_plugin_notifiers() discovers them.
NOTIFIER_ENTRYPOINT_GROUP = "foreman.notifiers"


@dataclass
class NotifyButton:
    """A single tap-through button: visible label + destination URL (e.g. a deep link to a card)."""

    text: str
    url: str


class Notifier:
    """One channel. Implement :meth:`send` — that is the entire contract (DESIGN §776).

    ``name`` is the channel id (used to de-dupe built-ins vs plugins). ``enabled`` lets a
    misconfigured channel opt out so the hub skips it. ``send`` returns SENT / DISABLED / ERROR.
    """

    name: str = "notifier"
    enabled: bool = True

    def send(self, title: str, body: str, buttons: list[NotifyButton] | None = None) -> str:
        raise NotImplementedError


# ── helpers ────────────────────────────────────────────────────────────────────────────────────
def _usable_buttons(buttons: list[NotifyButton] | None) -> list[NotifyButton]:
    """Keep only buttons with both a label and a URL — a half-filled button renders garbage and
    some APIs (e.g. Telegram inline keyboards) reject an empty label outright."""
    return [b for b in (buttons or []) if getattr(b, "text", "") and getattr(b, "url", "")]


def _buttons_as_text(buttons: list[NotifyButton] | None) -> str:
    """Render buttons as trailing ``label: url`` lines, for channels with no native buttons."""
    usable = _usable_buttons(buttons)
    if not usable:
        return ""
    return "\n\n" + "\n".join(f"{b.text}: {b.url}" for b in usable)


def _default_post(url: str, *, json=None, data=None, timeout: float = 10.0):
    """Default HTTP POST (httpx, imported lazily). Injectable in tests via the ``post`` ctor arg."""
    import httpx

    return httpx.post(url, json=json, data=data, timeout=timeout)


def _http_ok(resp: object) -> bool:
    code = getattr(resp, "status_code", None)
    return code is not None and 200 <= code < 300


def _json_field(resp: object, key: str, default=None):
    """Read a field from a JSON response body, defensively (non-JSON / non-dict → default)."""
    getter = getattr(resp, "json", None)
    if not callable(getter):
        return default
    try:
        data = getter()
    except Exception:  # noqa: BLE001 — a non-JSON body just means "use the default"
        return default
    return data.get(key, default) if isinstance(data, dict) else default


# ── built-in channels ────────────────────────────────────────────────────────────────────────--
class FeishuNotifier(Notifier):
    """飞书 custom-bot webhook (a text message; buttons appended as link lines)."""

    name = "feishu"

    def __init__(self, webhook: str, *, post: Callable | None = None) -> None:
        self.webhook = (webhook or "").strip()
        self.enabled = bool(self.webhook)
        self._post = post or _default_post

    def send(self, title, body, buttons=None):
        if not self.enabled:
            return DISABLED
        text = f"{title}\n\n{body}{_buttons_as_text(buttons)}"
        payload = {"msg_type": "text", "content": {"text": text}}
        try:
            resp = self._post(self.webhook, json=payload)
            if not _http_ok(resp):
                return ERROR
            code = _json_field(resp, "code")  # Feishu returns {"code": 0} on success
            return SENT if code in (None, 0) else ERROR
        except Exception:  # noqa: BLE001 — never propagate a channel failure to the caller
            log.warning("feishu notify failed", exc_info=True)
            return ERROR


class TelegramNotifier(Notifier):
    """Telegram Bot API ``sendMessage`` (buttons become an inline keyboard)."""

    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        api_base: str = "https://api.telegram.org",
        post: Callable | None = None,
    ) -> None:
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.api_base = (api_base or "https://api.telegram.org").rstrip("/")
        self.enabled = bool(self.token and self.chat_id)
        self._post = post or _default_post

    def send(self, title, body, buttons=None):
        if not self.enabled:
            return DISABLED
        payload = {
            "chat_id": self.chat_id,
            "text": f"{title}\n\n{body}",
            "disable_web_page_preview": True,
        }
        usable = _usable_buttons(buttons)
        if usable:
            keyboard = [[{"text": b.text, "url": b.url}] for b in usable]
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        url = f"{self.api_base}/bot{self.token}/sendMessage"
        try:
            resp = self._post(url, json=payload)
            if not _http_ok(resp):
                return ERROR
            return SENT if _json_field(resp, "ok", True) else ERROR
        except Exception:  # noqa: BLE001
            log.warning("telegram notify failed", exc_info=True)
            return ERROR


class BarkNotifier(Notifier):
    """Bark iOS push. Supports a single tap-through URL (the first button); the rest go in the body."""

    name = "bark"

    def __init__(
        self, key: str, *, server: str = "https://api.day.app", post: Callable | None = None
    ) -> None:
        self.key = (key or "").strip()
        self.server = (server or "https://api.day.app").rstrip("/")
        self.enabled = bool(self.key)
        self._post = post or _default_post

    def send(self, title, body, buttons=None):
        if not self.enabled:
            return DISABLED
        payload = {"device_key": self.key, "title": title, "body": body}
        usable = _usable_buttons(buttons)
        if usable:
            payload["url"] = usable[0].url  # Bark taps open one URL
            extra = _buttons_as_text(usable[1:])
            if extra:
                payload["body"] = body + extra
        try:
            resp = self._post(f"{self.server}/push", json=payload)
            if not _http_ok(resp):
                return ERROR
            code = _json_field(resp, "code", 200)  # Bark returns {"code": 200} on success
            return SENT if code in (200, None) else ERROR
        except Exception:  # noqa: BLE001
            log.warning("bark notify failed", exc_info=True)
            return ERROR


def _default_smtp_send(msg, *, host, port, username, password, use_tls) -> None:
    """Default SMTP delivery (stdlib smtplib). Injectable in tests via the ``sender`` ctor arg."""
    import smtplib

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if use_tls:
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)


class EmailNotifier(Notifier):
    """邮件 via SMTP (stdlib). Buttons are appended to the plain-text body as link lines."""

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        from_addr: str = "",
        to_addrs: list[str] | None = None,
        use_tls: bool = True,
        sender: Callable | None = None,
    ) -> None:
        self.host = (host or "").strip()
        self.port = port
        self.username = username
        self.password = password
        self.from_addr = (from_addr or "").strip()
        self.to_addrs = [a.strip() for a in (to_addrs or []) if a and a.strip()]
        self.use_tls = use_tls
        self.enabled = bool(self.host and self.from_addr and self.to_addrs)
        self._sender = sender or _default_smtp_send

    def send(self, title, body, buttons=None):
        if not self.enabled:
            return DISABLED
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg.set_content(body + _buttons_as_text(buttons))
        try:
            self._sender(
                msg,
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                use_tls=self.use_tls,
            )
            return SENT
        except Exception:  # noqa: BLE001
            log.warning("email notify failed", exc_info=True)
            return ERROR


# ── config-driven factories (also the entry-point targets a plugin would mimic) ─────────────────-
def feishu_from_config(config: Config) -> Notifier | None:
    cfg = config.notify.feishu
    webhook = config.secrets.feishu_webhook
    if not (cfg.enabled and webhook):
        return None
    return FeishuNotifier(webhook)


def telegram_from_config(config: Config) -> Notifier | None:
    cfg = config.notify.telegram
    token = config.secrets.telegram_bot_token
    if not (cfg.enabled and token and cfg.chat_id):
        return None
    return TelegramNotifier(token, cfg.chat_id, api_base=cfg.api_base)


def bark_from_config(config: Config) -> Notifier | None:
    cfg = config.notify.bark
    key = config.secrets.bark_key
    if not (cfg.enabled and key):
        return None
    return BarkNotifier(key, server=cfg.server)


def email_from_config(config: Config) -> Notifier | None:
    cfg = config.notify.email
    if not (cfg.enabled and cfg.smtp_host and cfg.from_addr and cfg.to_addrs):
        return None
    return EmailNotifier(
        host=cfg.smtp_host,
        port=cfg.smtp_port,
        username=cfg.username,
        password=config.secrets.smtp_password,
        from_addr=cfg.from_addr,
        to_addrs=cfg.to_addrs,
        use_tls=cfg.use_tls,
    )


# Built-ins are loaded directly (always available, no install step) — third-party channels arrive
# via entry points. Keeping built-ins out of the entry-point path means core notifications work even
# if the package was not (re)installed, and unit tests don't depend on install state.
BUILTIN_NOTIFIER_FACTORIES: dict[str, Callable[[Config], "Notifier | None"]] = {
    "feishu": feishu_from_config,
    "telegram": telegram_from_config,
    "bark": bark_from_config,
    "email": email_from_config,
}


# ── plugin entry points (DESIGN §777) ───────────────────────────────────────────────────────────
def _iter_entry_points(group: str):
    """List entry points for ``group`` (importlib.metadata). Defensive against API/version quirks."""
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=group))  # py3.10+ keyword API (we require 3.11)
    except TypeError:  # pragma: no cover — legacy dict API, defensive only
        return list(entry_points().get(group, []))


def load_plugin_notifiers(config: Config, *, entry_points_fn: Callable | None = None) -> list[Notifier]:
    """Discover + build third-party notifiers from the ``foreman.notifiers`` entry-point group.

    Each entry point resolves to a factory ``(config) -> Notifier | None``. A plugin that fails to
    import / build / is disabled is **skipped** with a warning — one bad plugin never breaks startup.
    ``entry_points_fn`` is injectable so tests don't need an installed package.
    """
    eps = (entry_points_fn or _iter_entry_points)(NOTIFIER_ENTRYPOINT_GROUP)
    out: list[Notifier] = []
    for ep in eps:
        name = getattr(ep, "name", "?")
        try:
            factory = ep.load()
            notifier = factory(config)
        except Exception:  # noqa: BLE001 — isolate plugin failures
            log.warning("notifier plugin %r failed to load — skipping", name, exc_info=True)
            continue
        if notifier is None or not getattr(notifier, "enabled", True):
            continue
        out.append(notifier)
    return out


class NotificationHub:
    """Fans one notification out to every configured channel (built-ins + plugins).

    Construct via :meth:`from_config`. ``notify`` is sync (channels are simple/blocking);
    ``notify_async`` runs the fan-out in a thread so an async caller's event loop isn't stalled
    (same approach as Pusher). This is the integration seam for Gate / Briefing to add channels
    alongside Web Push.
    """

    def __init__(self, notifiers: list[Notifier] | None = None) -> None:
        self.notifiers = list(notifiers or [])

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        include_plugins: bool = True,
        entry_points_fn: Callable | None = None,
    ) -> "NotificationHub":
        notifiers: list[Notifier] = []
        seen: set[str] = set()
        for name, factory in BUILTIN_NOTIFIER_FACTORIES.items():
            try:
                notifier = factory(config)
            except Exception:  # noqa: BLE001
                log.warning("built-in notifier %r failed to build", name, exc_info=True)
                notifier = None
            if notifier is not None and getattr(notifier, "enabled", True):
                notifiers.append(notifier)
                seen.add(notifier.name)
        if include_plugins:
            for notifier in load_plugin_notifiers(config, entry_points_fn=entry_points_fn):
                if notifier.name in seen:
                    log.warning("notifier plugin %r shadows a built-in — skipping", notifier.name)
                    continue
                seen.add(notifier.name)
                notifiers.append(notifier)
        return cls(notifiers)

    def notify(self, title: str, body: str, buttons: list[NotifyButton] | None = None) -> dict[str, str]:
        """Send to every channel; return ``{channel_name: result}``. Never raises."""
        results: dict[str, str] = {}
        for notifier in self.notifiers:
            try:
                results[notifier.name] = notifier.send(title, body, buttons)
            except Exception:  # noqa: BLE001 — a misbehaving channel must not break the fan-out
                log.warning("notifier %r raised in send()", notifier.name, exc_info=True)
                results[notifier.name] = ERROR
        return results

    async def notify_async(
        self, title: str, body: str, buttons: list[NotifyButton] | None = None
    ) -> dict[str, str]:
        import asyncio

        return await asyncio.to_thread(self.notify, title, body, buttons)


__all__ = [
    "SENT",
    "DISABLED",
    "ERROR",
    "NOTIFIER_ENTRYPOINT_GROUP",
    "NotifyButton",
    "Notifier",
    "FeishuNotifier",
    "TelegramNotifier",
    "BarkNotifier",
    "EmailNotifier",
    "feishu_from_config",
    "telegram_from_config",
    "bark_from_config",
    "email_from_config",
    "BUILTIN_NOTIFIER_FACTORIES",
    "load_plugin_notifiers",
    "NotificationHub",
]
