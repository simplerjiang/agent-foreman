"""Seed foreman.db with a demo session + events so `foreman app` shows a populated timeline.

No agents, no tokens — just sample rows. Usage (repo root, package installed):
    python scripts/seed_demo.py
    foreman app
"""

from foreman.client.store import Store
from foreman.client.store.models import Session, Task
from foreman.shared.events import make_event


def main() -> None:
    store = Store("foreman.db")
    store.init()
    store.add_session(Session(id="demo1", goal="refactor auth module", status="running",
                              agent_type="claude-code", workspace="."))
    store.add_task(Task(id="demo-t1", session_id="demo1", instruction="refactor auth module"))
    for ev in [
        make_event("agent_output", "claude-code", "demo1", payload={"text": "reading auth.py"}),
        make_event("tool_pre", "claude-code", "demo1", payload={"tool": "Edit", "file": "auth.py"}),
        make_event("tool_post", "claude-code", "demo1", payload={"file": "auth.py", "ok": True}),
        make_event("stop", "claude-code", "demo1", payload={"result": "done"}),
    ]:
        store.add_event(ev)
    print("seeded foreman.db with session 'demo1' (4 events). Now run:  foreman app")


if __name__ == "__main__":
    main()
