"""Configuration loading: config.yaml (structure) + .env (secrets).

Secrets never live in config.yaml. They are read from environment / .env with the
FOREMAN_ prefix (see .env.example).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Secrets(BaseSettings):
    """Secret values, sourced from environment / .env (FOREMAN_ prefix)."""

    model_config = SettingsConfigDict(env_prefix="FOREMAN_", env_file=".env", extra="ignore")

    llm_api_key: str = ""
    vapid_private_key: str = ""
    auth_token: str = ""
    # Optional at-rest encryption key for definition bodies (DESIGN §765, T6.2). A urlsafe-base64
    # Fernet key (scripts/gen_definition_key.py). Empty (the default) → bodies stay plaintext.
    definition_key: str = ""
    # Notification-channel secrets (DESIGN §776, T6.3) — bot tokens / webhook URLs / SMTP password.
    # Empty = that channel stays disabled. Never put these in config.yaml or git.
    feishu_webhook: str = ""
    telegram_bot_token: str = ""
    bark_key: str = ""
    smtp_password: str = ""


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    public_base_url: str = ""
    # "personal" (default) | "team". Team mode makes `foreman serve` the relay 总机 (DESIGN §8.5):
    # it builds the server store (accounts / access_keys / process_registry), an AuthManager and a
    # Relay, so local processes dial in at /relay and the PWA routes by account. Personal mode keeps
    # serve as today — just /health + PWA + the single-user REST/WS the tunnel exposes.
    mode: str = "personal"
    # Server/relay DB (team mode only; DESIGN §7.2). Holds NO 秘方/diffs/LLM keys — those stay on
    # each user's local process (§8.3). Separate file from the client's StoreCfg.db_path.
    db_path: str = "foreman-server.db"
    # Trust CF-Connecting-IP / X-Forwarded-For for the real client IP (auth rate-limit bucketing).
    # OFF by default: those headers are client-spoofable, so trusting them on a directly-reachable
    # server lets an attacker evade the brute-force limiter and inflate its key map. Set true ONLY
    # when a trusted proxy (e.g. the Cloudflare tunnel, which sets CF-Connecting-IP) fronts the app.
    trust_proxy_headers: bool = False
    # ── security hardening (issue #1) ─────────────────────────────────────────────────────────
    # Personal-mode operational APIs are gated by a shared access token (FOREMAN_AUTH_TOKEN). When
    # no token is set, exposing them on a non-loopback bind (or with a public_base_url) FAILS
    # CLOSED at startup — anyone with the URL could otherwise read sessions / dispatch work /
    # approve actions (issue #1 P0). Set this true ONLY for a trusted LAN where you accept that.
    allow_insecure_bind: bool = False
    # Redirect http→https at the app layer (issue #1 P2). Prefer terminating TLS + redirecting at
    # Cloudflare/your proxy; this is a defense-in-depth fallback that honours X-Forwarded-Proto so
    # it won't loop behind a TLS-terminating proxy. Off by default.
    force_https: bool = False
    # Emit Strict-Transport-Security. Turn on ONLY once HTTPS is stable end-to-end (issue #1 P2) —
    # a premature HSTS header can lock clients out of an http fallback. max-age seconds.
    hsts: bool = False
    hsts_max_age: int = 31536000
    # Content-Security-Policy for the PWA (conservative default — the front-end has no inline
    # script/style, so 'self' is sufficient). Empty string disables the header (issue #1 P2).
    # connect-src lists ws:/wss: explicitly: the live timeline opens a same-origin WebSocket, and
    # not every browser treats `'self'` as covering the ws/wss schemes — without this the /ws
    # stream is blocked under the default hardening header (codex acceptance finding). script-src is
    # still locked to 'self' with no inline script, so only first-party code can open a connection.
    csp: str = (
        "default-src 'self'; img-src 'self' data:; connect-src 'self' ws: wss:; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    # Include the DB path in /health. Off by default so the public readiness probe doesn't leak the
    # deployment's filesystem layout (issue #1 P2).
    health_show_db: bool = False


class LLMCfg(BaseModel):
    provider: str = "openai"  # "openai" | "anthropic"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    request_timeout_s: int = 60
    max_tokens: int = 2048
    # Wire transport. "http" (default): POST {base_url}/chat/completions (or /messages for anthropic).
    # "ws": the Responses API over a WebSocket — GET {ws base_url}/responses, send a `response.create`
    # frame, read `response.output_text.delta` events until `response.completed` (CLIProxyAPI style).
    transport: str = "http"


class StoreCfg(BaseModel):
    db_path: str = "foreman.db"


class WorkspaceCfg(BaseModel):
    path: str
    name: str = ""


class AgentCfg(BaseModel):
    enabled: bool = True
    command: str
    mode: str = "headless"  # "headless" | "pty"


class MonitorCfg(BaseModel):
    hooks_enabled: bool = True
    git_watch: bool = True
    idle_seconds: int = 120


# Baked-in irreversible-command denylist (DESIGN §6.6 red line). Defaults are NON-empty so the
# Gate catches hard-dangerous actions even with no config.yaml — critical now that the decision loop
# can auto-execute "safe" actions (P4): an empty list would classify everything "safe" → auto.
# config.yaml may extend/replace these. Substring match, case-insensitive (see Gate.classify).
_DEFAULT_REQUIRES_APPROVAL = [
    "git push", "git push --force", "push --force", "rm -rf", "rmdir /s",
    "drop table", "drop database", "truncate", "deploy", "publish",
    "secrets", "shutdown", "reboot", "mkfs", "format ", ":(){", "sudo", "runas",
]


class GatesCfg(BaseModel):
    requires_approval: list[str] = Field(default_factory=lambda: list(_DEFAULT_REQUIRES_APPROVAL))
    needs_strategy: list[str] = Field(default_factory=list)
    approval_timeout_s: int = 0


class ScheduleCfg(BaseModel):
    assess_every_s: int = 60
    daily_briefing: str = ""


class PushCfg(BaseModel):
    enabled: bool = True
    vapid_public_key: str = ""
    vapid_subject: str = "mailto:you@example.com"


class UICfg(BaseModel):
    language: str = "zh"  # zh | en — default UI + LLM output language (DESIGN §15)


class AutonomyCfg(BaseModel):
    # Baseline autonomy dial 0..3 (DESIGN §6.4); a config_kv "autonomy.level" overrides at runtime.
    level: int = 1


# Notification channels (DESIGN §776, T6.3). Structure (enabled flags / addresses / hosts) lives
# here; the secrets (webhook URL / bot token / device key / SMTP password) live in .env / Secrets.
# Each channel defaults to disabled — opt in per channel.
class FeishuCfg(BaseModel):
    enabled: bool = False  # webhook URL itself is the secret (Secrets.feishu_webhook)


class TelegramCfg(BaseModel):
    enabled: bool = False
    chat_id: str = ""
    api_base: str = "https://api.telegram.org"


class BarkCfg(BaseModel):
    enabled: bool = False
    server: str = "https://api.day.app"


class EmailCfg(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    use_tls: bool = True
    username: str = ""
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)


class NotifyCfg(BaseModel):
    feishu: FeishuCfg = FeishuCfg()
    telegram: TelegramCfg = TelegramCfg()
    bark: BarkCfg = BarkCfg()
    email: EmailCfg = EmailCfg()


class Config(BaseModel):
    server: ServerCfg = ServerCfg()
    llm: LLMCfg = LLMCfg()
    store: StoreCfg = StoreCfg()
    workspaces: list[WorkspaceCfg] = Field(default_factory=list)
    # When no workspace allowlist is configured, a dispatch with an explicit workspace path FAILS
    # CLOSED (rejected) unless this dev flag is set — so a phone dispatch can never launch an agent
    # in an arbitrary cwd on a server with no roots configured (issue #1 P2). Defaults false.
    allow_unlisted_workspaces_for_dev: bool = False
    agents: dict[str, AgentCfg] = Field(default_factory=dict)
    monitor: MonitorCfg = MonitorCfg()
    gates: GatesCfg = GatesCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    push: PushCfg = PushCfg()
    ui: UICfg = UICfg()
    autonomy: AutonomyCfg = AutonomyCfg()
    notify: NotifyCfg = NotifyCfg()

    # Populated from .env, not from config.yaml.
    secrets: Secrets = Field(default_factory=Secrets)


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load config.yaml (falling back to defaults if absent) and merge secrets from .env."""
    data: dict = {}
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = Config(**data)
    cfg.secrets = Secrets()  # re-read env/.env
    return cfg
