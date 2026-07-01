"""Self-update (便携版一键自更新) — version compare, check gating, and the in-place swap mechanics.

These never hit the network or spawn a process: _fetch_latest and subprocess.Popen are monkeypatched.
The swap test exercises the real file renames on a temp folder to prove ONLY the exe files move and
the (stand-in) data files are left untouched.
"""

from __future__ import annotations

import threading
import time

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
    monkeypatch.setattr(update, "_fetch_update_changes", lambda current, latest, timeout: [])
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


def test_begin_apply_reports_progress_and_can_cancel(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "1.0.1")
    monkeypatch.setattr(update, "exe_path", lambda: tmp_path / "foreman.exe")
    monkeypatch.setattr(update, "_fetch_latest",
                        lambda timeout=6.0: {"version": "1.0.2", "url": "http://x/f.exe",
                                             "size": 100, "name": "f", "notes": ""})
    monkeypatch.setattr(update, "_fetch_update_changes", lambda current, latest, timeout: [])
    started = threading.Event()

    def fake_download(url, dest, expected_size, on_progress=None, cancel_event=None):
        assert expected_size == 100
        assert cancel_event is not None
        if on_progress is not None:
            on_progress(25, 100)
        started.set()
        while not cancel_event.is_set():
            time.sleep(0.01)
        raise update.UpdateCancelled()

    monkeypatch.setattr(update, "_download", fake_download)
    up = update.Updater()
    assert up.check()["available"] is True
    assert up.begin_apply()["ok"] is True
    assert started.wait(1.0)

    status = up.status()
    assert status["applying"] is True
    assert status["phase"] == "downloading"
    assert status["downloaded"] == 25
    assert status["total"] == 100
    assert status["percent"] == 25.0

    assert up.cancel_apply()["ok"] is True
    for _ in range(100):
        status = up.status()
        if status["phase"] == "cancelled":
            break
        time.sleep(0.01)
    assert status["phase"] == "cancelled"
    assert status["applying"] is False


def test_parse_kv():
    out = update._parse_kv(["--old-pid", "1234", "--target", "C:\\a b\\foreman.exe"])
    assert out["old-pid"] == "1234"
    assert out["target"] == "C:\\a b\\foreman.exe"


def test_cleanup_stale_removes_leftovers(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    old = tmp_path / update.OLD_EXE
    old_unique = tmp_path / "foreman.old.123.456.0.exe"
    new = tmp_path / update.NEW_EXE
    old.write_text("old")
    old_unique.write_text("old unique")
    new.write_text("new")
    keep = tmp_path / "foreman.db"
    keep.write_text("data")
    update.cleanup_stale(tmp_path)
    assert not old.exists()
    assert not old_unique.exists()
    assert not new.exists()
    assert keep.read_text() == "data"  # data file untouched


def test_backup_path_uses_unique_name_when_existing_old_is_locked(monkeypatch, tmp_path):
    target = tmp_path / "foreman.exe"
    locked = tmp_path / update.OLD_EXE
    locked.write_text("LOCKED")
    messages: list[str] = []

    monkeypatch.setattr(update, "_retry_with_error",
                        lambda *a, **k: (False, OSError(5, "locked")))

    backup = update._backup_path(target, type("Log", (), {"write": messages.append})())

    assert backup.name.startswith("foreman.old.")
    assert backup != locked
    assert locked.read_text() == "LOCKED"
    assert messages and "using" in messages[0]


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


def test_run_swap_terminates_stale_target_process_before_replace(monkeypatch, tmp_path):
    target = tmp_path / "foreman.exe"
    new_exe = tmp_path / update.NEW_EXE
    target.write_text("OLD")
    new_exe.write_text("NEW")

    monkeypatch.setattr(update, "exe_path", lambda: new_exe)
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: object())
    blocker = {"pid": 4242, "name": "foreman.exe", "exe": str(target)}
    terminated: list[dict[str, object]] = []

    def fake_wait(path, timeout, exclude_pids):
        assert path == target
        assert update.os.getpid() in exclude_pids
        return [] if terminated else [blocker]

    monkeypatch.setattr(update, "_wait_processes_using_path_gone", fake_wait)
    monkeypatch.setattr(update, "_terminate_processes",
                        lambda processes, timeout: terminated.extend(processes))

    update.run_swap(["--old-pid", "0", "--target", str(target)])

    assert terminated == [blocker]
    assert target.read_text() == "NEW"


def test_retry_with_error_returns_last_oserror():
    attempts = []

    def fail():
        attempts.append(1)
        raise OSError(5, "locked")

    ok, err = update._retry_with_error(fail, attempts=2, delay=0)

    assert ok is False
    assert isinstance(err, OSError)
    assert len(attempts) == 2


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
    status = c.get("/api/update/status").json()
    assert status["applying"] is False and status["phase"] == "idle"
    assert c.post("/api/update/apply").status_code == 400
    assert c.post("/api/update/cancel").status_code == 400


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
    monkeypatch.setattr(update, "_fetch_update_changes", lambda current, latest, timeout: [])
    up = update.Updater()
    # Don't actually download/relaunch when apply fires.
    monkeypatch.setattr(up, "_apply_worker", lambda info: None)

    c = TestClient(create_app(load_config(), updater=up))
    chk = c.get("/api/update/check").json()
    assert chk["available"] is True and chk["latest"] == "1.0.2"
    res = c.post("/api/update/apply").json()
    assert res["ok"] is True and res["started"] is True
    status = c.get("/api/update/status").json()
    assert status["applying"] is True and status["phase"] == "starting"
    cancel = c.post("/api/update/cancel").json()
    assert cancel["ok"] is True and cancel["cancelled"] is True


def test_check_truncates_notes(monkeypatch):
    body = "x" * 5000
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "1.0.1")
    # _fetch_latest itself truncates to 4000; simulate that contract holds end-to-end.
    monkeypatch.setattr(update, "_fetch_latest",
                        lambda timeout=6.0: {"version": "1.0.2", "url": "u", "size": 1,
                                             "name": "n", "notes": body[:4000]})
    monkeypatch.setattr(update, "_fetch_update_changes", lambda current, latest, timeout: [])
    assert len(update.Updater().check()["notes"]) == 4000


def test_version_history_range_selects_versions_between_current_and_latest():
    history = """
## v1.3.4

English:

- Too new.

中文：

- 太新。

## v1.3.3

English:

- Third update.

中文：

- 第三次更新。

## v1.3.2

English:

- Second update.

中文：

- 第二次更新。

## v1.3.1

English:

- First update.

中文：

- 第一次更新。

## v1.3.0

English:

- Current update.
"""

    selected = update._history_for_range(
        update._parse_version_history(history),
        current="1.3.0",
        latest="1.3.3",
    )

    assert [item["version"] for item in selected] == ["v1.3.1", "v1.3.2", "v1.3.3"]
    assert selected[0]["en"] == ["First update."]
    assert selected[0]["zh"] == ["第一次更新。"]


def test_check_uses_version_history_range_instead_of_automated_release_body(monkeypatch):
    history = """
## v1.3.3

English:

- Hide cmd flashes.

中文：

- 隐藏 cmd 闪窗。

## v1.3.2

English:

- Show workspace git status.

中文：

- 显示工作区 git 状态。

## v1.3.1

English:

- Preserve PM reasoning spaces.

中文：

- 保留 PM 思考空格。

## v1.3.0

English:

- Existing updater modal.
"""
    monkeypatch.setattr(update, "is_frozen", lambda: True)
    monkeypatch.setattr(update, "current_version", lambda: "1.3.0")
    monkeypatch.setattr(update, "_fetch_latest",
                        lambda timeout=6.0: {"version": "1.3.3", "url": "u", "size": 1,
                                             "name": "n", "notes": "Automated build body"})
    monkeypatch.setattr(update, "_fetch_version_history", lambda timeout=6.0: history)

    out = update.Updater().check()

    assert out["available"] is True
    assert [item["version"] for item in out["changes"]] == ["v1.3.1", "v1.3.2", "v1.3.3"]
    assert "v1.3.1" in out["notes"] and "v1.3.3" in out["notes"]
    assert "v1.3.0" not in out["notes"] and "Automated build body" not in out["notes"]
