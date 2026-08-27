"""Tests for the bot-memo bridge (記憶橋 v1): claude_discord/ext/bot_memo.py.

Covers the pure entry builder (build_memo_entry), the file-append I/O
(append_memo) — including first-write frontmatter and failure handling — and
the OFF-by-default wiring in _run_helper.run_claude_with_config().
"""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from claude_discord.claude.types import MessageType, StreamEvent
from claude_discord.cogs._run_helper import run_claude_in_thread, run_claude_with_config
from claude_discord.cogs.run_config import RunConfig
from claude_discord.ext.bot_memo import append_memo, build_memo_entry


class TestBuildMemoEntry:
    """Unit tests for the pure entry-builder function."""

    def _now(self) -> datetime:
        return datetime(2026, 8, 27, 14, 32, 0)

    def test_contains_time_bot_thread_session(self) -> None:
        entry = build_memo_entry(
            bot_name="ccdb",
            thread_id=123456789,
            thread_name="質問スレ",
            prompt="こんにちは",
            result_text="こんにちは！",
            session_id="sess-abc",
            now=self._now(),
        )
        assert "14:32" in entry
        assert "ccdb" in entry
        assert "123456789" in entry
        assert "sess-abc" in entry
        assert "こんにちは" in entry
        assert "こんにちは！" in entry

    def test_missing_session_id_shown_as_none_marker(self) -> None:
        entry = build_memo_entry(
            bot_name="ccdb",
            thread_id=1,
            thread_name="t",
            prompt="p",
            result_text="r",
            session_id=None,
            now=self._now(),
        )
        assert "(none)" in entry

    def test_prompt_under_limit_not_truncated(self) -> None:
        prompt = "x" * 300
        entry = build_memo_entry(
            bot_name="ccdb",
            thread_id=1,
            thread_name="t",
            prompt=prompt,
            result_text="ok",
            session_id="s1",
            now=self._now(),
        )
        assert prompt in entry
        assert "省略" not in entry.split("**A:**")[0]

    def test_prompt_over_limit_truncated_with_marker(self) -> None:
        prompt = "x" * 400
        entry = build_memo_entry(
            bot_name="ccdb",
            thread_id=1,
            thread_name="t",
            prompt=prompt,
            result_text="ok",
            session_id="s1",
            now=self._now(),
        )
        q_line = entry.split("**A:**")[0]
        assert "x" * 300 in q_line
        assert "x" * 301 not in q_line
        assert "300字超のため省略" in q_line

    def test_result_under_limit_not_truncated(self) -> None:
        result = "y" * 1500
        entry = build_memo_entry(
            bot_name="ccdb",
            thread_id=1,
            thread_name="t",
            prompt="q",
            result_text=result,
            session_id="s1",
            now=self._now(),
        )
        assert result in entry

    def test_result_over_limit_truncated_with_marker(self) -> None:
        result = "y" * 2000
        entry = build_memo_entry(
            bot_name="ccdb",
            thread_id=1,
            thread_name="t",
            prompt="q",
            result_text=result,
            session_id="s1",
            now=self._now(),
        )
        assert "y" * 1500 in entry
        assert "y" * 1501 not in entry
        assert "1500字超のため省略" in entry

    def test_never_raises_on_ordinary_strings(self) -> None:
        # Pure function contract: no I/O, should not raise for well-formed input.
        entry = build_memo_entry(
            bot_name="",
            thread_id=0,
            thread_name="",
            prompt="",
            result_text="",
            session_id=None,
            now=self._now(),
        )
        assert isinstance(entry, str)


class TestAppendMemo:
    """Tests for the file-append I/O layer."""

    def _now(self) -> datetime:
        return datetime(2026, 8, 27, 9, 5, 0)

    def test_creates_new_file_with_frontmatter(self, tmp_path: Path) -> None:
        entry = (
            "### 09:05 — ccdb (thread 123456, session s1)\n\n"
            "**Q:** hi\n\n**A:** hello\n\n---\n\n"
        )
        result = append_memo(
            memo_dir=str(tmp_path),
            bot_name="ccdb",
            thread_id=123456,
            thread_name="雑談スレ",
            entry=entry,
            now=self._now(),
        )
        assert result is not None
        content = result.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "type: bot-conversation" in content
        assert "when: 2026-08-27" in content
        assert "topic: [ccdb, Discord]" in content
        assert "# 雑談スレ" in content
        assert entry in content

    def test_filename_format(self, tmp_path: Path) -> None:
        result = append_memo(
            memo_dir=str(tmp_path),
            bot_name="ccdb",
            thread_id=987654321123456,  # last 6 digits: 123456
            thread_name="t",
            entry="entry\n",
            now=self._now(),
        )
        assert result is not None
        assert result.name == "2026-08-27_ccdb_123456.md"

    def test_second_call_same_day_appends_without_duplicate_frontmatter(
        self, tmp_path: Path
    ) -> None:
        append_memo(
            memo_dir=str(tmp_path),
            bot_name="ccdb",
            thread_id=42,
            thread_name="t",
            entry="first entry\n",
            now=self._now(),
        )
        result = append_memo(
            memo_dir=str(tmp_path),
            bot_name="ccdb",
            thread_id=42,
            thread_name="t",
            entry="second entry\n",
            now=self._now(),
        )
        assert result is not None
        content = result.read_text(encoding="utf-8")
        assert content.count("type: bot-conversation") == 1
        assert "first entry" in content
        assert "second entry" in content

    def test_thread_name_sanitized_in_heading(self, tmp_path: Path) -> None:
        result = append_memo(
            memo_dir=str(tmp_path),
            bot_name="ccdb",
            thread_id=1,
            thread_name="a/b\nc" + "z" * 60,
            entry="entry\n",
            now=self._now(),
        )
        assert result is not None
        content = result.read_text(encoding="utf-8")
        assert "a_b c" in content
        # Heading line must not contain a raw newline injected from thread_name.
        heading_line = next(line for line in content.splitlines() if line.startswith("# "))
        assert len(heading_line) <= 2 + 50

    def test_creates_memo_dir_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "does" / "not" / "exist"
        result = append_memo(
            memo_dir=str(nested),
            bot_name="ccdb",
            thread_id=1,
            thread_name="t",
            entry="entry\n",
            now=self._now(),
        )
        assert result is not None
        assert nested.exists()

    def test_write_failure_returns_none_and_does_not_raise(self, tmp_path: Path) -> None:
        """A memo_dir that exists but is not writable must fail closed (None), not raise."""
        if os.name == "nt" or os.geteuid() == 0:
            pytest.skip("permission bits are not enforceable as root or on Windows")

        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(stat.S_IREAD | stat.S_IEXEC)  # r-x, no write

        try:
            result = append_memo(
                memo_dir=str(readonly_dir),
                bot_name="ccdb",
                thread_id=1,
                thread_name="t",
                entry="entry\n",
                now=self._now(),
            )
            assert result is None
        finally:
            # Restore permissions so pytest's tmp_path cleanup can remove it.
            readonly_dir.chmod(stat.S_IRWXU)


class TestRunHelperWiring:
    """Tests for the BOT_MEMO_DIR-gated wiring inside run_claude_with_config()."""

    @pytest.fixture
    def thread(self) -> MagicMock:
        t = MagicMock(spec=discord.Thread)
        t.id = 555666777888
        t.name = "テストスレ"
        t.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
        return t

    @pytest.fixture
    def runner(self) -> MagicMock:
        r = MagicMock()
        r.working_dir = None
        return r

    def _make_async_gen(self, events: list[StreamEvent]):
        async def gen(*args, **kwargs):
            for e in events:
                yield e

        return gen

    def _simple_events(self, text: str = "Final answer.") -> list[StreamEvent]:
        return [
            StreamEvent(message_type=MessageType.SYSTEM, session_id="sess-memo"),
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                text=text,
                session_id="sess-memo",
                cost_usd=0.01,
                duration_ms=500,
            ),
        ]

    @pytest.mark.asyncio
    async def test_off_by_default_no_file_written(
        self, thread: MagicMock, runner: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With BOT_MEMO_DIR unset, no memo file is created — behaviour is unchanged."""
        monkeypatch.delenv("BOT_MEMO_DIR", raising=False)
        runner.run = self._make_async_gen(self._simple_events())

        config = RunConfig(thread=thread, runner=runner, prompt="hello there")
        result = await run_claude_with_config(config)

        assert result == "sess-memo"
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_enabled_writes_memo_file(
        self, thread: MagicMock, runner: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With BOT_MEMO_DIR set, a memo file is written after the turn completes."""
        monkeypatch.setenv("BOT_MEMO_DIR", str(tmp_path))
        monkeypatch.setenv("BOT_NAME", "ccdb-test")
        runner.run = self._make_async_gen(self._simple_events("Here is the answer."))

        config = RunConfig(thread=thread, runner=runner, prompt="what's the status?")
        result = await run_claude_with_config(config)

        assert result == "sess-memo"
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "what's the status?" in content
        assert "Here is the answer." in content
        assert "ccdb-test" in content
        assert "sess-memo" in content

    @pytest.mark.asyncio
    async def test_error_turn_not_memoized(
        self, thread: MagicMock, runner: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A RESULT event carrying an error must not produce a memo entry."""
        monkeypatch.setenv("BOT_MEMO_DIR", str(tmp_path))
        events = [
            StreamEvent(
                message_type=MessageType.RESULT,
                is_complete=True,
                error="Something went wrong",
            ),
        ]
        runner.run = self._make_async_gen(events)

        config = RunConfig(thread=thread, runner=runner, prompt="do the thing")
        await run_claude_with_config(config)

        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_memo_write_failure_does_not_break_run(
        self, thread: MagicMock, runner: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A BOT_MEMO_DIR pointing at an unwritable path must not crash the run."""
        if os.name == "nt" or os.geteuid() == 0:
            pytest.skip("permission bits are not enforceable as root or on Windows")

        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(stat.S_IREAD | stat.S_IEXEC)
        monkeypatch.setenv("BOT_MEMO_DIR", str(readonly_dir))
        runner.run = self._make_async_gen(self._simple_events())

        try:
            config = RunConfig(thread=thread, runner=runner, prompt="hello")
            result = await run_claude_with_config(config)
            # The run itself must complete normally despite the memo failure.
            assert result == "sess-memo"
        finally:
            readonly_dir.chmod(stat.S_IRWXU)

    @pytest.mark.asyncio
    async def test_legacy_shim_also_respects_flag(
        self, thread: MagicMock, runner: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_claude_in_thread() (legacy shim) goes through the same wiring."""
        monkeypatch.setenv("BOT_MEMO_DIR", str(tmp_path))
        runner.run = self._make_async_gen(self._simple_events("Shim answer."))

        repo = MagicMock()
        repo.save = AsyncMock()

        await run_claude_in_thread(thread, runner, repo, "shim prompt", None)

        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert "Shim answer." in files[0].read_text(encoding="utf-8")
