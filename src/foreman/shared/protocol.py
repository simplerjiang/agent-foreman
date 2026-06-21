"""Local process <-> relay server WebSocket (wss) message contract.

Placeholder for P3/P7 (DESIGN §8.5): the client connects OUTBOUND over wss, sends its
access key in the first frame, then both ends exchange framed `Envelope` messages
(hello / heartbeat / event / command / card / ack ...). Defining the envelope here — in
the shared layer — lets the client and server evolve against one agreed contract.

Envelope helpers (to/from dict + json) land with the relay (T3.2); the live transport on
each end (server /relay endpoint, client outbound dialer) is built on top of this shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# Bumped whenever the envelope changes; both ends compare it and warn on mismatch
# instead of failing cryptically (DESIGN §11.1 — version the PC<->server channel).
PROTOCOL_VERSION = 1

# The frame vocabulary on the relay link (DESIGN §8.5). Plain strings, kept here so both
# ends agree on the names without importing each other.
KIND_HELLO = "hello"            # local process -> relay: first frame, carries the access key
KIND_HELLO_ACK = "hello_ack"    # relay -> local process: accepted/denied + resolved process_id
KIND_HEARTBEAT = "heartbeat"    # either way: ping/pong keep-alive (§8.5 ③)
KIND_EVENT = "event"            # local process -> relay: an AgentEvent for the PWA
KIND_COMMAND = "command"        # relay -> local process: a request from the PWA
KIND_CARD = "card"              # decision card push
KIND_ACK = "ack"                # generic acknowledgement
KIND_CACHE_SYNC = "cache_sync"  # local process -> relay: display-cache snapshot (sessions/cards)
                                # so the PWA can read a read-only copy while the PC is offline (§8.5 ③)


@dataclass
class Envelope:
    """One framed message over the wss link (DESIGN §8.5)."""

    kind: str            # one of the KIND_* constants above
    id: str = ""         # correlation id for request/response pairing
    account_id: str = ""  # set by the relay after the access-key handshake
    payload: dict = field(default_factory=dict)
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "id": self.id,
            "account_id": self.account_id,
            "payload": self.payload,
            "version": self.version,
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
        return cls(
            kind=str(data.get("kind", "")),
            id=str(data.get("id", "")),
            account_id=str(data.get("account_id", "")),
            payload=payload,
            version=version,
        )

    @classmethod
    def from_json(cls, raw: str) -> Envelope:
        try:
            return cls.from_dict(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            return cls(kind="")
