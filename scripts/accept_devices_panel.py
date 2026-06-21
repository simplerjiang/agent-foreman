"""Browser acceptance for the "我的设备 / My machines" panel (T7.4, DESIGN §8.4 multi-tenant
isolation: 用户只看自己的).

Seeds a team-mode app with TWO accounts — alice (two machines: one online, one offline) and
bob (one machine) — runs uvicorn in a thread, then drives keys.html with headless chromium:
log in as alice → the devices panel lists ONLY alice's two machines (never bob's), with
online/offline badges. Captures a screenshot as evidence. Not part of the pytest suite (manual
acceptance per the autodev loop).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from foreman.server.app import create_app
from foreman.server.auth_manager import AuthManager
from foreman.server.store import ServerStore
from foreman.server.store.models import ProcessRegistry
from foreman.shared.config import load_config

PORT = 8831
SHOT_DIR = Path(__file__).resolve().parent.parent / "artifacts"
SHOT_DIR.mkdir(exist_ok=True)


def build_app():
    db = SHOT_DIR / "accept_devices.db"
    if db.exists():
        db.unlink()
    store = ServerStore(str(db))
    store.init()
    auth = AuthManager(store)
    alice = auth.create_account("alice", "password1", display_name="Alice")["account_id"]
    bob = auth.create_account("bob", "password1", display_name="Bob")["account_id"]
    # alice has two machines (one online, one offline); bob has one — alice must never see bob's.
    store.register_process(ProcessRegistry(
        id="ka1", account_id=alice, access_key_id="ka1", name="我的台式机", online=True))
    store.register_process(ProcessRegistry(
        id="ka2", account_id=alice, access_key_id="ka2", name="笔记本", online=False))
    store.register_process(ProcessRegistry(
        id="kb1", account_id=bob, access_key_id="kb1", name="bob-secret-box", online=True))
    return create_app(load_config(), auth=auth)


def main() -> None:
    app = build_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    base = f"http://127.0.0.1:{PORT}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{base}/keys.html")

            page.fill("#login-user", "alice")
            page.fill("#login-pass", "password1")
            page.click("#login-form button[type=submit]")
            page.wait_for_selector("#keys-pane:not([hidden])")

            # the devices panel lists exactly alice's two machines
            page.wait_for_function("document.querySelectorAll('#proc-list .card').length === 2")
            procs = page.text_content("#proc-list")
            assert "我的台式机" in procs and "笔记本" in procs, procs
            assert "bob-secret-box" not in procs, f"LEAK — saw bob's machine: {procs!r}"
            # online/offline badges both render
            badges = page.eval_on_selector_all(
                "#proc-list .pill", "els => els.map(e => e.textContent.trim())"
            )
            assert any("在线" in b or "online" in b for b in badges), badges
            assert any("离线" in b or "offline" in b for b in badges), badges
            page.screenshot(path=str(SHOT_DIR / "devices_1_alice.png"))

            browser.close()
        print("ACCEPT OK — alice sees only her 2 machines (台式机 online / 笔记本 offline), "
              "never bob's; screenshot in artifacts/")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
