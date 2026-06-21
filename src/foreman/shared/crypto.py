"""Optional at-rest encryption for definition bodies (DESIGN §11.2 / §765, T6.2).

Encryption is **optional**. With no key configured the 秘方 bodies stay plaintext (today's
behaviour); when a key IS configured, a definition's ``body`` is encrypted before it ever touches
``foreman.db``, so a stolen .db file is "只是一堆密文" (§765). The same mechanism encrypts an
exported backup bundle so it can be carried between machines without leaking the recipes.

Ciphertext is tagged with a ``fenc:v1:`` prefix. That tag is what makes encryption *optional and
reversible*:

* ``decrypt`` is a **pass-through** on anything untagged — plaintext rows, and rows written before
  encryption was turned on, round-trip unchanged. Encrypted and plaintext rows coexist in one DB.
* ``encrypt`` is **idempotent** — an already-tagged value is returned as-is, so a double-encrypt
  (e.g. re-importing an already-encrypted bundle) can't corrupt data.

The actual cipher is Fernet (AES-128-CBC + HMAC-SHA256, authenticated) from the ``cryptography``
package. It is imported **lazily** inside the constructors, so importing this module never requires
``cryptography`` — only constructing a ``BodyCipher`` (i.e. only when a key is actually configured)
does. Bodies are only ever encrypted/decrypted here; they are **never executed** (DESIGN §11.2).
"""

from __future__ import annotations

# Tag marking a value as Foreman-encrypted. Versioned so the scheme can evolve without ambiguity.
FENC_PREFIX = "fenc:v1:"

# Default PBKDF2 salt for passphrase-derived keys. A constant salt means a given passphrase always
# derives the same key (needed so the same passphrase decrypts the same DB across restarts). The
# strength therefore rests on passphrase entropy + the high iteration count, not the salt — callers
# wanting per-DB salting can pass their own ``salt``. Prefer ``from_key`` with a generated key.
_DEFAULT_SALT = b"foreman.definition.v1"
_PBKDF2_ITERATIONS = 200_000


class BodyCipher:
    """Encrypts / decrypts a definition body with a symmetric authenticated cipher.

    Construct via :meth:`from_key` (a high-entropy Fernet key — preferred) or :meth:`from_passphrase`
    (a human passphrase, stretched with PBKDF2). Both lazily import ``cryptography``; if it is not
    installed they raise a clear ``RuntimeError`` rather than failing obscurely.
    """

    def __init__(self, fernet) -> None:
        self._fernet = fernet

    # ── construction ──────────────────────────────────────────────────────────────────────────
    @classmethod
    def from_key(cls, key: str | bytes) -> BodyCipher:
        """Build from a urlsafe-base64 32-byte Fernet key (see :meth:`generate_key`)."""
        fernet_cls = _load_fernet()
        if isinstance(key, str):
            key = key.encode("utf-8")
        try:
            return cls(fernet_cls(key))
        except (ValueError, TypeError) as exc:  # malformed key
            raise ValueError("invalid definition encryption key") from exc

    @classmethod
    def from_passphrase(
        cls,
        passphrase: str,
        *,
        salt: bytes = _DEFAULT_SALT,
        iterations: int = _PBKDF2_ITERATIONS,
    ) -> BodyCipher:
        """Derive a key from a passphrase via PBKDF2-HMAC-SHA256, then build the cipher."""
        if not passphrase:
            raise ValueError("passphrase must not be empty")
        import base64
        import hashlib

        raw = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=32)
        return cls.from_key(base64.urlsafe_b64encode(raw))

    @staticmethod
    def generate_key() -> str:
        """A fresh random Fernet key (urlsafe-base64 str) for ``FOREMAN_DEFINITION_KEY`` / config."""
        return _load_fernet().generate_key().decode("utf-8")

    # ── encrypt / decrypt ─────────────────────────────────────────────────────────────────────
    @staticmethod
    def is_encrypted(text: str) -> bool:
        """True iff ``text`` is a Foreman ciphertext (tagged). Cheap, no key needed."""
        return isinstance(text, str) and text.startswith(FENC_PREFIX)

    def encrypt(self, text: str) -> str:
        """Encrypt ``text`` → tagged ciphertext. Idempotent (already-tagged → returned as-is).

        Empty strings are left empty: a blank body reveals nothing and new/empty definitions stay
        clean. Non-str input is returned unchanged (defensive)."""
        if not isinstance(text, str) or not text or self.is_encrypted(text):
            return text
        token = self._fernet.encrypt(text.encode("utf-8")).decode("ascii")
        return FENC_PREFIX + token

    def decrypt(self, text: str) -> str:
        """Decrypt tagged ciphertext → plaintext. Pass-through on anything untagged.

        Raises ``cryptography.fernet.InvalidToken`` if a tagged value fails authentication (tampered
        or wrong key) — fail loud rather than return garbage."""
        if not self.is_encrypted(text):
            return text
        token = text[len(FENC_PREFIX):].encode("ascii")
        return self._fernet.decrypt(token).decode("utf-8")


def maybe_encrypt(cipher: BodyCipher | None, text: str) -> str:
    """Encrypt with ``cipher`` if one is configured, else pass ``text`` through unchanged."""
    return cipher.encrypt(text) if cipher is not None else text


def maybe_decrypt(cipher: BodyCipher | None, text: str) -> str:
    """Decrypt with ``cipher`` if one is configured, else pass ``text`` through unchanged.

    Note: a tagged value with no cipher is returned tagged (still ciphertext) — callers that must
    surface plaintext should check :meth:`BodyCipher.is_encrypted` and require a key."""
    return cipher.decrypt(text) if cipher is not None else text


def cipher_from_config(key: str) -> BodyCipher | None:
    """Build the at-rest cipher from a configured key string, or None if encryption is off (empty).

    The key comes from a secret (``FOREMAN_DEFINITION_KEY`` / .env), never config.yaml. Empty =
    encryption disabled (the default) → returns None and bodies stay plaintext."""
    key = (key or "").strip()
    if not key:
        return None
    return BodyCipher.from_key(key)


def _load_fernet():
    """Lazily import Fernet so importing this module never requires ``cryptography``."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - exercised only without the optional dep
        raise RuntimeError(
            "definition body encryption needs the 'cryptography' package "
            "(install foreman with the [client] extra)"
        ) from exc
    return Fernet


__all__ = [
    "BodyCipher",
    "FENC_PREFIX",
    "maybe_encrypt",
    "maybe_decrypt",
    "cipher_from_config",
]
