# PM Browser Runtime Experiment

This directory is an isolated experiment for a Foreman PM-agent browser runtime. It is not production Foreman code.

## Files

- `pm_browser_runtime_experiment.py`: minimal built-in Playwright runtime.
- `test_pm_browser_runtime_experiment.py`: deterministic local-page tests.
- `provider_browser_llm_experiment.py`: GPT-5.5 browser tool-loop experiment.
- `results/`: generated markdown/JSON reports and screenshot artifacts.

## Run

```powershell
$env:PYTHONPATH='E:\AutoWorkAgent-pm-tool-runtime-research\experiments\pm_browser_runtime'
python -m pytest experiments\pm_browser_runtime\test_pm_browser_runtime_experiment.py -q
```

```powershell
$env:PYTHONPATH='E:\AutoWorkAgent-pm-tool-runtime-research\src;E:\AutoWorkAgent-pm-tool-runtime-research\experiments\pm_browser_runtime'
python experiments\pm_browser_runtime\provider_browser_llm_experiment.py
```

## Scope

The runtime supports:

- `browser_open`
- `browser_snapshot`
- `browser_click`
- `browser_type`
- `browser_extract_text`
- `browser_screenshot`
- `browser_close`

The experiment uses an isolated Playwright Chromium context, an explicit localhost origin allowlist, and screenshot artifacts saved under `results/artifacts`.
