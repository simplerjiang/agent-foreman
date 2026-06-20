"""Tests for the Claude Code hook receiver — POST /hooks (TASKS T2.4).

Covers the pure mapping (hook_to_event / action_text), the HookReceiver (persist + publish +
Gate screening), and the FastAPI route (header/body hook-name resolution, deny on dangerous
PreToolUse, 503 when no receiver is wired). The receiver is injected so server.app stays
client-free (DESIGN §14 boundary).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from foreman.client.core.gate import Gate
from foreman.client.monitor.hooks import HookReceiver, action_text, hook_to_event
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.server.app import create_app
from foreman.shared.config import GatesCfg, load_config
from foreman.shared.events import EVENT_TYPES, EventBus


# ── pure mapping ─────────────────────────────────────────────────────────────────────────────

def test_notification_is_a_known_event_type():
    """Claude's Notification hook is a first-class watchdog signal (§4.1/§5.6)."""
    assert "notification" in EVENT_TYPES


@pytest.mark.parametrize(
    "hook,expected",
    [
        ("PreToolUse", "tool_pre"),
        ("PostToolUse", "tool_post"),
        ("Stop", "stop"),
        ("SubagentStop", "stop"),
        ("Notification", "notification"),
        ("Mystery", "agent_output"),  # unknown hook → safe default
    ],
)
def test_hook_to_event_maps_type(hook, expected):
    ev = hook_to_event(hook, {"k": "v"}, "s1")
    assert ev.type == expected
    assert ev.source == "hook"
    assert ev.session_id == "s1"
    assert ev.payload["hook"] == hook and ev.payload["k"] == "v"
    assert ev.ts  # make_event stamps it


def test_action_text_flattens_tool_input():
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push origin main", "x": 1}}
    text = action_text(payload)
    assert "Bash" in text and "git push origin main" in text
    assert "1" not in text  # non-str input fields are ignored


# ── HookReceiver (unit) ──────────────────────────────────────────────────────────────────────

def _store(tmp_path):
    s = Store(str(tmp_path / "h.db"))
    s.init()
    s.add_session(Session(id="s1", goal="g"))
    return s


async def test_receiver_persists_and_publishes(tmp_path):
    store = _store(tmp_path)
    bus = EventBus()
    q = bus.subscribe_queue()
    rcv = HookReceiver(store, bus)

    out = await rcv.handle("PostToolUse", {"tool_name": "Read"}, "s1")

    assert out == {"ok": True}
    rows = store.get_events("s1")
    assert [r.type for r in rows] == ["tool_post"]
    assert q.get_nowait().type == "tool_post"  # also fanned out on the bus


async def test_receiver_blocks_dangerous_pretooluse(tmp_path):
    store = _store(tmp_path)
    rcv = HookReceiver(store, EventBus(), Gate(GatesCfg(requires_approval=["git push"])))

    out = await rcv.handle(
        "PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "git push -f"}}, "s1"
    )

    assert out["decision"] == "block"
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    types = [r.type for r in store.get_events("s1")]
    assert types == ["tool_pre", "approval_req"]  # the call is recorded AND an approval is raised


async def test_receiver_allows_safe_pretooluse(tmp_path):
    store = _store(tmp_path)
    rcv = HookReceiver(store, EventBus(), Gate(GatesCfg(requires_approval=["git push"])))

    out = await rcv.handle(
        "PreToolUse", {"tool_name": "Read", "tool_input": {"file_path": "a.py"}}, "s1"
    )

    assert out == {"ok": True}
    assert [r.type for r in store.get_events("s1")] == ["tool_pre"]  # no approval_req


async def test_receiver_falls_back_to_native_session_id(tmp_path):
    store = _store(tmp_path)
    rcv = HookReceiver(store, EventBus())
    await rcv.handle("Stop", {"session_id": "native-123"}, None)
    assert [r.session_id for r in store.get_events("native-123")] == ["native-123"]


# ── FastAPI route ────────────────────────────────────────────────────────────────────────────

def _client(tmp_path, gate=None):
    store = _store(tmp_path)
    bus = EventBus()
    rcv = HookReceiver(store, bus, gate)
    app = create_app(load_config(tmp_path / "none.yaml"), store, bus, hooks=rcv)
    return TestClient(app), store


def test_route_hook_name_from_header(tmp_path):
    client, store = _client(tmp_path)
    r = client.post(
        "/hooks?session_id=s1", headers={"X-Hook": "PostToolUse"}, json={"tool_name": "Edit"}
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert [e.type for e in store.get_events("s1")] == ["tool_post"]


def test_route_hook_name_from_body(tmp_path):
    client, store = _client(tmp_path)
    r = client.post("/hooks?session_id=s1", json={"hook_event_name": "Stop", "result": "done"})
    assert r.status_code == 200
    assert [e.type for e in store.get_events("s1")] == ["stop"]


def test_route_blocks_dangerous_action(tmp_path):
    client, store = _client(tmp_path, gate=Gate(GatesCfg(requires_approval=["rm -rf"])))
    r = client.post(
        "/hooks?session_id=s1",
        headers={"X-Hook": "PreToolUse"},
        json={"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
    )
    body = r.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert [e.type for e in store.get_events("s1")] == ["tool_pre", "approval_req"]


def test_route_503_without_receiver(tmp_path):
    client = TestClient(create_app(load_config(tmp_path / "none.yaml")))  # hooks=None
    assert client.post("/hooks", json={"hook_event_name": "Stop"}).status_code == 503
