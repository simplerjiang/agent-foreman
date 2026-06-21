"""Generate a key for optional at-rest encryption of definition bodies (DESIGN §765, T6.2).

Usage:
    python scripts/gen_definition_key.py

Prints a fresh Fernet key. Put it in .env as FOREMAN_DEFINITION_KEY (never config.yaml, never
git). With a key configured, definition 秘方 bodies are encrypted in foreman.db, so a stolen .db
file is "只是一堆密文". Encryption is OPTIONAL — with no key set, bodies stay plaintext (the default)
and existing rows keep working. Keep this key safe: lose it and the encrypted bodies are unreadable.
"""

from __future__ import annotations


def main() -> None:
    try:
        from foreman.shared.crypto import BodyCipher
    except RuntimeError as exc:  # pragma: no cover
        raise SystemExit(str(exc))
    key = BodyCipher.generate_key()
    print("Add this to .env (keep it secret, out of git):\n")
    print(f"FOREMAN_DEFINITION_KEY={key}")


if __name__ == "__main__":
    main()
