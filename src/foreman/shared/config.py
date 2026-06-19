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


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    public_base_url: str = ""


class LLMCfg(BaseModel):
    provider: str = "openai"  # "openai" | "anthropic"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    request_timeout_s: int = 60
    max_tokens: int = 2048


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


class GatesCfg(BaseModel):
    requires_approval: list[str] = Field(default_factory=list)
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


class Config(BaseModel):
    server: ServerCfg = ServerCfg()
    llm: LLMCfg = LLMCfg()
    store: StoreCfg = StoreCfg()
    workspaces: list[WorkspaceCfg] = Field(default_factory=list)
    agents: dict[str, AgentCfg] = Field(default_factory=dict)
    monitor: MonitorCfg = MonitorCfg()
    gates: GatesCfg = GatesCfg()
    schedule: ScheduleCfg = ScheduleCfg()
    push: PushCfg = PushCfg()
    ui: UICfg = UICfg()

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
