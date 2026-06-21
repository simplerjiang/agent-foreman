"""Display cache (T7.5, DESIGN §8.5 ③ / §7.2): the relay keeps a read-only copy of each
account's session summaries + decision cards so the PWA can view them while the PC is OFFLINE.

Covered here:
  • ServerStore cache helpers — account-scoped upsert (idempotent by natural key) + read.
  • DisplayCacheService — sync (bounds + skip malformed) and account-scoped reads.
  • Relay cache_sync frame — scoped to the AUTHENTICATED account (never the frame's account).
  • REST /api/cache/* — require_account, account-scoped, personal-mode 503.
  • client cache_sync builder — display-safe (no diff/raw content leaves the machine, §8.3).
  • End-to-end through the REAL /relay endpoint — a process pushes a snapshot, the owner reads
    it back via REST, another tenant cannot.

No network; everything runs against a real on-disk ServerStore (tmp file so every connection
sees the same db).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from foreman.client.cache_sync import build_cache_sync, card_summary, session_summary
from foreman.server.app import create_app
from foreman.server.auth_manager import AuthManager
from foreman.server.display_cache import (
    MAX_SUMMARY_BYTES,
    DisplayCacheService,
)
from foreman.server.relay import Relay, RelayClient
from foreman.server.store import ServerStore
from foreman.server.store.models import AccessKey, Account, CacheCard, CacheSession
from foreman.server.auth import hash_access_key
from foreman.shared.config import load_config
from foreman.shared.protocol import KIND_CACHE_SYNC, KIND_HEARTBEAT, KIND_HELLO, Envelope


def _store(tmp_path) -> ServerStore:
    st = ServerStore(str(tmp_path / "srv.db"))
    st.init()
    return st


def _cache(tmp_path):
    # deterministic ids so a re-sync of the same (account, session) is easy to reason about
    counter = {"n": 0}

    def gen_id() -> str:
        counter["n"] += 1
        return f"id{counter['n']}"

    return DisplayCacheService(_store(tmp_path), now=lambda: "2026-06-21T00:00:00+00:00", gen_id=gen_id)


# ── ServerStore cache helpers ─────────────────────────────────────────────────────────────────────
def test_upsert_cache_session_is_idempotent_by_account_and_session(tmp_path):
    st = _store(tmp_path)
    st.upsert_cache_session(
        CacheSession(id="x1", account_id="a", session_id="s1", summary_json='{"v":1}')
    )
    # same (account, session) → overwrite, not a second row
    st.upsert_cache_session(
        CacheSession(id="x2", account_id="a", session_id="s1", summary_json='{"v":2}')
    )
    rows = st.get_cache_sessions("a")
    assert len(rows) == 1 and rows[0].summary_json == '{"v":2}'
    assert rows[0].updated_at  # stamped


def test_cache_sessions_are_account_scoped(tmp_path):
    st = _store(tmp_path)
    st.upsert_cache_session(CacheSession(id="x1", account_id="a", session_id="s1"))
    st.upsert_cache_session(CacheSession(id="x2", account_id="b", session_id="s2"))
    assert [r.session_id for r in st.get_cache_sessions("a")] == ["s1"]
    assert [r.session_id for r in st.get_cache_sessions("b")] == ["s2"]


def test_upsert_cache_card_is_idempotent_and_scoped(tmp_path):
    st = _store(tmp_path)
    st.upsert_cache_card(CacheCard(id="c1", account_id="a", card_id="k1", status="pending"))
    st.upsert_cache_card(CacheCard(id="c2", account_id="a", card_id="k1", status="decided"))
    st.upsert_cache_card(CacheCard(id="c3", account_id="b", card_id="k2"))
    a_cards = st.get_cache_cards("a")
    assert len(a_cards) == 1 and a_cards[0].status == "decided"
    assert [r.card_id for r in st.get_cache_cards("b")] == ["k2"]


# ── DisplayCacheService.sync ────────────────────────────────────────────────────────────────────
def test_sync_stores_and_reads_back(tmp_path):
    c = _cache(tmp_path)
    res = c.sync(
        "a",
        sessions=[{"session_id": "s1", "summary": {"goal": "ship it", "status": "running"}}],
        cards=[{"card_id": "k1", "status": "pending", "payload": {"session_id": "s1", "summary": "diff?"}}],
    )
    assert res == {"ok": True, "sessions": 1, "cards": 1}

    sessions = c.list_sessions("a")
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["summary"]["goal"] == "ship it"
    assert sessions[0]["cached"] is True

    cards = c.list_cards("a")
    assert cards[0]["card_id"] == "k1" and cards[0]["status"] == "pending"
    assert cards[0]["payload"]["summary"] == "diff?" and cards[0]["cached"] is True


def test_sync_skips_malformed_items(tmp_path):
    c = _cache(tmp_path)
    res = c.sync(
        "a",
        sessions=[
            {"session_id": "ok", "summary": {"goal": "g"}},
            {"summary": {"goal": "no id"}},        # missing session_id → skip
            {"session_id": "bad", "summary": "not a dict"},  # non-dict summary → skip
            "garbage",                              # not even a dict → skip
        ],
        cards=[{"card_id": "", "payload": {}}],     # blank card_id → skip
    )
    assert res["sessions"] == 1 and res["cards"] == 0
    assert [s["session_id"] for s in c.list_sessions("a")] == ["ok"]


def test_sync_drops_oversized_payload(tmp_path):
    c = _cache(tmp_path)
    huge = "x" * (MAX_SUMMARY_BYTES + 1)
    res = c.sync("a", sessions=[{"session_id": "s1", "summary": {"blob": huge}}])
    assert res["sessions"] == 0                     # oversized dropped, never truncated
    assert c.list_sessions("a") == []


def test_sync_fail_closed_on_non_list_inputs(tmp_path):
    """A non-list sessions/cards (object, number) from a buggy/hostile process is treated as
    nothing-to-sync — it must NOT crash sync() (which would tear down the relay connection)."""
    c = _cache(tmp_path)
    res = c.sync("a", sessions={"session_id": "x"}, cards=5)  # dict / int — would crash naive slicing
    assert res == {"ok": True, "sessions": 0, "cards": 0}
    assert c.list_sessions("a") == [] and c.list_cards("a") == []


def test_sync_skips_falsy_non_dict_summary(tmp_path):
    """summary present but a falsy non-dict ([], 0, "") is malformed → skipped, same as a truthy
    non-dict; only an absent summary defaults to {} and is stored."""
    c = _cache(tmp_path)
    res = c.sync(
        "a",
        sessions=[
            {"session_id": "s1", "summary": []},        # falsy non-dict → skip
            {"session_id": "s2"},                        # absent → default {} → store
        ],
    )
    assert res["sessions"] == 1
    assert [s["session_id"] for s in c.list_sessions("a")] == ["s2"]


def test_sync_requires_an_account(tmp_path):
    c = _cache(tmp_path)
    assert c.sync("", sessions=[{"session_id": "s1", "summary": {}}]) == {
        "ok": False, "error": "no_account"
    }


def test_sync_is_multitenant_isolated(tmp_path):
    c = _cache(tmp_path)
    c.sync("a", sessions=[{"session_id": "sa", "summary": {"goal": "alice"}}])
    c.sync("b", sessions=[{"session_id": "sb", "summary": {"goal": "bob"}}])
    assert [s["session_id"] for s in c.list_sessions("a")] == ["sa"]
    assert [s["session_id"] for s in c.list_sessions("b")] == ["sb"]


def test_list_cards_can_filter_by_session(tmp_path):
    c = _cache(tmp_path)
    c.sync(
        "a",
        cards=[
            {"card_id": "k1", "payload": {"session_id": "s1"}},
            {"card_id": "k2", "payload": {"session_id": "s2"}},
        ],
    )
    assert {x["card_id"] for x in c.list_cards("a")} == {"k1", "k2"}
    assert [x["card_id"] for x in c.list_cards("a", session_id="s1")] == ["k1"]
    assert c.list_cards("a", session_id="nope") == []


# ── Relay cache_sync frame ──────────────────────────────────────────────────────────────────────
class _SendOnlyWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


async def test_relay_cache_sync_scopes_to_authenticated_account(tmp_path):
    """The relay caches under the connection's account (from the key), NEVER any account in
    the frame — a hostile process can't write into another tenant's cache (§8.4)."""
    c = _cache(tmp_path)
    relay = Relay(c.store, cache=c)
    client = RelayClient(account_id="a", process_id="p1", key_id="k1", name="box", ws=_SendOnlyWS())
    frame = Envelope(
        kind=KIND_CACHE_SYNC,
        account_id="EVIL-OTHER-ACCOUNT",  # must be ignored
        payload={"sessions": [{"session_id": "s1", "summary": {"goal": "g"}}]},
    ).to_dict()
    await relay._on_frame(client, frame)
    assert [s["session_id"] for s in c.list_sessions("a")] == ["s1"]  # cached under "a"
    assert c.list_sessions("EVIL-OTHER-ACCOUNT") == []                # not the frame's account


async def test_relay_without_cache_drops_sync_without_crashing(tmp_path):
    relay = Relay(_store(tmp_path), cache=None)  # routing-only deployment
    client = RelayClient(account_id="a", process_id="p1", key_id="k1", name="", ws=_SendOnlyWS())
    await relay._on_frame(
        client, Envelope(kind=KIND_CACHE_SYNC, payload={"sessions": []}).to_dict()
    )  # must not raise


# ── REST /api/cache/* ───────────────────────────────────────────────────────────────────────────
def _rest(tmp_path, *, with_cache=True):
    cfg = load_config(tmp_path / "none.yaml")
    st = _store(tmp_path)
    auth = AuthManager(st)
    cache = DisplayCacheService(st) if with_cache else None
    return TestClient(create_app(cfg, auth=auth, cache=cache)), auth, cache


def _token(client, auth, username, password="pw", role="member"):
    auth.create_account(username, password, role=role)
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]


def test_cache_endpoints_require_auth_and_scope_to_caller(tmp_path):
    client, auth, cache = _rest(tmp_path)
    ta = _token(client, auth, "alice")
    tb = _token(client, auth, "bob")
    a = auth.resolve_token(ta).id
    b = auth.resolve_token(tb).id
    cache.sync(a, sessions=[{"session_id": "sa", "summary": {"goal": "alice"}}],
               cards=[{"card_id": "ka", "payload": {"session_id": "sa"}}])
    cache.sync(b, sessions=[{"session_id": "sb", "summary": {"goal": "bob"}}])

    # no token → 401
    assert client.get("/api/cache/sessions").status_code == 401
    assert client.get("/api/cache/cards").status_code == 401

    # alice sees only her own
    r = client.get("/api/cache/sessions", headers={"Authorization": f"Bearer {ta}"})
    assert r.status_code == 200
    assert {s["session_id"] for s in r.json()} == {"sa"}
    rc = client.get("/api/cache/cards", headers={"Authorization": f"Bearer {ta}"})
    assert {x["card_id"] for x in rc.json()} == {"ka"}

    # bob never sees alice's cache
    rb = client.get("/api/cache/sessions", headers={"Authorization": f"Bearer {tb}"})
    assert {s["session_id"] for s in rb.json()} == {"sb"}


def test_cache_endpoints_503_without_cache_service(tmp_path):
    """A relay box with auth but no display cache (or personal mode) → 503 after auth."""
    client, auth, _ = _rest(tmp_path, with_cache=False)
    ta = _token(client, auth, "alice")
    assert client.get(
        "/api/cache/sessions", headers={"Authorization": f"Bearer {ta}"}
    ).status_code == 503


def test_cache_cards_session_filter_over_rest(tmp_path):
    client, auth, cache = _rest(tmp_path)
    ta = _token(client, auth, "alice")
    a = auth.resolve_token(ta).id
    cache.sync(a, cards=[
        {"card_id": "k1", "payload": {"session_id": "s1"}},
        {"card_id": "k2", "payload": {"session_id": "s2"}},
    ])
    r = client.get(
        "/api/cache/cards", params={"session_id": "s1"},
        headers={"Authorization": f"Bearer {ta}"},
    )
    assert [x["card_id"] for x in r.json()] == ["k1"]


# ── client cache_sync builder: only display-safe data leaves the machine (§8.3) ───────────────────
def test_session_summary_is_display_safe():
    s = SimpleNamespace(
        id="s1", goal="add feature", status="running", agent_type="claude-code",
        created_at="t0", updated_at="t1",
    )
    out = session_summary(s)
    assert out["session_id"] == "s1"
    assert out["summary"] == {
        "goal": "add feature", "status": "running", "agent_type": "claude-code",
        "created_at": "t0", "updated_at": "t1",
    }


def test_card_summary_carries_diff_stat_line_not_the_diff():
    card = SimpleNamespace(
        id="k1", session_id="s1", summary="changed login", audit_note="looks ok",
        diff_stat="3 files +124 / −80", options_json='[{"label":"approve"}]',
        chosen="", decided_at="", ts="t0",
    )
    out = card_summary(card)
    assert out["card_id"] == "k1" and out["status"] == "pending"
    p = out["payload"]
    assert p["diff_stat"] == "3 files +124 / −80"      # the summary LINE is display-safe
    assert p["options"] == [{"label": "approve"}]
    # no field carries a raw diff / raw agent output (§8.3) — only the folded summary leaves
    assert set(p) == {"session_id", "summary", "audit_note", "diff_stat", "options", "chosen",
                      "decided_at", "ts"}


def test_build_cache_sync_frame_shape():
    s = SimpleNamespace(id="s1", goal="g", status="idle", agent_type="codex",
                        created_at="", updated_at="")
    card = SimpleNamespace(id="k1", session_id="s1", summary="", audit_note="", diff_stat="",
                          options_json="[]", chosen="x", decided_at="t", ts="t")
    env = build_cache_sync([s], [card])
    assert env.kind == KIND_CACHE_SYNC
    assert env.payload["sessions"][0]["session_id"] == "s1"
    assert env.payload["cards"][0]["status"] == "decided"  # chosen set → decided


def test_card_summary_tolerates_bad_options_json():
    card = SimpleNamespace(id="k1", session_id="s1", summary="", audit_note="", diff_stat="",
                          options_json="{not json", chosen="", decided_at="", ts="")
    assert card_summary(card)["payload"]["options"] == []


# ── end-to-end through the REAL /relay endpoint ───────────────────────────────────────────────────
def test_e2e_process_pushes_snapshot_owner_reads_it_back(tmp_path):
    """A local process dials the real /relay, pushes a cache_sync snapshot; the owning account
    reads it back via REST while another tenant sees nothing (DESIGN §8.5 ③ + §8.4)."""
    st = _store(tmp_path)
    auth = AuthManager(st)
    cache = DisplayCacheService(st)
    relay = Relay(st, cache=cache)
    cfg = load_config(tmp_path / "none.yaml")
    client = TestClient(create_app(cfg, relay=relay, auth=auth, cache=cache))

    # alice has a login (to read the cache) + an access key (for her machine to dial in); bob too.
    auth.create_account("alice", "pw")
    auth.create_account("bob", "pw")
    ta = client.post("/api/auth/login", json={"username": "alice", "password": "pw"}).json()["token"]
    tb = client.post("/api/auth/login", json={"username": "bob", "password": "pw"}).json()["token"]
    a = auth.resolve_token(ta).id
    keyplain = auth.create_access_key(a, label="alice-box")["key"]

    snapshot = Envelope(
        kind=KIND_CACHE_SYNC,
        payload={
            "sessions": [{"session_id": "s1", "summary": {"goal": "ship", "status": "running"}}],
            "cards": [{"card_id": "k1", "status": "pending", "payload": {"session_id": "s1"}}],
        },
    ).to_dict()

    with client.websocket_connect("/relay") as ws:
        ws.send_json(Envelope(kind=KIND_HELLO, payload={"access_key": keyplain, "name": "box"}).to_dict())
        assert ws.receive_json()["payload"]["ok"] is True
        ws.send_json(snapshot)
        # heartbeat ping AFTER the snapshot → its pong proves the (in-order) snapshot was processed
        ws.send_json(Envelope(kind=KIND_HEARTBEAT, payload={"ping": True}).to_dict())
        assert ws.receive_json()["payload"]["pong"] is True

    # alice reads her cached snapshot back (the read-only offline copy)
    r = client.get("/api/cache/sessions", headers={"Authorization": f"Bearer {ta}"})
    assert r.status_code == 200
    body = r.json()
    assert body[0]["session_id"] == "s1" and body[0]["summary"]["goal"] == "ship"
    rc = client.get("/api/cache/cards", headers={"Authorization": f"Bearer {ta}"})
    assert [x["card_id"] for x in rc.json()] == ["k1"]

    # bob (a different tenant) sees nothing — alice's snapshot is scoped to alice (§8.4)
    assert client.get("/api/cache/sessions", headers={"Authorization": f"Bearer {tb}"}).json() == []


def test_e2e_cache_holds_no_access_key_or_secret(tmp_path):
    """Sanity: what the relay caches is display data only — no key hash / plaintext leaks in."""
    st = _store(tmp_path)
    cache = DisplayCacheService(st)
    st.add_account(Account(id="a", username="alice"))
    st.add_access_key(AccessKey(id="k1", account_id="a", key_hash=hash_access_key("super-secret")))
    cache.sync("a", sessions=[{"session_id": "s1", "summary": {"goal": "g"}}])
    blob = json.dumps(cache.list_sessions("a") + cache.list_cards("a"))
    for leak in ("super-secret", hash_access_key("super-secret"), "key_hash"):
        assert leak not in blob
