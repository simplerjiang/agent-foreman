"""Configuration loading: config.yaml (structure) + .env (secrets).

Secrets never live in config.yaml. They are read from environment / .env with the
FOREMAN_ prefix (see .env.example).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PM_TOOLS_MIN_ROUNDS = 1
PM_TOOLS_DEFAULT_ROUNDS = 6


def clamp_pm_tool_rounds(value: Any) -> int:
    try:
        rounds = int(value)
    except (TypeError, ValueError):
        rounds = PM_TOOLS_DEFAULT_ROUNDS
    return max(PM_TOOLS_MIN_ROUNDS, rounds)


class Secrets(BaseSettings):
    """Secret values, sourced from environment / .env (FOREMAN_ prefix)."""

    model_config = SettingsConfigDict(env_prefix="FOREMAN_", env_file=".env", extra="ignore")

    llm_api_key: str = ""
    vapid_private_key: str = ""
    auth_token: str = ""
    # Access key the local process uses to dial the team relay 总机 (DESIGN §8.5 ①). One per
    # machine, minted at the relay's /keys.html, individually revocable. Stays local — the relay
    # only ever sees its hash. Empty = this machine isn't linked to a cloud relay.
    cloud_access_key: str = ""
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
    # Content-Security-Policy for the PWA. script-src stays locked to 'self' (no inline script —
    # the Ant Design console is plain first-party JS using vendored React/antd UMD bundles served
    # from /vendor, so only first-party code runs). style-src adds 'unsafe-inline' because Ant
    # Design v5 injects its component styles as runtime <style> tags (CSS-in-JS) and inline style=
    # attributes — without it the console renders unstyled under the hardening header. Inline STYLE
    # (not script) is low-risk: it can't execute code. connect-src lists ws:/wss: explicitly: the
    # live timeline opens a same-origin WebSocket and not every browser treats `'self'` as covering
    # the ws/wss schemes. Empty string disables the header (issue #1 P2).
    csp: str = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; base-uri 'none'; frame-ancestors 'none'"
    )
    # Include the DB path in /health. Off by default so the public readiness probe doesn't leak the
    # deployment's filesystem layout (issue #1 P2).
    health_show_db: bool = False
    # Remote commands are the highest-risk surface: a browser asks a local process to run work.
    # Default OFF. Snapshot/presence/notification traffic still works while this breaker is off.
    remote_execution_enabled: bool = False


class LLMCfg(BaseModel):
    provider: str = "openai"  # "openai" | "anthropic"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    request_timeout_s: int = 300
    max_tokens: int = 2048
    # Optional PM-brain reasoning effort for providers that require an explicit knob.
    # Empty means "do not send a provider-specific parameter".
    reasoning_effort: str = ""
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
    model: str = ""  # Optional model for the driven CLI agent; empty = CLI default.
    # Default reasoning level / 速度档位 for this agent: low | medium | high ("" = CLI default).
    # A per-dispatch override (phone/web) wins over this. claude maps it to CLAUDE_CODE_EFFORT_LEVEL;
    # codex to `-c model_reasoning_effort=` (DESIGN §4.2).
    effort: str = ""
    # Let the coding CLI use its built-in file, shell, and network/search tools without pausing on
    # every permission prompt. The workspace allowlist still controls where Foreman may launch it.
    full_access: bool = True


def default_agents() -> dict[str, AgentCfg]:
    """Local exe defaults: usable even when no config.yaml is found beside the process."""
    return {
        "claude-code": AgentCfg(command="claude", enabled=True, mode="headless"),
        "codex": AgentCfg(command="codex", enabled=True, mode="headless"),
        "copilot-cli": AgentCfg(
            command="copilot",
            enabled=False,
            mode="headless",
            model="",
            effort="high",
            full_access=True,
        ),
    }


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


class PMToolsCfg(BaseModel):
    file_read: bool = True
    file_write: bool = False
    shell: bool = False
    web_fetch: bool = False
    web_search: bool = False
    browser: bool = False
    allowed_commands: list[str] = Field(default_factory=lambda: ["python --version"])
    allowed_origins: list[str] = Field(default_factory=list)
    web_search_provider: str = "duckduckgo"  # duckduckgo | searxng
    searxng_url: str = ""
    browser_headless: bool = False
    max_rounds: int = PM_TOOLS_DEFAULT_ROUNDS

    @field_validator("max_rounds")
    @classmethod
    def _clamp_max_rounds(cls, value: int) -> int:
        return clamp_pm_tool_rounds(value)


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


class WorkModeCfg(BaseModel):
    """Work-mode semantic-retrieval switches (P3 §3.5). Default OFF → pure lexical (P0/P1 behavior),
    byte-for-byte unchanged unless explicitly enabled."""

    # off = lexical only; auto = use embeddings if a channel works else lexical; on = force (still
    # falls back to lexical on failure).
    semantic_search: str = "off"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 256  # local fallback embedder dimension

    @field_validator("semantic_search", mode="before")
    @classmethod
    def _coerce_semantic_search(cls, v: object) -> object:
        # YAML parses bare on/off/yes/no as bools, so `semantic_search: on` in config.yaml arrives as
        # True. Map bool → the string form this field expects (on→"on", off→"off"); strings (incl.
        # "auto") pass through untouched.
        if isinstance(v, bool):
            return "on" if v else "off"
        return v


class DebugCfg(BaseModel):
    """Debug/observability switches (work-mode P1b-trace, §8C). All default OFF / safe."""

    # LLM request/response PLAINTEXT trace to disk. Default False. ON writes the FULL conversation
    # (incl. user source + decrypted 秘方) to log_dir — local-only, never uploaded, git-excluded.
    llm_trace: bool = False
    # Trace root dir (process-local). Relative paths resolve against the config file's directory.
    log_dir: str = ".foreman/debug"
    # Rotation/retention [defaults 2026-06-24]: ≤50 MB per file, keep latest 20 OR 14 days (whichever
    # prunes first). All config-overridable.
    llm_trace_max_bytes: int = 50 * 1024 * 1024
    llm_trace_keep: int = 20
    llm_trace_keep_days: int = 14


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
    pm_tools: PMToolsCfg = PMToolsCfg()
    notify: NotifyCfg = NotifyCfg()
    debug: DebugCfg = DebugCfg()
    work_mode: WorkModeCfg = WorkModeCfg()

    # Populated from .env, not from config.yaml.
    secrets: Secrets = Field(default_factory=Secrets)
    # Runtime bookkeeping: where load_config read structure/secrets from. Used by the local UI when
    # it writes secrets back to the same sibling .env file.
    config_path: str = ""
    env_path: str = ""


# config_kv key for the per-machine remote-execution breaker (DESIGN §8.5). Stored as "1"/"0" so the
# machine owner can toggle 「允许远端执行」 live from the local Settings → 云端连接 card (no restart).
REMOTE_EXEC_SETTING = "cloud.remote_execution_enabled"


def remote_execution_enabled(store: object, default: bool) -> bool:
    """Effective remote-command breaker. A config_kv override (toggled live from the local UI) wins
    over the cfg baseline (ServerCfg.remote_execution_enabled, default OFF). Shared by the client
    gate (cloud.py, which decides whether to run an inbound command) and the settings API
    (server/app.py, which reports + persists the flag) so the key + truthiness parse never drift
    across the §14 boundary. Read per command, so a flip takes effect without a restart."""
    get = getattr(store, "get_setting", None)
    if callable(get):
        raw = get(REMOTE_EXEC_SETTING)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return bool(default)


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load config.yaml (falling back to defaults if absent) and merge secrets from .env."""
    data: dict = {}
    p = Path(path)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = Config(**data)
    if not cfg.agents:
        cfg.agents = default_agents()
    cfg.config_path = str(p)
    cfg.env_path = str(p.with_name(".env"))
    cfg.secrets = Secrets(_env_file=cfg.env_path)  # type: ignore[call-arg]  # pydantic-settings
    # env→config glue: only Secrets is env-driven; the structured Config segments are not. Let
    # FOREMAN_DEBUG_LLM_TRACE flip the debug switch (env wins over yaml — "turn it on right now").
    _env_trace = os.environ.get("FOREMAN_DEBUG_LLM_TRACE")
    if _env_trace is not None:
        cfg.debug.llm_trace = _env_trace.strip().lower() in {"1", "true", "yes", "on"}
    return cfg
