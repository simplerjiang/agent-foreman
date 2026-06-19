"""Server persistence (team/relay mode) — SQLite via SQLModel. See DESIGN §7.2.

Separate from the client's local store. Holds NO 秘方, no full diffs/raw output, and NO
per-user LLM keys (those live in each user's local .env — DESIGN §8.3/§8.4).
"""

from .db import ServerStore

__all__ = ["ServerStore"]
