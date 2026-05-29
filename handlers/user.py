from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, CommandStart
from aiogram.types import Message

from config import Config
from database import Database
from keyboards import moderation_keyboard
from services.formatter import format_admin_preview


logger = logging.getLogger(__name__)
router = Router(name="user")

START_TEXT = (
    "Привіт! Надішли новину, посилання, фото, відео або документ для Marvel Rivals UA. "
    "Після модерації вона може бути опублікована в спільноті."
)
THANK_YOU_TEXT = "Дякуємо! Твою новину відправлено на модерацію."
UNSUPPORTED_TEXT = "Наразі я можу приймати текст, посилання, фото, відео або документи."
SUBMISSION_ERROR_TEXT = "Вибач, не вдалося відправити новину на модерацію. Спробуй пізніше."

URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


class SubmissionChatFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config) -> bool:
        return message.chat.id != config.admin_chat_id


@router.message(SubmissionChatFilter(), CommandStart())
async def start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(SubmissionChatFilter(), F.text)
async def submit_text(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if not message.text:
        await message.answer(UNSUPPORTED_TEXT)
        return

    message_type = "link" if _contains_link(message) else "text"
    await _create_and_send_submission(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type=message_type,
        original_text=message.text,
        file_id=None,
    )


@router.message(SubmissionChatFilter(), F.photo)
async def submit_photo(message: Message, bot: Bot, config: Config, db: Database) -> None:
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        await message.answer(UNSUPPORTED_TEXT)
        return

    await _create_and_send_submission(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="photo",
        original_text=message.caption,
        file_id=photo.file_id,
    )


@router.message(SubmissionChatFilter(), F.video)
async def submit_video(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.video is None:
        await message.answer(UNSUPPORTED_TEXT)
        return

    await _create_and_send_submission(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="video",
        original_text=message.caption,
        file_id=message.video.file_id,
    )


@router.message(SubmissionChatFilter(), F.document)
async def submit_document(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.document is None:
        await message.answer(UNSUPPORTED_TEXT)
        return

    await _create_and_send_submission(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="document",
        original_text=message.caption,
        file_id=message.document.file_id,
    )


@router.message(SubmissionChatFilter())
async def unsupported(message: Message) -> None:
    await message.answer(UNSUPPORTED_TEXT)


async def _create_and_send_submission(
    *,
    message: Message,
    bot: Bot,
    config: Config,
    db: Database,
    message_type: str,
    original_text: str | None,
    file_id: str | None,
) -> None:
    if message.from_user is None:
        await message.answer(UNSUPPORTED_TEXT)
        return

    submission_id = await db.create_submission(
        user_id=message.from_user.id,
        username=message.from_user.username,
        message_type=message_type,
        original_text=original_text,
        file_id=file_id,
    )
    submission = await db.get_submission(submission_id)
    if submission is None:
        logger.error("Submission %s was created but could not be loaded", submission_id)
        await message.answer(SUBMISSION_ERROR_TEXT)
        return

    logger.info(
        "Created submission %s from user %s as %s",
        submission_id,
        message.from_user.id,
        message_type,
    )

    admin_media_message_id = await _copy_media_context_to_admin(message, bot, config, submission_id, message_type)
    if admin_media_message_id is not None:
        await db.set_admin_media_message_id(submission_id, admin_media_message_id)
        submission = await db.get_submission(submission_id) or submission

    try:
        admin_message = await bot.send_message(
            chat_id=config.admin_chat_id,
            text=format_admin_preview(submission),
            reply_markup=moderation_keyboard(submission_id),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        logger.exception("Failed to send submission %s to admin chat", submission_id)
        await message.answer(SUBMISSION_ERROR_TEXT)
        return

    await db.set_admin_message_id(submission_id, admin_message.message_id)
    logger.info("Sent submission %s to admin chat as message %s", submission_id, admin_message.message_id)
    await message.answer(THANK_YOU_TEXT)


async def _copy_media_context_to_admin(
    message: Message,
    bot: Bot,
    config: Config,
    submission_id: int,
    message_type: str,
) -> int | None:
    if message_type not in {"photo", "video", "document"}:
        return None

    try:
        copied_message = await bot.copy_message(
            chat_id=config.admin_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramAPIError:
        logger.exception("Failed to copy media context for submission %s", submission_id)
        return None

    try:
        await bot.send_message(
            chat_id=config.admin_chat_id,
            text=f"Початкове медіа для заявки #{submission_id}.",
            reply_to_message_id=copied_message.message_id,
            allow_sending_without_reply=True,
        )
    except TelegramAPIError:
        logger.info("Failed to send initial media marker for submission %s", submission_id)

    return copied_message.message_id


def _contains_link(message: Message) -> bool:
    text = message.text or ""
    if URL_PATTERN.search(text):
        return True

    for entity in message.entities or []:
        entity_type = getattr(entity.type, "value", entity.type)
        if entity_type in {"url", "text_link"}:
            return True

    return False
