"""Language selection → output-language directive for LLM prompts (DESIGN §15).

The chosen UI language (zh/en) also drives what language the LLM replies in: append
`language_directive(lang)` to a Foreman LLM call's system prompt and its output comes back in
that language. The prompt skeletons (the user's secret sauce) stay language-neutral; only the
output language switches.
"""

from __future__ import annotations

SUPPORTED = ("zh", "en")
DEFAULT = "zh"

_DIRECTIVES = {
    "zh": "请始终用简体中文回答。",
    "en": "Always respond in English.",
}


def normalize(lang: str | None) -> str:
    """Coerce any input (e.g. 'English', 'zh-CN', None) to a supported code; fall back to DEFAULT."""
    code = (lang or "").strip().lower()
    if code.startswith("zh") or "中" in code or code in ("chinese", "cn"):
        return "zh"
    if code.startswith("en") or code == "english":
        return "en"
    return DEFAULT


def language_directive(lang: str | None) -> str:
    """One-line instruction to append to a system prompt so the LLM replies in `lang`."""
    return _DIRECTIVES[normalize(lang)]
