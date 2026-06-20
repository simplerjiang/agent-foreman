"""Single-user bearer-token auth + device pairing.

The token is generated on first run (or read from .env) and required on every REST/WS call.
New devices must be confirmed via a pairing code shown on the PC. See docs/SECURITY.md.
"""

from __future__ import annotations

import hashlib
import secrets


def generate_token() -> str:
    """Create a strong random bearer token."""
    return secrets.token_urlsafe(32)


def generate_access_key() -> str:
    """Create a strong random access key (the local process's "SIM card" — DESIGN §8.2).

    Plaintext is shown to the user exactly once at creation; only its hash is stored
    server-side (§8.4). High-entropy random, so a plain SHA-256 (not a slow KDF) is the
    right fit — same rationale as hashing API keys.
    """
    return secrets.token_urlsafe(32)


def hash_access_key(plaintext: str) -> str:
    """Hash an access key for storage / handshake lookup (DESIGN §8.4 — hash only)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def verify_access_key(plaintext: str, expected_hash: str) -> bool:
    """Constant-time check of an access key plaintext against a stored hash."""
    if not plaintext or not expected_hash:
        return False
    return secrets.compare_digest(hash_access_key(plaintext), expected_hash)


def generate_pairing_code() -> str:
    """Short numeric code shown on the PC to confirm a new device (P3)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def verify_token(provided: str, expected: str) -> bool:
    """Constant-time token comparison."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)
