"""Tests for RunConfig validation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from claude_discord.cogs.run_config import RunConfig


def _make_config(**overrides):
    """Create a RunConfig with minimal required fields."""
    defaults = {
        "thread": MagicMock(),
        "runner": MagicMock(),
        "prompt": "hello",
    }
    defaults.update(overrides)
    return RunConfig(**defaults)


class TestRunConfigValidation:
    """Test RunConfig.__post_init__ validation."""

    def test_empty_prompt_no_images_raises(self):
        """Empty prompt without images should raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            _make_config(prompt="")

    def test_empty_prompt_with_images_allowed(self):
        """Empty prompt with image_urls should be accepted."""
        config = _make_config(prompt="", image_urls=["https://cdn.example.com/img.png"])
        assert config.prompt == ""
        assert config.image_urls == ["https://cdn.example.com/img.png"]

    def test_nonempty_prompt_no_images_allowed(self):
        """Normal text prompt should work as before."""
        config = _make_config(prompt="hello")
        assert config.prompt == "hello"

    def test_nonempty_prompt_with_images_allowed(self):
        """Text prompt with images should work."""
        config = _make_config(prompt="describe this", image_urls=["https://example.com/a.png"])
        assert config.prompt == "describe this"

    def test_empty_prompt_empty_images_raises(self):
        """Empty prompt with empty image list should raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            _make_config(prompt="", image_urls=[])

    def test_with_prompt_preserves_images(self):
        """with_prompt should carry over image_urls."""
        original = _make_config(prompt="old", image_urls=["https://example.com/img.png"])
        updated = original.with_prompt("new prompt")
        assert updated.prompt == "new prompt"
        assert updated.image_urls == ["https://example.com/img.png"]

    def test_with_prompt_preserves_session_id_by_default(self):
        """A1 regression: with_prompt() must not silently drop session_id.

        Before the fix, with_prompt(prompt) only ever replaced ``prompt``,
        so a resume call that wanted to move to a NEW session_id had no way
        to express that via with_prompt() — the caller (_run_helper.py) had
        to reach into the dataclass directly, or (as the real bug was) just
        never carried the new id forward at all.  This test locks down that
        plain with_prompt(prompt) keeps session_id unchanged (existing
        immutable-copy behaviour for every other field).
        """
        original = _make_config(prompt="old", session_id="sess-original")
        updated = original.with_prompt("new prompt")
        assert updated.prompt == "new prompt"
        assert updated.session_id == "sess-original"

    def test_with_prompt_can_update_session_id(self):
        """A1: with_prompt(prompt, session_id=...) overrides session_id.

        This is what _run_helper.py now uses after an AskUserQuestion round
        trip: it carries forward the session_id the just-finished run
        produced (not the id the run started with), so the resumed run
        continues the same session instead of starting a context-free one.
        """
        original = _make_config(prompt="old", session_id=None)
        updated = original.with_prompt("answer prompt", session_id="sess-new")
        assert updated.prompt == "answer prompt"
        assert updated.session_id == "sess-new"
        # Original config must remain untouched (immutability).
        assert original.session_id is None
        assert original.prompt == "old"

    def test_with_prompt_still_preserves_other_fields_when_session_id_given(self):
        """Passing session_id must not regress the "preserve all other fields" contract."""
        original = _make_config(
            prompt="old",
            session_id=None,
            image_urls=["https://example.com/img.png"],
            discord_user_id="u1",
        )
        updated = original.with_prompt("answer", session_id="sess-new")
        assert updated.image_urls == ["https://example.com/img.png"]
        assert updated.discord_user_id == "u1"
        assert updated.thread is original.thread
        assert updated.runner is original.runner
