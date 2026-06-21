"""Definition editor service — CRUD for the four 秘方 building blocks (DESIGN §11.2, T6.1).

The four kinds (workflow | skill | code_standard | qa_rubric) all live in one ``definitions``
table (§11.2C: a fifth kind needs no schema change, just one more entry in ``KNOWN_KINDS``). This
service lets you **add / edit / delete** them straight from the phone/web UI — "数据库就是唯一真相源,
不碰文件、不用重新部署" (§11.2C). Versioning (enable / disable / rollback) rides on the store helpers
from T5.1: each (kind, name) keeps a version history and exactly one is live at a time.

Like the Gate / CardService this is **client-side core**, INJECTED into ``server.app.create_app``
as ``definitions`` so app.py stays shared-only and the 秘方 never leave the local process — the
shared server库 **never** holds definitions (DESIGN §8.3 / §14). Bodies are only ever stored and
read back, **never executed** (the workflow engine parses them with ``yaml.safe_load``, T5.2).
"""

from __future__ import annotations

import json
import uuid

from foreman.shared.events import make_event, utc_now_iso

from ..store.models import Definition

# The registered kinds the editor offers. Extending Foreman with a fifth kind is a one-line change
# here (no schema migration) — exactly the §11.2C extensibility promise. Unknown kinds are rejected
# so a typo can't quietly create an orphan kind nothing reads.
KNOWN_KINDS: frozenset[str] = frozenset({"workflow", "skill", "code_standard", "qa_rubric"})

# Editable status values (identity — kind/name/version — is not editable; make a new version).
KNOWN_STATUS: frozenset[str] = frozenset({"draft", "active", "archived"})

MAX_NAME = 200          # a name is an identifier, not prose
MAX_BODY = 200_000      # generous body cap so a huge paste can't bloat the local DB / a payload


def _definition_to_dict(d: Definition) -> dict:
    """A Definition row → JSON-friendly dict for the API / UI editor."""
    return {
        "id": d.id,
        "kind": d.kind,
        "name": d.name,
        "version": d.version,
        "status": d.status,
        "is_active": d.is_active,
        "scope_json": d.scope_json,
        "body": d.body,
        "metadata_json": d.metadata_json,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


def _valid_json_object(text: str) -> bool:
    """True iff `text` parses to a JSON object ({}). scope/metadata are objects by design (§7.1)."""
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, TypeError):
        return False


class DefinitionService:
    """Create / list / read / update / activate / delete definitions (the UI editor, §11.2).

    ``store`` is the local client Store (it owns the definitions table). ``bus`` (optional) gets a
    persist-first ``definition`` event on every mutation, as a cheap audit trail (§7.1). All inputs
    are validated fail-closed: unknown kind, blank name, oversize body, or non-object scope/metadata
    JSON are rejected before anything is written.
    """

    def __init__(self, store: object, *, bus: object | None = None, clock=None) -> None:
        self.store = store
        self.bus = bus
        self._clock = clock or utc_now_iso

    # ── read ─────────────────────────────────────────────────────────────────────────────────
    def list_definitions(
        self, *, kind: str | None = None, name: str | None = None, active_only: bool = False
    ) -> list[dict]:
        """Definitions as JSON-friendly dicts (ordered kind→name→version). Caller: app.py (shared)."""
        if self.store is None or not hasattr(self.store, "get_definitions"):
            return []
        rows = self.store.get_definitions(kind=kind, name=name, active_only=active_only)
        return [_definition_to_dict(d) for d in rows]

    def get_definition(self, definition_id: str) -> dict | None:
        """One definition (the editor opens it to edit its body). None → route maps to 404."""
        if self.store is None or not hasattr(self.store, "get_definition"):
            return None
        row = self.store.get_definition(definition_id)
        return _definition_to_dict(row) if row is not None else None

    # ── create ───────────────────────────────────────────────────────────────────────────────
    async def create_definition(
        self,
        *,
        kind: str,
        name: str,
        body: str = "",
        scope_json: str = "{}",
        metadata_json: str = "{}",
        version: int | None = None,
        activate: bool = True,
    ) -> dict:
        """Create a new definition / a new version of an existing one (the 增 path, §11.2).

        ``version`` defaults to the next free version for this (kind, name) so the UI never trips the
        store's per-(kind,name,version) uniqueness rule. ``activate`` (default True) makes the new
        version THE live one for its (kind, name) — every sibling is deactivated (§11.2 rollback knob).
        Returns {"ok": True, "definition": {...}} or {"ok": False, "error": ...} with error ∈
        {bad_kind, bad_name, body_too_large, bad_scope_json, bad_metadata_json, version_exists, no_store}.
        """
        err = self._validate(kind=kind, name=name, body=body, scope=scope_json, meta=metadata_json)
        if err:
            return {"ok": False, "error": err}
        if self.store is None or not hasattr(self.store, "add_definition"):
            return {"ok": False, "error": "no_store"}
        name = name.strip()
        if version is None:
            version = self._next_version(kind, name)
        row = Definition(
            id=uuid.uuid4().hex,
            kind=kind,
            name=name,
            version=version,
            status="draft",
            scope_json=scope_json or "{}",
            body=body,
            metadata_json=metadata_json or "{}",
        )
        try:
            self.store.add_definition(row)
        except ValueError:  # duplicate (kind, name, version) — caller should bump the version
            return {"ok": False, "error": "version_exists"}
        if activate and hasattr(self.store, "set_definition_active"):
            self.store.set_definition_active(row.id)
            row = self.store.get_definition(row.id) or row
        await self._emit("created", row)
        return {"ok": True, "definition": _definition_to_dict(row)}

    def _next_version(self, kind: str, name: str) -> int:
        """The next free version for (kind, name): max existing + 1, else 1."""
        if not hasattr(self.store, "get_definitions"):
            return 1
        existing = self.store.get_definitions(kind=kind, name=name)
        return max((d.version for d in existing), default=0) + 1

    # ── update ───────────────────────────────────────────────────────────────────────────────
    async def update_definition(
        self,
        definition_id: str,
        *,
        body: str | None = None,
        scope_json: str | None = None,
        metadata_json: str | None = None,
        status: str | None = None,
    ) -> dict:
        """Edit a definition in place (the 改 path, §11.2). Only the passed fields change; identity
        (kind/name/version) is never editable here — make a new version instead. Returns
        {"ok": True, "definition": {...}} or {"ok": False, "error": ...} with error ∈
        {body_too_large, bad_scope_json, bad_metadata_json, bad_status, not_found, no_store}.
        """
        if body is not None and len(body) > MAX_BODY:
            return {"ok": False, "error": "body_too_large"}
        if scope_json is not None and not _valid_json_object(scope_json):
            return {"ok": False, "error": "bad_scope_json"}
        if metadata_json is not None and not _valid_json_object(metadata_json):
            return {"ok": False, "error": "bad_metadata_json"}
        if status is not None and status not in KNOWN_STATUS:
            return {"ok": False, "error": "bad_status"}
        if self.store is None or not hasattr(self.store, "update_definition"):
            return {"ok": False, "error": "no_store"}
        row = self.store.update_definition(
            definition_id,
            body=body,
            scope_json=scope_json,
            metadata_json=metadata_json,
            status=status,
        )
        if row is None:
            return {"ok": False, "error": "not_found"}
        await self._emit("updated", row)
        return {"ok": True, "definition": _definition_to_dict(row)}

    # ── activate (enable / rollback) ───────────────────────────────────────────────────────────
    async def activate_definition(self, definition_id: str) -> dict:
        """Make this version THE live one for its (kind, name) — the enable/rollback knob (§11.2).
        Every sibling version is deactivated. Returns {"ok": True, "definition": {...}} or
        {"ok": False, "error": ...} with error ∈ {not_found, no_store}."""
        if self.store is None or not hasattr(self.store, "set_definition_active"):
            return {"ok": False, "error": "no_store"}
        row = self.store.set_definition_active(definition_id)
        if row is None:
            return {"ok": False, "error": "not_found"}
        await self._emit("activated", row)
        return {"ok": True, "definition": _definition_to_dict(row)}

    # ── delete ───────────────────────────────────────────────────────────────────────────────
    async def delete_definition(self, definition_id: str) -> dict:
        """Delete a definition + its links (the 删 path, §11.2). Returns {"ok": True, "id"} or
        {"ok": False, "error": ...} with error ∈ {not_found, no_store}."""
        if self.store is None or not hasattr(self.store, "delete_definition"):
            return {"ok": False, "error": "no_store"}
        # Capture identity before deletion so the audit event can name what went away.
        row = self.store.get_definition(definition_id) if hasattr(
            self.store, "get_definition"
        ) else None
        removed = self.store.delete_definition(definition_id)
        if not removed:
            return {"ok": False, "error": "not_found"}
        await self._emit_deleted(definition_id, row)
        return {"ok": True, "id": definition_id}

    # ── validation + events ────────────────────────────────────────────────────────────────────
    def _validate(self, *, kind: str, name: str, body: str, scope: str, meta: str) -> str | None:
        """Fail-closed input checks shared by create. Returns an error code, or None if ok."""
        if kind not in KNOWN_KINDS:
            return "bad_kind"
        if not name or not name.strip() or len(name.strip()) > MAX_NAME:
            return "bad_name"
        if len(body) > MAX_BODY:
            return "body_too_large"
        if not _valid_json_object(scope or "{}"):
            return "bad_scope_json"
        if not _valid_json_object(meta or "{}"):
            return "bad_metadata_json"
        return None

    async def _emit(self, action: str, row: Definition) -> None:
        """Persist-then-publish a `definition` audit event (mirrors Gate/CardService).

        Metadata only — the body (your 秘方) never goes on the bus. session_id is empty because the
        editor is global, not session-scoped."""
        event = make_event(
            "definition",
            "definitions",
            "",
            payload={
                "action": action,
                "id": row.id,
                "kind": row.kind,
                "name": row.name,
                "version": row.version,
                "is_active": row.is_active,
            },
        )
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)

    async def _emit_deleted(self, definition_id: str, row: Definition | None) -> None:
        event = make_event(
            "definition",
            "definitions",
            "",
            payload={
                "action": "deleted",
                "id": definition_id,
                "kind": getattr(row, "kind", ""),
                "name": getattr(row, "name", ""),
                "version": getattr(row, "version", None),
            },
        )
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)


__all__ = ["DefinitionService", "KNOWN_KINDS", "KNOWN_STATUS"]
