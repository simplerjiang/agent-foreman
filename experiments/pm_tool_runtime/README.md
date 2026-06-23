# PM Tool Runtime Experiment

日期: 2026-06-23

这个目录是隔离实验, 不是生产 Foreman runtime。

实验目标:

- 验证最小 tool runtime 模型是否能表达 PM 派发前调查。
- 验证 path guard、web taint 后 shell 降级、`replace_in_file` 唯一匹配语义。
- 用 fake LLM 验证 JSON tool loop。
- 用真实 Claude Opus 验证 LLM 是否能理解 `tool_calls -> tool_results -> final_plan` 协议。

运行:

```powershell
$env:PYTHONPATH='E:\AutoWorkAgent-pm-tool-runtime-research\experiments\pm_tool_runtime'
python -m pytest experiments\pm_tool_runtime\test_pm_tool_runtime_experiment.py -q
python -m ruff check experiments\pm_tool_runtime
python -m py_compile experiments\pm_tool_runtime\pm_tool_runtime_experiment.py experiments\pm_tool_runtime\test_pm_tool_runtime_experiment.py
```

当前结果:

- pytest: `4 passed in 0.04s`
- ruff: `All checks passed!`
- py_compile: passed

更新后的全工具确定性结果:

- pytest: `6 passed in 0.59s`
- ruff: `All checks passed!`
- py_compile: passed

GPT-5.5 provider 实验:

```powershell
$env:PYTHONPATH='E:\AutoWorkAgent-pm-tool-runtime-research\src;E:\AutoWorkAgent-pm-tool-runtime-research\experiments\pm_tool_runtime'
python experiments\pm_tool_runtime\provider_llm_tool_experiment.py
```

输出报告:

- `experiments/pm_tool_runtime/results/2026-06-23-gpt-5.5-all-tools-provider-experiment.md`
- `experiments/pm_tool_runtime/results/2026-06-23-gpt-5.5-all-tools-provider-experiment.json`

GPT-5.5 provider 实验结果:

| Tool | LLM emitted expected call | Runtime ok | Result summary |
|---|---:|---:|---|
| `list_files` | yes | yes | 3 files |
| `read_file` | yes | yes | 10 chars |
| `search_repo` | yes | yes | 2 matches |
| `write_file` | yes | yes | 35 bytes written |
| `replace_in_file` | yes | yes | match_count=1 |
| `run_command` | yes | yes | exit 0 |
| `fetch_url` | yes | yes | 39 chars fetched |
| `web_search` | yes | yes | 1 synthetic result |

关键结论:

- Fake LLM loop 可以稳定完成 `read_file -> tool_results -> final_plan`。
- Path escape 被拒绝。
- `web_search` 返回 `external_web_content` taint 后, 非 allowlist shell 会被降级为 `requires-approval`。
- `replace_in_file` 对多重匹配返回失败和 `match_count`。
- 真实 Claude Opus 能按协议先请求 `read_file`, 再基于 tool result 输出 `final_plan`。
- 真实 Claude Opus 的 final plan 有一个 scope drift: 它建议把 provider-native tool calling 延后, 与计划书的生产优先级不一致。生产 PM loop 需要 final-plan validator 或 hard policy 检查。
