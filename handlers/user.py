from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, CommandStart
from aiogram.types import Message

from config import Config
from database import Database
from services.i18n import t
from services.moderation import ModerationSendError, send_submission_to_moderation


logger = logging.getLogger(__name__)
router = Router(name="user")

URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


class SubmissionChatFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config) -> bool:
        return message.chat.id != config.admin_chat_id


@router.message(SubmissionChatFilter(), CommandStart())
async def start(message: Message) -> None:
    await message.answer(t("user.start"))


@router.message(SubmissionChatFilter(), F.text)
async def submit_text(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if not message.text:
        await message.answer(t("user.unsupported"))
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
        await message.answer(t("user.unsupported"))
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
        await message.answer(t("user.unsupported"))
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
        await message.answer(t("user.unsupported"))
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
    await message.answer(t("user.unsupported"))


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
        await message.answer(t("user.unsupported"))
        return

    cooldown_remaining = await _submission_cooldown_remaining(db, message.from_user.id, config)
    if cooldown_remaining > 0:
        await message.answer(t("user.cooldown", seconds=cooldown_remaining))
        logger.info(
            "Rejected submission from user %s due to cooldown: %s seconds remaining",
            message.from_user.id,
            cooldown_remaining,
        )
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
        await message.answer(t("user.submission_error"))
        return

    logger.info(
        "Created submission %s from user %s as %s",
        submission_id,
        message.from_user.id,
        message_type,
    )

    try:
        await send_submission_to_moderation(bot, config, db, submission_id)
    except ModerationSendError:
        logger.exception("Failed to send submission %s to admin chat", submission_id)
        await message.answer(t("user.submission_error"))
        return

    await message.answer(t("user.thank_you"))


def _contains_link(message: Message) -> bool:
    text = message.text or ""
    if URL_PATTERN.search(text):
        return True

    for entity in message.entities or []:
        entity_type = getattr(entity.type, "value", entity.type)
        if entity_type in {"url", "text_link"}:
            return True

    return False


async def _submission_cooldown_remaining(db: Database, user_id: int, config: Config) -> int:
    if user_id in config.admin_user_ids:
        return 0

    cooldown_seconds = config.submission_cooldown_seconds
    if cooldown_seconds <= 0:
        return 0

    latest_submission = await db.get_latest_user_submission(user_id)
    if latest_submission is None:
        return 0

    created_at = _parse_utc_datetime(str(latest_submission["created_at"]))
    elapsed_seconds = int((datetime.now(timezone.utc) - created_at).total_seconds())
    remaining_seconds = cooldown_seconds - elapsed_seconds
    return max(0, remaining_seconds)


def _parse_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)
