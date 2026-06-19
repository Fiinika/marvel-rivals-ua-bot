from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.base import CollectorDefinition, DraftCandidate, ListingEntry
from services.collectors.runner import BaseNewsCollector
from services.collectors.youtube.feed_fetcher import YouTubeFeedFetcher, YouTubeVideo
from services.i18n import t


logger = logging.getLogger(__name__)

COLLECTOR_ID = "youtube"
SOURCE_TYPE = "youtube"

TITLE_MAX_LENGTH = 140
FALLBACK_TITLE = "Нове відео Marvel Rivals (YouTube)"

DEFINITION = CollectorDefinition(
    collector_id=COLLECTOR_ID,
    source_type=SOURCE_TYPE,
    title_key="collectors.youtube.title",
    button_key="buttons.collector_youtube",
)


class YouTubeCollector(BaseNewsCollector):
    definition = DEFINITION

    def __init__(self, *, config: Config, db: Database, bot: Bot) -> None:
        super().__init__(config=config, db=db, bot=bot)
        self.fetcher = YouTubeFeedFetcher(config.youtube_channel_id)
        self.exclude_keywords = config.youtube_exclude_keywords

    def missing_gemini_warning(self) -> str:
        return t("collectors.youtube.errors.missing_gemini_api_key")

    async def fetch_listing(self) -> list[ListingEntry]:
        videos = await self.fetcher.fetch_recent_videos()
        best_by_key: dict[str, YouTubeVideo] = {}
        order: list[str] = []
        for video in videos:
            if not video.title and not video.description:
                continue
            if _is_excluded(video.title, self.exclude_keywords):
                logger.info("Skipping excluded YouTube video %r", video.title)
                continue

            # Collapse the channel's re-uploads of the same trailer (different video
            # IDs, identical normalised title) to a single entry. Among the collapsed
            # variants prefer the one that carries a description: the channel ships
            # the same trailer both as a Short (no description) and as a full /watch
            # upload (full description), and the AI draft needs that text — so a
            # description-less Short must never hide the richer upload, regardless of
            # feed order. Otherwise keep the newest (videos are sorted newest-first).
            dedup_key = _dedup_key(video)
            existing = best_by_key.get(dedup_key)
            if existing is None:
                best_by_key[dedup_key] = video
                order.append(dedup_key)
            elif _is_richer_variant(video, existing):
                best_by_key[dedup_key] = video

        return [ListingEntry(dedup_key=key, payload=best_by_key[key]) for key in order]

    async def parse_entry(self, entry: ListingEntry) -> DraftCandidate:
        video: YouTubeVideo = entry.payload  # type: ignore[assignment]
        title = _derive_title(video.title)

        # No attached photo: a YouTube post is published as text with the watch link
        # so Telegram renders a PLAYABLE video preview instead of a static thumbnail
        # (the publisher enables the link preview for the "youtube" source).
        return DraftCandidate(
            # source_id mirrors the normalised-title dedup_key so a later re-upload of
            # the same trailer (new video id) is recognised as already seen.
            source_id=entry.dedup_key,
            source_url=video.web_url,
            title=title,
            body_text=video.description,
            source_name=t("collectors.youtube.source_name"),
            username=t("collectors.youtube.username"),
            original_text=_build_original_text(video, title),
            article_date=video.published,
            article_date_display=_display_date(video.published),
            has_media=False,
            media_url=None,
            media_type="none",
            additional_media_urls=None,
        )


def _normalize_title(title: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", title.casefold(), flags=re.UNICODE)
    return " ".join(cleaned.split())


def _dedup_key(video: YouTubeVideo) -> str:
    normalized = _normalize_title(video.title)
    if normalized:
        # Scope the title key to the publish DAY: the channel's same-trailer
        # re-uploads land minutes apart (same day) so they still collapse, while a
        # verbatim title legitimately reused on another day is treated as new
        # rather than dropped forever (seen_sources has no TTL).
        day = video.published[:10] if video.published else ""
        return f"yt-title:{day}:{normalized}"
    return f"yt-video:{video.video_id}"


def _is_richer_variant(candidate: YouTubeVideo, current: YouTubeVideo) -> bool:
    """Whether ``candidate`` should replace an already-collapsed ``current`` of the
    same trailer. Prefer a variant that actually carries a description (the full
    /watch upload) over a description-less one (a Short). Equal cases keep the
    first-seen (newest) variant."""
    return bool(candidate.description.strip()) and not current.description.strip()


def _is_excluded(title: str, exclude_keywords: Iterable[str]) -> bool:
    if not title:
        return False
    lowered = title.casefold()
    return any(keyword in lowered for keyword in exclude_keywords)


def _derive_title(title: str) -> str:
    stripped = title.strip()
    if not stripped:
        return FALLBACK_TITLE

    if len(stripped) <= TITLE_MAX_LENGTH:
        return stripped

    truncated = stripped[:TITLE_MAX_LENGTH].rsplit(" ", 1)[0].strip()
    return f"{truncated or stripped[:TITLE_MAX_LENGTH].strip()}…"


def _display_date(published: str | None) -> str | None:
    if not published:
        return None

    return published[:10]


def _build_original_text(video: YouTubeVideo, title: str) -> str:
    parts = [
        t("collectors.common.original_text.article_title", value=title),
        t("collectors.common.original_text.article_url", value=video.web_url),
    ]

    if video.published:
        parts.append(t("collectors.common.original_text.original_article_date", value=video.published))

    if video.description:
        parts.extend(["", t("collectors.common.original_text.parsed_article_text"), video.description])

    if video.thumbnail_url:
        parts.extend(
            [
                "",
                t("collectors.common.original_text.media_url", value=video.thumbnail_url),
                t("collectors.common.original_text.media_type", value="photo (прев'ю відео)"),
            ]
        )

    return "\n".join(parts).strip()
