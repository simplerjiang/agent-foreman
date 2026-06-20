"""Autonomy Dial (自治档位) — how proactive Foreman is (DESIGN §6.4 / §6.6).

The capabilities (the MCP "hands": shell / files / screenshot / mouse / keyboard, §4.7) are
ALWAYS full; the dial only changes whether *placing a move* needs to ask you first
("能力给满，落子由你松紧"). It maps a Gate classification (safe | needs-strategy |
requires-approval, §6.6) to a **disposition**:

    auto   — run on the pipeline without asking (reversible, trusted at this level)
    card   — surface a decision card and wait for your tap (§6.3)
    report — do nothing, just observe & report (you drive manually)

Levels (default 1):
    0 报告 only       — every class → report; Foreman never places a move on its own
    1 ask everything  — every class → card (⭐ default)
    2 auto reversible — safe → auto; needs-strategy & requires-approval → card
    3 bold autonomy   — safe & needs-strategy → auto; only irreversible → card

🔑 Red line (§6.6): an irreversible (requires-approval) action is **never** auto at **any**
level — undo can't save it, so it must be asked first. The dial only relaxes reversible work.
The *enforcement* (actually executing vs carding) is the client decision loop; this module is
the shared policy + labels so both the loop and the server settings endpoint agree on it.
"""

from __future__ import annotations

LEVELS = (0, 1, 2, 3)
DEFAULT_LEVEL = 1

# Dispositions.
AUTO = "auto"
CARD = "card"
REPORT = "report"

# Gate classifications this module reasons about (DESIGN §6.6). Anything else is treated as
# irreversible (fail-closed) — an unknown class must never slip into the `auto` lane.
_REVERSIBLE = ("safe", "needs-strategy")

_LABELS = {
    "zh": {
        0: "档0 只汇报：不自己落子，只观察和汇报。",
        1: "档1 凡事都问（默认）：每个动作都先弹决策卡等你点。",
        2: "档2 自动干可逆小事、大事弹卡：可逆动作自动执行，需策略/不可逆动作弹卡。",
        3: "档3 大胆自治，只拦不可逆：可逆与需策略动作自动执行，只有不可逆动作弹卡。",
    },
    "en": {
        0: "Level 0 report-only: never act autonomously; just observe and report.",
        1: "Level 1 ask everything (default): every action surfaces a decision card first.",
        2: "Level 2 auto reversible: safe actions run automatically; needs-strategy / "
        "irreversible actions pop a card.",
        3: "Level 3 bold autonomy: safe & needs-strategy run automatically; only irreversible "
        "actions pop a card.",
    },
}


def normalize_level(value) -> int:
    """Coerce any input (str / int / None / garbage) to a valid level: default 1, clamp 0..3."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    if n < LEVELS[0]:
        return LEVELS[0]
    if n > LEVELS[-1]:
        return LEVELS[-1]
    return n


def decide_disposition(classification: str, level) -> str:
    """Map (Gate classification, autonomy level) → ``auto`` | ``card`` | ``report``.

    Follows DESIGN §6.4/§6.6. Irreversible (requires-approval, or any unknown class) is never
    ``auto`` at any level ≥ 1 — the dial only relaxes reversible work. At level 0 nothing is
    placed autonomously, so everything is ``report``.
    """
    lvl = normalize_level(level)
    if lvl == 0:
        return REPORT
    cls = (classification or "").strip().lower()
    if cls not in _REVERSIBLE:
        return CARD  # red line: always ask before an irreversible (or unknown) move
    if lvl == 1:
        return CARD
    if lvl == 2:
        return AUTO if cls == "safe" else CARD
    return AUTO  # level 3: safe + needs-strategy auto (irreversible handled above)


def level_label(level, lang: str = "zh") -> str:
    """One-line human-readable description of a level, for the Auditor prompt / UI (§6.4)."""
    lvl = normalize_level(level)
    code = "en" if str(lang or "").strip().lower().startswith("en") else "zh"
    return _LABELS[code][lvl]
