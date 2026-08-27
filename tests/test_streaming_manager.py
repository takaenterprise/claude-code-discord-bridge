"""Regression tests for StreamingMessageManager's overflow handling.

These reproduce the display bugs that were traced to two interacting flaws:

1. ``append()``'s overflow-split only ran ``if len(buffer) > STREAM_MAX_CHARS
   and self._current_message`` -- so a burst of text arriving before the
   first Discord message existed could grow the buffer past 2000 chars
   unnoticed (the guard was skipped because ``self._current_message`` was
   still ``None``).
2. ``_flush()``'s old "> 2000 chars" chunk path sent the overflow as several
   fresh messages but never cleared ``self._buffer``. The *next* overflow
   cycle then re-derived "first N chars of the whole (stale, never-cleared)
   buffer" and edited the *most recently sent* message with it -- silently
   overwriting a message that already displayed later (tail) content with
   earlier (head) content, and repeating already-delivered text.

The fix (``_drain_overflow``) makes the buffer-splitting run unconditionally
on every append, so the buffer only ever holds the content of the message
currently open for editing, split at fence-aware boundaries via
``chunker.split_chunk``. See ``streaming_manager.py`` for the full mechanism.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from claude_discord.discord_ui.streaming_manager import StreamingMessageManager


class _RecordingThread:
    """A fake ``discord.Thread`` whose ``send()`` returns a fresh,
    independently trackable message on every call.

    A single shared ``MagicMock`` message (as used by the default `thread`
    fixture elsewhere in this test suite) cannot distinguish "message A was
    edited" from "message B was edited" -- both look like calls on the same
    object. That indistinguishability is exactly what let the overwrite bug
    ship unnoticed, so these tests need per-message tracking instead.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, int, str]] = []  # (kind, msg_index, content)
        self._next_index = 0

    async def send(self, content: str, **kwargs: object) -> MagicMock:
        idx = self._next_index
        self._next_index += 1
        self.events.append(("send", idx, content))

        async def _edit(*, content: str, **_kwargs: object) -> None:
            self.events.append(("edit", idx, content))

        msg = MagicMock(spec=discord.Message)
        msg.edit = AsyncMock(side_effect=_edit)
        return msg


def _per_message_history(events: list[tuple[str, int, str]]) -> dict[int, list[str]]:
    """Group event contents by message index, preserving call order."""
    history: dict[int, list[str]] = {}
    for _kind, idx, content in events:
        history.setdefault(idx, []).append(content)
    return history


class TestOverflowDoesNotOverwriteSentMessages:
    """① 2000字超ストリーミングの再現ケース: 既送メッセージが先頭文で上書き
    されない・末尾が消えないことを確認する。
    """

    @pytest.mark.asyncio
    async def test_message_content_only_grows_never_gets_overwritten(self) -> None:
        thread = _RecordingThread()
        mgr = StreamingMessageManager(thread)

        # A single append far over STREAM_MAX_CHARS (2050 chars, no
        # newlines) arriving before any message exists yet -- exactly the
        # condition that used to skip append()'s overflow-split guard.
        head = "H" * 1000
        tail_1 = "T" * 1050
        await mgr.append(head + tail_1)

        # More text arrives afterward. Under the old code this re-sliced the
        # never-cleared buffer (still holding the *entire* head+tail_1 text)
        # and edited the most-recently-sent message with "first N chars of
        # the whole buffer" -- mostly head content -- even though that
        # message actually displayed the tail.
        new_text = "Z" * 50
        await mgr.append(new_text)

        await mgr.finalize()

        history = _per_message_history(thread.events)
        assert len(history) >= 2, "text this long must span multiple messages"

        # Core invariant: whatever a message displays may only ever grow by
        # appending -- each successive edit's content must retain everything
        # the previous one showed as a prefix. A replacement that is NOT an
        # extension (as in the old bug: a tail message's "T...100" replaced
        # by an unrelated "H...1000T...900") is exactly the overwrite bug.
        for idx, contents in history.items():
            for prev, cur in zip(contents, contents[1:], strict=False):
                assert cur.startswith(prev), (
                    f"message {idx} was overwritten instead of extended: "
                    f"{prev[:30]!r}... -> {cur[:30]!r}..."
                )

        # The trailing text must actually be delivered somewhere, not lost
        # when its message was closed out.
        final_contents = [contents[-1] for contents in history.values()]
        assert any(new_text in c for c in final_contents), "trailing text was lost"

        # The head must not end up duplicated across more than one message.
        head_hits = sum(1 for c in final_contents if head in c)
        assert head_hits <= 1, "head content was duplicated across messages"

    @pytest.mark.asyncio
    async def test_many_small_appends_never_edit_a_closed_out_message(self) -> None:
        """A more realistic partial-stream burst: many small deltas whose
        cumulative total crosses STREAM_MAX_CHARS several times. Once a
        later message has been opened (a new ``send()``), the earlier one
        must never receive another edit.
        """
        thread = _RecordingThread()
        mgr = StreamingMessageManager(thread)

        for i in range(40):
            await mgr.append(f"[{i:03d}]" + ("y" * 120) + " ")
        await mgr.finalize()

        send_pos = {idx: pos for pos, (kind, idx, _c) in enumerate(thread.events) if kind == "send"}
        assert len(send_pos) >= 2, "text this long must span multiple messages"

        for pos, (kind, idx, _content) in enumerate(thread.events):
            if kind != "edit":
                continue
            next_send_pos = send_pos.get(idx + 1)
            assert next_send_pos is None or pos < next_send_pos, (
                f"message {idx} was edited (event #{pos}) after message {idx + 1} "
                f"had already been opened (event #{next_send_pos})"
            )


class TestOverflowClosesOpenCodeFence:
    """② フェンスまたぎ切断でMarkdownが閉じることを確認する。"""

    @pytest.mark.asyncio
    async def test_fence_is_closed_and_reopened_across_split(self) -> None:
        thread = _RecordingThread()
        mgr = StreamingMessageManager(thread)

        code_lines = "x = 1\n" * 400  # ~2400 chars, forces a split mid-fence
        text = "Before the fence.\n```python\n" + code_lines + "```\nAfter the fence."
        await mgr.append(text)
        await mgr.finalize()

        delivered = [content for _kind, _idx, content in thread.events]
        assert len(delivered) >= 2, "text this long must span multiple messages"

        # Every individual chunk must have balanced ``` markers -- any fence
        # opened inside a chunk must be closed before the chunk ends, so
        # Markdown never renders half-open in Discord.
        for i, content in enumerate(delivered):
            assert content.count("```") % 2 == 0, (
                f"chunk {i} has an unbalanced code fence: {content!r}"
            )

        # The language annotation must survive the reopen in the next chunk.
        assert any(c.strip().startswith("```python") for c in delivered[1:]), (
            "reopened fence lost its language annotation"
        )

        # Nothing from the code block should be silently dropped at the seam.
        assert "x = 1" in delivered[0]
        assert "x = 1" in delivered[-1]
        assert "After the fence." in delivered[-1]

    @pytest.mark.asyncio
    async def test_overflow_on_already_open_message_stays_fence_aware(self) -> None:
        """The append()-side overflow cut (old code: naive
        ``self._buffer[STREAM_MAX_CHARS:]`` slicing, no fence awareness) must
        stay fence-aware even when it fires on a message that already exists
        -- not just on the very first message (which used to go through
        chunk_message()'s already fence-aware chunk path instead).
        """
        thread = _RecordingThread()
        mgr = StreamingMessageManager(thread)

        # First append: a short, still-open fence that fits in one message.
        # Its content is legitimately unbalanced at this point -- it is
        # still being live-edited, not yet closed out.
        await mgr.append("Intro.\n```python\n")
        # Second append: enough code to push the buffer well past
        # STREAM_MAX_CHARS while still inside that open fence. This forces
        # message 0 to be closed out mid-fence. The fence is properly closed
        # at the very end, as a complete response would.
        await mgr.append("x = 1\n" * 400 + "```\nDone.")
        await mgr.finalize()

        history = _per_message_history(thread.events)
        assert len(history) >= 2

        # Once a message is closed out (superseded by a later one, or the
        # last message at finalize()), its FINAL displayed content must have
        # balanced fence markers -- an intermediate, still-growing edit may
        # legitimately be unbalanced, but nothing may be left permanently
        # half-open.
        for idx, contents in history.items():
            final = contents[-1]
            assert final.count("```") % 2 == 0, (
                f"message {idx}'s final content has an unbalanced code fence: {final!r}"
            )
