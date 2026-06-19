from __future__ import annotations

import logging

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.base import CollectorDefinition, DraftCandidate, ListingEntry
from services.collectors.bluesky.feed_fetcher import BlueskyFeedFetcher, BlueskyPost
from services.collectors.bluesky.video import resolve_video_blob_url
from services.collectors.runner import BaseNewsCollector
from services.i18n import t


logger = logging.getLogger(__name__)

COLLECTOR_ID = "bluesky"
SOURCE_TYPE = "bluesky"

TITLE_MAX_LENGTH = 120
FALLBACK_TITLE = "Допис Marvel Rivals (Bluesky)"

DEFINITION = CollectorDefinition(
    collector_id=COLLECTOR_ID,
    source_type=SOURCE_TYPE,
    title_key="collectors.bluesky.title",
    button_key="buttons.collector_bluesky",
)


class BlueskyCollector(BaseNewsCollector):
    definition = DEFINITION

    def __init__(self, *, config: Config, db: Database, bot: Bot) -> None:
        super().__init__(config=config, db=db, bot=bot)
        self.fetcher = BlueskyFeedFetcher(config.bluesky_actor)

    def missing_gemini_warning(self) -> str:
        return t("collectors.bluesky.errors.missing_gemini_api_key")

    async def fetch_listing(self) -> list[ListingEntry]:
        posts = await self.fetcher.fetch_recent_posts()
        return [
            ListingEntry(dedup_key=post.uri, payload=post)
            for post in posts
            if post.text or post.image_urls
        ]

    async def parse_entry(self, entry: ListingEntry) -> DraftCandidate:
        post: BlueskyPost = entry.payload  # type: ignore[assignment]
        title = _derive_title(post.text)
        has_media, media_type, media_url, additional_media_urls = await self._resolve_media(post)

        return DraftCandidate(
            source_id=post.uri,
            source_url=post.web_url,
            title=title,
            body_text=post.text,
            source_name=t("collectors.bluesky.source_name"),
            username=t("collectors.bluesky.username"),
            original_text=_build_original_text(post, title),
            article_date=post.created_at,
            article_date_display=_display_date(post.created_at),
            has_media=has_media,
            media_url=media_url,
            media_type=media_type,
            additional_media_urls=additional_media_urls,
        )

    async def _resolve_media(self, post: BlueskyPost) -> tuple[bool, str, str | None, list[str] | None]:
        """Return (has_media, media_type, media_url, additional_urls). Photos win
        when present; otherwise a video post is resolved to its direct MP4 blob URL
        (downloaded + re-uploaded natively at send time). Falls back to a text post
        when video download is disabled or the blob URL cannot be resolved."""
        if post.image_urls:
            return True, "photo", post.image_urls[0], list(post.image_urls[1:]) or None

        download_enabled = getattr(self.config, "enable_bluesky_video_download", True)
        if post.has_video and post.video_cid and download_enabled:
            blob_url = await resolve_video_blob_url(post.author_did, post.video_cid)
            if blob_url:
                return True, "video", blob_url, None

        return False, "none", None, None


def _derive_title(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        return FALLBACK_TITLE

    if len(first_line) <= TITLE_MAX_LENGTH:
        return first_line

    truncated = first_line[:TITLE_MAX_LENGTH].rsplit(" ", 1)[0].strip()
    return f"{truncated or first_line[:TITLE_MAX_LENGTH].strip()}…"


def _display_date(created_at: str | None) -> str | None:
    if not created_at:
        return None

    return created_at[:10]


def _build_original_text(post: BlueskyPost, title: str) -> str:
    parts = [
        t("collectors.common.original_text.article_title", value=title),
        t("collectors.common.original_text.article_url", value=post.web_url),
    ]

    if post.created_at:
        parts.append(t("collectors.common.original_text.original_article_date", value=post.created_at))

    if post.text:
        parts.extend(["", t("collectors.common.original_text.parsed_article_text"), post.text])

    if post.image_urls:
        media_lines = [
            t("collectors.common.original_text.media_url", value=image_url) for image_url in post.image_urls
        ]
        parts.extend(["", *media_lines, t("collectors.common.original_text.media_type", value="photo")])
    elif post.has_video:
        parts.extend(["", t("collectors.common.original_text.media_type", value="video")])

    return "\n".join(parts).strip()
