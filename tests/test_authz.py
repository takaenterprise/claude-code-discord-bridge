"""Tests for Discord-side authorization (authz.py, GatedCommandTree, AskView binding).

Regression tests for the 2026-09-17 security audit:
- slash commands had no user authorization (any guild member could mint webhook
  tokens, change the global model, kill sessions, trigger batches)
- AskUserQuestion answers were not bound to allowed users (a non-allowlisted
  member could write the next prompt of someone else's session)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from claude_discord.authz import (
    SELF_AUTHORIZED_EXTRA,
    GatedCommandTree,
    build_allowed_user_ids,
    is_allowed,
    resolve_allowed_user_ids,
)
from claude_discord.bot import ClaudeDiscordBot
from claude_discord.claude.types import AskOption, AskQuestion
from claude_discord.discord_ui.ask_bus import AskAnswerBus
from claude_discord.discord_ui.ask_view import AskView

OWNER = 111
MEMBER = 222
STRANGER = 999


def _interaction(user_id: int, *, command=None, itype=discord.InteractionType.application_command):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.command = command
    interaction.type = itype
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    return interaction


class TestBuildAllowedUserIds:
    def test_unconfigured_is_unrestricted(self) -> None:
        assert build_allowed_user_ids(None, None) is None
        assert is_allowed(None, STRANGER)

    def test_owner_only(self) -> None:
        assert build_allowed_user_ids(None, OWNER) == frozenset({OWNER})

    def test_allowlist_plus_owner(self) -> None:
        assert build_allowed_user_ids({MEMBER}, OWNER) == frozenset({MEMBER, OWNER})

    def test_empty_allowlist_without_owner_denies_all(self) -> None:
        allowed = build_allowed_user_ids(set(), None)
        assert allowed == frozenset()
        assert not is_allowed(allowed, STRANGER)


class TestGatedCommandTree:
    def _bot(self, owner_id=OWNER, allowed_user_ids=None) -> ClaudeDiscordBot:
        return ClaudeDiscordBot(channel_id=1, owner_id=owner_id, allowed_user_ids=allowed_user_ids)

    def test_bot_uses_gated_tree(self) -> None:
        assert isinstance(self._bot().tree, GatedCommandTree)

    @pytest.mark.asyncio
    async def test_stranger_rejected_with_ephemeral_message(self) -> None:
        bot = self._bot(allowed_user_ids={MEMBER})
        interaction = _interaction(STRANGER)
        interaction.client = bot
        assert await bot.tree.interaction_check(interaction) is False
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("user_id", [OWNER, MEMBER])
    async def test_owner_and_allowlisted_pass(self, user_id: int) -> None:
        bot = self._bot(allowed_user_ids={MEMBER})
        interaction = _interaction(user_id)
        interaction.client = bot
        assert await bot.tree.interaction_check(interaction) is True
        interaction.response.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unconfigured_bot_keeps_legacy_open_access(self) -> None:
        bot = self._bot(owner_id=None, allowed_user_ids=None)
        interaction = _interaction(STRANGER)
        interaction.client = bot
        assert await bot.tree.interaction_check(interaction) is True

    @pytest.mark.asyncio
    async def test_self_authorized_command_is_left_to_its_own_check(self) -> None:
        bot = self._bot()
        command = MagicMock()
        command.extras = {SELF_AUTHORIZED_EXTRA: True}
        command.parent = None
        interaction = _interaction(STRANGER, command=command)
        interaction.client = bot
        assert await bot.tree.interaction_check(interaction) is True

    @pytest.mark.asyncio
    async def test_autocomplete_rejected_without_message(self) -> None:
        bot = self._bot()
        interaction = _interaction(STRANGER, itype=discord.InteractionType.autocomplete)
        interaction.client = bot
        assert await bot.tree.interaction_check(interaction) is False
        interaction.response.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_real_cogs_only_order_and_shuppin_are_self_authorized(self) -> None:
        """Every registered command is gated except those that enforce their own allowlist."""
        from claude_discord.cogs.channel_manage import ChannelManageCog
        from claude_discord.cogs.listing_command import ListingCommandCog
        from claude_discord.cogs.order_command import OrderCommandCog

        bot = self._bot()
        await bot.add_cog(ChannelManageCog(bot))
        await bot.add_cog(OrderCommandCog(bot))
        await bot.add_cog(ListingCommandCog(bot))

        self_authorized = set()
        for command in bot.tree.get_commands():
            interaction = _interaction(STRANGER, command=command)
            interaction.client = bot
            if await bot.tree.interaction_check(interaction):
                self_authorized.add(command.name)
        assert self_authorized == {"order", "shuppin"}
        assert {"webhook-list", "channel-webhook", "channel-create"} <= {
            c.name for c in bot.tree.get_commands()
        }

    def test_resolve_from_bot_attributes(self) -> None:
        bot = self._bot(allowed_user_ids={MEMBER})
        assert resolve_allowed_user_ids(bot) == frozenset({MEMBER, OWNER})


class TestAskViewBinding:
    def _view(self, allowed, bus: AskAnswerBus) -> AskView:
        q = AskQuestion(question="Deploy to which env?", options=[AskOption(label="staging")])
        return AskView(q, thread_id=777, q_idx=0, bus=bus, allowed_user_ids=allowed)

    @pytest.mark.asyncio
    async def test_stranger_cannot_answer(self) -> None:
        bus = AskAnswerBus()
        queue = bus.register(777)
        view = self._view(frozenset({OWNER}), bus)
        interaction = _interaction(STRANGER)
        assert await view.interaction_check(interaction) is False
        interaction.response.send_message.assert_awaited_once()
        assert queue.empty()

    @pytest.mark.asyncio
    async def test_allowed_user_can_answer(self) -> None:
        view = self._view(frozenset({OWNER}), AskAnswerBus())
        assert await view.interaction_check(_interaction(OWNER)) is True

    @pytest.mark.asyncio
    async def test_unrestricted_when_unconfigured(self) -> None:
        view = self._view(None, AskAnswerBus())
        assert await view.interaction_check(_interaction(STRANGER)) is True

    @pytest.mark.asyncio
    async def test_collect_ask_answers_binds_view_to_allowed_users(self) -> None:
        from claude_discord.discord_ui.ask_handler import collect_ask_answers

        q = AskQuestion(question="Which?", options=[AskOption(label="A")])
        thread = MagicMock()
        thread.id = 4242
        thread.send = AsyncMock(return_value=AsyncMock())

        with patch(
            "claude_discord.discord_ui.ask_handler.asyncio.wait_for",
            side_effect=asyncio.TimeoutError,
        ):
            await collect_ask_answers(
                thread, [q], session_id="s", allowed_user_ids=frozenset({OWNER})
            )

        view = thread.send.await_args.kwargs["view"]
        assert await view.interaction_check(_interaction(STRANGER)) is False
        assert await view.interaction_check(_interaction(OWNER)) is True

    @pytest.mark.asyncio
    async def test_restored_views_are_bound_to_bot_principals(self) -> None:
        bot = ClaudeDiscordBot(channel_id=1, owner_id=OWNER, allowed_user_ids={MEMBER})
        record = MagicMock()
        record.thread_id = 5
        record.question_idx = 0
        record.questions = MagicMock(
            return_value=[{"question": "Q?", "options": [{"label": "A"}], "multi_select": False}]
        )
        bot.ask_repo = MagicMock()
        bot.ask_repo.list_all = AsyncMock(return_value=[record])
        added = []
        with patch.object(bot, "add_view", side_effect=added.append):
            await bot._restore_pending_ask_views()
        assert len(added) == 1
        assert await added[0].interaction_check(_interaction(STRANGER)) is False
        assert await added[0].interaction_check(_interaction(MEMBER)) is True


class TestRunHelperPassesAnswerers:
    @pytest.mark.asyncio
    async def test_ask_answerer_ids_reach_collect_ask_answers(self) -> None:
        """run_claude_with_config forwards RunConfig.ask_answerer_ids to collect_ask_answers."""
        import inspect

        from claude_discord.cogs import _run_helper

        source = inspect.getsource(_run_helper.run_claude_with_config)
        assert "allowed_user_ids=config.ask_answerer_ids" in source
