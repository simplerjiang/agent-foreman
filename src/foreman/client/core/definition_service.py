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
from typing import Any

from foreman.shared.crypto import BodyCipher
from foreman.shared.events import make_event, utc_now_iso

from ..store.models import Definition, DefinitionLink

# The registered kinds the editor offers. Extending Foreman with a fifth kind is a one-line change
# here (no schema migration) — exactly the §11.2C extensibility promise. Unknown kinds are rejected
# so a typo can't quietly create an orphan kind nothing reads.
KNOWN_KINDS: frozenset[str] = frozenset({"workflow", "skill", "code_standard", "qa_rubric"})

# Editable status values (identity — kind/name/version — is not editable; make a new version).
KNOWN_STATUS: frozenset[str] = frozenset({"draft", "active", "archived"})

MAX_NAME = 200          # a name is an identifier, not prose
MAX_BODY = 200_000      # generous body cap so a huge paste can't bloat the local DB / a payload
MAX_DESCRIPTION = 1024  # metadata.description cap — the L0 selection signal (DESIGN §4.2 / P0)

# Backup-bundle envelope (export/import, T6.2). Tagging the format + a schema version lets `import`
# reject foreign / future files fail-closed instead of silently mangling them.
BUNDLE_FORMAT = "foreman-definitions"
BUNDLE_VERSION = 1
MAX_IMPORT_DEFINITIONS = 10_000   # bound an import so a crafted bundle can't exhaust memory/DB


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


def _link_to_dict(link: DefinitionLink) -> dict:
    """A DefinitionLink row → JSON-friendly dict (export only, carries no 秘方 body)."""
    return {
        "id": link.id,
        "from_id": link.from_id,
        "to_id": link.to_id,
        "relation": link.relation,
        "step_index": link.step_index,
    }


def _valid_json_object(text: str) -> bool:
    """True iff `text` parses to a JSON object ({}). scope/metadata are objects by design (§7.1)."""
    try:
        return isinstance(json.loads(text), dict)
    except (ValueError, TypeError):
        return False


def _json_object_or_default(text: object) -> str:
    """Return `text` if it's a valid JSON object string, else "{}" — sanitizes imported scope/metadata
    so a malformed bundle can't persist a non-object that later json.loads readers choke on."""
    return text if isinstance(text, str) and _valid_json_object(text) else "{}"


def _description_error(meta: str) -> str | None:
    """Require a non-empty ``metadata.description`` ≤ :data:`MAX_DESCRIPTION` — the L0 selection
    signal (DESIGN §4.2/§4.3). Returns "missing_description" / "description_too_long", or None if ok.

    This is the **write-time gate** for create/update only. It is deliberately NOT applied on the
    import path (``import_bundle`` keeps the lenient ``_json_object_or_default`` route) nor on
    ``seed_examples`` (which inserts via ``store.add_definition``), so existing/imported rows stay
    readable and re-imports stay idempotent (§4.3 铁律 / §12 backward-compat). "无 description 不进
    自动选择" for existing rows is handled separately by the resolver's exclusion (work_mode_context).
    """
    try:
        obj = json.loads(meta or "{}")
    except (ValueError, TypeError):
        return "bad_metadata_json"
    desc = obj.get("description") if isinstance(obj, dict) else None
    if not (isinstance(desc, str) and desc.strip()):
        return "missing_description"
    if len(desc) > MAX_DESCRIPTION:
        return "description_too_long"
    return None


class DefinitionService:
    """Create / list / read / update / activate / delete definitions (the UI editor, §11.2).

    ``store`` is the local client Store (it owns the definitions table). ``bus`` (optional) gets a
    persist-first ``definition`` event on every mutation, as a cheap audit trail (§7.1). All inputs
    are validated fail-closed: unknown kind, blank name, oversize body, or non-object scope/metadata
    JSON are rejected before anything is written.
    """

    def __init__(
        self, store: Any, *, bus: Any = None, clock=None, cipher: BodyCipher | None = None
    ) -> None:
        self.store = store
        self.bus = bus
        self._clock = clock or utc_now_iso
        # Optional cipher for encrypting an EXPORT bundle (carry recipes between machines without
        # leaking them, §765) and decrypting an encrypted bundle on IMPORT. Independent of the
        # store's own at-rest cipher — None here just means export defaults to plaintext.
        self._cipher = cipher

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
        {bad_kind, bad_name, body_too_large, bad_scope_json, bad_metadata_json, missing_description,
        description_too_long, version_exists, no_store}.
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
        {body_too_large, bad_scope_json, bad_metadata_json, missing_description, description_too_long,
        bad_status, not_found, no_store}.
        """
        if body is not None and len(body) > MAX_BODY:
            return {"ok": False, "error": "body_too_large"}
        if scope_json is not None and not _valid_json_object(scope_json):
            return {"ok": False, "error": "bad_scope_json"}
        if metadata_json is not None and not _valid_json_object(metadata_json):
            return {"ok": False, "error": "bad_metadata_json"}
        # Apply the description gate only when this PATCH actually carries metadata_json — a body-only
        # edit must not be forced to re-supply a description (§4.3 / P0 task 5).
        if metadata_json is not None:
            desc_err = _description_error(metadata_json)
            if desc_err:
                return {"ok": False, "error": desc_err}
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

    # ── export / import (backup, T6.2) ─────────────────────────────────────────────────────────
    def export_bundle(self, *, encrypt: bool = False) -> dict:
        """Dump every definition + its wiring into one JSON-friendly backup bundle (§11.2C/§765).

        Bodies come out **plaintext** by default (the store decrypts on read, so at-rest encryption
        is transparent here). Pass ``encrypt=True`` to encrypt each body with the configured cipher
        so the bundle can be carried between machines without leaking the 秘方 — requires a cipher
        (else ``{"ok": False, "error": "no_cipher"}``). Links carry only ids/relations, never a body,
        so they are always safe to serialize.
        """
        if self.store is None or not hasattr(self.store, "get_definitions"):
            return {"ok": False, "error": "no_store"}
        if encrypt and self._cipher is None:
            return {"ok": False, "error": "no_cipher"}
        defs = []
        for d in self.store.get_definitions():
            row = _definition_to_dict(d)
            if encrypt:
                assert self._cipher is not None  # guarded by the no_cipher check above
                row["body"] = self._cipher.encrypt(row["body"])
            defs.append(row)
        links = []
        if hasattr(self.store, "get_all_definition_links"):
            links = [_link_to_dict(link) for link in self.store.get_all_definition_links()]
        return {
            "ok": True,
            "bundle": {
                "format": BUNDLE_FORMAT,
                "version": BUNDLE_VERSION,
                "exported_at": self._clock(),
                "encrypted": bool(encrypt),
                "definitions": defs,
                "links": links,
            },
        }

    async def import_bundle(self, bundle: object) -> dict:
        """Restore definitions + links from a backup bundle (T6.2). Merge semantics: a definition
        whose id already exists, or whose (kind, name, version) already exists, is **skipped** — so
        re-importing the same bundle is idempotent and never clobbers live recipes. A link is added
        only when its id is new and both endpoints resolve (dangling links are dropped).

        Encrypted bundles are decrypted with the configured cipher; a tagged body with no/wrong key
        fails closed (``error`` ∈ {needs_key, bad_key}) rather than storing unreadable ciphertext.
        Returns counts: {"ok": True, "imported", "skipped", "links_imported", "links_skipped"}.
        Errors: {bad_bundle, bad_format, unsupported_version, too_large, needs_key, bad_key, no_store}.
        """
        if self.store is None or not hasattr(self.store, "add_definition"):
            return {"ok": False, "error": "no_store"}
        if not isinstance(bundle, dict):
            return {"ok": False, "error": "bad_bundle"}
        if bundle.get("format") != BUNDLE_FORMAT:
            return {"ok": False, "error": "bad_format"}
        if bundle.get("version") != BUNDLE_VERSION:
            return {"ok": False, "error": "unsupported_version"}
        defs = bundle.get("definitions")
        links = bundle.get("links", [])
        if not isinstance(defs, list) or not isinstance(links, list):
            return {"ok": False, "error": "bad_bundle"}
        if len(defs) > MAX_IMPORT_DEFINITIONS:
            return {"ok": False, "error": "too_large"}

        existing_ids = {d.id for d in self.store.get_definitions()}
        imported = skipped = 0
        active_ids: list[str] = []  # enforce "exactly one live per (kind,name)" AFTER inserting
        for raw in defs:
            if not isinstance(raw, dict) or raw.get("kind") not in KNOWN_KINDS:
                skipped += 1
                continue
            rid = raw.get("id")
            if not rid or rid in existing_ids:
                skipped += 1
                continue
            body = raw.get("body", "") or ""
            if BodyCipher.is_encrypted(body):
                if self._cipher is None:
                    return {"ok": False, "error": "needs_key"}
                try:
                    body = self._cipher.decrypt(body)
                except Exception:  # InvalidToken / wrong key — fail closed, don't store garbage
                    return {"ok": False, "error": "bad_key"}
            if len(body) > MAX_BODY:  # same cap as the create path — a crafted bundle can't bloat the DB
                skipped += 1
                continue
            row = Definition(
                id=rid,
                kind=raw["kind"],
                name=str(raw.get("name", ""))[:MAX_NAME],
                version=int(raw.get("version", 1) or 1),
                # sanitize fail-closed: an unknown status / non-object JSON can't slip in and break readers
                status=raw["status"] if raw.get("status") in KNOWN_STATUS else "draft",
                is_active=bool(raw.get("is_active", False)),
                scope_json=_json_object_or_default(raw.get("scope_json")),
                body=body,
                metadata_json=_json_object_or_default(raw.get("metadata_json")),
                created_at=raw.get("created_at", "") or "",
                updated_at=raw.get("updated_at", "") or "",
            )
            try:
                self.store.add_definition(row)
            except ValueError:  # duplicate (kind, name, version) — skip, never clobber
                skipped += 1
                continue
            existing_ids.add(rid)
            if row.is_active:
                active_ids.append(rid)
            imported += 1

        # Re-assert the single-live invariant: add_definition inserts is_active verbatim, so an
        # imported active version could collide with an already-live sibling. Routing each through
        # set_definition_active deactivates every other version of its (kind, name) — exactly one live.
        if hasattr(self.store, "set_definition_active"):
            for rid in active_ids:
                self.store.set_definition_active(rid)

        links_imported, links_skipped = self._import_links(links)
        await self._emit_bundle(imported, links_imported)
        return {
            "ok": True,
            "imported": imported,
            "skipped": skipped,
            "links_imported": links_imported,
            "links_skipped": links_skipped,
        }

    def _import_links(self, links: list) -> tuple[int, int]:
        """Add new links whose endpoints resolve; skip duplicates and dangling ones."""
        if not hasattr(self.store, "add_definition_link"):
            return 0, len(links)
        def_ids = {d.id for d in self.store.get_definitions()}
        existing_link_ids: set[str] = set()
        if hasattr(self.store, "get_all_definition_links"):
            existing_link_ids = {link.id for link in self.store.get_all_definition_links()}
        imported = skipped = 0
        for raw in links:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            lid, from_id, to_id = raw.get("id"), raw.get("from_id"), raw.get("to_id")
            if (
                not lid
                or lid in existing_link_ids
                or from_id not in def_ids
                or to_id not in def_ids
            ):
                skipped += 1
                continue
            step = raw.get("step_index")
            self.store.add_definition_link(
                DefinitionLink(
                    id=lid,
                    from_id=from_id,
                    to_id=to_id,
                    relation=str(raw.get("relation", "")),
                    step_index=int(step) if isinstance(step, int) else None,
                )
            )
            existing_link_ids.add(lid)
            imported += 1
        return imported, skipped

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
        # description gate LAST so the negative checks above keep returning their own codes (§4.3).
        return _description_error(meta or "{}")

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

    async def _emit_bundle(self, imported: int, links_imported: int) -> None:
        """Persist-then-publish a `definition` audit event for an import (metadata only — counts,
        never any body)."""
        event = make_event(
            "definition",
            "definitions",
            "",
            payload={
                "action": "imported",
                "imported": imported,
                "links_imported": links_imported,
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


__all__ = [
    "DefinitionService",
    "KNOWN_KINDS",
    "KNOWN_STATUS",
    "BUNDLE_FORMAT",
    "BUNDLE_VERSION",
]
