"""Entry point for claude-code-discord-bridge bot."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from .bot import ClaudeDiscordBot
from .claude.runner import ClaudeRunner
from .cogs.claude_chat import ClaudeChatCog
from .database.ask_repo import PendingAskRepository
from .database.lounge_repo import LoungeRepository
from .database.models import init_db
from .database.notification_repo import NotificationRepository
from .database.repository import SessionRepository
from .utils.logger import setup_logging

logger = logging.getLogger(__name__)


def load_config() -> dict[str, str]:
    """Load and validate configuration from environment."""
    load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        logger.error("DISCORD_BOT_TOKEN is required")
        sys.exit(1)

    channel_id = os.getenv("DISCORD_CHANNEL_ID", "")
    if not channel_id:
        logger.error("DISCORD_CHANNEL_ID is required")
        sys.exit(1)

    return {
        "token": token,
        "channel_id": channel_id,
        "claude_command": os.getenv("CLAUDE_COMMAND", "claude"),
        "claude_model": os.getenv("CLAUDE_MODEL", "sonnet"),
        "claude_permission_mode": os.getenv("CLAUDE_PERMISSION_MODE", "acceptEdits"),
        "claude_working_dir": os.getenv("CLAUDE_WORKING_DIR", ""),
        "max_concurrent": os.getenv("MAX_CONCURRENT_SESSIONS", "3"),
        "timeout": os.getenv("SESSION_TIMEOUT_SECONDS", "300"),
        "owner_id": os.getenv("DISCORD_OWNER_ID", ""),
        "coordination_channel_id": os.getenv("COORDINATION_CHANNEL_ID", ""),
        "api_host": os.getenv("API_HOST", "0.0.0.0"),
        "api_port": os.getenv("API_PORT", "8080"),
        "api_secret": os.getenv("API_SECRET", ""),
    }


async def main() -> None:
    """Start the bot."""
    setup_logging()
    config = load_config()

    # Initialize database
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    db_path = str(data_dir / "sessions.db")
    await init_db(db_path)

    # Create components
    repo = SessionRepository(db_path)
    ask_repo = PendingAskRepository(db_path)
    lounge_repo = LoungeRepository(db_path)
    notification_repo = NotificationRepository(db_path)
    await notification_repo.init_db()
    runner = ClaudeRunner(
        command=config["claude_command"],
        model=config["claude_model"],
        permission_mode=config["claude_permission_mode"],
        working_dir=config["claude_working_dir"] or None,
        timeout_seconds=int(config["timeout"]),
    )

    owner_id = int(config["owner_id"]) if config["owner_id"] else None
    coordination_channel_id = (
        int(config["coordination_channel_id"]) if config["coordination_channel_id"] else None
    )
    bot = ClaudeDiscordBot(
        channel_id=int(config["channel_id"]),
        owner_id=owner_id,
        coordination_channel_id=coordination_channel_id,
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
        lounge_channel_id=coordination_channel_id,  # lounge uses the same channel
    )

    # Register cog
    cog = ClaudeChatCog(
        bot=bot,
        repo=repo,
        runner=runner,
        max_concurrent=int(config["max_concurrent"]),
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
    )

    # API server (serves /paste web form and REST endpoints)
    api_server = None
    try:
        from .ext.api_server import ApiServer

        api_server = ApiServer(
            repo=notification_repo,
            bot=bot,
            default_channel_id=int(config["channel_id"]),
            host=config["api_host"],
            port=int(config["api_port"]),
            api_secret=config["api_secret"] or None,
            lounge_repo=lounge_repo,
            lounge_channel_id=coordination_channel_id,
            session_repo=repo,
        )
        runner.api_port = int(config["api_port"])
    except ImportError:
        logger.info("aiohttp not installed — API server disabled")

    async with bot:
        await bot.add_cog(cog)

        # Cleanup old sessions on startup
        deleted = await repo.cleanup_old(days=30)
        if deleted:
            logger.info("Cleaned up %d old sessions", deleted)

        # Start API server
        if api_server is not None:
            await api_server.start()

        # Handle signals (add_signal_handler is not supported on Windows)
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))

        try:
            await bot.start(config["token"])
        finally:
            if api_server is not None:
                await api_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
