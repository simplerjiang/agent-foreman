"""Single-user bearer-token auth + device pairing.

The token is generated on first run (or read from .env) and required on every REST/WS call.
New devices must be confirmed via a pairing code shown on the PC. See docs/SECURITY.md.
"""

from __future__ import annotations

import secrets


def generate_token() -> str:
    """Create a strong random bearer token."""
    return secrets.token_urlsafe(32)


def generate_pairing_code() -> str:
    """Short numeric code shown on the PC to confirm a new device (P3)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def verify_token(provided: str, expected: str) -> bool:
    """Constant-time token comparison."""
    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)
