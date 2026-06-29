"""Token-aware context budgeting (work-mode integration P1b-context, DESIGN §8B).

Unifies the work-mode L0/L1 layers with Foreman's existing ContextPack compaction into ONE budgeted,
token-aware policy instead of three hard-coded 12000-char constants. The PM model window drives each
lane's char budget; when the provider's /models omits context_length (many OpenAI-compatible proxies
do) the budgeter falls back to a conservative default window (better to over-compact than overflow).

This is a leaf helper: no LLMClient/Config import at module load (``resolve_window_tokens`` takes a
duck-typed client). Char↔token is a ~4 char/token approximation (swap one constant for a real
tokenizer later).
"""

from __future__ import annotations

# ── budget constants (single source; mirrored in 90-conventions-and-glossary.md) ──
CHARS_PER_TOKEN = 4
DEFAULT_CTX_WINDOW_TOKENS = 272_000  # [default 2026-06-29] GPT-compatible fallback window
OUTPUT_RESERVE_TOKENS = 4_000        # reserve for the response (mirrors app.js outputReserve)
AUTO_COMPACT_THRESHOLD = 0.70        # [default 2026-06-24] compact when (lane5+6+7) ≥ this × window
AUTO_COMPACT_EVERY_N_RUNS = 8        # [default 2026-06-24] also compact every N review runs

# §8B.3 lane → window ratio (lane 7 = remainder, compacted first; lanes 1-4 are not pack-managed).
LANE_BUDGET_RATIO = {
    "session_memory": 0.25,   # lane 5 (ContextPack)
    "l1_bodies": 0.15,        # lane 6 (pulled work-mode bodies)
}

# MemoryItem.scope is a free-form str with no enum/CHECK and get_memory_items does exact-string
# matching — a typo silently never matches. Centralize the legal values (§8B.5/§8B.6; P5 reuses).
MEMORY_SCOPE_SESSION = "session"
MEMORY_SCOPE_WORKSPACE = "workspace"
MEMORY_SCOPE_WORKFLOW = "workflow"
MEMORY_SCOPE_USER = "user"


def approx_tokens(text: str) -> int:
    """~4 chars/token approximation (P1b-context interim; real tokenizer swaps this one fn)."""
    n = len(text or "")
    return (n + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def char_budget(window_tokens: int, ratio: float) -> int:
    """Char budget for a lane: window_tokens × ratio, back to chars. Never negative."""
    return max(0, int(window_tokens * ratio) * CHARS_PER_TOKEN)


async def resolve_window_tokens(llm: object, model: str = "") -> int:
    """Effective context window (minus output reserve) for ``model``, from the provider's /models
    ``context_length``. Falls back to :data:`DEFAULT_CTX_WINDOW_TOKENS` when unavailable (no static
    per-model table exists, and many proxies omit context_length). Never raises — a broken /models
    call must not break a dispatch."""
    configured_window = _runtime_context_window_tokens(llm) or DEFAULT_CTX_WINDOW_TOKENS
    window = configured_window
    output_reserve = _runtime_max_tokens(llm) or OUTPUT_RESERVE_TOKENS
    fetch = getattr(llm, "list_model_infos", None)
    if fetch is not None:
        try:
            infos = await fetch()
        except Exception:  # noqa: BLE001 — network/parse failure → fall back, never break dispatch
            infos = None
        ctx = _context_length_for(infos, model)
        if ctx and ctx > 0:
            window = min(ctx, configured_window)
    output_reserve = max(0, min(output_reserve, window - 1_000))
    return max(1_000, window - output_reserve)


def _runtime_context_window_tokens(llm: object) -> int:
    read = getattr(llm, "runtime_context_window_tokens", None)
    if callable(read):
        try:
            return _positive_int(read())
        except Exception:  # noqa: BLE001 - bad resolver/config falls back
            return 0
    return _positive_int(getattr(llm, "context_window_tokens", 0))


def _runtime_max_tokens(llm: object) -> int:
    read = getattr(llm, "runtime_max_tokens", None)
    if callable(read):
        try:
            return _positive_int(read())
        except Exception:  # noqa: BLE001 - bad resolver/config falls back
            return 0
    return _positive_int(getattr(llm, "max_tokens", 0))


def _positive_int(value: object) -> int:
    try:
        n = int(value)  # type: ignore[arg-type,call-overload]
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _context_length_for(infos: object, model: str) -> int:
    """Pull context_length for ``model`` (or the first that has one) from a list_model_infos result."""
    if not isinstance(infos, list):
        return 0
    target = (model or "").strip().lower()
    first = 0
    for info in infos:
        if not isinstance(info, dict):
            continue
        cl = info.get("context_length") or info.get("context_window")
        try:
            cl = int(cl) if cl is not None else 0
        except (TypeError, ValueError):
            cl = 0
        if cl <= 0:
            continue
        name = str(info.get("id") or info.get("name") or "").strip().lower()
        if target and name == target:
            return cl
        if not first:
            first = cl
    # No target → use the first model that reports a window. A SPECIFIC target that matched nothing
    # falls back to 0 (caller → DEFAULT window) rather than borrowing an unrelated model's window.
    return 0 if target else first


def should_auto_compact(
    lane5_tokens: int,
    lane6_tokens: int,
    lane7_tokens: int,
    *,
    window_tokens: int,
    run_count: int,
    threshold: float = AUTO_COMPACT_THRESHOLD,
    every_n_runs: int = AUTO_COMPACT_EVERY_N_RUNS,
) -> bool:
    """True when (lane5+6+7) ≥ threshold×window OR run_count is a multiple of every_n_runs (§8B.8)."""
    used = lane5_tokens + lane6_tokens + lane7_tokens
    if window_tokens > 0 and used >= int(window_tokens * threshold):
        return True
    return every_n_runs > 0 and run_count > 0 and run_count % every_n_runs == 0


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_CTX_WINDOW_TOKENS",
    "OUTPUT_RESERVE_TOKENS",
    "AUTO_COMPACT_THRESHOLD",
    "AUTO_COMPACT_EVERY_N_RUNS",
    "LANE_BUDGET_RATIO",
    "MEMORY_SCOPE_SESSION",
    "MEMORY_SCOPE_WORKSPACE",
    "MEMORY_SCOPE_WORKFLOW",
    "MEMORY_SCOPE_USER",
    "approx_tokens",
    "char_budget",
    "resolve_window_tokens",
    "should_auto_compact",
]
