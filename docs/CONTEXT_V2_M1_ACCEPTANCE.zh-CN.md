Result: PASS WITH NON-BLOCKING NOTES.
PM Tool Surface is explicitly out of scope for M1 and tracked as M2.

# Context V2 M1 Acceptance Report

## Summary

- Branch: `codex/context-v2-20260701-151241`
- Commit HEAD: `e6512ab`
- Result: PASS WITH NON-BLOCKING NOTES
- Date/time: `2026-07-03 09:25:53 +08:00`
- Tester: Codex

## Scope

In scope:
- ContextFrame / ContextCheckpoint / replacement history restore
- deterministic materializer / cursor / compact thresholds
- PM active context envelope and PM plan/review main path
- subagent runtime state
- Context API/UI and no-build JS split
- legacy/corruption fallback

Out of scope:
- PM Tool Surface M2
- browser wait/assert tools
- additional UI feature work
- further JS splitting
- DB schema/migrations beyond already implemented M1

## Automated Tests

| Command | Result | Notes |
|---|---|---|
| pytest targeted suite | PASS | `291 passed, 3 warnings` |
| py_compile | PASS | All requested Python files compiled |
| node --check | PASS | `app-core.js`, `app-context.js`, `app-timeline.js`, `app.js` |
| git diff --check | PASS | No whitespace errors |
| realistic session single test | PASS | `5 passed` |

Warnings were external FastAPI/websockets deprecation warnings only.

## Architecture Checks

| Check | Result | Evidence |
|---|---|---|
| ContextFrame deterministic | PASS | `tests/test_context_v2_frames.py::test_make_frame_id_is_deterministic_for_same_payload_order` |
| ContextCheckpoint restore | PASS | `tests/test_context_v2_active_context.py::test_build_active_context_with_valid_checkpoint_uses_replacement_history` |
| Session.plan not restore source | PASS | `restore_from_latest_checkpoint()` marks legacy fallback as `legacy_summary`; covered by `test_legacy_session_plan_is_summary_not_replacement_history` |
| events_to_text fallback only | PASS | Context v2 compact covered by `test_compact_now_does_not_call_events_to_text`; remaining uses are legacy/fallback/recovery paths |
| no background compact | PASS | `create_task` hits are dispatch/agent background paths, not Context v2 compact writes |

## Compact / Restore

| Check | Result | Evidence |
|---|---|---|
| replacement_history required | PASS | `test_empty_replacement_history_prevents_checkpoint_install`, `_validate_replacement_history_items()` |
| remote/local compact | PASS | `test_remote_compact_mock_200_writes_context_checkpoint`, `test_remote_compact_unsupported_falls_back_local`, `tests/test_remote_compact_adapter.py` |
| failure atomicity | PASS | `test_compact_failure_is_atomic`, manual compact API failure tests |
| cursor avoids duplicate review | PASS | `test_compact_source_cursor_end_filters_next_review`, `test_pm_review_checkpoint_cursor_does_not_duplicate_covered_frames` |
| threshold behavior | PASS | `tests/test_context_v2_budget.py`; soft `70%`, hard `90%`, run-count `8`; no xfail/skip found |

## PM Plan / Review

| Check | Result | Evidence |
|---|---|---|
| plan uses active context | PASS | `_pm_context_text(... purpose="pm_plan")`; `test_pm_plan_invokes_maybe_compact_and_uses_rebuilt_context` |
| review uses active context | PASS | `_pm_context_text(... purpose="pm_review")`; `test_pm_review_uses_active_context_as_context_not_timeline` |
| review timeline incremental | PASS | `_review_timeline_from_active_context()`; tests for no duplicate, no lane 7 noise, no output contract in timeline |
| direct answer no dispatch | PASS | `test_direct_answer_active_context_does_not_dispatch_agent` |
| validator error recovery | PASS | `pm_validation_error` persists and materializes as `previous_validation_error`; `test_previous_validation_error_is_visible_in_next_pm_plan_context` |

## Subagent Runtime

| Check | Result | Evidence |
|---|---|---|
| cwd/worktree/branch captured | PASS | `_subprocess.py`, `copilot_cli.py`, `detect_git_refs()`; `test_multi_agent_multi_worktree_runtime_state_active_agents` |
| terminal status precedence | PASS | `_terminal_status_rank()`, `_choose_agent_status()` |
| failed not overwritten | PASS | `test_failed_stop_not_overwritten_by_completed_runner_handle`, `test_failed_task_not_overwritten_by_completed_runner_handle` |
| compact restore preserves agents | PASS | `test_compact_restore_preserves_agent_worktree_status_and_native_session` |

## Context UI/API

| Check | Result | Evidence |
|---|---|---|
| context overview API | PASS | `GET /api/sessions/{session_id}/context`; `test_get_context_returns_usage_runtime_and_latest_checkpoint` |
| checkpoint list/detail API | PASS | list/detail routes; `test_checkpoint_detail_excludes_provider_payload_and_encrypted_content` |
| manual compact API | PASS | `POST /context/compact`; success/failure API tests |
| Context panel | PASS | `app-context.js`; UI selector/render tests |
| redaction | PASS | API and UI tests cover `provider_payload`, `encrypted_content`, hidden reasoning |
| scroll behavior | PASS | `test_new_message_scrolls_conversation_to_bottom`, `test_context_panel_refresh_does_not_jump_conversation_to_top` |

Missing selectors: none found. `data-testid="context-tab"` exists in `src/foreman/server/web/app.js`.

## Realistic Session Smoke

- Result: SKIPPED for live/local DB manual smoke; PASS for automated realistic session tests
- Session id: N/A
- Notes: No `foreman.db*` exists in the target worktree, so I did not fabricate a live session smoke. `tests/test_context_v2_realistic_session.py` passed separately.
- Missing evidence, if any: Optional live UI/API smoke against a real persisted user session.

## Blockers

No M1 blockers found.

## Non-blocking Follow-ups

Expected follow-ups:
- PM Tool Surface M2
- Optional browser E2E on packaged/local app
- Optional further JS splitting
- Optional live DB/session smoke once a real target session is available

## Final Decision

PASS WITH NON-BLOCKING NOTES: accepted for M1; follow-ups listed.
