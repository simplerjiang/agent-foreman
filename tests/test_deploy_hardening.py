from __future__ import annotations

import runpy
from pathlib import Path

import yaml


def _load_hardener():
    script = Path(__file__).resolve().parents[1] / "deploy" / "harden_config.py"
    return runpy.run_path(str(script))["harden_config"]


def test_harden_config_enables_public_https_redirect_and_sanitized_health(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
server:
  host: 0.0.0.0
  port: 8787
  health_show_db: true
store:
  db_path: /opt/foreman/app/foreman.db
""".lstrip(),
        encoding="utf-8",
    )

    changed = _load_hardener()(cfg)
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))

    assert changed is True
    assert data["server"]["force_https"] is True
    assert data["server"]["trust_proxy_headers"] is True
    assert data["server"]["health_show_db"] is False
    assert data["store"]["db_path"] == "/opt/foreman/app/foreman.db"


def test_harden_config_is_idempotent(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
server:
  force_https: true
  trust_proxy_headers: true
  health_show_db: false
""".lstrip(),
        encoding="utf-8",
    )

    harden = _load_hardener()
    assert harden(cfg) is False
    assert harden(cfg) is False
