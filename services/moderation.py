from __future__ import annotations

import logging
import re
from html import unescape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import Config
from database import Database
from keyboards import moderation_keyboard
from services.formatter import format_admin_preview
from services.i18n import t
from services.post_footer import format_post_html
from services.publisher import DISABLED_LINK_PREVIEW, TELEGRAM_CAPTION_LIMIT


logger = logging.getLogger(__name__)


class ModerationSendError(RuntimeError):
    """Raised when a submission cannot be sent to the moderation queue."""


async def send_submission_to_moderation(bot: Bot, config: Config, db: Database, submission_id: int) -> None:
    submission = await db.get_submission(submission_id)
    if submission is None:
        raise ModerationSendError(f"Submission {submission_id} was not found")

    await _send_submission_parts_to_moderation(bot, config, db, submission)

    submission = await db.get_submission(submission_id) or submission
    try:
        admin_message = await bot.send_message(
            chat_id=config.admin_chat_id,
            text=format_admin_preview(submission),
            reply_markup=moderation_keyboard(submission_id),
            parse_mode="HTML",
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramAPIError as exc:
        raise ModerationSendError("Failed to send admin moderation preview") from exc

    await db.set_admin_message_id(submission_id, admin_message.message_id)
    logger.info("Sent submission %s to admin moderation queue as message %s", submission_id, admin_message.message_id)


async def _send_submission_parts_to_moderation(
    bot: Bot,
    config: Config,
    db: Database,
    submission: dict,
) -> None:
    for part in submission.get("parts", []):
        part_index = int(part["part_index"])
        try:
            message = await _send_part_message(bot, config.admin_chat_id, part)
        except TelegramAPIError as exc:
            raise ModerationSendError(
                f"Failed to send submission {submission['id']} part {part_index} to moderation"
            ) from exc

        await db.set_submission_part_admin_message_id(int(submission["id"]), part_index, message.message_id)
        if _has_media(part) and part_index == 1:
            await db.set_admin_media_message_id(int(submission["id"]), message.message_id)

        logger.info(
            "Sent submission %s part %s to admin moderation queue as message %s",
            submission["id"],
            part_index,
            message.message_id,
        )


async def _send_part_message(bot: Bot, chat_id: int, part: dict):
    message_type = str(part.get("message_type") or "text")
    text = _format_part_for_moderation(str(part.get("text") or ""))
    media_value = part.get("file_id") or part.get("media_url")

    if message_type == "photo" and media_value:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=media_value,
            caption=_caption_or_none(text),
            parse_mode="HTML",
        )
    if message_type == "video" and media_value:
        return await bot.send_video(
            chat_id=chat_id,
            video=media_value,
            caption=_caption_or_none(text),
            parse_mode="HTML",
        )
    if message_type == "document" and media_value:
        return await bot.send_document(
            chat_id=chat_id,
            document=media_value,
            caption=_caption_or_none(text),
            parse_mode="HTML",
        )

    return await bot.send_message(
        chat_id=chat_id,
        text=text or t("formatter.empty"),
        parse_mode="HTML",
        link_preview_options=DISABLED_LINK_PREVIEW,
    )


def _caption_or_none(text: str) -> str | None:
    if not text:
        return None

    if _html_visible_length(text) <= TELEGRAM_CAPTION_LIMIT:
        return text

    return format_post_html("")


def _format_part_for_moderation(text: str) -> str:
    return format_post_html(text)


def _html_visible_length(text: str) -> int:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return len(unescape(without_tags))


def _has_media(part: dict) -> bool:
    return str(part.get("message_type") or "") in {"photo", "video", "document"} and bool(
        part.get("file_id") or part.get("media_url")
    )
