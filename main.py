from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

from aiogram import Bot, Dispatcher

from config import Config, ConfigError, load_config
from database import Database
from discord_moderation import start_discord_moderation
from handlers import admin, user
from services.collectors.registry import run_all_collectors


logger = logging.getLogger(__name__)


async def run() -> None:
    config = load_config()
    database = Database(config.database_path)
    await database.init()

    bot = Bot(token=config.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_routers(admin.router, user.router)

    logger.info("Starting Marvel Rivals UA submission bot")
    logger.info("Admin chat: %s, publish chat: %s", config.admin_chat_id, config.publish_chat_id)
    logger.info("Submission cooldown: %s seconds", config.submission_cooldown_seconds)

    news_scheduler_task = _start_news_scheduler_if_enabled(bot, config, database)
    # Optional, independent Discord moderation bot. Returns None when disabled or
    # misconfigured; any Discord failure is contained and never stops Telegram.
    discord_task = start_discord_moderation(config)
    try:
        await dispatcher.start_polling(
            bot,
            config=config,
            db=database,
            allowed_updates=["message", "channel_post", "callback_query"],
        )
    finally:
        for background_task in (news_scheduler_task, discord_task):
            if background_task is not None:
                background_task.cancel()
                with suppress(asyncio.CancelledError):
                    await background_task
        await bot.session.close()


def _start_news_scheduler_if_enabled(bot: Bot, config: Config, db: Database) -> asyncio.Task | None:
    if not config.gemini_api_key:
        logger.warning("GEMINI_API_KEY is missing. Automatic news collector scheduler is disabled.")
        return None

    if config.news_check_interval_minutes is None:
        logger.info("NEWS_CHECK_INTERVAL_MINUTES is empty or invalid. News collector scheduler is disabled.")
        return None

    logger.info(
        "News collector scheduler enabled: every %s minutes",
        config.news_check_interval_minutes,
    )
    return asyncio.create_task(_news_scheduler(bot, config, db), name="news-collector-scheduler")


async def _news_scheduler(bot: Bot, config: Config, db: Database) -> None:
    interval_seconds = config.news_check_interval_minutes * 60
    while True:
        await asyncio.sleep(interval_seconds)
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
