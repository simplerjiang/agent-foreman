"""Local process <-> relay server WebSocket (wss) message contract.

Placeholder for P3/P7 (DESIGN §8.5): the client connects OUTBOUND over wss, sends its
access key in the first frame, then both ends exchange framed `Envelope` messages
(hello / heartbeat / event / command / card / ack ...). Defining the envelope here — in
the shared layer — lets the client and server evolve against one agreed contract.

Envelope helpers (to/from dict + json) land with the relay (T3.2); the live transport on
each end (server /relay endpoint, client outbound dialer) is built on top of this shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field

# Bumped whenever the envelope changes; both ends compare it and warn on mismatch
# instead of failing cryptically (DESIGN §11.1 — version the PC<->server channel).
PROTOCOL_VERSION = 2

# The frame vocabulary on the relay link (DESIGN §8.5). Plain strings, kept here so both
# ends agree on the names without importing each other.
KIND_HELLO = "hello"            # local process -> relay: first frame, carries the access key
KIND_HELLO_ACK = "hello_ack"    # relay -> local process: accepted/denied + resolved process_id
KIND_HEARTBEAT = "heartbeat"    # either way: ping/pong keep-alive (§8.5 ③)
KIND_EVENT = "event"            # local process -> relay: an AgentEvent for the PWA
KIND_COMMAND = "command"        # relay -> local process: a request from the PWA
KIND_CARD = "card"              # decision card push
KIND_ACK = "ack"                # generic acknowledgement
KIND_SUBSCRIBE = "subscribe"    # relay -> local process: at least one PWA is watching
KIND_UNSUBSCRIBE = "unsubscribe"  # relay -> local process: no PWA subscribers remain
KIND_SNAPSHOT_REQ = "snapshot_req"  # PWA -> local process, routed by relay
KIND_SNAPSHOT = "snapshot"      # local process -> PWA, display-safe first-screen state
KIND_NOTIFY = "notify"          # local process -> relay: tiny TTL notification
KIND_CACHE_SYNC = "cache_sync"  # retired v1 frame; v2 readers ignore it without crashing


def new_id() -> str:
    """Correlation/idempotency id for a logical request over the relay link."""
    return uuid.uuid4().hex


def new_nonce() -> str:
    """Fresh transport nonce for MAC-protected frames."""
    return uuid.uuid4().hex


def mac_message(env: "Envelope") -> bytes:
    """Canonical bytes covered by a frame MAC.

    The MAC intentionally excludes ``mac`` itself and ``account_id``. The account is already
    bound by the access key / bearer token at the relay; including it would make harmless relay
    annotation changes break verification.
    """
    data = {
        "id": env.id,
        "kind": env.kind,
        "nonce": env.nonce,
        "payload": env.payload,
        "seq": env.seq,
        "ts": env.ts,
        "version": env.version,
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sign_envelope(env: "Envelope", key_material: str) -> str:
    """Return the hex HMAC for ``env`` using per-access-key material."""
    return hmac.new(key_material.encode("utf-8"), mac_message(env), hashlib.sha256).hexdigest()


def attach_mac(env: "Envelope", key_material: str) -> "Envelope":
    """Set ``env.mac`` in place and return it for fluent builders."""
    env.mac = sign_envelope(env, key_material)
    return env


def verify_mac(env: "Envelope", key_material: str) -> bool:
    """Constant-time MAC verification. Missing/empty values fail closed."""
    if not env.mac or not key_material:
        return False
    return hmac.compare_digest(env.mac, sign_envelope(env, key_material))


@dataclass
class Envelope:
    """One framed message over the wss link (DESIGN §8.5)."""

    kind: str            # one of the KIND_* constants above
    id: str = ""         # correlation id for request/response pairing
    account_id: str = ""  # set by the relay after the access-key handshake
    payload: dict = field(default_factory=dict)
    version: int = PROTOCOL_VERSION
    nonce: str = ""      # fresh transport nonce; separate from logical idempotency id
    seq: int = 0         # per-connection monotonic sequence number for replay defense
    ts: float = 0.0      # sender wall-clock seconds
    mac: str = ""        # HMAC over the v2 transport fields for command/control frames

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "account_id": self.account_id,
            "payload": self.payload,
            "version": self.version,
            "nonce": self.nonce,
            "seq": self.seq,
            "ts": self.ts,
            "mac": self.mac,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: object) -> Envelope:
        """Parse a decoded frame. Tolerant by design — a malformed frame becomes an empty
        envelope (kind="") rather than raising, so the read loop can drop it and carry on."""
        if not isinstance(data, dict):
            return cls(kind="")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        try:
            version = int(data.get("version", PROTOCOL_VERSION))
        except (TypeError, ValueError):
            version = PROTOCOL_VERSION
        try:
            seq = int(data.get("seq", 0) or 0)
        except (TypeError, ValueError):
            seq = 0
        try:
            ts = float(data.get("ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        return cls(
            kind=str(data.get("kind", "")),
            id=str(data.get("id", "")),
            account_id=str(data.get("account_id", "")),
            payload=payload,
            version=version,
            nonce=str(data.get("nonce", "")),
            seq=seq,
            ts=ts,
            mac=str(data.get("mac", "")),
        )

    @classmethod
    def from_json(cls, raw: str) -> Envelope:
        try:
            return cls.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return cls(kind="")


def command_envelope(action: str, payload: dict, *, seq: int = 0, corr_id: str = "") -> Envelope:
    """Build a v2 command frame. The caller signs it after assigning the target key material."""
    return Envelope(
        kind=KIND_COMMAND,
        id=corr_id or new_id(),
        payload={"action": action, **payload},
        nonce=new_nonce(),
        seq=seq,
        ts=time.time(),
    )
