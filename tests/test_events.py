"""Tests for the event vocabulary + factory (TASKS T1.1)."""

from __future__ import annotations

from datetime import datetime

import pytest

from foreman.shared.events import EVENT_TYPES, AgentEvent, make_event, utc_now_iso


def test_utc_now_iso_is_tzaware_utc():
    dt = datetime.fromisoformat(utc_now_iso())
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_make_event_stamps_and_populates():
    ev = make_event("dispatch", "test", "s1", payload={"k": "v"})
    assert isinstance(ev, AgentEvent)
    assert (ev.type, ev.source, ev.session_id) == ("dispatch", "test", "s1")
    assert ev.payload == {"k": "v"}
    assert ev.task_id is None
    assert ev.ts  # stamped, non-empty


def test_make_event_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown event type"):
        make_event("bogus_type", "test", "s1")


def test_event_types_cover_design_vocab():
    for t in ("agent_output", "tool_pre", "tool_post", "checkpoint", "audit",
              "card_decided", "health", "stall", "recover", "dispatch"):
        assert t in EVENT_TYPES
