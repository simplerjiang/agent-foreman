"""Server-side SQLModel tables (team/relay mode) — DESIGN §7.2.

The SERVER database: accounts, access keys, the online-process registry, display caches,
and invites. It deliberately holds NO 秘方 (definitions), no full diffs/raw output, and NO
per-user LLM keys (those live in each user's local .env — DESIGN §8.3/§8.4).

Table names are set explicitly so they never clash with the client store on the shared
SQLModel.metadata (e.g. when both are imported in one test interpreter).

Placeholder for P7 — tables defined; relay/admin logic lands later.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class Account(SQLModel, table=True):
    __tablename__ = "accounts"
    id: str = Field(primary_key=True)
    username: str = Field(index=True)
    display_name: str = ""
    role: str = "member"          # admin | member
    status: str = "active"        # active | disabled
    created_at: str = ""


class AccessKey(SQLModel, table=True):
    __tablename__ = "access_keys"
    id: str = Field(primary_key=True)
    account_id: str = Field(index=True, foreign_key="accounts.id")
    key_hash: str = Field(index=True)   # only the hash is stored; plaintext shown once at creation
    label: str = ""                     # human name of the machine ("我的台式机")
    last_seen_at: str = ""
    status: str = "active"              # active | revoked
    expires_at: str = ""
    created_at: str = ""


class ProcessRegistry(SQLModel, table=True):
    __tablename__ = "process_registry"
    id: str = Field(primary_key=True)
    account_id: str = Field(index=True, foreign_key="accounts.id")
    access_key_id: str = Field(foreign_key="access_keys.id")
    name: str = ""
    online: bool = False
    last_heartbeat: str = ""
    created_at: str = ""


class CacheSession(SQLModel, table=True):
    __tablename__ = "cache_sessions"
    id: str = Field(primary_key=True)        # surrogate; (account_id, session_id) also indexed
    account_id: str = Field(index=True)
    session_id: str = Field(index=True)
    summary_json: str = "{}"
    updated_at: str = ""


class CacheCard(SQLModel, table=True):
    __tablename__ = "cache_cards"
    id: str = Field(primary_key=True)
    account_id: str = Field(index=True)
    card_id: str = Field(index=True)
    payload_json: str = "{}"
    status: str = ""
    updated_at: str = ""


class Invite(SQLModel, table=True):
    __tablename__ = "invites"
    id: str = Field(primary_key=True)
    code_hash: str = Field(index=True)
    account_id: str = Field(foreign_key="accounts.id")
    expires_at: str = ""
    used_at: str = ""


class ServerSchemaVersion(SQLModel, table=True):
    __tablename__ = "schema_version"  # distinct class name avoids clashing with client SchemaVersion
    version: int = Field(primary_key=True)
    applied_at: str = ""


# The tables this store owns — used to scope create_all so a shared metadata (tests) never
# leaks client tables into the server DB.
SERVER_TABLES = (
    Account, AccessKey, ProcessRegistry, CacheSession, CacheCard, Invite, ServerSchemaVersion,
)
