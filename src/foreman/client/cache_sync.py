"""Build display snapshots for the subscription-driven relay view.

First-screen snapshots are summary-only. Explicit session snapshots include that selected
session's stored timeline so team mode renders the same thread as the local UI.

In protocol v2 the server no longer stores a display cache. A subscribed PWA asks the local
process for a one-shot snapshot, then follows live relay events while the browser is online.

The whole point of the snapshot boundary (§8.3) is that only DISPLAY summaries leave the machine
— never full diffs, raw agent output, or 秘方. These builders are pure (take ORM rows, return
plain dicts) so what's shared is explicit and unit-testable: a session's goal/status/timestamps,
and a card's folded summary + the `diff_stat` *line* ("3 files +124/−80") — never the diff itself.
The full diff / raw return stays on the machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foreman.shared.config import remote_execution_enabled
from foreman.shared.protocol import KIND_CACHE_SYNC, KIND_SNAPSHOT, Envelope
from foreman.shared.autonomy import level_label, normalize_level
from foreman.shared.i18n import normalize as normalize_lang


def session_summary(session) -> dict:
    """A display-safe summary of one local session (no diffs/raw output — §8.3)."""
    workspace = getattr(session, "workspace", "") or ""
    main_workspace = getattr(session, "main_workspace", "") or workspace
    return {
        "session_id": session.id,
        "summary": {
            "goal": session.goal,
            "status": session.status,
            "agent_type": session.agent_type,
            "workspace": workspace,
            "main_workspace": main_workspace,
            "workspace_exists": bool(workspace and Path(workspace).expanduser().is_dir()),
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


def approval_summary(approval) -> dict:
    """A pending approval row, including its one-time nonce so the browser can decide it."""
    return {
        "id": approval.id,
        "session_id": approval.session_id,
        "task_id": approval.task_id,
        "action": approval.action,
        "risk_level": approval.risk_level,
        "diff_summary": approval.diff_summary,
        "status": approval.status,
        "reason": approval.reason,
        "nonce": approval.nonce,
        "requested_at": approval.requested_at,
        "decided_at": approval.decided_at,
    }


def report_summary(report) -> dict:
    return {
        "id": report.id,
        "session_id": report.session_id,
        "kind": report.kind,
        "title": report.title,
        "body_md": report.body_md,
        "sent": report.sent,
        "ts": report.ts,
    }


def definition_summary(definition) -> dict:
    """Definition rows are sent only in the live, on-demand snapshot and are not stored by relay."""
    return {
        "id": definition.id,
        "kind": definition.kind,
        "name": definition.name,
        "version": definition.version,
        "status": definition.status,
        "is_active": definition.is_active,
        "scope_json": definition.scope_json,
        "body": definition.body,
        "metadata_json": definition.metadata_json,
        "created_at": definition.created_at,
        "updated_at": definition.updated_at,
    }


def event_summary(event) -> dict:
    """A stored timeline event in the same JSON shape as the local timeline API."""
    try:
        payload = json.loads(getattr(event, "payload_json", "") or "{}")
    except (TypeError, ValueError):
        payload = {}
    return {
        "id": getattr(event, "id", ""),
        "session_id": getattr(event, "session_id", ""),
        "task_id": getattr(event, "task_id", None),
        "type": getattr(event, "type", ""),
        "source": getattr(event, "source", ""),
        "payload": payload,
        "ts": getattr(event, "ts", ""),
    }


def _setting(store: Any, key: str, default: str = "") -> str:
    get = getattr(store, "get_setting", None)
    if callable(get):
        value = get(key)
        if value is not None:
            return str(value)
    return default


def _json_setting(store: Any, key: str, fallback: Any) -> Any:
    raw = _setting(store, key, "")
    if raw:
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return fallback
    return fallback


def _model_dump(obj: Any) -> dict:
    if obj is None:
        return {}
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    if isinstance(obj, dict):
        return dict(obj)
    return {
        k: v for k, v in vars(obj).items()
        if not k.startswith("_") and isinstance(v, (str, int, bool, list, dict, type(None)))
    }


def _workspace_rows(store: Any, cfg: Any) -> list[dict]:
    fallback = [
        {"path": getattr(w, "path", ""), "name": getattr(w, "name", "")}
        for w in (getattr(cfg, "workspaces", []) if cfg is not None else [])
    ]
    rows = _json_setting(store, "workspaces.json", fallback)
    return [
        {"path": str((row or {}).get("path") or ""), "name": str((row or {}).get("name") or "")}
        for row in rows if isinstance(row, dict) and str(row.get("path") or "").strip()
    ]


def _agent_rows(store: Any, cfg: Any) -> list[dict]:
    agents = getattr(cfg, "agents", {}) if cfg is not None else {}
    fallback = [
        {"name": name, **_model_dump(agent)}
        for name, agent in sorted((agents or {}).items())
    ]
    rows = _json_setting(store, "agents.json", fallback)
    return [dict(row) for row in rows if isinstance(row, dict)]


def _pm_tools(store: Any, cfg: Any) -> dict:
    fallback = _model_dump(getattr(cfg, "pm_tools", None) if cfg is not None else None)
    data = _json_setting(store, "pm_tools.json", fallback)
    return dict(data) if isinstance(data, dict) else fallback


def _llm_settings(store: Any, cfg: Any) -> dict:
    llm = getattr(cfg, "llm", None) if cfg is not None else None
    secrets = getattr(cfg, "secrets", None) if cfg is not None else None
    provider = _setting(store, "llm.provider", "") or getattr(llm, "provider", "openai")
    model = _setting(store, "llm.model", "") or getattr(llm, "model", "")
    base_url = _setting(store, "llm.base_url", "") or getattr(llm, "base_url", "")
    transport = _setting(store, "llm.transport", "") or getattr(llm, "transport", "http")
    reasoning_effort = _setting(store, "llm.reasoning_effort", "") or getattr(llm, "reasoning_effort", "")
    try:
        timeout = int(_setting(store, "llm.request_timeout_s", str(getattr(llm, "request_timeout_s", 300))) or 300)
    except (TypeError, ValueError):
        timeout = 300
    try:
        context_window_tokens = int(_setting(store, "llm.context_window_tokens", str(getattr(llm, "context_window_tokens", 272000))) or 272000)
    except (TypeError, ValueError):
        context_window_tokens = 272000
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "transport": transport,
        "request_timeout_s": timeout,
        "context_window_tokens": context_window_tokens,
        "reasoning_effort": reasoning_effort,
        "api_key_set": bool(str(getattr(secrets, "llm_api_key", "") or "").strip()),
    }


def _debug_settings(store: Any, cfg: Any) -> dict:
    raw = _setting(store, "debug.llm_trace", "")
    if raw.strip():
        on = raw.strip().lower() in {"1", "true", "yes", "on"}
    else:
        on = bool(getattr(getattr(cfg, "debug", None), "llm_trace", False))
    return {"llm_trace": on}


def _cloud_settings(store: Any, cfg: Any) -> dict:
    server = getattr(cfg, "server", None) if cfg is not None else None
    secrets = getattr(cfg, "secrets", None) if cfg is not None else None
    return {
        "available": True,
        "url": _setting(store, "cloud.url", ""),
        "access_key_set": bool(str(getattr(secrets, "cloud_access_key", "") or "").strip()),
        "connected": False,
        "error": "",
        "remote_execution_enabled": remote_execution_enabled(
            store, bool(getattr(server, "remote_execution_enabled", False))
        ),
    }


def local_state_summary(store: Any = None, cfg: Any = None) -> dict:
    """Display state that must reflect the selected local machine in team mode."""
    ui = getattr(cfg, "ui", None) if cfg is not None else None
    autonomy = getattr(cfg, "autonomy", None) if cfg is not None else None
    lang = normalize_lang(_setting(store, "ui.language", getattr(ui, "language", "zh")))
    level = normalize_level(_setting(store, "autonomy.level", str(getattr(autonomy, "level", 1))))
    out: dict[str, Any] = {
        "workspaces": _workspace_rows(store, cfg),
        "agent_settings": _agent_rows(store, cfg),
        "pm_tools": _pm_tools(store, cfg),
        "llm": _llm_settings(store, cfg),
        "debug": _debug_settings(store, cfg),
        "cloud": _cloud_settings(store, cfg),
        "autonomy": {"level": level, "label": level_label(level, lang)},
        "language": lang,
    }
    if store is not None and hasattr(store, "get_pending_approvals"):
        out["approvals"] = [approval_summary(a) for a in store.get_pending_approvals()]
    if store is not None and hasattr(store, "get_reports"):
        out["reports"] = [report_summary(r) for r in store.get_reports(None)]
    if store is not None and hasattr(store, "get_definitions"):
        out["definitions"] = [definition_summary(d) for d in store.get_definitions()]
    return out


def build_cache_sync(sessions, cards) -> Envelope:
    """Legacy v1 helper. Retained for compatibility; v2 uses ``build_snapshot`` on demand."""
    return Envelope(
        kind=KIND_CACHE_SYNC,
        payload={
            "sessions": [session_summary(s) for s in sessions],
            "cards": [card_summary(c) for c in cards],
        },
    )


def build_snapshot(
    sessions,
    cards,
    *,
    corr_id: str = "",
    store: Any = None,
    cfg: Any = None,
    session_id: str = "",
) -> Envelope:
    """Assemble an on-demand display snapshot for a subscribed browser."""
    env = build_cache_sync(sessions, cards)
    env.kind = KIND_SNAPSHOT
    env.id = corr_id
    env.payload.update(local_state_summary(store, cfg))
    selected = str(session_id or "").strip()
    if selected and store is not None and hasattr(store, "get_events"):
        env.payload["session_id"] = selected
        env.payload["events"] = [event_summary(e) for e in store.get_events(selected)]
    return env
