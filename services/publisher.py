from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import Config
from services.post_footer import append_community_footer, format_post_html


logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


class PublishingError(RuntimeError):
    """Raised when a submission cannot be published to Telegram."""


async def publish_submission(bot: Bot, config: Config, submission: dict[str, Any]) -> None:
    message_type = submission["message_type"]
    draft_text = submission.get("draft_text") or submission.get("original_text") or ""
    parse_mode: str | None = None
    force_single_media_message = False
    if submission.get("source_type"):
        draft_text = append_community_footer(draft_text)
        draft_text = format_post_html(draft_text)
        parse_mode = "HTML"
        force_single_media_message = True

    try:
        if message_type in {"text", "link"}:
            await _send_text(bot, config.publish_chat_id, draft_text, parse_mode=parse_mode)
        elif message_type == "photo":
            await _send_media_with_optional_text(
                bot,
                bot.send_photo,
                config.publish_chat_id,
                submission,
                draft_text,
                parse_mode=parse_mode,
                force_single_message=force_single_media_message,
            )
        elif message_type == "video":
            await _send_media_with_optional_text(
                bot,
                bot.send_video,
                config.publish_chat_id,
                submission,
                draft_text,
                parse_mode=parse_mode,
                force_single_message=force_single_media_message,
            )
        elif message_type == "document":
            await _send_media_with_optional_text(
                bot,
                bot.send_document,
                config.publish_chat_id,
                submission,
                draft_text,
                parse_mode=parse_mode,
                force_single_message=force_single_media_message,
            )
        else:
            raise PublishingError(f"Unsupported submission type: {message_type}")
    except TelegramAPIError as exc:
        raise PublishingError("Telegram API rejected the publish request") from exc

    logger.info("Published submission %s to chat %s", submission["id"], config.publish_chat_id)


async def _send_text(bot: Bot, chat_id: int, text: str, *, parse_mode: str | None = None) -> None:
    text = text.strip()
    if not text:
        raise PublishingError("Text submission has no draft text")

    if parse_mode is not None:
        if _telegram_visible_length(text, parse_mode=parse_mode) > TELEGRAM_TEXT_LIMIT:
            raise PublishingError("Text submission exceeds Telegram single-message limit")
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        return

    for chunk in split_text(text, TELEGRAM_TEXT_LIMIT):
        await bot.send_message(chat_id=chat_id, text=chunk)


async def _send_media_with_optional_text(
    bot: Bot,
    send_method,
    chat_id: int,
    submission: dict[str, Any],
    text: str,
    parse_mode: str | None = None,
    force_single_message: bool = False,
) -> None:
    media_value = submission.get("file_id") or submission.get("media_url")
    if not media_value:
        raise PublishingError("Media submission has no file_id or media_url")

    uses_external_media = not submission.get("file_id") and bool(submission.get("media_url"))
    text = text.strip()
    media_argument = _media_argument_name(submission["message_type"])

    try:
        if text and _telegram_visible_length(text, parse_mode=parse_mode) <= TELEGRAM_CAPTION_LIMIT:
            await send_method(chat_id=chat_id, **{media_argument: media_value}, caption=text, parse_mode=parse_mode)
            return

        if force_single_message:
            raise PublishingError("Media caption exceeds Telegram single-message limit")

        await send_method(chat_id=chat_id, **{media_argument: media_value})
    except TelegramAPIError:
        if not uses_external_media:
            raise

        logger.exception(
            "Failed to publish external media for submission %s. Falling back to text-only publish.",
            submission["id"],
        )
        await _send_text(bot, chat_id, text, parse_mode=parse_mode)
        return

    if text:
        for chunk in split_text(text, TELEGRAM_TEXT_LIMIT):
            await bot.send_message(chat_id=chat_id, text=chunk)


def _media_argument_name(message_type: str) -> str:
    if message_type == "photo":
        return "photo"
    if message_type == "video":
        return "video"
    if message_type == "document":
        return "document"
    raise PublishingError(f"Unsupported media type: {message_type}")


def split_text(text: str, limit: int) -> list[str]:
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


def _telegram_visible_length(text: str, *, parse_mode: str | None) -> int:
    if parse_mode != "HTML":
        return len(text)

    without_tags = re.sub(r"<[^>]+>", "", text)
    return len(unescape(without_tags))
