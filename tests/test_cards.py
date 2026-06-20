"""Decision Card + step-detail drill-down (TASKS T4.3, DESIGN §6.3).

Covers the pure diff parser, the CardService (build/list cards + assemble raw return + per-line
diff with an injected checkpoint manager), and the REST routes (GET /api/cards, GET
/api/actions/{id}/detail) through the injected client store — proving app.py stays shared-only.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.core.cards import (
    CardService,
    diff_summary,
    format_diff_stat,
    parse_unified_diff,
)
from foreman.client.store import Store
from foreman.client.store.models import Action, Checkpoint, Session
from foreman.server.app import create_app
from foreman.shared.config import load_config
from foreman.shared.events import EventBus, make_event

# A small two-file unified diff: one modified file, one brand-new file (/dev/null old side).
SAMPLE_DIFF = """diff --git a/auth.py b/auth.py
index 1111111..2222222 100644
--- a/auth.py
+++ b/auth.py
@@ -1,4 +1,4 @@
 import os
-def login(u, p):
+def login(user, password):
     return True
 # end
diff --git a/new.py b/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+# brand new
+print("hi")
"""


# ── pure diff parser ─────────────────────────────────────────────────────────────────────────
def test_parse_unified_diff_multi_file_counts_and_paths():
    files = parse_unified_diff(SAMPLE_DIFF)
    assert [f.path for f in files] == ["auth.py", "new.py"]
    auth, new = files
    assert (auth.additions, auth.deletions) == (1, 1)
    assert (new.additions, new.deletions) == (2, 0)


def test_parse_unified_diff_tags_and_line_numbers():
    auth = parse_unified_diff(SAMPLE_DIFF)[0]
    add = [ln for ln in auth.lines if ln.kind == "add"]
    dele = [ln for ln in auth.lines if ln.kind == "del"]
    ctx = [ln for ln in auth.lines if ln.kind == "context"]
    assert add[0].text == "def login(user, password):"
    assert dele[0].text == "def login(u, p):"
    # context lines carry both old+new line numbers; add carries only new, del only old.
    assert add[0].new_n is not None and add[0].old_n is None
    assert dele[0].old_n is not None and dele[0].new_n is None
    assert ctx and ctx[0].old_n == 1 and ctx[0].new_n == 1


def test_parse_unified_diff_binary_and_empty():
    files = parse_unified_diff(
        "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    )
    assert len(files) == 1 and files[0].binary is True
    assert files[0].additions == 0 and files[0].deletions == 0
    assert parse_unified_diff("") == []


def test_diff_summary_and_stat_text():
    files = parse_unified_diff(SAMPLE_DIFF)
    summary = diff_summary(files)
    assert summary == {"files": 2, "additions": 3, "deletions": 1}
    assert format_diff_stat(summary) == "2 个文件 +3 / −1"


# ── CardService: build + list ─────────────────────────────────────────────────────────────────
def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "t.db"))
    s.init()
    return s


def test_build_and_list_cards(tmp_path):
    store = _store(tmp_path)
    svc = CardService(store, clock=lambda: "2026-01-01T00:00:00Z")
    card = svc.build_card(
        action_id="a1", session_id="s1", summary="抽成 hook，删了 80 行",
        audit_note="异常没测；其余 OK", diff_stat="3 个文件 +124 / −80",
    )
    assert card["summary"].startswith("抽成 hook")
    # default options are the 4 standard one-tap buttons (§6.3 mock).
    assert {o["action"] for o in card["options"]} == {"approve", "revise", "undo", "manual"}
    listed = svc.list_cards("s1")
    assert len(listed) == 1 and listed[0]["diff_stat"] == "3 个文件 +124 / −80"
    assert svc.list_cards("other") == []  # scoped per session


# ── CardService: step detail (raw return + per-line diff) ─────────────────────────────────────
class _FakeCkptMgr:
    """Stands in for CheckpointManager — returns a canned diff regardless of ref (no git)."""

    def __init__(self, diff_text: str):
        self._diff = diff_text

    def diff(self, from_ref, to_ref=None):
        return self._diff


def _ev(type, source, payload, ts):
    e = make_event(type, source, "s1", payload=payload)
    e.ts = ts  # pin the timestamp so the step-window scoping is deterministic
    return e


def _seed_step(store: Store) -> None:
    store.add_session(Session(id="s1", goal="g", workspace="/ws"))
    store.add_checkpoint(Checkpoint(
        id="c1", session_id="s1", step_index=0, vcs_ref="deadbeef", created_at="2026-01-01T00:00:00Z"
    ))
    store.add_checkpoint(Checkpoint(
        id="c2", session_id="s1", step_index=1, vcs_ref="cafe", created_at="2026-01-01T00:10:00Z"
    ))
    store.add_action(Action(id="a1", session_id="s1", command="edit auth.py", checkpoint_id="c1"))
    # events: two inside this step's window, one after (belongs to the next step), one non-raw.
    store.add_event(_ev("agent_output", "claude-code", {"t": "in"}, "2026-01-01T00:05:00Z"))
    store.add_event(_ev("tool_post", "hook", {"tool": "Edit"}, "2026-01-01T00:06:00Z"))
    store.add_event(_ev("agent_output", "claude-code", {"t": "after"}, "2026-01-01T00:15:00Z"))
    store.add_event(_ev("git_diff", "git", {"n": 1}, "2026-01-01T00:07:00Z"))


def test_step_detail_raw_return_scoped_to_checkpoint_window(tmp_path):
    store = _store(tmp_path)
    _seed_step(store)
    svc = CardService(store, checkpoint_factory=lambda ws: _FakeCkptMgr(SAMPLE_DIFF))
    detail = svc.step_detail("a1")
    # raw return: only the two raw-type events inside [c1, c2), not the later or non-raw ones.
    texts = [e["payload"].get("t") or e["payload"].get("tool") for e in detail["raw"]]
    assert texts == ["in", "Edit"]
    assert all(e["type"] in ("agent_output", "tool_post") for e in detail["raw"])


def test_step_detail_code_changes_per_file_per_line(tmp_path):
    store = _store(tmp_path)
    _seed_step(store)
    svc = CardService(store, checkpoint_factory=lambda ws: _FakeCkptMgr(SAMPLE_DIFF))
    diff = svc.step_detail("a1")["diff"]
    assert diff["summary"] == {"files": 2, "additions": 3, "deletions": 1}
    assert [f["path"] for f in diff["files"]] == ["auth.py", "new.py"]
    # per-line tags survive into the JSON payload.
    kinds = {ln["kind"] for f in diff["files"] for ln in f["lines"]}
    assert {"add", "del", "context"} <= kinds


def test_record_choice_persists_and_emits(tmp_path):
    import asyncio

    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace="/ws"))
    bus = EventBus()
    events = []
    q = bus.subscribe_queue()
    svc = CardService(store, bus=bus, clock=lambda: "2026-01-01T00:00:00Z")
    card = svc.build_card(action_id="a1", session_id="s1", summary="x")

    async def go():
        res = await svc.record_choice(card["id"], "approve")
        events.append(await asyncio.wait_for(q.get(), timeout=1))
        return res

    res = asyncio.run(go())
    assert res == {"ok": True, "id": card["id"], "chosen": "approve"}
    assert svc.list_cards()[0]["chosen"] == "approve"
    ev = events[0]
    assert ev.type == "card_decided" and ev.payload["execution_deferred"] is True


def test_record_choice_rejects_bad_option_and_unknown_card(tmp_path):
    import asyncio

    svc = CardService(_store(tmp_path))
    assert asyncio.run(svc.record_choice("c", "delete-everything"))["error"] == "bad_option"
    assert asyncio.run(svc.record_choice("missing", "approve"))["error"] == "not_found"


def test_step_detail_unknown_action_is_none(tmp_path):
    svc = CardService(_store(tmp_path))
    assert svc.step_detail("nope") is None


def test_step_detail_no_checkpoint_yields_empty_diff(tmp_path):
    store = _store(tmp_path)
    store.add_session(Session(id="s1", goal="g", workspace="/ws"))
    store.add_action(Action(id="a1", session_id="s1", command="x"))  # no checkpoint_id
    svc = CardService(store, checkpoint_factory=lambda ws: _FakeCkptMgr(SAMPLE_DIFF))
    detail = svc.step_detail("a1")
    assert detail["diff"]["files"] == [] and "note" in detail["diff"]


def test_step_detail_diff_failure_degrades_gracefully(tmp_path):
    store = _store(tmp_path)
    _seed_step(store)

    class _Boom:
        def diff(self, *a, **k):
            raise RuntimeError("not a repo")

    svc = CardService(store, checkpoint_factory=lambda ws: _Boom())
    diff = svc.step_detail("a1")["diff"]
    assert diff["files"] == [] and "unavailable" in diff["note"]


# ── REST routes ───────────────────────────────────────────────────────────────────────────────
def _app(tmp_path):
    store = _store(tmp_path)
    _seed_step(store)
    svc = CardService(store, checkpoint_factory=lambda ws: _FakeCkptMgr(SAMPLE_DIFF))
    svc.build_card(action_id="a1", session_id="s1", summary="改了登录", diff_stat="2 个文件 +3 / −1")
    return create_app(load_config(), store, EventBus(), cards=svc), store


def test_api_cards_lists(tmp_path):
    app, _ = _app(tmp_path)
    cards = TestClient(app).get("/api/cards").json()
    assert len(cards) == 1 and cards[0]["action_id"] == "a1"


def test_api_action_detail(tmp_path):
    app, _ = _app(tmp_path)
    detail = TestClient(app).get("/api/actions/a1/detail").json()
    assert detail["diff"]["summary"]["files"] == 2
    assert [e["type"] for e in detail["raw"]] == ["agent_output", "tool_post"]


def test_api_action_detail_404(tmp_path):
    app, _ = _app(tmp_path)
    assert TestClient(app).get("/api/actions/missing/detail").status_code == 404


def test_api_choose_card(tmp_path):
    app, store = _app(tmp_path)
    c = TestClient(app)
    card_id = c.get("/api/cards").json()[0]["id"]
    r = c.post(f"/api/cards/{card_id}/choose", json={"option": "approve"})
    assert r.status_code == 200 and r.json()["chosen"] == "approve"
    assert store.get_decision_card(card_id).chosen == "approve"
    # bad option → 400; unknown card → 404
    assert c.post(f"/api/cards/{card_id}/choose", json={"option": "nope"}).status_code == 400
    assert c.post("/api/cards/zzz/choose", json={"option": "approve"}).status_code == 404


def test_api_cards_503_without_service(tmp_path):
    app = create_app(load_config())  # no cards service injected
    c = TestClient(app)
    assert c.get("/api/cards").status_code == 503
    assert c.get("/api/actions/a1/detail").status_code == 503
