from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import Config
from database import Database
from keyboards import moderation_keyboard
from services.formatter import format_admin_preview
from services.i18n import t
from services.publisher import TELEGRAM_CAPTION_LIMIT, TELEGRAM_TEXT_LIMIT, split_text


logger = logging.getLogger(__name__)


class ModerationSendError(RuntimeError):
    """Raised when a submission cannot be sent to the moderation queue."""


async def send_submission_to_moderation(bot: Bot, config: Config, db: Database, submission_id: int) -> None:
    submission = await db.get_submission(submission_id)
    if submission is None:
        raise ModerationSendError(f"Submission {submission_id} was not found")

    await _send_optional_ai_media_preview(bot, config, db, submission)

    submission = await db.get_submission(submission_id) or submission
    try:
        admin_message = await bot.send_message(
            chat_id=config.admin_chat_id,
            text=format_admin_preview(submission),
            reply_markup=moderation_keyboard(submission_id),
            parse_mode="HTML",
        )
    except TelegramAPIError as exc:
        raise ModerationSendError("Failed to send admin moderation preview") from exc

    await db.set_admin_message_id(submission_id, admin_message.message_id)
    logger.info("Sent submission %s to admin moderation queue as message %s", submission_id, admin_message.message_id)


async def _send_optional_ai_media_preview(
    bot: Bot,
    config: Config,
    db: Database,
    submission: dict,
) -> None:
    media_url = submission.get("media_url")
    media_type = submission.get("media_type")
    file_id = submission.get("file_id")
    if file_id or media_type != "photo" or not media_url:
        return

    submission_id = int(submission["id"])
    draft_text = (submission.get("draft_text") or "").strip()
    caption = (
        draft_text
        if 0 < len(draft_text) <= TELEGRAM_CAPTION_LIMIT
        else t("admin.media_preview.photo_caption", submission_id=submission_id)
    )

    try:
        media_message = await bot.send_photo(
            chat_id=config.admin_chat_id,
            photo=media_url,
            caption=caption,
        )
    except TelegramAPIError:
        logger.exception("Failed to send media URL preview for submission %s. Falling back to text-only moderation.", submission_id)
        return

    await db.set_admin_media_message_id(submission_id, media_message.message_id)
    logger.info("Sent media URL preview for submission %s as message %s", submission_id, media_message.message_id)

    if len(draft_text) <= TELEGRAM_CAPTION_LIMIT:
        return

    long_draft_text = f"{t('admin.media_preview.draft_text_prefix', submission_id=submission_id)}\n\n{draft_text}"
    for chunk in split_text(long_draft_text, TELEGRAM_TEXT_LIMIT):
        try:
            await bot.send_message(chat_id=config.admin_chat_id, text=chunk)
        except TelegramAPIError:
            logger.info("Could not send long draft text companion message for submission %s", submission_id)
            return
