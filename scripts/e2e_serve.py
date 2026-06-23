"""E2E harness: start a real local Foreman app with seeded data, for browser acceptance.

Not shipped — a dev/CI helper. Builds a temp store, seeds a realistic session (a full event
sequence: dispatch → pm_plan → pm_output → agent_output + tool_pre + git_diff → stop), an open
decision card, a pending approval, and the built-in example definitions, then serves the local
app on 127.0.0.1:8799 so the redesigned PWA can be driven by a browser.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

# Prefer this worktree's src over any editable install so we serve the code under test.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from foreman.client.core.cards import CardService
from foreman.client.core.examples import seed_examples
from foreman.client.core.gate import Gate
from foreman.client.local_app import start_local_app
from foreman.client.store import Store
from foreman.client.store.models import Session
from foreman.shared.config import WorkspaceCfg, load_config
from foreman.shared.events import AgentEvent, utc_now_iso

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799


def seed(store: Store) -> None:
    repo = str(Path(__file__).resolve().parents[1])
    sid = "sess-auth-refactor"
    now = utc_now_iso()
    store.add_session(Session(
        id=sid, goal="重构 auth 模块，push 前问我", plan="", status="running",
        workspace=repo, agent_type="claude-code", created_at=now, updated_at=now,
    ))
    evs = [
        AgentEvent("dispatch", "desktop", sid, payload={"goal": "把 auth 模块拆出独立的 token 校验层，补单测，push 前问我。", "agent": "claude-code", "model": "sonnet", "effort": "medium"}),
        AgentEvent("pm_plan", "pm", sid, payload={"summary": "拆出 TokenVerifier，复用 shared/crypto，补单测，push 前停下。", "todo": ["抽取 TokenVerifier 到独立模块", "复用 shared/crypto 改写校验", "补 test_auth.py 单测", "本地跑测试，push 前停下问你"], "deliberation": ["复用现有 crypto，避免重复实现", "保留旧路径一个 release 周期"]}),
        AgentEvent("pm_output", "pm", sid, payload={"text": "计划已确认。我把每一步交给子代理执行 —— 展开下面的面板能看到回复、命令和具体改动。"}),
        AgentEvent("agent_output", "claude-code", sid, task_id="t1", payload={"text": "先定位所有 verify_token 调用点，把校验逻辑抽到独立的 TokenVerifier，改用 shared/crypto 的常量时间比较。"}),
        AgentEvent("tool_pre", "claude-code", sid, task_id="t1", payload={"command": "grep -n \"verify_token\" src/foreman/server/auth.py"}),
        AgentEvent("tool_pre", "claude-code", sid, task_id="t1", payload={"command": "pytest -k token -q"}),
        AgentEvent("git_diff", "git", sid, task_id="t1", payload={"path": "src/foreman/server/token_verifier.py", "additions": 64, "deletions": 12, "stat": "+64 −12", "lines": [{"kind": "context", "text": "class TokenVerifier:"}, {"kind": "add", "text": "    def verify(self, raw): ..."}, {"kind": "del", "text": "    if sig == expected:  # timing-unsafe"}]}),
        AgentEvent("agent_output", "codex", sid, task_id="t2", payload={"text": "复查新校验路径上的所有比较操作，确认全部经过 constant_time_eq，无时序泄露。"}),
        AgentEvent("tool_pre", "codex", sid, task_id="t2", payload={"command": "rg \"==|!=\" src/foreman/server/token_verifier.py"}),
        AgentEvent("review", "codex", sid, task_id="t2", payload={"summary": "无时序泄露，可继续。"}),
        AgentEvent("checkpoint", "supervisor", sid, payload={"summary": "step 2 snapshot"}),
        AgentEvent("stop", "claude-code", sid, payload={"status": "idle"}),
    ]
    for e in evs:
        store.add_event(e)

    cards = CardService(store)
    cards.build_card(
        action_id="act-1", session_id=sid,
        summary="旧 `HS256` 校验路径要删掉吗？",
        audit_note="新 TokenVerifier 已覆盖全部调用点。删除可减少 ~40 行遗留代码，但外部脚本若仍签发旧 token 会失效。建议保留一个 release 周期。",
        diff_stat="2 个文件 +64 / −12",
        options=[{"action": "approve", "label": "保留一个周期"}, {"action": "manual", "label": "现在删除"}, {"action": "revise", "label": "让我看 diff"}],
    )
    gate = Gate(load_config().gates, store=store)
    asyncio.run(gate.request_approval(sid, "git push origin auth-refactor", tool="shell", risk_level="high", diff_summary="git push origin auth-refactor"))

    res = seed_examples(store)
    print("seeded examples:", res)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="foreman-e2e-"))
    cfg = load_config(tmp / "config.yaml")
    cfg.store.db_path = str(tmp / "foreman.db")
    cfg.env_path = str(tmp / ".env")
    cfg.workspaces = [WorkspaceCfg(path=str(Path(__file__).resolve().parents[1]), name="Foreman")]

    store = Store(cfg.store.db_path)
    store.init()
    seed(store)

    app = start_local_app(cfg, host="127.0.0.1", port=PORT)
    print(f"E2E server up at {app.url}  (db={cfg.store.db_path})", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    main()
