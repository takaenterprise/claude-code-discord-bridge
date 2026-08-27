"""Configuration dataclass for Claude Code execution.

Bundles all parameters needed to execute Claude Code CLI and stream results
to a Discord thread. Using a dataclass instead of a long positional argument
list makes call sites more readable and extension safer (new fields can be
added without changing every caller).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from ..claude.runner import ClaudeRunner
from ..concurrency import SessionRegistry
from ..database.ask_repo import PendingAskRepository
from ..database.lounge_repo import LoungeRepository
from ..database.repository import SessionRepository
from ..database.usage_repo import UsageRepository
from ..discord_ui.status import StatusManager

if TYPE_CHECKING:
    from ..discord_ui.views import StopView
    from ..worktree import WorktreeManager


@dataclass
class RunConfig:
    """All parameters needed for a single Claude Code execution.

    Required fields:
        thread: Discord thread to post results to.
        runner: A fresh (cloned) ClaudeRunner instance.
        prompt: The user's message or skill invocation.

    Optional fields:
        session_id: Session ID to resume. None for new sessions.
        repo: Session repository for persisting thread-session mappings.
              Pass None for automated workflows without session persistence.
        status: StatusManager for emoji reactions on the user's message.
        registry: SessionRegistry for concurrency awareness. When provided,
                  the session is registered during execution and a concurrency
                  notice is prepended to the prompt.
        ask_repo: Repository for persisting AskUserQuestion state across restarts.
        lounge_repo: Repository for AI Lounge context injection.
        stop_view: StopView instance to bump after each major message, keeping
                   the Stop button at the bottom of the thread.
        worktree_manager: WorktreeManager for automatic session worktree cleanup.
                          When provided, the worktree for this thread is removed
                          (if clean) after the session ends.
    """

    thread: discord.Thread
    runner: ClaudeRunner
    prompt: str
    session_id: str | None = None
    repo: SessionRepository | None = None
    status: StatusManager | None = None
    registry: SessionRegistry | None = None
    ask_repo: PendingAskRepository | None = None
    lounge_repo: LoungeRepository | None = None
    stop_view: StopView | None = None
    worktree_manager: WorktreeManager | None = None
    # HTTPS URLs of image attachments to pass as stream-json url-type image blocks.
    # Claude Code CLI silently drops base64 image blocks; URL type is required.
    image_urls: list[str] | None = None
    # Usage tracking: who invoked this session and via which bot.
    usage_repo: UsageRepository | None = None
    discord_user_id: str | None = None
    discord_username: str | None = None
    bot_name: str | None = None

    # Prevent accidental field mutation — RunConfig is a value object.
    # Use dataclasses.replace() to create modified copies.
    def __post_init__(self) -> None:
        if not self.prompt and not self.image_urls:
            raise ValueError("RunConfig.prompt must not be empty")

    def with_prompt(self, prompt: str, session_id: str | None = None) -> RunConfig:
        """Return a new RunConfig with a different prompt (immutable copy).

        All other fields (including session_id) are carried over from self
        unchanged, unless ``session_id`` is given explicitly — in which case
        it replaces the copy's session_id. This is used to resume with the
        session_id that a just-finished run produced (e.g. after an
        AskUserQuestion round-trip), rather than silently falling back to
        the session_id the caller started with (which is often None for a
        brand-new session, causing a fresh session with zero context to be
        started instead of a resume).
        """
        from dataclasses import replace

        if session_id is not None:
            return replace(self, prompt=prompt, session_id=session_id)
        return replace(self, prompt=prompt)
