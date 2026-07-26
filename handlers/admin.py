from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from html import unescape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from config import Config
from database import STATUS_PENDING, Database
from keyboards import (
    CollectorCallback,
    ModerationCallback,
    ModerationPartCallback,
    collector_source_keyboard,
    edit_part_keyboard,
    moderation_keyboard,
    save_draft_keyboard,
)
from services.collectors.registry import (
    CollectionMode,
    create_collector,
    format_collection_report,
    get_collector_definition,
    list_collector_definitions,
)
from services.collectors.wiki_facts.collector import run_wiki_facts_once
from services.digests.fanart import run_fanart_digest_once
from services.formatter import format_admin_preview
from services.i18n import t
from services.post_footer import format_post_html, submission_allows_source_link
from services.publisher import (
    DISABLED_LINK_PREVIEW,
    DOWNLOAD_VIDEO_SOURCE_TYPES,
    PublishingError,
    TELEGRAM_CAPTION_LIMIT,
    TELEGRAM_TEXT_LIMIT,
    publish_submission,
)


logger = logging.getLogger(__name__)
router = Router(name="admin")

_approve_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_edit_prompt_message_ids: dict[int, list[tuple[int, int]]] = {}
URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)


class AdminChatFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config) -> bool:
        return message.chat.id == config.admin_chat_id


class AdminCommandChatFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config) -> bool:
        if message.from_user is None or message.from_user.id not in config.admin_user_ids:
            return False

        return message.chat.id == config.admin_chat_id or _chat_type(message) == "private"


class AdminOrPrivateChatFilter(BaseFilter):
    """Chat-scope-only gate: the admin chat or a private DM.

    Used for /fetch_news so the bot never answers it in the public moderated
    group — there the message falls through to the moderation router instead
    (flood-counted and filtered like any other member message).
    """

    async def __call__(self, message: Message, config: Config) -> bool:
        return message.chat.id == config.admin_chat_id or _chat_type(message) == "private"


class AdminEditMessageFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config, db: Database) -> bool:
        if message.from_user is None or message.from_user.id not in config.admin_user_ids:
            return False

        if message.chat.id != config.admin_chat_id and _chat_type(message) != "private":
            return False

        state = await db.get_admin_edit_state(message.from_user.id)
        return state is not None and state.get("mode") == "add_part"


class AdminChannelEditPostFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config, db: Database) -> bool:
        if message.chat.id != config.admin_chat_id or _chat_type(message) != "channel":
            return False

        state = await db.get_latest_admin_edit_state()
        return state is not None and state.get("mode") == "add_part"


class AdminDraftMessageFilter(BaseFilter):
    async def __call__(self, message: Message, config: Config, db: Database) -> bool:
        if message.from_user is None or message.from_user.id not in config.admin_user_ids:
            return False

        if message.chat.id != config.admin_chat_id and _chat_type(message) != "private":
            return False

        state = await db.get_admin_edit_state(message.from_user.id)
        return state is not None and state.get("mode") == "draft_part"


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


@router.callback_query(ModerationPartCallback.filter())
async def moderation_part_callback(
    callback: CallbackQuery,
    callback_data: ModerationPartCallback,
    bot: Bot,
    config: Config,
    db: Database,
) -> None:
    admin_id = callback.from_user.id
    if admin_id not in config.admin_user_ids:
        logger.warning("User %s tried to use admin part action %s", admin_id, callback_data.action)
        await _answer_callback(callback, t("admin.alert.admin_only"), show_alert=True)
        return

    if callback_data.action == "select":
        await _start_part_draft(callback, callback_data.submission_id, callback_data.part_index, bot, config, db)
    elif callback_data.action == "add":
        await _start_add_part(callback, callback_data.submission_id, bot, config, db)
    elif callback_data.action == "save":
        await _save_part_draft(callback, callback_data.submission_id, callback_data.part_index, bot, config, db)
    else:
        logger.warning("Unknown moderation part action: %s", callback_data.action)
        await _answer_callback(callback, t("admin.alert.unknown_action"), show_alert=True)


@router.message(AdminOrPrivateChatFilter(), Command("fetch_news"))
async def fetch_news(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.from_user is None or message.from_user.id not in config.admin_user_ids:
        await message.answer(t("admin.news_fetch.no_permission"))
        return

    if message.chat.id != config.admin_chat_id and _chat_type(message) != "private":
        await message.answer(t("admin.news_fetch.wrong_chat"))
        return

    await message.answer(
        t("admin.news_fetch.choose_source"),
        reply_markup=collector_source_keyboard(list_collector_definitions(config)),
    )


@router.message(AdminOrPrivateChatFilter(), Command("fanartdigest"))
async def fanart_digest_command(
    message: Message, command: CommandObject, bot: Bot, config: Config, db: Database
) -> None:
    """Manually build this week's fan-art digest into the moderation queue. Pass
    `force` to bypass the once-a-week guard (useful for testing)."""
    if message.from_user is None or message.from_user.id not in config.admin_user_ids:
        await message.answer(t("admin.fanart_digest.no_permission"))
        return

    if message.chat.id != config.admin_chat_id and _chat_type(message) != "private":
        await message.answer(t("admin.fanart_digest.wrong_chat"))
        return

    force = (command.args or "").strip().casefold() == "force"
    await message.answer(t("admin.fanart_digest.started"))
    try:
        created = await run_fanart_digest_once(bot, config, db, force=force)
    except Exception:
        logger.exception("Manual fan-art digest failed")
        await message.answer(t("admin.fanart_digest.failed"))
        return

    await message.answer(t("admin.fanart_digest.queued") if created else t("admin.fanart_digest.skipped"))


@router.message(AdminOrPrivateChatFilter(), Command("wikifact"))
async def wiki_fact_command(message: Message, bot: Bot, config: Config, db: Database) -> None:
    """Queue one "Чи знали ви?" fact on demand.

    The rubric also has a /fetch_news button, but only this command reports WHY it
    is idle: the button simply disappears when the rubric is off, while this replies
    with the failing precondition (ENABLE_WIKI_FACTS or GEMINI_API_KEY).
    """
    if message.from_user is None or message.from_user.id not in config.admin_user_ids:
        await message.answer(t("admin.wiki_fact.no_permission"))
        return

    if message.chat.id != config.admin_chat_id and _chat_type(message) != "private":
        await message.answer(t("admin.wiki_fact.wrong_chat"))
        return

    # Report the same two conditions the weekly scheduler checks at startup, so a
    # silent "the rubric never posts" is diagnosable without reading server logs.
    if not config.enable_wiki_facts:
        await message.answer(t("admin.wiki_fact.disabled"))
        return
    if not config.gemini_api_key:
        await message.answer(t("admin.wiki_fact.no_gemini"))
        return

    await message.answer(t("admin.wiki_fact.started"))
    try:
        created = await run_wiki_facts_once(bot, config, db)
    except Exception:
        logger.exception("Manual wiki-facts run failed")
        await message.answer(t("admin.wiki_fact.failed"))
        return

    await message.answer(t("admin.wiki_fact.queued") if created else t("admin.wiki_fact.skipped"))


@router.callback_query(CollectorCallback.filter())
async def collector_callback(
    callback: CallbackQuery,
    callback_data: CollectorCallback,
    bot: Bot,
    config: Config,
    db: Database,
) -> None:
    admin_id = callback.from_user.id
    if admin_id not in config.admin_user_ids:
        logger.warning("User %s tried to run collector %s", admin_id, callback_data.collector_id)
        await _answer_callback(callback, t("admin.alert.admin_only"), show_alert=True)
        return

    definition = get_collector_definition(callback_data.collector_id)
    collector = create_collector(callback_data.collector_id, config=config, db=db, bot=bot)
    if definition is None or collector is None:
        await _answer_callback(callback, t("admin.news_fetch.unknown_source"), show_alert=True)
        return

    target_chat_id = callback.message.chat.id if callback.message is not None else config.admin_chat_id
    target_message_id = callback.message.message_id if callback.message is not None else None
    started_text = t("admin.news_fetch.started", source=definition.title)

    if target_message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=target_chat_id,
                message_id=target_message_id,
                text=started_text,
                reply_markup=None,
                link_preview_options=DISABLED_LINK_PREVIEW,
            )
        except TelegramAPIError:
            logger.info("Could not edit collector selector message; sending parsing status separately")
            status_message = await bot.send_message(
                chat_id=target_chat_id,
                text=started_text,
                link_preview_options=DISABLED_LINK_PREVIEW,
            )
            target_message_id = status_message.message_id
    else:
        status_message = await bot.send_message(
            chat_id=target_chat_id,
            text=started_text,
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
        target_message_id = status_message.message_id

    await _answer_callback(callback)
    stats = await collector.run_once(mode=CollectionMode.MANUAL_LATEST)
    report = format_collection_report(stats)

    try:
        await bot.edit_message_text(
            chat_id=target_chat_id,
            message_id=target_message_id,
            text=report,
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramAPIError:
        logger.info("Could not edit collector status message; sending a new report")
        await bot.send_message(chat_id=target_chat_id, text=report, link_preview_options=DISABLED_LINK_PREVIEW)


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

    await _receive_admin_part_message(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="photo",
        text=message.caption or "",
        file_id=photo.file_id,
    )


@router.message(AdminEditMessageFilter(), F.video)
async def receive_admin_video_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.video is None:
        return

    await _receive_admin_part_message(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="video",
        text=message.caption or "",
        file_id=message.video.file_id,
    )


@router.message(AdminEditMessageFilter(), F.document)
async def receive_admin_document_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.document is None:
        return

    await _receive_admin_part_message(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="document",
        text=message.caption or "",
        file_id=message.document.file_id,
    )


@router.message(AdminEditMessageFilter(), F.text)
async def receive_admin_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    assert message.from_user is not None
    await _receive_admin_part_message(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="link" if _contains_link(message.text or "") else "text",
        text=message.text or "",
        file_id=None,
    )


@router.channel_post(AdminChannelEditPostFilter(), F.photo)
async def receive_channel_photo_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        return

    await _receive_channel_part_message(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="photo",
        text=message.caption or "",
        file_id=photo.file_id,
    )


@router.channel_post(AdminChannelEditPostFilter(), F.video)
async def receive_channel_video_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.video is None:
        return

    await _receive_channel_part_message(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="video",
        text=message.caption or "",
        file_id=message.video.file_id,
    )


@router.channel_post(AdminChannelEditPostFilter(), F.document)
async def receive_channel_document_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if message.document is None:
        return

    await _receive_channel_part_message(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="document",
        text=message.caption or "",
        file_id=message.document.file_id,
    )


@router.channel_post(AdminChannelEditPostFilter(), F.text)
async def receive_channel_admin_text_edit(message: Message, bot: Bot, config: Config, db: Database) -> None:
    if _is_channel_prompt(message.text):
        return

    await _receive_channel_part_message(
        message=message,
        bot=bot,
        config=config,
        db=db,
        message_type="link" if _contains_link(message.text or "") else "text",
        text=message.text or "",
        file_id=None,
    )


@router.message(AdminDraftMessageFilter())
async def update_draft_from_admin_message(message: Message, bot: Bot, config: Config, db: Database) -> None:
    assert message.from_user is not None
    state = await db.get_admin_edit_state(message.from_user.id)
    if state is None:
        return

    submission_id = int(state["submission_id"])
    part_index = int(state["part_index"])
    part = await db.get_submission_part(submission_id, part_index)
    if part is None:
        await message.answer(t("admin.alert.submission_not_found"))
        return

    new_text = _message_text(message).strip()
    validation_error = _validate_part_text(part, new_text)
    if validation_error is not None:
        await message.answer(validation_error)
        return

    if not await _edit_draft_part_message(bot, config, state, part, new_text):
        await message.answer(t("admin.alert.save_failed"))
        return

    await _delete_message_safely(bot, message.chat.id, message.message_id, "draft source")


async def _receive_admin_part_message(
    *,
    message: Message,
    bot: Bot,
    config: Config,
    db: Database,
    message_type: str,
    text: str,
    file_id: str | None,
) -> None:
    assert message.from_user is not None
    state = await db.get_admin_edit_state(message.from_user.id)
    if state is None or state.get("mode") != "add_part":
        return

    submission_id = int(state["submission_id"])
    copied_message_id = await _copy_new_part_message(
        bot=bot,
        config=config,
        submission_id=submission_id,
        message_type=message_type,
        text=text,
        file_id=file_id,
    )
    if copied_message_id is None:
        await message.answer(t("admin.alert.save_failed"))
        return

    await _add_part_and_refresh_metadata(
        db=db,
        bot=bot,
        config=config,
        admin_id=message.from_user.id,
        submission_id=submission_id,
        message_type=message_type,
        text=text,
        file_id=file_id,
        admin_message_id=copied_message_id,
    )
    await _delete_message_safely(bot, message.chat.id, message.message_id, "new part source")


async def _receive_channel_part_message(
    *,
    message: Message,
    bot: Bot,
    config: Config,
    db: Database,
    message_type: str,
    text: str,
    file_id: str | None,
) -> None:
    state = await db.get_latest_admin_edit_state()
    if state is None or state.get("mode") != "add_part":
        return

    submission_id = int(state["submission_id"])
    admin_id = int(state["admin_id"])
    logger.info(
        "Received channel part for submission %s from admin state %s as %s",
        submission_id,
        admin_id,
        message_type,
    )
    copied_message_id = await _copy_new_part_message(
        bot=bot,
        config=config,
        submission_id=submission_id,
        message_type=message_type,
        text=text,
        file_id=file_id,
    )
    if copied_message_id is None:
        return

    await _add_part_and_refresh_metadata(
        db=db,
        bot=bot,
        config=config,
        admin_id=admin_id,
        submission_id=submission_id,
        message_type=message_type,
        text=text,
        file_id=file_id,
        admin_message_id=copied_message_id,
    )
    await _delete_message_safely(bot, message.chat.id, message.message_id, "new part source")


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

    # Album digests (one grouped post) and native-video posts (YouTube, or a
    # downloaded Bluesky video) are not edited in place — the per-part edit flow
    # can't edit a media group / video message's text — so admins approve or
    # reject them as a whole.
    message_type = str(submission.get("message_type") or "")
    source_type = str(submission.get("source_type") or "")
    if (
        message_type == "album"
        or source_type == "youtube"
        or (message_type == "video" and source_type in DOWNLOAD_VIDEO_SOURCE_TYPES)
    ):
        await _answer_callback(callback, t("admin.alert.media_post_edit_unsupported"), show_alert=True)
        return

    parts = submission.get("parts", [])
    logger.info("Admin %s opened part selector for submission %s", callback.from_user.id, submission_id)

    if callback.message is not None:
        try:
            prompt_message = await bot.send_message(
                chat_id=config.admin_chat_id,
                text=t("admin.edit.choose_part", submission_id=submission_id),
                reply_markup=edit_part_keyboard(submission_id, max(1, len(parts))),
                link_preview_options=DISABLED_LINK_PREVIEW,
            )
            _remember_edit_prompt(
                callback.from_user.id,
                config.admin_chat_id,
                prompt_message.message_id,
            )
        except TelegramAPIError:
            logger.exception("Could not send edit part selector for submission %s", submission_id)
            await _answer_callback(callback, t("admin.edit.draft_unavailable"), show_alert=True)
            return

    await _answer_callback(callback)


async def _start_part_draft(
    callback: CallbackQuery,
    submission_id: int,
    part_index: int,
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

    part = await db.get_submission_part(submission_id, part_index)
    if part is None:
        await _answer_callback(callback, t("admin.alert.submission_not_found"), show_alert=True)
        return

    draft_message_id = await _copy_part_to_draft_message(bot, config, submission_id, part)
    if draft_message_id is None:
        await _answer_callback(callback, t("admin.edit.draft_unavailable"), show_alert=True)
        return

    await db.set_admin_edit_state(
        callback.from_user.id,
        submission_id,
        mode="draft_part",
        part_index=part_index,
        draft_message_id=draft_message_id,
    )
    _remember_edit_prompt(callback.from_user.id, config.admin_chat_id, draft_message_id)

    if callback.message is not None:
        await _delete_message_safely(
            bot,
            callback.message.chat.id,
            callback.message.message_id,
            "edit part selector",
        )
        _forget_edit_prompt(callback.from_user.id, callback.message.chat.id, callback.message.message_id)

    logger.info("Admin %s created draft for submission %s part %s", callback.from_user.id, submission_id, part_index)
    await _answer_callback(callback)


async def _start_add_part(
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

    await db.set_admin_edit_state(callback.from_user.id, submission_id, mode="add_part")
    try:
        prompt_message = await bot.send_message(
            chat_id=config.admin_chat_id,
            text=t("admin.edit.add_part_channel_prompt" if _callback_chat_type(callback) == "channel" else "admin.edit.add_part_prompt", submission_id=submission_id),
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
        _remember_edit_prompt(callback.from_user.id, config.admin_chat_id, prompt_message.message_id)
    except TelegramAPIError:
        logger.exception("Could not send add-part prompt for submission %s", submission_id)
        await db.clear_admin_edit_state(callback.from_user.id)
        await _answer_callback(callback, t("admin.edit.draft_unavailable"), show_alert=True)
        return

    if callback.message is not None and callback.message.message_id != prompt_message.message_id:
        _remember_edit_prompt(callback.from_user.id, callback.message.chat.id, callback.message.message_id)

    logger.info("Admin %s started adding a new part to submission %s", callback.from_user.id, submission_id)
    await _answer_callback(callback)


async def _save_part_draft(
    callback: CallbackQuery,
    submission_id: int,
    part_index: int,
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

    part = await db.get_submission_part(submission_id, part_index)
    if part is None or callback.message is None:
        await _answer_callback(callback, t("admin.alert.submission_not_found"), show_alert=True)
        return

    new_text = _message_text(callback.message).strip()
    validation_error = _validate_part_text(part, new_text)
    if validation_error is not None:
        await _answer_callback(callback, validation_error, show_alert=True)
        return

    if not await _edit_original_part_message(bot, config, part, new_text):
        await _answer_callback(callback, t("admin.alert.save_failed"), show_alert=True)
        return

    await db.update_submission_part_text(submission_id, part_index, new_text)
    await db.clear_admin_edit_state(callback.from_user.id)
    await _delete_message_safely(bot, callback.message.chat.id, callback.message.message_id, "draft part")
    _forget_edit_prompt(callback.from_user.id, callback.message.chat.id, callback.message.message_id)
    await _delete_edit_prompts(bot, callback.from_user.id)

    updated_submission = await db.get_submission(submission_id)
    if updated_submission is not None:
        await _update_admin_preview(bot, config, updated_submission, keep_buttons=True)

    logger.info("Admin %s saved submission %s part %s", callback.from_user.id, submission_id, part_index)
    await _answer_callback(callback, t("admin.edit.saved"))


async def _copy_part_to_draft_message(
    bot: Bot,
    config: Config,
    submission_id: int,
    part: dict,
) -> int | None:
    reply_markup = save_draft_keyboard(submission_id, int(part["part_index"]))
    try:
        message = await _send_part_draft_message(bot, config.admin_chat_id, part, reply_markup=reply_markup)
    except TelegramAPIError:
        logger.exception("Could not send fallback draft for submission %s part %s", submission_id, part["part_index"])
        return None

    return message.message_id


async def _send_part_draft_message(bot: Bot, chat_id: int, part: dict, *, reply_markup):
    message_type = str(part.get("message_type") or "text")
    text = _format_part_for_moderation(str(part.get("text") or ""), part)
    media_value = part.get("file_id") or part.get("media_url")
    if _part_has_media(part) and message_type == "photo" and media_value:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=media_value,
            caption=text or None,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    if _part_has_media(part) and message_type == "video" and media_value:
        return await bot.send_video(
            chat_id=chat_id,
            video=media_value,
            caption=text or None,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    if _part_has_media(part) and message_type == "document" and media_value:
        return await bot.send_document(
            chat_id=chat_id,
            document=media_value,
            caption=text or None,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    return await bot.send_message(
        chat_id=chat_id,
        text=text or t("formatter.empty"),
        parse_mode="HTML",
        reply_markup=reply_markup,
        link_preview_options=DISABLED_LINK_PREVIEW,
    )


async def _copy_new_part_message(
    *,
    bot: Bot,
    config: Config,
    submission_id: int,
    message_type: str,
    text: str,
    file_id: str | None,
) -> int | None:
    display_text = _format_part_for_moderation(text)
    try:
        if message_type == "photo" and file_id:
            copied_message = await bot.send_photo(
                chat_id=config.admin_chat_id,
                photo=file_id,
                caption=_caption_or_none(display_text),
                parse_mode="HTML",
            )
        elif message_type == "video" and file_id:
            copied_message = await bot.send_video(
                chat_id=config.admin_chat_id,
                video=file_id,
                caption=_caption_or_none(display_text),
                parse_mode="HTML",
            )
        elif message_type == "document" and file_id:
            copied_message = await bot.send_document(
                chat_id=config.admin_chat_id,
                document=file_id,
                caption=_caption_or_none(display_text),
                parse_mode="HTML",
            )
        else:
            copied_message = await bot.send_message(
                chat_id=config.admin_chat_id,
                text=display_text,
                parse_mode="HTML",
                link_preview_options=DISABLED_LINK_PREVIEW,
            )
    except TelegramAPIError:
        logger.exception("Failed to copy new part message for submission %s", submission_id)
        return None

    return copied_message.message_id


async def _add_part_and_refresh_metadata(
    *,
    db: Database,
    bot: Bot,
    config: Config,
    admin_id: int,
    submission_id: int,
    message_type: str,
    text: str,
    file_id: str | None,
    admin_message_id: int,
) -> None:
    submission = await db.get_submission(submission_id)
    if submission is None or submission["status"] != STATUS_PENDING:
        await db.clear_admin_edit_state(admin_id)
        return

    await db.add_submission_part(
        submission_id,
        message_type=message_type,
        text=text,
        file_id=file_id,
        media_url=None,
        media_type=_media_type_for(message_type),
        admin_message_id=admin_message_id,
    )
    await db.clear_admin_edit_state(admin_id)
    await _delete_edit_prompts(bot, admin_id)

    updated_submission = await db.get_submission(submission_id)
    if updated_submission is not None:
        await _move_admin_preview_to_bottom(bot, config, db, updated_submission, keep_buttons=True)

    logger.info("Admin %s added a new part to submission %s", admin_id, submission_id)


async def _move_admin_preview_to_bottom(
    bot: Bot,
    config: Config,
    db: Database,
    submission: dict,
    *,
    keep_buttons: bool,
) -> None:
    old_admin_message_id = submission.get("admin_message_id")
    if old_admin_message_id is not None:
        deleted = await _delete_message_safely(bot, config.admin_chat_id, int(old_admin_message_id), "old admin preview")
        if not deleted:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=config.admin_chat_id,
                    message_id=int(old_admin_message_id),
                    reply_markup=None,
                )
            except TelegramAPIError:
                logger.info("Could not remove buttons from old admin preview %s", old_admin_message_id)

    reply_markup = moderation_keyboard(submission["id"]) if keep_buttons else None
    admin_message = await bot.send_message(
        chat_id=config.admin_chat_id,
        text=format_admin_preview(submission),
        reply_markup=reply_markup,
        parse_mode="HTML",
        link_preview_options=DISABLED_LINK_PREVIEW,
    )
    await db.set_admin_message_id(int(submission["id"]), admin_message.message_id)


async def _edit_original_part_message(bot: Bot, config: Config, part: dict, new_text: str) -> bool:
    admin_message_id = part.get("admin_message_id")
    if admin_message_id is None:
        logger.warning("Submission part %s has no admin_message_id", part.get("part_index"))
        return False

    display_text = _format_part_for_moderation(new_text, part)
    try:
        if _part_has_media(part):
            await bot.edit_message_caption(
                chat_id=config.admin_chat_id,
                message_id=int(admin_message_id),
                caption=display_text or None,
                parse_mode="HTML",
            )
        else:
            await bot.edit_message_text(
                chat_id=config.admin_chat_id,
                message_id=int(admin_message_id),
                text=display_text,
                parse_mode="HTML",
                link_preview_options=DISABLED_LINK_PREVIEW,
            )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        logger.exception("Failed to edit original part message %s", admin_message_id)
        return False
    except TelegramAPIError:
        logger.exception("Failed to edit original part message %s", admin_message_id)
        return False

    return True


async def _edit_draft_part_message(bot: Bot, config: Config, state: dict, part: dict, new_text: str) -> bool:
    draft_message_id = state.get("draft_message_id")
    if draft_message_id is None:
        return False

    reply_markup = save_draft_keyboard(int(state["submission_id"]), int(state["part_index"]))
    display_text = _format_part_for_moderation(new_text, part)
    try:
        if _part_has_media(part):
            await bot.edit_message_caption(
                chat_id=config.admin_chat_id,
                message_id=int(draft_message_id),
                caption=display_text or None,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
            await bot.edit_message_text(
                chat_id=config.admin_chat_id,
                message_id=int(draft_message_id),
                text=display_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                link_preview_options=DISABLED_LINK_PREVIEW,
            )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        logger.exception("Failed to edit draft part message %s", draft_message_id)
        return False
    except TelegramAPIError:
        logger.exception("Failed to edit draft part message %s", draft_message_id)
        return False

    return True


def _message_text(message: Message) -> str:
    if message.text is not None:
        return message.text
    if message.caption is not None:
        return message.caption
    return ""


def _validate_part_text(part: dict, text: str) -> str | None:
    rendered_text = _format_part_for_moderation(text, part)
    if _part_has_media(part):
        if _html_visible_length(rendered_text) > TELEGRAM_CAPTION_LIMIT:
            return t("admin.alert.text_too_long")
        return None

    if not text.strip():
        return t("admin.alert.empty_part")
    if _html_visible_length(rendered_text) > TELEGRAM_TEXT_LIMIT:
        return t("admin.alert.text_too_long")
    return None


def _format_part_for_moderation(text: str, part: dict | None = None) -> str:
    part = part or {}
    return format_post_html(
        text,
        source_url=str(part.get("source_url") or ""),
        allow_source_link=submission_allows_source_link(part),
        include_community_footer=True,
    )


def _caption_or_none(text: str) -> str | None:
    if not text:
        return None

    if _html_visible_length(text) <= TELEGRAM_CAPTION_LIMIT:
        return text

    return format_post_html("")


def _html_visible_length(text: str) -> int:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return len(unescape(without_tags))


def _part_has_media(part: dict) -> bool:
    if str(part.get("message_type") or "") not in {"photo", "video", "document"}:
        return False
    if not (part.get("file_id") or part.get("media_url")):
        return False

    admin_message_id = part.get("admin_message_id")
    admin_media_message_id = part.get("admin_media_message_id")
    if admin_message_id is not None and admin_media_message_id is None:
        return False
    if admin_message_id is None or admin_media_message_id is None:
        return True

    return int(admin_message_id) == int(admin_media_message_id)


def _media_type_for(message_type: str) -> str:
    return message_type if message_type in {"photo", "video", "document"} else "none"


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
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.exception("Failed to update admin preview for submission %s", submission["id"])
    except TelegramAPIError:
        logger.exception("Failed to update admin preview for submission %s", submission["id"])


async def _delete_edit_prompts(bot: Bot, admin_id: int) -> None:
    for chat_id, message_id in _edit_prompt_message_ids.pop(admin_id, []):
        await _delete_message_safely(bot, chat_id, message_id, "edit prompt")


async def _delete_message_safely(bot: Bot, chat_id: int, message_id: int, label: str) -> bool:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        logger.info("Could not delete %s message %s in chat %s", label, message_id, chat_id)
        return False

    return True


def _remember_edit_prompt(admin_id: int, chat_id: int, message_id: int) -> None:
    _edit_prompt_message_ids.setdefault(admin_id, []).append((chat_id, message_id))


def _forget_edit_prompt(admin_id: int, chat_id: int, message_id: int) -> None:
    refs = _edit_prompt_message_ids.get(admin_id)
    if not refs:
        return

    _edit_prompt_message_ids[admin_id] = [
        (stored_chat_id, stored_message_id)
        for stored_chat_id, stored_message_id in refs
        if stored_chat_id != chat_id or stored_message_id != message_id
    ]
    if not _edit_prompt_message_ids[admin_id]:
        _edit_prompt_message_ids.pop(admin_id, None)


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


def _is_channel_add_prompt(text: str | None) -> bool:
    return bool(
        text
        and text.startswith(t("admin.edit.add_prompt_detection_prefix"))
        and t("admin.edit.add_prompt_detection_marker") in text
    )


def _is_channel_prompt(text: str | None) -> bool:
    return _is_channel_edit_prompt(text) or _is_channel_add_prompt(text)


def _contains_link(text: str) -> bool:
    return bool(URL_PATTERN.search(text))


def _callback_chat_type(callback: CallbackQuery) -> str | None:
    if callback.message is None:
        return None

    return _chat_type(callback.message)


def _chat_type(message: Message) -> str:
    chat_type = message.chat.type
    return getattr(chat_type, "value", chat_type)
