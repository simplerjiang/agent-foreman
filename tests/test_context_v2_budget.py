from __future__ import annotations

import json

import pytest

from foreman.client.core.context_v2 import (
    ActiveContext,
    ContextCompactError,
    ContextManager,
    ContextUsage,
    estimate_active_context_tokens,
    estimate_context_usage,
    should_hard_compact,
    should_soft_compact,
)
from foreman.client.store import Store
from foreman.client.store.models import Session


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "budget.db"))
    store.init()
    return store


def test_estimate_context_usage_percent_and_lane_usage():
    active = ActiveContext(
        rendered_text="x" * 280,
        frames_after_checkpoint=[
            {"type": "task", "lane": 1, "payload": {"text": "stable"}},
            {"type": "pm_reasoning", "lane": 7, "payload": {"text": "noise" * 100}},
        ],
    )

    usage = estimate_context_usage(active, 100)

    assert usage.used_tokens >= 70
    assert usage.window_tokens == 100
    assert usage.percent >= 0.70
    assert usage.tokens_until_soft_compact == 0
    assert usage.lane_usage["1"] > 0
    assert usage.lane_usage["7"] > 0


def test_usage_uses_uncapped_context_not_rendered_text():
    active = ActiveContext(
        rendered_text="short",
        replacement_history=[
            {
                "role": "assistant",
                "kind": "checkpoint_summary",
                "content": "prior decision " * 200,
                "source_refs": ["event:e1"],
            }
        ],
        frames_after_checkpoint=[
            {"type": "agent_output", "lane": 6, "payload": {"text": "new evidence " * 200}},
        ],
        runtime_state={"changed_files": ["src/app.py"]},
    )

    usage = estimate_context_usage(active, 600)

    assert usage.used_tokens > estimate_active_context_tokens(ActiveContext(rendered_text="short"))
    assert should_soft_compact(usage) is True


def test_large_lane7_noise_triggers_usage_even_when_rendered_text_capped():
    active = ActiveContext(
        rendered_text="short",
        frames_after_checkpoint=[
            {"type": "pm_reasoning", "lane": 7, "payload": {"text": f"noise {idx} " * 200}}
            for idx in range(10)
        ],
    )

    usage = estimate_context_usage(active, 500)

    assert usage.lane_usage["7"] > 0
    assert usage.percent >= usage.soft_threshold


def test_estimate_context_usage_window_zero_is_safe():
    usage = estimate_context_usage(ActiveContext(rendered_text="x" * 100), 0)

    assert usage.window_tokens == 0
    assert usage.percent == 0.0
    assert usage.tokens_until_soft_compact == 0
    assert usage.tokens_until_hard_compact == 0


@pytest.mark.parametrize(
    ("percent", "run_count", "soft", "hard"),
    [
        (0.69, 0, False, False),
        (0.70, 0, True, False),
        (0.90, 0, False, True),
        (0.10, 8, True, False),
        (0.10, 16, True, False),
        (0.10, 7, False, False),
    ],
)
def test_threshold_predicates(percent, run_count, soft, hard):
    usage = ContextUsage(
        used_tokens=int(percent * 100),
        window_tokens=100,
        percent=percent,
        tokens_until_soft_compact=0,
        tokens_until_hard_compact=0,
    )

    assert should_soft_compact(usage, run_count=run_count) is soft
    assert should_hard_compact(usage) is hard


class _Manager(ContextManager):
    def __init__(self, store, active: ActiveContext, *, fail: bool = False):
        super().__init__(store)
        self.active = active
        self.fail = fail
        self.compact_calls: list[dict] = []

    def build_active_context(self, session_id: str, *, purpose: str = "pm_plan", window_tokens: int = 0):
        return self.active

    async def compact_now(self, session_id: str, *, trigger: str, reason: str, window_tokens: int, hard: bool = False):
        self.compact_calls.append({"trigger": trigger, "reason": reason, "hard": hard})
        if self.fail:
            self._emit_compact_failed(
                session_id,
                hard=hard,
                method_attempted="local",
                error="forced",
                reason=reason,
            )
            raise ContextCompactError("forced")
        return object()


async def test_maybe_compact_no_trigger_returns_none(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    manager = _Manager(store, ActiveContext(rendered_text="x" * 100))

    assert await manager.maybe_compact("s1", reason="test", purpose="pm_plan", window_tokens=1000) is None
    assert manager.compact_calls == []


async def test_maybe_compact_soft_hard_and_run_count(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))

    soft = _Manager(store, ActiveContext(rendered_text="x" * 280))
    await soft.maybe_compact("s1", reason="soft", purpose="pm_plan", window_tokens=140)
    assert soft.compact_calls[-1]["hard"] is False

    hard = _Manager(store, ActiveContext(rendered_text="x" * 360))
    await hard.maybe_compact("s1", reason="hard", purpose="pm_plan", window_tokens=100)
    assert hard.compact_calls[-1]["hard"] is True

    run = _Manager(store, ActiveContext(rendered_text="x" * 40))
    await run.maybe_compact("s1", reason="run", purpose="pm_review", window_tokens=100, run_count=8)
    assert run.compact_calls[-1]["hard"] is False


async def test_maybe_compact_soft_failure_visible_and_non_blocking(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    manager = _Manager(store, ActiveContext(rendered_text="x" * 280), fail=True)

    assert await manager.maybe_compact("s1", reason="soft", purpose="pm_plan", window_tokens=140) is None
    failed = [json.loads(e.payload_json) for e in store.get_events("s1") if e.type == "context_compact"]
    assert failed[-1]["status"] == "failed"
    assert failed[-1]["hard"] is False


async def test_maybe_compact_hard_failure_raises(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))
    manager = _Manager(store, ActiveContext(rendered_text="x" * 360), fail=True)

    with pytest.raises(ContextCompactError):
        await manager.maybe_compact("s1", reason="hard", purpose="pm_plan", window_tokens=100)
    failed = [json.loads(e.payload_json) for e in store.get_events("s1") if e.type == "context_compact"]
    assert failed[-1]["status"] == "failed"
    assert failed[-1]["hard"] is True


async def test_maybe_compact_build_active_context_failure_is_soft_and_visible(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="goal"))

    class BrokenManager(ContextManager):
        def build_active_context(self, session_id: str, *, purpose: str = "pm_plan", window_tokens: int = 0):
            raise RuntimeError("restore boom")

    manager = BrokenManager(store)

    assert await manager.maybe_compact("s1", reason="pre_turn", purpose="pm_plan", window_tokens=100) is None
    payloads = [json.loads(e.payload_json) for e in store.get_events("s1") if e.type == "context_compact"]
    assert payloads[-1]["status"] == "warning"
    assert "maybe_compact_usage_estimate_failed" in payloads[-1]["warning"]
    assert "restore boom" in payloads[-1]["warning"]
