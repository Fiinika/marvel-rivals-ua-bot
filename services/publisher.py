from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import Config


logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


class PublishingError(RuntimeError):
    """Raised when a submission cannot be published to Telegram."""


async def publish_submission(bot: Bot, config: Config, submission: dict[str, Any]) -> None:
    message_type = submission["message_type"]
    draft_text = submission.get("draft_text") or submission.get("original_text") or ""

    try:
        if message_type in {"text", "link"}:
            await _send_text(bot, config.publish_chat_id, draft_text)
        elif message_type == "photo":
            await _send_media_with_optional_text(bot, bot.send_photo, config.publish_chat_id, submission, draft_text)
        elif message_type == "video":
            await _send_media_with_optional_text(bot, bot.send_video, config.publish_chat_id, submission, draft_text)
        elif message_type == "document":
            await _send_media_with_optional_text(bot, bot.send_document, config.publish_chat_id, submission, draft_text)
        else:
            raise PublishingError(f"Unsupported submission type: {message_type}")
    except TelegramAPIError as exc:
        raise PublishingError("Telegram API rejected the publish request") from exc

    logger.info("Published submission %s to chat %s", submission["id"], config.publish_chat_id)


async def _send_text(bot: Bot, chat_id: int, text: str) -> None:
    text = text.strip()
    if not text:
        raise PublishingError("Text submission has no draft text")

    for chunk in _split_text(text, TELEGRAM_TEXT_LIMIT):
        await bot.send_message(chat_id=chat_id, text=chunk)


async def _send_media_with_optional_text(
    bot: Bot,
    send_method,
    chat_id: int,
    submission: dict[str, Any],
    text: str,
) -> None:
    file_id = submission.get("file_id")
    if not file_id:
        raise PublishingError("Media submission has no file_id")

    text = text.strip()
    media_argument = _media_argument_name(submission["message_type"])

    if text and len(text) <= TELEGRAM_CAPTION_LIMIT:
        await send_method(chat_id=chat_id, **{media_argument: file_id}, caption=text)
        return

    await send_method(chat_id=chat_id, **{media_argument: file_id})
    if text:
        for chunk in _split_text(text, TELEGRAM_TEXT_LIMIT):
            await bot.send_message(chat_id=chat_id, text=chunk)


def _media_argument_name(message_type: str) -> str:
    if message_type == "photo":
        return "photo"
    if message_type == "video":
        return "video"
    if message_type == "document":
        return "document"
    raise PublishingError(f"Unsupported media type: {message_type}")


def _split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks
