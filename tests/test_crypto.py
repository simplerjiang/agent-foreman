"""Tests for shared.crypto — optional at-rest encryption of definition bodies (T6.2, DESIGN §765).

The cipher is OPTIONAL and reversible: untagged plaintext round-trips unchanged, encryption is
idempotent, and a missing key just means plaintext. ``cryptography`` is the only hard requirement
here; it ships with the [client] extra.
"""

from __future__ import annotations

import pytest

from foreman.shared.crypto import (
    FENC_PREFIX,
    BodyCipher,
    cipher_from_config,
    maybe_decrypt,
    maybe_encrypt,
)

cryptography = pytest.importorskip("cryptography")


def _cipher() -> BodyCipher:
    return BodyCipher.from_key(BodyCipher.generate_key())


def test_round_trip():
    c = _cipher()
    plain = "steps:\n  - name: write tests"
    enc = c.encrypt(plain)
    assert enc.startswith(FENC_PREFIX)
    assert enc != plain
    assert c.decrypt(enc) == plain


def test_decrypt_is_pass_through_on_plaintext():
    # rows written before encryption was turned on stay readable (untagged → returned as-is)
    c = _cipher()
    assert c.decrypt("just plaintext") == "just plaintext"
    assert BodyCipher.is_encrypted("just plaintext") is False


def test_encrypt_is_idempotent():
    c = _cipher()
    once = c.encrypt("body")
    twice = c.encrypt(once)  # already tagged → returned unchanged, no double-wrap
    assert once == twice
    assert c.decrypt(twice) == "body"


def test_empty_and_non_str_left_alone():
    c = _cipher()
    assert c.encrypt("") == ""
    assert c.encrypt(None) is None  # type: ignore[arg-type]


def test_passphrase_is_deterministic():
    # same passphrase → same key → cross-restart decryptability
    a = BodyCipher.from_passphrase("correct horse battery staple")
    b = BodyCipher.from_passphrase("correct horse battery staple")
    enc = a.encrypt("secret recipe")
    assert b.decrypt(enc) == "secret recipe"


def test_empty_passphrase_rejected():
    with pytest.raises(ValueError):
        BodyCipher.from_passphrase("")


def test_invalid_key_rejected():
    with pytest.raises(ValueError):
        BodyCipher.from_key("not-a-valid-fernet-key")


def test_wrong_key_fails_loud():
    enc = _cipher().encrypt("payload")
    other = _cipher()
    with pytest.raises(Exception):  # cryptography.fernet.InvalidToken — never returns garbage
        other.decrypt(enc)


def test_cipher_from_config():
    assert cipher_from_config("") is None       # empty → encryption off (the default)
    assert cipher_from_config("   ") is None     # whitespace-only also off
    c = cipher_from_config(BodyCipher.generate_key())
    assert isinstance(c, BodyCipher)


def test_maybe_helpers_passthrough_without_cipher():
    assert maybe_encrypt(None, "x") == "x"
    assert maybe_decrypt(None, "x") == "x"
    c = _cipher()
    assert maybe_decrypt(c, maybe_encrypt(c, "x")) == "x"
