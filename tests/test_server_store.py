"""Tests for the server store r/w helpers (TASKS T3.1, DESIGN §7.2).

Server (team/relay) side: accounts / access_keys / process_registry. Uses a tmp_path sqlite
FILE so every connection sees the same db (`:memory:` would give each its own).
"""

from __future__ import annotations

from foreman.server.store import SERVER_SCHEMA_VERSION, ServerStore
from foreman.server.store.models import (
    AccessKey,
    Account,
    Notification,
    ProcessRegistry,
    ServerSchemaVersion,
)


def _store(tmp_path) -> ServerStore:
    st = ServerStore(str(tmp_path / "srv.db"))
    st.init()
    return st


# ── schema version ───────────────────────────────────────────────────────────────────────────
def test_schema_version_recorded(tmp_path):
    st = _store(tmp_path)
    with st.session() as s:
        sv = s.get(ServerSchemaVersion, SERVER_SCHEMA_VERSION)
    assert sv is not None and sv.applied_at


def test_init_is_idempotent(tmp_path):
    st = _store(tmp_path)
    from sqlmodel import select

    with st.session() as s:
        before = {r.version for r in s.exec(select(ServerSchemaVersion)).all()}
    st.init()  # second call must not duplicate any schema_version ledger row or raise
    with st.session() as s:
        after = {r.version for r in s.exec(select(ServerSchemaVersion)).all()}
    assert before == after == {1, 2, 3}  # one ledger row per applied migration, no duplicates
    assert st.schema_version() == SERVER_SCHEMA_VERSION


# ── accounts ─────────────────────────────────────────────────────────────────────────────────
def test_account_roundtrip_and_lookup(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice", display_name="Alice", role="admin"))
    st.add_account(Account(id="a2", username="bob"))

    assert st.get_account("a1").display_name == "Alice"
    assert st.get_account_by_username("bob").id == "a2"
    assert st.get_account_by_username("nobody") is None
    assert {a.id for a in st.get_accounts()} == {"a1", "a2"}


def test_add_account_stamps_created_at(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    assert st.get_account("a1").created_at  # auto-stamped when unset


def test_set_account_status(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    st.set_account_status("a1", "disabled")
    assert st.get_account("a1").status == "disabled"
    st.set_account_status("ghost", "disabled")  # no-op, must not raise


# ── access keys ──────────────────────────────────────────────────────────────────────────────
def test_access_key_stored_by_hash_and_listed_newest_first(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    st.add_access_key(
        AccessKey(id="k1", account_id="a1", key_hash="h1", label="desktop", created_at="2026-01-01")
    )
    st.add_access_key(
        AccessKey(id="k2", account_id="a1", key_hash="h2", label="laptop", created_at="2026-02-01")
    )

    keys = st.get_access_keys("a1")
    assert [k.id for k in keys] == ["k2", "k1"]  # newest (created_at) first
    # only the hash is persisted, never plaintext
    assert all(not hasattr(k, "key_plain") for k in keys)
    assert st.get_access_key_by_hash("h2").id == "k2"
    assert st.get_access_key_by_hash("nope") is None


def test_revoke_access_key_keeps_others(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    st.add_access_key(AccessKey(id="k1", account_id="a1", key_hash="h1"))
    st.add_access_key(AccessKey(id="k2", account_id="a1", key_hash="h2"))

    st.revoke_access_key("k1")
    by_id = {k.id: k.status for k in st.get_access_keys("a1")}
    assert by_id == {"k1": "revoked", "k2": "active"}
    # revoked key is still resolvable by hash; status surfaced for the caller to reject
    assert st.get_access_key_by_hash("h1").status == "revoked"


def test_touch_access_key_sets_last_seen(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    st.add_access_key(AccessKey(id="k1", account_id="a1", key_hash="h1"))
    st.touch_access_key("k1", when="2026-06-20T00:00:00Z")
    assert st.get_access_keys("a1")[0].last_seen_at == "2026-06-20T00:00:00Z"


# ── process registry ─────────────────────────────────────────────────────────────────────────
def test_register_process_upsert(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    st.add_access_key(AccessKey(id="k1", account_id="a1", key_hash="h1"))

    st.register_process(
        ProcessRegistry(id="p1", account_id="a1", access_key_id="k1", name="box", online=True)
    )
    created = st.get_processes("a1")[0].created_at
    assert created  # stamped on first insert

    # re-register same id -> updates in place, keeps created_at, no duplicate row
    st.register_process(
        ProcessRegistry(id="p1", account_id="a1", access_key_id="k1", name="box-renamed", online=False)
    )
    updated = st.register_process(
        ProcessRegistry(id="p1", account_id="a1", access_key_id="k1", name="box-again", online=True)
    )
    assert updated.created_at == created  # update path returns the persisted row, not the input

    procs = st.get_processes("a1")
    assert len(procs) == 1
    assert procs[0].name == "box-again" and procs[0].online is True
    assert procs[0].created_at == created


def test_register_process_refuses_cross_account_rehome(tmp_path):
    """Defense-in-depth (DESIGN §8.4): a registry row is never re-homed to another account."""
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    st.add_account(Account(id="a2", username="bob"))
    st.add_access_key(AccessKey(id="k1", account_id="a1", key_hash="h1"))
    st.add_access_key(AccessKey(id="k2", account_id="a2", key_hash="h2"))
    st.register_process(ProcessRegistry(id="p1", account_id="a1", access_key_id="k1", name="alice-box"))

    # a2 tries to hijack p1 -> upsert refused, row stays with a1 untouched
    returned = st.register_process(
        ProcessRegistry(id="p1", account_id="a2", access_key_id="k2", name="stolen", online=True)
    )
    assert returned.account_id == "a1" and returned.name == "alice-box"
    assert [p.id for p in st.get_processes("a1")] == ["p1"]
    assert st.get_processes("a2") == []  # nothing leaked to the attacker's account


def test_set_process_online_and_online_filter(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    st.add_account(Account(id="a2", username="bob"))
    st.add_access_key(AccessKey(id="k1", account_id="a1", key_hash="h1"))
    st.add_access_key(AccessKey(id="k2", account_id="a2", key_hash="h2"))
    st.register_process(ProcessRegistry(id="p1", account_id="a1", access_key_id="k1", online=False))
    st.register_process(ProcessRegistry(id="p2", account_id="a2", access_key_id="k2", online=True))

    st.set_process_online("p1", True, last_heartbeat="2026-06-20T00:00:00Z")
    assert {p.id for p in st.get_online_processes()} == {"p1", "p2"}
    assert {p.id for p in st.get_online_processes("a1")} == {"p1"}  # scoped to account

    st.set_process_online("p2", False)
    assert {p.id for p in st.get_online_processes()} == {"p1"}
    st.set_process_online("ghost", True)  # no-op, must not raise


def test_process_registry_isolated_per_account(tmp_path):
    st = _store(tmp_path)
    st.add_account(Account(id="a1", username="alice"))
    st.add_account(Account(id="a2", username="bob"))
    st.add_access_key(AccessKey(id="k1", account_id="a1", key_hash="h1"))
    st.add_access_key(AccessKey(id="k2", account_id="a2", key_hash="h2"))
    st.register_process(ProcessRegistry(id="p1", account_id="a1", access_key_id="k1"))
    st.register_process(ProcessRegistry(id="p2", account_id="a2", access_key_id="k2"))

    assert [p.id for p in st.get_processes("a1")] == ["p1"]
    assert [p.id for p in st.get_processes("a2")] == ["p2"]


# ── remote-control notifications ───────────────────────────────────────────────────────────
def test_notification_queue_upsert_ack_ttl_and_limit(tmp_path):
    st = _store(tmp_path)
    st.upsert_notification(
        Notification(
            id="n1",
            account_id="a1",
            process_id="p1",
            kind="decision_needed",
            ref="c1",
            title="old",
            dedup_key="p1:c1",
            created_at="2026-06-20T00:00:00+00:00",
            expires_at="2026-06-27T00:00:00+00:00",
        ),
        per_account_limit=2,
    )
    st.upsert_notification(
        Notification(
            id="n2",
            account_id="a1",
            process_id="p1",
            kind="decision_needed",
            ref="c1",
            title="new",
            dedup_key="p1:c1",
            created_at="2026-06-21T00:00:00+00:00",
            expires_at="2026-06-28T00:00:00+00:00",
        ),
        per_account_limit=2,
    )
    rows = st.list_notifications("a1", now="2026-06-22T00:00:00+00:00")
    assert len(rows) == 1 and rows[0].title == "new"

    st.upsert_notification(Notification(id="n3", account_id="a1", process_id="p1", kind="result_ready", ref="s1", dedup_key="p1:s1", created_at="2026-06-22T00:00:00+00:00", expires_at="2026-06-29T00:00:00+00:00"), per_account_limit=2)
    st.upsert_notification(Notification(id="n4", account_id="a1", process_id="p1", kind="result_ready", ref="s2", dedup_key="p1:s2", created_at="2026-06-23T00:00:00+00:00", expires_at="2026-06-30T00:00:00+00:00"), per_account_limit=2)
    assert [n.id for n in st.list_notifications("a1", now="2026-06-24T00:00:00+00:00")] == ["n4", "n3"]

    assert st.ack_notifications("a1", ["n3"], now="2026-06-24T00:00:00+00:00") == 1
    assert [n.id for n in st.list_notifications("a1", now="2026-06-24T00:00:00+00:00")] == ["n4"]

    st.upsert_notification(Notification(id="old", account_id="a1", process_id="p1", kind="result_ready", ref="s-old", dedup_key="old", created_at="2026-01-01T00:00:00+00:00", expires_at="2026-01-02T00:00:00+00:00"))
    assert st.sweep_notifications(now="2026-06-24T00:00:00+00:00") == 1


def test_push_subscriptions_are_account_scoped_and_replace_by_endpoint(tmp_path):
    st = _store(tmp_path)
    st.add_push_subscription(account_id="a1", endpoint="https://push/1", p256dh="p", auth="a")
    st.add_push_subscription(account_id="a1", endpoint="https://push/1", p256dh="p2", auth="a2")
    st.add_push_subscription(account_id="a2", endpoint="https://push/1", p256dh="pb", auth="ab")

    a1 = st.get_push_subscriptions("a1")
    assert len(a1) == 1 and a1[0].p256dh == "p2"
    assert len(st.get_push_subscriptions("a2")) == 1
    assert st.delete_push_subscription("https://push/1", account_id="a1") == 1
    assert st.get_push_subscriptions("a1") == []
    assert len(st.get_push_subscriptions("a2")) == 1
