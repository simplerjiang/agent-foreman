"""P3 phase-acceptance integration test (ROADMAP P3 "Done when…", docs/TASKS.md P3 验收).

验收: claude 想 `git push` 被拦 → 手机收推送 → 点批准 → 恢复。

The P3 unit suites cover each block alone (Gate/Hook T3.4, Web Push T3.3, auth T3.5, relay T3.2).
This test ties them into the end-to-end flows the milestone describes, over *real* infrastructure —
a real client Store (SQLite), a real server ServerStore (SQLite), a real EventBus, the real FastAPI
app via TestClient (the surface the phone actually hits) — to prove the phase coheres, not just that
each part passes in isolation:

  1. 被拦 → 手机收推送 → 点批准  — a dangerous `git push` PreToolUse hook is DENIED back to Claude
                                  Code (tool blocked), the Gate persists a pending approval and pushes
                                  an approval card to the subscribed phone, then the phone taps Approve
                                  through the real `POST /api/approvals/{id}` route → approved +
                                  `approval_decided` event + the queue drains.
  2. 驳回 + 防重放                — the same loop's reject branch and the §6.8 one-shot/nonce replay
                                  guard hold through the real HTTP surface (driver/驳回 405-safe path).
  3. 团队远程的安全底座           — an AuthManager-minted access key authenticates through the Relay
                                  handshake (T3.5 ⨯ T3.2) and is rejected once revoked, the security
                                  underpinning of P3's remote phone access.

「恢复 (resume the held agent)」执行层 deferred to P4 (Runner two-way control: send/interrupt) and is
asserted here as `execution_deferred=True` on the decision event — the hook already blocks the tool;
actually un-blocking + resuming needs the P4 decision loop (noted in T2.6/T3.4 and TASKS.md P3 验收).

No network, no tokens, no live VAPID/HTTPS: Web Push goes through a fake Pusher; the wss relay
handshake logic runs directly (its live dialer is the deferred team rollout, T7.1).
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from foreman.client.core.gate import Gate
from foreman.client.monitor.hooks import HookReceiver
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.server.auth_manager import AuthManager
from foreman.server.relay import Relay
from foreman.server.store import ServerStore
from foreman.shared.config import GatesCfg, load_config
from foreman.shared.events import EventBus


# ── fakes / helpers ──────────────────────────────────────────────────────────────────────────────
class _FakePhone:
    """Stands in for Web Push delivery to the phone — records the cards pushed (no live VAPID)."""

    def __init__(self) -> None:
        self.cards: list[dict] = []

    async def send_to_all(self, subs, title, body, data=None):
        self.cards.append({"title": title, "body": body, "data": data, "n_subs": len(subs)})
        return []  # nothing GONE


def _client_store(tmp_path) -> Store:
    st = Store(str(tmp_path / "foreman.db"))
    st.init()
    st.add_session(Session(id="s1", goal="ship the feature"))
    return st


def _gate(store, bus, phone) -> Gate:
    return Gate(
        GatesCfg(requires_approval=["git push", "rm -rf"], needs_strategy=["pip install"]),
        store=store,
        bus=bus,
        pusher=phone,
    )


def _drain(q: asyncio.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


# ── 1: blocked git push → phone buzzes → tap Approve closes the loop ──────────────────────────────
def test_blocked_push_buzzes_phone_then_tap_approve_closes_loop(tmp_path):
    store = _client_store(tmp_path)
    store.add_push_subscription(endpoint="https://push/phone", p256dh="pk", auth="ak")  # the phone
    bus = EventBus()
    bus_q = bus.subscribe_queue()
    phone = _FakePhone()
    gate = _gate(store, bus, phone)
    rcv = HookReceiver(store, bus, gate)

    # ① Claude Code tries `git push` → PreToolUse hook → Gate holds it → tool is DENIED.
    deny = asyncio.run(
        rcv.handle(
            "PreToolUse",
            {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
            "s1",
        )
    )
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"  # 被拦

    # ② 手机收推送: exactly one approval card was pushed to the subscribed phone, with one-tap actions.
    assert len(phone.cards) == 1
    card = phone.cards[0]
    assert {a["action"] for a in card["data"]["actions"]} == {"approve", "reject"}
    assert "git push origin main" in card["body"]

    # A pending approval is queued; the timeline recorded the hold (tool_pre → approval_req).
    app = create_app_for(store, bus, gate)
    client = TestClient(app)
    pending = client.get("/api/approvals").json()
    assert len(pending) == 1
    approval_id = pending[0]["id"]
    nonce = pending[0]["nonce"]  # the phone reads the nonce off the card/queue, like the PWA does
    assert card["data"]["url"] == f"/?approval={approval_id}"  # same-origin deep link to this card
    assert [e.type for e in store.get_events("s1")] == ["tool_pre", "approval_req"]

    # ③ 点批准: the phone taps Approve through the REAL REST route the PWA uses.
    r = client.post(f"/api/approvals/{approval_id}", json={"decision": "approve", "nonce": nonce})
    assert r.status_code == 200 and r.json()["status"] == "approved"

    # The decision is recorded + the queue drains; resume itself is deferred to P4.
    assert store.get_approval(approval_id).status == "approved"
    assert client.get("/api/approvals").json() == []  # queue drained
    decided = next(e for e in store.get_events("s1") if e.type == "approval_decided")
    payload = json.loads(decided.payload_json)
    assert payload["status"] == "approved"
    assert payload["execution_deferred"] is True  # 恢复 (resume) lands in P4's two-way control
    # the bus saw the same decision stream (delivery, not just persistence)
    assert "approval_decided" in [e.type for e in _drain(bus_q)]


# ── 2: the reject branch + one-shot / nonce replay guard hold through the HTTP surface (§6.8) ──────
def test_blocked_push_reject_and_replay_safe_over_http(tmp_path):
    store = _client_store(tmp_path)
    bus = EventBus()
    gate = _gate(store, bus, _FakePhone())
    rcv = HookReceiver(store, bus, gate)

    asyncio.run(
        rcv.handle("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}, "s1")
    )
    client = TestClient(create_app_for(store, bus, gate))
    pending = client.get("/api/approvals").json()
    approval_id, nonce = pending[0]["id"], pending[0]["nonce"]

    # a wrong nonce is refused (403) and the approval stays pending (replay/forge resistant)
    assert (
        client.post(
            f"/api/approvals/{approval_id}", json={"decision": "reject", "nonce": "WRONG"}
        ).status_code
        == 403
    )
    assert store.get_approval(approval_id).status == "pending"

    # 驳回: the real reject with the right nonce sticks
    r = client.post(f"/api/approvals/{approval_id}", json={"decision": "reject", "nonce": nonce})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert store.get_approval(approval_id).status == "rejected"

    # a replayed decision on the now-resolved approval is refused (409) — one-shot
    assert (
        client.post(
            f"/api/approvals/{approval_id}", json={"decision": "approve", "nonce": nonce}
        ).status_code
        == 409
    )
    assert store.get_approval(approval_id).status == "rejected"  # first decision stands


# ── 3: team-mode security underpinning — minted key authenticates via relay, dies on revoke ───────
def test_minted_access_key_authenticates_via_relay_then_revoked(tmp_path):
    srv = ServerStore(str(tmp_path / "srv.db"))
    srv.init()
    auth = AuthManager(srv)
    relay = Relay(srv)

    aid = auth.create_account("alice", "pw")["account_id"]
    minted = auth.create_access_key(aid, label="alice-laptop")
    key = minted["key"]  # plaintext shown exactly once

    # the relay handshake authenticates the minted key by hash, deriving the account server-side
    ok = relay.authenticate({"access_key": key})
    assert ok.ok and ok.account_id == aid and ok.key_id == minted["id"]
    # account_id is never taken from the client frame — a forged one is ignored
    forged = relay.authenticate({"access_key": key, "account_id": "attacker"})
    assert forged.ok and forged.account_id == aid

    # the human also logs in for a bearer token (the PWA's credential) — distinct from the access key
    token = auth.login("alice", "pw")["token"]
    assert auth.resolve_token(token).id == aid

    # revoking the key cuts the handshake immediately (§7.2 single-key revoke)
    assert auth.revoke_access_key(aid, minted["id"]) == {"ok": True}
    assert relay.authenticate({"access_key": key}).ok is False
    # an unknown key is likewise rejected
    assert relay.authenticate({"access_key": "not-a-real-key"}).ok is False


def create_app_for(store, bus, gate):
    """The local FastAPI app wired with the client store/bus/gate (the surface the phone hits)."""
    from foreman.server.app import create_app

    return create_app(load_config(), store, bus, gate=gate)
