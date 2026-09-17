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
