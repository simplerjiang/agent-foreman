"""Load + seed the built-in starter 秘方 examples (T6.4, DESIGN §11.2C / §765).

The repo ships a tiny, **generic, redacted** set of example definitions (under
``foreman.examples/definitions``) so OSS users get a working, editable library to start from —
``foreman.db`` itself never enters git, so without these a fresh install would have an empty
definition library. This module reads those shipped files and seeds them into a local Store.

It is **client-side core**: the 秘方 (definitions) live ONLY in the local process, never the shared
server (DESIGN §8.3 / §14). Bodies are only ever read as data and stored — never executed (the
workflow engine parses them with ``yaml.safe_load``, T5.2).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from importlib.resources import files

from foreman.shared.events import utc_now_iso

from ..store.models import Definition
from .definition_service import KNOWN_KINDS

# Where the shipped example files live (a data package, see foreman/examples/__init__.py).
EXAMPLES_PACKAGE = "foreman.examples"
_DEFINITIONS_DIR = "definitions"
_MANIFEST = "manifest.yaml"


@dataclass(frozen=True)
class ExampleDefinition:
    """One shipped example, resolved from the manifest + its body file (ready to seed as a row)."""

    kind: str
    name: str
    body: str
    scope_json: str = "{}"
    metadata_json: str = "{}"


def _safe_rel(rel: str) -> bool:
    """True iff ``rel`` is a safe, relative path under the examples dir (defense-in-depth).

    The manifest is trusted, shipped data, but never resolve a ``..`` / absolute / drive segment —
    so even a future user-supplied manifest can't read outside ``definitions/``."""
    if not rel:
        return False
    segments = rel.split("/")
    return not any(s in ("", "..") or ":" in s or "\\" in s for s in segments)


def _read_text(*parts: str) -> str:
    """Read a shipped example file by path segments (works for both source + wheel installs)."""
    node = files(EXAMPLES_PACKAGE).joinpath(_DEFINITIONS_DIR)
    for part in parts:
        node = node.joinpath(part)
    return node.read_text(encoding="utf-8")


def load_example_definitions() -> list[ExampleDefinition]:
    """Parse the manifest and resolve every shipped example to an :class:`ExampleDefinition`.

    Fail-closed on a bad manifest entry: an unknown ``kind`` or a missing ``name``/``file`` is
    skipped (the built-in data is trusted, but this keeps a typo from seeding an orphan kind nothing
    reads). Every example is tagged ``metadata.example = true`` so it's distinguishable from your own.
    """
    import yaml

    manifest = yaml.safe_load(_read_text(_MANIFEST)) or {}
    entries = manifest.get("definitions", []) if isinstance(manifest, dict) else []
    out: list[ExampleDefinition] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind", "")).strip()
        name = str(entry.get("name", "")).strip()
        rel = str(entry.get("file", "")).strip()
        if kind not in KNOWN_KINDS or not name or not _safe_rel(rel):
            continue
        body = _read_text(*rel.split("/"))
        scope = entry.get("scope") or {}
        metadata = {"example": True, **(entry.get("metadata") or {})}
        out.append(
            ExampleDefinition(
                kind=kind,
                name=name,
                body=body,
                scope_json=json.dumps(scope, ensure_ascii=False),
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        )
    return out


def seed_examples(store: object, *, activate: bool = True, clock=utc_now_iso) -> dict:
    """Seed the shipped examples into ``store`` — **idempotent** (DESIGN §11.2C / §765).

    For each example: if **any** version of (kind, name) already exists it's left untouched (skip —
    don't clobber the user's own edits / their archived copies); otherwise version 1 is inserted and,
    when ``activate`` is True, made the live version. Safe to re-run in any mode — a second call seeds
    nothing (the skip is independent of activation, so ``activate=False`` re-runs don't duplicate).

    Returns ``{"added": [...], "skipped": [...]}`` (each entry ``"kind/name"``).
    """
    added: list[str] = []
    skipped: list[str] = []
    for ex in load_example_definitions():
        label = f"{ex.kind}/{ex.name}"
        if _exists(store, ex.kind, ex.name):
            skipped.append(label)
            continue
        row = Definition(
            id=uuid.uuid4().hex,
            kind=ex.kind,
            name=ex.name,
            version=1,  # only inserted when no (kind, name) exists, so v1 is always free
            status="draft",
            scope_json=ex.scope_json,
            body=ex.body,
            metadata_json=ex.metadata_json,
            created_at=clock(),
        )
        try:
            store.add_definition(row)
        except ValueError:  # backstop: a (kind, name, version) collision — never clobber, just skip
            skipped.append(label)
            continue
        if activate and hasattr(store, "set_definition_active"):
            store.set_definition_active(row.id)
        added.append(label)
    return {"added": added, "skipped": skipped}


def _exists(store: object, kind: str, name: str) -> bool:
    """True iff any version of (kind, name) is already in the store (idempotency guard)."""
    if not hasattr(store, "get_definitions"):
        return False
    return bool(store.get_definitions(kind=kind, name=name))


__all__ = ["ExampleDefinition", "load_example_definitions", "seed_examples", "EXAMPLES_PACKAGE"]
