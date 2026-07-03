from __future__ import annotations

from pathlib import Path

from foreman.shared.config import Config, default_worktree_root, load_config, resolve_worktree_roots


def test_pm_worktree_config_defaults_are_explicit():
    cfg = Config()

    assert cfg.pm_tools.git_worktree is False
    assert cfg.pm_tools.worktree_roots == []
    assert cfg.pm_tools.worktree_branch_prefix == "foreman/"
    assert cfg.pm_tools.default_base_ref == "HEAD"


def test_pm_worktree_config_loads_yaml_values(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "worktrees"
    config_path.write_text(
        "\n".join(
            [
                "pm_tools:",
                "  git_worktree: true",
                f"  worktree_roots: ['{root.as_posix()}']",
                "  worktree_branch_prefix: 'codex/'",
                "  default_base_ref: 'main'",
            ]
        ),
        encoding="utf-8",
    )

    cfg = load_config(config_path)

    assert cfg.pm_tools.git_worktree is True
    assert cfg.pm_tools.worktree_roots == [root.as_posix()]
    assert cfg.pm_tools.worktree_branch_prefix == "codex/"
    assert cfg.pm_tools.default_base_ref == "main"


def test_empty_worktree_roots_derive_repo_external_sibling_root(tmp_path: Path):
    main = tmp_path / "repo"

    assert default_worktree_root(main) == tmp_path / ".foreman-worktrees" / "repo"
    assert resolve_worktree_roots(main, []) == [tmp_path / ".foreman-worktrees" / "repo"]
    assert resolve_worktree_roots(main, [str(tmp_path / "custom")]) == [tmp_path / "custom"]
