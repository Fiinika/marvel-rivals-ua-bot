from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import ConfigError, load_config
from database import Database
from handlers import admin, user


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

    try:
        await dispatcher.start_polling(
            bot,
            config=config,
            db=database,
            allowed_updates=["message", "channel_post", "callback_query"],
        )
    finally:
        await bot.session.close()


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
