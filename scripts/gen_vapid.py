"""Generate a VAPID keypair for Web Push.

Usage:
    python scripts/gen_vapid.py

Prints the PUBLIC key (put in config.yaml -> push.vapid_public_key) and the
PRIVATE key (put in .env -> FOREMAN_VAPID_PRIVATE_KEY). The private key must
stay secret and out of git.
"""

from __future__ import annotations

import base64


def main() -> None:
    try:
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid02
    except ImportError:
        raise SystemExit("py-vapid not installed. Run: pip install -e \".[server]\"")

    vapid = Vapid02()
    vapid.generate_keys()

    # Application-server keys, base64url (no padding) — the format the browser PushManager and
    # pywebpush expect. The public key is the raw uncompressed EC point (65 bytes); the private
    # key is the raw 32-byte secret scalar.
    raw_pub = vapid._public_key.public_bytes(  # type: ignore[attr-defined]
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    raw_priv = vapid._private_key.private_numbers().private_value.to_bytes(32, "big")  # type: ignore[attr-defined]
    public_key = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode()
    private_key = base64.urlsafe_b64encode(raw_priv).rstrip(b"=").decode()

    print("=== VAPID keypair generated ===\n")
    print("config.yaml -> push.vapid_public_key:")
    print(f"  {public_key}\n")
    print(".env -> FOREMAN_VAPID_PRIVATE_KEY:")
    print(f"  {private_key}\n")
    print("Keep the PRIVATE key secret. Do not commit it.")


if __name__ == "__main__":
    main()
