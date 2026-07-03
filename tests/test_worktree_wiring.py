"""Tests for standalone-mode WorktreeManager wiring (main.py).

``ccdb start`` (main.py) must honor WORKTREE_BASE_DIR the same way
``setup.setup_bridge()`` does: when the env var is set, a WorktreeManager
is created so session worktrees are cleaned up at session end and startup.
"""

from __future__ import annotations

from claude_discord.main import create_worktree_manager, load_config
from claude_discord.worktree import WorktreeManager


def _set_required_env(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "123")


def test_load_config_includes_worktree_base_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # avoid loading the repo's real .env
    _set_required_env(monkeypatch)
    monkeypatch.setenv("WORKTREE_BASE_DIR", "/home/user")

    config = load_config()

    assert config["worktree_base_dir"] == "/home/user"


def test_load_config_worktree_base_dir_defaults_to_empty(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _set_required_env(monkeypatch)
    monkeypatch.delenv("WORKTREE_BASE_DIR", raising=False)

    config = load_config()

    assert config["worktree_base_dir"] == ""


def test_create_worktree_manager_enabled_when_base_dir_set():
    manager = create_worktree_manager({"worktree_base_dir": "/home/user"})

    assert isinstance(manager, WorktreeManager)
    assert manager._base_dir == "/home/user"


def test_create_worktree_manager_disabled_when_unset():
    assert create_worktree_manager({"worktree_base_dir": ""}) is None
    assert create_worktree_manager({}) is None
