from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import CallbackQuery, Message

from config import Config
from database import STATUS_PENDING, Database
from keyboards import ModerationCallback, moderation_keyboard
from services.formatter import format_admin_preview
from services.i18n import t
from services.publisher import PublishingError, publish_submission


logger = logging.getLogger(__name__)
router = Router(name="admin")

_approve_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_edit_prompt_message_ids: dict[int, list[tuple[int, int]]] = {}


class AdminChatFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config) -> bool:
        return message.chat.id == config.admin_chat_id


class AdminCommandChatFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config) -> bool:
        if message.from_user is None or message.from_user.id not in config.admin_user_ids:
            return False

        return message.chat.id == config.admin_chat_id or _chat_type(message) == "private"


class AdminEditMessageFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config, db: Database) -> bool:
        if message.from_user is None or message.from_user.id not in config.admin_user_ids:
            return False

        if message.chat.id != config.admin_chat_id and _chat_type(message) != "private":
            return False

        state = await db.get_admin_edit_state(message.from_user.id)
        return state is not None


class AdminChannelEditPostFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config, db: Database) -> bool:
        if message.chat.id != config.admin_chat_id or _chat_type(message) != "channel":
            return False

        state = await db.get_latest_admin_edit_state()
        return state is not None


@router.callback_query(ModerationCallback.filter())
async def moderation_callback(
    callback: CallbackQuery,
    callback_data: ModerationCallback,
    bot: Bot,
    config: Config,
    db: Database,
) -> None:
    admin_id = callback.from_user.id
    if admin_id not in config.admin_user_ids:
        logger.warning("User %s tried to use admin action %s", admin_id, callback_data.action)
        await _answer_callback(callback, t("admin.alert.admin_only"), show_alert=True)
        return

    if callback_data.action == "approve":
        await _approve_submission(callback, callback_data.submission_id, bot, config, db)
    elif callback_data.action == "reject":
        await _reject_submission(callback, callback_data.submission_id, bot, config, db)
    elif callback_data.action == "edit":
        await _start_edit(callback, callback_data.submission_id, bot, config, db)
    else:
        logger.warning("Unknown moderation action: %s", callback_data.action)
        await _answer_callback(callback, t("admin.alert.unknown_action"), show_alert=True)


@router.message(AdminCommandChatFilter(), Command("cancel"))
async def cancel_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    assert message.from_user is not None
    state = await db.get_admin_edit_state(message.from_user.id)
    if state is None:
        await message.answer(t("admin.edit.no_active_edit"))
        return

    await db.clear_admin_edit_state(message.from_user.id)
    await _delete_edit_prompts(bot, message.from_user.id)
    logger.info("Admin %s cancelled editing submission %s", message.from_user.id, state["submission_id"])
    await message.answer(t("admin.edit.cancelled"))


@router.message(AdminEditMessageFilter(), CommandStart())
async def remind_active_edit(message: Message, db: Database) -> None:
    assert message.from_user is not None
    state = await db.get_admin_edit_state(message.from_user.id)
    if state is None:
        return

    await message.answer(t("admin.edit.active_reminder", submission_id=state["submission_id"]))


@router.message(AdminEditMessageFilter(), F.photo)
async def receive_admin_photo_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        return

    await _receive_admin_media_edit(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="photo",
        file_id=photo.file_id,
    )


@router.message(AdminEditMessageFilter(), F.video)
async def receive_admin_video_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.video is None:
        return

    await _receive_admin_media_edit(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="video",
        file_id=message.video.file_id,
    )


@router.message(AdminEditMessageFilter(), F.document)
async def receive_admin_document_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.document is None:
        return

    await _receive_admin_media_edit(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="document",
        file_id=message.document.file_id,
    )


@router.message(AdminEditMessageFilter(), F.text)
async def receive_admin_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    assert message.from_user is not None
    state = await db.get_admin_edit_state(message.from_user.id)
    if state is None:
        return

    submission_id = int(state["submission_id"])
    submission = await db.get_submission(submission_id)
    edit_applied = await _apply_edit(
        submission_id=submission_id,
        admin_id=message.from_user.id,
        new_text=message.text or "",
        bot=bot,
        config=config,
        db=db,
    )
    await _delete_edit_messages(
        bot,
        message.from_user.id,
        message.chat.id,
        message.message_id,
        protected_message_refs=_protected_message_refs(submission, config.admin_chat_id),
    )

    if edit_applied:
        logger.info("Draft for submission %s was updated from admin message", submission_id)
    else:
        await message.answer(t("admin.edit.cancelled_generic"))


@router.channel_post(AdminChannelEditPostFilter(), F.photo)
async def receive_channel_photo_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        return

    await _receive_channel_media_edit(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="photo",
        file_id=photo.file_id,
    )


@router.channel_post(AdminChannelEditPostFilter(), F.video)
async def receive_channel_video_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.video is None:
        return

    await _receive_channel_media_edit(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="video",
        file_id=message.video.file_id,
    )


@router.channel_post(AdminChannelEditPostFilter(), F.document)
async def receive_channel_document_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.document is None:
        return

    await _receive_channel_media_edit(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="document",
        file_id=message.document.file_id,
    )


@router.channel_post(AdminChannelEditPostFilter(), F.text)
async def receive_channel_admin_text_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if _is_channel_edit_prompt(message.text):
        return

    state = await db.get_latest_admin_edit_state()
    if state is None:
        return

    submission_id = int(state["submission_id"])
    admin_id = int(state["admin_id"])
    submission = await db.get_submission(submission_id)
    logger.info("Received channel edit text for submission %s from admin state %s", submission_id, admin_id)
    edit_applied = await _apply_edit(
        submission_id=submission_id,
        admin_id=admin_id,
        new_text=message.text or "",
        bot=bot,
        config=config,
        db=db,
    )
    if not edit_applied:
        return

    await _delete_edit_messages(
        bot,
        admin_id,
        message.chat.id,
        message.message_id,
        protected_message_refs=_protected_message_refs(submission, config.admin_chat_id),
    )


async def _receive_admin_media_edit(
    *,
    message: Message,
    bot: Bot,
    config: Config,
    db: Database,
    message_type: str,
    file_id: str,
) -> None:
    assert message.from_user is not None
    state = await db.get_admin_edit_state(message.from_user.id)
    if state is None:
        return

    submission_id = int(state["submission_id"])
    submission = await db.get_submission(submission_id)
    old_media_message_id = _admin_media_message_id(submission)
    edit_applied = await _apply_edit(
        submission_id=submission_id,
        admin_id=message.from_user.id,
        new_text=message.caption,
        new_message_type=message_type,
        new_file_id=file_id,
        new_admin_media_message_id=message.message_id,
        bot=bot,
        config=config,
        db=db,
    )
    await _delete_edit_messages(
        bot,
        message.from_user.id,
        message.chat.id,
        message.message_id,
        protected_message_refs=_protected_message_refs(submission, config.admin_chat_id),
        delete_edit_content=False,
    )

    if edit_applied:
        await _mark_media_replacement(
            bot=bot,
            config=config,
            submission_id=submission_id,
            old_media_message_id=old_media_message_id,
            new_media_chat_id=message.chat.id,
            new_media_message_id=message.message_id,
        )
        logger.info("Media for submission %s was updated from admin message", submission_id)
    else:
        await message.answer(t("admin.edit.cancelled_generic"))


async def _receive_channel_media_edit(
    *,
    message: Message,
    bot: Bot,
    config: Config,
    db: Database,
    message_type: str,
    file_id: str,
) -> None:
    state = await db.get_latest_admin_edit_state()
    if state is None:
        return

    submission_id = int(state["submission_id"])
    admin_id = int(state["admin_id"])
    submission = await db.get_submission(submission_id)
    old_media_message_id = _admin_media_message_id(submission)
    logger.info(
        "Received channel edit media for submission %s from admin state %s as %s",
        submission_id,
        admin_id,
        message_type,
    )
    edit_applied = await _apply_edit(
        submission_id=submission_id,
        admin_id=admin_id,
        new_text=message.caption,
        new_message_type=message_type,
        new_file_id=file_id,
        new_admin_media_message_id=message.message_id,
        bot=bot,
        config=config,
        db=db,
    )
    if not edit_applied:
        return

    await _delete_edit_messages(
        bot,
        admin_id,
        message.chat.id,
        message.message_id,
        protected_message_refs=_protected_message_refs(submission, config.admin_chat_id),
        delete_edit_content=False,
    )
    await _mark_media_replacement(
        bot=bot,
        config=config,
        submission_id=submission_id,
        old_media_message_id=old_media_message_id,
        new_media_chat_id=message.chat.id,
        new_media_message_id=message.message_id,
    )


async def _approve_submission(
    callback: CallbackQuery,
    submission_id: int,
    bot: Bot,
    config: Config,
    db: Database,
) -> None:
    async with _approve_locks[submission_id]:
        submission = await db.get_submission(submission_id)
        if submission is None:
            await _answer_callback(callback, t("admin.alert.submission_not_found"), show_alert=True)
            return

        if submission["status"] != STATUS_PENDING:
            await _answer_callback(callback, t("admin.alert.submission_already_processed"), show_alert=True)
            return

        try:
            await publish_submission(bot, config, submission)
        except PublishingError as exc:
            logger.exception("Failed to publish submission %s: %s", submission_id, exc)
            await _answer_callback(callback, t("admin.alert.publish_failed"), show_alert=True)
            return

        await db.mark_published(submission_id)
        updated_submission = await db.get_submission(submission_id)
        if updated_submission is not None:
            await _update_admin_preview(bot, config, updated_submission, keep_buttons=False)

        logger.info("Admin %s approved submission %s", callback.from_user.id, submission_id)
        await _answer_callback(callback, t("admin.alert.published"))


async def _reject_submission(
    callback: CallbackQuery,
    submission_id: int,
    bot: Bot,
    config: Config,
    db: Database,
) -> None:
    submission = await db.get_submission(submission_id)
    if submission is None:
        await _answer_callback(callback, t("admin.alert.submission_not_found"), show_alert=True)
        return

    if submission["status"] != STATUS_PENDING:
        await _answer_callback(callback, t("admin.alert.submission_already_processed"), show_alert=True)
        return

    await db.mark_rejected(submission_id)
    updated_submission = await db.get_submission(submission_id)
    if updated_submission is not None:
        await _update_admin_preview(bot, config, updated_submission, keep_buttons=False)

    logger.info("Admin %s rejected submission %s", callback.from_user.id, submission_id)
    await _answer_callback(callback, t("admin.alert.rejected"))


async def _start_edit(
    callback: CallbackQuery,
    submission_id: int,
    bot: Bot,
    config: Config,
    db: Database,
) -> None:
    submission = await db.get_submission(submission_id)
    if submission is None:
        await _answer_callback(callback, t("admin.alert.submission_not_found"), show_alert=True)
        return

    if submission["status"] != STATUS_PENDING:
        await _answer_callback(callback, t("admin.alert.submission_already_processed"), show_alert=True)
        return

    await db.set_admin_edit_state(callback.from_user.id, submission_id)
    logger.info("Admin %s started editing submission %s", callback.from_user.id, submission_id)

    prompt = t("admin.edit.prompt", submission_id=submission_id)

    if callback.message is not None:
        if _chat_type(callback.message) == "channel":
            await _answer_callback(callback)

            try:
                prompt_message = await bot.send_message(
                    chat_id=config.admin_chat_id,
                    text=t("admin.edit.channel_prompt", submission_id=submission_id),
                )
                _remember_edit_prompt(
                    callback.from_user.id,
                    config.admin_chat_id,
                    prompt_message.message_id,
                )
            except TelegramAPIError:
                logger.info(
                    "Could not send channel edit prompt for submission %s to admin chat %s",
                    submission_id,
                    config.admin_chat_id,
                )
                return

            return

        prompt_message = await callback.message.answer(prompt)
        _remember_edit_prompt(
            callback.from_user.id,
            prompt_message.chat.id,
            prompt_message.message_id,
        )
    await _answer_callback(callback)


async def _update_admin_preview(
    bot: Bot,
    config: Config,
    submission: dict,
    *,
    keep_buttons: bool,
) -> None:
    admin_message_id = submission.get("admin_message_id")
    if admin_message_id is None:
        logger.warning("Submission %s has no admin_message_id", submission["id"])
        return

    reply_markup = moderation_keyboard(submission["id"]) if keep_buttons else None
    try:
        await bot.edit_message_text(
            chat_id=config.admin_chat_id,
            message_id=admin_message_id,
            text=format_admin_preview(submission),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.exception("Failed to update admin preview for submission %s", submission["id"])
    except TelegramAPIError:
        logger.exception("Failed to update admin preview for submission %s", submission["id"])


async def _apply_edit(
    *,
    submission_id: int,
    admin_id: int,
    new_text: str | None,
    bot: Bot,
    config: Config,
    db: Database,
    new_message_type: str | None = None,
    new_file_id: str | None = None,
    new_admin_media_message_id: int | None = None,
) -> bool:
    submission = await db.get_submission(submission_id)
    if submission is None:
        await db.clear_admin_edit_state(admin_id)
        logger.warning("Submission %s was not found while admin %s edited it", submission_id, admin_id)
        return False

    if submission["status"] != STATUS_PENDING:
        await db.clear_admin_edit_state(admin_id)
        logger.info("Admin %s tried to edit already processed submission %s", admin_id, submission_id)
        return False

    draft_text = new_text if new_text is not None else submission.get("draft_text") or ""
    message_type = new_message_type or submission["message_type"]
    file_id = new_file_id if new_message_type is not None else submission.get("file_id")
    admin_media_message_id = (
        new_admin_media_message_id if new_message_type is not None else submission.get("admin_media_message_id")
    )

    if new_message_type is None:
        await db.update_draft_text(submission_id, draft_text)
    else:
        await db.update_submission_content(
            submission_id,
            message_type=message_type,
            draft_text=draft_text,
            file_id=file_id,
            admin_media_message_id=admin_media_message_id,
        )
    await db.clear_admin_edit_state(admin_id)

    updated_submission = await db.get_submission(submission_id)
    if updated_submission is not None:
        await _update_admin_preview(bot, config, updated_submission, keep_buttons=True)

    logger.info("Admin %s edited submission %s and closed edit mode", admin_id, submission_id)
    return True


async def _delete_edit_messages(
    bot: Bot,
    admin_id: int,
    edit_chat_id: int,
    edit_message_id: int,
    *,
    protected_message_refs: set[tuple[int, int]],
    delete_edit_content: bool = True,
) -> None:
    messages_to_delete = []
    if delete_edit_content:
        messages_to_delete.append((edit_chat_id, edit_message_id, "edit content"))
    messages_to_delete.extend(
        (chat_id, message_id, "edit prompt")
        for chat_id, message_id in _edit_prompt_message_ids.pop(admin_id, [])
    )

    for chat_id, message_id, label in messages_to_delete:
        if (chat_id, message_id) in protected_message_refs:
            logger.warning("Skipped deleting protected moderation message %s in chat %s", message_id, chat_id)
            continue

        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            logger.info("Could not delete %s message %s in chat %s", label, message_id, chat_id)


async def _delete_edit_prompts(bot: Bot, admin_id: int) -> None:
    for chat_id, message_id in _edit_prompt_message_ids.pop(admin_id, []):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            logger.info("Could not delete edit prompt message %s in chat %s", message_id, chat_id)


async def _mark_media_replacement(
    *,
    bot: Bot,
    config: Config,
    submission_id: int,
    old_media_message_id: int | None,
    new_media_chat_id: int,
    new_media_message_id: int,
) -> None:
    await _send_media_marker(
        bot=bot,
        chat_id=new_media_chat_id,
        message_id=new_media_message_id,
        text=t("admin.media.new_marker", submission_id=submission_id),
    )

    if old_media_message_id is None:
        return

    if config.admin_chat_id == new_media_chat_id and old_media_message_id == new_media_message_id:
        return

    await _send_media_marker(
        bot=bot,
        chat_id=config.admin_chat_id,
        message_id=old_media_message_id,
        text=t("admin.media.old_marker", submission_id=submission_id),
    )


async def _send_media_marker(bot: Bot, chat_id: int, message_id: int, text: str) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=message_id,
            allow_sending_without_reply=True,
        )
    except TelegramAPIError:
        logger.info("Could not send media marker as reply to message %s in chat %s", message_id, chat_id)
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except TelegramAPIError:
            logger.info("Could not send media marker fallback in chat %s", chat_id)


def _remember_edit_prompt(admin_id: int, chat_id: int, message_id: int) -> None:
    _edit_prompt_message_ids.setdefault(admin_id, []).append((chat_id, message_id))


def _admin_media_message_id(submission: dict | None) -> int | None:
    if submission is None or submission.get("admin_media_message_id") is None:
        return None

    return int(submission["admin_media_message_id"])


def _protected_message_refs(submission: dict | None, admin_chat_id: int) -> set[tuple[int, int]]:
    if submission is None or submission.get("admin_message_id") is None:
        return set()

    return {(admin_chat_id, int(submission["admin_message_id"]))}


async def _answer_callback(callback: CallbackQuery, text: str | None = None, *, show_alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        if _is_expired_callback_error(exc):
            logger.info("Callback query %s expired before the bot could answer it", callback.id)
            return
        logger.warning("Failed to answer callback query %s: %s", callback.id, exc)
    except TelegramAPIError:
        logger.warning("Failed to answer callback query %s", callback.id, exc_info=True)


def _is_expired_callback_error(exc: TelegramBadRequest) -> bool:
    message = str(exc).lower()
    return "query is too old" in message or "query id is invalid" in message


def _is_channel_edit_prompt(text: str | None) -> bool:
    return bool(
        text
        and text.startswith(t("admin.edit.prompt_detection_prefix"))
        and t("admin.edit.prompt_detection_marker") in text
    )


def _chat_type(message: Message) -> str:
    chat_type = message.chat.type
    return getattr(chat_type, "value", chat_type)
