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

import re
import secrets
import uuid

from foreman.shared.autonomy import decide_disposition
from foreman.shared.config import GatesCfg
from foreman.shared.events import make_event, utc_now_iso

from ..store.models import Approval

# Whitespace/flag-robust backstop for irreversible commands the plain substring denylist
# (config.GatesCfg.requires_approval) misses. The deterministic Gate is the red line (§6.7①), so it
# must not be defeated by trivial reformatting (`rm  -rf`, `rm -fr`, `rm -i -rf`, `git -C <path>
# push`) nor be blind to the Windows/PowerShell verbs absent from the Unix-centric default list.
#
# Matched against the lowercased text with runs of spaces/tabs collapsed to one space but NEWLINES
# PRESERVED, so `[^|&;\n]*` genuinely keeps a match inside a single command segment (a later pipe /
# `;` / `&` / newline stage can't smuggle a false hit across it). This is a *backstop*, not a
# complete parser: it deliberately errs toward over-blocking (fail-closed, §6.7 从严默认) — a spurious
# approval card costs one tap, a missed irreversible command may be unrecoverable. It does NOT
# replace the Auditor LLM (the gray-area judge) or per-step checkpoints; novel obfuscations
# (encoded payloads, custom aliases) remain the Auditor's job by design.
_IRREVERSIBLE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # rm with a -r/-f flag ANYWHERE in the segment (so `rm -i -rf`, `rm --verbose -rf` count);
        # the flag must be a space-led token so a filename like `report-final.txt` isn't a false hit.
        r"\brm\b[^|&;\n]*\s-\S*[rf]",
        r"\brm\b[^|&;\n]*\s--(?:recursive|force)\b",             # rm --recursive / --force
        r"\bremove-item\b[^|&;\n]*-(?:recurse|force|r|fo|f)\b",  # PowerShell rm (Remove-Item)
        r"\bri\b[^|&;\n]*\s-\S*(?:recurse|force|r|fo|f)\b",      # Remove-Item alias `ri`
        r"\b(?:del|erase|rd|rmdir)\b[^|&;\n]*\s/[sq]\b",         # cmd recursive/quiet delete
        r"\b(?:del|erase|rd|rmdir)\b[^|&;\n]*\s-(?:recurse|force)\b",  # PowerShell-style flags
        r"\bgit\b[^|&;\n]*\bpush\b",                             # git ... push (incl. -C <path>)
        r"\bgit\b[^|&;\n]*\breset\b[^|&;\n]*--hard\b",           # discards committed/working state
        r"\bgit\b[^|&;\n]*\bclean\b[^|&;\n]*(?:\s-\S*f|--force)",  # deletes untracked files
        r"\bgit\b[^|&;\n]*\bcheckout\b[^|&;\n]*\s--(?:\s|$)",    # discards worktree changes
        r"\b(?:stop-computer|restart-computer)\b",              # PowerShell shutdown/reboot
        r"\bformat\b\s+[a-z]:",                                  # format C:
        r"\b(?:mkfs|diskpart|format-volume|clear-disk)\b",      # filesystem/disk destroyers
        r"\binvoke-expression\b",                               # iex: pipe-fetched code → execute
        r"\biex\b",
        r"-encodedcommand\b",                                   # powershell -EncodedCommand (obfusc.)
    )
)


def _matches_irreversible(low_text: str) -> bool:
    """True if the (already-lowercased) action text trips a built-in irreversible pattern.

    Runs of spaces/tabs are collapsed (so `rm  -rf` can't slip past a spacing-sensitive match) while
    newlines are KEPT as real segment separators — the deterministic red line stays robust to
    reformatting yet still bounds each pattern to one command segment (§6.7①)."""
    norm = re.sub(r"[ \t]+", " ", low_text)
    return any(p.search(norm) for p in _IRREVERSIBLE_PATTERNS)


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
        """Return safe | needs-strategy | requires-approval.

        Two layers decide requires-approval: ① the configured substring denylist (user-extensible
        via config.yaml) and ② a built-in whitespace/flag-robust regex set that catches irreversible
        commands the plain list misses (Windows/PowerShell verbs + spacing/order bypasses of the Unix
        ones). The deterministic Gate is the red line (§6.7①), so it can't be defeated by trivial
        reformatting or by running on Windows."""
        low = action_text.lower()
        if any(p.lower() in low for p in self.cfg.requires_approval):
            return "requires-approval"
        if _matches_irreversible(low):
            return "requires-approval"
        if any(p.lower() in low for p in self.cfg.needs_strategy):
            return "needs-strategy"
        return "safe"

    def disposition(self, action_text: str, level) -> str:
        """Combine the deterministic classification with the autonomy dial → auto|card|report.

        The single call the decision loop makes per proposed action (DESIGN §6.4): classify the
        action by rule, then let the dial decide whether to run it, ask via a card, or only report.
        Irreversible (requires-approval) actions never resolve to ``auto`` at any level (§6.6).
        Actually executing the ``auto``/``card`` outcome is the two-way control layer (P4)."""
        return decide_disposition(self.classify(action_text), level)

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
