"""Discord-side authorization shared by slash commands and interactive views.

Why this module exists
----------------------
Before this module, "who may do this?" was answered separately in each entry
point: ``ClaudeChatCog.on_message`` checked ``allowed_user_ids``, a few cogs
(/order, /shuppin, /skill, /exec) had their own checks, and every other slash
command and the AskUserQuestion buttons checked nothing.  Any guild member
could therefore mint webhook tokens, change the global model, kill other
users' sessions, or type the next prompt of someone else's session through
the AskUserQuestion "Other" button.

This module gives all of those entry points one definition of the principal
set, and a :class:`GatedCommandTree` that enforces it before any slash-command
callback runs.

``GatedCommandTree`` is **not enough on its own**: discord.py only routes
application-command and autocomplete interactions through the command tree.
Component interactions (button clicks, select menus) go straight from
``ConnectionState.parse_interaction_create`` to ``ViewStore.dispatch_view``,
which runs only the item checks and ``View.interaction_check``.  Every view
that performs a privileged action therefore needs its own check.  Use
:class:`GatedView` (allowlist) or :class:`InvokerBoundView` (allowlist plus the
user who opened the dialog) instead of ``discord.ui.View`` for those.

Semantics (kept identical to ``ClaudeChatCog.on_message``):

- If neither an allowlist nor an owner is configured, access is unrestricted
  (``None``).  This preserves behaviour for consumers that never configured
  access control.
- Otherwise the allowed set is ``allowed_user_ids ∪ {owner_id}``.

Commands that implement their own, possibly broader, allowlist (for example
``/order`` with ``ORDER_ALLOWED_USER_IDS``) opt out of the tree gate with
``extras={SELF_AUTHORIZED_EXTRA: True}``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

#: ``app_commands.command(extras=...)`` key for commands that enforce their own
#: user allowlist and must not be additionally restricted by the tree gate.
SELF_AUTHORIZED_EXTRA = "ccdb_self_authorized"

_DENIED_MSG = "このコマンドの使用権限がありません。管理者に連絡してください。"

#: Denial shown when a component click comes from outside the bot's principal set.
VIEW_DENIED_MSG = "この操作を行う権限がありません。"

#: Denial shown when a component click comes from someone other than the user who
#: opened the dialog.  Same wording as the pre-existing OrderConfirmView /
#: ListingConfirmView checks so the user-visible behaviour stays uniform.
VIEW_NOT_INVOKER_MSG = "このボタンは操作できません。"


def build_allowed_user_ids(
    allowed_user_ids: Iterable[int] | None,
    owner_id: int | None,
) -> frozenset[int] | None:
    """Return the principal set, or ``None`` when access control is unconfigured."""
    if allowed_user_ids is None and not owner_id:
        return None
    ids = set(allowed_user_ids or ())
    if owner_id:
        ids.add(owner_id)
    return frozenset(ids)


def resolve_allowed_user_ids(client: object) -> frozenset[int] | None:
    """Resolve the principal set from a bot instance's attributes."""
    return build_allowed_user_ids(
        getattr(client, "allowed_user_ids", None),
        getattr(client, "owner_id", None),
    )


def is_allowed(allowed: frozenset[int] | None, user_id: int) -> bool:
    return allowed is None or user_id in allowed


def _is_self_authorized(command: object) -> bool:
    while command is not None:
        extras = getattr(command, "extras", None) or {}
        if extras.get(SELF_AUTHORIZED_EXTRA):
            return True
        command = getattr(command, "parent", None)
    return False


class GatedCommandTree(app_commands.CommandTree):
    """CommandTree that rejects users outside the bot's principal set."""

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        allowed = resolve_allowed_user_ids(interaction.client)
        if is_allowed(allowed, interaction.user.id):
            return True
        if _is_self_authorized(interaction.command):
            return True

        logger.warning(
            "Rejected app command %r from unauthorized user %s",
            getattr(interaction.command, "qualified_name", None),
            interaction.user.id,
        )
        # Autocomplete interactions cannot carry a message response.
        if (
            interaction.type is not discord.InteractionType.autocomplete
            and not interaction.response.is_done()
        ):
            try:
                await interaction.response.send_message(_DENIED_MSG, ephemeral=True)
            except discord.HTTPException:
                logger.debug("Could not send denial message", exc_info=True)
        return False


async def deny_view_interaction(interaction: discord.Interaction, message: str) -> None:
    """Send an ephemeral denial for a component interaction, best effort."""
    if interaction.response.is_done():
        return
    try:
        await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.debug("Could not send view denial message", exc_info=True)


class GatedView(discord.ui.View):
    """View whose component clicks are limited to the bot's principal set.

    Component interactions never reach :class:`GatedCommandTree`, so a view that
    performs a privileged action (interrupting a session, launching a batch)
    must gate itself.  Semantics match the tree gate: when neither an allowlist
    nor an owner is configured on the bot, every click is allowed, so consumers
    that never configured access control keep their current behaviour.
    """

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        allowed = resolve_allowed_user_ids(interaction.client)
        if is_allowed(allowed, interaction.user.id):
            return True
        logger.warning(
            "Rejected %s component interaction from unauthorized user %s",
            type(self).__name__,
            interaction.user.id,
        )
        await deny_view_interaction(interaction, VIEW_DENIED_MSG)
        return False


class InvokerBoundView(GatedView):
    """:class:`GatedView` that additionally binds clicks to one user.

    ``invoker_id`` is the user who opened the dialog (``interaction.user.id`` of
    the slash command).  It defaults to ``None`` so existing call sites keep
    working; with ``None`` only the allowlist gate applies.
    """

    def __init__(self, *args: object, invoker_id: int | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if self.invoker_id is None or interaction.user.id == self.invoker_id:
            return True
        logger.warning(
            "Rejected %s component interaction from non-invoker %s (invoker=%s)",
            type(self).__name__,
            interaction.user.id,
            self.invoker_id,
        )
        await deny_view_interaction(interaction, VIEW_NOT_INVOKER_MSG)
        return False
