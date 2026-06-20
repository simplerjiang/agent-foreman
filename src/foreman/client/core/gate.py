"""Gate — classifies actions and holds dangerous (irreversible) ones for human approval.

Levels: safe | needs-strategy | requires-approval (DESIGN §6.6). The Gate is the
*deterministic* backstop: hard-dangerous, irreversible actions (`git push`, deploy, change
secrets, `rm -rf`/drop table…) are paused here by rule — never on an LLM's say-so (§6.7 ①,
"绝不让 LLM 当唯一闸门"). The Auditor (P4) handles the gray "is this garbage/off-track" judgment.

When a requires-approval action is hit, `request_approval` records a pending `approvals` row
(with a one-time `nonce` for replay protection, §6.8), pushes an approval card to the phone via
Web Push, and the agent's tool call is denied pending your tap. You decide on PC/phone; `resolve`
applies approve/reject (one-shot, nonce-checked) and emits an `approval_decided` event. Actually
resuming the held agent is the two-way control layer (P4, Runner.send/interrupt) — this task
delivers the classify → hold → push → decide round-trip; resume is noted as deferred.

This module is client-side core. It reaches Web Push through an INJECTED `pusher` (duck-typed
foreman.server.push.Pusher) so the Gate never imports server (DESIGN §14 boundary); local_app.py
wires store + bus + pusher in.
"""

from __future__ import annotations

import secrets
import uuid

from foreman.shared.config import GatesCfg
from foreman.shared.events import make_event, utc_now_iso

from ..store.models import Approval


class Gate:
    def __init__(
        self,
        cfg: GatesCfg,
        *,
        store: object | None = None,
        bus: object | None = None,
        pusher: object | None = None,
        public_base_url: str = "",
        nonce_factory=None,
        clock=None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.pusher = pusher
        self.public_base_url = public_base_url.rstrip("/")
        self._nonce = nonce_factory or (lambda: secrets.token_urlsafe(16))
        self._clock = clock or utc_now_iso

    def classify(self, action_text: str) -> str:
        """Return safe | needs-strategy | requires-approval based on configured patterns."""
        low = action_text.lower()
        if any(p.lower() in low for p in self.cfg.requires_approval):
            return "requires-approval"
        if any(p.lower() in low for p in self.cfg.needs_strategy):
            return "needs-strategy"
        return "safe"

    async def request_approval(
        self,
        session_id: str,
        action: str,
        *,
        task_id: str | None = None,
        tool: str = "",
        diff_summary: str = "",
        risk_level: str = "requires-approval",
    ) -> dict | None:
        """Hold a dangerous action: persist a pending approval (+nonce) and push a card.

        Returns {id, nonce} for correlation, or None when there is no store to record into
        (e.g. a bare Gate used only for `classify` — the caller still records its own event).
        Pushing the card is best-effort: a disabled/absent Pusher or no subscriptions is a no-op,
        and GONE (404/410) endpoints are pruned so a stale browser never wedges the loop."""
        if self.store is None or not hasattr(self.store, "add_approval"):
            return None
        approval_id = uuid.uuid4().hex
        nonce = self._nonce()
        self.store.add_approval(
            Approval(
                id=approval_id,
                session_id=session_id,
                task_id=task_id,
                action=action,
                risk_level=risk_level,
                diff_summary=diff_summary or action,
                status="pending",
                nonce=nonce,
                requested_at=self._clock(),
            )
        )
        await self._push_card(approval_id, session_id, action, tool)
        return {"id": approval_id, "nonce": nonce}

    async def _push_card(
        self, approval_id: str, session_id: str, action: str, tool: str
    ) -> None:
        """Best-effort Web Push of the approval card to every subscribed browser (§4.6)."""
        if self.pusher is None or not hasattr(self.store, "get_push_subscriptions"):
            return
        subs = self.store.get_push_subscriptions()
        if not subs:
            return
        title = "🦺 需要审批" if tool else "🦺 Foreman 审批"
        body = (f"{tool}: " if tool else "") + action
        data = {
            # same-origin deep link; the SW validates origin before following (sw.js).
            "url": f"/?approval={approval_id}",
            "tag": f"approval-{approval_id}",
            "approval_id": approval_id,
            "session_id": session_id,
            "actions": [
                {"action": "approve", "title": "✅ 批准"},
                {"action": "reject", "title": "⛔ 驳回"},
            ],
        }
        gone = await self.pusher.send_to_all(subs, title, body[:200], data)
        for endpoint in gone or []:
            if hasattr(self.store, "delete_push_subscription"):
                self.store.delete_push_subscription(endpoint)

    def list_pending(self) -> list[dict]:
        """Pending approvals as JSON-friendly dicts (the phone's queue). Caller: server app.py
        (shared-only) — hence dicts, not Approval models (DESIGN §14)."""
        if self.store is None or not hasattr(self.store, "get_pending_approvals"):
            return []
        return [_approval_to_dict(a) for a in self.store.get_pending_approvals()]

    async def resolve(
        self,
        approval_id: str,
        decision: str,
        *,
        nonce: str | None = None,
        reason: str = "",
    ) -> dict:
        """Apply approve/reject. One-shot + nonce-checked (replay-safe, §6.8).

        Returns {"ok": True, "id", "status"} on success, else {"ok": False, "error": ...} with
        error ∈ {bad_decision, no_store, not_found, bad_nonce, not_pending}. Emits an
        `approval_decided` event (persist-then-publish). Resuming the held agent is P4."""
        status = {"approve": "approved", "reject": "rejected"}.get(decision)
        if status is None:
            return {"ok": False, "error": "bad_decision"}
        if self.store is None or not hasattr(self.store, "get_approval"):
            return {"ok": False, "error": "no_store"}
        row = self.store.get_approval(approval_id)
        if row is None:
            return {"ok": False, "error": "not_found"}
        if row.status != "pending":
            # already decided → treat a second decision as a replay and refuse it.
            return {"ok": False, "error": "not_pending"}
        # Timing-safe nonce check: an old captured request carries a stale/blank nonce. A row with
        # no nonce can never be decided (defence-in-depth — every real approval is created with one).
        if not row.nonce or not secrets.compare_digest(str(nonce or ""), str(row.nonce)):
            return {"ok": False, "error": "bad_nonce"}
        updated = self.store.decide_approval(
            approval_id, status=status, reason=reason, decided_at=self._clock()
        )
        if updated is None:  # lost a race — someone else decided it first.
            return {"ok": False, "error": "not_pending"}
        await self._emit_decided(updated)
        return {"ok": True, "id": approval_id, "status": status}

    async def _emit_decided(self, approval: Approval) -> None:
        """Record + publish the decision (persist-first, mirrors Runner/HookReceiver)."""
        event = make_event(
            "approval_decided",
            "gate",
            approval.session_id,
            task_id=approval.task_id,
            payload={
                "approval_id": approval.id,
                "status": approval.status,
                "action": approval.action,
                "reason": approval.reason,
                # resuming the held agent is the two-way control layer (P4, Runner.send/interrupt).
                "execution_deferred": True,
            },
        )
        if self.store is not None and hasattr(self.store, "add_event"):
            self.store.add_event(event)
        if self.bus is not None:
            await self.bus.publish(event)


def _approval_to_dict(a: Approval) -> dict:
    return {
        "id": a.id,
        "session_id": a.session_id,
        "task_id": a.task_id,
        "action": a.action,
        "risk_level": a.risk_level,
        "diff_summary": a.diff_summary,
        "status": a.status,
        "nonce": a.nonce,
        "requested_at": a.requested_at,
    }
