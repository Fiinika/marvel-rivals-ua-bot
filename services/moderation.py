from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiohttp import ClientOSError

from config import Config
from database import Database
from keyboards import moderation_keyboard
from services.formatter import format_admin_preview
from services.i18n import t
from services.post_footer import format_post_html, submission_allows_source_link
from services.publisher import (
    DISABLED_LINK_PREVIEW,
    PREVIEW_LINK_SOURCE_TYPES,
    TELEGRAM_CAPTION_LIMIT,
    album_caption_html,
    album_image_urls,
    link_preview_options_for,
    needs_external_video_download,
    send_album_message,
    send_downloaded_video_post,
    send_youtube_post,
)
from services.telegram_retry import delay_between_telegram_sends, send_with_retries


logger = logging.getLogger(__name__)


class ModerationSendError(RuntimeError):
    """Raised when a submission cannot be sent to the moderation queue."""


@dataclass(frozen=True)
class SentModerationPart:
    content_message_id: int
    media_message_id: int | None = None


async def send_submission_to_moderation(bot: Bot, config: Config, db: Database, submission_id: int) -> None:
    submission = await db.get_submission(submission_id)
    if submission is None:
        raise ModerationSendError(f"Submission {submission_id} was not found")

    await _send_submission_parts_to_moderation(bot, config, db, submission)

    submission = await db.get_submission(submission_id) or submission
    try:
        await delay_between_telegram_sends()
        admin_message = await send_with_retries(
            bot.send_message,
            label=f"admin moderation preview for submission {submission_id}",
            chat_id=config.admin_chat_id,
            text=format_admin_preview(submission),
            reply_markup=moderation_keyboard(submission_id),
            parse_mode="HTML",
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except (TelegramAPIError, TimeoutError, ClientOSError) as exc:
        raise ModerationSendError("Failed to send admin moderation preview") from exc

    await db.set_admin_message_id(submission_id, admin_message.message_id)
    logger.info("Sent submission %s to admin moderation queue as message %s", submission_id, admin_message.message_id)


async def _send_submission_parts_to_moderation(
    bot: Bot,
    config: Config,
    db: Database,
    submission: dict,
) -> None:
    if str(submission.get("message_type") or "") == "album":
        await _send_album_to_moderation(bot, config, submission)
        return

    parts = list(submission.get("parts", []))
    for position, part in enumerate(parts):
        part_index = int(part["part_index"])
        part_with_context = {**submission, **part, "id": submission["id"]}
        try:
            sent_part = await _send_part_message(bot, config, config.admin_chat_id, part_with_context)
        except (TelegramAPIError, TimeoutError, ClientOSError) as exc:
            raise ModerationSendError(
                f"Failed to send submission {submission['id']} part {part_index} to moderation"
            ) from exc

        await db.set_submission_part_admin_message_id(
            int(submission["id"]),
            part_index,
            sent_part.content_message_id,
        )
        if _has_media(part) and part_index == 1:
            await db.set_admin_media_message_id(int(submission["id"]), sent_part.media_message_id)

        logger.info(
            "Sent submission %s part %s to admin moderation queue as message %s",
            submission["id"],
            part_index,
            sent_part.content_message_id,
        )
        if position < len(parts) - 1:
            await delay_between_telegram_sends()


async def _send_album_to_moderation(bot: Bot, config: Config, submission: dict) -> None:
    """Preview an album submission for the admins: the media group plus the same
    caption (with footer/links) that will be published."""
    images = album_image_urls(submission)
    caption = album_caption_html(submission)
    try:
        await send_album_message(bot, config.admin_chat_id, images, caption)
    except (TelegramAPIError, TimeoutError, ClientOSError) as exc:
        raise ModerationSendError(
            f"Failed to send album submission {submission.get('id')} to moderation"
        ) from exc


async def _send_part_message(bot: Bot, config: Config, chat_id: int, part: dict) -> SentModerationPart:
    message_type = str(part.get("message_type") or "text")
    text = _format_part_for_moderation(str(part.get("text") or ""), part)
    file_id = part.get("file_id")
    media_url = part.get("media_url")
    media_value = file_id or media_url
    uses_external_media = not file_id and bool(media_url)

    # A YouTube post is text + a source link; preview it the SAME way it will be
    # published — as a native inline video (downloaded), falling back to a playable
    # link preview — so the admin approves exactly what goes out.
    if (
        message_type == "text"
        and config.enable_youtube_video_download
        and str(part.get("source_type") or "") in PREVIEW_LINK_SOURCE_TYPES
        and str(part.get("source_url") or "").strip()
    ):
        sent = await send_youtube_post(
            bot,
            chat_id,
            str(part.get("source_url")),
            text,
            parse_mode="HTML",
            link_preview=link_preview_options_for(part),
            max_bytes=max(1, config.youtube_video_max_mb) * 1024 * 1024,
        )
        if sent is not None:
            return SentModerationPart(content_message_id=sent.message_id)

    # A Bluesky (or other external-URL) video is downloaded and previewed as a
    # native inline video — exactly as it will be published — with a text fallback.
    if needs_external_video_download(part):
        sent = await send_downloaded_video_post(
            bot,
            chat_id,
            str(part.get("media_url") or ""),
            text,
            parse_mode="HTML",
            link_preview=link_preview_options_for(part),
            max_bytes=max(1, config.bluesky_video_max_mb) * 1024 * 1024,
        )
        # The single returned message carries the preview (native video or text
        # fallback); point both ids at it so cleanup deletes the right message.
        return SentModerationPart(content_message_id=sent.message_id, media_message_id=sent.message_id)

    if message_type == "photo" and media_value:
        return await _send_media_part(
            bot.send_photo,
            chat_id=chat_id,
            media_argument="photo",
            media_value=media_value,
            text=text,
            short_caption=_short_media_caption(part),
            uses_external_media=uses_external_media,
            fallback_label="photo",
        )
    if message_type == "video" and media_value:
        return await _send_media_part(
            bot.send_video,
            chat_id=chat_id,
            media_argument="video",
            media_value=media_value,
            text=text,
            short_caption=_short_media_caption(part),
            uses_external_media=uses_external_media,
            fallback_label="video",
        )
    if message_type == "document" and media_value:
        return await _send_media_part(
            bot.send_document,
            chat_id=chat_id,
            media_argument="document",
            media_value=media_value,
            text=text,
            short_caption=_short_media_caption(part),
            uses_external_media=uses_external_media,
            fallback_label="document",
        )

    message = await send_with_retries(
        bot.send_message,
        label="moderation text part",
        chat_id=chat_id,
        text=text or t("formatter.empty"),
        parse_mode="HTML",
        link_preview_options=link_preview_options_for(part),
    )
    return SentModerationPart(content_message_id=message.message_id)


async def _send_media_part(
    send_method,
    *,
    chat_id: int,
    media_argument: str,
    media_value: str,
    text: str,
    short_caption: str,
    uses_external_media: bool,
    fallback_label: str,
) -> SentModerationPart:
    try:
        if text and _html_visible_length(text) > TELEGRAM_CAPTION_LIMIT:
            media_message = await send_with_retries(
                send_method,
                label=f"moderation {fallback_label} media without long caption",
                chat_id=chat_id,
                **{media_argument: media_value},
                caption=short_caption,
                parse_mode="HTML",
            )
            await delay_between_telegram_sends()
            text_message = await send_with_retries(
                send_method.__self__.send_message,
                label=f"moderation text fallback for long {fallback_label} caption",
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                link_preview_options=DISABLED_LINK_PREVIEW,
            )
            return SentModerationPart(
                content_message_id=text_message.message_id,
                media_message_id=media_message.message_id,
            )

        media_message = await send_with_retries(
            send_method,
            label=f"moderation {fallback_label} media part",
            chat_id=chat_id,
            **{media_argument: media_value},
            caption=text or None,
            parse_mode="HTML",
        )
        return SentModerationPart(
            content_message_id=media_message.message_id,
            media_message_id=media_message.message_id,
        )
    except (TelegramAPIError, TimeoutError, ClientOSError):
        if not uses_external_media:
            raise

        logger.exception("Failed to send external %s to moderation. Falling back to text-only.", fallback_label)
        text_message = await send_with_retries(
            send_method.__self__.send_message,
            label=f"moderation text-only fallback for failed {fallback_label}",
            chat_id=chat_id,
            text=text or t("formatter.empty"),
            parse_mode="HTML",
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
        return SentModerationPart(content_message_id=text_message.message_id)


def _caption_or_none(text: str) -> str | None:
    if not text:
        return None

    if _html_visible_length(text) <= TELEGRAM_CAPTION_LIMIT:
        return text

    return format_post_html("")


def _format_part_for_moderation(text: str, part: dict) -> str:
    return format_post_html(
        text,
        source_url=str(part.get("source_url") or ""),
        allow_source_link=submission_allows_source_link(part),
        include_community_footer=True,
    )


def _html_visible_length(text: str) -> int:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return len(unescape(without_tags))


def _has_media(part: dict) -> bool:
    return str(part.get("message_type") or "") in {"photo", "video", "document"} and bool(
        part.get("file_id") or part.get("media_url")
    )


def _short_media_caption(part: dict) -> str:
    submission_id = part.get("submission_id")
    if submission_id:
        return f"Медіа до заявки #{submission_id}"

    return "Медіа до заявки"
