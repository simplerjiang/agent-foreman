# Claude Opus Protocol Probe

日期: 2026-06-23

模型:

- `claude.cmd -p --model opus --effort max --tools '' --no-session-persistence`

目的:

验证真实 LLM 是否理解 Foreman PM tool protocol:

1. 无工具结果时, 应输出 `tool_calls`, 不应编造最终计划。
2. 收到 runtime 生成的 `tool_results` 后, 应输出 `final_plan`。
3. 观察是否存在 scope drift。

## Round 1

Prompt 摘要:

```text
You are testing a PM tool-calling protocol.
Available tools: read_file, search_repo, web_search, run_command.
Goal: Before dispatching a coding agent, inspect docs/PM_TOOL_RUNTIME_RESEARCH_PLAN.md.
No tool results have been provided yet.
```

实际输出:

```json
{
  "type": "tool_calls",
  "tool_calls": [
    {
      "id": "call_read_plan_1",
      "name": "read_file",
      "arguments": {
        "path": "docs/PM_TOOL_RUNTIME_RESEARCH_PLAN.md",
        "start_line": 1,
        "end_line": 400
      }
    }
  ],
  "decision_notes": [
    "Goal explicitly requires inspecting docs/PM_TOOL_RUNTIME_RESEARCH_PLAN.md before dispatching a coding agent, and no tool results have been provided yet, so I currently have zero evidence about the plan's contents, scope, or stated next steps.",
    "Reading the file first (lines 1-400 to capture a typical full research plan) so I can ground the next-session decision in the plan's actual objectives, current status, and any 'next steps'/open-questions sections rather than guessing.",
    "Starting with a single focused read because the plan's contents will determine what else is worth examining (e.g., the new untracked experiments/ directory); I'll branch into follow-up reads/searches once I see what it references.",
    "Not emitting a final_plan yet and not fabricating plan contents — doing so would violate the protocol's evidence requirement."
  ]
}
```

Round 1 结果:

- Pass: 输出了 `tool_calls`。
- Pass: 没有伪造 `final_plan`。
- Pass: 选择了正确文件和合理 line range。

## Runtime Tool Result

命令:

```powershell
$env:PYTHONPATH='E:\AutoWorkAgent-pm-tool-runtime-research\experiments\pm_tool_runtime'
@'
import json
from pathlib import Path
from pm_tool_runtime_experiment import MiniPMToolRuntime, RuntimeConfig
runtime = MiniPMToolRuntime(RuntimeConfig(workspace=Path(r"E:\AutoWorkAgent-pm-tool-runtime-research"), max_output_chars=8000))
call = {"id":"call_read_plan_1","name":"read_file","arguments":{"path":"docs/PM_TOOL_RUNTIME_RESEARCH_PLAN.md","start_line":1,"end_line":400}}
result = runtime.call(call)
print(json.dumps({"tool_results":[result.__dict__]}, ensure_ascii=False))
'@ | python -
```

结果摘要:

```json
{
  "tool_results": [
    {
      "id": "call_read_plan_1",
      "name": "read_file",
      "ok": true,
      "data": {
        "path": "docs/PM_TOOL_RUNTIME_RESEARCH_PLAN.md",
        "text": "# PM Tool Runtime Research Plan..."
      },
      "risk": "safe",
      "taint": [],
      "truncated": true
    }
  ]
}
```

## Round 2

Prompt 摘要:

```text
Prior assistant response: read_file call.
Tool result from runtime: read_file result for docs/PM_TOOL_RUNTIME_RESEARCH_PLAN.md.
Now return a final_plan for the next implementation session, grounded only in tool result.
```

实际输出摘要:

```json
{
  "type": "final_plan",
  "ready": true,
  "summary": "Phase 1 of the PM tool runtime per docs/PM_TOOL_RUNTIME_RESEARCH_PLAN.md: scaffold the src/foreman/client/tools/ package...",
  "todo": [
    "Create the src/foreman/client/tools/ package...",
    "Define models.py with Pydantic ToolSpec...",
    "Implement policy.py safety defaults...",
    "Implement the three read-only tools in files.py...",
    "Implement runtime.py...",
    "Build the provider-neutral JSON PMToolLoop...",
    "Plumb context_taint and hard_policy..."
  ],
  "agent": "codex",
  "instruction": "Implement Phase 1 of the PM tool runtime exactly as scoped..."
}
```

Round 2 结果:

- Pass: 输出了 `final_plan`。
- Pass: final plan 基于 tool result 中的计划书内容。
- Pass: 保留了只读默认、path guard、context taint、hard policy 等关键项。
- Warning: final plan 建议“defer OpenAI/Anthropic provider-native tool calling to a later phase”, 与计划书中“生产实现 native 优先, JSON fallback 兼容”的修订结论不一致。

## 结论

真实 LLM 能理解基础协议:

- 能在缺少证据时请求 tool call。
- 能在收到 tool result 后输出 final plan。
- 能遵守“不要伪造工具结果”的指令。

但真实 LLM 仍可能发生 scope drift:

- 它把 provider-native tool calling 延后, 说明 PMToolLoop 不能只信任模型 final plan。
- 生产实现需要 final-plan validator, 至少校验 P0/P1 安全要求没有被降级或跳过。
