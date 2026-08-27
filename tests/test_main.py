"""Tests for main.py: entry-point configuration and wiring (lane A fixes).

Covers:
- A3: CCDB_DATA_DIR overrides the default "data" directory.
- A5: CLAUDE_ALLOWED_TOOLS is parsed into ClaudeRunner.allowed_tools.
- A2: the direct main.py launch path wires resume_repo/settings_repo/
      usage_repo into ClaudeChatCog (previously silently omitted).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_discord.database.ask_repo import PendingAskRepository
from claude_discord.database.lounge_repo import LoungeRepository
from claude_discord.database.repository import SessionRepository
from claude_discord.database.resume_repo import PendingResumeRepository
from claude_discord.database.settings_repo import SettingsRepository
from claude_discord.database.usage_repo import UsageRepository
from claude_discord.main import _build_claude_chat_cog, _resolve_data_dir, create_runner


def _base_config(**overrides: str) -> dict[str, str]:
    """Minimal config dict matching load_config()'s shape for create_runner()."""
    defaults = {
        "runner_backend": "claude",
        "claude_command": "claude",
        "claude_model": "sonnet",
        "claude_permission_mode": "acceptEdits",
        "claude_working_dir": "",
        "timeout": "300",
        "append_system_prompt": "",
        "claude_allowed_tools": "",
    }
    defaults.update(overrides)
    return defaults


class TestResolveDataDir:
    """A3: CCDB_DATA_DIR lets bot instances keep separate session ledgers."""

    def test_default_is_data_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CCDB_DATA_DIR", raising=False)
        assert _resolve_data_dir() == Path("data")

    def test_env_override_is_used(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        custom = tmp_path / "bot2-data"
        monkeypatch.setenv("CCDB_DATA_DIR", str(custom))
        assert _resolve_data_dir() == custom

    def test_empty_env_falls_back_to_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty (but set) CCDB_DATA_DIR must not resolve to the CWD root."""
        monkeypatch.setenv("CCDB_DATA_DIR", "")
        assert _resolve_data_dir() == Path("data")


class TestCreateRunnerAllowedTools:
    """A5: CLAUDE_ALLOWED_TOOLS (via config['claude_allowed_tools']) -> ClaudeRunner."""

    def test_unset_yields_none_identical_to_before(self) -> None:
        config = _base_config(claude_allowed_tools="")
        runner = create_runner(config)
        assert runner.allowed_tools is None

    def test_parses_comma_separated_list(self) -> None:
        config = _base_config(claude_allowed_tools="Read,Grep,Bash")
        runner = create_runner(config)
        assert runner.allowed_tools == ["Read", "Grep", "Bash"]

    def test_strips_whitespace_and_drops_empties(self) -> None:
        config = _base_config(claude_allowed_tools=" Read , Grep,, Bash ")
        runner = create_runner(config)
        assert runner.allowed_tools == ["Read", "Grep", "Bash"]

    def test_other_runner_fields_unaffected(self) -> None:
        """Sanity check: adding allowed_tools must not disturb existing fields."""
        config = _base_config(
            claude_allowed_tools="Read",
            claude_command="claude",
            claude_model="opus",
            claude_permission_mode="plan",
            claude_working_dir="/tmp/work",
            timeout="42",
            append_system_prompt="be nice",
        )
        runner = create_runner(config)
        assert runner.command == "claude"
        assert runner.model == "opus"
        assert runner.permission_mode == "plan"
        assert runner.working_dir == "/tmp/work"
        assert runner.timeout_seconds == 42
        assert runner.append_system_prompt == "be nice"
        assert runner.allowed_tools == ["Read"]


class TestBuildClaudeChatCog:
    """A2: the direct main.py launch path must wire resume/settings/usage repos.

    Regression for: main.py's create_runner()/ClaudeChatCog(...) call site
    used to omit resume_repo/settings_repo/usage_repo entirely, so
    ClaudeChatCog fell back to ``getattr(bot, "resume_repo", None)`` etc.,
    which is also None on this launch path (only setup_bridge()'s consumers
    set those bot attributes) — silently disabling restart-resume, the
    dynamic /model override, and usage tracking with no error anywhere.
    """

    def _make_bot(self) -> MagicMock:
        bot = MagicMock()
        # ClaudeChatCog falls back to getattr(bot, ...) for these — make sure
        # the bot itself does NOT provide them, so the test only passes if
        # _build_claude_chat_cog's own explicit kwargs are what wins.
        del bot.resume_repo
        del bot.settings_repo
        del bot.usage_repo
        return bot

    def test_resume_settings_usage_repos_are_wired(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "sessions.db")
        bot = self._make_bot()
        runner = MagicMock()
        repo = SessionRepository(db_path)
        ask_repo = PendingAskRepository(db_path)
        lounge_repo = LoungeRepository(db_path)
        resume_repo = PendingResumeRepository(db_path)
        settings_repo = SettingsRepository(db_path)
        usage_repo = UsageRepository(db_path)

        cog = _build_claude_chat_cog(
            bot=bot,
            repo=repo,
            runner=runner,
            max_concurrent=3,
            allowed_user_ids=None,
            ask_repo=ask_repo,
            lounge_repo=lounge_repo,
            resume_repo=resume_repo,
            settings_repo=settings_repo,
            usage_repo=usage_repo,
        )

        assert cog._resume_repo is resume_repo
        assert cog._settings_repo is settings_repo
        assert cog._usage_repo is usage_repo
        # Pre-existing wiring must be unaffected.
        assert cog._ask_repo is ask_repo
        assert cog._lounge_repo is lounge_repo
        assert cog.repo is repo
        assert cog.runner is runner
