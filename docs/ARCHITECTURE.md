# Architecture (component contracts)

This complements [DESIGN.zh-CN.md](DESIGN.zh-CN.md) with the concrete contracts between components.

## Event flow

```
 Agent CLI ──stdout/stream-json──┐
 Claude hooks ──HTTP POST────────┤
 Git watcher ────────────────────┤──▶  Event Bus  ──▶  Store (events table)
 Process watcher ────────────────┘         │
                                           ├──▶  PM Brain    (assess state, decide next)
                                           ├──▶  Reviewer    (on checkpoint → LLM verdict)
                                           ├──▶  Gate        (dangerous action → approval)
                                           └──▶  WebSocket   (live push to open PWA)
```

Every event is **persisted first, then dispatched** — so the phone timeline is a replay of the DB,
and a reconnecting PWA can backfill from `events`.

## Core types (Python, indicative)

```python
@dataclass
class AgentEvent:
    type: str            # agent_output | tool_pre | tool_post | stop | error | ...
    source: str          # "claude-code" | "codex" | "hook" | "git" | "process"
    session_id: str
    task_id: str | None
    payload: dict
    ts: str              # UTC ISO8601

class AgentAdapter(Protocol):
    name: str
    async def start(
        self, instruction: str, workspace: Path, session_id: str, model: str = ""
    ) -> "AgentHandle": ...
    async def send(self, handle: "AgentHandle", text: str) -> None: ...
    async def stream(self, handle: "AgentHandle") -> AsyncIterator[AgentEvent]: ...
    async def interrupt(self, handle: "AgentHandle") -> None: ...
    async def stop(self, handle: "AgentHandle") -> None: ...

class Reviewer(Protocol):
    async def review(self, task: "Task", diff: str, context: str) -> "ReviewResult": ...

@dataclass
class ReviewResult:
    verdict: str         # approve | request_changes | escalate
    summary: str
    risks: list[str]
    suggestions: list[str]
    needs_human: bool

class Gate(Protocol):
    def classify(self, action: "Action") -> str:          # safe | needs-strategy | requires-approval
        ...
    async def request_approval(self, action: "Action") -> "Approval": ...
    async def resolve(self, approval_id: str, decision: str, reason: str | None) -> None: ...
```

## Process & threading model
- One asyncio event loop hosts: FastAPI app, event bus, scheduler, and per-agent stream readers.
- Each agent runs as a child process; its stdout is read by an async task that emits `AgentEvent`s.
- Blocking work (git, file IO) goes through `asyncio.to_thread` / `watchfiles` async API.

## API surface (FastAPI, P3+)
```
GET  /health
POST /api/tasks                  # dispatch a task; body may include agent/workspace/model
GET  /api/sessions               # list sessions
GET  /api/sessions/{id}/events   # timeline (paginated)
GET  /api/cards?status=pending   # decision cards awaiting a tap
POST /api/cards/{id}             # {chosen: approve|redirect|undo|custom, text?}
GET  /api/actions/{id}/detail    # drill-down: ① raw agent output + ② per-file/per-line diff
GET  /api/approvals?status=pending
POST /api/approvals/{id}         # {decision: approve|reject, reason?}
GET  /api/reports
POST /api/push/subscribe         # store WebPush subscription
WS   /ws                         # live event stream
POST /hooks                      # Claude Code hooks sink (localhost only)
```

## Hooks integration (Claude Code)
Hooks POST JSON to `http://127.0.0.1:<port>/hooks`. The receiver maps each hook event into an
`AgentEvent` and (for `PreToolUse` on dangerous tools) can return a blocking decision that routes
to the Gate. See [`hooks/claude-hooks.example.json`](../hooks/claude-hooks.example.json).
