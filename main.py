from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
)

from config import Config, ConfigError, load_config
from database import Database
from discord_moderation import start_discord_moderation
from handlers import admin, moderation, user
from services.collectors.registry import run_all_collectors
from services.collectors.wiki_facts.collector import start_wiki_facts_scheduler
from services.db_backup import start_db_backup_scheduler
from services.digests.fanart import start_fanart_digest_scheduler
from services.i18n import t


logger = logging.getLogger(__name__)


async def run() -> None:
    config = load_config()
    database = Database(config.database_path)
    await database.init()

    bot = Bot(token=config.bot_token)
    dispatcher = Dispatcher()
    # Order matters: admin (admin-chat/private) first, then the optional moderation
    # router (scoped to the allowlisted group chats), then the private-only user
    # submission router. The moderation router is only registered when enabled and
    # at least one chat id is configured, so behaviour is unchanged when off.
    routers = [admin.router]
    if config.enable_telegram_moderation and config.telegram_moderation_chat_ids:
        routers.append(moderation.router)
        logger.info("Telegram chat moderation enabled for chats: %s", sorted(config.telegram_moderation_chat_ids))
    else:
        logger.info("Telegram chat moderation is disabled.")
    routers.append(user.router)
    dispatcher.include_routers(*routers)

    logger.info("Starting Marvel Rivals UA submission bot")
    logger.info("Admin chat: %s, publish chat: %s", config.admin_chat_id, config.publish_chat_id)
    logger.info("Submission cooldown: %s seconds", config.submission_cooldown_seconds)

    await _configure_bot_commands(bot, config)

    news_scheduler_task = _start_news_scheduler_if_enabled(bot, config, database)
    # Optional, independent Discord moderation bot. Returns None when disabled or
    # misconfigured; any Discord failure is contained and never stops Telegram.
    discord_task = start_discord_moderation(config)
    backup_task = start_db_backup_scheduler(config)
    fanart_digest_task = start_fanart_digest_scheduler(bot, config, database)
    wiki_facts_task = start_wiki_facts_scheduler(bot, config, database)
    try:
        await dispatcher.start_polling(
            bot,
            config=config,
            db=database,
            allowed_updates=["message", "edited_message", "channel_post", "callback_query"],
        )
    finally:
        for background_task in (news_scheduler_task, discord_task, backup_task, fanart_digest_task, wiki_facts_task):
            if background_task is not None:
                background_task.cancel()
                with suppress(asyncio.CancelledError):
                    await background_task
        await bot.session.close()


_COMMAND_MENU_ATTEMPTS = 3
_COMMAND_MENU_RETRY_DELAY_SECONDS = 5


async def _configure_bot_commands(bot: Bot, config: Config) -> None:
    """Scope the command menu so the submission hint never shows in group chats.

    Retries a few times: a transient network blip right at startup would
    otherwise leave the menu unconfigured until the next restart. Any persistent
    failure is logged and never stops the bot.
    """
    last_error: Exception | None = None
    for attempt in range(1, _COMMAND_MENU_ATTEMPTS + 1):
        try:
            await _apply_command_menu(bot, config)
            return
        except TelegramAPIError as exc:
            last_error = exc
            if attempt < _COMMAND_MENU_ATTEMPTS:
                logger.info(
                    "Configuring the bot command menu failed (attempt %s/%s); retrying in %s s.",
                    attempt,
                    _COMMAND_MENU_ATTEMPTS,
                    _COMMAND_MENU_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(_COMMAND_MENU_RETRY_DELAY_SECONDS)
    logger.warning(
        "Could not configure the bot command menu (%s). The previous menu, stored "
        "server-side by Telegram, stays in effect.",
        last_error.__class__.__name__ if last_error is not None else "unknown error",
    )


async def _apply_command_menu(bot: Bot, config: Config) -> None:
    """One attempt at applying the scoped command menu.

    A default-scope command list (e.g. set once via BotFather) is shown in EVERY
    chat, including the moderated public group. Instead: private chats get /start,
    the default and group scopes are cleared, and each moderated chat additionally
    shows the moderation commands to its administrators only.
    """
    await bot.set_my_commands(
        [BotCommand(command="start", description=t("commands.start"))],
        scope=BotCommandScopeAllPrivateChats(),
    )
    # Clear the default scope so group chats inherit no command menu at all.
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    logger.info("Bot command menu configured: /start in private chats only.")

    await _apply_admin_chat_commands(bot, config)

    if not (config.enable_telegram_moderation and config.telegram_moderation_chat_ids):
        # Per-chat command menus persist server-side across restarts. When the
        # listed chats are no longer moderated, clear their menus so members do
        # not keep seeing dead /report and /rules entries.
        for chat_id in sorted(config.telegram_moderation_chat_ids):
            try:
                await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
                await bot.delete_my_commands(scope=BotCommandScopeChatAdministrators(chat_id=chat_id))
            except TelegramAPIError:
                logger.info("Could not clear the command menu for chat %s", chat_id)
        return

    # Ordinary members of a moderated chat see /report and /rules; its administrators
    # see the full moderation list (the admins scope fully overrides the chat scope).
    member_commands = [
        BotCommand(command=name, description=t(f"commands.{name}"))
        for name in ("report", "rules")
    ]
    moderator_commands = [
        BotCommand(command=name, description=t(f"commands.{name}"))
        for name in (
            "modhelp",
            "del",
            "mute",
            "unmute",
            "ban",
            "unban",
            "kick",
            "warn",
            "warnings",
            "clearwarnings",
            "report",
            "rules",
        )
    ]
    for chat_id in sorted(config.telegram_moderation_chat_ids):
        try:
            await bot.set_my_commands(member_commands, scope=BotCommandScopeChat(chat_id=chat_id))
            await bot.set_my_commands(
                moderator_commands,
                scope=BotCommandScopeChatAdministrators(chat_id=chat_id),
            )
        except TelegramAPIError:
            logger.info(
                "Could not set moderator commands for chat %s (is the bot a member there?)",
                chat_id,
            )


async def _apply_admin_chat_commands(bot: Bot, config: Config) -> None:
    """Make the content commands discoverable in the admin chat only.

    They are gated on ADMIN_USER_IDS at handler level and also work in a DM, but
    nothing advertised them anywhere, so an admin had to remember the exact
    spelling. Listing them under the admin chat's own scope keeps them out of
    every other chat's menu.
    """
    if config.admin_chat_id in config.telegram_moderation_chat_ids:
        # That chat's menu belongs to the moderation scopes below: overwriting its
        # chat scope here would advertise admin commands to ordinary members.
        logger.info("Admin chat %s is also moderated; leaving its command menu to moderation.", config.admin_chat_id)
        return

    admin_commands = [
        BotCommand(command=name, description=t(f"commands.{name}"))
        for name in ("fetch_news", "fanartdigest", "wikifact", "cleanup", "cancel")
    ]
    try:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=config.admin_chat_id))
    except TelegramAPIError:
        logger.info("Could not set the admin command menu for chat %s", config.admin_chat_id)
        return
    logger.info("Admin command menu configured for chat %s.", config.admin_chat_id)


def _start_news_scheduler_if_enabled(bot: Bot, config: Config, db: Database) -> asyncio.Task | None:
    if not config.gemini_api_key:
        logger.warning("GEMINI_API_KEY is missing. Automatic news collector scheduler is disabled.")
        return None

    if config.news_check_interval_minutes is None:
        logger.info("NEWS_CHECK_INTERVAL_MINUTES is empty or invalid. News collector scheduler is disabled.")
        return None

    # The model is named here because it is the one setting with no other visible
    # trace: a GEMINI_MODEL secret silently overrides the code default, and the
    # deploy script keeps an old value when the secret is removed, so the log is
    # the only way to tell which model is actually generating drafts.
    logger.info(
        "News collector scheduler enabled: every %s minutes, drafting with %s",
        config.news_check_interval_minutes,
        config.gemini_model,
    )
    return asyncio.create_task(_news_scheduler(bot, config, db), name="news-collector-scheduler")


async def _news_scheduler(bot: Bot, config: Config, db: Database) -> None:
    """Poll every source on a fixed cadence.

    The first run happens immediately rather than after one interval: a restart
    would otherwise leave the bot blind for a full interval, and a deploy is
    exactly when it is most likely to have missed something. The wait is measured
    from the START of each run, so a slow tick cannot make the schedule drift —
    sources publish on the hour, and a drifting poll turns a predictable lag into
    a random one.
    """
    interval_seconds = config.news_check_interval_minutes * 60
    while True:
        started_at = time.monotonic()
        try:
            stats_list = await run_all_collectors(config=config, db=db, bot=bot)
            total_found = sum(stats.found for stats in stats_list)
            total_duplicates = sum(stats.duplicates for stats in stats_list)
            total_new = sum(stats.new for stats in stats_list)
            total_sent = sum(stats.sent_to_moderation for stats in stats_list)
            total_failed = sum(stats.failed for stats in stats_list)
            logger.info(
                "News scheduler finished: sources=%s found=%s duplicates=%s new=%s sent=%s failed=%s",
                len(stats_list),
                total_found,
                total_duplicates,
                total_new,
                total_sent,
                total_failed,
            )
        except Exception:
            logger.exception("News scheduler failed")

        elapsed = time.monotonic() - started_at
        await asyncio.sleep(max(1.0, interval_seconds - elapsed))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        asyncio.run(run())
    except ConfigError as exc:
        logger.critical("Configuration error: %s", exc)
        raise SystemExit(1) from exc
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()
