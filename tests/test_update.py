"""Self-update (便携版一键自更新) — version compare, check gating, and the in-place swap mechanics.

These never hit the network or spawn a process: _fetch_latest and subprocess.Popen are monkeypatched.
The swap test exercises the real file renames on a temp folder to prove ONLY the exe files move and
the (stand-in) data files are left untouched.
"""

from __future__ import annotations

import pytest

from foreman.client import update


def test_parse_version_and_ordering():
    assert update.parse_version("1.0.2") == (1, 0, 2)
    assert update.parse_version("v1.0.10") == (1, 0, 10)
    assert update.parse_version("") == (0,)
    # carry / ordering the AGENTS.md §四 scheme relies on
    assert update.parse_version("1.0.10") > update.parse_version("1.0.9")
    assert update.parse_version("1.1.0") > update.parse_version("1.0.9")
    assert update.parse_version("2.0.0") > update.parse_version("1.9.9")
    assert update.parse_version("1.0.2") > update.parse_version("1.0.1")


def test_check_from_source_never_offers(monkeypatch):
    """From source (not frozen) there is no exe to swap → available must be False even if newer."""
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    monkeypatch.setattr(update, "current_version", lambda: "1.0.1")
    monkeypatch.setattr(update, "_fetch_latest",
                        lambda timeout=6.0: {"version": "9.9.9", "url": "http://x/foo.exe",
                                             "size": 1, "name": "foo", "notes": ""})
    out = update.Updater().check()
    assert out["frozen"] is False
    assert out["available"] is False
    assert out["latest"] == "9.9.9"


def test_check_offers_when_frozen_and_newer(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "1.0.1")
    monkeypatch.setattr(update, "_fetch_latest",
                        lambda timeout=6.0: {"version": "1.0.2", "url": "http://x/f.exe",
                                             "size": 10, "name": "f", "notes": "notes"})
    out = update.Updater().check()
    assert out["available"] is True
    assert out["latest"] == "1.0.2"
    assert out["notes"] == "notes"


def test_check_no_offer_when_same_version(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "1.0.2")
    monkeypatch.setattr(update, "_fetch_latest",
                        lambda timeout=6.0: {"version": "1.0.2", "url": "http://x/f.exe",
                                             "size": 10, "name": "f", "notes": ""})
    assert update.Updater().check()["available"] is False


def test_check_survives_network_failure(monkeypatch):
    def boom(timeout=6.0):
        raise RuntimeError("offline")

    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "_fetch_latest", boom)
    out = update.Updater().check()
    assert out["available"] is False
    assert "error" in out


def test_begin_apply_refused_from_source(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: False)
    res = update.Updater().begin_apply()
    assert res["ok"] is False
    assert res["reason"] == "not_frozen"


def test_parse_kv():
    out = update._parse_kv(["--old-pid", "1234", "--target", "C:\\a b\\foreman.exe"])
    assert out["old-pid"] == "1234"
    assert out["target"] == "C:\\a b\\foreman.exe"


def test_cleanup_stale_removes_leftovers(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    old = tmp_path / update.OLD_EXE
    new = tmp_path / update.NEW_EXE
    old.write_text("old")
    new.write_text("new")
    keep = tmp_path / "foreman.db"
    keep.write_text("data")
    update.cleanup_stale(tmp_path)
    assert not old.exists()
    assert not new.exists()
    assert keep.read_text() == "data"  # data file untouched


def test_run_swap_replaces_exe_only(monkeypatch, tmp_path):
    """The swap renames foreman.new.exe → foreman.exe (old → foreman.old.exe) and relaunches, while
    the sibling data files are never referenced. old-pid 0 short-circuits the wait."""
    target = tmp_path / "foreman.exe"
    new_exe = tmp_path / update.NEW_EXE
    target.write_text("OLD")
    new_exe.write_text("NEW")
    db = tmp_path / "foreman.db"
    cfg = tmp_path / "config.yaml"
    env = tmp_path / ".env"
    for f in (db, cfg, env):
        f.write_text("DATA")

    monkeypatch.setattr(update, "exe_path", lambda: new_exe)  # we ARE foreman.new.exe
    launched: list = []
    monkeypatch.setattr(update.subprocess, "Popen",
                        lambda *a, **k: launched.append((a, k)) or object())

    update.run_swap(["--old-pid", "0", "--target", str(target)])

    assert target.read_text() == "NEW"                       # new binary swapped in
    assert (tmp_path / update.OLD_EXE).read_text() == "OLD"  # old kept as backup for rollback
    assert not new_exe.exists()                              # new.exe renamed away
    # data files all untouched
    assert db.read_text() == "DATA" and cfg.read_text() == "DATA" and env.read_text() == "DATA"
    # relaunched the new exe in the exe folder (cwd) so it keeps finding the data
    assert launched and launched[0][1]["cwd"] == str(tmp_path)
    assert launched[0][0][0] == [str(target), "app"]


def test_run_swap_aborts_without_target(monkeypatch, tmp_path):
    # Missing --target must not raise and must not touch anything.
    monkeypatch.setattr(update, "exe_path", lambda: tmp_path / update.NEW_EXE)
    update.run_swap(["--old-pid", "0"])  # no target → no-op, no exception


def test_is_frozen_method_matches_module(monkeypatch):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    assert update.Updater().is_frozen() is True


def test_update_routes_without_updater():
    """The team server (no updater injected) reports unavailable and refuses apply."""
    from fastapi.testclient import TestClient

    from foreman.server.app import create_app
    from foreman.shared.config import load_config

    c = TestClient(create_app(load_config()))
    chk = c.get("/api/update/check").json()
    assert chk["available"] is False and chk["frozen"] is False and "current" in chk
    assert c.post("/api/update/apply").status_code == 400


def test_update_routes_with_injected_updater(monkeypatch):
    """The local app injects a frozen-reporting Updater → check offers + apply starts."""
    from fastapi.testclient import TestClient

    from foreman.server.app import create_app
    from foreman.shared.config import load_config

    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "1.0.1")
    monkeypatch.setattr(update, "_fetch_latest",
                        lambda timeout=6.0: {"version": "1.0.2", "url": "http://x/f.exe",
                                             "size": 1, "name": "f", "notes": ""})
    up = update.Updater()
    # Don't actually download/relaunch when apply fires.
    monkeypatch.setattr(up, "_apply_worker", lambda info: None)

    c = TestClient(create_app(load_config(), updater=up))
    chk = c.get("/api/update/check").json()
    assert chk["available"] is True and chk["latest"] == "1.0.2"
    res = c.post("/api/update/apply").json()
    assert res["ok"] is True and res["started"] is True


@pytest.mark.parametrize("body", ["x" * 5000])
def test_check_truncates_notes(monkeypatch, body):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "1.0.1")
    # _fetch_latest itself truncates to 4000; simulate that contract holds end-to-end.
    monkeypatch.setattr(update, "_fetch_latest",
                        lambda timeout=6.0: {"version": "1.0.2", "url": "u", "size": 1,
                                             "name": "n", "notes": body[:4000]})
    assert len(update.Updater().check()["notes"]) == 4000
