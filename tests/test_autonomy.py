"""Tests for the Autonomy Dial (TASKS T4.4 / DESIGN §6.4 / §6.6).

The dial decides, per proposed action, whether Foreman runs it (auto), asks via a card (card),
or only reports (report) — based on the Gate classification and the level (0..3, default 1).
Red line: an irreversible action is never `auto` at any level.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.client.core.gate import Gate
from foreman.client.store import Store
from foreman.server.app import create_app
from foreman.shared.autonomy import (
    AUTO,
    CARD,
    DEFAULT_LEVEL,
    REPORT,
    decide_disposition,
    level_label,
    normalize_level,
)
from foreman.shared.config import GatesCfg, load_config
from foreman.shared.events import EventBus


# ── normalize_level ──────────────────────────────────────────────────────────────────────────
def test_normalize_level_defaults_and_clamps():
    assert normalize_level(None) == DEFAULT_LEVEL == 1
    assert normalize_level("garbage") == 1
    assert normalize_level(-5) == 0
    assert normalize_level(99) == 3
    assert normalize_level("2") == 2  # string coercion (config_kv stores strings)
    assert normalize_level(3) == 3
    assert normalize_level(" 0 ") == 0


# ── decide_disposition: the full level × class matrix ────────────────────────────────────────
def test_level0_reports_everything():
    for cls in ("safe", "needs-strategy", "requires-approval"):
        assert decide_disposition(cls, 0) == REPORT


def test_level1_cards_everything():
    for cls in ("safe", "needs-strategy", "requires-approval"):
        assert decide_disposition(cls, 1) == CARD


def test_level2_auto_safe_card_rest():
    assert decide_disposition("safe", 2) == AUTO
    assert decide_disposition("needs-strategy", 2) == CARD
    assert decide_disposition("requires-approval", 2) == CARD


def test_level3_auto_reversible_card_irreversible():
    assert decide_disposition("safe", 3) == AUTO
    assert decide_disposition("needs-strategy", 3) == AUTO
    assert decide_disposition("requires-approval", 3) == CARD  # red line, never auto


def test_irreversible_never_auto_at_any_level():
    for lvl in (1, 2, 3):
        assert decide_disposition("requires-approval", lvl) != AUTO


def test_unknown_class_fails_closed_to_card():
    # An unknown/garbled classification must be treated as irreversible, never auto.
    assert decide_disposition("???", 3) == CARD
    assert decide_disposition("", 2) == CARD
    assert decide_disposition("SAFE", 2) == AUTO  # case-insensitive


# ── level_label ──────────────────────────────────────────────────────────────────────────────
def test_level_label_bilingual_and_clamped():
    assert "档1" in level_label(1, "zh")
    assert "Level 1" in level_label(1, "en")
    assert level_label(1, "English").startswith("Level")
    assert level_label(99, "zh") == level_label(3, "zh")  # clamped
    assert "档0" in level_label(0)  # default lang = zh


# ── Gate.disposition: classify + dial in one call ────────────────────────────────────────────
def _gate() -> Gate:
    return Gate(GatesCfg(requires_approval=["git push", "rm -rf"], needs_strategy=["refactor"]))


def test_gate_disposition_combines_classify_and_dial():
    g = _gate()
    assert g.disposition("git push origin main", 3) == CARD  # irreversible → card even at 3
    assert g.disposition("cat file.txt", 3) == AUTO  # safe → auto at 3
    assert g.disposition("refactor the module", 2) == CARD  # needs-strategy → card at 2
    assert g.disposition("cat file.txt", 2) == AUTO
    assert g.disposition("git push", 1) == CARD
    assert g.disposition("cat file.txt", 0) == REPORT


# ── REST endpoints ───────────────────────────────────────────────────────────────────────────
def _app_with_store(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init()
    return create_app(load_config(), store, EventBus())


def test_api_autonomy_default_and_roundtrip(tmp_path):
    c = TestClient(_app_with_store(tmp_path))
    # default (unset config_kv) → config baseline 1
    body = c.get("/api/settings/autonomy").json()
    assert body["level"] == 1
    assert body["label"]
    # set to 2 → persisted + reflected
    r = c.post("/api/settings/autonomy", json={"level": 2})
    assert r.status_code == 200
    assert r.json()["level"] == 2
    assert c.get("/api/settings/autonomy").json()["level"] == 2


def test_api_autonomy_clamps_out_of_range(tmp_path):
    c = TestClient(_app_with_store(tmp_path))
    assert c.post("/api/settings/autonomy", json={"level": 99}).json()["level"] == 3
    assert c.post("/api/settings/autonomy", json={"level": -1}).json()["level"] == 0


def test_api_autonomy_label_follows_language(tmp_path):
    c = TestClient(_app_with_store(tmp_path))
    c.post("/api/settings/language", json={"language": "en"})
    assert "Level" in c.get("/api/settings/autonomy").json()["label"]
    c.post("/api/settings/language", json={"language": "zh"})
    assert "档" in c.get("/api/settings/autonomy").json()["label"]


def test_api_autonomy_503_without_store():
    c = TestClient(create_app(load_config()))  # store=None
    # GET still answers from the config baseline; POST needs a store to persist into.
    assert c.get("/api/settings/autonomy").json()["level"] == 1
    assert c.post("/api/settings/autonomy", json={"level": 2}).status_code == 503
