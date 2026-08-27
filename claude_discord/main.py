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
from .claude.base_runner import BaseRunner
from .claude.runner import ClaudeRunner
from .cogs.claude_chat import ClaudeChatCog
from .database.ask_repo import PendingAskRepository
from .database.lounge_repo import LoungeRepository
from .database.models import init_db
from .database.repository import SessionRepository
from .database.resume_repo import PendingResumeRepository
from .database.settings_repo import SettingsRepository
from .database.usage_repo import UsageRepository
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
        "runner_backend": os.getenv("RUNNER_BACKEND", "claude"),
        "claude_command": os.getenv("CLAUDE_COMMAND", "claude"),
        "claude_model": os.getenv("CLAUDE_MODEL", "sonnet"),
        "claude_permission_mode": os.getenv("CLAUDE_PERMISSION_MODE", "acceptEdits"),
        "claude_working_dir": os.getenv("CLAUDE_WORKING_DIR", ""),
        "max_concurrent": os.getenv("MAX_CONCURRENT_SESSIONS", "3"),
        "timeout": os.getenv("SESSION_TIMEOUT_SECONDS", "300"),
        "owner_id": os.getenv("DISCORD_OWNER_ID", ""),
        "allowed_user_ids": os.getenv("DISCORD_ALLOWED_USER_IDS", ""),
        "allowed_skills": os.getenv("ALLOWED_SKILLS", ""),
        "coordination_channel_id": os.getenv("COORDINATION_CHANNEL_ID", ""),
        "append_system_prompt": os.getenv("APPEND_SYSTEM_PROMPT", ""),
        # A5: comma-separated Claude tool allowlist (e.g. "Read,Grep,Bash").
        # Unset (default) => None => ClaudeRunner passes no --allowedTools,
        # identical to current behaviour.
        "claude_allowed_tools": os.getenv("CLAUDE_ALLOWED_TOOLS", ""),
    }


def create_runner(config: dict[str, str]) -> BaseRunner:
    """Create a runner instance based on RUNNER_BACKEND env var.

    Supported backends:
      - "claude" (default): Claude Code CLI (claude -p --output-format stream-json)
      - Future: "codex", "api", etc.
    """
    backend = config.get("runner_backend", "claude")

    if backend == "claude":
        # A5: CLAUDE_ALLOWED_TOOLS unset => allowed_tools_str == "" => None,
        # i.e. ClaudeRunner behaves exactly as before (no --allowedTools).
        allowed_tools_str = config.get("claude_allowed_tools", "")
        allowed_tools = (
            [t.strip() for t in allowed_tools_str.split(",") if t.strip()]
            if allowed_tools_str
            else None
        )
        return ClaudeRunner(
            command=config["claude_command"],
            model=config["claude_model"],
            permission_mode=config["claude_permission_mode"],
            working_dir=config["claude_working_dir"] or None,
            timeout_seconds=int(config["timeout"]),
            append_system_prompt=config["append_system_prompt"] or None,
            allowed_tools=allowed_tools,
        )

    raise ValueError(
        f"Unknown RUNNER_BACKEND: {backend!r}. "
        f"Supported: 'claude'. "
        f"Future: 'codex', 'api'."
    )


def _resolve_data_dir() -> Path:
    """Resolve the data directory for session/task/notification DBs (A3).

    ``CCDB_DATA_DIR`` unset (default) => ``Path("data")``, identical to the
    previous hardcoded behaviour. Setting it lets multiple bot instances
    (e.g. bot1/bot2 sharing one host) keep separate session ledgers.
    Extracted to a standalone function so it is testable without running
    the full bot startup.
    """
    return Path(os.getenv("CCDB_DATA_DIR") or "data")


def _build_claude_chat_cog(
    bot: ClaudeDiscordBot,
    repo: SessionRepository,
    runner: ClaudeRunner,
    max_concurrent: int,
    allowed_user_ids: set[int] | None,
    ask_repo: PendingAskRepository,
    lounge_repo: LoungeRepository,
    resume_repo: PendingResumeRepository,
    settings_repo: SettingsRepository,
    usage_repo: UsageRepository,
) -> ClaudeChatCog:
    """Build ClaudeChatCog with the full repo set (A2).

    setup_bridge()-based consumers already get resume_repo/settings_repo/
    usage_repo wired in (see setup.py's ``setup_bridge()``). The direct
    main.py launch path used to omit them, which left restart-resume
    (claude_chat.py ``ClaudeChatCog.cog_unload``/``on_ready``) silently
    disabled, ``/model`` global overrides ignored, and usage tracking off —
    all with no error, since every one of these repos is optional (``None``
    is a valid, quietly-degraded value). Extracted to a standalone function
    so the wiring is testable without needing a running Discord bot.
    """
    return ClaudeChatCog(
        bot=bot,
        repo=repo,
        runner=runner,
        max_concurrent=max_concurrent,
        allowed_user_ids=allowed_user_ids,
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
        settings_repo=settings_repo,
        usage_repo=usage_repo,
    )


async def main() -> None:
    """Start the bot."""
    setup_logging()
    config = load_config()

    # Initialize database
    data_dir = _resolve_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "sessions.db")
    await init_db(db_path)

    # Create components
    repo = SessionRepository(db_path)
    ask_repo = PendingAskRepository(db_path)
    lounge_repo = LoungeRepository(db_path)
    # A2: wire the same repos setup_bridge()/setup_bridge()-based consumers get,
    # so the direct main.py launch path also supports restart-resume
    # (claude_chat.py ClaudeChatCog.cog_unload/on_ready), dynamic model
    # settings, and usage tracking instead of silently no-op'ing on None.
    resume_repo = PendingResumeRepository(db_path)
    settings_repo = SettingsRepository(db_path)
    usage_repo = UsageRepository(db_path)
    await usage_repo.ensure_schema()
    runner = create_runner(config)

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

    # Build allowed_user_ids early (used by both ClaudeChatCog and SkillCommandCog)
    allowed_user_ids_str = config.get("allowed_user_ids", "")
    if allowed_user_ids_str:
        allowed_user_ids = {
            int(uid.strip()) for uid in allowed_user_ids_str.split(",") if uid.strip()
        }
    elif config["owner_id"]:
        allowed_user_ids = {int(config["owner_id"])}
    else:
        allowed_user_ids = None

    # Build allowed_skills (Bot別スキル権限フィルタ)
    allowed_skills_str = config.get("allowed_skills", "")
    if allowed_skills_str:
        allowed_skills: set[str] | None = {
            s.strip() for s in allowed_skills_str.split(",") if s.strip()
        }
        logger.info("Allowed skills configured: %s", ", ".join(sorted(allowed_skills)))
    else:
        allowed_skills = None  # 全スキル許可

    # Register cog
    cog = _build_claude_chat_cog(
        bot=bot,
        repo=repo,
        runner=runner,
        max_concurrent=int(config["max_concurrent"]),
        allowed_user_ids=allowed_user_ids,
        ask_repo=ask_repo,
        lounge_repo=lounge_repo,
        resume_repo=resume_repo,
        settings_repo=settings_repo,
        usage_repo=usage_repo,
    )

    # RepoViewerCog — /recent slash command
    from .cogs.repo_viewer import RepoViewerCog

    repo_viewer_cog = RepoViewerCog(bot)

    # SkillCommandCog — /skill slash command (skills from ~/.claude/skills/)
    from .cogs.skill_command import SkillCommandCog
    from .cogs.session_manage import SessionManageCog

    channel_id_int = int(config["channel_id"])

    skill_cog = SkillCommandCog(
        bot,
        repo=repo,
        runner=runner,
        claude_channel_id=channel_id_int,
        allowed_user_ids=allowed_user_ids,
        allowed_skills=allowed_skills,
    )

    # settings_repo already created above (shared with ClaudeChatCog — A2).
    session_manage_cog = SessionManageCog(
        bot,
        repo=repo,
        runner=runner,
        settings_repo=settings_repo,
    )

    # API server (optional — enables push notifications, lounge, etc.)
    api_server = None
    api_port_str = os.getenv("API_PORT", "8080")
    try:
        from .database.notification_repo import NotificationRepository
        from .ext.api_server import ApiServer

        notif_db_path = str(data_dir / "notifications.db")
        notif_repo = NotificationRepository(notif_db_path)
        await notif_repo.init_db()
        api_server = ApiServer(
            repo=notif_repo,
            bot=bot,
            default_channel_id=int(config["channel_id"]),
            port=int(api_port_str),
            lounge_repo=lounge_repo,
            session_repo=repo,
        )
        runner.api_port = int(api_port_str)
    except Exception:
        logger.warning("API server setup failed — continuing without it", exc_info=True)

    # ChannelManageCog — /channel-create, /channel-list, etc.
    from .cogs.channel_manage import ChannelManageCog

    channel_manage_cog = ChannelManageCog(bot)

    # ShellExecCog — /exec (owner-only shell command execution)
    from .cogs.shell_exec import ShellExecCog

    shell_exec_cog = ShellExecCog(bot)

    # ShohinSearchCog — /shohin (商品マスター検索)
    from .cogs.shohin_search import ShohinSearchCog

    shohin_cog = ShohinSearchCog(bot)

    # ImageGenCommandCog — /lp /thumbnail /manga (画像生成バッチ)
    from .cogs.image_gen_command import ImageGenCommandCog

    image_gen_cog = ImageGenCommandCog(bot)

    # KwTriggerCog — /kw, /kw-opt (n8n KW WF Webhook trigger)
    from .cogs.kw_trigger import KwTriggerCog

    kw_cog = KwTriggerCog(bot)

    # ListingCommandCog — /shuppin (スマホ出品: JAN+原価→全モール出品)
    # SHUPPIN_ENABLED=1 の場合のみ読み込む（デフォルト無効）
    listing_cog = None
    if os.getenv("SHUPPIN_ENABLED", "").strip() in ("1", "true", "yes"):
        from .cogs.listing_command import ListingCommandCog
        listing_cog = ListingCommandCog(bot)
        logger.info("ListingCommandCog enabled (SHUPPIN_ENABLED=1)")
    else:
        logger.info("ListingCommandCog disabled (set SHUPPIN_ENABLED=1 to enable)")

    # OrderCommandCog — /order (手動発注: JAN:cs → SS-07/SS-13書き込み)
    from .cogs.order_command import OrderCommandCog

    order_cog = OrderCommandCog(bot)

    # ZaikoCommandCog — /pro-kakidasi (GoQ在庫連携シート生成)
    from .cogs.zaiko_command import ZaikoCommandCog

    zaiko_cog = ZaikoCommandCog(bot)

    # GoqUploadCommandCog — /goq-upload (SS-08シート5〜11→GoQ一括CSVアップロード)
    from .cogs.goq_upload_command import GoqUploadCommandCog

    goq_upload_cog = GoqUploadCommandCog(bot)

    # CategoryCommandCog — /category-search (全7モールカテゴリ一括検索)
    from .cogs.category_command import CategoryCommandCog

    category_cog = CategoryCommandCog(bot)

    # HaibanCommandCog — /haiban (廃盤管理・原価変更・在庫チェック)
    from .cogs.haiban_command import HaibanCommandCog

    haiban_cog = HaibanCommandCog(bot)

    # ProfitCommandCog — /profit (モール固有SKU/JANから利益率算出)
    from .cogs.profit_command import ProfitCommandCog

    profit_cog = ProfitCommandCog(bot)

    # Claude Code（claude -p）課金対策（2026-06-15〜）:
    # フリーチャット(ClaudeChatCog)と /skill(SkillCommandCog) は claude -p を呼ぶため
    # 従量課金対象。デフォルト無効。再開する場合のみ .env で 1/true/yes を設定。
    claude_chat_enabled = os.getenv("CLAUDE_CHAT_ENABLED", "").strip() in (
        "1", "true", "yes"
    )
    skill_enabled = os.getenv("SKILL_ENABLED", "").strip() in ("1", "true", "yes")
    if claude_chat_enabled:
        logger.info("ClaudeChatCog enabled (CLAUDE_CHAT_ENABLED=1) — claude -p 課金対象")
    else:
        logger.info("ClaudeChatCog disabled (set CLAUDE_CHAT_ENABLED=1 to enable)")
    if skill_enabled:
        logger.info("SkillCommandCog enabled (SKILL_ENABLED=1) — claude -p 課金対象")
    else:
        logger.info("SkillCommandCog disabled (set SKILL_ENABLED=1 to enable)")

    async with bot:
        if claude_chat_enabled:
            await bot.add_cog(cog)
        await bot.add_cog(repo_viewer_cog)
        await bot.add_cog(channel_manage_cog)
        if skill_enabled:
            await bot.add_cog(skill_cog)
        await bot.add_cog(session_manage_cog)
        await bot.add_cog(shell_exec_cog)
        await bot.add_cog(shohin_cog)
        await bot.add_cog(image_gen_cog)
        await bot.add_cog(kw_cog)
        await bot.add_cog(order_cog)
        if listing_cog is not None:
            await bot.add_cog(listing_cog)
        await bot.add_cog(zaiko_cog)
        await bot.add_cog(goq_upload_cog)
        await bot.add_cog(category_cog)
        await bot.add_cog(haiban_cog)
        await bot.add_cog(profit_cog)

        # Cleanup old sessions on startup
        deleted = await repo.cleanup_old(days=30)
        if deleted:
            logger.info("Cleaned up %d old sessions", deleted)

        # Start API server if configured
        if api_server is not None:
            try:
                await api_server.start()
            except Exception:
                logger.warning("API server start failed", exc_info=True)

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
