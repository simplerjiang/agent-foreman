"""Generate a VAPID keypair for Web Push.

Usage:
    python scripts/gen_vapid.py

Prints the PUBLIC key (put in config.yaml -> push.vapid_public_key) and the
PRIVATE key (put in .env -> FOREMAN_VAPID_PRIVATE_KEY). The private key must
stay secret and out of git.
"""

from __future__ import annotations


def main() -> None:
    try:
        from py_vapid import Vapid02
    except ImportError:
        raise SystemExit("py-vapid not installed. Run: pip install -e .")

    vapid = Vapid02()
    vapid.generate_keys()

    # Application-server keys, base64url-encoded, as the browser PushManager expects.
    public_key = vapid.public_key_urlsafe_base64()  # type: ignore[attr-defined]
    private_key = vapid.private_key_urlsafe_base64()  # type: ignore[attr-defined]

    print("=== VAPID keypair generated ===\n")
    print("config.yaml -> push.vapid_public_key:")
    print(f"  {public_key}\n")
    print(".env -> FOREMAN_VAPID_PRIVATE_KEY:")
    print(f"  {private_key}\n")
    print("Keep the PRIVATE key secret. Do not commit it.")


if __name__ == "__main__":
    main()
