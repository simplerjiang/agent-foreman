"""Auth primitives.

Two unrelated credential families live here:
  • Single-user bearer token + device pairing (personal mode): generate_token / verify_token /
    generate_pairing_code — the personal-mode tunnel guard.
  • Team-mode (DESIGN §8.2): access-key hashing (generate/hash/verify_access_key) and user-login
    password hashing (hash_password / verify_password, PBKDF2-HMAC-SHA256).

All secret comparisons are constant-time; the server stores only hashes (DESIGN §8.4).
"""

from __future__ import annotations

import hashlib
import secrets

# Password hashing parameters (DESIGN §8.2 user login). PBKDF2-HMAC-SHA256 via the stdlib —
# no extra dependency. Unlike high-entropy access keys/tokens (which can use a plain SHA-256),
# user passwords are low-entropy and human-chosen, so they MUST use a slow, salted KDF.
_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 240_000


def generate_token() -> str:
    """Create a strong random bearer token."""
    return secrets.token_urlsafe(32)


def hash_password(plaintext: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """Hash a user-login password with a per-password random salt (DESIGN §8.2).

    Returns a self-describing string `pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>` so
    verify_password can read the parameters back and the work factor can be raised later
    without invalidating existing hashes.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", plaintext.encode("utf-8"), salt, iterations)
    return f"{_PBKDF2_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(plaintext: str, stored: str) -> bool:
    """Constant-time check of a password against a stored pbkdf2_sha256 hash.

    Tolerant of malformed/empty stored hashes (returns False) so a missing password never
    crashes the login path.
    """
    if not plaintext or not stored:
        return False
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != _PBKDF2_ALGO:
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", plaintext.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(digest, expected)


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
