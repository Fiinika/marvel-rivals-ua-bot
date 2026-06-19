from __future__ import annotations

import ipaddress
import logging
import re
from html import unescape
from typing import Any

from urllib.parse import urlsplit

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto, LinkPreviewOptions
from aiohttp import ClientOSError

from config import Config
from services.post_footer import format_post_html, submission_allows_source_link
from services.telegram_retry import delay_between_telegram_sends, send_with_retries
from services.youtube_video import download_youtube_video


logger = logging.getLogger(__name__)

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
DISABLED_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Sources whose posts are published as TEXT with a playable link preview of the
# source URL (e.g. YouTube — Telegram embeds the player) instead of a static photo.
PREVIEW_LINK_SOURCE_TYPES = {"youtube"}
# Album sources whose draft_text is ALREADY rendered HTML (built with its own
# hyperlinks/footer) and must be sent as-is; every other album caption is plain
# text run through the normal formatter.
PRERENDERED_ALBUM_SOURCE_TYPES = {"reddit_fanart"}

# Album images are downloaded and re-uploaded as bytes (not sent by URL): Telegram
# rejects by-URL photos over ~5MB (fan art is often larger) and a media group is
# atomic, so one bad URL fails the whole album. Uploading bytes lifts the limit to
# 10MB and lets us validate/skip non-image content first.
_ALBUM_IMAGE_USER_AGENT = "Mozilla/5.0 (compatible; MarvelRivalsUABot/1.0; +https://t.me/MarvelRivalsUABot)"
_ALBUM_IMAGE_TIMEOUT_SECONDS = 25.0
_ALBUM_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_ALBUM_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


class PublishingError(RuntimeError):
    """Raised when a submission cannot be published to Telegram."""


async def publish_submission(bot: Bot, config: Config, submission: dict[str, Any]) -> None:
    if str(submission.get("message_type") or "") == "album":
        await _publish_album(bot, config, submission)
        return

    parse_mode = "HTML"
    force_single_media_message = False
    parts = _submission_parts(submission)

    try:
        for index, part in enumerate(parts):
            message_type = str(part.get("message_type") or "text")
            part_submission = {**submission, **part, "id": submission["id"], "message_type": message_type}
            draft_text = _format_part_text(str(part.get("text") or ""), part_submission)

            if message_type in {"text", "link"}:
                await _publish_text_or_youtube_video(
                    bot, config, part_submission, draft_text, parse_mode=parse_mode
                )
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


async def _publish_album(bot: Bot, config: Config, submission: dict[str, Any]) -> None:
    images = album_image_urls(submission)
    caption = album_caption_html(submission)
    try:
        await send_album_message(bot, config.publish_chat_id, images, caption)
    except (TelegramAPIError, TimeoutError, ClientOSError) as exc:
        raise PublishingError("Telegram API rejected the album publish request") from exc

    logger.info("Published album submission %s to chat %s", submission["id"], config.publish_chat_id)


async def _publish_text_or_youtube_video(
    bot: Bot,
    config: Config,
    submission: dict[str, Any],
    text: str,
    *,
    parse_mode: str | None,
) -> None:
    link_preview = link_preview_options_for(submission)
    source_type = str(submission.get("source_type") or "")
    source_url = str(submission.get("source_url") or "").strip()
    if (
        config.enable_youtube_video_download
        and source_type in PREVIEW_LINK_SOURCE_TYPES
        and _is_safe_http_url(source_url)
    ):
        await send_youtube_post(
            bot,
            config.publish_chat_id,
            source_url,
            text,
            parse_mode=parse_mode,
            link_preview=link_preview,
            max_bytes=max(1, config.youtube_video_max_mb) * 1024 * 1024,
        )
        return

    await _send_text(bot, config.publish_chat_id, text, parse_mode=parse_mode, link_preview=link_preview)


async def send_youtube_post(
    bot: Bot,
    chat_id: int,
    source_url: str,
    caption: str,
    *,
    parse_mode: str | None,
    link_preview: LinkPreviewOptions,
    max_bytes: int,
):
    """Send a YouTube post as a NATIVE inline video (downloaded + re-uploaded),
    falling back to a text post with a playable link preview when the video is
    unavailable, too large, or the upload is rejected. Returns the primary sent
    message so callers (moderation) can record it."""
    video = await download_youtube_video(source_url, max_bytes=max_bytes)
    if video is not None:
        data, filename = video
        try:
            return await _send_video_bytes(bot, chat_id, data, filename, caption, parse_mode=parse_mode)
        except (TelegramAPIError, TimeoutError, ClientOSError) as exc:
            logger.warning("Native YouTube video upload failed (%s); falling back to link preview.", exc)

    return await _send_text(bot, chat_id, caption, parse_mode=parse_mode, link_preview=link_preview)


async def _send_video_bytes(
    bot: Bot,
    chat_id: int,
    data: bytes,
    filename: str,
    caption: str,
    *,
    parse_mode: str | None,
):
    caption = caption.strip()
    caption_fits = bool(caption) and _telegram_visible_length(caption, parse_mode=parse_mode) <= TELEGRAM_CAPTION_LIMIT
    file = BufferedInputFile(data, filename=filename)

    if caption_fits:
        return await send_with_retries(
            bot.send_video,
            label="publish youtube video",
            chat_id=chat_id,
            video=file,
            caption=caption or None,
            parse_mode=parse_mode,
            supports_streaming=True,
        )

    video_message = await send_with_retries(
        bot.send_video,
        label="publish youtube video",
        chat_id=chat_id,
        video=file,
        supports_streaming=True,
    )
    if caption:
        await delay_between_telegram_sends()
        await _send_text(bot, chat_id, caption, parse_mode=parse_mode)
    return video_message


def link_preview_options_for(submission: dict[str, Any]) -> LinkPreviewOptions:
    """Enable a large, playable link preview of the source URL for sources in
    PREVIEW_LINK_SOURCE_TYPES (YouTube); disable previews for everything else."""
    source_type = str(submission.get("source_type") or "")
    source_url = str(submission.get("source_url") or "").strip()
    if source_type in PREVIEW_LINK_SOURCE_TYPES and _is_safe_http_url(source_url):
        return LinkPreviewOptions(
            is_disabled=False, url=source_url, prefer_large_media=True, show_above_text=True
        )
    return DISABLED_LINK_PREVIEW


def album_caption_html(submission: dict[str, Any]) -> str:
    """The album caption as HTML. Digest captions are pre-rendered HTML (sent as-is);
    collector-album captions are plain Gemini text run through the post formatter."""
    text = str(submission.get("draft_text") or "").strip()
    if str(submission.get("source_type") or "") in PRERENDERED_ALBUM_SOURCE_TYPES:
        return text
    return _format_part_text(text, submission)


def _is_safe_http_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def album_image_urls(submission: dict[str, Any]) -> list[str]:
    """The de-duplicated image URLs of an ``album`` submission (one per part),
    capped at Telegram's media-group maximum of 10."""
    urls: list[str] = []
    seen: set[str] = set()
    for part in submission.get("parts") or []:
        url = str(part.get("media_url") or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls:
        single = str(submission.get("media_url") or "").strip()
        if single:
            urls.append(single)
    return urls[:10]


async def send_album_message(
    bot: Bot,
    chat_id: int,
    images: list[str],
    caption: str,
    *,
    parse_mode: str | None = "HTML",
) -> None:
    """Send an image album to ``chat_id``. Images are downloaded and uploaded as
    bytes (unreachable / non-image / oversized ones are skipped). A media group
    needs 2-10 items, so a single usable image is sent as a plain photo. The
    caption rides on the first item when it fits Telegram's 1024 limit, otherwise
    it follows as a separate text message."""
    if not images:
        raise PublishingError("Album submission has no images")

    photos = await _download_album_photos(images)
    if not photos:
        raise PublishingError("None of the album images could be downloaded as valid photos")

    caption = caption.strip()
    caption_fits = bool(caption) and _telegram_visible_length(caption, parse_mode=parse_mode) <= TELEGRAM_CAPTION_LIMIT

    # A media group is atomic: if Telegram rejects one image (e.g. invalid
    # dimensions, which the size/content-type guard can't catch), the whole send
    # fails. So drop the offending item (Telegram names it as "message #N") and
    # retry, rather than losing the entire album.
    remaining = list(photos)
    while remaining:
        try:
            if len(remaining) == 1:
                data, filename = remaining[0]
                await send_with_retries(
                    bot.send_photo,
                    label="publish album single photo",
                    chat_id=chat_id,
                    photo=BufferedInputFile(data, filename=filename),
                    **({"caption": caption, "parse_mode": parse_mode} if caption_fits else {}),
                )
            else:
                await send_with_retries(
                    bot.send_media_group,
                    label="publish album media group",
                    chat_id=chat_id,
                    media=_build_album_media(remaining, caption, caption_fits=caption_fits, parse_mode=parse_mode),
                )
            break
        except TelegramBadRequest as exc:
            index = _failing_media_index(str(exc), len(remaining))
            dropped = remaining.pop(index)
            logger.warning(
                "Telegram rejected album image %s (%s); dropping it and retrying with %s left.",
                dropped[1],
                exc,
                len(remaining),
            )
            if not remaining:
                raise PublishingError("Every album image was rejected by Telegram") from exc

    if caption and not caption_fits:
        await delay_between_telegram_sends()
        await _send_text(bot, chat_id, caption, parse_mode=parse_mode)


def _build_album_media(
    photos: list[tuple[bytes, str]],
    caption: str,
    *,
    caption_fits: bool,
    parse_mode: str | None,
) -> list[InputMediaPhoto]:
    media: list[InputMediaPhoto] = []
    for index, (data, filename) in enumerate(photos):
        file = BufferedInputFile(data, filename=filename)
        if index == 0 and caption_fits:
            media.append(InputMediaPhoto(media=file, caption=caption, parse_mode=parse_mode))
        else:
            media.append(InputMediaPhoto(media=file))
    return media


def _failing_media_index(error_text: str, count: int) -> int:
    """The 0-based index Telegram blamed in a media-group error ("...message #N..."),
    or 0 when the message names no usable index."""
    match = re.search(r"message #(\d+)", error_text)
    if match:
        index = int(match.group(1)) - 1
        if 0 <= index < count:
            return index
    return 0


def _is_fetchable_media_url(url: str) -> bool:
    """Guard the bot-side album download against SSRF. Require https, and reject an
    IP-literal host in a non-public range (loopback / private / link-local /
    reserved) — e.g. the cloud metadata endpoint 169.254.169.254 or 127.0.0.1.
    Hostnames pass through: redirects are disabled on the download client and the
    collectors already allowlist their image CDN host, so the only thing left to
    block here is an explicit internal-IP target."""
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https":
        return False
    host = parsed.hostname or ""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True  # a DNS hostname, not an IP literal


async def _download_album_photos(images: list[str]) -> list[tuple[bytes, str]]:
    photos: list[tuple[bytes, str]] = []
    # follow_redirects=False so an allowlisted-looking URL cannot 302 to an internal
    # host — the redirect target would otherwise be an unchecked SSRF target.
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(_ALBUM_IMAGE_TIMEOUT_SECONDS),
        headers={"User-Agent": _ALBUM_IMAGE_USER_AGENT},
    ) as client:
        for index, url in enumerate(images):
            photo = await _download_album_photo(client, url, index)
            if photo is not None:
                photos.append(photo)
    return photos


async def _download_album_photo(client: httpx.AsyncClient, url: str, index: int) -> tuple[bytes, str] | None:
    if not _is_fetchable_media_url(url):
        logger.warning("Skipping album image with a disallowed URL: %s", url)
        return None

    try:
        # Stream so an oversized body is aborted mid-download instead of being fully
        # buffered into memory first (a non-image internal/huge response otherwise
        # costs up to its full size in RAM before the post-hoc length check).
        async with client.stream("GET", url) as response:
            response.raise_for_status()

            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type not in _ALBUM_ALLOWED_CONTENT_TYPES:
                logger.warning("Skipping album image with unsupported content-type %r: %s", content_type, url)
                return None

            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > _ALBUM_MAX_IMAGE_BYTES:
                logger.warning("Skipping album image (declared %s bytes) over the upload limit: %s", declared, url)
                return None

            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > _ALBUM_MAX_IMAGE_BYTES:
                    logger.warning("Skipping album image over the %s-byte upload limit: %s", _ALBUM_MAX_IMAGE_BYTES, url)
                    return None
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Album image download failed for %s: %s", url, exc)
        return None

    if not buffer:
        logger.warning("Skipping empty album image: %s", url)
        return None

    extension = "png" if content_type == "image/png" else ("webp" if content_type == "image/webp" else "jpg")
    return (bytes(buffer), f"art_{index + 1}.{extension}")


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
        allow_source_link=submission_allows_source_link(submission),
        include_community_footer=True,
    )


async def _send_text(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
    link_preview: LinkPreviewOptions = DISABLED_LINK_PREVIEW,
) -> None:
    text = text.strip()
    if not text:
        raise PublishingError("Text submission has no draft text")

    if parse_mode is not None:
        if _telegram_visible_length(text, parse_mode=parse_mode) > TELEGRAM_TEXT_LIMIT:
            raise PublishingError("Text submission exceeds Telegram single-message limit")
        return await send_with_retries(
            bot.send_message,
            label="publish text message",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            link_preview_options=link_preview,
        )

    chunks = split_text(text, TELEGRAM_TEXT_LIMIT)
    for index, chunk in enumerate(chunks):
        await send_with_retries(
            bot.send_message,
            label="publish text chunk",
            chat_id=chat_id,
            text=chunk,
            link_preview_options=link_preview,
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
