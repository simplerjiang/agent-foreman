"""LLM request/response debug trace (work-mode integration P1b-trace, DESIGN §8C).

When ``debug.llm_trace`` is on, every PM-brain LLM call (the two choke points ``LLMClient.complete`` /
``tool_complete``) is recorded as one JSONL line under ``.foreman/debug/`` — the full request (system
+ all messages + tool schema), the full response (text / tool_calls), timing, and the correlating
``session_id`` / ``task_id`` / ``phase``. This is the eyes for tuning the work-mode context (§8B): you
can replay exactly what was fed to the model and where a bad plan came from.

Design points (DESIGN §8C):
  * Correlation rides a single ``contextvars.ContextVar`` (set at call boundaries) so no call-site
    signature changes — and it is the SAME source the §16 work_mode telemetry ids come from.
  * This module is a stdlib-only LEAF (no import of ``client``) so ``client`` can import it without a
    cycle. ``messages`` are duck-typed (``.role`` / ``.content``).
  * Payloads are SENSITIVE (decrypted 秘方 + user source). Local-only, never uploaded, git-excluded,
    size-rotated. API keys never reach this layer (keys live only in HTTP headers, assembled below
    ``messages``); a belt-and-suspenders redactor scrubs ``sk-…`` / ``Bearer …`` anyway.
  * The tracer NEVER raises into the caller — a broken trace must not break a real LLM call.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Single correlation source — shared with the §16 work_mode telemetry ids (DESIGN §8C.3).
_TRACE_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "foreman_llm_trace_ctx", default={}
)


@contextlib.contextmanager
def trace_context(
    *, session_id: str | None = None, task_id: str | None = None, phase: str | None = None
):
    """Set the trace correlation at a call boundary. MERGES with the current context (pass only
    ``phase`` to relabel a nested call — e.g. tool-round-N — while keeping the outer session/task).
    Always token+reset so concurrent tasks never bleed into each other."""
    cur = _TRACE_CTX.get()
    new = dict(cur)
    if session_id is not None:
        new["session_id"] = session_id
    if task_id is not None:
        new["task_id"] = task_id
    if phase is not None:
        new["phase"] = phase
    token = _TRACE_CTX.set(new)
    try:
        yield
    finally:
        _TRACE_CTX.reset(token)


def current_trace_context() -> dict:
    return _TRACE_CTX.get()


_REDACT_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(x-api-key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]+"),
]


def _redact(text: str) -> str:
    """Belt-and-suspenders scrub of anything key-shaped (keys never reach here on the normal path)."""
    out = text
    for pat in _REDACT_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _approx_tokens(chars: int) -> int:
    return math.ceil(chars / 4)


class LLMTracer:
    """JSONL sink with size rotation + count/age retention. Injected into LLMClient; one instance is
    shared across every PM client so ``seq`` is process-monotonic and files are unified."""

    def __init__(
        self, *, log_dir: Path | str, max_bytes: int, keep: int, keep_days: int
    ) -> None:
        self._dir = Path(log_dir)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._max_bytes = max(1, int(max_bytes))
        self._keep = max(1, int(keep))
        self._keep_days = max(0, int(keep_days))
        self._seq = itertools.count(1)

    # ── public API ────────────────────────────────────────────────────────────────────────────
    def record(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
        transport: str,
        json_mode: bool,
        messages: Any,
        tools: Any,
        response_text: str,
        tool_calls: Any,
        latency_ms: float,
        error: str | None,
    ) -> None:
        """Write one trace line. Never raises into the caller (DESIGN §8C: a broken trace must not
        break the real LLM call)."""
        try:
            ctx = current_trace_context()
            msgs = [
                {"role": getattr(m, "role", ""), "content": getattr(m, "content", "")}
                for m in (messages or [])
            ]
            req_chars = sum(len(m["content"] or "") for m in msgs)
            resp_chars = len(response_text or "")
            rec = {
                "ts": _utc_iso(),
                "seq": next(self._seq),
                "session_id": ctx.get("session_id", ""),
                "task_id": ctx.get("task_id", ""),
                "phase": ctx.get("phase", ""),
                "kind": kind,
                "provider": provider,
                "model": model,
                "transport": transport,
                "json_mode": json_mode,
                "request": {"messages": msgs, "tools": list(tools or [])},
                "response": {"text": response_text or "", "tool_calls": list(tool_calls or [])},
                "metrics": {
                    "req_chars": req_chars,
                    "resp_chars": resp_chars,
                    "approx_req_tokens": _approx_tokens(req_chars),
                    "approx_resp_tokens": _approx_tokens(resp_chars),
                    "latency_ms": round(latency_ms, 2),
                },
                "error": error,
            }
            line = _redact(json.dumps(rec, ensure_ascii=False)) + "\n"
            self._write(self._path_for(ctx.get("session_id", "")), line)
        except Exception:  # noqa: BLE001 — tracing must never crash a real LLM call
            return

    # ── internals ─────────────────────────────────────────────────────────────────────────────
    def _path_for(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._\-]", "_", session_id) if session_id else "_no-session"
        return self._dir / f"llm-trace-{safe}.jsonl"

    def _write(self, path: Path, line: str) -> None:
        data = line.encode("utf-8")
        try:
            existing = path.stat().st_size if path.exists() else 0
        except OSError:
            existing = 0
        if existing and existing + len(data) > self._max_bytes:
            self._rotate(path)
        with path.open("ab") as fh:
            fh.write(data)
        self._prune(path)

    def _rotate(self, path: Path) -> None:
        # name.jsonl.(keep-1) is dropped; shift .k → .(k+1); name.jsonl → name.jsonl.1
        oldest = path.with_suffix(path.suffix + f".{self._keep - 1}")
        if oldest.exists():
            with contextlib.suppress(OSError):
                oldest.unlink()
        for i in range(self._keep - 2, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            if src.exists():
                with contextlib.suppress(OSError):
                    src.replace(path.with_suffix(path.suffix + f".{i + 1}"))
        with contextlib.suppress(OSError):
            path.replace(path.with_suffix(path.suffix + ".1"))

    def _prune(self, path: Path) -> None:
        rotated = sorted(
            path.parent.glob(path.name + ".*"),
            key=lambda p: _suffix_index(p),
        )
        # count retention (keep current + up to keep-1 rotated)
        for extra in rotated[self._keep - 1:]:
            with contextlib.suppress(OSError):
                extra.unlink()
        # age retention
        if self._keep_days > 0:
            cutoff = time.time() - self._keep_days * 86400
            for p in path.parent.glob(path.name + ".*"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass


def _suffix_index(p: Path) -> int:
    try:
        return int(p.suffix.lstrip("."))
    except ValueError:
        return 0


__all__ = ["LLMTracer", "trace_context", "current_trace_context"]
