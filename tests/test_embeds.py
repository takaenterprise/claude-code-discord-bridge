"""Tests for Discord embed builders."""

from __future__ import annotations

from claude_discord.claude.types import ToolCategory, ToolUseEvent
from claude_discord.discord_ui.embeds import (
    redacted_thinking_embed,
    session_complete_embed,
    thinking_embed,
    tool_result_embed,
    tool_use_embed,
)


class TestThinkingEmbed:
    def test_content_uses_plain_code_block(self) -> None:
        """Code block must be in content (not embed description) for mobile readability."""
        content, embed = thinking_embed("Let me think about this.")
        assert content is not None
        assert content.startswith("```\n")
        assert content.endswith("\n```")
        assert "||" not in content

    def test_embed_has_no_description(self) -> None:
        """Embed should carry only the title, no description (code block is in content)."""
        _content, embed = thinking_embed("Let me think about this.")
        assert embed.description is None

    def test_thinking_text_preserved(self) -> None:
        """Original thinking text should appear inside the code block content."""
        text = "Step 1: analyze. Step 2: respond."
        content, _embed = thinking_embed(text)
        assert text in content

    def test_long_text_truncated(self) -> None:
        """Text exceeding the limit should be truncated with a notice."""
        long_text = "x" * 5000
        content, _embed = thinking_embed(long_text)
        assert len(content) <= 2000
        assert "(truncated)" in content

    def test_short_text_not_truncated(self) -> None:
        """Short text should not be modified."""
        text = "Brief thought."
        content, _embed = thinking_embed(text)
        assert "(truncated)" not in content
        assert text in content

    def test_title(self) -> None:
        """Embed should have the Thinking title."""
        _content, embed = thinking_embed("x")
        assert embed.title is not None
        assert "Thinking" in embed.title


class TestSessionCompleteEmbed:
    def test_shows_tokens_when_provided(self) -> None:
        embed = session_complete_embed(input_tokens=1000, output_tokens=500)
        assert embed.description is not None
        assert "1.0k" in embed.description
        assert "500" in embed.description

    def test_shows_cache_hit_percentage(self) -> None:
        embed = session_complete_embed(input_tokens=700, output_tokens=100, cache_read_tokens=300)
        assert embed.description is not None
        assert "%" in embed.description

    def test_no_tokens_omits_token_line(self) -> None:
        embed = session_complete_embed(cost_usd=0.01)
        assert embed.description is not None
        assert "\U0001f4ca" not in embed.description

    def test_zero_cache_omits_cache_pct(self) -> None:
        embed = session_complete_embed(input_tokens=500, output_tokens=100, cache_read_tokens=0)
        assert embed.description is not None
        assert "%" not in embed.description


class TestSessionCompleteContextUsage:
    """Tests for context usage display in session complete embed."""

    def test_context_usage_shown(self) -> None:
        # Output tokens are NOT counted — only input tokens determine context usage.
        embed = session_complete_embed(
            input_tokens=100000,
            output_tokens=5000,
            context_window=200000,
        )
        assert embed.description is not None
        # 100000 / 200000 = 50% (output_tokens excluded)
        assert "50% ctx" in embed.description

    def test_context_usage_shows_remaining_until_compact(self) -> None:
        embed = session_complete_embed(
            input_tokens=100000,
            output_tokens=5000,
            context_window=200000,
        )
        assert embed.description is not None
        # 50% used → AUTOCOMPACT_THRESHOLD(83.5) - 50 = 33.5 → 34% until compact
        assert "until compact" in embed.description

    def test_context_usage_with_cache(self) -> None:
        # cache_read_tokens counts toward context; output_tokens do not.
        embed = session_complete_embed(
            input_tokens=50000,
            output_tokens=5000,
            cache_read_tokens=100000,
            context_window=200000,
        )
        assert embed.description is not None
        # (50000 + 100000) / 200000 = 75% (output_tokens excluded)
        assert "75% ctx" in embed.description

    def test_context_usage_includes_cache_creation(self) -> None:
        # cache_creation_tokens also count toward context usage.
        embed = session_complete_embed(
            input_tokens=50000,
            output_tokens=5000,
            cache_creation_tokens=50000,
            context_window=200000,
        )
        assert embed.description is not None
        # (50000 + 50000) / 200000 = 50%
        assert "50% ctx" in embed.description

    def test_output_tokens_do_not_affect_context_pct(self) -> None:
        """Output tokens must be excluded from context window calculation."""
        embed_small = session_complete_embed(
            input_tokens=50000,
            output_tokens=100,
            context_window=200000,
        )
        embed_large = session_complete_embed(
            input_tokens=50000,
            output_tokens=100000,
            context_window=200000,
        )
        # Both should show the same context % regardless of output size
        assert embed_small.description is not None
        assert embed_large.description is not None
        assert "25% ctx" in embed_small.description
        assert "25% ctx" in embed_large.description

    def test_context_usage_capped_at_100_percent(self) -> None:
        """Context % must never exceed 100% even with very large token counts."""
        embed = session_complete_embed(
            input_tokens=500000,
            output_tokens=0,
            context_window=200000,
        )
        assert embed.description is not None
        assert "100% ctx" in embed.description

    def test_autocompact_warning_when_above_threshold(self) -> None:
        # 170000 / 200000 = 85% > 83.5% threshold → footer warning
        embed = session_complete_embed(
            input_tokens=170000,
            output_tokens=5000,
            context_window=200000,
        )
        assert embed.footer is not None
        assert "auto-compact" in embed.footer.text

    def test_above_threshold_shows_warning_icon_in_description(self) -> None:
        embed = session_complete_embed(
            input_tokens=170000,
            output_tokens=5000,
            context_window=200000,
        )
        assert embed.description is not None
        # Should show ⚠️ in the description line instead of "until compact"
        assert "\u26a0\ufe0f" in embed.description
        assert "until compact" not in embed.description

    def test_no_warning_below_threshold(self) -> None:
        embed = session_complete_embed(
            input_tokens=50000,
            output_tokens=5000,
            context_window=200000,
        )
        assert embed.footer is None or embed.footer.text is None

    def test_no_context_info_without_context_window(self) -> None:
        embed = session_complete_embed(
            input_tokens=50000,
            output_tokens=5000,
        )
        assert "% ctx" not in (embed.description or "")


class TestToolUseEmbed:
    """Tests for tool_use_embed elapsed time display."""

    def _bash_tool(self) -> ToolUseEvent:
        return ToolUseEvent(
            tool_id="t1",
            tool_name="Bash",
            tool_input={"command": "az login --use-device-code"},
            category=ToolCategory.COMMAND,
        )

    def test_in_progress_without_elapsed(self) -> None:
        embed = tool_use_embed(self._bash_tool(), in_progress=True)
        assert embed.title is not None
        assert embed.title.endswith("...")
        assert embed.description is None  # no elapsed → no description

    def test_in_progress_with_elapsed_shows_description(self) -> None:
        """Elapsed time appears in description, leaving the title stable."""
        embed = tool_use_embed(self._bash_tool(), in_progress=True, elapsed_s=42)
        assert embed.title is not None
        assert embed.title.endswith("...")
        # Title must NOT contain the elapsed time (keeps it stable across ticks)
        assert "42s" not in embed.title
        # Description carries the elapsed time
        assert embed.description is not None
        assert "42s" in embed.description

    def test_completed_no_ellipsis(self) -> None:
        embed = tool_use_embed(self._bash_tool(), in_progress=False)
        assert embed.title is not None
        assert not embed.title.endswith("...")

    def test_completed_ignores_elapsed(self) -> None:
        """elapsed_s has no effect when in_progress=False."""
        embed = tool_use_embed(self._bash_tool(), in_progress=False, elapsed_s=99)
        assert embed.title is not None
        assert "99s" not in embed.title
        assert embed.description is None

    def test_title_stable_across_ticks(self) -> None:
        """Title must be identical at t=0, t=10, t=20 so Discord doesn't re-render it."""
        base = tool_use_embed(self._bash_tool(), in_progress=True)
        tick1 = tool_use_embed(self._bash_tool(), in_progress=True, elapsed_s=10)
        tick2 = tool_use_embed(self._bash_tool(), in_progress=True, elapsed_s=20)
        assert base.title == tick1.title == tick2.title

    def test_title_within_discord_limit(self) -> None:
        """Even with a very long command name, title should be capped at 256 chars."""
        long_tool = ToolUseEvent(
            tool_id="t2",
            tool_name="Bash",
            tool_input={"command": "x" * 200},
            category=ToolCategory.COMMAND,
        )
        embed = tool_use_embed(long_tool, in_progress=True, elapsed_s=120)
        assert len(embed.title) <= 256


class TestToolResultEmbed:
    """Tests for tool_result_embed — code block in content, not embed description."""

    def test_result_in_content_not_embed(self) -> None:
        """Result content must be in content (message text), not embed description."""
        content, embed = tool_result_embed("\U0001f527 Running: ls...", "file1\nfile2\nfile3")
        assert content is not None
        assert "file1" in content
        assert embed.description is None
        assert len(embed.fields) == 0

    def test_result_wrapped_in_code_block(self) -> None:
        content, _embed = tool_result_embed("\U0001f527 Running: ls...", "output here")
        assert content is not None
        assert content.startswith("```")
        assert content.endswith("```")

    def test_title_strips_trailing_dots(self) -> None:
        """The '...' suffix from the in-progress title should be stripped."""
        _content, embed = tool_result_embed("\U0001f527 Running: ls...", "ok")
        assert embed.title is not None
        assert not embed.title.endswith(".")

    def test_30_lines_fit_without_truncation(self) -> None:
        """30 lines of typical output should display in full."""
        lines = [f"Step {i:02d}/30 \u2014 output text here" for i in range(1, 31)]
        result_text = "\n".join(lines)
        content, _embed = tool_result_embed("\U0001f527 Running: cmd...", result_text)
        assert content is not None
        for line in lines:
            assert line in content

    def test_empty_result_no_content(self) -> None:
        content, embed = tool_result_embed("\U0001f527 Running: ls...", "")
        assert content is None
        assert embed.description is None


class TestRedactedThinkingEmbed:
    def test_title_mentions_redacted(self) -> None:
        embed = redacted_thinking_embed()
        assert embed.title is not None
        assert "redacted" in embed.title.lower()

    def test_has_description(self) -> None:
        embed = redacted_thinking_embed()
        assert embed.description is not None
        assert len(embed.description) > 0

    def test_color_distinct_from_regular_thinking(self) -> None:
        _content, regular_embed = thinking_embed("x")
        redacted = redacted_thinking_embed()
        assert redacted.colour.value != regular_embed.colour.value


class TestTodoEmbed:
    """Tests for todo_embed()."""

    def test_shows_all_statuses(self) -> None:
        from claude_discord.claude.types import TodoItem
        from claude_discord.discord_ui.embeds import todo_embed

        todos = [
            TodoItem(content="Task A", status="completed"),
            TodoItem(content="Task B", status="in_progress", active_form="Doing Task B"),
            TodoItem(content="Task C", status="pending"),
        ]
        embed = todo_embed(todos)
        assert embed.description is not None
        assert "\u2705" in embed.description
        assert "\U0001f504" in embed.description
        assert "\u2b1c" in embed.description

    def test_in_progress_shows_active_form(self) -> None:
        from claude_discord.claude.types import TodoItem
        from claude_discord.discord_ui.embeds import todo_embed

        todos = [TodoItem(content="Task", status="in_progress", active_form="Running Task")]
        embed = todo_embed(todos)
        assert embed.description is not None
        assert "Running Task" in embed.description

    def test_in_progress_falls_back_to_content(self) -> None:
        from claude_discord.claude.types import TodoItem
        from claude_discord.discord_ui.embeds import todo_embed

        todos = [TodoItem(content="Task without active form", status="in_progress", active_form="")]
        embed = todo_embed(todos)
        assert embed.description is not None
        assert "Task without active form" in embed.description

    def test_title_shows_progress_count(self) -> None:
        from claude_discord.claude.types import TodoItem
        from claude_discord.discord_ui.embeds import todo_embed

        todos = [
            TodoItem(content="Done", status="completed"),
            TodoItem(content="Pending", status="pending"),
        ]
        embed = todo_embed(todos)
        assert embed.title is not None
        assert "1/2" in embed.title

    def test_empty_list(self) -> None:
        from claude_discord.discord_ui.embeds import todo_embed

        embed = todo_embed([])
        assert embed.description is not None
        assert "no tasks" in embed.description


class TestToolResultPreviewEmbed:
    """tool_result_preview_embed — collapsed view showing first 3 lines."""

    def test_first_3_lines_visible(self) -> None:
        from claude_discord.discord_ui.embeds import tool_result_preview_embed

        full = "\n".join(f"line{i}" for i in range(10))
        content, _embed = tool_result_preview_embed("\U0001f527 Running: cat", full)

        assert content is not None
        assert "line0" in content
        assert "line1" in content
        assert "line2" in content

    def test_remaining_lines_hidden(self) -> None:
        from claude_discord.discord_ui.embeds import tool_result_preview_embed

        full = "\n".join(f"line{i}" for i in range(10))
        content, _embed = tool_result_preview_embed("\U0001f527 Running: cat", full)

        assert "line3" not in content

    def test_hidden_line_count_shown(self) -> None:
        from claude_discord.discord_ui.embeds import tool_result_preview_embed

        full = "\n".join(f"line{i}" for i in range(10))
        content, _embed = tool_result_preview_embed("\U0001f527 Running: cat", full)

        # 10 lines total, 3 shown → 7 hidden
        assert "+7" in content

    def test_short_content_shows_all(self) -> None:
        """3 lines or fewer — no hidden-line hint needed."""
        from claude_discord.discord_ui.embeds import tool_result_preview_embed

        full = "line1\nline2\nline3"
        content, _embed = tool_result_preview_embed("\U0001f527 Running: cat", full)

        assert "line1" in content
        assert "line2" in content
        assert "line3" in content
        assert "lines" not in content  # No hidden-line hint

    def test_title_strips_trailing_dots(self) -> None:
        from claude_discord.discord_ui.embeds import tool_result_preview_embed

        _content, embed = tool_result_preview_embed("\U0001f527 Running: cat...", "output")
        assert embed.title is not None
        assert not embed.title.endswith(".")
