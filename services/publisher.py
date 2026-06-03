from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LinkPreviewOptions
from aiohttp import ClientOSError

from config import Config
from services.post_footer import format_post_html
from services.telegram_retry import delay_between_telegram_sends, send_with_retries


logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
DISABLED_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


class PublishingError(RuntimeError):
    """Raised when a submission cannot be published to Telegram."""


async def publish_submission(bot: Bot, config: Config, submission: dict[str, Any]) -> None:
    parse_mode = "HTML"
    force_single_media_message = False
    parts = _submission_parts(submission)

    try:
        for index, part in enumerate(parts):
            message_type = str(part.get("message_type") or "text")
            part_submission = {**submission, **part, "id": submission["id"], "message_type": message_type}
            draft_text = _format_part_text(str(part.get("text") or ""), part_submission)

            if message_type in {"text", "link"}:
                await _send_text(bot, config.publish_chat_id, draft_text, parse_mode=parse_mode)
            elif message_type == "photo":
                await _send_media_with_optional_text(
                    bot,
                    bot.send_photo,
                    config.publish_chat_id,
                    part_submission,
                    draft_text,
                    parse_mode=parse_mode,
                    force_single_message=force_single_media_message,
                )
            elif message_type == "video":
                await _send_media_with_optional_text(
                    bot,
                    bot.send_video,
                    config.publish_chat_id,
                    part_submission,
                    draft_text,
                    parse_mode=parse_mode,
                    force_single_message=force_single_media_message,
                )
            elif message_type == "document":
                await _send_media_with_optional_text(
                    bot,
                    bot.send_document,
                    config.publish_chat_id,
                    part_submission,
                    draft_text,
                    parse_mode=parse_mode,
                    force_single_message=force_single_media_message,
                )
            else:
                raise PublishingError(f"Unsupported submission type: {message_type}")
            if index < len(parts) - 1:
                await delay_between_telegram_sends()
    except (TelegramAPIError, TimeoutError, ClientOSError) as exc:
        raise PublishingError("Telegram API rejected the publish request") from exc

    logger.info("Published submission %s to chat %s", submission["id"], config.publish_chat_id)


def _submission_parts(submission: dict[str, Any]) -> list[dict[str, Any]]:
    parts = submission.get("parts")
    if isinstance(parts, list) and parts:
        return [dict(part) for part in parts]

    return [
        {
            "message_type": submission["message_type"],
            "text": submission.get("draft_text") or submission.get("original_text") or "",
            "file_id": submission.get("file_id"),
            "media_url": submission.get("media_url"),
        }
    ]


def _format_part_text(text: str, submission: dict[str, Any]) -> str:
    return format_post_html(
        text,
        source_url=str(submission.get("source_url") or ""),
        allow_source_link=_is_official_source_submission(submission),
        include_community_footer=_is_official_source_submission(submission),
    )


def _is_official_source_submission(submission: dict[str, Any]) -> bool:
    return str(submission.get("source_type") or "") == "official_marvel_rivals"


async def _send_text(bot: Bot, chat_id: int, text: str, *, parse_mode: str | None = None) -> None:
    text = text.strip()
    if not text:
        raise PublishingError("Text submission has no draft text")

    if parse_mode is not None:
        if _telegram_visible_length(text, parse_mode=parse_mode) > TELEGRAM_TEXT_LIMIT:
            raise PublishingError("Text submission exceeds Telegram single-message limit")
        await send_with_retries(
            bot.send_message,
            label="publish text message",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
        return

    chunks = split_text(text, TELEGRAM_TEXT_LIMIT)
    for index, chunk in enumerate(chunks):
        await send_with_retries(
            bot.send_message,
            label="publish text chunk",
            chat_id=chat_id,
            text=chunk,
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
        if index < len(chunks) - 1:
            await delay_between_telegram_sends()


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
            await send_with_retries(
                send_method,
                label=f"publish {submission['message_type']} with caption",
                chat_id=chat_id,
                **{media_argument: media_value},
                caption=text,
                parse_mode=parse_mode,
            )
            return

        if force_single_message:
            raise PublishingError("Media caption exceeds Telegram single-message limit")

        await send_with_retries(
            send_method,
            label=f"publish {submission['message_type']} without long caption",
            chat_id=chat_id,
            **{media_argument: media_value},
            caption=None,
            parse_mode=parse_mode,
        )
    except (TelegramAPIError, TimeoutError, ClientOSError):
        if not uses_external_media:
            raise

        logger.exception(
            "Failed to publish external media for submission %s. Falling back to text-only publish.",
            submission["id"],
        )
        await _send_text(bot, chat_id, text, parse_mode=parse_mode)
        return

    if text:
        await delay_between_telegram_sends()
        await _send_text(bot, chat_id, text, parse_mode=parse_mode)


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
