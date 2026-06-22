"""In-memory log ring buffer for the admin console's 日志管理 view.

The team server normally logs to stdout/journald, which an admin can only reach over SSH.
This handler keeps the most recent N log records in a bounded deque so the admin PWA can read
them over the authenticated REST API (``GET /api/admin/logs``) without shelling out to
``journalctl`` (which would be a fragile, injection-prone surface).

It captures ONLY log text already destined for the console — never request bodies, secrets, or
tenant content (the app never logs those, §8.3/§8.4). A process restart clears it (in-memory by
design), which is acceptable for an operational tail.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone


class RingBufferHandler(logging.Handler):
    """A logging.Handler that retains the last ``capacity`` records in memory (newest evicts
    oldest). Read with :meth:`records`; it never raises into the logging path."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._buf: deque[dict] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Dedup across the logger hierarchy: the SAME LogRecord object is passed to this
            # handler at each ancestor it's attached to (e.g. `uvicorn.error` propagates up to
            # `uvicorn`, both tapped), which would buffer it twice. Tagging the record on first
            # sight and skipping repeats guarantees exactly one entry regardless of overlap.
            if getattr(record, "_foreman_buffered", False):
                return
            record._foreman_buffered = True  # type: ignore[attr-defined]
            self._buf.append(
                {
                    "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
            )
        except Exception:  # noqa: BLE001 — a log handler must never raise into the caller
            pass

    def records(self, limit: int = 200, level: str | None = None) -> list[dict]:
        """Recent records, newest first. ``level`` filters by exact level name (e.g. ``ERROR``)."""
        items = list(self._buf)
        if level:
            want = level.strip().upper()
            items = [r for r in items if r["level"] == want]
        if limit and limit > 0:
            items = items[-limit:]
        items.reverse()  # newest first for the UI
        return items

    def clear(self) -> None:
        self._buf.clear()


# Loggers we tap: root (catches the app's own ``foreman.*`` loggers, which propagate) plus the two
# uvicorn loggers that uvicorn configures with ``propagate=False`` (so they'd otherwise bypass
# root): ``uvicorn`` (general + error — ``uvicorn.error`` propagates UP into it, so tapping the
# child too would just double-log) and ``uvicorn.access`` (the request lines). emit() also dedups
# by record identity as a belt-and-suspenders against any hierarchy overlap.
_TAP_LOGGERS = ("", "uvicorn", "uvicorn.access")

# Module singleton: one buffer shared by every app instance. Using a singleton (rather than
# per-app) avoids stacking a fresh handler each time create_app() runs (e.g. across many
# TestClient apps in one test interpreter), which would duplicate every line and leak handlers.
_SINGLETON: RingBufferHandler | None = None


def _attach(handler: RingBufferHandler) -> None:
    """Idempotently add ``handler`` to each tapped logger (dedup by identity)."""
    for name in _TAP_LOGGERS:
        lg = logging.getLogger(name)
        if handler not in lg.handlers:
            lg.addHandler(handler)


def get_log_buffer(capacity: int = 500) -> RingBufferHandler:
    """Return the process-wide log buffer, (re)attaching it to the tapped loggers.

    Attachment is re-run on every call (idempotent) ON PURPOSE: ``uvicorn.run()`` applies its own
    logging dictConfig AFTER the app is built, which replaces the uvicorn loggers' handler lists
    and would drop a handler attached at build time. Calling this again from the app's ``startup``
    event re-attaches it once uvicorn's config is in place, so the admin log tail actually captures
    request/error lines in production (tests, which use TestClient, never run uvicorn)."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = RingBufferHandler(capacity)
        _SINGLETON.setLevel(logging.INFO)
    _attach(_SINGLETON)
    return _SINGLETON
