"""Non-browser checks for the timeline page (TASKS T1.11).

Confirms the static page ships and wires the API + WS (browser acceptance is done separately
with a real browser per the autodev loop).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from foreman.server.app import create_app
from foreman.shared.config import load_config


def test_index_served():
    c = TestClient(create_app(load_config()))
    r = c.get("/")
    assert r.status_code == 200
    assert "Foreman" in r.text


def test_app_js_wires_api_and_ws():
    c = TestClient(create_app(load_config()))
    js = c.get("/app.js")
    assert js.status_code == 200
    assert "/api/sessions" in js.text
    assert "/ws?session_id=" in js.text
    # agent output must be rendered safely (no innerHTML of event payloads)
    assert "textContent" in js.text


def test_i18n_wired_in_page():
    c = TestClient(create_app(load_config()))
    html = c.get("/").text
    assert "lang-toggle" in html and "data-i18n" in html
    js = c.get("/app.js").text
    assert "I18N" in js and "/api/settings/language" in js


def test_decision_card_and_detail_wired(tmp_path):
    """The PWA fetches cards + drills into step detail, and renders the diff safely (T4.3 §6.3)."""
    c = TestClient(create_app(load_config()))
    html = c.get("/").text
    assert "card-template" in html and "view-detail" in html and 'data-tab="diff"' in html
    js = c.get("/app.js").text
    assert "/api/cards" in js and "/api/actions/" in js and "/detail" in js
    assert "chooseCard" in js and "/choose" in js  # one-tap card decision wired
    # diff + raw output are untrusted → rendered via textContent, never assigned to innerHTML.
    assert "renderDiff" in js and "textContent" in js and ".innerHTML" not in js
