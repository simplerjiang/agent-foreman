"""Local process <-> relay server WebSocket (wss) message contract.

Placeholder for P3/P7 (DESIGN §8.5): the client connects OUTBOUND over wss, sends its
access key in the first frame, then both ends exchange framed `Envelope` messages
(hello / heartbeat / event / command / card / ack ...). Defining the envelope here — in
the shared layer — lets the client and server evolve against one agreed contract.

Filled in when the relay lands; today it only fixes the shape + version.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bumped whenever the envelope changes; both ends compare it and warn on mismatch
# instead of failing cryptically (DESIGN §11.1 — version the PC<->server channel).
PROTOCOL_VERSION = 1


@dataclass
class Envelope:
    """One framed message over the wss link (DESIGN §8.5)."""

    kind: str            # "hello" | "heartbeat" | "event" | "command" | "card" | "ack" | ...
    id: str = ""         # correlation id for request/response pairing
    account_id: str = ""  # set by the relay after the access-key handshake
    payload: dict = field(default_factory=dict)
    version: int = PROTOCOL_VERSION
